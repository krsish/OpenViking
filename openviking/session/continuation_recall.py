# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Select bounded cold continuation candidates without changing their residency."""

import json
import re
from copy import deepcopy
from typing import Any

from openviking.message import Message, TextPart, ToolPart
from openviking.session.retention import is_user_query
from openviking.utils.token_estimation import estimate_text_tokens

_STOPWORDS = frozenset(
    "a an and are about can could for from has have hello hi how into is it its "
    "me my of on or our please tell thank thanks that the their them there these "
    "they this those to was we were what when which will with would you your "
    "你好 您好 谢谢 请问 一下 这个 那个 之前 继续 我们 是否 怎么 什么 可以 还有".split()
)


def _keywords(text: str) -> set[str]:
    """Use words and Chinese bigrams; no separate index or tokenizer is needed."""
    words = set(re.findall(r"[a-z][a-z0-9_]{2,}", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words - _STOPWORDS


def select_cold_continuations(
    store: dict[str, Any],
    messages: list[Message],
    *,
    max_items: int = 3,
    token_budget: int = 2000,
) -> list[dict[str, Any]]:
    """Recall whole active/cold entries for the latest actual user request.

    Exact IDs in that request or its tool results take priority. Tool output
    alone is not a keyword query: reading a large history must not bring every
    topic in it back into working memory. A selected candidate still needs an
    explicit extraction action with relevant fresh evidence to become hot.

    Oversized entries, including exact-ID matches, stay in storage for direct
    reading through the checkpoint's recovery reference; bodies are never cut.
    """
    if max_items <= 0 or token_budget <= 0:
        return []

    latest_user = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].message_kind != "checkpoint" and is_user_query(messages[index])
        ),
        None,
    )
    query = (
        "\n".join(part.text for part in messages[latest_user].parts if isinstance(part, TextPart))
        if latest_user is not None
        else ""
    )
    keywords = _keywords(query)
    current_turn = messages[latest_user:] if latest_user is not None else messages
    references = "\n".join(
        [query]
        + [
            part.tool_output
            for message in current_turn
            if message.message_kind != "checkpoint"
            for part in message.parts
            if isinstance(part, ToolPart) and part.tool_output
        ]
    )

    candidates = []
    for identity, entry in store.get("items", {}).items():
        if entry.get("state") != "active" or entry.get("residency") != "cold":
            continue
        value = entry["message"]
        exact = bool(re.search(r"(?<![\w-])" + re.escape(identity) + r"(?![\w-])", references))
        body = "\n".join(
            part.get("text", "") for part in value.get("parts", []) if part.get("type") == "text"
        )
        overlap = len(keywords & _keywords(body))
        coverage = overlap / len(keywords) if keywords else 0
        if not exact and (overlap < 2 or coverage < 0.3):
            continue
        rank = (exact, coverage, overlap, entry.get("last_activity_turn", 0), identity)
        candidates.append((rank, value))

    selected = []
    for _, value in sorted(candidates, key=lambda candidate: candidate[0], reverse=True):
        proposed = [*selected, value]
        if estimate_text_tokens(json.dumps(proposed, ensure_ascii=False)) <= token_budget:
            selected.append(value)
        if len(selected) == max_items:
            break
    return deepcopy(selected)
