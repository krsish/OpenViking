# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Cold recall selects bounded background; it cannot reactivate or mutate items."""

import json
from copy import deepcopy

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session.continuation_recall import select_cold_continuations
from openviking.session.work_items import continuation_message
from openviking.utils.token_estimation import estimate_text_tokens


def stored(identity, summary, *, state="active", residency="cold", turn=1):
    return {
        "message": continuation_message(
            summary,
            "viking://user/alice/sessions/test/history/archive_001",
            ["original-evidence"],
            "2026-10-01T00:00:00Z",
            continuation_id=identity,
        ),
        "state": state,
        "residency": residency,
        "last_activity_turn": turn,
    }


def user(text, *, kind="user_query"):
    return Message(id="current-request", role="user", message_kind=kind, parts=[TextPart(text)])


@pytest.mark.parametrize(
    ("query", "summary"),
    [
        ("Can we resume the invoice reconciliation?", "Invoice reconciliation awaits approval."),
        ("部署权限的审批进度怎样？", "部署权限审批还在等待负责人回复。"),
    ],
)
def test_current_request_recalls_related_english_or_chinese_item(query, summary):
    entry = stored("wi-continuation-related", summary)
    store = {
        "items": {
            "wi-continuation-related": entry,
            "wi-continuation-other": stored("wi-continuation-other", "Dinner reservation pending."),
        }
    }
    assert select_cold_continuations(store, [user(query)]) == [entry["message"]]


@pytest.mark.parametrize("query", ["hello", "hello there, thanks!", "你好，谢谢！", "How are you?"])
def test_greetings_do_not_recall_history(query):
    store = {"items": {"c": stored("c", "Hello there, thanks! 你好，谢谢！ How are you?")}}
    assert select_cold_continuations(store, [user(query)]) == []


def test_only_latest_actual_user_query_supplies_keywords():
    entry = stored("wi-continuation-invoice", "Invoice reconciliation needs approval.")
    messages = [
        user("Invoice reconciliation status?"),
        user("What is for dinner?"),
        Message(
            id="tool-result",
            role="user",
            parts=[
                ToolPart(tool_name="read", tool_output="Invoice reconciliation needs approval.")
            ],
        ),
        user("Invoice reconciliation?", kind="checkpoint"),
    ]
    assert select_cold_continuations({"items": {entry["message"]["id"]: entry}}, messages) == []


def test_exact_ids_take_priority_and_only_active_cold_state_is_eligible():
    entries = {
        "wi-continuation-keywords": stored("wi-continuation-keywords", "Invoice reconciliation"),
        "wi-continuation-explicit": stored("wi-continuation-explicit", "Approval pending."),
        "wi-continuation-resolved": stored(
            "wi-continuation-resolved", "Invoice reconciliation", state="resolved"
        ),
        "wi-continuation-promoted": stored(
            "wi-continuation-promoted", "Invoice reconciliation", state="promoted"
        ),
        "wi-continuation-hot": stored(
            "wi-continuation-hot", "Invoice reconciliation", residency="hot"
        ),
    }
    messages = [
        user(
            "Invoice reconciliation: wi-continuation-explicit; wi-continuation-resolved; wi-continuation-promoted; wi-continuation-hot"
        )
    ]
    result = select_cold_continuations({"items": entries}, messages, max_items=1)
    assert result == [entries["wi-continuation-explicit"]["message"]]


def test_tool_result_can_identify_candidate_but_never_changes_store():
    identity = "wi-continuation-invoice"
    store = {"items": {identity: stored(identity, "Invoice reconciliation pending.")}}
    original = deepcopy(store)
    messages = [
        user("Look up that older item."),
        Message(
            id="read",
            role="assistant",
            parts=[ToolPart(tool_name="read", tool_output=f"[{identity}] Invoice reconciliation")],
        ),
    ]
    selected = select_cold_continuations(store, messages)
    assert selected == [store["items"][identity]["message"]]
    selected[0]["parts"][0]["text"] = "Changed by caller"
    assert store == original


def test_previous_turn_tool_ids_and_partial_id_matches_do_not_recall():
    identity = "wi-continuation-invoice"
    store = {"items": {identity: stored(identity, "Waiting for approval.")}}
    messages = [
        user("Read the old item."),
        Message(id="read", role="assistant", parts=[ToolPart(tool_output=identity)]),
        user(f"Hello {identity}-other"),
    ]
    assert select_cold_continuations(store, messages) == []


def test_one_shared_generic_word_is_insufficient():
    store = {"items": {"c": stored("c", "Approval for unrelated travel is pending.")}}
    assert select_cold_continuations(store, [user("deployment approval")]) == []


def test_whole_entry_budget_skips_oversized_exact_match_and_keeps_smaller_one():
    large_id = "wi-continuation-large"
    small_id = "wi-continuation-small"
    large = stored(large_id, "完整保留。" * 1000)
    small = stored(small_id, "Still waiting.")
    store = {"items": {large_id: large, small_id: small}}
    budget = estimate_text_tokens(json.dumps([small["message"]], ensure_ascii=False))
    result = select_cold_continuations(store, [user(f"{large_id} {small_id}")], token_budget=budget)
    assert result == [small["message"]]
    assert large["message"]["parts"][0]["text"] == "完整保留。" * 1000
    assert select_cold_continuations(store, [user(large_id)], token_budget=budget) == []


def test_item_count_and_combined_budget_are_bounded_with_recent_ties_first():
    entries = {
        f"wi-continuation-{index}": stored(
            f"wi-continuation-{index}", "Invoice reconciliation pending.", turn=index
        )
        for index in range(6)
    }
    store = {"items": entries}
    messages = [user("Invoice reconciliation")]
    expected = [entries[f"wi-continuation-{index}"]["message"] for index in (5, 4, 3)]
    assert select_cold_continuations(store, messages) == expected
    budget = estimate_text_tokens(json.dumps(expected[:2], ensure_ascii=False))
    assert select_cold_continuations(store, messages, token_budget=budget) == expected[:2]
    assert select_cold_continuations(store, messages, max_items=0) == []
    assert select_cold_continuations(store, messages, token_budget=0) == []


def test_no_query_or_tool_reference_leaves_state_cold():
    store = {"items": {"c": stored("c", "Invoice reconciliation pending.")}}
    assert select_cold_continuations(store, []) == []
    assert (
        select_cold_continuations(
            store, [Message(id="a", role="assistant", parts=[TextPart("Invoice reconciliation")])]
        )
        == []
    )
