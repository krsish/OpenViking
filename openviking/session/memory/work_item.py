# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Small, shared invariants for bounded, user-owned work-item memories."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from openviking.core.namespace import canonical_user_root, uri_parts
from openviking.session.memory.utils.memory_file_utils import memory_version_from_fields
from openviking.session.work_item_budget import get_work_item_budgets
from openviking.utils.time_utils import parse_iso_datetime
from openviking.utils.token_estimation import estimate_text_tokens
from openviking_cli.exceptions import ConflictError

WORK_ITEM_FIELDS = (
    "title",
    "scope",
    "goal",
    "status",
    "current_state",
    "next_action",
    "constraints",
    "waiting_for",
    "decisions",
    "refs",
)
WORK_ITEM_STATUSES = frozenset({"open", "in_progress", "waiting", "blocked", "done", "cancelled"})
WORK_ITEM_TERMINAL_STATUSES = frozenset({"done", "cancelled"})
_WORK_ITEM_ID_RE = re.compile(r"wi-[A-Za-z0-9_-]+$")


def is_work_item_uri(uri: str) -> bool:
    if not isinstance(uri, str) or not uri.startswith("viking://"):
        return False
    parts = uri_parts(uri)
    return (
        len(parts) >= 4
        and parts[0] in {"user", "agent"}
        and parts[2:4] == ["memories", "work_item"]
    )


def selected_source_messages(extract_context: Any, ranges: str) -> list[Any]:
    """Validate source ranges without the generic parser's out-of-bounds clamping."""
    messages = list(getattr(extract_context, "messages", []) or [])
    if not messages and not ranges:
        return []  # Explicit offline memory consolidation has no new transcript.
    if not isinstance(ranges, str) or not ranges.strip():
        raise ValueError("work_item requires non-empty source ranges from the current conversation")
    indexes: set[int] = set()
    for span in ranges.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:-(\d+))?\s*", span)
        if not match:
            raise ValueError("Invalid work_item source ranges")
        start, end = int(match[1]), int(match[2] or match[1])
        if start > end or end >= len(messages):
            raise ValueError("work_item source ranges are outside the current conversation")
        indexes.update(range(start, end + 1))
    return [messages[index] for index in sorted(indexes)]


def covered_source_message_ids(extract_context: Any, ranges: str) -> list[str]:
    """Report a raw message only when all its extraction chunks were selected.

    This is source attribution, not a proof that every fact in a message survived.
    """
    selected = selected_source_messages(extract_context, ranges)
    selected_ids = {id(message) for message in selected}
    chunk_meta = getattr(extract_context, "chunk_meta", {}) or {}
    all_chunks: dict[str, list[Any]] = {}
    for message in getattr(extract_context, "messages", []) or []:
        meta = chunk_meta.get(id(message))
        source_id = meta.source_message_id if meta is not None else message.id
        all_chunks.setdefault(source_id, []).append(message)
    return [
        source_id
        for source_id, chunks in all_chunks.items()
        if all(id(message) in selected_ids for message in chunks)
    ]


def new_work_item_id(extract_context: Any, ranges: str, ordinal: int, namespace: str = "") -> str:
    """Allocate a server identity for a source slot in an extraction plan.

    Retries reuse the persisted plan: model output order is not task identity.
    No title, semantic hash, or model-supplied ID participates. A collision with
    a different extraction must be read/reconciled, never overwritten.
    """
    selected = selected_source_messages(extract_context, ranges)
    if not selected:
        return "wi-" + uuid4().hex
    first = selected[0]
    meta = (getattr(extract_context, "chunk_meta", {}) or {}).get(id(first))
    source_id = meta.source_message_id if meta is not None else first.id
    encoded = base64.urlsafe_b64encode(f"{namespace}\0{source_id}".encode()).decode().rstrip("=")
    identity = f"wi-{encoded}-{ordinal}"
    if len(identity) > 220:
        raise ValueError("work_item source identity is too long for a portable filename")
    return identity


