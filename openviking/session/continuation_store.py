# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Session-local continuation state, with an independently bounded hot projection."""

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openviking.message import Message, ToolPart
from openviking.session.continuation_state import continuation_fingerprint
from openviking.session.retention import is_user_query
from openviking_cli.utils.config import get_openviking_config


@dataclass(frozen=True)
class ContinuationRetentionPolicy:
    enabled: bool = True
    idle_turns: int = 30
    idle_days: int = 7
    min_idle_turns: int = 5


def get_continuation_retention_policy() -> ContinuationRetentionPolicy:
    defaults = ContinuationRetentionPolicy()
    try:
        memory = get_openviking_config().memory
    except FileNotFoundError:
        return defaults
    return ContinuationRetentionPolicy(
        enabled=getattr(memory, "continuation_ttl_enabled", defaults.enabled),
        idle_turns=getattr(memory, "continuation_idle_turns", defaults.idle_turns),
        idle_days=getattr(memory, "continuation_idle_days", defaults.idle_days),
        min_idle_turns=getattr(memory, "continuation_min_idle_turns", defaults.min_idle_turns),
    )


def _timestamp(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError:
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def _later(first: str | None, second: str | datetime | None) -> str | None:
    value = _timestamp(second)
    previous = _timestamp(first)
    return value.isoformat() if value and (not previous or value > previous) else first


def _is_recovery_read(message: Message) -> bool:
    """Reading an archived state is not, by itself, new progress on that state."""
    for part in message.parts:
        if not isinstance(part, ToolPart) or "read" not in part.tool_name.lower():
            continue
        arguments = json.dumps(part.tool_input or {}, ensure_ascii=False)
        if any(
            name in arguments
            for name in (
                "continuation-store.json",
                "continuation-overflow.json",
                "continuation-provenance.json",
                "/messages.jsonl",
            )
        ):
            return True
    return False


def _advance_activity(
    store: dict[str, Any], messages: list[Message]
) -> dict[str, tuple[int, str | None]]:
    seen_turns = set(store["seen_turn_ids"])
    seen_messages = set(store["seen_message_ids"])
    activity = {}
    for message in messages:
        if message.message_kind == "checkpoint" or message.id in seen_messages:
            continue
        seen_messages.add(message.id)
        store["seen_message_ids"].append(message.id)
        if is_user_query(message):
            identity = f"turn:{message.turn_id}" if message.turn_id else f"message:{message.id}"
            if identity not in seen_turns:
                seen_turns.add(identity)
                store["seen_turn_ids"].append(identity)
                store["turn_count"] += 1
        store["last_event_at"] = _later(store.get("last_event_at"), message.created_at)
        if not _is_recovery_read(message):
            activity[message.id] = (store["turn_count"], message.created_at)
    return activity


def _new_item(message: dict[str, Any], turn: int, timestamp: str | None) -> dict[str, Any]:
    return {
        "message": deepcopy(message),
        "state": "active",
        "residency": "hot",
        "last_activity_turn": turn,
        "last_activity_at": timestamp,
        "eviction_reason": None,
    }


def _touch_item(
    item: dict[str, Any], sources: list[str], activity: dict[str, tuple[int, str | None]]
) -> None:
    touched = [activity[source] for source in sources if source in activity]
    if touched:
        for turn, timestamp in touched:
            item["last_activity_turn"] = max(item["last_activity_turn"], turn)
            item["last_activity_at"] = _later(item["last_activity_at"], timestamp)
        item.update(residency="hot", eviction_reason=None)


def _idle_reason(
    item: dict[str, Any], store: dict[str, Any], policy: ContinuationRetentionPolicy
) -> str | None:
    if not policy.enabled or item.get("protection", {}).get("kind") in {
        "constraint",
        "pinned",
        "commitment",
    }:
        return None
    turns = store["turn_count"] - item["last_activity_turn"]
    if turns >= policy.idle_turns:
        return "idle_turns"
    last = _timestamp(item.get("last_activity_at"))
    now = _timestamp(store.get("last_event_at"))
    if (
        turns >= policy.min_idle_turns
        and last
        and now
        and (now - last).total_seconds() >= policy.idle_days * 86400
    ):
        return "idle_days"
    return None


def prepare_continuation_store(
    previous_store: dict[str, Any] | None,
    previous_hot: list[dict[str, Any]],
    residual: list[dict[str, Any]],
    messages: list[Message],
    actions: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    archive_uri: str,
    policy: ContinuationRetentionPolicy | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply current activity before idle eviction; absence never resolves an item.

    ``residual`` is the reducer's resulting state, including any retrieved cold
    candidates. Closure comes from its coverage receipts, not requested actions.
    Seen input identities live only in storage and make publication replay inert.
    """
    from openviking.session.work_items import continuation_content, continuation_message

    policy = policy or get_continuation_retention_policy()
    store = deepcopy(previous_store) if previous_store else {}
    store.setdefault("version", 1)
    store.setdefault("turn_count", 0)
    store.setdefault("seen_turn_ids", [])
    store.setdefault("seen_message_ids", [])
    store.setdefault("last_event_at", None)
    items = store.setdefault("items", {})
    start_turn = store["turn_count"]
    start_time = store["last_event_at"]
    activity = _advance_activity(store, messages)
    # Pre-index migration age is unknown. Give existing items a full grace period.
    for message in previous_hot:
        items.setdefault(
            message["id"], _new_item(message, store["turn_count"], store["last_event_at"])
        )
    for entry in actions:
        identity = entry.get("continuation_id")
        if identity and entry.get("action") in {"create", "update"} and entry.get("summary"):
            message = continuation_message(
                entry["summary"],
                archive_uri,
                entry.get("source_message_ids", []),
                store["last_event_at"],
                continuation_id=identity,
            )
            item = items.setdefault(identity, _new_item(message, start_turn, start_time))
            if item["state"] == "active":
                # A later batch may close this item in the same publication.
                # Its tombstone should retain the latest complete body too.
                item["message"] = message
    for message in residual:
        item = items.setdefault(message["id"], _new_item(message, start_turn, start_time))
        if item["state"] == "active":
            item["message"] = deepcopy(message)
            # Legacy summary coverage has no explicit lifecycle action. Its
            # direct new sources still establish when the state was discussed.
            if not any(entry.get("continuation_id") == message["id"] for entry in actions):
                _touch_item(item, message.get("source_message_ids", []), activity)

    current_ids = {message["id"] for message in residual}
    for row in coverage:
        identity = row.get("continuation_id")
        state = row.get("continuation_state")
        if identity in items and identity not in current_ids and state in {"resolved", "promoted"}:
            items[identity].update(state=state, residency="cold", eviction_reason=None)

    for entry in actions:
        item = items.get(entry.get("continuation_id"))
        if (
            not item
            or item["state"] != "active"
            or entry.get("action")
            not in {
                "create",
                "update",
                "keep",
            }
        ):
            continue
        protection = entry.get("protection")
        sources = list(entry.get("source_message_ids", []))
        if protection:
            protection_sources = protection.get("source_message_ids", [])
            sources.extend(protection_sources)
            fresh_protection = any(source in activity for source in protection_sources)
            initial_protection = (
                "protection" not in item
                and protection.get("kind") != "none"
                and protection_sources == [entry["continuation_id"]]
                and entry.get("continuation_fingerprint")
                == continuation_fingerprint(continuation_content(item["message"]))
            )
            if fresh_protection or initial_protection:
                item["protection"] = deepcopy(protection)
        _touch_item(item, sources, activity)

    hot = []
    for message in residual:
        item = items[message["id"]]
        if item["state"] != "active" or item["residency"] != "hot":
            continue
        reason = _idle_reason(item, store, policy)
        if reason:
            item.update(residency="cold", eviction_reason=reason)
        else:
            hot.append(deepcopy(item["message"]))
    # Whole-entry budget fallback walks newest first. A kept or restored item
    # with fresh evidence deserves the same priority as a newly written item.
    hot.sort(
        key=lambda message: (
            items[message["id"]]["last_activity_turn"],
            _timestamp(items[message["id"]].get("last_activity_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    store["archive_uri"] = archive_uri
    return store, hot


def finalize_continuation_store(
    store: dict[str, Any], projected_hot: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record budget eviction without replacing omitted entries with a stub."""
    result = deepcopy(store)
    selected = {message["id"]: message for message in projected_hot}
    for identity, item in result["items"].items():
        if item["state"] != "active":
            continue
        if identity in selected:
            item.update(message=deepcopy(selected[identity]), residency="hot", eviction_reason=None)
        elif item["residency"] == "hot":
            item.update(residency="cold", eviction_reason="budget")
    return result
