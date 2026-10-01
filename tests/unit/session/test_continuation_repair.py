# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.continuation import compact_continuation
from openviking.session.work_items import continuation_message, residual_text
from openviking.utils.token_estimation import estimate_text_tokens


def previous():
    return [
        continuation_message(
            "Tests failed; fix X next. Do not deploy without approval.",
            "viking://user/u/sessions/s/history/archive_001",
            ["m1", "m2"],
            "2026-10-01T00:00:00Z",
        )
    ]


@pytest.mark.asyncio
async def test_repair_keeps_source_identity_as_historical_assistant_state():
    summary = "Fix X and rerun tests. Deployment requires approval."
    vlm = SimpleNamespace(
        get_completion_async=AsyncMock(return_value=json.dumps({"summary": summary}))
    )
    source = previous()
    result = await compact_continuation(vlm, source, 200)
    assert result[0]["role"] == "assistant"
    assert result[0]["message_kind"] == "checkpoint"
    assert result[0]["id"] not in ["m1", "m2", source[0]["id"]]
    assert result[0]["source_message_ids"] == ["m1", "m2"]
    assert summary in residual_text(result)
    assert estimate_text_tokens(residual_text(result)) <= 200
    assert "Do not deploy without approval" in vlm.get_completion_async.call_args.args[0]


@pytest.mark.asyncio
async def test_repair_retries_invalid_and_over_budget_candidates_without_truncation():
    vlm = SimpleNamespace(
        get_completion_async=AsyncMock(
            side_effect=[
                '{"summary": ""}',
                json.dumps({"summary": "long " * 1000}),
                '```json\n{"summary":"Do not deploy; fix X next."}\n```',
            ]
        )
    )
    result = await compact_continuation(vlm, previous(), 200)
    assert vlm.get_completion_async.await_count == 3
    assert "Do not deploy; fix X next." in residual_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["not json", '{"summary":null}', '{"summary":[]}'])
async def test_repair_has_a_bounded_retry_count_for_invalid_responses(response):
    vlm = SimpleNamespace(get_completion_async=AsyncMock(return_value=response))
    with pytest.raises(ValueError, match="within its token budget"):
        await compact_continuation(vlm, previous(), 200)
    assert vlm.get_completion_async.await_count == 3


