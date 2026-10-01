# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session.compressor_v3 import SessionCompressorV3
from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import (
    ExtractContext,
    MemoryUpdater,
    MemoryUpdateResult,
)
from openviking.session.memory.streaming_memory_updater import attach_source_to_request_operations
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.work_item import new_work_item_id
from openviking_cli.exceptions import ConflictError
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def replay_case(monkeypatch):
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/work_item.yaml")
    )
    registry.initialize_memory_files = AsyncMock()
    files = {}
    writes = []

    async def read(uri, **kwargs):
        if uri not in files:
            raise FileNotFoundError(uri)
        return files[uri]

    async def write(uri, content, **kwargs):
        files[uri] = content
        writes.append(uri)

    fs = SimpleNamespace(read_file=read, write_file=write)
    monkeypatch.setattr("openviking.session.compressor_v3.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.session.memory.account_templates.resolve_account_memory_registry",
        AsyncMock(return_value=registry),
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    messages = [Message(id=f"m{i}", role="user", parts=[TextPart(f"Task {i}")]) for i in (1, 2)]
    context = ExtractContext(messages)
    operations = []
    for index in range(2):
        identity = new_work_item_id(context, str(index), index, "session")
        operations.append(
            ResolvedOperation(
                memory_type="work_item",
                uris=[f"viking://user/alice/memories/work_item/{identity}.md"],
                memory_fields={
                    "work_item_id": identity,
                    "title": f"Task {index}",
                    "goal": "finish",
                    "status": "open",
                    "ranges": str(index),
                },
                source_message_ids=[messages[index].id],
                source_evidence_message_ids=[messages[index].id],
            )
        )
    resolved = ResolvedOperations(upsert_operations=operations, delete_file_contents=[], errors=[])
    updater = MemoryUpdater(registry=registry)
    updater._viking_fs = fs
    state = SimpleNamespace(fail_after_first=True, partial=set())

    async def submit(request):
        attach_source_to_request_operations(request)
        result = MemoryUpdateResult()
        for operation in request.operations.upsert_operations:
            await updater._apply_upsert(operation, ctx, ExtractContext(request.messages))
            if state.fail_after_first:
                state.fail_after_first = False
                raise RuntimeError("interrupted after canonical write")
            result.add_written(operation.uris[0])
        return SimpleNamespace(operations=request.operations, apply_result=result)

    submit_mock = AsyncMock(side_effect=submit)
    monkeypatch.setattr(
        "openviking.session.compressor_v3.get_streaming_memory_updater",
        AsyncMock(return_value=SimpleNamespace(submit=submit_mock)),
    )
    compressor = SessionCompressorV3(
        vikingdb=None,
        rollout_analyzer=SimpleNamespace(),
        vlm_resolver=SimpleNamespace(get_vlm=AsyncMock(return_value=SimpleNamespace())),
    )

    def orchestrator(**kwargs):
        kwargs["context_provider"].work_item_partial_tool_message_ids = state.partial
        return SimpleNamespace(run=AsyncMock(return_value=(resolved.model_copy(deep=True), [])))

    compressor._get_or_create_react = Mock(side_effect=orchestrator)
    plans = []

    async def save(plan):
        if not plans:
            assert not files, "plan must be durable before the first canonical write"
        plans.append(json.loads(json.dumps(plan)))

    return SimpleNamespace(
        compressor=compressor,
        ctx=ctx,
        messages=messages,
        resolved=resolved,
        files=files,
        writes=writes,
        state=state,
        plans=plans,
        save=save,
        updater=updater,
        submit=submit_mock,
        registry=registry,
    )


async def extract(case, **kwargs):
    return await case.compressor._extract_user_memories(
        messages=case.messages,
        session_id="session",
        ctx=case.ctx,
        allowed_memory_types={"work_item"},
        work_item_uris=[],
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_types", [None, {"profile", "work_item"}])
@pytest.mark.parametrize("fail_work_item", [False, True])
async def test_memory_and_work_item_have_independent_prompts_and_calls(
    replay_case, allowed_types, fail_work_item
):
    from openviking.session.memory.schema_model_generator import SchemaModelGenerator

    case = replay_case
    case.registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/profile.yaml")
    )
    case.messages[0].parts.append(ToolPart(tool_name="bash", tool_output="tool-only evidence"))
    calls = {}
    both_started = asyncio.Event()
    ordinary_finished = asyncio.Event()

    def orchestrator(**kwargs):
        provider = kwargs["context_provider"]
        schemas = provider.get_memory_schemas(case.ctx)
        assert len(schemas) == 1
        kind = schemas[0].memory_type
        model = SchemaModelGenerator(schemas).create_structured_operations_model()
        provider.search_files = AsyncMock(return_value=[])
        provider._append_structured_read_result = AsyncMock(return_value=1)

        async def run():
            calls[kind] = (model, await provider.prefetch())
            if len(calls) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            if kind == "profile":
                # The other pass can fail while this pass is still completing.
                await asyncio.sleep(0)
                ordinary_finished.set()
            elif fail_work_item:
                raise ValueError("work_item extraction failed")
            return ResolvedOperations(upsert_operations=[], delete_file_contents=[], errors=[]), []

        return SimpleNamespace(run=run)

    case.compressor._get_or_create_react = Mock(side_effect=orchestrator)
    kwargs = {
        "messages": case.messages,
        "ctx": case.ctx,
        "allowed_memory_types": allowed_types,
        "work_item_uris": [],
    }
    if fail_work_item:
        with pytest.raises(ValueError, match="work_item extraction failed"):
            await case.compressor._extract_user_memories(**kwargs)
    else:
        await case.compressor._extract_user_memories(**kwargs)

    assert ordinary_finished.is_set()
    assert case.compressor._get_or_create_react.call_count == 2
    ordinary_model, ordinary_prompt = calls["profile"]
    work_model, work_prompt = calls["work_item"]
    assert "work_item" not in ordinary_model.model_fields
    assert "continuation_coverage" not in ordinary_model.model_fields
    assert "tool-only evidence" not in str(ordinary_prompt)
    assert "Execution evidence" not in str(ordinary_prompt)
    assert "profile" not in work_model.model_fields
    assert "continuation_coverage" in work_model.model_fields
    assert "tool-only evidence" in str(work_prompt)


@pytest.mark.asyncio
async def test_split_extraction_keeps_both_memory_writes_and_work_item_receipts(replay_case):
    case = replay_case
    case.state.fail_after_first = False
    case.registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/profile.yaml")
    )
    profile_uri = "viking://user/alice/memories/profile.md"
    profile = ResolvedOperation(
        memory_type="profile",
        uris=[profile_uri],
        memory_fields={"content": "# Alice\n- Engineer (as of 2026-09-30)", "ranges": "0"},
    )

    def orchestrator(**kwargs):
        schemas = kwargs["context_provider"].get_memory_schemas(case.ctx)
        operations = (
            ResolvedOperations(upsert_operations=[profile], delete_file_contents=[], errors=[])
            if schemas[0].memory_type == "profile"
            else case.resolved.model_copy(deep=True)
        )
        return SimpleNamespace(run=AsyncMock(return_value=(operations, [])))

    case.compressor._get_or_create_react = Mock(side_effect=orchestrator)
    case.compressor._session_skill_extraction_enabled = lambda: False
    archive_uri = "viking://user/alice/sessions/test/history/archive_001"
    save = AsyncMock()

    result = await case.compressor.extract_long_term_memories(
        messages=case.messages,
        ctx=case.ctx,
        session_id="test",
        allowed_memory_types={"profile", "work_item"},
        agent_evolution_enabled=False,
        strict_extract_errors=True,
        archive_uri=archive_uri,
        work_item_uris=[],
        save_work_item_replay=save,
    )

    assert "Engineer" in MemoryFileUtils.read(case.files[profile_uri]).content
    assert len(result["contexts"]) == 3
    assert len(result["work_items"]) == len(result["work_item_coverage"]) == 2
    diff = json.loads(case.files[f"{archive_uri}/memory_diff.json"])
    assert {entry["uri"] for entry in diff["operations"]["adds"]} == {
        profile_uri,
        *(op.uris[0] for op in case.resolved.upsert_operations),
    }
    assert {
        op["memory_type"] for op in save.await_args.args[0]["operations"]["upsert_operations"]
    } == {"work_item"}


