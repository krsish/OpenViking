# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session.memory.dataclass import (
    MemoryFile,
    MemoryOperationSource,
    ResolvedOperation,
    ResolvedOperations,
)
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.work_item_conflicts import reconcile_work_item_conflicts
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def conflict_case(monkeypatch):
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    uri = "viking://user/alice/memories/work_item/wi-target.md"
    fields = {
        "work_item_id": "wi-target",
        "title": "Target task",
        "goal": "Ship after approval",
        "status": "waiting",
        "current_state": "Newer verified state",
        "constraints": "Never deploy without approval",
        "version": 2,
    }
    canonical = MemoryFile(uri=uri, memory_type="work_item", extra_fields=fields)
    contents = {uri: MemoryFileUtils.write(canonical)}

    async def read(target, **kwargs):
        return contents[target]

    fs = SimpleNamespace(read_file=AsyncMock(side_effect=read))
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/work_item.yaml")
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(eager_prefetch=False, prefetch_search_topn=5, link_enabled=False)
    )
    monkeypatch.setattr(
        "openviking.session.memory.session_extract_context_provider.get_openviking_config",
        lambda: config,
    )
    messages = [
        Message(
            id="unrelated",
            role="user",
            parts=[TextPart("UNRELATED_TASK_MUST_NOT_ENTER_RECONCILIATION")],
            created_at="2026-01-01T00:00:00Z",
        ),
        Message(
            id="evidence",
            role="user",
            parts=[TextPart("Wait for approval before deploying.")],
            created_at="2026-01-01T01:00:00Z",
        ),
        Message(
            id="tool",
            role="assistant",
            parts=[ToolPart(tool_name="read", tool_output="APPROVAL_STILL_PENDING")],
            created_at="2026-01-01T02:00:00Z",
        ),
    ]
    old = canonical.model_copy(deep=True)
    old.extra_fields.update(version=1, current_state="Old plan snapshot")
    original = ResolvedOperation(
        memory_type="work_item",
        uris=[uri],
        old_memory_file_content=old,
        memory_fields={**fields, "ranges": "9999"},
        source_message_ids=["evidence"],
        source_evidence_message_ids=["evidence", "tool"],
        source=MemoryOperationSource(extraction_id="wi-replay-original", session_id="session"),
    )
    frozen = ResolvedOperations(upsert_operations=[original], delete_file_contents=[], errors=[])
    case = SimpleNamespace(
        ctx=ctx,
        uri=uri,
        fs=fs,
        contents=contents,
        registry=registry,
        original=original,
        frozen=frozen,
        messages=messages,
        mutate=None,
        observed={},
    )

    def orchestrator(**kwargs):
        provider = kwargs["context_provider"]
        case.observed["provider"] = provider
        case.observed["isolation"] = kwargs["isolation_handler"]
        case.observed["messages"] = kwargs["messages"]

        async def run():
            case.observed["prefetch"] = await provider.prefetch()
            snapshot = provider.read_file_contents[uri].model_copy(deep=True)
            operation = ResolvedOperation(
                memory_type="work_item",
                uris=[uri],
                old_memory_file_content=snapshot,
                memory_fields={
                    **snapshot.extra_fields,
                    "ranges": "0",
                    "current_state": "Reconciled with newer verified state",
                },
                source_message_ids=["unrelated"],
                source_evidence_message_ids=["unrelated"],
                source=MemoryOperationSource(extraction_id="untrusted-new-extraction"),
            )
            result = ResolvedOperations(
                upsert_operations=[operation], delete_file_contents=[], errors=[]
            )
            if case.mutate:
                case.mutate(result)
            return result, []

        return SimpleNamespace(run=run)

    case.compressor = SimpleNamespace(_get_or_create_react=Mock(side_effect=orchestrator))
    return case


async def reconcile(case):
    return await reconcile_work_item_conflicts(
        compressor=case.compressor,
        frozen=case.frozen,
        conflict_uris={case.uri},
        messages=case.messages,
        ctx=case.ctx,
        registry=case.registry,
        viking_fs=case.fs,
        vlm_config=SimpleNamespace(),
        session_id="session",
        frozen_source_coverage=[{"uri": case.uri, "source_message_ids": ["evidence"]}],
    )


