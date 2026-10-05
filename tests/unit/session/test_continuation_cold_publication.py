# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Cold continuation survives publication, retries and later topic recovery."""

import json
from unittest.mock import AsyncMock

import pytest

from openviking.session import work_items as wi
from openviking.session.continuation_store import ContinuationRetentionPolicy
from openviking.session.memory.memory_updater import ExtractContext
from tests.unit.session.test_continuation_state_publication import (
    prepare,
    publish,
    store_transitions,
    transition,
)
from tests.unit.session.test_work_item_checkpoint import (
    archive,
    message,
    session_with_fs,
    wire_work_item_phase2,
)


@pytest.fixture
def quick_retention(monkeypatch):
    policy = ContinuationRetentionPolicy(idle_turns=2, idle_days=7, min_idle_turns=1)
    monkeypatch.setattr(
        "openviking.session.continuation_store.get_continuation_retention_policy", lambda: policy
    )
    return policy


def read_store(fs, checkpoint):
    return json.loads(fs.files[checkpoint["continuation_store_uri"]])


async def make_cold(session, fs):
    source = message("request", "Investigate the database migration error.")
    uri, previous = await prepare(
        session,
        fs,
        1,
        [source],
        {},
        [transition("create", [source.id], summary=source.content)],
    )
    await publish(session, uri, previous)
    identity = previous["residual"][0]["id"]
    for number in (2, 3):
        uri, previous = await prepare(
            session,
            fs,
            number,
            [message(f"chitchat-{number}", "Good morning.")],
            previous,
            [transition("keep", identity=identity, reason="The investigation remains pending.")],
        )
        await publish(session, uri, previous)
    assert previous["residual"] == []
    return previous, identity


@pytest.mark.asyncio
async def test_idle_eviction_preserves_whole_state_and_single_normal_recovery_notice(
    quick_retention,
):
    session, fs = session_with_fs()
    previous, identity = await make_cold(session, fs)
    store = read_store(fs, previous)
    # The read tool supports line ranges; the index must not become one huge line.
    assert '\n  "items": {' in fs.files[previous["continuation_store_uri"]]
    value = store["items"][identity]
    assert value["state"] == "active"
    assert value["residency"] == "cold"
    assert value["eviction_reason"] == "idle_turns"
    assert wi.continuation_content(value["message"]) == "Investigate the database migration error."
    assert previous["continuation_degraded"] is False
    assert previous["cold_continuation_count"] == 1
    overview, _ = await session._read_work_item_projection(previous)
    assert overview.count(previous["continuation_store_uri"]) == 1
    assert identity not in overview
    assert "Cold continuation" in overview

    # The next checkpoint points directly to the current index, not a growing
    # list of previous cold snapshots in the model's working memory.
    uri, next_checkpoint = await prepare(
        session, fs, 4, [message("another-topic", "Good evening.")], previous, []
    )
    assert identity in read_store(fs, next_checkpoint)["items"]
    assert previous["continuation_store_uri"] not in fs.files[f"{uri}/.overview.md"]


@pytest.mark.asyncio
async def test_related_followup_restores_original_id_through_extraction_and_can_resolve(
    monkeypatch, quick_retention
):
    session, fs = session_with_fs()
    previous, identity = await make_cold(session, fs)
    fresh = message("followup", "Continue investigating the database migration error.")
    uri = archive(fs, 4, [fresh])

    async def extract(**kwargs):
        background = kwargs["continuation_background"]
        assert [value.id for value in background] == [identity]
        context = ExtractContext(background + [fresh])
        actions = wi.resolve_continuation_coverage(
            context,
            [
                {
                    "action": "keep",
                    "continuation_id": identity,
                    "ranges": "1",
                    "reason": "The user explicitly resumed this investigation.",
                }
            ],
        )
        return {"continuation_coverage": actions}

    extractor = AsyncMock(side_effect=extract)
    tracker = wire_work_item_phase2(monkeypatch, session, extractor)
    policy = {"working_memory": {"mode": "work_item"}, "memory_types": ["work_item"]}
    await session._run_memory_extraction("cold-recall", uri, [fresh], fresh.id, fresh.id, policy)
    tracker.fail.assert_not_awaited()
    extractor.assert_awaited_once()
    restored = json.loads(fs.files[f"{uri}/.done"])
    assert [value["id"] for value in restored["residual"]] == [identity]
    assert restored["cold_continuation_count"] == 0
    assert read_store(fs, restored)["items"][identity]["last_activity_turn"] == 4

    settled = message("settled", "The database migration error is fixed and verified.")
    uri, closed = await prepare(
        session,
        fs,
        5,
        [settled],
        restored,
        [transition("resolve", [settled.id], identity=identity, reason="Verified fixed.")],
    )
    await publish(session, uri, closed)
    assert read_store(fs, closed)["items"][identity]["state"] == "resolved"
    assert (
        session._continuation_extraction_background(closed, read_store(fs, closed), [fresh]) == []
    )
    assert closed["residual"] == []


