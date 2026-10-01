# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Exercise continuation transitions through real checkpoint publication."""

import json
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message
from openviking.session import work_items as wi
from openviking.session.continuation_state import confirm_continuation_promotions
from openviking.session.work_item_budget import WorkItemBudgets
from openviking.utils.token_estimation import estimate_text_tokens
from tests.unit.session.test_work_item_checkpoint import (
    _persist_canonical_work_item,
    archive,
    item,
    message,
    session_with_fs,
)


def transition(action, source_ids=(), *, identity="", summary="", reason="", target=""):
    return {
        "action": action,
        "continuation_id": identity,
        "source_message_ids": list(source_ids),
        "summary": summary,
        "reason": reason,
        **({"work_item_uri": target} if target else {}),
    }


def store_transitions(fs, uri, entries, **metadata):
    meta = json.loads(fs.files[f"{uri}/.meta.json"])
    meta.update(
        continuation_coverage=confirm_continuation_promotions(
            entries, metadata.get("work_item_coverage", [])
        ),
        **metadata,
    )
    fs.files[f"{uri}/.meta.json"] = json.dumps(meta)


async def prepare(session, fs, number, fresh, previous, transitions, **metadata):
    uri = archive(fs, number, fresh)
    store_transitions(fs, uri, transitions, **metadata)
    inputs = [Message.from_dict(value) for value in previous.get("residual", [])] + fresh
    done = await session._prepare_work_item_checkpoint(uri, inputs, previous)
    return uri, done


async def publish(session, uri, done):
    await session._write_done_file(uri, "first", "last", checkpoint=done)


@pytest.mark.asyncio
async def test_many_rounds_replace_current_issue_and_preserve_omitted_issue():
    session, fs = session_with_fs()
    first = [message("tests", "Tests pending."), message("approval", "Approval is required.")]
    uri, previous = await prepare(
        session,
        fs,
        1,
        first,
        {},
        [
            transition("create", [first[0].id], summary="Tests pending."),
            transition("create", [first[1].id], summary="Do not deploy without approval."),
        ],
    )
    await publish(session, uri, previous)
    original = fs.files[f"{uri}/messages.jsonl"]
    initial = {Message.from_dict(value).content: value["id"] for value in previous["residual"]}
    progress_id = next(
        identity for content, identity in initial.items() if "Tests pending" in content
    )
    approval_id = next(
        identity for content, identity in initial.items() if "Do not deploy" in content
    )

    for number in range(2, 8):
        fresh = message(
            f"progress-{number}", f"Test stage {number} passed, continue the next stage."
        )
        uri, current = await prepare(
            session,
            fs,
            number,
            [fresh],
            previous,
            [
                transition(
                    "update",
                    [fresh.id],
                    identity=progress_id,
                    summary=f"Test stage {number} passed; next stage pending.",
                )
            ],
        )
        await publish(session, uri, current)
        assert {value["id"] for value in current["residual"]} == {progress_id, approval_id}
        assert all(value.get("continuation_state_version") == 1 for value in current["residual"])
        overview = fs.files[f"{uri}/.overview.md"]
        assert overview.count("## Continuation") == 1
        assert overview.count("Do not deploy without approval.") == 1
        assert overview.count("Previous continuation summary") <= 1
        assert overview.count("background, not new user evidence") <= 1
        assert f"Test stage {number} passed; next stage pending." in overview
        assert "Tests pending." not in overview
        if number > 2:
            assert f"Test stage {number - 1} passed; next stage pending." not in overview
        assert not any(value.get("source_message_ids") for value in current["residual"])
        previous = current

    assert fs.files[f"{session._session_uri}/history/archive_001/messages.jsonl"] == original
    # A valid empty response leaves existing issues unchanged, without duplicate copies.
    uri, unchanged = await prepare(session, fs, 8, [message("chitchat")], previous, [])
    assert {value["id"] for value in unchanged["residual"]} == {progress_id, approval_id}
    assert len(unchanged["residual"]) == 2


