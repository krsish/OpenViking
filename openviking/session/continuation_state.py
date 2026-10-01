# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Apply explicit transitions to session-local continuation items.

The live list contains only current state. Closed items and transitions remain
in the checkpoint's coverage ledger; omission never closes an existing item.
"""

import hashlib
import json
from copy import deepcopy
from typing import Any

from openviking.message import Message


class ContinuationResolutionError(ValueError):
    """A continuation action needs correction before any canonical writes."""


def continuation_fingerprint(text: str) -> str:
    """Bind a transition to its historical state; chunking preserves the body."""
    return hashlib.sha256(text.strip().encode()).hexdigest()


def new_continuation_id(namespace: str, sources: list[str], summary: str) -> str:
    """Stable across retries and recovery into a later archive in this session."""
    encoded = json.dumps([namespace, sources, summary], ensure_ascii=False)
    return "wi-continuation-" + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def confirm_continuation_promotions(
    actions: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach server receipts from this applied extraction, never historical coverage."""
    result = deepcopy(actions)
    for entry in result:
        if entry.get("action") != "promote":
            continue
        required = {
            entry["continuation_id"],
            *entry.get("source_message_ids", []),
            *entry.get("work_item_source_message_ids", []),
        }
        receipt = next(
            (
                row
                for row in coverage
                if row.get("uri") == entry.get("work_item_uri")
                and required <= set(row.get("source_message_ids", []))
            ),
            None,
        )
        entry["promotion_receipt"] = deepcopy(receipt)
    return result


def apply_continuation_actions(
    messages: list[Message],
    operations: list[dict[str, Any]],
    archive_uri: str,
    actions: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    residual: list[dict[str, Any]],
    report: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from openviking.session.work_items import continuation_content, continuation_message

    old = {value["id"]: value for value in previous}
    current = {value["id"]: value for value in residual}
    rows = {value["message_id"]: value for value in report}
    order = {message.id: index for index, message in enumerate(messages)}
    by_id = {message.id: message for message in messages}
    recency = {value["id"]: (0, index) for index, value in enumerate(residual)}
    handled: set[str] = set()
    states = dict.fromkeys(current, "active")
    for entry in actions:
        action = entry["action"]
        identity = entry.get("continuation_id", "")
        sources = entry.get("source_message_ids", [])
        previously_handled = identity in handled
        newest = max((order.get(source, -1) for source in sources), default=-1)
        created_at = messages[newest].created_at if newest >= 0 else None
        if action != "create":
            if identity not in old:
                raise ValueError(f"Unknown continuation item: {identity}")
            # Even a legacy work-item attribution cannot close an item while
            # its explicit promotion is still awaiting a durable write receipt.
            # Multiple frozen extraction batches may update the same item in
            # order. A later keep must not restore its pre-batch contents.
            if identity not in handled:
                current.setdefault(identity, old[identity])
            handled.add(identity)
        if action in {"create", "update"}:
            # Within one publication, frozen batches may successively change
            # an item. Keep direct evidence from those batches in the archive;
            # provenance normalization removes this list from the hot state.
            if action == "update" and identity in current and previously_handled:
                sources = list(
                    dict.fromkeys(current[identity].get("source_message_ids", []) + sources)
                )
            value = continuation_message(
                entry["summary"],
                archive_uri,
                sources,
                created_at,
                continuation_id=identity
                or new_continuation_id(
                    archive_uri.split("/history/", 1)[0], sources, entry["summary"]
                ),
            )
            if action == "update":
                value["source_checkpoint_uri"] = old[identity].get("source_checkpoint_uri", "")
            identity = value["id"]
            current[identity] = value
            recency[identity] = (1, newest)
            state = "active"
            if action == "create":
                # Later batches in this same archive may already update it.
                old.setdefault(identity, value)
                handled.add(identity)
        elif action == "keep":
            state = states.get(identity, "active")
        elif action == "resolve":
            snapshot = entry.get("continuation_fingerprint")
            if not snapshot or (
                identity in current
                and snapshot == continuation_fingerprint(continuation_content(current[identity]))
            ):
                current.pop(identity, None)
                state = "resolved"
            else:
                state = states.get(identity, "active")
        elif action == "promote":
            target = entry.get("work_item_uri")
            receipt = entry.get("promotion_receipt")
            required = {
                identity,
                *sources,
                *entry.get("work_item_source_message_ids", []),
                *(
                    current[identity].get("source_message_ids", [])
                    if not entry.get("continuation_fingerprint")
                    and previously_handled
                    and identity in current
                    else []
                ),
            }
            confirmed = (
                isinstance(receipt, dict)
                and receipt.get("uri") == target
                and required <= set(receipt.get("source_message_ids", []))
                and (
                    not entry.get("continuation_fingerprint")
                    or (
                        identity in current
                        and entry["continuation_fingerprint"]
                        == continuation_fingerprint(continuation_content(current[identity]))
                    )
                )
            )
            if confirmed:
                current.pop(identity, None)
            state = "promoted" if confirmed else states.get(identity, "active")
        else:
            raise ValueError(f"Invalid continuation action: {action}")
        states[identity] = state

        transition = {
            "continuation_id": identity,
            "action": action,
            "state": state,
            "source_message_ids": list(sources),
        }
        for key in ("summary", "reason", "work_item_uri"):
            if entry.get(key):
                transition[key] = entry[key]
        if action == "promote" and state == "active":
            transition["pending_promotion"] = True
        if action == "resolve" and state == "active":
            transition["pending_resolution"] = True
        affected = list(dict.fromkeys(([identity] if identity in old else []) + sources))
        for source in affected:
            if source not in rows:
                row = {"message_id": source}
                report.append(row)
                rows[source] = row
            row = rows[source]
            row.setdefault("continuation_actions", []).append(dict(transition))
            if action in {"create", "update"}:
                row["summary"] = entry["summary"]
            if source == identity or action in {"create", "update"}:
                row.update(continuation_id=identity, continuation_state=state)
                row["disposition"] = (
                    "work_item"
                    if state == "promoted"
                    else "archive_only"
                    if state == "resolved"
                    else "continuation"
                )
            if (
                source == identity
                and action in {"resolve", "promote"}
                and state in {"resolved", "promoted"}
            ):
                row["explicitly_dropped"] = entry["reason"]
            if source == identity and old[identity].get("source_checkpoint_uri"):
                row["source_checkpoint_uri"] = old[identity]["source_checkpoint_uri"]
            if source in by_id and by_id[source].message_kind != "checkpoint":
                row.setdefault("archive_uri", f"{archive_uri}/messages.jsonl")
    return sorted(current.values(), key=lambda value: recency.get(value["id"], (0, -1))), report
