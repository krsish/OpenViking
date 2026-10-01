# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from types import SimpleNamespace

import pytest

from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session.memory.dataclass import MemoryOperationSource, ResolvedOperation
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.work_item_receipts import (
    WORK_ITEM_REPLAY_RECEIPT_FIELD,
    flush_work_item_receipt,
    make_work_item_receipt,
    read_work_item_receipt,
    work_item_receipt_uri,
)
from openviking_cli.session.user_id import UserIdentifier

URI = "viking://user/alice/memories/work_item/wi-one.md"
ARCHIVE = "viking://user/alice/sessions/original/history/archive_001"
EXTRACTION = "wi-replay-original"


@pytest.fixture
def case():
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    files, writes = {}, []
    failure = SimpleNamespace(sidecar=False, canonical=False, read=None)

    async def read(uri, **kwargs):
        if uri == failure.read:
            raise OSError("read unavailable")
        if uri not in files:
            raise FileNotFoundError(uri)
        return files[uri]

    async def write(uri, content, **kwargs):
        if (failure.sidecar and "/work-item-receipts/" in uri) or (
            failure.canonical and uri == URI
        ):
            raise OSError("simulated interrupted write")
        files[uri] = content
        writes.append(uri)

    fs = SimpleNamespace(read_file=read, write_file=write)
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory/work_item.yaml")
    )
    updater = MemoryUpdater(registry=registry)
    updater._viking_fs = fs
    return SimpleNamespace(
        ctx=ctx, fs=fs, files=files, writes=writes, failure=failure, updater=updater
    )


def operation(snapshot=None, *, extraction=EXTRACTION, archive=ARCHIVE, state="Original state"):
    return ResolvedOperation(
        old_memory_file_content=snapshot,
        memory_type="work_item",
        uris=[URI],
        memory_fields={
            "work_item_id": "wi-one",
            "title": "Deployment",
            "goal": "Ship safely",
            "status": "open",
            "current_state": state,
        },
        source=MemoryOperationSource(extraction_id=extraction, archive_uri=archive),
        source_message_ids=["source-message"],
    )


def canonical(case):
    return MemoryFileUtils.read(case.files[URI], uri=URI)


@pytest.mark.asyncio
async def test_canonical_write_and_archive_sidecar_prove_replay_once(case):
    await case.updater._apply_upsert(operation(), case.ctx)
    receipt = await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI)
    assert receipt == make_work_item_receipt(case.ctx, ARCHIVE, EXTRACTION, URI, 1)
    assert canonical(case).extra_fields[WORK_ITEM_REPLAY_RECEIPT_FIELD] == receipt
    location = work_item_receipt_uri(case.ctx, ARCHIVE, EXTRACTION, URI)
    assert json.loads(case.files[location]) == receipt
    before = dict(case.files)
    await case.updater._apply_upsert(operation(), case.ctx)
    assert case.files == before
    assert case.writes.count(URI) == 1


@pytest.mark.asyncio
async def test_crash_before_sidecar_survives_another_session_update(case):
    case.failure.sidecar = True
    with pytest.raises(OSError, match="interrupted"):
        await case.updater._apply_upsert(operation(), case.ctx)
    assert canonical(case).extra_fields["version"] == 1
    assert await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI)
    original = case.files[URI]
    newer = operation(
        canonical(case), extraction="other-session", archive=None, state="Newer state"
    )
    # The old marker may not be erased while its sidecar is unavailable.
    with pytest.raises(OSError, match="interrupted"):
        await case.updater._apply_upsert(newer, case.ctx)
    assert case.files[URI] == original
    case.failure.sidecar = False
    await case.updater._apply_upsert(newer, case.ctx)
    assert canonical(case).extra_fields["version"] == 2
    assert canonical(case).extra_fields["current_state"] == "Newer state"
    assert WORK_ITEM_REPLAY_RECEIPT_FIELD not in canonical(case).extra_fields
    receipt = await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI)
    assert receipt["version"] == 1
    before = dict(case.files)
    # A's first operation must not run again just because someone advanced A.
    await case.updater._apply_upsert(operation(), case.ctx)
    assert case.files == before
    assert case.writes.count(URI) == 2


@pytest.mark.asyncio
async def test_new_replay_flushes_old_marker_without_accumulating_canonical_history(case):
    case.failure.sidecar = True
    with pytest.raises(OSError):
        await case.updater._apply_upsert(operation(), case.ctx)
    case.failure.sidecar = False
    next_archive = "viking://user/alice/sessions/other/history/archive_007"
    await case.updater._apply_upsert(
        operation(
            canonical(case), extraction="wi-replay-other", archive=next_archive, state="Later"
        ),
        case.ctx,
    )
    marker = canonical(case).extra_fields[WORK_ITEM_REPLAY_RECEIPT_FIELD]
    assert marker["extraction_id"] == "wi-replay-other"
    assert marker["version"] == 2
    assert (await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI))[
        "version"
    ] == 1
    assert (await read_work_item_receipt(case.fs, case.ctx, next_archive, "wi-replay-other", URI))[
        "version"
    ] == 2
    assert len([uri for uri in case.files if "/work-item-receipts/" in uri]) == 2


