# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Work-item identity, bounded state, and stale-session safety contracts."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openviking.message import Message
from openviking.message.part import TextPart, ToolPart
from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session.compressor_v3 import _v3_extraction_response, _work_item_extraction_metadata
from openviking.session.memory.dataclass import (
    MemoryFile,
    MemoryOperationSource,
    ResolvedOperation,
    ResolvedOperations,
)
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import ExtractContext, MemoryUpdater
from openviking.session.memory.schema_model_generator import SchemaModelGenerator
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.work_item import covered_source_message_ids, new_work_item_id
from openviking.storage.content_write import ContentWriteCoordinator
from openviking_cli.exceptions import ConflictError, InvalidArgumentError
from openviking_cli.session.user_id import UserIdentifier

URI = "viking://user/alice/memories/work_item/wi-one.md"


@pytest.fixture
def ctx():
    return RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)


@pytest.fixture
def registry():
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory" / "work_item.yaml")
    )
    return registry


class Files:
    def __init__(self):
        self.values = {}
        self.read_error = None

    async def read_file(self, uri, **kwargs):
        if self.read_error:
            raise self.read_error
        if uri not in self.values:
            raise FileNotFoundError(uri)
        return self.values[uri]

    async def write_file(self, uri, content, **kwargs):
        self.values[uri] = content

    def parsed(self, uri=URI):
        return MemoryFileUtils.read(self.values[uri], uri=uri)


def op(snapshot=None, **fields):
    data = {
        "work_item_id": "wi-one",
        "title": "Fix compaction",
        "goal": "Bound session memory",
        "status": "open",
    }
    data.update(fields)
    return ResolvedOperation(
        old_memory_file_content=snapshot,
        memory_fields=data,
        memory_type="work_item",
        uris=[URI],
        source_message_ids=["msg"],
        source=MemoryOperationSource(extraction_id="extract1"),
    )


def setup_updater(registry):
    updater = MemoryUpdater(registry=registry)
    fs = Files()
    updater._viking_fs = fs
    return updater, fs


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_terminal_time_uses_frozen_source_ids_after_range_layout_changes(
    registry, ctx, split
):
    updater, fs = setup_updater(registry)
    completion = Message(
        id="completion",
        role="user",
        created_at="2020-01-01T00:01:00+00:00",
        parts=[TextPart("Finished and verified. " * (1000 if split else 1))],
    )
    unrelated = Message(
        id="unrelated",
        role="user",
        created_at="2020-01-01T00:03:00+00:00",
        parts=[TextPart("A later message about something else.")],
    )
    context = ExtractContext([completion, unrelated])
    operation = op(status="done", ranges=str(len(context.messages) - 1))
    operation.source_message_ids = ["completion"]
    operation.source_evidence_message_ids = ["completion"]
    await updater._apply_upsert(operation, ctx, context)
    terminal = fs.parsed()
    assert terminal.extra_fields["terminal_evidence_at"] == "2020-01-01T00:01:00+00:00"
    reopen = Message(
        id="reopen",
        role="user",
        created_at="2020-01-01T00:02:00+00:00",
        parts=[TextPart("Please reopen this work.")],
    )
    operation = op(terminal, reopen_reason="Please reopen this work.")
    operation.source_message_ids = ["reopen"]
    await updater._apply_upsert(operation, ctx, ExtractContext([reopen]))
    assert fs.parsed().extra_fields["status"] == "open"


@pytest.mark.asyncio
async def test_latest_version_guard_and_explicit_clear(registry, ctx):
    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(waiting_for="User decision"), ctx)
    snapshot = fs.parsed()
    await updater._apply_upsert(op(snapshot, waiting_for="", current_state="Implemented"), ctx)
    assert fs.parsed().extra_fields["waiting_for"] == ""
    assert fs.parsed().extra_fields["version"] == 2
    assert "Implemented" in fs.parsed().content
    with pytest.raises(ConflictError, match="version changed"):
        await updater._apply_upsert(op(snapshot, current_state="Old state"), ctx)
    assert fs.parsed().extra_fields["current_state"] == "Implemented"


