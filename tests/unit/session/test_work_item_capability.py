# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Reject unsupported work-item mode before changing durable session state."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart
from openviking.session.session import Session
from openviking_cli.exceptions import FailedPreconditionError

SESSION_URI = "viking://user/default/sessions/capability"
WORK_ITEM_POLICY = {"working_memory": {"mode": "work_item"}}
SUPPORTED_SCHEMA = {"Fields": [{"FieldName": "work_item_version", "FieldType": "int64"}]}


class _MemoryFS:
    def __init__(self):
        self.files = {}
        self.directories = set()
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_exact=AsyncMock(return_value={"lease": "session"}),
            pathlock_release=AsyncMock(),
        )

    def _uri_to_path(self, uri, ctx=None):
        return uri

    async def exists(self, uri, ctx=None):
        return uri in self.files or uri in self.directories

    async def stat(self, uri, **kwargs):
        if uri not in self.files and uri not in self.directories:
            raise FileNotFoundError(uri)
        return {"isDir": uri in self.directories}

    async def mkdir(self, uri, **kwargs):
        self.directories.add(uri)

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, **kwargs):
        self.files[uri] = content


def _session(*, policy=None, schema=None, policy_provider=None):
    fs = _MemoryFS()
    db = SimpleNamespace(
        get_collection_meta=AsyncMock(return_value=schema or {"Fields": []}),
        get_strict=AsyncMock(),
    )
    session = Session(
        viking_fs=fs,
        vikingdb_manager=db,
        session_id="capability",
        session_uri=SESSION_URI,
        agent_evolution_enabled=False,
        memory_policy_provider=policy_provider,
    )
    session.meta.memory_policy = policy
    return session, fs, db


def _message():
    return Message(
        id="keep-raw",
        role="user",
        parts=[TextPart("Keep this request and its exact constraints.")],
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        None,
        {"working_memory": {"mode": "legacy"}},
        {"working_memory": {"mode": "work_item", "enabled": False}},
    ],
)
async def test_legacy_or_disabled_mode_does_not_read_schema_for_create_or_commit(policy):
    session, fs, db = _session(policy=policy)
    db.get_collection_meta.side_effect = AssertionError("legacy must not require schema access")

    await session.ensure_exists()
    result = await session.commit_async()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_messages"
    assert fs.files[f"{SESSION_URI}/messages.jsonl"] == ""
    assert not any("/history/" in uri for uri in fs.files)
    db.get_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [False, True])
async def test_new_work_item_session_rejects_schema_gap_before_any_files_are_created(inherited):
    provider = AsyncMock(return_value=WORK_ITEM_POLICY) if inherited else None
    session, fs, db = _session(
        policy=None if inherited else WORK_ITEM_POLICY, policy_provider=provider
    )

    with pytest.raises(FailedPreconditionError) as error:
        await session.ensure_exists()

    assert error.value.details["reason"] == "field_missing"
    assert fs.files == {}
    assert fs.directories == set()
    db.get_collection_meta.assert_awaited_once_with(ctx=session.ctx)
    db.get_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_work_item_session_accepts_verified_int64_schema():
    session, fs, db = _session(policy=WORK_ITEM_POLICY, schema=SUPPORTED_SCHEMA)

    await session.ensure_exists()

    assert SESSION_URI in fs.directories
    assert fs.files[f"{SESSION_URI}/messages.jsonl"] == ""
    db.get_collection_meta.assert_awaited_once_with(ctx=session.ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_source", ["session", "override", "fresh_persisted"])
async def test_commit_rejects_schema_gap_before_archiving_or_rewriting_messages(policy_source):
    session, fs, db = _session(policy=WORK_ITEM_POLICY if policy_source == "session" else None)
    raw = _message()
    session._messages = [raw]
    persisted = session.meta.to_dict()
    if policy_source == "fresh_persisted":
        # Another worker selected work-item mode after this Session was loaded.
        persisted["memory_policy"] = WORK_ITEM_POLICY
    fs.directories.add(SESSION_URI)
    fs.files[f"{SESSION_URI}/.meta.json"] = json.dumps(persisted)
    fs.files[f"{SESSION_URI}/messages.jsonl"] = raw.to_jsonl() + "\n"
    before = dict(fs.files)

    with pytest.raises(FailedPreconditionError) as error:
        await session.commit_async(
            memory_policy=WORK_ITEM_POLICY if policy_source == "override" else None
        )

    assert error.value.details["reason"] == "field_missing"
    assert fs.files == before
    assert fs.directories == {SESSION_URI}
    assert [message.id for message in session._messages] == [raw.id]
    assert not any("/history/" in uri for uri in fs.files)
    db.get_collection_meta.assert_awaited_once_with(ctx=session.ctx)
    fs._async_agfs.pathlock_release.assert_awaited_once_with({"lease": "session"})


@pytest.mark.asyncio
async def test_queued_work_item_commit_fails_explicitly_without_running_extraction(monkeypatch):
    session, fs, db = _session(policy=WORK_ITEM_POLICY)
    archive_uri = f"{SESSION_URI}/history/archive_001"
    raw = _message()
    fs.files[f"{archive_uri}/messages.jsonl"] = raw.to_jsonl() + "\n"
    original_raw = fs.files[f"{archive_uri}/messages.jsonl"]
    tracker = SimpleNamespace(start=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    prepare = AsyncMock(
        side_effect=AssertionError("unsupported schema must fail before extraction")
    )
    monkeypatch.setattr(session, "_prepare_phase2_archive_messages", prepare)

    await session._run_memory_extraction(
        task_id="existing-queue-task",
        archive_uri=archive_uri,
        messages=[raw],
        first_message_id=raw.id,
        last_message_id=raw.id,
        memory_policy=WORK_ITEM_POLICY,
        agent_evolution_enabled=False,
    )

    failed = json.loads(fs.files[f"{archive_uri}/.failed.json"])
    assert "work_item_version" in failed["error"]
    assert "int64" in failed["error"]
    assert failed["stage"] == "memory_extraction"
    assert f"{archive_uri}/.done" not in fs.files
    assert f"{archive_uri}/.overview.md" not in fs.files
    assert fs.files[f"{archive_uri}/messages.jsonl"] == original_raw
    tracker.fail.assert_awaited_once()
    tracker.complete.assert_not_awaited()
    prepare.assert_not_awaited()
    db.get_collection_meta.assert_awaited_once_with(ctx=session.ctx)
