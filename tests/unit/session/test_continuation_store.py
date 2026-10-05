# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Idle eviction is independent of closure and cannot revive archived state."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session.continuation_overflow import (
    continuation_store_notice,
    select_continuation_fallback,
)
from openviking.session.continuation_state import continuation_fingerprint
from openviking.session.continuation_store import (
    ContinuationRetentionPolicy,
    finalize_continuation_store,
    get_continuation_retention_policy,
    prepare_continuation_store,
)
from openviking.session.work_items import continuation_message, residual_text
from openviking.utils.token_estimation import estimate_text_tokens

ARCHIVE = "viking://user/alice/sessions/test/history/archive_001"
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
POLICY = ContinuationRetentionPolicy(idle_turns=3, idle_days=7, min_idle_turns=2)


def message(identity, *, day=0, role="user", turn_id=None, kind=None, parts=None):
    return Message(
        id=identity,
        role=role,
        parts=parts or [TextPart("Discussion")],
        created_at=(NOW + timedelta(days=day)).isoformat(),
        turn_id=turn_id,
        message_kind=kind,
    )


def continuation(identity="item", summary="Check the final result"):
    return continuation_message(
        summary, ARCHIVE, ["initial"], NOW.isoformat(), continuation_id=identity
    )


def prepare(
    store=None,
    previous=None,
    current=None,
    messages=None,
    actions=None,
    coverage=None,
    policy=POLICY,
):
    return prepare_continuation_store(
        store,
        previous or [],
        current or [],
        messages or [],
        actions or [],
        coverage or [],
        ARCHIVE,
        policy,
    )


def initialized(*, protection=None):
    item = continuation()
    store, hot = prepare(previous=[item], current=[item], messages=[message("initial")])
    if protection:
        store["items"]["item"]["protection"] = protection
    return store, hot


def test_idle_turns_cold_archive_complete_entry_without_resolving():
    store, hot = initialized()
    original = deepcopy(store)
    next_store, remaining = prepare(
        store, hot, hot, [message(f"new-{index}") for index in range(3)]
    )

    assert remaining == []
    assert next_store["turn_count"] == 4
    saved = next_store["items"]["item"]
    assert saved["message"] == hot[0]
    assert saved["state"] == "active"
    assert saved["residency"] == "cold"
    assert saved["eviction_reason"] == "idle_turns"
    assert store == original


def test_new_related_keep_activity_precedes_idle_eviction():
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        hot,
        [message(f"new-{index}") for index in range(3)],
        [{"action": "keep", "continuation_id": "item", "source_message_ids": ["new-2"]}],
    )

    assert remaining == hot
    assert next_store["items"]["item"]["last_activity_turn"] == 4


@pytest.mark.parametrize("actions", [[], [{"action": "keep", "continuation_id": "item"}]])
def test_omission_and_plain_keep_do_not_refresh(actions):
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        hot,
        [message(f"new-{index}") for index in range(3)],
        actions,
    )

    assert remaining == []
    assert next_store["items"]["item"]["last_activity_turn"] == 1


def test_tool_transport_and_repeated_turn_ids_do_not_count_as_user_rounds():
    store, hot = initialized()
    messages = [
        message("question", turn_id="turn-1"),
        message("followup-chunk", turn_id="turn-1"),
        *[message(f"tool-{index}", kind="tool_transport") for index in range(100)],
        message("legacy-tool", parts=[ToolPart(tool_name="test", tool_output="Passed")]),
        message("assistant", role="assistant"),
        message("checkpoint", kind="checkpoint"),
    ]
    next_store, remaining = prepare(store, hot, hot, messages)
    replayed, replay_hot = prepare(next_store, remaining, remaining, messages)

    assert next_store["turn_count"] == 2
    assert replayed == next_store
    assert replay_hot == remaining == hot


def test_replayed_message_does_not_refresh_even_when_referenced_in_a_new_action():
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        hot,
        [message("initial"), *[message(f"new-{index}") for index in range(3)]],
        [{"action": "keep", "continuation_id": "item", "source_message_ids": ["initial"]}],
    )

    assert remaining == []
    assert next_store["items"]["item"]["last_activity_turn"] == 1


@pytest.mark.parametrize("turns, expected", [(0, True), (1, True), (2, False)])
def test_calendar_age_requires_minimum_intervening_user_rounds(turns, expected):
    store, hot = initialized()
    messages = [message(f"new-{index}", day=10) for index in range(turns)]
    next_store, remaining = prepare(store, hot, hot, messages)

    assert bool(remaining) is expected
    if not expected:
        assert next_store["items"]["item"]["eviction_reason"] == "idle_days"


def test_unknown_migration_age_gets_grace_instead_of_immediate_eviction():
    item = continuation()
    store, hot = prepare(
        previous=[item],
        current=[item],
        messages=[message(f"new-{index}", day=30) for index in range(50)],
    )

    assert hot == [item]
    assert store["items"]["item"]["last_activity_turn"] == 50