@pytest.mark.asyncio
async def test_terminal_requires_current_user_reopen_evidence(registry, ctx):
    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(status="done"), ctx)
    snapshot = fs.parsed()
    with pytest.raises(ValueError, match="current user request"):
        await updater._apply_upsert(op(snapshot), ctx)
    messages = [
        Message(
            id="msg",
            role="user",
            created_at=datetime.now(timezone.utc).isoformat(),
            parts=[TextPart(text="Please reopen this work.")],
        )
    ]
    await updater._apply_upsert(
        op(snapshot, reopen_reason="Please reopen this work."), ctx, ExtractContext(messages)
    )
    assert fs.parsed().extra_fields["status"] == "open"
    assert "reopen_reason" not in fs.parsed().extra_fields


@pytest.mark.asyncio
async def test_size_guard_keeps_previous_body_and_rejects_unread_collision(registry, ctx):
    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(), ctx)
    before = fs.values[URI]
    with pytest.raises(ValueError, match="exceeds"):
        await updater._apply_upsert(op(fs.parsed(), current_state="增长" * 5000), ctx)
    assert fs.values[URI] == before
    with pytest.raises(ConflictError, match="already exists"):
        await updater._apply_upsert(op(), ctx)
    assert fs.values[URI] == before


@pytest.mark.asyncio
async def test_latest_read_failure_and_removed_snapshot_fail_closed(registry, ctx):
    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(), ctx)
    snapshot = fs.parsed()
    fs.read_error = OSError("unavailable")
    with pytest.raises(OSError):
        await updater._apply_upsert(op(snapshot), ctx)
    fs.read_error = None
    del fs.values[URI]
    with pytest.raises(ConflictError, match="removed"):
        await updater._apply_upsert(op(snapshot), ctx)
    assert not fs.values


@pytest.mark.asyncio
async def test_wrong_user_uri_type_and_delete_are_rejected(registry, ctx):
    updater, fs = setup_updater(registry)
    wrong_user = op()
    wrong_user.uris = [URI.replace("alice", "bob")]
    with pytest.raises(ValueError, match="canonical user"):
        await updater._apply_upsert(wrong_user, ctx)
    wrong_type = op()
    wrong_type.memory_type = "preferences"
    with pytest.raises(ValueError, match="canonical memory type"):
        await updater._apply_upsert(wrong_type, ctx)
    with pytest.raises(ValueError, match="cannot delete"):
        await updater._apply_delete(URI, ctx)
    assert not fs.values


def test_retry_identity_and_full_chunk_coverage():
    messages = [
        Message(
            id="raw",
            role="user",
            parts=[TextPart(text="A lengthy sentence about a still-open issue. " * 100)],
        )
    ]
    context = ExtractContext(messages)
    assert len(context.messages) > 1
    assert covered_source_message_ids(context, "0") == []
    assert covered_source_message_ids(context, f"0-{len(context.messages) - 1}") == ["raw"]
    assert new_work_item_id(context, "0", 0, "session") == new_work_item_id(
        ExtractContext(messages), "0", 0, "session"
    )
    assert new_work_item_id(context, "0", 0, "other") != new_work_item_id(
        context, "0", 0, "session"
    )
    with pytest.raises(ValueError, match="outside"):
        covered_source_message_ids(context, "9999")


@pytest.mark.asyncio
async def test_resolver_assigns_id_and_existing_page_keeps_it(registry, ctx):
    messages = [Message(id="msg", role="user", parts=[TextPart(text="Fix the work-item issue")])]
    context = ExtractContext(messages)
    schema = registry.get("work_item")
    generator = SchemaModelGenerator([schema])
    model = generator.create_flat_data_model(schema)
    operations_model = generator.create_structured_operations_model()
    assert "work_item_id" not in model.model_fields
    item = model(page_id=100, title="Fix", goal="Fix issue", status="open", ranges="0")
    provider = SimpleNamespace(
        get_memory_schemas=lambda ctx: [schema],
        read_file_contents={},
        work_item_namespace="session",
    )
    isolation = MemoryIsolationHandler(ctx, context)
    isolation.prepare_messages()
    loop = ExtractLoop(
        vlm=MagicMock(),
        viking_fs=MagicMock(),
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
    )
    loop._extract_context = context
    resolved, _ = await loop.resolve_operations(
        operations_model(work_item=[item], links=[], delete_ids=[])
    )
    created = resolved.upsert_operations[0]
    assert created.source_message_ids == ["msg"]
    identity = created.memory_fields["work_item_id"]
    assert created.uris == [f"viking://user/alice/memories/work_item/{identity}.md"]
    old = MemoryFile(
        uri=created.uris[0], memory_type="work_item", extra_fields=created.memory_fields
    )
    provider.read_file_contents[old.uri] = old
    page_id = context.page_id_map.get_page_id(old.uri)
    existing = model(page_id=page_id, title="Renamed", ranges="0")
    resolved, _ = await loop.resolve_operations(
        operations_model(work_item=[existing], links=[], delete_ids=[])
    )
    assert resolved.upsert_operations[0].memory_fields["work_item_id"] == identity
    assert resolved.upsert_operations[0].uris == [old.uri]


