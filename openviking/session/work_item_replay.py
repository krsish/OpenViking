# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Select the latest persisted revision of each immutable extraction batch."""

from copy import deepcopy
from typing import Any


def merge_work_item_replays(*collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge plans without letting an ancestor or delayed save restore old state.

    Extraction identity and source batch never change during reconciliation.
    Legacy plans have revision zero. Keep the first batch's position, but use
    its highest revision; later copies win ties. Reject ambiguous batch identity
    instead of letting a caller's first-match lookup select arbitrary writes.
    Returned plans are detached from the inputs so in-memory edits cannot alter
    the supposedly persisted revision before its replacement is saved.
    """
    by_extraction: dict[str, dict[str, Any]] = {}
    batch_by_extraction: dict[str, tuple[str, ...]] = {}
    extraction_by_batch: dict[tuple[str, ...], str] = {}
    for collection in collections:
        if not isinstance(collection, list):
            raise ValueError("work_item replay plans must be a list")
        for plan in collection:
            if not isinstance(plan, dict):
                raise ValueError("work_item replay plan must be an object")
            extraction_id = plan.get("extraction_id")
            message_ids = plan.get("message_ids")
            revision = plan.get("revision", 0)
            if not isinstance(extraction_id, str) or not extraction_id:
                raise ValueError("work_item replay requires an extraction identity")
            if (
                not isinstance(message_ids, list)
                or not message_ids
                or any(not isinstance(identity, str) or not identity for identity in message_ids)
                or len(set(message_ids)) != len(message_ids)
            ):
                raise ValueError("work_item replay requires an ordered, unique message batch")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError("work_item replay revision must be a nonnegative integer")
            batch = tuple(message_ids)
            if extraction_id in batch_by_extraction and batch_by_extraction[extraction_id] != batch:
                raise ValueError("work_item replay cannot change its original message batch")
            if batch in extraction_by_batch and extraction_by_batch[batch] != extraction_id:
                raise ValueError("work_item replay batch has conflicting extraction identities")
            batch_by_extraction[extraction_id] = batch
            extraction_by_batch[batch] = extraction_id
            previous = by_extraction.get(extraction_id)
            if previous is None or revision >= previous.get("revision", 0):
                by_extraction[extraction_id] = deepcopy(plan)
    return list(by_extraction.values())
