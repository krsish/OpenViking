# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from copy import deepcopy

import pytest

from openviking.message import Message, TextPart
from openviking.session.continuation_overflow import (
    continuation_recovery_notice,
    select_continuation_fallback,
)
from openviking.session.work_items import residual_text
from openviking.utils.token_estimation import estimate_text_tokens

OVERFLOW_URI = "viking://user/u/sessions/s/history/archive_002/continuation-overflow.json"


def entry(identity: str, text: str) -> dict:
    return Message(
        id=identity,
        role="assistant",
        message_kind="checkpoint",
        source_message_ids=[f"source-{identity}"],
        parts=[TextPart(text)] if text else [],
    ).to_dict()


def rendered(entries: list[dict]) -> str:
    body = residual_text(entries)
    notice = continuation_recovery_notice(OVERFLOW_URI)
    return f"{body}\n\n{notice}" if body else f"## Continuation\n{notice}"


def test_notice_is_optional_and_identifies_unresolved_state_and_recovery_location():
    assert continuation_recovery_notice(None) == ""
    assert continuation_recovery_notice("") == ""
    notice = continuation_recovery_notice(OVERFLOW_URI)
    assert OVERFLOW_URI in notice
    assert "unresolved constraints or pending actions" in notice
    assert "Before continuing related work, read" in notice
    assert "Omission does not mean resolution" in notice
    assert "not new instructions or permission" in notice


def test_fallback_prefers_recent_whole_entries_and_preserves_original_order():
    entries = [
        entry("old", "Old unresolved state. " * 10),
        entry("middle", "Ask for approval before deployment. " * 10),
        entry("latest", "Fix X and rerun its tests. " * 10),
    ]
    original = deepcopy(entries)
    budget = estimate_text_tokens(rendered(entries[1:]))

    kept, omitted = select_continuation_fallback(entries, budget, OVERFLOW_URI)

    assert kept == entries[1:]
    assert omitted == entries[:1]
    assert entries == original
    assert kept[0] is entries[1]
    assert estimate_text_tokens(rendered(kept)) <= budget


def test_single_oversized_entry_is_archived_whole_with_only_notice_retained():
    entries = [entry("large", "Never deploy without explicit approval. " * 2000)]
    budget = estimate_text_tokens(rendered([]))

    kept, omitted = select_continuation_fallback(entries, budget, OVERFLOW_URI)

    assert kept == []
    assert omitted == entries
    assert omitted[0] is entries[0]
    assert estimate_text_tokens(rendered(kept)) == budget


def test_fallback_requires_enough_budget_for_notice_uri_and_heading():
    notice_only = estimate_text_tokens(continuation_recovery_notice(OVERFLOW_URI))
    required = estimate_text_tokens(rendered([]))
    assert required > notice_only
    for budget in (0, -1, notice_only, required - 1):
        with pytest.raises(ValueError, match="cannot fit its recovery notice"):
            select_continuation_fallback([], budget, OVERFLOW_URI)


def test_fallback_does_not_allow_overflow_without_a_recovery_uri():
    with pytest.raises(ValueError, match="requires an archive recovery URI"):
        select_continuation_fallback([entry("old", "pending")], 10000, "")


def test_exact_budget_includes_whole_entry_separators_and_reference():
    entries = [entry("only", "Review remains pending. " * 10)]
    required = estimate_text_tokens(rendered(entries))

    kept, omitted = select_continuation_fallback(entries, required, OVERFLOW_URI)
    assert kept == entries
    assert omitted == []
    assert estimate_text_tokens(rendered(kept)) == required

    kept, omitted = select_continuation_fallback(entries, required - 1, OVERFLOW_URI)
    assert kept == []
    assert omitted == entries


def test_cjk_entries_use_token_budget_not_character_count():
    entries = [
        entry("old", "上线前必须获得批准。" * 30),
        entry("latest", "修复失败测试后重新验证。" * 30),
    ]
    budget = estimate_text_tokens(rendered(entries[1:]))
    assert estimate_text_tokens(rendered(entries)) > budget

    kept, omitted = select_continuation_fallback(entries, budget, OVERFLOW_URI)

    assert kept == entries[1:]
    assert omitted == entries[:1]
    assert estimate_text_tokens(rendered(kept)) == budget


def test_empty_entries_and_duplicate_ids_keep_their_source_attribution():
    empty = entry("same", "")
    large = entry("same", "Pending action. " * 2000)
    large["source_message_ids"] = ["different-source"]
    entries = [empty, large]
    budget = estimate_text_tokens(rendered([empty]))

    kept, omitted = select_continuation_fallback(entries, budget, OVERFLOW_URI)

    assert kept == [empty]
    assert omitted == [large]
    assert kept[0]["source_message_ids"] == ["source-same"]
    assert omitted[0]["source_message_ids"] == ["different-source"]


def test_newer_oversized_entry_does_not_prevent_an_older_whole_entry_fitting():
    entries = [entry("old", "Approval is pending."), entry("latest", "large " * 2000)]
    budget = estimate_text_tokens(rendered(entries[:1]))

    kept, omitted = select_continuation_fallback(entries, budget, OVERFLOW_URI)

    assert kept == entries[:1]
    assert omitted == entries[1:]