@pytest.mark.asyncio
async def test_exact_prefetch_and_bounded_tools_are_scoped_to_work_items(registry, ctx):
    output = "tool-output" * 1000
    ref = "viking://user/alice/sessions/test/tool-results/tool-one"
    messages = [
        Message(
            id="msg",
            role="assistant",
            parts=[
                ToolPart(
                    tool_name="run",
                    tool_status="completed",
                    tool_input={"command": "i" * 1000},
                    tool_output=output,
                    tool_output_ref=ref,
                )
            ],
        )
    ]
    provider = SessionExtractContextProvider(
        messages,
        ctx=ctx,
        viking_fs=MagicMock(),
        memory_registry=registry,
        work_item_uris=[URI, URI.replace("alice", "bob")],
    )
    provider._isolation_handler = MemoryIsolationHandler(ctx, provider.get_extract_context())
    provider.search_files = AsyncMock(return_value=[])
    provider._append_structured_read_result = AsyncMock(return_value=1)
    result = await provider.prefetch()
    assert [
        call.kwargs["file_uri"] for call in provider._append_structured_read_result.call_args_list
    ] == [URI]
    evidence = next(
        message["content"]
        for message in result
        if str(message.get("content", "")).startswith("## Execution evidence")
    )
    assert output[:500] in evidence
    assert output not in evidence
    assert "i" * 1000 in evidence
    assert evidence.count("more characters truncated]") == 1
    assert "not complete execution evidence" in evidence
    assert ref in evidence
    assert provider.work_item_partial_tool_message_ids == {"msg"}
    assert messages[0].parts[0].tool_output == output
    provider._isolation_handler = MemoryIsolationHandler(
        ctx, provider.get_extract_context(), allowed_memory_types=set()
    )
    provider._append_structured_read_result.reset_mock()
    result = await provider.prefetch()
    provider._append_structured_read_result.assert_not_called()
    assert output not in "\n".join(str(message.get("content", "")) for message in result)
    assert not any(
        str(message.get("content", "")).startswith("## Execution evidence") for message in result
    )


@pytest.mark.parametrize("text", ["x" * 500, "结果" * 250, "🙂" * 500])
def test_work_item_tool_evidence_budget_preserves_recent_refs(text, ctx):
    from openviking.utils.token_estimation import estimate_text_tokens

    messages = [
        Message(
            id=f"msg-{index}",
            role="assistant",
            parts=[
                ToolPart(
                    tool_name=f"tool-{index}",
                    tool_status="completed",
                    tool_output=text,
                    tool_output_ref=f"viking://user/alice/sessions/test/tool-results/{index}",
                )
            ],
        )
        for index in range(150)
    ]
    provider = SessionExtractContextProvider(messages, ctx=ctx)

    evidence = provider._build_work_item_tool_evidence()

    assert estimate_text_tokens(evidence) <= 16000
    assert "Some tool evidence was omitted" in evidence
    assert "tool=tool-149;" in evidence
    assert "tool-results/149" in evidence
    assert "tool=tool-0;" not in evidence
    assert "msg-0" in provider.work_item_partial_tool_message_ids
    assert "msg-149" not in provider.work_item_partial_tool_message_ids


def test_work_item_tool_evidence_bounds_metadata_without_claiming_it_was_read(ctx):
    messages = [
        Message(
            id="large-metadata",
            role="assistant",
            parts=[ToolPart(tool_name="n" * 100000, tool_output="done")],
        )
    ]
    provider = SessionExtractContextProvider(messages, ctx=ctx)

    evidence = provider._build_work_item_tool_evidence()

    assert "Some tool evidence was omitted" in evidence
    assert "tool=" not in evidence
    assert provider.work_item_partial_tool_message_ids == {"large-metadata"}


