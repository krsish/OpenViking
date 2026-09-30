# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bounded work-item projections and their archive publication record.

The mutable Markdown memory is canonical. A checkpoint records the versions
used for its projection and retains unclassified messages verbatim. No model
call, semantic merge, or character truncation is performed here.
"""

import json
from typing import Any

from openviking.message import Message, TextPart
from openviking.session.memory.utils.memory_file_utils import (
    MemoryFileUtils,
    memory_version_from_fields,
)
from openviking.session.memory.work_item import WORK_ITEM_FIELDS, WORK_ITEM_TERMINAL_STATUSES
from openviking.utils.token_estimation import estimate_text_tokens

WORK_ITEM_MODE = "work_item"
PROJECTION_TOKEN_BUDGET = 3000
RESIDUAL_TOKEN_BUDGET = 1000
MAX_ACTIVE_WORK_ITEMS = 3
DETAILS_HINT = (
    "Details: recall the work_item URI or use archive_search for the original transcript."
)


def ready_checkpoint(done: dict[str, Any], archive_id: str) -> bool:
    return (
        done.get("mode") == WORK_ITEM_MODE
        and done.get("version") == 1
        and done.get("archive_id") == archive_id
        and done.get("compact_ready") is True
    )


def residual_text(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    return "## Continuation (summaries and unassigned original messages)\n" + "\n".join(
        json.dumps(message, ensure_ascii=False) for message in messages
    )


def render_item(item: dict[str, Any]) -> str:
    fields = item["fields"]
    lines = [f"## work_item {fields.get('title', '')}", f"URI: {item['uri']}"]
    for name in WORK_ITEM_FIELDS:
        if name != "title" and fields.get(name):
            lines.append(f"{name}: {fields[name]}")
    return "\n".join(lines)


def build_projection(
    items: list[dict[str, Any]],
    residual: list[dict[str, Any]],
    token_budget: int = PROJECTION_TOKEN_BUDGET,
) -> tuple[str, list[dict[str, Any]]]:
    """Pack whole item blocks in priority order; never truncate constraints."""
    remaining = residual_text(residual)
    residual_tokens = estimate_text_tokens(remaining)
    if residual_tokens > RESIDUAL_TOKEN_BUDGET:
        identities = [message.get("id") for message in residual]
        raise ValueError(
            "work_item checkpoint residual exceeds its token budget: "
            f"residual_tokens={residual_tokens}, budget={RESIDUAL_TOKEN_BUDGET}, "
            f"message_ids={identities}"
        )
    parts = ["# Working memory", remaining, DETAILS_HINT]
    if estimate_text_tokens("\n\n".join(filter(None, parts))) > token_budget:
        raise ValueError("work_item checkpoint cannot fit required residual")
    selected = []
    for item in items:
        if item["fields"].get("status") in WORK_ITEM_TERMINAL_STATUSES:
            continue
        block = render_item(item)
        candidate = [parts[0], *[render_item(value) for value in selected], block, *parts[1:]]
        if (
            len(selected) < MAX_ACTIVE_WORK_ITEMS
            and estimate_text_tokens("\n\n".join(filter(None, candidate))) <= token_budget
        ):
            selected.append(item)
    result = "\n\n".join(filter(None, [parts[0], *map(render_item, selected), *parts[1:]]))
    return result, selected


async def read_work_item(viking_fs: Any, ctx: Any, uri: str) -> dict[str, Any]:
    memory = MemoryFileUtils.read(await viking_fs.read_file(uri, ctx=ctx), uri=uri)
    fields = memory.extra_fields
    return {
        "uri": uri,
        "version": memory_version_from_fields(fields),
        "fields": {name: fields.get(name, "") for name in WORK_ITEM_FIELDS},
    }


def coverage_report(
    messages: list[Message],
    operations: list[dict[str, Any]],
    archive_uri: str,
    continuation_coverage: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give every source message a destination, retaining unexplained text.

    Model-provided source ranges are an extraction attribution, not a semantic
    proof. Missing attribution is never interpreted as permission to drop text.
    """
    destinations: dict[str, list[str]] = {}
    for operation in operations:
        for message_id in operation.get("source_message_ids", []):
            destinations.setdefault(message_id, []).append(operation["uri"])
    classifications: dict[str, dict[str, Any]] = {}
    for entry in continuation_coverage or []:
        for message_id in entry["source_message_ids"]:
            classifications[message_id] = entry
    residual, report = [], []
    emitted_summaries: set[int] = set()
    for message in messages:
        uris = list(dict.fromkeys(destinations.get(message.id, [])))
        row = {"message_id": message.id}
        if uris:
            row["work_item_uris"] = uris
        if message.id in classifications:
            entry = classifications[message.id]
            if entry.get("summary"):
                # Preserve source identity/time but mark derived text as assistant
                # context: a previous summary must never authorize reopening work.
                if id(entry) not in emitted_summaries:
                    summary = Message(
                        id=message.id,
                        role="assistant",
                        parts=[
                            TextPart(
                                "Previous continuation summary (background, not new user evidence):\n"
                                + entry["summary"]
                                + f"\nSource coverage: {archive_uri}/.done"
                            )
                        ],
                        created_at=message.created_at,
                    )
                    residual.append(summary.to_dict())
                    emitted_summaries.add(id(entry))
                row.update(residual_uri=f"{archive_uri}/.done", summary=entry["summary"])
            else:
                row["explicitly_dropped"] = entry["reason"]
        elif not uris:
            residual.append(message.to_dict())
            row["residual_uri"] = f"{archive_uri}/.done"
        report.append(row)
    return residual, report


def resolve_continuation_coverage(
    extract_context: Any,
    raw_items: list[Any],
    *,
    partial_tool_message_ids: Any = (),
) -> list[dict[str, Any]]:
    """Validate model attribution; partial source chunks never release raw messages."""
    from openviking.session.memory.work_item import covered_source_message_ids

    result = []
    seen: set[str] = set()
    for item in raw_items:
        fields = item.model_dump() if hasattr(item, "model_dump") else item
        summary = fields.get("summary", "")
        reason = fields.get("reason", "")
        if not isinstance(summary, str) or not isinstance(reason, str):
            raise ValueError("continuation summary and reason must be strings")
        summary, reason = summary.strip(), reason.strip()
        if bool(summary) == bool(reason):
            raise ValueError("continuation requires exactly one of summary or reason")
        ids = [
            identity
            for identity in covered_source_message_ids(extract_context, fields.get("ranges"))
            if identity not in partial_tool_message_ids
        ]
        if seen.intersection(ids):
            raise ValueError("continuation classifications must not overlap")
        seen.update(ids)
        if ids:
            result.append({"source_message_ids": ids, "summary": summary, "reason": reason})
    return result