def use_real_apply(case):
    case.updater._sync_resource_refs_for_result = AsyncMock()
    case.updater._vectorize_memories = AsyncMock()
    case.updater.generate_overview = AsyncMock()

    async def submit(request):
        attach_source_to_request_operations(request)
        result = await case.updater.apply_operations(
            request.operations, case.ctx, ExtractContext(request.messages)
        )
        return SimpleNamespace(operations=request.operations, apply_result=result)

    case.submit.side_effect = submit


@pytest.mark.asyncio
async def test_reported_partial_failure_keeps_plan_pending_until_replayed(replay_case):
    case = replay_case
    use_real_apply(case)
    original_write = case.updater._viking_fs.write_file
    failed_uri = case.resolved.upsert_operations[1].uris[0]

    async def fail_second_once(uri, content, **kwargs):
        if uri == failed_uri and case.state.fail_after_first:
            case.state.fail_after_first = False
            raise OSError("second item unavailable")
        await original_write(uri, content, **kwargs)

    case.updater._viking_fs.write_file = fail_second_once
    with pytest.raises(OSError, match="second item unavailable"):
        await extract(case, save_work_item_replay=case.save)
    assert len(case.files) == 1
    assert case.plans[0]["completed_uris"] == []
    assert case.plans[-1]["completed_uris"] == [case.resolved.upsert_operations[0].uris[0]]

    result = await extract(case, work_item_replay=case.plans[-1])
    case.compressor._get_or_create_react.assert_called_once()
    assert len(case.files) == len(case.writes) == 2
    assert all(
        MemoryFileUtils.read(raw).extra_fields["version"] == 1 for raw in case.files.values()
    )
    assert len(result.work_item_coverage) == 2


@pytest.mark.asyncio
async def test_replay_requires_write_receipts_even_without_reported_errors(replay_case):
    case = replay_case
    case.submit.side_effect = lambda request: SimpleNamespace(
        operations=request.operations, apply_result=MemoryUpdateResult()
    )
    with pytest.raises(RuntimeError, match="Incomplete work_item replay writes"):
        await extract(case, save_work_item_replay=case.save)
    assert len(case.plans) == 1
    assert case.files == {}