@pytest.mark.parametrize(
    "output_length, original_chars, partial", [(30, 300, True), (300, 300, False), (30, None, True)]
)
def test_work_item_tool_evidence_marks_unhydrated_result_partial(
    output_length, original_chars, partial, ctx
):
    messages = [
        Message(
            id="externalized",
            role="assistant",
            parts=[
                ToolPart(
                    tool_name="read",
                    tool_output="x" * output_length,
                    tool_output_ref="viking://user/alice/sessions/test/tool-results/result",
                    tool_output_truncated=True,
                    tool_output_original_chars=original_chars,
                )
            ],
        )
    ]
    provider = SessionExtractContextProvider(messages, ctx=ctx)

    provider._build_work_item_tool_evidence()

    assert ("externalized" in provider.work_item_partial_tool_message_ids) is partial


@pytest.mark.parametrize("body", ["x" * 12000, "结果" * 1500, "🙂" * 3000])
def test_tool_preview_keeps_first_2000_characters_and_marks_truncation(body, ctx):
    output = "Starting tests\n" + body + "\nFAILED: rerun the failing test"
    message = Message(
        id="long-result",
        role="assistant",
        parts=[ToolPart(tool_name="bash", tool_output=output)],
    )
    provider = SessionExtractContextProvider([message], ctx=ctx)

    evidence = provider._build_work_item_tool_evidence()
    preview = evidence.split("; output=", 1)[1]

    assert preview == output[:2000] + f"\n\n[... {len(output) - 2000} more characters truncated]"
    assert "FAILED: rerun the failing test" not in preview
    assert provider.work_item_partial_tool_message_ids == {message.id}
    assert message.parts[0].tool_output == output


def test_tool_output_above_500_characters_is_not_automatically_partial(ctx):
    output = "visible result " * 100
    source = Message(
        id="result", role="assistant", parts=[ToolPart(tool_name="read", tool_output=output)]
    )
    provider = SessionExtractContextProvider([source], ctx=ctx)

    assert output in provider._build_work_item_tool_evidence()
    assert provider.work_item_partial_tool_message_ids == set()


@pytest.mark.parametrize("char", ["x", "结", "🙂"])
@pytest.mark.parametrize("length", [2000, 2001])
def test_tool_preview_character_limit_boundary(char, length, ctx):
    source = Message(
        id="result",
        role="assistant",
        parts=[ToolPart(tool_name="read", tool_output=char * length)],
    )
    provider = SessionExtractContextProvider([source], ctx=ctx)

    evidence = provider._build_work_item_tool_evidence()

    assert char * 2000 in evidence
    assert ("more characters truncated]" in evidence) is (length > 2000)
    assert (source.id in provider.work_item_partial_tool_message_ids) is (length > 2000)


@pytest.mark.asyncio
async def test_partial_tool_can_support_work_item_state_and_attribution(registry, ctx):
    messages = [
        Message(id="request", role="user", parts=[TextPart("Fix the tests")]),
        Message(
            id="result",
            role="assistant",
            parts=[ToolPart(tool_name="bash", tool_output="1 test failed\n" + "logs " * 3000)],
        ),
    ]
    provider = SessionExtractContextProvider(messages, ctx=ctx, memory_registry=registry)
    context = provider.get_extract_context()
    isolation = MemoryIsolationHandler(ctx, context)
    isolation.prepare_messages()
    provider._build_work_item_tool_evidence()
    assert provider.work_item_partial_tool_message_ids == {"result"}
    loop = ExtractLoop(
        vlm=MagicMock(),
        viking_fs=MagicMock(),
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
    )
    loop._extract_context = context
    generator = SchemaModelGenerator([registry.get("work_item")])
    model = generator.create_structured_operations_model()
    operations = model.model_validate(
        {
            "work_item": [
                {
                    "page_id": 100,
                    "title": "Fix tests",
                    "goal": "Tests pass",
                    "status": "in_progress",
                    "current_state": "One test still fails",
                    "next_action": "Inspect the failed test",
                    "ranges": "0-1",
                }
            ]
        }
    )

    resolved, _ = await loop.resolve_operations(operations)

    assert resolved.errors == []
    operation = resolved.upsert_operations[0]
    assert operation.source_message_ids == ["request", "result"]
    assert operation.memory_fields["status"] == "in_progress"
    assert operation.memory_fields["current_state"] == "One test still fails"


