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
    assert not result[0].get("source_message_ids")
    assert result[0]["created_at"] == source[0]["created_at"]
    assert summary in residual_text(result)
    assert estimate_text_tokens(residual_text(result)) <= 200
    assert "Do not deploy without approval" in vlm.get_completion_async.call_args.args[0]


@pytest.mark.asyncio
async def test_repeated_repair_does_not_accumulate_source_ids_in_model_input():
    summary = "Do not deploy without approval."
    vlm = SimpleNamespace(
        get_completion_async=AsyncMock(return_value=json.dumps({"summary": summary}))
    )
    residual = previous()
    prompts = []
    identities = []
    for step in range(3):
        residual[0]["source_message_ids"] = [
            f"historical-message-{i:05}" for i in range((step + 1) * 1000)
        ]
        residual[0]["internal_metadata"] = {"never_send_this": "x" * 10000}
        residual[0]["parts"][0]["internal_metadata"] = "never_send_this"
        residual = await compact_continuation(vlm, residual, 200)
        prompt = vlm.get_completion_async.call_args.args[0]
        prompts.append(prompt)
        identities.append(residual[0]["id"])
        assert "source_message_ids" not in prompt
        assert "historical-message-" not in prompt
        assert "never_send_this" not in prompt
        assert not residual[0].get("source_message_ids")
        assert '"role": "assistant"' in prompt
        assert "2026-10-01T00:00:00Z" in prompt
    assert prompts[1] == prompts[2]
    assert identities[1] == identities[2]
    assert max(map(len, prompts)) < 2000


@pytest.mark.asyncio
async def test_repair_preserves_all_legacy_message_content_and_reference_fields():
    vlm = SimpleNamespace(get_completion_async=AsyncMock(return_value='{"summary":"Fix X."}'))
    residual = [
        {
            "id": "old-raw",
            "role": "user",
            "created_at": "2026-10-01T00:00:00Z",
            "parts": [
                {"type": "text", "text": "Only deploy after approval."},
                {"type": "text", "text": "Also rerun the failing test."},
                {
                    "type": "tool",
                    "tool_name": "pytest",
                    "tool_output": "X failed",
                    "tool_output_ref": "tool-output.txt",
                },
            ],
        }
    ]
    await compact_continuation(vlm, residual, 200)
    prompt = vlm.get_completion_async.call_args.args[0]
    assert "Only deploy after approval." in prompt
    assert "Also rerun the failing test." in prompt
    assert "X failed" in prompt
    assert "tool-output.txt" in prompt
    assert '"role": "user"' in prompt


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
    from tests.unit.session.test_work_item_checkpoint import MemoryFS

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
        _viking_fs=MemoryFS(),
        ctx=None,
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
    from tests.unit.session.test_work_item_checkpoint import MemoryFS

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
        _viking_fs=MemoryFS(),
        _compact_work_item_continuation=lambda residual, budget: compact_continuation(
            vlm, residual, budget
        ),
    )
    owner._prepare_work_item_continuation = lambda uri, residual, meta, **kwargs: (
        Session._prepare_work_item_continuation(owner, uri, residual, meta, **kwargs)
    )
    migrated = await Session._prepare_work_item_checkpoint(
        owner, current_archive, [legacy], previous_checkpoint
    )
    value = migrated["residual"][0]
    assert old_archive not in residual_text([value]), (
        "recovery must not depend on the model repeating an old URI"
    )
    assert not value.get("source_continuation_ids")
    assert not value.get("source_message_ids")
    provenance = json.loads(owner._viking_fs.files[migrated["continuation_provenance_uri"]])
    assert provenance["outputs"][value["id"]] == [legacy.id]
    assert provenance["inputs"] == previous_checkpoint["residual"]
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


@pytest.mark.asyncio
async def test_below_budget_legacy_sources_move_to_ledger_without_model_and_stay_out_of_cache(
    monkeypatch,
):
    from tests.unit.session.test_work_item_checkpoint import archive, message, session_with_fs

    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("new")])
    residual = previous()
    source_ids = [f"old-source-{index}" for index in range(1000)]
    residual[0]["source_message_ids"] = source_ids
    residual[0]["source_continuation_ids"] = [f"old-summary-{index}" for index in range(1000)]
    compactor = AsyncMock(side_effect=AssertionError("The summary already fits"))
    monkeypatch.setattr(session, "_compact_work_item_continuation", compactor)
    meta = {}

    result = await session._prepare_work_item_continuation(uri, residual, meta)
    cached = await session._prepare_work_item_continuation(uri, residual, meta)

    compactor.assert_not_awaited()
    assert cached == result
    assert result[0]["id"] == residual[0]["id"]
    assert not result[0].get("source_message_ids")
    assert not result[0].get("source_continuation_ids")
    assert "old-source-999" not in json.dumps(meta["continuation_projection"])
    ledger = json.loads(fs.files[f"{uri}/continuation-provenance.json"])
    assert ledger["inputs"] == residual
    assert ledger["inputs"][0]["source_message_ids"] == source_ids
    assert ledger["outputs"][result[0]["id"]] == [residual[0]["id"]]


@pytest.mark.asyncio
async def test_ready_legacy_cache_migrates_source_ids_without_repeating_successful_compaction(
    monkeypatch,
):
    import hashlib

    from tests.unit.session.test_work_item_checkpoint import archive, message, session_with_fs

    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("new")])
    residual = previous()
    residual[0]["parts"][0]["text"] = "Unresolved deployment constraint. " * 10000
    candidate = previous()
    candidate[0]["id"] = "already-compacted"
    candidate[0]["source_message_ids"] = [f"old-source-{index}" for index in range(1000)]
    residual[0]["source_message_ids"] = candidate[0]["source_message_ids"]
    candidate[0]["source_continuation_ids"] = [residual[0]["id"]]
    old_hash = hashlib.sha256(
        json.dumps([residual, 10000], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    meta = {
        "continuation_projection": {
            "status": "ready",
            "input_hash": old_hash,
            "residual": candidate,
        }
    }
    compactor = AsyncMock(side_effect=AssertionError("The cached model result must be reused"))
    monkeypatch.setattr(session, "_compact_work_item_continuation", compactor)

    result = await session._prepare_work_item_continuation(uri, residual, meta)
    reused = await session._prepare_work_item_continuation(uri, residual, meta)

    compactor.assert_not_awaited()
    assert reused == result
    assert result[0]["id"] == "already-compacted"
    assert not result[0].get("source_message_ids")
    assert not result[0].get("source_continuation_ids")
    assert "Do not deploy without approval" in residual_text(result)
    assert meta["continuation_projection"]["provenance_version"] == 1
    ledger = json.loads(fs.files[f"{uri}/continuation-provenance.json"])
    assert ledger["inputs"] == residual
    assert ledger["outputs"][result[0]["id"]] == [residual[0]["id"]]
