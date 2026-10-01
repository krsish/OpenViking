# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Reconcile only conflicted work items against fresh canonical snapshots."""

from copy import deepcopy
from typing import Any

from openviking.message import Message
from openviking.session.memory.dataclass import ResolvedOperation, ResolvedOperations
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.tools import add_tool_call_pair_to_messages
from openviking.session.memory.work_item import (
    is_work_item_uri,
    selected_source_messages,
    validate_work_item_update,
)


class _ConflictContextProvider(SessionExtractContextProvider):
    """Expose exactly the selected historical evidence and already-read targets."""

    def __init__(self, *, target_uris: list[str], **kwargs: Any):
        super().__init__(**kwargs)
        self._targets = frozenset(target_uris)
        self._read_results: dict[str, dict[str, Any]] = {}

    def instruction(self) -> str:
        return (
            "Reconcile a failed work-item update after another session changed its canonical state. "
            "Only the listed existing work items may be updated. Their complete, freshly read "
            "canonical states are supplied below; no discovery or additional tools are available. "
            "Use the original conversation evidence only where it still applies to the latest "
            "state. Do not revert newer verified progress or restore already resolved work merely "
            "because older evidence mentions it. Preserve still-valid current constraints. "
            "Return exactly one work_item update per supplied target page_id, keeping its existing "
            "work_item_id and URI. Never create, delete, rename, merge, link, activate, or emit "
            "continuation classifications. If no field needs changing, return the current state "
            "for that target anyway; omission is not allowed. Each update must provide valid "
            "ranges referring to the supplied original conversation evidence. These are historical "
            "messages with their original timestamps, not new user requests: do not reopen terminal "
            "work unless the actual original user request satisfies the normal reopening rules. "
            f"Write memory content in {self.get_output_language()}."
        )

    async def read_file(self, uri: str) -> dict[str, Any] | None:
        if uri not in self._targets:
            raise ValueError("work_item reconciliation cannot read outside its conflict targets")
        if uri not in self._read_results:
            result = await super().read_file(uri)
            if result is None or uri not in self.read_file_contents:
                raise ValueError(f"Cannot read latest conflicted work_item: {uri}")
            self._read_results[uri] = deepcopy(result)
        return deepcopy(self._read_results[uri])

    async def prefetch(self) -> list[dict[str, Any]]:
        messages = [self._build_conversation_message()]
        evidence = self._build_work_item_tool_evidence()
        if evidence:
            messages.append({"role": "user", "content": evidence})
        for index, uri in enumerate(self.work_item_uris):
            if uri not in self._read_results:
                raise ValueError("work_item conflict target was not read before reconciliation")
            add_tool_call_pair_to_messages(
                messages,
                call_id=index,
                tool_name="read",
                params={"uri": uri},
                result=deepcopy(self._read_results[uri]),
            )
        return messages

    def get_tools(self) -> list[str]:
        return []

    async def execute_tool(self, tool_call: Any) -> Any:
        raise ValueError("work_item reconciliation does not allow additional tool calls")

    async def search_files(self, *args: Any, **kwargs: Any) -> list[str]:
        raise ValueError("work_item reconciliation does not allow memory discovery")


