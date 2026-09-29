# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bounded work-item projections and their archive publication record.

The mutable Markdown memory is canonical. A checkpoint records the versions
used for its projection and retains unclassified messages verbatim. No model
call, semantic merge, or character truncation is performed here.
"""

import json
from typing import Any

from openviking.message import Message
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
    return "## Unassigned continuation (original messages)\n" + "\n".join(
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
    if estimate_text_tokens(remaining) > RESIDUAL_TOKEN_BUDGET:
        raise ValueError("work_item checkpoint residual exceeds its token budget")
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
    messages: list[Message], operations: list[dict[str, Any]], archive_uri: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give every source message a destination, retaining unexplained text.

    Model-provided source ranges are an extraction attribution, not a semantic
    proof. Missing attribution is never interpreted as permission to drop text.
    """
    destinations: dict[str, list[str]] = {}
    for operation in operations:
        for message_id in operation.get("source_message_ids", []):
            destinations.setdefault(message_id, []).append(operation["uri"])
    residual, report = [], []
    for message in messages:
        uris = list(dict.fromkeys(destinations.get(message.id, [])))
        if uris:
            report.append({"message_id": message.id, "work_item_uris": uris})
        else:
            residual.append(message.to_dict())
            report.append({"message_id": message.id, "residual_uri": f"{archive_uri}/.done"})
    return residual, report