def test_legacy_summary_uses_its_new_evidence_for_activity_without_lifecycle_action():
    item = continuation_message(
        "Check the latest result", ARCHIVE, ["new-49"], NOW.isoformat(), continuation_id="legacy"
    )
    store, hot = prepare(current=[item], messages=[message(f"new-{index}") for index in range(50)])

    assert hot == [item]
    assert store["items"]["legacy"]["last_activity_turn"] == 50


def test_creation_uses_evidence_position_even_with_many_user_turns_in_one_archive():
    item = continuation_message(
        "Check the latest result", ARCHIVE, ["new-49"], NOW.isoformat(), continuation_id="new"
    )
    store, hot = prepare(
        current=[item],
        messages=[message(f"new-{index}") for index in range(50)],
        actions=[
            {
                "action": "create",
                "continuation_id": "new",
                "summary": "Check the latest result",
                "source_message_ids": ["new-49"],
            }
        ],
    )

    assert hot == [item]
    assert store["items"]["new"]["last_activity_turn"] == 50


@pytest.mark.parametrize("kind", ["constraint", "pinned", "commitment"])
def test_protection_blocks_ttl_but_not_budget_eviction(kind):
    store, hot = initialized(
        protection={"kind": kind, "reason": "Ongoing", "source_message_ids": ["initial"]}
    )
    next_store, remaining = prepare(
        store, hot, hot, [message(f"new-{index}", day=10) for index in range(4)]
    )
    cold = finalize_continuation_store(next_store, [])

    assert remaining == hot
    assert cold["items"]["item"]["residency"] == "cold"
    assert cold["items"]["item"]["eviction_reason"] == "budget"
    assert cold["items"]["item"]["message"] == hot[0]


def test_first_checkpoint_protection_registration_does_not_refresh_activity():
    store, hot = initialized()
    action = {
        "action": "keep",
        "continuation_id": "item",
        "continuation_fingerprint": continuation_fingerprint("Check the final result"),
        "protection": {
            "kind": "constraint",
            "reason": "Still binding",
            "source_message_ids": ["item"],
        },
    }
    next_store, remaining = prepare(
        store, hot, hot, [message(f"new-{index}") for index in range(3)], [action]
    )

    assert remaining == hot
    assert next_store["items"]["item"]["protection"] == action["protection"]
    assert next_store["items"]["item"]["last_activity_turn"] == 1


@pytest.mark.parametrize("changed", ["stale", "released"])
def test_stale_checkpoint_or_previously_released_protection_cannot_register(changed):
    store, hot = initialized()
    if changed == "released":
        store["items"]["item"]["protection"] = {"kind": "none"}
    action = {
        "action": "keep",
        "continuation_id": "item",
        "continuation_fingerprint": continuation_fingerprint(
            "old content" if changed == "stale" else "Check the final result"
        ),
        "protection": {
            "kind": "constraint",
            "reason": "Still binding",
            "source_message_ids": ["item"],
        },
    }
    next_store, remaining = prepare(
        store, hot, hot, [message(f"new-{index}") for index in range(3)], [action]
    )

    assert remaining == []
    assert next_store["items"]["item"].get("protection", {}).get("kind") != "constraint"


def test_fresh_protection_source_can_release_protection_and_refresh_activity():
    store, hot = initialized(protection={"kind": "constraint"})
    action = {
        "action": "keep",
        "continuation_id": "item",
        "protection": {"kind": "none", "reason": "Scope ended", "source_message_ids": ["new-2"]},
    }
    next_store, remaining = prepare(
        store, hot, hot, [message(f"new-{index}") for index in range(3)], [action]
    )

    assert remaining == hot
    assert next_store["items"]["item"]["protection"]["kind"] == "none"
    assert next_store["items"]["item"]["last_activity_turn"] == 4


@pytest.mark.parametrize("fresh", [True, False])
def test_cold_candidate_only_warms_with_new_relevant_evidence(fresh):
    store, hot = initialized()
    store = finalize_continuation_store(store, [])
    action = {"action": "keep", "continuation_id": "item"}
    if fresh:
        action["source_message_ids"] = ["revisit"]
    next_store, remaining = prepare(store, [], hot, [message("revisit")], [action])

    assert bool(remaining) is fresh
    assert next_store["items"]["item"]["residency"] == ("hot" if fresh else "cold")


def test_archive_read_itself_does_not_warm_a_cold_item():
    store, hot = initialized()
    store = finalize_continuation_store(store, [])
    recovered = message(
        "read",
        role="assistant",
        parts=[
            ToolPart(
                tool_name="openviking_read",
                tool_input={"uri": f"{ARCHIVE}/continuation-store.json"},
                tool_output="Previously recorded state",
            )
        ],
    )
    next_store, remaining = prepare(
        store,
        [],
        hot,
        [recovered],
        [
            {
                "action": "keep",
                "continuation_id": "item",
                "source_message_ids": ["read"],
            }
        ],
    )

    assert remaining == []
    assert next_store["items"]["item"]["last_activity_turn"] == 1


