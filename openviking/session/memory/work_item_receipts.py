# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Durable work-item replay receipts with a single canonical write-ahead marker.

The marker is written atomically with the canonical state. Before another
writer replaces it, that writer must flush it into its originating archive.
Canonical metadata therefore stays bounded while proof survives later edits.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from openviking.core.identifiers import validate_identifier_part
from openviking.core.namespace import canonical_user_root
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import (
    MemoryFileUtils,
    memory_version_from_fields,
)
from openviking_cli.exceptions import NotFoundError

WORK_ITEM_REPLAY_RECEIPT_FIELD = "work_item_replay_receipt"


def _validate_location(ctx: Any, archive_uri: str, extraction_id: str, uri: str) -> None:
    root = canonical_user_root(ctx)
    prefix = f"{root}/sessions/"
    if not isinstance(archive_uri, str) or not archive_uri.startswith(prefix):
        raise ValueError("work_item receipt archive must belong to the canonical user")
    parts = archive_uri[len(prefix) :].split("/")
    if (
        len(parts) != 3
        or validate_identifier_part(parts[0], "session_id") is not None
        or parts[1] != "history"
        or not re.fullmatch(r"archive_[0-9]+", parts[2])
    ):
        raise ValueError("work_item receipt requires a canonical session archive URI")
    if not isinstance(extraction_id, str) or not extraction_id.startswith("wi-replay-"):
        raise ValueError("work_item receipt requires a replay extraction identity")
    item_prefix = f"{root}/memories/work_item/"
    if (
        not isinstance(uri, str)
        or not uri.startswith(item_prefix)
        or not re.fullmatch(r"wi-[A-Za-z0-9_-]+\.md", uri[len(item_prefix) :])
    ):
        raise ValueError("work_item receipt requires a canonical user work_item URI")


def work_item_receipt_uri(ctx: Any, archive_uri: str, extraction_id: str, uri: str) -> str:
    _validate_location(ctx, archive_uri, extraction_id, uri)
    identity = json.dumps([extraction_id, uri], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"{archive_uri}/work-item-receipts/{digest}.json"


def make_work_item_receipt(
    ctx: Any, archive_uri: str, extraction_id: str, uri: str, version: int
) -> dict[str, Any]:
    _validate_location(ctx, archive_uri, extraction_id, uri)
    if type(version) is not int or version <= 0:
        raise ValueError("work_item receipt version must be a positive integer")
    return {
        "extraction_id": extraction_id,
        "archive_uri": archive_uri,
        "uri": uri,
        "version": version,
    }


def _validated_receipt(ctx: Any, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("work_item replay receipt must be an object")
    return make_work_item_receipt(
        ctx,
        value.get("archive_uri"),
        value.get("extraction_id"),
        value.get("uri"),
        value.get("version"),
    )


async def _read_sidecar(fs: Any, ctx: Any, location: str) -> dict[str, Any] | None:
    try:
        raw = await fs.read_file(location, ctx=ctx)
    except (FileNotFoundError, NotFoundError):
        return None
    return _validated_receipt(ctx, json.loads(raw))


async def read_work_item_receipt(
    fs: Any, ctx: Any, archive_uri: str, extraction_id: str, uri: str
) -> dict[str, Any] | None:
    """Read durable proof, including a canonical marker not flushed before a crash."""
    location = work_item_receipt_uri(ctx, archive_uri, extraction_id, uri)
    receipt = await _read_sidecar(fs, ctx, location)
    if receipt is not None:
        if any(
            receipt[name] != expected
            for name, expected in (
                ("archive_uri", archive_uri),
                ("extraction_id", extraction_id),
                ("uri", uri),
            )
        ):
            raise ValueError("work_item receipt does not match its storage identity")
        return receipt
    try:
        canonical = MemoryFileUtils.read(await fs.read_file(uri, ctx=ctx), uri=uri)
    except (FileNotFoundError, NotFoundError):
        return None
    marker = canonical.extra_fields.get(WORK_ITEM_REPLAY_RECEIPT_FIELD)
    if marker is None:
        return None
    receipt = _validated_receipt(ctx, marker)
    if receipt["uri"] != uri or receipt["version"] > memory_version_from_fields(
        canonical.extra_fields
    ):
        raise ValueError("work_item replay marker does not match its canonical file")
    if receipt["archive_uri"] == archive_uri and receipt["extraction_id"] == extraction_id:
        return receipt
    return None


async def flush_work_item_receipt(fs: Any, ctx: Any, canonical: MemoryFile | None) -> None:
    """Preserve a previous writer's proof before its canonical marker is overwritten."""
    if canonical is None:
        return
    marker = canonical.extra_fields.get(WORK_ITEM_REPLAY_RECEIPT_FIELD)
    if marker is None:
        return
    receipt = _validated_receipt(ctx, marker)
    if receipt["uri"] != canonical.uri or receipt["version"] > memory_version_from_fields(
        canonical.extra_fields
    ):
        raise ValueError("work_item replay marker does not match its canonical file")
    location = work_item_receipt_uri(
        ctx, receipt["archive_uri"], receipt["extraction_id"], receipt["uri"]
    )
    existing = await _read_sidecar(fs, ctx, location)
    if existing is not None:
        if any(existing[key] != receipt[key] for key in ("archive_uri", "extraction_id", "uri")):
            raise ValueError("work_item receipt does not match its storage identity")
        return
    # The canonical operation's lease covers memory paths, not this archive
    # sidecar. Let write_file acquire its own exact lock; forwarding that
    # unrelated lease would fail RAGFS's coverage check.
    await fs.write_file(location, json.dumps(receipt, ensure_ascii=False), ctx=ctx)