@pytest.mark.asyncio
async def test_resolution_errors_are_not_frozen_or_applied(replay_case):
    case = replay_case
    case.resolved.errors = ["work item reference could not be resolved"]
    with pytest.raises(ValueError, match="resolution errors"):
        await extract(case, save_work_item_replay=case.save)
    assert case.plans == []
    assert case.files == {}
    case.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_memory_apply_error_does_not_fail_work_item_receipts(replay_case):
    case = replay_case
    use_real_apply(case)
    original_apply = case.updater._apply_upsert

    async def fail_ordinary(operation, *args, **kwargs):
        if operation.memory_type == "profile":
            raise OSError("ordinary profile unavailable")
        return await original_apply(operation, *args, **kwargs)

    case.updater._apply_upsert = fail_ordinary
    case.resolved.upsert_operations.append(
        ResolvedOperation(
            memory_type="profile",
            memory_fields={},
            uris=["viking://user/alice/memories/profile.md"],
        )
    )
    result = await extract(case, save_work_item_replay=case.save)
    assert len(case.files) == len(result.work_item_coverage) == 2
    assert len(case.plans[0]["operations"]["upsert_operations"]) == 2


@pytest.mark.asyncio
async def test_partial_write_replay_reuses_ids_without_reextracting_or_incrementing(replay_case):
    case = replay_case
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    assert len(case.files) == len(case.plans) == 1
    result = await extract(case, work_item_replay=case.plans[0])
    case.compressor._get_or_create_react.assert_called_once()
    assert set(case.files) == {op.uris[0] for op in case.resolved.upsert_operations}
    assert len(case.writes) == 2
    assert all(
        MemoryFileUtils.read(raw).extra_fields["version"] == 1 for raw in case.files.values()
    )
    assert len(result.work_item_coverage) == 2


@pytest.mark.asyncio
async def test_failed_plan_save_prevents_canonical_writes(replay_case):
    case = replay_case
    with pytest.raises(OSError, match="metadata unavailable"):
        await extract(
            case, save_work_item_replay=AsyncMock(side_effect=OSError("metadata unavailable"))
        )
    assert case.files == {}
    case.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_does_not_overwrite_another_session_newer_version(replay_case):
    case = replay_case
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    uri = case.resolved.upsert_operations[0].uris[0]
    canonical = MemoryFileUtils.read(case.files[uri], uri=uri)
    canonical.extra_fields.update(
        version=2, source_extraction_id="other-session", current_state="newer"
    )
    case.files[uri] = MemoryFileUtils.write(canonical)
    before = dict(case.files)
    with pytest.raises(ConflictError):
        await extract(case, work_item_replay=case.plans[0])
    assert case.files == before


@pytest.mark.asyncio
async def test_no_write_plan_keeps_continuation_classification_and_partial_ids(replay_case):
    case = replay_case
    case.resolved.upsert_operations = []
    case.resolved.continuation_coverage = [
        {"source_message_ids": ["m1"], "summary": "keep constraint", "reason": ""}
    ]
    case.state.partial = {"m2"}
    first = await extract(case, save_work_item_replay=case.save)
    second = await extract(case, work_item_replay=case.plans[0])
    assert (
        first.continuation_coverage
        == second.continuation_coverage
        == case.resolved.continuation_coverage
    )
    assert case.plans[0]["partial_tool_message_ids"] == ["m2"]
    case.compressor._get_or_create_react.assert_called_once()
    case.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_preserves_partial_tool_attribution_in_union_coverage(replay_case):
    case = replay_case
    case.state.partial = {"m2"}
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    result = await extract(case, work_item_replay=case.plans[0])
    assert sorted(
        identity for item in result.work_item_coverage for identity in item["source_message_ids"]
    ) == ["m1", "m2"]


@pytest.mark.asyncio
async def test_existing_item_update_replay_keeps_the_successful_version(replay_case):
    case = replay_case
    operation = case.resolved.upsert_operations[0]
    await case.updater._apply_upsert(operation, case.ctx)
    operation.old_memory_file_content = MemoryFileUtils.read(
        case.files[operation.uris[0]], uri=operation.uris[0]
    )
    operation.memory_fields["current_state"] = "Implemented"
    case.resolved.upsert_operations = [operation]
    save = AsyncMock()
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=save)
    plan = json.loads(json.dumps(save.await_args.args[0]))
    result = await extract(case, work_item_replay=plan)
    canonical = MemoryFileUtils.read(case.files[operation.uris[0]])
    assert canonical.extra_fields["version"] == 2
    assert canonical.extra_fields["current_state"] == "Implemented"
    assert len(case.writes) == 2
    assert result.work_items[0]["version"] == 2