@pytest.mark.parametrize("state", ["resolved", "promoted"])
def test_actual_closure_creates_tombstone_and_prevents_snapshot_resurrection(state):
    store, hot = initialized()
    closed, remaining = prepare(
        store,
        hot,
        [],
        coverage=[
            {
                "continuation_id": "item",
                "continuation_state": state,
            }
        ],
    )
    next_store, resurrected = prepare(
        closed,
        hot,
        hot,
        [message("later")],
        [
            {
                "action": "keep",
                "continuation_id": "item",
                "source_message_ids": ["later"],
            }
        ],
    )

    assert remaining == resurrected == []
    assert next_store["items"]["item"]["state"] == state
    assert next_store["items"]["item"]["message"] == hot[0]


def test_requested_but_unconfirmed_closure_does_not_make_tombstone():
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        hot,
        actions=[
            {
                "action": "resolve",
                "continuation_id": "item",
            }
        ],
        coverage=[{"continuation_id": "item", "continuation_state": "active"}],
    )

    assert remaining == hot
    assert next_store["items"]["item"]["state"] == "active"


def test_update_then_close_in_one_publication_keeps_latest_body_in_tombstone():
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        [],
        [message("result")],
        actions=[
            {
                "action": "update",
                "continuation_id": "item",
                "summary": "Verification passed",
                "source_message_ids": ["result"],
            },
            {"action": "resolve", "continuation_id": "item"},
        ],
        coverage=[{"continuation_id": "item", "continuation_state": "resolved"}],
    )

    assert remaining == []
    assert next_store["items"]["item"]["message"]["parts"][0]["text"] == "Verification passed"
    assert next_store["items"]["item"]["state"] == "resolved"


def test_budget_keeps_omitted_full_body_and_updates_only_projected_body():
    first, second = continuation(), continuation("second", "A long second summary")
    store, _ = prepare(previous=[first, second], current=[first, second])
    compact = continuation("item", "Shorter")
    final = finalize_continuation_store(store, [compact])

    assert final["items"]["item"]["message"] == compact
    assert final["items"]["second"]["message"] == second
    assert final["items"]["second"]["eviction_reason"] == "budget"
    assert store["items"]["item"]["message"] == first


@pytest.mark.parametrize("was_cold", [False, True])
def test_related_keep_or_recovery_takes_priority_in_whole_entry_budget_fallback(was_cold):
    first = continuation("first", "First topic pending. " * 80)
    second = continuation("second", "Second topic pending. " * 80)
    store, hot = prepare(
        previous=[first, second], current=[first, second], messages=[message("initial")]
    )
    if was_cold:
        store = finalize_continuation_store(store, [second])
    next_store, hot = prepare(
        store,
        [second] if was_cold else hot,
        [first, second],
        [message("followup", day=1)],
        [{"action": "keep", "continuation_id": "first", "source_message_ids": ["followup"]}],
    )
    assert [value["id"] for value in hot] == ["second", "first"]
    uri = f"{ARCHIVE}/continuation-store.json"
    limit = estimate_text_tokens(residual_text([first]) + "\n\n" + continuation_store_notice(uri))
    selected, omitted = select_continuation_fallback(hot, limit, uri, cold_store=True)
    final = finalize_continuation_store(next_store, selected)

    assert selected == [first]
    assert omitted == [second]
    assert final["items"]["first"]["residency"] == "hot"
    assert final["items"]["second"]["eviction_reason"] == "budget"


def test_disabled_policy_still_records_turns_and_keeps_replay_protection():
    store, hot = initialized()
    next_store, remaining = prepare(
        store,
        hot,
        hot,
        [message(f"new-{index}", day=30) for index in range(50)],
        policy=ContinuationRetentionPolicy(enabled=False),
    )

    assert remaining == hot
    assert next_store["turn_count"] == 51


def test_policy_reads_current_configuration_and_defaults_for_standalone_use():
    config = SimpleNamespace(
        memory=SimpleNamespace(
            continuation_ttl_enabled=False,
            continuation_idle_turns=40,
            continuation_idle_days=14,
            continuation_min_idle_turns=6,
        )
    )
    with patch("openviking.session.continuation_store.get_openviking_config", return_value=config):
        assert get_continuation_retention_policy() == ContinuationRetentionPolicy(False, 40, 14, 6)
    with patch(
        "openviking.session.continuation_store.get_openviking_config", side_effect=FileNotFoundError
    ):
        assert get_continuation_retention_policy() == ContinuationRetentionPolicy()
