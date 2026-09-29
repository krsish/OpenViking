# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.work_item_index import (
    upsert_work_item_embedding,
    work_item_index_ready,
)
from openviking.storage.vector_ids import vector_record_id
from openviking_cli.session.user_id import UserIdentifier

URI = "viking://user/alice/memories/work_item/wi_a.md"


def _ctx():
    return RequestContext(user=UserIdentifier(account_id="acme", user_id="alice"), role=Role.USER)


def _record(version):
    return {
        "id": vector_record_id("acme", URI, 2),
        "uri": URI,
        "account_id": "acme",
        "level": 2,
        "meta": {"work_item_version": version},
        "vector": [(version or 0) / 10.0, 0.2],
    }


class _FS:
    def __init__(self, version):
        self.version = version
        self.lock = asyncio.Lock()
        self._async_agfs = self
        self.reads = 0

    def _uri_to_path(self, uri, *, ctx):
        assert ctx.account_id == "acme"
        assert uri == URI
        return "/acme/user/alice/memories/work_item/wi_a.md"

    async def pathlock_acquire_exact(self, path, *, timeout_secs):
        assert path.endswith("/wi_a.md")
        await self.lock.acquire()
        return {"lease": "test"}

    async def pathlock_release(self, lease):
        assert lease == {"lease": "test"}
        self.lock.release()

    async def read_file(self, uri, *, ctx):
        assert self.lock.locked()
        self.reads += 1
        if self.version is None:
            raise FileNotFoundError(uri)
        return MemoryFileUtils.write(
            MemoryFile(uri=uri, content="current state", extra_fields={"version": self.version})
        )


@pytest.mark.parametrize("version", [1, 3, None])
async def test_obsolete_or_deleted_work_item_embedding_cannot_overwrite_index(monkeypatch, version):
    fs = _FS(version)
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    db = SimpleNamespace(upsert=AsyncMock(return_value="record"))

    result = await upsert_work_item_embedding(db, _record(2), ctx=_ctx(), options=None)

    assert result == ""
    db.upsert.assert_not_awaited()
    assert not fs.lock.locked()


async def test_late_old_embedding_is_rejected_after_new_version_was_indexed(monkeypatch):
    fs = _FS(2)
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    written = []

    async def upsert(data, **kwargs):
        assert fs.lock.locked()
        written.append(dict(data))
        return data["id"]

    db = SimpleNamespace(upsert=upsert)
    assert await upsert_work_item_embedding(db, _record(2), ctx=_ctx(), options=None)
    assert await upsert_work_item_embedding(db, _record(1), ctx=_ctx(), options=None) == ""
    assert len(written) == 1
    assert written[0]["work_item_version"] == 2
    assert not fs.lock.locked()


async def test_vector_write_holds_uri_lock_through_cancellation(monkeypatch):
    fs = _FS(2)
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    started = asyncio.Event()
    finish = asyncio.Event()
    written = []

    async def upsert(data, **kwargs):
        started.set()
        await finish.wait()
        assert fs.lock.locked()
        written.append(dict(data))
        return data["id"]

    task = asyncio.create_task(
        upsert_work_item_embedding(
            SimpleNamespace(upsert=upsert), _record(2), ctx=_ctx(), options=None
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert fs.lock.locked()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written[0]["work_item_version"] == 2
    assert not fs.lock.locked()


async def test_unversioned_work_item_embedding_is_rejected(monkeypatch):
    fs = _FS(2)
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fs)
    data = _record(2)
    data.pop("meta")
    with pytest.raises(ValueError, match="positive source version"):
        await upsert_work_item_embedding(SimpleNamespace(), data, ctx=_ctx(), options=None)
    assert fs.reads == 0


@pytest.mark.parametrize(
    "indexed,required,ready", [(None, 2, False), (1, 2, False), (2, 2, True), (3, 2, True)]
)
async def test_index_readiness_uses_only_exact_record_and_required_version(
    indexed, required, ready
):
    record = _record(indexed)
    record["work_item_version"] = indexed
    db = SimpleNamespace(get_strict=AsyncMock(return_value=[record]))
    ctx = _ctx()

    assert await work_item_index_ready(db, URI, required, ctx=ctx) is ready
    db.get_strict.assert_awaited_once_with([vector_record_id("acme", URI, 2)], ctx=ctx)


async def test_missing_index_record_is_not_ready():
    db = SimpleNamespace(get_strict=AsyncMock(return_value=[]))
    assert not await work_item_index_ready(db, URI, 2, ctx=_ctx())