@pytest.mark.asyncio
async def test_conflict_reconciliation_reads_only_latest_target_and_frozen_source_evidence(
    conflict_case,
):
    case = conflict_case
    result = await reconcile(case)

    provider = case.observed["provider"]
    assert [source.id for source in case.observed["messages"]] == ["evidence", "tool"]
    assert provider.get_tools() == []
    assert [schema.memory_type for schema in provider.get_memory_schemas(case.ctx)] == ["work_item"]
    assert case.observed["isolation"].peer_memory_enabled is False
    prefetch = json.dumps(case.observed["prefetch"])
    assert "UNRELATED_TASK" not in prefetch
    assert "APPROVAL_STILL_PENDING" in prefetch
    assert "Newer verified state" in prefetch
    assert "Old plan snapshot" not in prefetch
    case.fs.read_file.assert_awaited_once_with(case.uri, ctx=case.ctx)
    replacement = result.upsert_operations[0]
    assert replacement.uris == [case.uri]
    assert replacement.memory_fields["work_item_id"] == "wi-target"
    assert replacement.old_memory_file_content.extra_fields["version"] == 2
    assert replacement.source == case.original.source
    assert replacement.source_message_ids == ["evidence"]
    assert replacement.source_evidence_message_ids == ["evidence", "tool"]
    assert replacement.memory_fields["ranges"] == "0"
    assert case.original.memory_fields["ranges"] == "9999"
    assert case.original.old_memory_file_content.extra_fields["version"] == 1
    for action in (
        provider.search_files("other task"),
        provider.read_file("viking://user/alice/memories/work_item/wi-other.md"),
        provider.execute_tool(SimpleNamespace(name="search")),
    ):
        with pytest.raises(ValueError, match="reconciliation"):
            await action


@pytest.mark.asyncio
@pytest.mark.parametrize("use_coverage", [False, True])
async def test_old_replay_source_ids_are_resolved_without_reinterpreting_ranges(
    conflict_case, use_coverage
):
    case = conflict_case
    case.original.source_evidence_message_ids = None
    if use_coverage:
        case.original.source_message_ids = None

    result = await reconcile(case)

    assert [source.id for source in case.observed["messages"]] == ["evidence"]
    assert result.upsert_operations[0].source_evidence_message_ids == ["evidence"]
    assert result.upsert_operations[0].source_message_ids == case.original.source_message_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "omitted",
        "duplicate",
        "foreign_uri",
        "changed_identity",
        "stale_snapshot",
        "error",
        "activation",
        "continuation",
        "delete",
    ],
)
async def test_conflict_reconciliation_rejects_expanded_or_unverified_operations(
    conflict_case, mutation
):
    case = conflict_case

    def mutate(result):
        operation = result.upsert_operations[0]
        if mutation == "omitted":
            result.upsert_operations = []
        elif mutation == "duplicate":
            result.upsert_operations.append(operation.model_copy(deep=True))
        elif mutation == "foreign_uri":
            operation.uris = ["viking://user/alice/memories/work_item/wi-other.md"]
        elif mutation == "changed_identity":
            operation.memory_fields["work_item_id"] = "wi-other"
        elif mutation == "stale_snapshot":
            operation.old_memory_file_content.extra_fields["version"] = 1
        elif mutation == "error":
            result.errors = ["incomplete"]
        elif mutation == "activation":
            result.work_item_activations = [{"uri": case.uri}]
        elif mutation == "continuation":
            result.continuation_coverage = [{"source_message_ids": ["evidence"], "summary": "new"}]
        elif mutation == "delete":
            result.delete_file_contents = [operation.old_memory_file_content]

    case.mutate = mutate
    with pytest.raises(ValueError, match="reconciliation"):
        await reconcile(case)


@pytest.mark.asyncio
async def test_missing_original_evidence_blocks_reconciliation_before_model_call(conflict_case):
    case = conflict_case
    case.messages = [source for source in case.messages if source.id != "tool"]
    with pytest.raises(ValueError, match="original source evidence"):
        await reconcile(case)
    case.compressor._get_or_create_react.assert_not_called()
    case.fs.read_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_user_evidence_cannot_reopen_newer_terminal_state(conflict_case):
    case = conflict_case
    canonical = MemoryFileUtils.read(case.contents[case.uri], uri=case.uri)
    canonical.extra_fields.update(status="done", terminal_evidence_at="2026-01-02T00:00:00Z")
    case.contents[case.uri] = MemoryFileUtils.write(canonical)

    def reopen(result):
        result.upsert_operations[0].memory_fields.update(
            status="open", reopen_reason="Wait for approval before deploying."
        )

    case.mutate = reopen
    with pytest.raises(ValueError, match="Reopening a terminal"):
        await reconcile(case)