async def reconcile_work_item_conflicts(
    *,
    compressor: Any,
    frozen: ResolvedOperations,
    conflict_uris: set[str],
    messages: list[Message],
    ctx: Any,
    registry: Any,
    viking_fs: Any,
    vlm_config: Any,
    session_id: str,
    frozen_source_coverage: list[dict[str, Any]],
) -> ResolvedOperations:
    """Return replacement operations without applying writes or widening coverage.

    The caller persists this replacement as a new replay revision before writing.
    A subsequent concurrent update may still conflict at the existing item CAS;
    it must trigger another bounded retry, never an unconditional overwrite.
    """
    originals: dict[str, ResolvedOperation] = {}
    for operation in frozen.upsert_operations:
        targets = conflict_uris.intersection(operation.uris)
        if not targets:
            continue
        if operation.memory_type != "work_item" or len(operation.uris) != 1:
            raise ValueError("work_item reconciliation requires one existing target per operation")
        uri = operation.uris[0]
        if uri in originals or not is_work_item_uri(uri):
            raise ValueError("work_item reconciliation has an invalid or duplicate target")
        originals[uri] = operation
    if not conflict_uris or set(originals) != conflict_uris:
        raise ValueError("work_item reconciliation targets must belong to the frozen plan")

    sources: dict[str, Message] = {}
    for message in messages:
        if message.id in sources:
            raise ValueError("work_item reconciliation source messages have duplicate identities")
        sources[message.id] = message
    evidence_by_uri: dict[str, list[str]] = {}
    for uri, operation in originals.items():
        evidence = operation.source_evidence_message_ids or operation.source_message_ids
        if not evidence:
            evidence = [
                identity
                for entry in frozen_source_coverage
                if entry.get("uri") == uri
                for identity in entry.get("source_message_ids", [])
            ]
        evidence = list(dict.fromkeys(evidence))
        if not evidence or any(identity not in sources for identity in evidence):
            raise ValueError("work_item reconciliation is missing its original source evidence")
        evidence_by_uri[uri] = evidence
    selected_ids = {identity for evidence in evidence_by_uri.values() for identity in evidence}
    selected_messages = [message for message in messages if message.id in selected_ids]
    provider = _ConflictContextProvider(
        target_uris=list(originals),
        messages=selected_messages,
        latest_archive_overview="",
        ctx=ctx,
        viking_fs=viking_fs,
        memory_registry=registry,
        vlm_config=vlm_config,
        work_item_uris=list(originals),
        work_item_namespace=session_id,
    )
    await provider.prepare_extraction_messages()
    extract_context = provider.get_extract_context()
    isolation = MemoryIsolationHandler(
        ctx,
        extract_context,
        allowed_memory_types={"work_item"},
        allow_self=True,
        allowed_peer_ids=set(),
        peer_memory_enabled=False,
    )
    isolation.prepare_messages()
    provider._isolation_handler = isolation
    for uri in originals:
        await provider.read_file(uri)
    snapshots = {uri: provider.read_file_contents[uri].model_copy(deep=True) for uri in originals}
    for uri, original in originals.items():
        identity = original.memory_fields.get("work_item_id")
        snapshot = snapshots[uri]
        if (
            not isinstance(identity, str)
            or not identity
            or snapshot.uri != uri
            or snapshot.memory_type != "work_item"
            or snapshot.extra_fields.get("work_item_id") != identity
        ):
            raise ValueError("work_item reconciliation cannot change canonical identity")

    orchestrator = compressor._get_or_create_react(
        ctx=ctx,
        messages=selected_messages,
        latest_archive_overview="",
        isolation_handler=isolation,
        transaction_handle=None,
        context_provider=provider,
        vlm_config=vlm_config,
    )
    resolved, _tools_used = await orchestrator.run()
    if not isinstance(resolved, ResolvedOperations) or (
        resolved.errors
        or resolved.delete_file_contents
        or resolved.delete_replacements
        or resolved.resolved_links
        or resolved.work_item_activations
        or resolved.continuation_coverage
    ):
        raise ValueError("work_item reconciliation must return only successful target updates")

    replacements: dict[str, ResolvedOperation] = {}
    for operation in resolved.upsert_operations:
        if operation.memory_type != "work_item" or len(operation.uris) != 1:
            raise ValueError("work_item reconciliation returned an unrelated operation")
        uri = operation.uris[0]
        if uri not in originals or uri in replacements or operation.resolution_skip is not None:
            raise ValueError("work_item reconciliation returned an unexpected or duplicate target")
        original, snapshot = originals[uri], snapshots[uri]
        if operation.memory_fields.get("work_item_id") != original.memory_fields["work_item_id"]:
            raise ValueError("work_item reconciliation changed a frozen identity")
        old = operation.old_memory_file_content
        if old is None or old.model_dump(mode="json") != snapshot.model_dump(mode="json"):
            raise ValueError("work_item reconciliation did not use the freshly read snapshot")
        selected_source_messages(extract_context, operation.memory_fields.get("ranges"))
        replacement = operation.model_copy(deep=True)
        replacement.old_memory_file_content = snapshot.model_copy(deep=True)
        replacement.source = deepcopy(original.source)
        replacement.source_message_ids = deepcopy(original.source_message_ids)
        replacement.source_evidence_message_ids = list(evidence_by_uri[uri])
        replacement.search_tags = deepcopy(original.search_tags)
        validate_work_item_update(replacement, snapshot, ctx, extract_context)
        replacements[uri] = replacement
    if set(replacements) != conflict_uris:
        raise ValueError("work_item reconciliation omitted a conflicted target")
    return ResolvedOperations(
        upsert_operations=[replacements[uri] for uri in originals],
        delete_file_contents=[],
        errors=[],
    )
