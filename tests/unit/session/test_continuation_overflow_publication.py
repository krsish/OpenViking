# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from unittest.mock import AsyncMock

import pytest

from openviking.session import work_items as wi
from openviking.session.work_item_budget import WorkItemBudgets
from openviking.utils.token_estimation import estimate_text_tokens
from tests.unit.session.test_work_item_checkpoint import (
    archive,
    checkpoint,
    message,
    session_with_fs,
    wire_work_item_phase2,
)


@pytest.fixture
def continuation_budgets(monkeypatch):
    budgets = WorkItemBudgets(continuation_token_budget=300, projection_token_budget=2000)
    monkeypatch.setattr(wi, "get_work_item_budgets", lambda: budgets)
    return budgets


def set_continuation(fs, archive_uri, source_id, summary):
    meta = json.loads(fs.files[f"{archive_uri}/.meta.json"])
    meta["continuation_coverage"] = [
        {
            "source_message_ids": [source_id],
            "summary": summary,
            "reason": "" if summary else "This question has been answered.",
        }
    ]
    fs.files[f"{archive_uri}/.meta.json"] = json.dumps(meta)


async def publish(session, archive_uri, source, previous):
    result = await session._prepare_work_item_checkpoint(archive_uri, [source], previous)
    await session._write_done_file(archive_uri, source.id, source.id, checkpoint=result)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "empty", "over_budget"])
async def test_compaction_failure_publishes_bounded_checkpoint_with_complete_overflow(
    monkeypatch, continuation_budgets, failure
):
    session, fs = session_with_fs()
    source = message("approval", "Approval is required before deployment.")
    uri = archive(fs, 1, [source])
    summary = "Deployment still needs user approval; preserve this constraint. " * 1000
    extractor = AsyncMock(
        return_value={
            "continuation_coverage": [
                {"source_message_ids": [source.id], "summary": summary, "reason": ""}
            ]
        }
    )
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    if failure == "network":
        compactor = AsyncMock(side_effect=RuntimeError("VLM unavailable"))
    elif failure == "empty":
        compactor = AsyncMock(return_value=[])
    else:
        compactor = AsyncMock(
            return_value=[wi.continuation_message("still too long " * 10000, uri, [], None)]
        )
    monkeypatch.setattr(session, "_compact_work_item_continuation", compactor)
    policy = {"working_memory": {"mode": "work_item"}, "memory_types": ["work_item"]}

    await session._run_memory_extraction("overflow", uri, [source], source.id, source.id, policy)

    tracker.fail.assert_not_awaited()
    tracker.complete.assert_awaited_once()
    extractor.assert_awaited_once()
    compactor.assert_awaited_once()
    done = json.loads(fs.files[f"{uri}/.done"])
    pending_uri = f"{uri}/continuation-overflow.json"
    assert done["compact_ready"]
    assert done["continuation_degraded"] is True
    assert done["pending_continuation_uri"] == pending_uri
    assert estimate_text_tokens(wi.residual_text(done["residual"])) <= 300
    overflow = json.loads(fs.files[pending_uri])
    assert overflow["version"] == 1
    assert not overflow.get("previous_pending_continuation_uri")
    assert overflow["entries"] == compactor.call_args.args[0]
    assert summary.strip() in wi.residual_text(overflow["entries"])
    assert overflow["entries"][0]["parts"][0]["text"] == summary
    assert overflow["reason"]
    # No entry fits by itself. Publish the recovery instruction, never a sliced
    # prefix that could silently remove the deployment constraint.
    assert summary not in fs.files[f"{uri}/.overview.md"]
    assert pending_uri in fs.files[f"{uri}/.overview.md"]
    public = await session.get_session_archive("archive_001")
    assert public["status"] == "ready"
    assert pending_uri in public["overview"]
    assert estimate_text_tokens(public["overview"]) <= continuation_budgets.projection_token_budget
    assert fs.files[f"{uri}/messages.jsonl"] == source.to_jsonl()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["Rerun the failing test.", ""])
