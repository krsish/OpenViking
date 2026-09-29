# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Versioned work-item vector writes and per-item checkpoint readiness.

Existing local/cuvs collections receive the work_item_version int64 field via
normal schema migration. Existing remote collections must add that field with
default 0 and reindex work items before cold checkpoints can become ready.
"""

from typing import Any, Dict

from openviking.server.identity import RequestContext
from openviking.service.task_tracker_concurrency import run_to_completion
from openviking.session.memory.utils.memory_file_utils import (
    MemoryFileUtils,
    memory_version_from_fields,
)
from openviking.session.memory.work_item import is_work_item_uri
from openviking.storage.vector_ids import vector_record_id
from openviking_cli.exceptions import NotFoundError

WORK_ITEM_VERSION_FIELD = "work_item_version"


def is_work_item_leaf(uri: str) -> bool:
    return (
        is_work_item_uri(uri)
        and uri.endswith(".md")
        and not uri.endswith(("/.abstract.md", "/.overview.md"))
    )


async def work_item_index_ready(
    vikingdb: Any,
    uri: str,
    version: int,
    *,
    ctx: RequestContext,
) -> bool:
    """Whether the L2 vector record covers the checkpoint's required version.

    This exact read never waits for other items or asks an embedding service.
    Missing version fields, including unmigrated remote collections, are not
    ready. Backend failures propagate so callers can distinguish a read failure
    from pending work. Newer indexed versions also satisfy an older checkpoint.
    """
    if version <= 0 or not is_work_item_leaf(uri):
        return False
    records = await vikingdb.get_strict([vector_record_id(ctx.account_id, uri, 2)], ctx=ctx)
    for record in records:
        if record.get("uri") != uri:
            continue
        try:
            indexed_version = int(record.get(WORK_ITEM_VERSION_FIELD) or 0)
        except (TypeError, ValueError):
            continue
        if indexed_version >= version:
            return True
    return False


async def upsert_work_item_embedding(
    vikingdb: Any,
    data: Dict[str, Any],
    *,
    ctx: RequestContext,
    options: Any,
) -> str:
    """Write only a vector generated from the still-current canonical version.

    Embedding itself happens before locking. The exact URI lock is shared with
    memory updates and deletion, and stays held until the vector write settles.
    """
    from openviking.storage.viking_fs import get_viking_fs

    meta = data.get("meta") or {}
    try:
        version = int(meta.get(WORK_ITEM_VERSION_FIELD) or 0)
    except (AttributeError, TypeError, ValueError):
        version = 0
    if version <= 0:
        raise ValueError("work_item embedding requires a positive source version")

    uri = data["uri"]
    viking_fs = get_viking_fs()
    lease = await viking_fs._async_agfs.pathlock_acquire_exact(
        viking_fs._uri_to_path(uri, ctx=ctx), timeout_secs=300.0
    )
    try:
        try:
            raw = await viking_fs.read_file(uri, ctx=ctx)
        except (FileNotFoundError, NotFoundError):
            return ""
        current = MemoryFileUtils.read(raw or "", uri=uri)
        if memory_version_from_fields(current.extra_fields, default=0) != version:
            return ""
        data[WORK_ITEM_VERSION_FIELD] = version
        return await run_to_completion(lambda: vikingdb.upsert(data, ctx=ctx, options=options))
    finally:
        await viking_fs._async_agfs.pathlock_release(lease)