@pytest.mark.asyncio
async def test_inherited_replay_recovers_origin_receipt_after_another_session_updates(replay_case):
    from openviking.session.memory.dataclass import MemoryOperationSource
    from openviking.session.memory.work_item_receipts import work_item_receipt_uri

    case = replay_case
    origin_archive = "viking://user/alice/sessions/s/history/archive_001"
    next_archive = "viking://user/alice/sessions/s/history/archive_002"
    first, second = case.resolved.upsert_operations
    first_uri, second_uri = first.uris[0], second.uris[0]
    original_write = case.updater._viking_fs.write_file

    async def crash_after_canonical_write(uri, content, **kwargs):
        await original_write(uri, content, **kwargs)
        if uri == first_uri:
            raise RuntimeError("crash before archive receipt and progress save")

    case.updater._viking_fs.write_file = crash_after_canonical_write
    with pytest.raises(RuntimeError, match="crash before archive receipt"):
        await extract(case, archive_uri=origin_archive, save_work_item_replay=case.save)
    original_plan = case.plans[0]
    assert len(case.plans) == 1
    assert original_plan["completed_uris"] == []
    assert original_plan["receipt_archive_uri"] == origin_archive
    receipt_uri = work_item_receipt_uri(
        case.ctx, origin_archive, original_plan["extraction_id"], first_uri
    )
    assert receipt_uri not in case.files
    case.updater._viking_fs.write_file = original_write

    # A real later writer must preserve the pending marker before replacing A.
    another_update = first.model_copy(deep=True)
    another_update.old_memory_file_content = MemoryFileUtils.read(
        case.files[first_uri], uri=first_uri
    )
    another_update.memory_fields["current_state"] = "Another session's newer result"
    another_update.source = MemoryOperationSource(
        extraction_id="another-session-update", session_id="other"
    )
    await case.updater._apply_upsert(another_update, case.ctx, ExtractContext(case.messages))
    receipt = json.loads(case.files[receipt_uri])
    assert receipt == {
        "archive_uri": origin_archive,
        "extraction_id": original_plan["extraction_id"],
        "uri": first_uri,
        "version": 1,
    }
    preserved_first = case.files[first_uri]
    assert MemoryFileUtils.read(preserved_first).extra_fields["version"] == 2
    assert "work_item_replay_receipt" not in MemoryFileUtils.read(preserved_first).extra_fields
    assert case.writes.index(receipt_uri) < len(case.writes) - 1
    assert case.writes[-1] == first_uri

    use_real_apply(case)
    case.submit.reset_mock()
    writes_before_retry = len(case.writes)
    result = await extract(
        case,
        archive_uri=next_archive,
        work_item_replay=original_plan,
        save_work_item_replay=case.save,
    )

    case.compressor._get_or_create_react.assert_called_once()
    case.submit.assert_awaited_once()
    request = case.submit.await_args.args[0]
    assert [operation.uris for operation in request.operations.upsert_operations] == [[second_uri]]
    assert request.operations.upsert_operations[0].source.archive_uri == origin_archive
    assert case.files[first_uri] == preserved_first
    assert [uri for uri in case.writes[writes_before_retry:] if uri in {first_uri, second_uri}] == [
        second_uri
    ]
    assert {item["uri"]: item["version"] for item in result.work_items} == {
        first_uri: 2,
        second_uri: 1,
    }
    assert {item["uri"]: item["source_message_ids"] for item in result.work_item_coverage} == {
        first_uri: ["m1"],
        second_uri: ["m2"],
    }
    assert case.plans[-1]["receipt_archive_uri"] == origin_archive
    assert set(case.plans[-1]["completed_uris"]) == {first_uri, second_uri}


@pytest.mark.asyncio
async def test_completed_replay_returns_bindings_and_coverage_without_submitting(replay_case):
    case = replay_case
    use_real_apply(case)
    first = await extract(case, save_work_item_replay=case.save)
    completed_plan = case.plans[-1]
    assert set(completed_plan["completed_uris"]) == {
        operation.uris[0] for operation in case.resolved.upsert_operations
    }
    files_before = dict(case.files)
    writes_before = list(case.writes)
    case.submit.reset_mock()
    case.compressor._get_or_create_react.reset_mock()

    replayed = await extract(case, work_item_replay=completed_plan, save_work_item_replay=case.save)

    case.submit.assert_not_awaited()
    case.compressor._get_or_create_react.assert_not_called()
    assert case.files == files_before
    assert case.writes == writes_before
    assert replayed.work_items == first.work_items
    assert replayed.work_item_coverage == first.work_item_coverage
    assert len(replayed.work_items) == len(replayed.work_item_coverage) == 2


@pytest.mark.asyncio
async def test_legacy_replay_discovers_stale_snapshot_without_saved_conflict_flags(replay_case):
    case = replay_case
    operation = case.resolved.upsert_operations[1]
    uri = operation.uris[0]
    await case.updater._apply_upsert(operation, case.ctx)
    operation.old_memory_file_content = MemoryFileUtils.read(case.files[uri], uri=uri)
    operation.memory_fields["current_state"] = "First session's planned update"
    case.resolved.upsert_operations = [operation]
    case.submit.side_effect = RuntimeError("crashed before submitting any write")
    save = AsyncMock()
    with pytest.raises(RuntimeError, match="before submitting"):
        await extract(case, save_work_item_replay=save)
    old_plan = json.loads(json.dumps(save.await_args.args[0]))
    for field in ("revision", "completed_uris", "conflict_uris", "receipt_archive_uri"):
        old_plan.pop(field, None)
    _advance_from_another_session(case, uri, "Another session's newer result")
    seen = _refresh_only_conflict(case, uri)
    use_real_apply(case)
    case.submit.reset_mock()
    case.writes.clear()

    result = await extract(case, work_item_replay=old_plan, save_work_item_replay=save)

    assert len(seen) == 1
    assert seen[0].old_memory_file_content.extra_fields["version"] == 2
    case.compressor._get_or_create_react.assert_called_once()
    case.submit.assert_awaited_once()
    assert case.writes == [uri]
    assert MemoryFileUtils.read(case.files[uri]).extra_fields["version"] == 3
    assert result.work_items == [{"uri": uri, "version": 3}]
    assert result.work_item_coverage == [{"uri": uri, "source_message_ids": ["m2"]}]