@pytest.mark.asyncio
async def test_resolved_issue_leaves_hot_view_but_archive_keeps_history_and_reason():
    session, fs = session_with_fs()
    source = message("request", "Wait for the test result.")
    old_uri, previous = await prepare(
        session,
        fs,
        1,
        [source],
        {},
        [transition("create", [source.id], summary="Test result pending.")],
    )
    await publish(session, old_uri, previous)
    historical_done = fs.files[f"{old_uri}/.done"]
    identity = previous["residual"][0]["id"]
    evidence = message("test-result", "All requested tests passed; no further check is required.")
    reason = "All requested tests passed; the pending verification is complete."
    uri, done = await prepare(
        session,
        fs,
        2,
        [evidence],
        previous,
        [transition("resolve", [evidence.id], identity=identity, reason=reason)],
    )
    await publish(session, uri, done)

    assert done["residual"] == []
    assert "Test result pending." not in fs.files[f"{uri}/.overview.md"]
    assert reason in json.dumps(done["coverage"])
    assert fs.files[f"{old_uri}/.done"] == historical_done
    assert fs.files[f"{old_uri}/messages.jsonl"] == source.to_jsonl()


@pytest.mark.asyncio
async def test_promotion_waits_for_matching_receipt_and_readable_canonical_state():
    session, fs = session_with_fs()
    source = message("request", "Investigate the failing import.")
    old_uri, previous = await prepare(
        session,
        fs,
        1,
        [source],
        {},
        [transition("create", [source.id], summary="Import investigation pending.")],
    )
    await publish(session, old_uri, previous)
    identity = previous["residual"][0]["id"]
    fresh = message("promote", "Track this investigation as a long-running task.")
    state = item("import-investigation")
    promotion = transition(
        "promote",
        [fresh.id],
        identity=identity,
        reason="The investigation now has a durable work item.",
        target=state["uri"],
    )
    uri, pending = await prepare(session, fs, 2, [fresh], previous, [promotion])
    assert [value["id"] for value in pending["residual"]] == [identity]

    store_transitions(
        fs,
        uri,
        [promotion],
        work_items=[{"uri": state["uri"], "version": 1}],
        work_item_coverage=[{"uri": state["uri"], "source_message_ids": [identity, fresh.id]}],
    )
    inputs = [Message.from_dict(previous["residual"][0]), fresh]
    with pytest.raises(FileNotFoundError):
        await session._prepare_work_item_checkpoint(uri, inputs, previous)
    assert f"{uri}/.done" not in fs.files

    _persist_canonical_work_item(fs, state)
    promoted = await session._prepare_work_item_checkpoint(uri, inputs, previous)
    await publish(session, uri, promoted)
    assert promoted["residual"] == []
    assert promoted["active_work_items"][0]["uri"] == state["uri"]
    assert "Import investigation pending." in fs.files[f"{old_uri}/.done"]


@pytest.mark.asyncio
async def test_publication_retry_preserves_created_identity_without_duplicate_issues():
    session, fs = session_with_fs()
    fresh = message("approval", "Wait for approval.")
    uri, first = await prepare(
        session,
        fs,
        1,
        [fresh],
        {},
        [transition("create", [fresh.id], summary="Approval is still required.")],
    )
    fs._async_agfs.mv.side_effect = RuntimeError("rename temporarily unavailable")
    with pytest.raises(RuntimeError, match="rename"):
        await publish(session, uri, first)
    assert f"{uri}/.done" not in fs.files
    provenance = fs.files[f"{uri}/continuation-provenance.json"]

    retried = await session._prepare_work_item_checkpoint(uri, [fresh], {})
    fs._async_agfs.mv.side_effect = fs.move
    await publish(session, uri, retried)
    assert retried["residual"] == first["residual"]
    assert len(retried["residual"]) == 1
    assert fs.files[f"{uri}/continuation-provenance.json"] == provenance
    assert fs.files[f"{uri}/.overview.md"].count("Approval is still required.") == 1