def validate_work_item_update(
    operation: Any, old_file: Any, ctx: Any, extract_context: Any
) -> dict[str, Any]:
    """Validate the latest state while the caller holds the shared item lease."""
    uri = operation.uris[0]
    prefix = f"{canonical_user_root(ctx)}/memories/work_item/"
    identity = str(operation.memory_fields.get("work_item_id") or "")
    if not _WORK_ITEM_ID_RE.fullmatch(identity) or uri != f"{prefix}{identity}.md":
        raise ValueError("work_item identity and canonical user URI must match")
    snapshot = operation.old_memory_file_content
    if snapshot is not None and snapshot.uri != uri:
        raise ValueError("work_item identities cannot be renamed or merged")
    if old_file is not None:
        if snapshot is None:
            raise ConflictError(
                "work_item already exists; read its latest version before updating", resource=uri
            )
        if memory_version_from_fields(snapshot.extra_fields) != memory_version_from_fields(
            old_file.extra_fields
        ):
            raise ConflictError(
                "work_item version changed; re-extract from the latest canonical state",
                resource=uri,
            )
        if old_file.extra_fields.get("work_item_id") != identity:
            raise ConflictError("work_item identity is immutable", resource=uri)
    elif snapshot is not None:
        raise ConflictError("work_item was removed after extraction", resource=uri)

    metadata = dict(old_file.extra_fields) if old_file is not None else {}
    for name in WORK_ITEM_FIELDS:
        value = operation.memory_fields.get(name)
        if value is None:
            value = metadata.get(name, "")
        if not isinstance(value, str):
            raise ValueError(f"work_item.{name} must be a string")
        metadata[name] = value.strip()
    if not metadata["title"] or not metadata["goal"]:
        raise ValueError("work_item requires a title and a goal")
    if metadata["status"] not in WORK_ITEM_STATUSES:
        raise ValueError("Invalid work_item status")
    token_budget = get_work_item_budgets().work_item_token_budget
    if estimate_text_tokens("\n".join(metadata[name] for name in WORK_ITEM_FIELDS)) > token_budget:
        raise ValueError(f"work_item state exceeds {token_budget} estimated tokens")

    prior_status = old_file.extra_fields.get("status") if old_file is not None else None
    if prior_status in WORK_ITEM_TERMINAL_STATUSES and metadata["status"] != prior_status:
        reason = operation.memory_fields.get("reopen_reason")
        source_ids = set(operation.source_message_ids or [])
        chunk_meta = getattr(extract_context, "chunk_meta", {}) or {}
        evidence = []
        terminal_updated_at = _aware_timestamp(
            old_file.extra_fields.get("terminal_evidence_at")
            or old_file.extra_fields.get("updated_at")
        )
        for message in getattr(extract_context, "messages", []) or []:
            chunk = chunk_meta.get(id(message))
            source_id = chunk.source_message_id if chunk is not None else message.id
            requested_at = _aware_timestamp(getattr(message, "created_at", None))
            if (
                source_id in source_ids
                and message.role == "user"
                and terminal_updated_at is not None
                and requested_at is not None
                and requested_at > terminal_updated_at
            ):
                evidence.append("\n".join(str(getattr(part, "text", "")) for part in message.parts))
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not any(reason.strip() in text for text in evidence)
        ):
            raise ValueError(
                "Reopening a terminal work_item requires an exact current user request in source ranges "
                "with created_at later than the terminal update"
            )
    now = datetime.now(timezone.utc)
    if metadata["status"] in WORK_ITEM_TERMINAL_STATUSES:
        if prior_status not in WORK_ITEM_TERMINAL_STATUSES or metadata["status"] != prior_status:
            # Compare requests with when completion was evidenced, not when an async
            # extraction eventually persisted it. Otherwise a legitimate queued reopen
            # could precede the slow write and be incorrectly rejected.
            evidence_ids = getattr(operation, "source_evidence_message_ids", None)
            if evidence_ids is None:
                try:
                    source_messages = selected_source_messages(
                        extract_context, operation.memory_fields.get("ranges")
                    )
                except ValueError:
                    evidence_ids = operation.source_message_ids or []
            if evidence_ids is not None:
                # Resolve source IDs fixed by the extractor, never reinterpret
                # old chunk indices after image preparation or retry batching.
                source_ids = set(evidence_ids)
                chunk_meta = getattr(extract_context, "chunk_meta", {}) or {}
                source_messages = [
                    message
                    for message in getattr(extract_context, "messages", []) or []
                    if (
                        chunk_meta[id(message)].source_message_id
                        if id(message) in chunk_meta
                        else message.id
                    )
                    in source_ids
                ]
            evidence_times = [
                timestamp
                for message in source_messages
                if (timestamp := _aware_timestamp(getattr(message, "created_at", None)))
            ]
            metadata["terminal_evidence_at"] = max(evidence_times) if evidence_times else now
    else:
        # Explicit None prevents the generic updater from restoring old system metadata.
        metadata["terminal_evidence_at"] = None
    metadata.update(work_item_id=identity, memory_type="work_item", updated_at=now)
    metadata["source_message_ids"] = list(operation.source_message_ids or [])
    metadata.pop("ranges", None)
    metadata.pop("reopen_reason", None)
    return metadata


def _aware_timestamp(value: Any) -> datetime | None:
    """Require comparable source time; missing/naive clocks cannot authorize reopen."""
    try:
        parsed = value if isinstance(value, datetime) else parse_iso_datetime(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def resolve_work_item_activations(
    activations: list[Any], *, extract_context: Any, read_files: dict[str, Any], ctx: Any
) -> list[dict[str, Any]]:
    """Validate model-selected resumption receipts; source attribution is not coverage."""
    if len(activations) > 3:
        raise ValueError("At most 3 work_item activations may be selected")
    prefix = f"{canonical_user_root(ctx)}/memories/work_item/"
    result = {}
    for activation in activations:
        uri = extract_context.page_id_map.resolve(activation.page_id)
        canonical = read_files.get(uri)
        if (
            canonical is None
            or canonical.memory_type != "work_item"
            or not uri.startswith(prefix)
            or uri != f"{prefix}{canonical.extra_fields.get('work_item_id', '')}.md"
        ):
            raise ValueError("work_item activation requires an already-read canonical user item")
        selected = selected_source_messages(extract_context, activation.ranges)
        source_ids = []
        for message in selected:
            if message.role != "user":
                continue
            meta = (getattr(extract_context, "chunk_meta", {}) or {}).get(id(message))
            source_id = meta.source_message_id if meta is not None else message.id
            if source_id not in source_ids:
                source_ids.append(source_id)
        if not source_ids:
            raise ValueError("work_item activation requires a current user source message")
        if canonical.extra_fields.get("status") in WORK_ITEM_TERMINAL_STATUSES:
            continue
        receipt = result.setdefault(
            uri,
            {
                "uri": uri,
                "version": memory_version_from_fields(canonical.extra_fields),
                "source_message_ids": [],
            },
        )
        receipt["source_message_ids"] = list(
            dict.fromkeys(receipt["source_message_ids"] + source_ids)
        )
    return list(result.values())