@pytest.mark.asyncio
async def test_repair_api_failure_is_not_an_empty_success():
    vlm = SimpleNamespace(get_completion_async=AsyncMock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        await compact_continuation(vlm, previous(), 200)


@pytest.mark.asyncio
async def test_session_repair_reserves_source_reference_before_candidate_retry(monkeypatch):
    from openviking.message import Message, TextPart
    from openviking.session import work_items as wi
    from openviking.session.session import Session
    from openviking.session.work_item_budget import WorkItemBudgets

    archive_uri = "viking://user/default/sessions/work-items/history/archive_002"
    first_response = json.dumps({"summary": "a" * 275})
    probe = SimpleNamespace(get_completion_async=AsyncMock(return_value=first_response))
    near_limit = await compact_continuation(probe, previous(), 100)
    assert estimate_text_tokens(residual_text(near_limit)) == 90
    near_limit[0]["source_checkpoint_uri"] = f"{archive_uri}/.done"
    assert estimate_text_tokens(residual_text(near_limit)) == 111

    vlm = SimpleNamespace(
        get_completion_async=AsyncMock(
            side_effect=[first_response, '{"summary":"Do not deploy without approval."}']
        )
    )
    owner = SimpleNamespace(
        _merge_archive_meta=AsyncMock(),
        _compact_work_item_continuation=lambda residual, budget: compact_continuation(
            vlm, residual, budget
        ),
    )
    legacy = [
        Message(
            id="legacy", role="user", parts=[TextPart("Do not deploy without approval.")]
        ).to_dict()
    ]
    monkeypatch.setattr(
        wi, "get_work_item_budgets", lambda: WorkItemBudgets(continuation_token_budget=100)
    )
    result = await Session._prepare_work_item_continuation(owner, archive_uri, legacy, {})

    assert vlm.get_completion_async.await_count == 2
    assert "exceeding" in vlm.get_completion_async.call_args.args[0]
    assert "Do not deploy without approval." in residual_text(result)
    assert result[0]["source_checkpoint_uri"] == f"{archive_uri}/.done"
    assert estimate_text_tokens(residual_text(result)) <= 100
    saved = owner._merge_archive_meta.call_args.args[1]
    assert saved["continuation_projection"]["status"] == "ready"
    cached = await Session._prepare_work_item_continuation(owner, archive_uri, legacy, saved)
    assert cached == result
    assert vlm.get_completion_async.await_count == 2


@pytest.mark.asyncio
async def test_legacy_summary_migration_and_next_update_keep_both_original_archive_sources():
    from openviking.message import Message, TextPart
    from openviking.session import work_items as wi
    from openviking.session.session import Session

    session_uri = "viking://user/u/sessions/s"
    first_archive = f"{session_uri}/history/archive_001"
    old_archive = f"{session_uri}/history/archive_002"
    current_archive = f"{session_uri}/history/archive_003"
    # V1 summaries reused their first original ID and did not mark themselves
    # as checkpoint messages. The other original can live in another archive.
    legacy = Message(
        id="m1",
        role="assistant",
        parts=[
            TextPart(
                "Previous continuation summary: approval required; review still pending.\n"
                + f"Source coverage: {old_archive}/.done"
            )
        ],
    )
    previous_checkpoint = {
        "archive_id": "archive_002",
        "residual": [legacy.to_dict()],
        "coverage": [
            {"message_id": "m1", "archive_uri": f"{first_archive}/messages.jsonl"},
            {"message_id": "m2", "archive_uri": f"{old_archive}/messages.jsonl"},
        ],
    }
    vlm = SimpleNamespace(
        get_completion_async=AsyncMock(
            return_value=json.dumps(
                {"summary": "Deployment needs approval; review remains pending."}
            )
        )
    )
    owner = SimpleNamespace(
        _session_uri=session_uri,
        ctx=None,
        _archives=SimpleNamespace(read_meta=AsyncMock(return_value={})),
        _work_item_source_archives=AsyncMock(return_value={legacy.id: first_archive}),
        _merge_archive_meta=AsyncMock(),
        _viking_fs=SimpleNamespace(write_file=AsyncMock()),
        _compact_work_item_continuation=lambda residual, budget: compact_continuation(
            vlm, residual, budget
        ),
    )
    owner._prepare_work_item_continuation = lambda uri, residual, meta: (
        Session._prepare_work_item_continuation(owner, uri, residual, meta)
    )
    migrated = await Session._prepare_work_item_checkpoint(
        owner, current_archive, [legacy], previous_checkpoint
    )
    value = migrated["residual"][0]
    assert old_archive not in residual_text([value]), (
        "recovery must not depend on the model repeating an old URI"
    )
    assert value["source_continuation_ids"] == [legacy.id]
    assert value["source_checkpoint_uri"] == f"{current_archive}/.done"
    assert migrated["coverage"][0]["archive_uri"] == f"{first_archive}/messages.jsonl"
    assert migrated["coverage"][0]["source_checkpoint_uri"] == f"{old_archive}/.done"

    next_archive = f"{session_uri}/history/archive_004"
    updated, ledger = wi.coverage_report(
        [Message.from_dict(value)],
        [],
        next_archive,
        [
            {
                "source_message_ids": [value["id"]],
                "summary": "Approval and review are still required.",
                "reason": "",
            }
        ],
        previous_residual=[value],
        previous_checkpoint_uri=f"{current_archive}/.done",
    )
    assert "Approval and review" in residual_text(updated)
    assert updated[0]["source_message_ids"] == [value["id"]]
    assert ledger[0]["source_checkpoint_uri"] == f"{current_archive}/.done"
    # The new update reaches the migration ledger, then the legacy checkpoint
    # which retains both raw source destinations, not just the reused first ID.
    records = {
        f"{current_archive}/.done": migrated,
        f"{old_archive}/.done": previous_checkpoint,
    }
    migrated_record = records[ledger[0]["source_checkpoint_uri"]]
    legacy_record = records[migrated_record["coverage"][0]["source_checkpoint_uri"]]
    assert {row["archive_uri"] for row in legacy_record["coverage"]} == {
        f"{first_archive}/messages.jsonl",
        f"{old_archive}/messages.jsonl",
    }