@pytest.mark.asyncio
async def test_failed_archive_updates_are_inherited_in_order_without_resurrecting_old_state():
    session, fs = session_with_fs()
    original = message("first", "Investigation pending.")
    old_uri, previous = await prepare(
        session,
        fs,
        1,
        [original],
        {},
        [transition("create", [original.id], summary="Investigation pending.")],
    )
    await publish(session, old_uri, previous)
    identity = previous["residual"][0]["id"]

    earlier = message("step-two", "Reproduction completed; implementation pending.")
    failed_uri = archive(fs, 2, [earlier], failed=True)
    store_transitions(
        fs,
        failed_uri,
        [
            transition(
                "update",
                [earlier.id],
                identity=identity,
                summary="Reproduction complete; implementation pending.",
            )
        ],
        completed_memory_steps={"long_term": [identity, earlier.id]},
    )
    latest = message("step-three", "Implementation completed; tests pending.")
    uri = archive(fs, 3, [latest])
    store_transitions(
        fs,
        uri,
        [
            transition(
                "update",
                [latest.id],
                identity=identity,
                summary="Implementation complete; tests pending.",
            )
        ],
    )
    completed = {}
    await session._inherit_work_item_progress(uri, previous, completed)
    # A publication retry must not append a second copy of inherited transitions.
    await session._inherit_work_item_progress(uri, previous, completed)
    meta = json.loads(fs.files[f"{uri}/.meta.json"])
    assert len(meta["continuation_coverage"]) == 2
    assert completed["long_term"] == {identity, earlier.id}

    inputs = [Message.from_dict(previous["residual"][0]), earlier, latest]
    done = await session._prepare_work_item_checkpoint(uri, inputs, previous)
    await publish(session, uri, done)
    assert [value["id"] for value in done["residual"]] == [identity]
    overview = fs.files[f"{uri}/.overview.md"]
    assert "Implementation complete; tests pending." in overview
    assert "Reproduction complete; implementation pending." not in overview
    assert "Investigation pending." not in overview
    row = next(value for value in done["coverage"] if value["message_id"] == identity)
    assert [entry["source_message_ids"] for entry in row["continuation_actions"]] == [
        [earlier.id],
        [earlier.id, latest.id],
    ]
    assert fs.files[f"{failed_uri}/messages.jsonl"] == earlier.to_jsonl()


@pytest.mark.asyncio
async def test_state_overflow_archives_whole_old_issue_and_keeps_recent_issue(monkeypatch):
    session, fs = session_with_fs()
    budgets = WorkItemBudgets(continuation_token_budget=400, projection_token_budget=2000)
    monkeypatch.setattr(wi, "get_work_item_budgets", lambda: budgets)
    monkeypatch.setattr(
        session, "_compact_work_item_continuation", AsyncMock(side_effect=RuntimeError("offline"))
    )
    older = message("old", "Keep a detailed approval constraint.")
    recent = message("new", "The test still needs a rerun.")
    long_text = "Prior approval constraint must remain recoverable. " * 1000
    short_text = "Rerun the failing test before continuing."
    uri, done = await prepare(
        session,
        fs,
        1,
        [older, recent],
        {},
        [
            transition("create", [older.id], summary=long_text),
            transition("create", [recent.id], summary=short_text),
        ],
    )
    await publish(session, uri, done)
    assert done["continuation_degraded"] is True
    pending = done["pending_continuation_uri"]
    saved = json.loads(fs.files[pending])
    assert long_text.strip() in wi.residual_text(saved["entries"])
    overview = fs.files[f"{uri}/.overview.md"]
    assert short_text in overview
    assert long_text not in overview
    assert pending in overview
    assert estimate_text_tokens(wi.residual_text(done["residual"], pending)) <= 400
    assert all(value.get("continuation_state_version") == 1 for value in done["residual"])