@pytest.mark.asyncio
async def test_activation_only_replay_reads_same_canonical_binding(replay_case):
    case = replay_case
    operation = case.resolved.upsert_operations[0]
    await case.updater._apply_upsert(operation, case.ctx)
    uri = operation.uris[0]
    activation = {"uri": uri, "version": 1, "source_message_ids": ["m1"]}
    frozen = ResolvedOperations(
        upsert_operations=[], delete_file_contents=[], errors=[], work_item_activations=[activation]
    )
    result = await extract(
        case,
        work_item_replay={
            "version": 1,
            "extraction_id": "wi-replay-activation",
            "message_ids": [message.id for message in case.messages],
            "operations": frozen.model_dump(mode="json"),
            "partial_tool_message_ids": [],
        },
    )
    assert result.work_items == [{"uri": uri, "version": 1}]
    assert result.work_item_activations == [activation]
    assert result.work_item_coverage == []
    case.compressor._get_or_create_react.assert_not_called()
    case.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_runs_other_memory_schemas_without_reassigning_work_items(replay_case):
    case = replay_case
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    case.registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/profile.yaml")
    )

    def ordinary_extraction(**kwargs):
        schemas = kwargs["context_provider"].get_memory_schemas(case.ctx)
        assert [schema.memory_type for schema in schemas] == ["profile"]
        return SimpleNamespace(
            run=AsyncMock(
                return_value=(
                    ResolvedOperations(upsert_operations=[], delete_file_contents=[], errors=[]),
                    [],
                )
            )
        )

    case.compressor._get_or_create_react = Mock(side_effect=ordinary_extraction)
    result = await case.compressor._extract_user_memories(
        messages=case.messages,
        session_id="session",
        ctx=case.ctx,
        allowed_memory_types={"work_item", "profile"},
        work_item_uris=[],
        work_item_replay=case.plans[0],
    )
    case.compressor._get_or_create_react.assert_called_once()
    assert len(case.files) == len(result.work_items) == 2
    assert len(case.writes) == 2


@pytest.mark.asyncio
async def test_replay_rejects_changed_message_batch_before_applying(replay_case):
    case = replay_case
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    before = dict(case.files)
    case.messages.reverse()
    with pytest.raises(ValueError, match="original message batch"):
        await extract(case, work_item_replay=case.plans[0])
    assert case.files == before


@pytest.mark.asyncio
async def test_replay_does_not_reinterpret_ranges_after_caption_layout_changes(
    replay_case, monkeypatch
):
    case = replay_case
    case.resolved.upsert_operations = case.resolved.upsert_operations[:1]
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)

    async def changed_caption_layout(provider):
        # Simulate a failed caption removing the first extraction-only message.
        # Raw session IDs are unchanged, but old range 0 would now mean m2.
        provider.messages = case.messages[1:]
        provider._extract_context = None

    monkeypatch.setattr(
        "openviking.session.compressor_v3.SessionExtractContextProvider.prepare_extraction_messages",
        changed_caption_layout,
    )
    result = await extract(case, work_item_replay=case.plans[0])
    assert result.work_item_coverage == [
        {"uri": case.resolved.upsert_operations[0].uris[0], "source_message_ids": ["m1"]}
    ]
    assert case.plans[0]["operations"]["upsert_operations"][0]["source_evidence_message_ids"] == [
        "m1"
    ]


@pytest.mark.asyncio
async def test_frozen_union_requires_every_destination_to_still_match_source(replay_case):
    from openviking.session.compressor_v3 import _work_item_extraction_metadata
    from openviking.session.memory.dataclass import MemoryOperationSource

    case = replay_case
    case.state.fail_after_first = False
    await extract(case, save_work_item_replay=case.save)
    uris = [op.uris[0] for op in case.resolved.upsert_operations]
    for operation in case.resolved.upsert_operations:
        operation.source = MemoryOperationSource(extraction_id=case.plans[0]["extraction_id"])
        operation.source_message_ids = []
    canonical = MemoryFileUtils.read(case.files[uris[0]], uri=uris[0])
    canonical.extra_fields.update(source_extraction_id="other-session", version=2)
    case.files[uris[0]] = MemoryFileUtils.write(canonical)
    result = MemoryUpdateResult()
    for uri in uris:
        result.add_written(uri)
    _, coverage = await _work_item_extraction_metadata(
        context_provider=SimpleNamespace(),
        operations=case.resolved,
        result=result,
        viking_fs=case.updater._viking_fs,
        ctx=case.ctx,
        frozen_source_coverage=[{"uri": uri, "source_message_ids": ["m1"]} for uri in uris],
    )
    assert coverage == []