@pytest.mark.asyncio
async def test_failed_canonical_write_never_creates_a_success_receipt(case):
    case.failure.canonical = True
    with pytest.raises(OSError):
        await case.updater._apply_upsert(operation(), case.ctx)
    assert not case.files
    assert await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI) is None


@pytest.mark.asyncio
async def test_legacy_replay_without_archive_keeps_existing_idempotency(case):
    await case.updater._apply_upsert(operation(archive=None), case.ctx)
    await case.updater._apply_upsert(operation(archive=None), case.ctx)
    assert case.writes == [URI]
    assert WORK_ITEM_REPLAY_RECEIPT_FIELD not in canonical(case).extra_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "archive",
    [
        "viking://user/bob/sessions/s/history/archive_001",
        "viking://user/alice/sessions/../history/archive_001",
        "viking://user/alice/sessions/s/history/archive_001/../../other",
    ],
)
async def test_receipt_cannot_write_to_another_namespace_or_traversal_path(case, archive):
    await case.updater._apply_upsert(operation(archive=None), case.ctx)
    value = canonical(case)
    value.extra_fields[WORK_ITEM_REPLAY_RECEIPT_FIELD] = {
        "extraction_id": EXTRACTION,
        "archive_uri": archive,
        "uri": URI,
        "version": 1,
    }
    before = dict(case.files)
    with pytest.raises(ValueError):
        await flush_work_item_receipt(case.fs, case.ctx, value)
    assert case.files == before


@pytest.mark.asyncio
async def test_receipt_rejects_wrong_file_identity_and_future_version(case):
    await case.updater._apply_upsert(operation(archive=None), case.ctx)
    value = canonical(case)
    value.extra_fields[WORK_ITEM_REPLAY_RECEIPT_FIELD] = make_work_item_receipt(
        case.ctx, ARCHIVE, EXTRACTION, URI, 2
    )
    with pytest.raises(ValueError, match="canonical file"):
        await flush_work_item_receipt(case.fs, case.ctx, value)
    location = work_item_receipt_uri(case.ctx, ARCHIVE, EXTRACTION, URI)
    case.files[location] = json.dumps(
        make_work_item_receipt(case.ctx, ARCHIVE, "wi-replay-wrong", URI, 1)
    )
    with pytest.raises(ValueError, match="storage identity"):
        await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI)


@pytest.mark.asyncio
async def test_receipt_read_error_is_not_treated_as_missing_proof(case):
    case.failure.read = work_item_receipt_uri(case.ctx, ARCHIVE, EXTRACTION, URI)
    with pytest.raises(OSError, match="read unavailable"):
        await read_work_item_receipt(case.fs, case.ctx, ARCHIVE, EXTRACTION, URI)


@pytest.mark.asyncio
async def test_receipt_flush_uses_independent_lock_while_canonical_and_session_are_locked(
    case, tmp_path
):
    from openviking.pyagfs import get_binding_client
    from openviking.storage.viking_fs import VikingFS
    from openviking.utils.agfs_utils import RagfsBindingConfig, create_agfs_client
    from openviking_cli.utils.config.agfs_config import AGFSConfig

    try:
        get_binding_client()
    except ImportError:
        pytest.skip("RAGFS native binding is unavailable")
    fs = VikingFS(
        agfs=create_agfs_client(
            RagfsBindingConfig(agfs=AGFSConfig(path=str(tmp_path), backend="local"))
        )
    )
    case.updater._viking_fs = fs
    session_uri = ARCHIVE.rsplit("/history/", 1)[0]
    session_lease = await fs._async_agfs.pathlock_acquire_exact(
        fs._uri_to_path(session_uri, ctx=case.ctx)
    )
    canonical_lease = None
    try:
        canonical_lease = await fs._async_agfs.pathlock_acquire_exact(
            fs._uri_to_path(URI, ctx=case.ctx)
        )
        await case.updater._apply_upsert(operation(), case.ctx, lease_ref=canonical_lease)
        receipt = await read_work_item_receipt(fs, case.ctx, ARCHIVE, EXTRACTION, URI)
        assert receipt["version"] == 1
        # The sidecar is not covered by the canonical lease: forwarding it
        # would make RAGFS reject the write. The helper acquires its own lock.
        location = work_item_receipt_uri(case.ctx, ARCHIVE, EXTRACTION, URI)
        assert json.loads(await fs.read_file(location, ctx=case.ctx)) == receipt
    finally:
        if canonical_lease is not None:
            await fs._async_agfs.pathlock_release(canonical_lease)
        await fs._async_agfs.pathlock_release(session_lease)
