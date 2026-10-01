# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Continuation state is shared across batches without changing source boundaries."""

import json
from unittest.mock import AsyncMock

import pytest

from openviking.session.memory.memory_updater import ExtractContext
from openviking.session.work_items import continuation_message, resolve_continuation_coverage
from tests.unit.session.test_work_item_checkpoint import (
    archive,
    checkpoint,
    message,
    session_with_fs,
    wire_work_item_phase2,
)

POLICY = {"working_memory": {"mode": "work_item"}, "memory_types": ["work_item"]}
BATCHING = {"message_count_threshold": 2, "pending_token_threshold": 0}


def publish_initial(fs):
    previous = checkpoint()
    old = continuation_message(
        "Tests pending; deployment still requires approval.",
        "viking://user/default/sessions/work-items/history/archive_001",
        ["original-request"],
        "2026-01-01T00:00:00Z",
    )
    old.pop("source_message_ids")
    previous["residual"] = [old]
    archive(fs, 1, [message("original-request")], previous)
    return previous, old["id"]


def resolve_action(kwargs, action, identity, fresh_id, **fields):
    # The compressor separately combines current background with this batch.
    # Resolve against that real context; the original source list is untouched.
    background = kwargs["continuation_background"]
    fresh = [value for value in kwargs["messages"] if value.message_kind != "checkpoint"]
    context = ExtractContext([*background, *fresh])
    source = next(index for index, value in enumerate(context.messages) if value.id == fresh_id)
    return resolve_continuation_coverage(
        context,
        [{"action": action, "continuation_id": identity, "ranges": str(source), **fields}],
    )


@pytest.mark.asyncio
async def test_second_batch_can_resolve_old_item_using_the_first_batchs_latest_state(monkeypatch):
    session, fs = session_with_fs()
    _, identity = publish_initial(fs)
    fresh = [message("tests-passed", "Tests passed."), message("approval", "Deployment approved.")]
    uri = archive(fs, 2, fresh)
    calls = []

    async def extract(**kwargs):
        ids = [value.id for value in kwargs["messages"]]
        background = kwargs["continuation_background"]
        assert len(background) == 1
        assert background[0].id == identity
        assert background[0].role == "assistant"
        assert background[0].message_kind == "checkpoint"
        assert not background[0].source_message_ids
        calls.append((ids, background[0].content))
        if "tests-passed" in ids:
            assert background[0].content == "Tests pending; deployment still requires approval."
            actions = resolve_action(
                kwargs,
                "update",
                identity,
                "tests-passed",
                summary="Tests passed; deployment still requires approval.",
            )
        else:
            assert ids == ["approval"]
            assert background[0].content == "Tests passed; deployment still requires approval."
            actions = resolve_action(
                kwargs,
                "resolve",
                identity,
                "approval",
                reason="Tests passed and deployment approved.",
            )
        return {"continuation_coverage": actions}

    extractor = AsyncMock(side_effect=extract)
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    await session._run_memory_extraction(
        "continuation-batches",
        uri,
        fresh,
        fresh[0].id,
        fresh[-1].id,
        POLICY,
        auto_commit_policy=BATCHING,
    )

    tracker.fail.assert_not_awaited()
    tracker.complete.assert_awaited_once()
    assert [ids for ids, _ in calls] == [[identity, "tests-passed"], ["approval"]]
    assert json.loads(fs.files[f"{uri}/.done"])["residual"] == []
    completed = json.loads(fs.files[f"{uri}/.meta.json"])["completed_memory_steps"]["long_term"]
    assert set(completed) == {identity, "tests-passed", "approval"}


@pytest.mark.asyncio
async def test_failed_archive_completed_ids_do_not_hide_latest_background_from_recovery(
    monkeypatch,
):
    session, fs = session_with_fs()
    _, identity = publish_initial(fs)
    earlier = message("tests-passed", "Tests passed.")
    failed_uri = archive(fs, 2, [earlier], failed=True)
    old_meta = json.loads(fs.files[f"{failed_uri}/.meta.json"])
    old_meta.update(
        continuation_coverage=[
            {
                "action": "update",
                "continuation_id": identity,
                "source_message_ids": [earlier.id],
                "summary": "Tests passed; deployment still requires approval.",
                "reason": "",
            }
        ],
        completed_memory_steps={"long_term": [identity, earlier.id]},
    )
    fs.files[f"{failed_uri}/.meta.json"] = json.dumps(old_meta)
    latest = message("approval", "Deployment approved.")
    uri = archive(fs, 3, [latest])

    async def extract(**kwargs):
        assert [value.id for value in kwargs["messages"]] == [latest.id]
        background = kwargs["continuation_background"]
        assert [(value.id, value.content) for value in background] == [
            (identity, "Tests passed; deployment still requires approval.")
        ]
        return {
            "continuation_coverage": resolve_action(
                kwargs,
                "resolve",
                identity,
                latest.id,
                reason="Tests passed and deployment approved.",
            )
        }

    extractor = AsyncMock(side_effect=extract)
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    await session._run_memory_extraction(
        "continuation-recovery",
        uri,
        [latest],
        latest.id,
        latest.id,
        POLICY,
        auto_commit_policy=BATCHING,
    )

    tracker.fail.assert_not_awaited()
    tracker.complete.assert_awaited_once()
    extractor.assert_awaited_once()
    assert json.loads(fs.files[f"{uri}/.done"])["residual"] == []


@pytest.mark.asyncio
async def test_new_item_from_first_batch_is_available_to_second_batch_without_expanding_sources(
    monkeypatch,
):
    session, fs = session_with_fs()
    fresh = [message("question", "Please verify X."), message("answer", "X is verified.")]
    uri = archive(fs, 1, fresh)
    seen_id = None
    sources = []

    async def extract(**kwargs):
        nonlocal seen_id
        ids = [value.id for value in kwargs["messages"]]
        sources.append(ids)
        background = kwargs["continuation_background"]
        if ids == ["question"]:
            assert background == []
            return {
                "continuation_coverage": [
                    {
                        "action": "create",
                        "continuation_id": "",
                        "source_message_ids": ["question"],
                        "summary": "Verify X.",
                        "reason": "",
                    }
                ]
            }
        assert ids == ["answer"]
        assert len(background) == 1
        assert background[0].content == "Verify X."
        seen_id = background[0].id
        return {
            "continuation_coverage": resolve_action(
                kwargs, "resolve", seen_id, "answer", reason="X has been verified."
            )
        }

    extractor = AsyncMock(side_effect=extract)
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    await session._run_memory_extraction(
        "new-continuation-batches",
        uri,
        fresh,
        fresh[0].id,
        fresh[-1].id,
        POLICY,
        auto_commit_policy={"message_count_threshold": 1, "pending_token_threshold": 0},
    )

    tracker.fail.assert_not_awaited()
    tracker.complete.assert_awaited_once()
    assert seen_id.startswith("wi-continuation-")
    assert sources == [["question"], ["answer"]]
    meta = json.loads(fs.files[f"{uri}/.meta.json"])
    assert set(meta["completed_memory_steps"]["long_term"]) == {"question", "answer"}
    assert json.loads(fs.files[f"{uri}/.done"])["residual"] == []