@pytest.mark.asyncio
async def test_resolver_freezes_partial_chunk_evidence_without_claiming_full_coverage(replay_case):
    from openviking.session.memory.extract_loop import ExtractLoop
    from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
    from openviking.session.memory.memory_updater import ChunkMeta
    from openviking.session.memory.schema_model_generator import SchemaModelGenerator

    case = replay_case
    chunks = [
        Message(id=f"m1#chunk_{i}", role="user", parts=[TextPart(f"Part {i}")]) for i in (0, 1)
    ]
    context = ExtractContext(
        chunks,
        chunk_meta={
            id(message): ChunkMeta(source_message_id="m1", chunk_index=i, chunk_count=2)
            for i, message in enumerate(chunks)
        },
    )
    schema = case.registry.get("work_item")
    generator = SchemaModelGenerator([schema])
    item_model = generator.create_flat_data_model(schema)
    operations_model = generator.create_structured_operations_model()
    isolation = MemoryIsolationHandler(case.ctx, context)
    isolation.prepare_messages()
    loop = ExtractLoop(
        vlm=Mock(),
        viking_fs=case.updater._viking_fs,
        ctx=case.ctx,
        context_provider=SimpleNamespace(
            get_memory_schemas=lambda ctx: [schema],
            read_file_contents={},
            work_item_namespace="session",
        ),
        isolation_handler=isolation,
    )
    loop._extract_context = context
    operations, _ = await loop.resolve_operations(
        operations_model(
            work_item=[
                item_model(page_id=100, title="Task", goal="Finish", status="open", ranges="0")
            ],
            links=[],
            delete_ids=[],
        )
    )
    operation = operations.upsert_operations[0]
    assert operation.source_evidence_message_ids == ["m1"]
    assert operation.source_message_ids == []
    assert ResolvedOperation.model_validate(operation.model_dump()).source_evidence_message_ids == [
        "m1"
    ]


def _advance_from_another_session(case, uri, text):
    canonical = MemoryFileUtils.read(case.files[uri], uri=uri)
    canonical.extra_fields.update(
        version=canonical.extra_fields["version"] + 1,
        source_extraction_id="another-session",
        current_state=text,
        constraints="Approval is required before deploying.",
    )
    case.files[uri] = MemoryFileUtils.write(canonical)


async def _prepare_update_conflict(case):
    """The real updater commits A, then rejects B's stale canonical snapshot."""
    use_real_apply(case)
    for operation in case.resolved.upsert_operations:
        await case.updater._apply_upsert(operation, case.ctx)
        operation.old_memory_file_content = MemoryFileUtils.read(
            case.files[operation.uris[0]], uri=operation.uris[0]
        )
        operation.memory_fields["current_state"] = "First session's planned update"
    case.writes.clear()
    first_uri, conflict_uri = (operation.uris[0] for operation in case.resolved.upsert_operations)
    real_submit = case.submit.side_effect
    submitted = []

    async def submit(request):
        submitted.append([operation.uris[0] for operation in request.operations.upsert_operations])
        if len(submitted) == 1:
            _advance_from_another_session(case, conflict_uri, "Second session's newer result")
        return await real_submit(request)

    saved = []
    saves_at_writes = []

    async def save(plan):
        saved.append(json.loads(json.dumps(plan)))
        saves_at_writes.append(list(case.writes))

    case.submit.side_effect = submit
    return SimpleNamespace(
        first_uri=first_uri,
        conflict_uri=conflict_uri,
        submitted=submitted,
        saved=saved,
        saves_at_writes=saves_at_writes,
        save=save,
        real_submit=real_submit,
    )


def _refresh_only_conflict(case, conflict_uri, *, ranges=None):
    """A model response based on the latest file, retaining its new constraint."""
    seen = []

    def orchestrator(**kwargs):
        provider = kwargs["context_provider"]

        async def run():
            await provider.read_file(conflict_uri)
            latest = MemoryFileUtils.read(case.files[conflict_uri], uri=conflict_uri)
            operation = next(
                value for value in case.resolved.upsert_operations if value.uris == [conflict_uri]
            ).model_copy(deep=True)
            operation.old_memory_file_content = latest
            operation.memory_fields.update(
                current_state=latest.extra_fields["current_state"]
                + "; first-session request reconciled",
                constraints=latest.extra_fields["constraints"],
            )
            # The retry exposes only B's original evidence, renumbered from zero.
            operation.memory_fields["ranges"] = "0" if ranges is None else ranges
            seen.append(operation.model_copy(deep=True))
            return ResolvedOperations(
                upsert_operations=[operation], delete_file_contents=[], errors=[]
            ), []

        return SimpleNamespace(run=run)

    case.compressor._get_or_create_react = Mock(side_effect=orchestrator)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("first_item_updated_elsewhere", [False, True])
