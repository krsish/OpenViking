# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.message import Message, TextPart
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
    assert len(case.files) == len(case.plans) == 1

    result = await extract(case, work_item_replay=case.plans[0])
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
async def test_replay_preserves_partial_evidence_exclusion_from_union_coverage(replay_case):
    case = replay_case
    case.state.partial = {"m2"}
    with pytest.raises(RuntimeError, match="interrupted"):
        await extract(case, save_work_item_replay=case.save)
    result = await extract(case, work_item_replay=case.plans[0])
    assert [
        identity for item in result.work_item_coverage for identity in item["source_message_ids"]
    ] == ["m1"]


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