@pytest.mark.asyncio
async def test_no_change_binding_is_not_coverage_and_only_applied_source_counts(registry, ctx):
    updater, fs = setup_updater(registry)
    operation = op()
    await updater._apply_upsert(operation, ctx)
    provider = SimpleNamespace(work_item_uris=[URI], read_file_contents={URI: fs.parsed()})
    bindings, coverage = await _work_item_extraction_metadata(
        context_provider=provider, operations=None, result=None, viking_fs=fs, ctx=ctx
    )
    assert bindings == [{"uri": URI, "version": 1}]
    assert coverage == []
    operations = ResolvedOperations(
        upsert_operations=[operation], delete_file_contents=[], errors=[]
    )
    result = SimpleNamespace(written_uris=[URI], edited_uris=[])
    _, coverage = await _work_item_extraction_metadata(
        context_provider=provider, operations=operations, result=result, viking_fs=fs, ctx=ctx
    )
    assert coverage == [{"uri": URI, "source_message_ids": ["msg"]}]
    operation.source.extraction_id = "failed-different-session"
    _, coverage = await _work_item_extraction_metadata(
        context_provider=provider, operations=operations, result=result, viking_fs=fs, ctx=ctx
    )
    assert coverage == []
    assert _v3_extraction_response(contexts=[], train_result={}, archive_uri="") == []
    assert (
        _v3_extraction_response(
            contexts=[], train_result={}, archive_uri="", include_work_items=True
        )["work_item_coverage"]
        == []
    )


def test_generic_content_writes_cannot_bypass_work_item_updater():
    coordinator = ContentWriteCoordinator(MagicMock())
    with pytest.raises(InvalidArgumentError, match="extraction/compile"):
        coordinator._ensure_content_write_policy(URI)
    coordinator._ensure_content_write_policy("viking://user/alice/memories/preferences/pref.md")


@pytest.mark.asyncio
async def test_cross_session_merge_preserves_each_snapshot_for_version_check(
    registry, ctx, monkeypatch
):
    from openviking.session.memory.streaming_memory_updater import merge_one_memory_type_operations

    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(), ctx)
    snapshot = fs.parsed()
    first = op(snapshot, current_state="new progress")
    second = op(snapshot, current_state="old session")
    second.source.extraction_id = "extract2"
    merged = await merge_one_memory_type_operations(
        memory_type="work_item",
        operations=[first, second],
        messages=[],
        ctx=ctx,
        registry=registry,
        force_merge=True,
    )
    assert merged.upsert_operations == [first, second]
    assert all(
        item.old_memory_file_content.extra_fields["version"] == 1
        for item in merged.upsert_operations
    )
    await updater._apply_upsert(merged.upsert_operations[0], ctx)
    with pytest.raises(ConflictError, match="version changed"):
        await updater._apply_upsert(merged.upsert_operations[1], ctx)
    assert fs.parsed().extra_fields["current_state"] == "new progress"


@pytest.mark.asyncio
async def test_custom_template_cannot_bypass_body_cap(registry, ctx):
    updater, fs = setup_updater(registry)
    registry.get("work_item").content_template = "large custom body " * 5000
    with pytest.raises(ValueError, match="rendered body"):
        await updater._apply_upsert(op(), ctx)
    assert not fs.values