async def test_conflict_refresh_retries_only_failed_item_without_rewriting_success(
    replay_case, first_item_updated_elsewhere
):
    case = replay_case
    case.resolved.continuation_coverage = [
        {
            "source_message_ids": ["m1"],
            "summary": "The deployment gate is still pending.",
            "reason": "",
        }
    ]
    conflict = await _prepare_update_conflict(case)
    with pytest.raises(ConflictError):
        await extract(case, save_work_item_replay=conflict.save)
    assert case.writes == [conflict.first_uri]
    assert conflict.saved[-1]["completed_uris"] == [conflict.first_uri]
    assert conflict.saved[-1]["conflict_uris"] == [conflict.conflict_uri]
    if first_item_updated_elsewhere:
        _advance_from_another_session(case, conflict.first_uri, "A was independently completed")
    first_canonical = case.files[conflict.first_uri]
    failed_plan = json.loads(json.dumps(conflict.saved[-1]))
    refreshed = _refresh_only_conflict(case, conflict.conflict_uri)

    result = await extract(case, work_item_replay=failed_plan, save_work_item_replay=conflict.save)

    assert len(refreshed) == 1
    case.compressor._get_or_create_react.assert_called_once()
    assert conflict.submitted[-1] == [conflict.conflict_uri]
    assert case.files[conflict.first_uri] == first_canonical
    assert case.writes == [conflict.first_uri, conflict.conflict_uri]
    current = MemoryFileUtils.read(case.files[conflict.conflict_uri])
    assert current.extra_fields["version"] == 3
    assert "Second session's newer result" in current.extra_fields["current_state"]
    assert current.extra_fields["constraints"] == "Approval is required before deploying."
    revision_saves = [
        (plan, writes)
        for plan, writes in zip(conflict.saved, conflict.saves_at_writes, strict=True)
        if plan.get("revision", 0) > failed_plan.get("revision", 0)
        and any(
            operation["uris"] == [conflict.conflict_uri]
            and operation.get("old_memory_file_content", {}).get("extra_fields", {}).get("version")
            == 2
            for operation in plan["operations"]["upsert_operations"]
        )
    ]
    assert revision_saves, "the reconciled decision must be saved with a newer revision"
    assert revision_saves[0][1] == [conflict.first_uri], "save revision before writing B"
    assert {item["uri"] for item in result.work_items} == {
        conflict.first_uri,
        conflict.conflict_uri,
    }
    assert {
        identity for row in result.work_item_coverage for identity in row["source_message_ids"]
    } == {"m1", "m2"}
    assert result.continuation_coverage == case.resolved.continuation_coverage


@pytest.mark.asyncio
async def test_conflict_revision_save_failure_prevents_any_repaired_write(replay_case):
    case = replay_case
    conflict = await _prepare_update_conflict(case)
    with pytest.raises(ConflictError):
        await extract(case, save_work_item_replay=conflict.save)
    _refresh_only_conflict(case, conflict.conflict_uri)
    before = dict(case.files)
    submitted = case.submit.await_count
    with pytest.raises(OSError, match="revision persistence unavailable"):
        await extract(
            case,
            work_item_replay=conflict.saved[-1],
            save_work_item_replay=AsyncMock(
                side_effect=OSError("revision persistence unavailable")
            ),
        )
    assert case.files == before
    assert case.submit.await_count == submitted


@pytest.mark.asyncio
async def test_repeated_competing_updates_have_one_bounded_refresh_per_retry(replay_case):
    case = replay_case
    conflict = await _prepare_update_conflict(case)
    with pytest.raises(ConflictError):
        await extract(case, save_work_item_replay=conflict.save)
    refreshed = _refresh_only_conflict(case, conflict.conflict_uri)

    async def competing_submit(request):
        assert [operation.uris[0] for operation in request.operations.upsert_operations] == [
            conflict.conflict_uri
        ]
        _advance_from_another_session(case, conflict.conflict_uri, "Concurrent progress continues")
        return await conflict.real_submit(request)

    case.submit.side_effect = competing_submit
    for attempt in range(3):
        before_calls = case.submit.await_count
        before_revision = conflict.saved[-1].get("revision", 0)
        with pytest.raises(ConflictError):
            await extract(
                case,
                work_item_replay=conflict.saved[-1],
                save_work_item_replay=conflict.save,
            )
        assert len(refreshed) == attempt + 1
        assert case.submit.await_count == before_calls + 1
        assert conflict.saved[-1].get("revision", 0) > before_revision
        assert conflict.saved[-1]["conflict_uris"] == [conflict.conflict_uri]
        assert conflict.saved[-1]["completed_uris"] == [conflict.first_uri]
    assert case.writes == [conflict.first_uri]


@pytest.mark.asyncio
async def test_conflicted_create_reuses_its_original_identity_when_reconciled(replay_case):
    case = replay_case
    use_real_apply(case)
    conflict_uri = case.resolved.upsert_operations[1].uris[0]
    real_submit = case.submit.side_effect
    saved = []
    initial = True

    async def submit(request):
        nonlocal initial
        if initial:
            initial = False
            # Another session creates the same stable task identity first.
            await case.updater._apply_upsert(case.resolved.upsert_operations[1], case.ctx)
            canonical = MemoryFileUtils.read(case.files[conflict_uri], uri=conflict_uri)
            canonical.extra_fields.update(
                source_extraction_id="another-session",
                current_state="Already created by another session",
                constraints="Approval is required before deploying.",
            )
            case.files[conflict_uri] = MemoryFileUtils.write(canonical)
        return await real_submit(request)

    async def save(plan):
        saved.append(json.loads(json.dumps(plan)))

    case.submit.side_effect = submit
    with pytest.raises(ConflictError, match="already exists"):
        await extract(case, save_work_item_replay=save)
    expected_uris = {operation.uris[0] for operation in case.resolved.upsert_operations}
    _refresh_only_conflict(case, conflict_uri)
    await extract(case, work_item_replay=saved[-1], save_work_item_replay=save)
    assert set(case.files) == expected_uris
    assert MemoryFileUtils.read(case.files[conflict_uri]).extra_fields["version"] == 2
    assert {
        uri
        for operation in saved[-1]["operations"]["upsert_operations"]
        for uri in operation["uris"]
    } == expected_uris


