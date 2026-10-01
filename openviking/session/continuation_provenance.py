# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Keep continuation attribution in archives instead of the hot state."""

from copy import deepcopy
from typing import Any

from openviking.message import Message, TextPart
from openviking.session.work_items import continuation_content


def prepare_continuation_provenance(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    archive_uri: str,
    *,
    compacted: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prepare bounded hot messages and a ledger of direct provenance edges.

    The caller persists the ledger before publishing the checkpoint referenced
    by the returned messages. Inputs are copied verbatim, including legacy
    attribution, but their referenced checkpoints are never read or expanded.
    """
    checkpoint_uri = f"{archive_uri.rstrip('/')}/.done"
    input_by_id = {}
    for value in inputs:
        identity = value.get("id")
        if not isinstance(identity, str) or not identity or identity in input_by_id:
            raise ValueError("continuation inputs require unique nonempty message IDs")
        input_by_id[identity] = value

    hot = []
    edges = {}
    for value in outputs:
        if value.get("message_kind") != "checkpoint":
            raise ValueError("continuation provenance requires checkpoint outputs")
        identity = value.get("id")
        if not isinstance(identity, str) or not identity or identity in edges:
            raise ValueError("continuation outputs require unique nonempty message IDs")
        # Stateful repair keeps the same ID and therefore one direct edge per
        # item. Changed IDs remain supported only for older merged snapshots.
        sources = [identity] if identity in input_by_id else list(input_by_id) if compacted else []
        if not sources or any(source not in input_by_id for source in sources):
            raise ValueError("continuation output has no matching source input")

        message = Message.from_dict(value)
        if any(not isinstance(part, TextPart) for part in message.parts):
            raise ValueError("continuation checkpoint output must contain only text")
        content = continuation_content(value)

        result = Message(
            id=identity,
            role="assistant",
            message_kind="checkpoint",
            parts=[TextPart(content)],
            created_at=message.created_at,
        ).to_dict()
        result["source_checkpoint_uri"] = checkpoint_uri
        result["continuation_state_version"] = 1
        hot.append(result)
        edges[identity] = sources

    return hot, {
        "version": 1,
        "checkpoint_uri": checkpoint_uri,
        "inputs": deepcopy(inputs),
        "outputs": edges,
    }