async def test_next_checkpoint_preserves_pending_overflow_after_short_or_empty_extraction(
    monkeypatch, continuation_budgets, summary
):
    session, fs = session_with_fs()
    old_source = message("old", "Deployment approval is still missing.")
    previous = checkpoint()
    old_uri = archive(fs, 1, [old_source], previous)
    pending_uri = f"{old_uri}/continuation-overflow.json"
    previous.update(pending_continuation_uri=pending_uri, continuation_degraded=True)
    fs.files[f"{old_uri}/.done"] = json.dumps(previous)
    fs.files[pending_uri] = json.dumps(
        {"version": 1, "entries": [old_source.to_dict()], "reason": "VLM unavailable"}
    )
    fresh = message("fresh", "The quick question has been answered.")
    uri = archive(fs, 2, [fresh])
    set_continuation(fs, uri, fresh.id, summary)
    compactor = AsyncMock(side_effect=AssertionError("No model repair is needed"))
    monkeypatch.setattr(session, "_compact_work_item_continuation", compactor)

    done = await publish(session, uri, fresh, previous)

    compactor.assert_not_awaited()
    assert done["pending_continuation_uri"] == pending_uri
    assert done["continuation_degraded"] is True
    assert pending_uri in fs.files[f"{uri}/.overview.md"]
    public = await session.get_session_archive("archive_002")
    assert public["status"] == "ready"
    assert pending_uri in public["overview"]
    assert estimate_text_tokens(public["overview"]) <= continuation_budgets.projection_token_budget
    if summary:
        assert summary in public["overview"]


@pytest.mark.asyncio
async def test_repeated_overflow_links_previous_artifact_without_growing_hot_pointer_list(
    monkeypatch, continuation_budgets
):
    session, fs = session_with_fs()
    compactor = AsyncMock(side_effect=RuntimeError("VLM unavailable"))
    monkeypatch.setattr(session, "_compact_work_item_continuation", compactor)
    previous = {}
    pending_uris = []
    for number in range(1, 4):
        source = message(f"source-{number}")
        uri = archive(fs, number, [source])
        set_continuation(fs, uri, source.id, f"Unresolved constraint {number}. " * 1000)
        done = await publish(session, uri, source, previous)
        pending_uri = f"{uri}/continuation-overflow.json"
        overflow = json.loads(fs.files[pending_uri])
        if pending_uris:
            assert overflow["previous_pending_continuation_uri"] == pending_uris[-1]
        else:
            assert not overflow.get("previous_pending_continuation_uri")
        assert done["pending_continuation_uri"] == pending_uri
        assert done["continuation_degraded"] is True
        hot = json.dumps(done["residual"])
        overview = fs.files[f"{uri}/.overview.md"]
        assert pending_uri in overview
        for prior in pending_uris:
            assert prior not in hot
            assert prior not in overview
        assert estimate_text_tokens(wi.residual_text(done["residual"])) <= 300
        pending_uris.append(pending_uri)
        previous = done


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_artifact", ["continuation-overflow.json", "continuation-provenance.json"]
)
async def test_failed_durable_continuation_write_does_not_publish_checkpoint(
    monkeypatch, continuation_budgets, failed_artifact
):
    session, fs = session_with_fs()
    source = message("approval", "Deployment must wait for approval.")
    uri = archive(fs, 1, [source])
    summary = "Deployment must wait for approval."
    if failed_artifact == "continuation-overflow.json":
        summary *= 1000
    extractor = AsyncMock(
        return_value={
            "continuation_coverage": [
                {"source_message_ids": [source.id], "summary": summary, "reason": ""}
            ]
        }
    )
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    monkeypatch.setattr(
        session,
        "_compact_work_item_continuation",
        AsyncMock(side_effect=RuntimeError("VLM unavailable")),
    )
    write = fs.write_file
    attempted = []

    async def fail_artifact_write(uri, content, ctx=None, **kwargs):
        attempted.append(uri)
        if uri.endswith(failed_artifact):
            raise OSError("archive storage unavailable")
        return await write(uri, content, ctx=ctx, **kwargs)

    monkeypatch.setattr(fs, "write_file", fail_artifact_write)
    policy = {"working_memory": {"mode": "work_item"}, "memory_types": ["work_item"]}

    await session._run_memory_extraction(
        "storage-failure", uri, [source], source.id, source.id, policy
    )

    assert f"{uri}/{failed_artifact}" in attempted
    assert f"{uri}/.done" not in fs.files
    assert f"{uri}/.failed.json" in fs.files
    tracker.complete.assert_not_awaited()
    tracker.fail.assert_awaited_once()
    assert fs.files[f"{uri}/messages.jsonl"] == source.to_jsonl()