@pytest.mark.asyncio
async def test_removed_create_collision_clears_stale_conflict_without_reextracting(replay_case):
    case = replay_case
    use_real_apply(case)
    first, conflicting = case.resolved.upsert_operations
    first_uri, conflict_uri = first.uris[0], conflicting.uris[0]
    await case.updater._apply_upsert(conflicting, case.ctx)
    save = AsyncMock()
    with pytest.raises(ConflictError, match="already exists"):
        await extract(case, save_work_item_replay=save)
    failed_plan = json.loads(json.dumps(save.await_args.args[0]))
    assert failed_plan["completed_uris"] == [first_uri]
    assert failed_plan["conflict_uris"] == [conflict_uri]

    # Removing the competing file makes the frozen create valid again.
    del case.files[conflict_uri]
    case.writes.clear()
    case.submit.reset_mock()
    case.compressor._get_or_create_react.reset_mock()
    result = await extract(case, work_item_replay=failed_plan, save_work_item_replay=save)

    case.compressor._get_or_create_react.assert_not_called()
    case.submit.assert_awaited_once()
    request = case.submit.await_args.args[0]
    assert [operation.uris for operation in request.operations.upsert_operations] == [
        [conflict_uri]
    ]
    assert case.writes == [conflict_uri]
    assert MemoryFileUtils.read(case.files[conflict_uri]).extra_fields["version"] == 1
    assert save.await_args.args[0]["conflict_uris"] == []
    assert set(save.await_args.args[0]["completed_uris"]) == {first_uri, conflict_uri}
    assert len(result.work_items) == len(result.work_item_coverage) == 2


@pytest.mark.asyncio
async def test_conflict_refresh_keeps_frozen_source_ids_when_caption_positions_change(
    replay_case, monkeypatch
):
    case = replay_case
    case.state.partial = {"m2"}
    conflict = await _prepare_update_conflict(case)
    with pytest.raises(ConflictError):
        await extract(case, save_work_item_replay=conflict.save)

    async def changed_caption_layout(provider):
        provider.messages = case.messages[1:]
        provider._extract_context = None

    monkeypatch.setattr(
        "openviking.session.compressor_v3.SessionExtractContextProvider.prepare_extraction_messages",
        changed_caption_layout,
    )
    _refresh_only_conflict(case, conflict.conflict_uri, ranges="0")
    result = await extract(
        case, work_item_replay=conflict.saved[-1], save_work_item_replay=conflict.save
    )
    assert {row["uri"]: row["source_message_ids"] for row in result.work_item_coverage} == {
        conflict.first_uri: ["m1"],
        conflict.conflict_uri: ["m2"],
    }
    assert conflict.saved[-1]["partial_tool_message_ids"] == ["m2"]
    saved_operations = {
        operation["uris"][0]: operation
        for operation in conflict.saved[-1]["operations"]["upsert_operations"]
    }
    assert saved_operations[conflict.first_uri]["source_evidence_message_ids"] == ["m1"]
    assert saved_operations[conflict.conflict_uri]["source_evidence_message_ids"] == ["m2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["new_uri", "missing_snapshot", "extra_continuation"])
async def test_conflict_refresh_rejects_decisions_outside_the_failed_update(replay_case, invalid):
    case = replay_case
    conflict = await _prepare_update_conflict(case)
    with pytest.raises(ConflictError):
        await extract(case, save_work_item_replay=conflict.save)
    repaired = case.resolved.upsert_operations[1].model_copy(deep=True)
    repaired.old_memory_file_content = MemoryFileUtils.read(
        case.files[conflict.conflict_uri], uri=conflict.conflict_uri
    )
    operations = ResolvedOperations(
        upsert_operations=[repaired], delete_file_contents=[], errors=[]
    )
    if invalid == "new_uri":
        identity = new_work_item_id(ExtractContext(case.messages), "1", 9, "session")
        repaired.uris = [f"viking://user/alice/memories/work_item/{identity}.md"]
        repaired.memory_fields["work_item_id"] = identity
        repaired.old_memory_file_content = None
    elif invalid == "missing_snapshot":
        repaired.old_memory_file_content = None
    else:
        operations.continuation_coverage = [
            {
                "source_message_ids": ["m1"],
                "summary": "",
                "reason": "Drop another task's constraint",
            }
        ]
    case.compressor._get_or_create_react = Mock(
        return_value=SimpleNamespace(run=AsyncMock(return_value=(operations, [])))
    )
    before = dict(case.files)
    submissions = case.submit.await_count
    with pytest.raises((ValueError, ConflictError)):
        await extract(
            case, work_item_replay=conflict.saved[-1], save_work_item_replay=conflict.save
        )
    assert case.files == before
    assert case.submit.await_count == submissions