@pytest.mark.asyncio
async def test_reopen_evidence_survives_extraction_chunking(registry, ctx):
    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(status="done"), ctx)
    messages = [
        Message(
            id="msg",
            role="user",
            created_at=datetime.now(timezone.utc).isoformat(),
            parts=[
                TextPart(
                    text="Please reopen this work. " + "Detailed follow-up requirements. " * 100
                )
            ],
        )
    ]
    context = ExtractContext(messages)
    assert len(context.messages) > 1
    await updater._apply_upsert(
        op(fs.parsed(), reopen_reason="Please reopen this work."),
        ctx,
        context,
    )
    assert fs.parsed().extra_fields["status"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_name", ["python", "json"])
async def test_resume_without_state_change_activates_only_selected_read_item(
    registry, ctx, protocol_name
):
    from openviking.session.compressor_v3 import _work_item_activation_receipts
    from openviking.session.memory.extraction_output_protocol import (
        ExtractionOutputContext,
        create_extraction_output_protocol,
    )

    updater, fs = setup_updater(registry)
    await updater._apply_upsert(op(status="waiting", waiting_for="Finance sample"), ctx)
    before = fs.values[URI]
    messages = [
        Message(
            id="return",
            role="user",
            peer_id="workspace-repo",
            parts=[TextPart(text="Continue the CSV issue")],
        )
    ]
    context = ExtractContext(messages)
    context.page_id_map.get_page_id(URI)
    schema = registry.get("work_item")
    model = SchemaModelGenerator([schema]).create_structured_operations_model()
    read_files = {URI: fs.parsed()}
    protocol_context = ExtractionOutputContext(
        operations_model=model,
        schemas=(schema,),
        page_id_map=context.page_id_map,
        read_file_contents=read_files,
        link_enabled=False,
    )
    protocol = create_extraction_output_protocol(protocol_name)
    protocol.render_contract(protocol_context)
    protocol.render_new_bindings(protocol_context, source="candidate read")
    response = (
        'work_item_1.activate(ranges="0")\nsdk.commit()'
        if protocol_name == "python"
        else '{"work_item_activations": [{"page_id": 1, "ranges": "0"}]}'
    )
    operations, error = protocol.parse(response, protocol_context)
    assert error is None
    assert not operations.is_empty()
    assert operations.work_item == []
    provider = SimpleNamespace(
        get_memory_schemas=lambda ctx: [schema],
        read_file_contents=read_files,
        work_item_uris=[],
        get_extract_context=lambda: context,
    )
    isolation = MemoryIsolationHandler(ctx, context)
    isolation.prepare_messages()
    loop = ExtractLoop(
        vlm=MagicMock(),
        viking_fs=fs,
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
    )
    loop._extract_context = context
    resolved, _ = await loop.resolve_operations(operations)
    assert resolved.upsert_operations == []
    assert resolved.work_item_activations == [
        {"uri": URI, "version": 1, "source_message_ids": ["return"]}
    ]
    bindings, coverage = await _work_item_extraction_metadata(
        context_provider=provider,
        operations=resolved,
        result=None,
        viking_fs=fs,
        ctx=ctx,
        activations=resolved.work_item_activations,
    )
    assert bindings == [{"uri": URI, "version": 1}]
    assert coverage == []
    assert (
        _work_item_activation_receipts(resolved.work_item_activations, bindings)
        == resolved.work_item_activations
    )
    assert fs.values[URI] == before
    # A search/read candidate without explicit selection never becomes an active binding.
    bindings, _ = await _work_item_extraction_metadata(
        context_provider=provider,
        operations=None,
        result=None,
        viking_fs=fs,
        ctx=ctx,
    )
    assert bindings == []
    # A different session completing the item between extraction and receipt prevents revival.
    await updater._apply_upsert(op(fs.parsed(), status="done"), ctx)
    bindings, _ = await _work_item_extraction_metadata(
        context_provider=provider,
        operations=None,
        result=None,
        viking_fs=fs,
        ctx=ctx,
        activations=resolved.work_item_activations,
    )
    assert _work_item_activation_receipts(resolved.work_item_activations, bindings) == []


@pytest.mark.asyncio
async def test_resume_candidates_are_bounded_read_only_and_latest_query_first(registry, ctx):
    messages = [
        Message(id="old", role="user", parts=[TextPart(text="OLD unrelated work " * 100)]),
        Message(
            id="new", role="user", parts=[TextPart(text="Resume the broken quoted CSV export")]
        ),
    ]
    provider = SessionExtractContextProvider(
        messages, ctx=ctx, viking_fs=MagicMock(), memory_registry=registry, work_item_uris=[URI]
    )
    provider._eager_prefetch = False
    provider._isolation_handler = MemoryIsolationHandler(ctx, provider.get_extract_context())
    candidates = [URI.replace("wi-one", f"wi-candidate{i}") for i in range(4)]
    provider.search_files = AsyncMock(return_value=[URI.replace("alice", "bob"), *candidates])
    provider._append_structured_read_result = AsyncMock(return_value=1)
    await provider.prefetch()
    assert provider.search_files.await_count == 1
    query = provider.search_files.call_args.kwargs["query"]
    assert "quoted CSV" in query and "OLD" not in query
    read_uris = {
        call.kwargs["file_uri"] for call in provider._append_structured_read_result.call_args_list
    }
    assert read_uris == {URI, *candidates[:3]}
    assert provider.work_item_uris == [URI]
    assert (
        "work_item_activations"
        not in SchemaModelGenerator([]).create_structured_operations_model().model_fields
    )


@pytest.mark.parametrize("invalid", ["unread", "other_user", "assistant", "terminal"])
def test_activation_rejects_invalid_sources_and_never_reopens(ctx, invalid):
    from openviking.session.memory.schema_model_generator import WorkItemActivation
    from openviking.session.memory.work_item import resolve_work_item_activations

    context = ExtractContext(
        [
            Message(
                id="return",
                role="assistant" if invalid == "assistant" else "user",
                parts=[TextPart(text="Resume the issue")],
            )
        ]
    )
    uri = URI.replace("alice", "bob") if invalid == "other_user" else URI
    page_id = context.page_id_map.get_page_id(uri)
    file = MemoryFile(
        uri=uri,
        memory_type="work_item",
        extra_fields={
            "work_item_id": "wi-one",
            "status": "done" if invalid == "terminal" else "waiting",
            "version": 1,
        },
    )

    def resolve():
        return resolve_work_item_activations(
            [WorkItemActivation(page_id=page_id, ranges="0")],
            extract_context=context,
            read_files={} if invalid == "unread" else {uri: file},
            ctx=ctx,
        )

    if invalid == "terminal":
        assert resolve() == []
    else:
        with pytest.raises(ValueError):
            resolve()


@pytest.mark.asyncio
async def test_coverage_unions_only_persisted_chunks_across_work_items(registry, ctx):
    messages = [
        Message(id="raw", role="user", parts=[TextPart(text="A detailed work request. " * 150)])
    ]
    context = ExtractContext(messages)
    assert len(context.messages) >= 2
    updater, fs = setup_updater(registry)
    a = op(ranges="0")
    a.source_message_ids = []
    b = op(ranges=f"1-{len(context.messages) - 1}", work_item_id="wi-two", title="Second work")
    b.uris = [URI.replace("wi-one", "wi-two")]
    b.source_message_ids = []
    await updater._apply_upsert(a, ctx)
    await updater._apply_upsert(b, ctx)
    operations = ResolvedOperations(upsert_operations=[a, b], delete_file_contents=[], errors=[])
    provider = SimpleNamespace(
        work_item_uris=[], read_file_contents={}, get_extract_context=lambda: context
    )
    kwargs = {"context_provider": provider, "operations": operations, "viking_fs": fs, "ctx": ctx}
    _, coverage = await _work_item_extraction_metadata(
        **kwargs,
        result=SimpleNamespace(written_uris=[URI], edited_uris=[]),
    )
    assert coverage == []
    _, coverage = await _work_item_extraction_metadata(
        **kwargs,
        result=SimpleNamespace(written_uris=[URI, b.uris[0]], edited_uris=[]),
    )
    assert {entry["uri"] for entry in coverage} == {URI, b.uris[0]}
    assert all(entry["source_message_ids"] == ["raw"] for entry in coverage)
    b.source.extraction_id = "stale-conflicting-write"
    _, coverage = await _work_item_extraction_metadata(
        **kwargs,
        result=SimpleNamespace(written_uris=[URI, b.uris[0]], edited_uris=[]),
    )
    assert coverage == []


@pytest.mark.asyncio
async def test_delayed_old_user_reopen_cannot_revive_newer_terminal_state(registry, ctx):
    updater, fs = setup_updater(registry)
    old_request = Message(
        id="msg",
        role="user",
        created_at="2020-01-01T00:00:00+00:00",
        parts=[TextPart(text="Please reopen this work.")],
    )
    await updater._apply_upsert(op(status="done"), ctx)
    latest = fs.parsed()
    before = fs.values[URI]
    # The extractor already saw the newest done version: CAS alone would pass.
    with pytest.raises(ValueError, match="later than the terminal update"):
        await updater._apply_upsert(
            op(latest, reopen_reason="Please reopen this work."), ctx, ExtractContext([old_request])
        )
    assert fs.values[URI] == before
    new_request = Message(
        id="msg",
        role="user",
        created_at=(latest.extra_fields["updated_at"] + timedelta(seconds=1)).isoformat(),
        parts=[TextPart(text="Please reopen this work.")],
    )
    await updater._apply_upsert(
        op(latest, reopen_reason="Please reopen this work."), ctx, ExtractContext([new_request])
    )
    assert fs.parsed().extra_fields["status"] == "open"


@pytest.mark.asyncio
async def test_queued_reopen_after_completion_evidence_before_slow_done_write(registry, ctx):
    updater, fs = setup_updater(registry)
    completed = Message(
        id="msg",
        role="assistant",
        created_at="2020-01-01T00:01:00+00:00",
        parts=[TextPart(text="The requested result is complete and verified.")],
    )
    await updater._apply_upsert(op(status="done", ranges="0"), ctx, ExtractContext([completed]))
    terminal = fs.parsed()
    assert terminal.extra_fields["terminal_evidence_at"] == "2020-01-01T00:01:00+00:00"
    queued = Message(
        id="msg",
        role="user",
        created_at="2020-01-01T00:02:00+00:00",
        parts=[TextPart(text="Please reopen this work.")],
    )
    assert datetime.fromisoformat(queued.created_at) < terminal.extra_fields["updated_at"]
    await updater._apply_upsert(
        op(terminal, reopen_reason="Please reopen this work."), ctx, ExtractContext([queued])
    )
    assert fs.parsed().extra_fields["status"] == "open"
    assert "terminal_evidence_at" not in fs.parsed().extra_fields


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_name", ["python", "json"])
async def test_real_canonical_datetime_metadata_survives_prefetch_and_protocol(
    registry, ctx, protocol_name
):
    from openviking.session.memory.extraction_output_protocol import (
        ExtractionOutputContext,
        create_extraction_output_protocol,
    )

    updater, fs = setup_updater(registry)
    closed = Message(
        id="msg",
        role="assistant",
        created_at="2026-01-01T00:01:00+00:00",
        parts=[TextPart(text="The fix passed verification.")],
    )
    await updater._apply_upsert(op(status="done", ranges="0"), ctx, ExtractContext([closed]))
    canonical = fs.parsed()
    assert isinstance(canonical.extra_fields["updated_at"], datetime)
    # Exercise both time fields with actual runtime datetime values, including the
    # supported deserialized metadata representation rather than a string-only mock.
    canonical.extra_fields["terminal_evidence_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fs.values[URI] = MemoryFileUtils.write(canonical)
    provider = SessionExtractContextProvider(
        [Message(id="query", role="user", parts=[TextPart(text="What happened to the fix?")])],
        ctx=ctx,
        viking_fs=fs,
        memory_registry=registry,
        work_item_uris=[URI],
    )
    provider._isolation_handler = MemoryIsolationHandler(ctx, provider.get_extract_context())
    provider.search_files = AsyncMock(return_value=[])
    prefetched = await provider.prefetch()
    read_results = [
        payload["result"]
        for message in prefetched
        if message.get("content", "").startswith('{"tool_call_name"')
        and (payload := json.loads(message["content"]))["tool_call_name"] == "read"
    ]
    assert len(read_results) == 1
    assert isinstance(read_results[0]["updated_at"], str)
    assert isinstance(read_results[0]["terminal_evidence_at"], str)
    assert isinstance(provider.read_file_contents[URI].extra_fields["updated_at"], datetime)
    schema = registry.get("work_item")
    protocol_context = ExtractionOutputContext(
        operations_model=SchemaModelGenerator([schema]).create_structured_operations_model(),
        schemas=(schema,),
        page_id_map=provider.get_extract_context().page_id_map,
        read_file_contents=provider.read_file_contents,
        link_enabled=False,
    )
    protocol = create_extraction_output_protocol(protocol_name)
    rendered = protocol.render_prefetch_messages(prefetched, protocol_context)
    assert rendered
    json.dumps(rendered)
    if protocol_name == "python":
        assert any(
            "work_item_1 = sdk.existing" in message.get("content", "") for message in rendered
        )
