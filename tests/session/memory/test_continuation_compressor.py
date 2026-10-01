# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Continuation background and promotion receipts cross the compressor boundary."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.message import Message, TextPart
from openviking.prompts.manager import PromptManager
from openviking.session import compressor_v3
from openviking.session.continuation_state import continuation_fingerprint
from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.memory_updater import MemoryUpdateResult
from tests.session.memory.test_work_item_replay import extract
from tests.session.memory.test_work_item_replay import replay_case as replay_case


def background(identity="continuation-a", content="Approval is still required."):
    return Message(
        id=identity,
        role="assistant",
        message_kind="checkpoint",
        parts=[TextPart(content)],
        created_at="2026-10-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_public_compressor_injects_current_background_only_into_work_item_pass(replay_case):
    case = replay_case
    case.registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/profile.yaml")
    )
    stale = background("old-background", "Stale previous state.")
    current = background(content="Latest state after an unpublished successful extraction.")
    original_messages = [stale, *case.messages]
    observed = {}

    def orchestrator(**kwargs):
        provider = kwargs["context_provider"]
        kind = provider.get_memory_schemas(case.ctx)[0].memory_type
        observed[kind] = provider.get_extract_context().messages
        return SimpleNamespace(
            run=AsyncMock(
                return_value=(
                    ResolvedOperations(upsert_operations=[], delete_file_contents=[], errors=[]),
                    [],
                )
            )
        )

    case.compressor._get_or_create_react = Mock(side_effect=orchestrator)
    case.compressor._session_skill_extraction_enabled = lambda: False
    await case.compressor.extract_long_term_memories(
        messages=original_messages,
        ctx=case.ctx,
        session_id="session",
        allowed_memory_types={"profile", "work_item"},
        agent_evolution_enabled=False,
        strict_extract_errors=True,
        continuation_background=[current],
    )

    assert [value.id for value in observed["work_item"]] == [current.id, "m1", "m2"]
    assert observed["work_item"][0].content == current.content
    assert [value.id for value in observed["profile"]] == [stale.id, "m1", "m2"]
    assert original_messages[0] is stale
    assert stale.content == "Stale previous state."
    case.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_freezes_background_ranges_and_confirms_only_its_successful_write(
    replay_case, monkeypatch
):
    case = replay_case
    case.state.fail_after_first = False
    old = background()
    operation = case.resolved.upsert_operations[0]
    operation.memory_fields["ranges"] = "0-1"
    operation.source_message_ids = [old.id, "m1"]
    operation.source_evidence_message_ids = [old.id, "m1"]
    case.resolved.upsert_operations = [operation]
    case.resolved.continuation_coverage = [
        {
            "action": "promote",
            "continuation_id": old.id,
            "summary": "",
            "reason": "The saved task preserves the approval constraint.",
            "source_message_ids": ["m1"],
            "work_item_source_message_ids": [old.id, "m1"],
            "work_item_uri": operation.uris[0],
            "continuation_fingerprint": continuation_fingerprint(old.content),
        }
    ]
    providers = []
    real_provider = compressor_v3.SessionExtractContextProvider

    def record_provider(**kwargs):
        provider = real_provider(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(compressor_v3, "SessionExtractContextProvider", record_provider)
    first = await extract(case, continuation_background=[old], save_work_item_replay=case.save)
    plan = deepcopy(case.plans[-1])
    assert plan["message_ids"] == ["m1", "m2"]
    assert plan["continuation_background"] == [old.to_dict()]
    assert plan["operations"]["continuation_coverage"][0]["continuation_fingerprint"] == (
        continuation_fingerprint(old.content)
    )
    assert [value.id for value in case.submit.call_args.args[0].messages] == [old.id, "m1", "m2"]
    receipt = first.continuation_coverage[0]["promotion_receipt"]
    assert receipt["uri"] == operation.uris[0]
    assert set(receipt["source_message_ids"]) >= {old.id, "m1"}

    changed = background("different-item", "Different later state must not shift frozen ranges.")
    replayed = await extract(
        case,
        continuation_background=[changed],
        work_item_replay=plan,
        save_work_item_replay=case.save,
    )
    assert [value.id for value in providers[-1].get_extract_context().messages] == [
        old.id,
        "m1",
        "m2",
    ]
    assert providers[-1].get_extract_context().messages[0].content == old.content
    assert replayed.continuation_coverage[0]["promotion_receipt"] == receipt
    case.compressor._get_or_create_react.assert_called_once()
    case.submit.assert_awaited_once()
    assert len(case.writes) == 1
    assert [value.id for value in case.messages] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_legacy_replay_does_not_inject_new_background_into_old_ranges(
    replay_case, monkeypatch
):
    case = replay_case
    case.state.fail_after_first = False
    await extract(case, save_work_item_replay=case.save)
    plan = deepcopy(case.plans[-1])
    assert "continuation_background" not in plan
    providers = []
    real_provider = compressor_v3.SessionExtractContextProvider

    def record_provider(**kwargs):
        provider = real_provider(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(compressor_v3, "SessionExtractContextProvider", record_provider)
    await extract(case, work_item_replay=plan, continuation_background=[background()])
    assert [value.id for value in providers[-1].get_extract_context().messages] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_pending_replay_writer_receives_frozen_background_after_canonical_crash(replay_case):
    case = replay_case
    old = background()
    old_content = old.content
    for index, operation in enumerate(case.resolved.upsert_operations):
        operation.memory_fields["ranges"] = str(index + 1)
    with pytest.raises(RuntimeError, match="interrupted after canonical write"):
        await extract(case, continuation_background=[old], save_work_item_replay=case.save)
    plan = deepcopy(case.plans[-1])
    assert plan["message_ids"] == ["m1", "m2"]
    assert plan["continuation_background"][0]["parts"][0]["text"] == old_content
    assert len(case.files) == 1

    old.parts[0].text = "The caller has since changed this state."
    result = await extract(
        case,
        continuation_background=[background("new-id", "Unrelated new background.")],
        work_item_replay=plan,
        save_work_item_replay=case.save,
    )
    request = case.submit.call_args.args[0]
    assert [value.id for value in request.messages] == [old.id, "m1", "m2"]
    assert request.messages[0].content == old_content
    assert plan["continuation_background"][0]["parts"][0]["text"] == old_content
    assert len(case.files) == len(case.writes) == 2
    assert len(result.work_item_coverage) == 2
    case.compressor._get_or_create_react.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_writes", [False, True])
async def test_no_successful_current_write_cannot_reuse_old_promotion_receipt(
    replay_case, no_writes
):
    case = replay_case
    operation = case.resolved.upsert_operations[0]
    case.resolved.continuation_coverage = [
        {
            "action": "promote",
            "continuation_id": "continuation-a",
            "source_message_ids": ["m1"],
            "work_item_source_message_ids": ["continuation-a", "m1"],
            "summary": "",
            "reason": "Transfer state.",
            "work_item_uri": operation.uris[0],
            "promotion_receipt": {
                "uri": operation.uris[0],
                "source_message_ids": ["continuation-a", "m1"],
            },
        }
    ]
    if no_writes:
        case.resolved.upsert_operations = []
    else:
        case.submit.side_effect = lambda request: SimpleNamespace(
            operations=request.operations, apply_result=MemoryUpdateResult()
        )
    result = await extract(case)
    assert result.work_item_coverage == []
    assert result.continuation_coverage[0]["promotion_receipt"] is None
    assert case.files == {}