@pytest.mark.asyncio
async def test_retrieved_candidate_without_new_attributed_evidence_stays_cold(quick_retention):
    session, fs = session_with_fs()
    previous, identity = await make_cold(session, fs)
    fresh = message("mention", "What about the database migration error?")
    _, checkpoint = await prepare(
        session,
        fs,
        4,
        [fresh],
        previous,
        [transition("keep", identity=identity, reason="Old state still pending.")],
    )
    assert checkpoint["residual"] == []
    assert read_store(fs, checkpoint)["items"][identity]["last_activity_turn"] == 1


@pytest.mark.asyncio
async def test_idle_store_write_failure_and_retry_do_not_advance_publication_twice(
    monkeypatch, quick_retention
):
    session, fs = session_with_fs()
    source = message("request", "A short investigation remains pending.")
    uri, previous = await prepare(
        session,
        fs,
        1,
        [source],
        {},
        [transition("create", [source.id], summary=source.content)],
    )
    await publish(session, uri, previous)
    fresh = [message("other-1", "Hello."), message("other-2", "Goodbye.")]
    uri = archive(fs, 2, fresh)
    store_transitions(fs, uri, [])
    write = fs.write_file

    async def fail_store(uri, content, **kwargs):
        if uri.endswith("continuation-store.json"):
            raise OSError("Cold store unavailable")
        return await write(uri, content, **kwargs)

    monkeypatch.setattr(fs, "write_file", fail_store)
    with pytest.raises(OSError, match="Cold store unavailable"):
        await session._prepare_work_item_checkpoint(uri, fresh, previous)
    assert f"{uri}/.done" not in fs.files
    assert read_store(fs, previous)["turn_count"] == 1
    monkeypatch.setattr(fs, "write_file", write)
    retried = await session._prepare_work_item_checkpoint(uri, fresh, previous)
    await publish(session, uri, retried)
    assert retried["residual"] == []
    assert read_store(fs, retried)["turn_count"] == 3
    repeated = await session._prepare_work_item_checkpoint(uri, fresh, previous)
    assert repeated == retried
    assert read_store(fs, repeated)["turn_count"] == 3


@pytest.mark.asyncio
async def test_standing_constraint_protection_survives_provenance_and_compaction(quick_retention):
    session, fs = session_with_fs()
    source = message("constraint", "Never push without explicit approval.")
    action = transition("create", [source.id], identity="constraint-id", summary=source.content)
    action["protection"] = {
        "kind": "constraint",
        "reason": "Applies throughout this session.",
        "source_message_ids": [source.id],
    }
    uri, previous = await prepare(session, fs, 1, [source], {}, [action])
    await publish(session, uri, previous)
    fresh = [message(f"unrelated-{number}", "Good morning.") for number in range(5)]
    _, checkpoint = await prepare(session, fs, 2, fresh, previous, [])
    assert [value["id"] for value in checkpoint["residual"]] == ["constraint-id"]
    assert "protection" not in checkpoint["residual"][0]
    assert (
        read_store(fs, checkpoint)["items"]["constraint-id"]["protection"]["kind"] == "constraint"
    )


@pytest.mark.asyncio
async def test_frozen_cold_action_replays_even_when_latest_topic_no_longer_matches(quick_retention):
    session, fs = session_with_fs()
    previous, identity = await make_cold(session, fs)
    fresh = [
        message("related", "Continue investigating the database migration error."),
        message("new-topic", "What is the weather like?"),
    ]
    _, checkpoint = await prepare(
        session,
        fs,
        4,
        fresh,
        previous,
        [
            transition(
                "update",
                [fresh[0].id],
                identity=identity,
                summary="Database migration reproduction is pending.",
            )
        ],
    )
    assert [value["id"] for value in checkpoint["residual"]] == [identity]
    assert "reproduction" in wi.residual_text(checkpoint["residual"])
