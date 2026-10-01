# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bounded work-item projections and their archive publication record.

The mutable Markdown memory is canonical. Original messages stay in archives;
only selected continuation state is projected. No model call or character
truncation is performed here.
"""

import hashlib
import json
from typing import Any

from openviking.message import Message, TextPart
from openviking.session.memory.utils.memory_file_utils import (
    MemoryFileUtils,
    memory_version_from_fields,
)
from openviking.session.memory.work_item import WORK_ITEM_FIELDS, WORK_ITEM_TERMINAL_STATUSES
from openviking.session.work_item_budget import get_work_item_budgets
from openviking.utils.token_estimation import estimate_text_tokens

WORK_ITEM_MODE = "work_item"
PROJECTION_TOKEN_BUDGET = 42000
RESIDUAL_TOKEN_BUDGET = 10000
MAX_ACTIVE_WORK_ITEMS = 3
DETAILS_HINT = (
    "Details: recall the work_item URI; list the session history and read an archive's "
    "messages.jsonl (and referenced tool results) for original evidence."
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
    parts = []
    for message in messages:
        if message.get("message_kind") != "checkpoint":
            parts.append(json.dumps(message, ensure_ascii=False))
            continue
        content = Message.from_dict(message).content
        source = message.get("source_checkpoint_uri")
        if source and source not in content:
            content += f"\nSource coverage: {source}"
        parts.append(content)
    return "## Continuation\n" + "\n\n".join(parts)


def continuation_message(
    summary: str, archive_uri: str, source_ids: list[str], created_at: str | None
) -> dict[str, Any]:
    """Derived state has its own identity, separate from archived user evidence."""
    identity = hashlib.sha256(
        json.dumps([archive_uri, source_ids, summary], ensure_ascii=False).encode()
    ).hexdigest()[:24]
    result = Message(
        id=f"wi-continuation-{identity}",
        role="assistant",
        message_kind="checkpoint",
        source_message_ids=list(dict.fromkeys(source_ids)),
        parts=[
            TextPart(
                "Previous continuation summary (background, not new user evidence):\n"
                + summary
                + f"\nSource coverage: {archive_uri}/.done"
            )
        ],
        created_at=created_at,
    ).to_dict()
    result["source_checkpoint_uri"] = f"{archive_uri}/.done"
    return result


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
    token_budget: int | None = None,
    *,
    residual_token_budget: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Pack whole item blocks in priority order; never truncate constraints."""
    budgets = get_work_item_budgets()
    if token_budget is None:
        token_budget = budgets.projection_token_budget
    if residual_token_budget is None:
        residual_token_budget = budgets.continuation_token_budget
    remaining = residual_text(residual)
    residual_tokens = estimate_text_tokens(remaining)
    if residual_tokens > residual_token_budget:
        identities = [message.get("id") for message in residual]
        raise ValueError(
            "work_item checkpoint residual exceeds its token budget: "
            f"residual_tokens={residual_tokens}, budget={residual_token_budget}, "
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
    *,
    previous_residual: list[dict[str, Any]] | None = None,
    source_archives: dict[str, str] | None = None,
    previous_checkpoint_uri: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Record hot-state attribution after successful extraction.

    Unselected new originals remain archive-only. Previously selected state is
    kept unless explicitly summarized, resolved, or moved into a work item.
    The session owner, not this pure function, verifies extraction completion.
    """
    previous = {value["id"]: value for value in previous_residual or []}
    source_archives = source_archives or {}
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
        row = {
            "message_id": message.id,
            "archive_uri": f"{source_archives.get(message.id, archive_uri)}/messages.jsonl",
        }
        if message.message_kind == "checkpoint":
            row.pop("archive_uri")
            row["source_message_ids"] = message.source_message_ids or []
        if message.id in previous:
            source_checkpoint = (
                previous[message.id].get("source_checkpoint_uri") or previous_checkpoint_uri
            )
            if source_checkpoint:
                row["source_checkpoint_uri"] = source_checkpoint
        if uris:
            row["work_item_uris"] = uris
        if message.id in classifications:
            entry = classifications[message.id]
            if entry.get("summary"):
                if id(entry) not in emitted_summaries:
                    residual.append(
                        continuation_message(
                            entry["summary"],
                            archive_uri,
                            entry["source_message_ids"],
                            message.created_at,
                        )
                    )
                    emitted_summaries.add(id(entry))
                row.update(
                    disposition="continuation",
                    residual_uri=f"{archive_uri}/.done",
                    summary=entry["summary"],
                )
            else:
                row["disposition"] = "archive_only" if not uris else "work_item"
                row["explicitly_dropped"] = entry["reason"]
        elif not uris:
            if message.id in previous:
                residual.append(previous[message.id])
                row.update(disposition="continuation", residual_uri=f"{archive_uri}/.done")
            else:
                row["disposition"] = "archive_only"
        else:
            row["disposition"] = "work_item"
        report.append(row)
    # Callers may pass only the new transcript; omission cannot resolve old state.
    seen = {message.id for message in messages}
    for identity, value in previous.items():
        if identity in seen:
            continue
        residual.append(value)
        report.append(
            {
                "message_id": identity,
                "disposition": "continuation",
                "residual_uri": f"{archive_uri}/.done",
                "source_checkpoint_uri": value.get("source_checkpoint_uri")
                or previous_checkpoint_uri,
            }
        )
    return residual, report


def resolve_continuation_coverage(
    extract_context: Any,
    raw_items: list[Any],
) -> list[dict[str, Any]]:
    """Validate attribution; all source chunks must be classified to release a raw message."""
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
        ids = covered_source_message_ids(extract_context, fields.get("ranges"))
        if seen.intersection(ids):
            raise ValueError("continuation classifications must not overlap")
        seen.update(ids)
        if ids:
            result.append({"source_message_ids": ids, "summary": summary, "reason": reason})
    return result
