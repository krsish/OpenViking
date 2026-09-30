# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart, ToolPart
from openviking.session import work_items as wi
from openviking.session.memory_policy import MemoryPolicy
from openviking.session.session import (
    Session,
    _apply_agent_evolution_setting,
    _effective_memory_types,
)
from openviking.utils.token_estimation import estimate_text_tokens

SESSION_URI = "viking://user/default/sessions/work-items"


class MemoryFS:
    def __init__(self):
        self.files = {}
        self._async_agfs = SimpleNamespace(mv=AsyncMock(side_effect=self.move))

    def _uri_to_path(self, uri, ctx=None):
        return uri

    async def move(self, source, target):
        self.files[target] = self.files.pop(source)

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, **kwargs):
        self.files[uri] = content

    async def exists(self, uri, ctx=None):
        return uri in self.files

    async def ls(self, uri, ctx=None):
        prefix = uri + "/"
        return [
            {"name": name}
            for name in sorted(
                {
                    path[len(prefix) :].split("/")[0]
                    for path in self.files
                    if path.startswith(prefix)
                }
            )
        ]


def message(identity, text="continue"):
    return Message(
        id=identity, role="user", parts=[TextPart(text)], created_at="2026-01-01T00:00:00Z"
    )


def item(identity, status="in_progress", text="current state"):
    return {
        "uri": f"viking://user/default/memories/work_item/{identity}.md",
        "version": 1,
        "fields": {
            "title": identity,
            "goal": "finish",
            "status": status,
            "current_state": text,
            "next_action": "test",
            "constraints": "keep compatibility",
        },
    }


def session_with_fs():
    fs = MemoryFS()
    session = Session(
        viking_fs=fs,
        session_id="work-items",
        session_uri=SESSION_URI,
        vikingdb_manager=versioned_index(),
    )
    session._meta.memory_policy = {"working_memory": {"mode": "work_item"}}
    return session, fs


def versioned_index():
    return SimpleNamespace(
        get_collection_meta=AsyncMock(
            return_value={"Fields": [{"FieldName": "work_item_version", "FieldType": "int64"}]}
        )
    )


def archive(fs, number, messages, done=None, failed=False):
    uri = f"{SESSION_URI}/history/archive_{number:03d}"
    fs.files[f"{uri}/messages.jsonl"] = "\n".join(value.to_jsonl() for value in messages)
    fs.files[f"{uri}/.meta.json"] = json.dumps({"working_memory_mode": "work_item"})
    if done is not None:
        fs.files[f"{uri}/.done"] = json.dumps(done)
        fs.files[f"{uri}/.overview.md"] = "published projection"
    if failed:
        fs.files[f"{uri}/.failed.json"] = "{}"
    return uri


def checkpoint(number=1):
    return {
        "mode": "work_item",
        "version": 1,
        "archive_id": f"archive_{number:03d}",
        "compact_ready": True,
        "starting_message_id": "1",
        "ending_message_id": "1",
        "work_items": [],
        "active_work_items": [],
        "residual": [],
    }


@pytest.mark.asyncio
async def test_work_item_history_stops_at_context_reset():
    session, fs = session_with_fs()
    archive(fs, 1, [message("old")], checkpoint())
    reset_uri = f"{SESSION_URI}/history/archive_002"
    fs.files[f"{reset_uri}/.done"] = json.dumps(
        {"context_reset": True, "working_memory_enabled": False}
    )
    archive(fs, 3, [message("after-reset")])
    for use_checkpoint in (True, False):
        done, messages = await session._work_item_history(use_checkpoint=use_checkpoint)
        assert done == {}
        assert [value.id for value in messages] == ["after-reset"]


def test_mode_roundtrip_and_agent_policy_copy():
    policy = MemoryPolicy.from_dict(
        {"working_memory": {"mode": "work_item"}, "memory_types": ["profile"]}
    )
    assert MemoryPolicy.from_dict(policy.to_dict()).working_memory_mode == "work_item"
    assert (
        _apply_agent_evolution_setting(policy, agent_evolution_enabled=False).working_memory_mode
        == "work_item"
    )
    assert MemoryPolicy.default().to_dict() == {
        "self": {"enabled": True},
        "peer": {"enabled": True},
    }


def test_legacy_policy_does_not_enable_work_items_when_agent_evolution_is_disabled(monkeypatch):
    monkeypatch.setattr(
        "openviking.session.session._enabled_memory_types", lambda: {"profile", "work_item"}
    )
    legacy = _apply_agent_evolution_setting(MemoryPolicy.default(), agent_evolution_enabled=False)
    assert _effective_memory_types(legacy) == {"profile"}
    explicit = MemoryPolicy.from_dict({"memory_types": ["work_item"]})
    assert _effective_memory_types(explicit) == {"work_item"}


def test_projection_bounded_across_long_task_history_and_multiple_active_tasks():
    for count in (2, 20, 200):
        items = [item(f"wi-{number}", text="state " * 50) for number in range(count)]
        projection, selected = wi.build_projection(items, [])
        assert len(selected) == min(count, 3)
        assert estimate_text_tokens(projection) <= wi.PROJECTION_TOKEN_BUDGET
        assert "wi-0" in projection and "wi-1" in projection
        assert projection.count("constraints: keep compatibility") == len(selected)


def test_projection_does_not_reactivate_terminal_item():
    projection, selected = wi.build_projection([item("A", "done"), item("B")], [])
    assert [value["uri"] for value in selected] == [item("B")["uri"]]
    assert "## work_item A" not in projection


def test_coverage_retains_unassigned_original_and_rejects_residual_overflow():
    messages = [message("covered"), message("unassigned", "important constraint")]
    residual, ledger = wi.coverage_report(
        messages, [{"uri": item("A")["uri"], "source_message_ids": ["covered"]}], "archive"
    )
    assert residual == [messages[1].to_dict()]
    assert len(ledger) == len(messages)
    projection, _ = wi.build_projection([item("A")], residual)
    assert "important constraint" in projection
    with pytest.raises(ValueError, match="residual"):
        wi.build_projection([], [message("huge", "x" * 10000).to_dict()])


@pytest.mark.asyncio
async def test_pending_and_failed_archives_keep_raw_tail_after_last_ready_checkpoint():
    session, fs = session_with_fs()
    archive(fs, 1, [message("1")], checkpoint())
    archive(fs, 2, [message("2")], failed=True)
    archive(fs, 3, [message("3")])
    session._messages = [message("4")]
    result = await session.get_session_context(5000)
    assert result["checkpoint"]["archive_id"] == "archive_001"
    assert [value["id"] for value in result["messages"]] == ["2", "3", "4"]
    too_small = await session.get_session_context(1)
    assert too_small["status"] == "budget_insufficient"
    assert "checkpoint" not in too_small


@pytest.mark.asyncio
async def test_inherited_commit_mode_uses_archive_metadata_when_session_policy_is_absent():
    session, fs = session_with_fs()
    session._meta.memory_policy = None
    archive(fs, 1, [message("1")], checkpoint())
    archive(fs, 2, [message("2")], failed=True)
    result = await session.get_session_context(5000)
    assert result["checkpoint"]["archive_id"] == "archive_001"
    assert [value["id"] for value in result["messages"]] == ["2"]


@pytest.mark.asyncio
async def test_unpublished_or_mismatched_done_cannot_advance_boundary():
    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("1")], checkpoint(2))
    assert (await session.get_session_archive("archive_001"))["status"] == "not_ready"
    del fs.files[f"{uri}/.done"]
    assert (await session.get_session_archive("archive_001"))["status"] == "not_ready"
    result = await session.get_session_context(5000)
    assert result["status"] == "not_ready"
    assert [value["id"] for value in result["messages"]] == ["1"]


@pytest.mark.asyncio
async def test_publish_done_last_and_failed_rename_never_publishes(monkeypatch):
    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("1")])
    fs.files[f"{uri}/.overview.md"] = "complete projection"
    fs._async_agfs.mv.side_effect = RuntimeError("crash before publish")
    with pytest.raises(RuntimeError):
        await session._write_done_file(uri, "1", "1", checkpoint=checkpoint())
    assert f"{uri}/.done" not in fs.files
    assert (await session.get_session_archive("archive_001"))["status"] == "not_ready"
    fs._async_agfs.mv.side_effect = fs.move
    await session._write_done_file(uri, "1", "1", checkpoint=checkpoint())
    assert (await session.get_session_archive("archive_001"))["status"] == "ready"
    before = fs.files[f"{uri}/.done"]
    await session._write_done_file(uri, "1", "1", checkpoint=checkpoint())
    assert fs.files[f"{uri}/.done"] == before


@pytest.mark.asyncio
async def test_hot_binding_does_not_wait_for_embedding(monkeypatch):
    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("1")])
    state = item("A")
    fs.files[f"{uri}/.meta.json"] = json.dumps(
        {
            "work_items": [{"uri": state["uri"], "version": 1}],
            "work_item_coverage": [{"uri": state["uri"], "source_message_ids": ["1"]}],
        }
    )
    monkeypatch.setattr(wi, "read_work_item", AsyncMock(return_value=state))
    index_check = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "openviking.session.memory.work_item_index.work_item_index_ready", index_check
    )
    published = await session._prepare_work_item_checkpoint(uri, [message("1")], {})
    assert published["compact_ready"]
    assert published["active_work_items"] == [state]
    index_check.assert_not_awaited()
    assert f"{uri}/.done" not in fs.files


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_terminal_binding_never_blocks_publication_or_refresh(monkeypatch, status):
    session, fs = session_with_fs()
    uri = archive(fs, 1, [message("1")])
    state = item("A", status=status)
    fs.files[f"{uri}/.meta.json"] = json.dumps(
        {
            "work_items": [{"uri": state["uri"], "version": 1}],
            "work_item_coverage": [{"uri": state["uri"], "source_message_ids": ["1"]}],
        }
    )
    monkeypatch.setattr(wi, "read_work_item", AsyncMock(return_value=state))
    index_check = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "openviking.session.memory.work_item_index.work_item_index_ready", index_check
    )
    published = await session._prepare_work_item_checkpoint(uri, [message("1")], {})
    assert published["compact_ready"] and published["active_work_items"] == []
    assert published["coverage"][0]["work_item_uris"] == [state["uri"]]
    _, refreshed = await session._read_work_item_projection({"active_work_items": [state]})
    assert refreshed["active_work_items"] == []
    index_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_restores_cold_item_without_claiming_new_message_coverage(monkeypatch):
    session, fs = session_with_fs()
    messages = [message("resume", "Continue the earlier billing investigation")]
    uri = archive(fs, 1, messages)
    states = {item(name)["uri"]: item(name) for name in ("A", "B", "C", "D")}
    a = item("A")
    fs.files[f"{uri}/.meta.json"] = json.dumps(
        {
            "work_items": [{"uri": a["uri"], "version": 1}],
            "work_item_activations": [{"uri": a["uri"], "source_message_ids": ["resume"]}],
        }
    )

    async def read_item(_fs, _ctx, item_uri):
        return states[item_uri]

    monkeypatch.setattr(wi, "read_work_item", read_item)
    index_check = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "openviking.session.memory.work_item_index.work_item_index_ready", index_check
    )
    previous = {"active_work_items": [item(name) for name in ("B", "C", "D")]}
    result = await session._prepare_work_item_checkpoint(uri, messages, previous)
    assert [value["fields"]["title"] for value in result["active_work_items"]] == ["A", "B", "C"]
    assert result["active_work_items"][0]["version"] == 1
    assert result["residual"] == [messages[0].to_dict()]
    assert "residual_uri" in result["coverage"][0]


@pytest.mark.asyncio
async def test_phase2_builds_checkpoint_from_extraction_without_summary_llm(monkeypatch):
    session, fs = session_with_fs()
    messages = [message("1", "Continue A; first run its regression tests")]
    uri = archive(fs, 1, messages)
    state = item("A")
    tracker = SimpleNamespace(start=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    wait_tracker = SimpleNamespace(
        register_request=lambda _: None, cleanup=lambda _: None, wait_for_request=AsyncMock()
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(extraction_enabled=True, session_skill_extraction_enabled=False)
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr("openviking.session.session.get_request_wait_tracker", lambda: wait_tracker)
    monkeypatch.setattr("openviking.session.session.get_openviking_config", lambda: config)
    monkeypatch.setattr(session, "_run_usage_reporting", AsyncMock(return_value=[]))
    monkeypatch.setattr(session, "_merge_and_save_commit_meta", AsyncMock())
    monkeypatch.setattr(
        session._tool_outputs, "hydrate_for_extraction", AsyncMock(return_value=messages)
    )
    monkeypatch.setattr(wi, "read_work_item", AsyncMock(return_value=state))
    session._session_compressor = SimpleNamespace(
        extract_long_term_memories=AsyncMock(
            return_value={
                "contexts": [],
                "work_items": [{"uri": state["uri"], "version": 1}],
                "work_item_coverage": [{"uri": state["uri"], "source_message_ids": ["1"]}],
            }
        )
    )
    summary_llm = AsyncMock(side_effect=AssertionError("work_item must not generate legacy WM"))
    monkeypatch.setattr(session, "_generate_archive_summary_async", summary_llm)
    policy = {"working_memory": {"mode": "work_item"}, "memory_types": ["work_item"]}
    await session._run_memory_extraction("test-task", uri, messages, "1", "1", policy)
    tracker.fail.assert_not_awaited()
    tracker.complete.assert_awaited_once()
    summary_llm.assert_not_awaited()
    wait_tracker.wait_for_request.assert_not_awaited()
    result = await session.get_session_archive("archive_001")
    assert result["checkpoint"]["compact_ready"]
    assert result["checkpoint"]["residual"] == []
    assert "next_action: test" in result["overview"]
    # Replaying after a failed publication uses persisted extraction progress.
    del fs.files[f"{uri}/.done"]
    await session._run_memory_extraction("test-task", uri, messages, "1", "1", policy)
    session._session_compressor.extract_long_term_memories.assert_awaited_once()
    assert (await session.get_session_archive("archive_001"))["status"] == "ready"


def _persist_canonical_work_item(fs, state):
    """Publish another session's canonical state without changing old checkpoints."""
    from openviking.session.memory.dataclass import MemoryFile
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    fs.files[state["uri"]] = MemoryFileUtils.write(
        MemoryFile(
            uri=state["uri"],
            memory_type="work_item",
            extra_fields={**state["fields"], "version": state["version"]},
        )
    )


def _checkpoint_with_items(states):
    published = checkpoint()
    published["active_work_items"] = states
    published["work_items"] = [
        {"uri": state["uri"], "version": state["version"], "index_ready": False} for state in states
    ]
    return published


@pytest.mark.asyncio
async def test_another_session_completing_active_item_removes_stale_next_action():
    from copy import deepcopy

    session, fs = session_with_fs()
    original = item("A")
    original["fields"]["next_action"] = "retry the already completed deployment"
    _persist_canonical_work_item(fs, original)
    uri = archive(fs, 1, [message("1")], _checkpoint_with_items([original]))
    session._messages = [message("live", "Do not repeat a completed deployment")]
    before = await session.get_session_context(5000)
    assert original["fields"]["next_action"] in before["latest_archive_overview"]
    published_done = fs.files[f"{uri}/.done"]

    # A different session of the same user completes A. The old session's
    # publication boundary stays fixed, but its next read must use version 2.
    completed = deepcopy(original)
    completed["version"] = 2
    completed["fields"]["status"] = "done"
    _persist_canonical_work_item(fs, completed)
    after = await session.get_session_context(5000)
    refreshed_archive = await session.get_session_archive("archive_001")
    assert after["status"] == refreshed_archive["status"] == "ready"
    assert after["checkpoint"]["archive_id"] == "archive_001"
    assert after["checkpoint"]["active_work_items"] == []
    assert after["checkpoint"]["work_items"][0]["version"] == 2
    assert original["fields"]["next_action"] not in after["latest_archive_overview"]
    assert original["fields"]["next_action"] not in refreshed_archive["overview"]
    assert after["messages"] == [session._messages[0].to_dict()]
    assert fs.files[f"{uri}/.done"] == published_done


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_after_second_batch_failure", [False, True])
async def test_next_batch_and_restarted_archive_prefetch_successful_work_item(
    monkeypatch, restart_after_second_batch_failure
):
    session, fs = session_with_fs()
    messages = [message("1", "Start A"), message("2", "Continue the same A")]
    uri = archive(fs, 1, messages)
    state = item("A")
    tracker = SimpleNamespace(start=AsyncMock(), complete=AsyncMock(), fail=AsyncMock())
    wait_tracker = SimpleNamespace(
        register_request=lambda _: None, cleanup=lambda _: None, wait_for_request=AsyncMock()
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(extraction_enabled=True, session_skill_extraction_enabled=False)
    )
    monkeypatch.setattr("openviking.service.task_tracker.get_task_tracker", lambda: tracker)
    monkeypatch.setattr("openviking.session.session.get_request_wait_tracker", lambda: wait_tracker)
    monkeypatch.setattr("openviking.session.session.get_openviking_config", lambda: config)
    monkeypatch.setattr("openviking.session.session.is_retryable_api_error", lambda exc: False)
    monkeypatch.setattr(
        "openviking.session.session._enabled_memory_types", lambda: {"profile", "work_item"}
    )
    calls = []
    injected_failure = False

    async def extract(**kwargs):
        nonlocal injected_failure
        ids = [source.id for source in kwargs["messages"]]
        calls.append((ids, list(kwargs["work_item_uris"])))
        # An empty explicit whitelist adds only the mode's required memory type.
        assert kwargs["allowed_memory_types"] == {"work_item"}
        if ids == ["1"]:
            assert kwargs["work_item_uris"] == []
            _persist_canonical_work_item(fs, state)
        else:
            assert ids == ["2"]
            assert kwargs["work_item_uris"] == [state["uri"]]
            if restart_after_second_batch_failure and not injected_failure:
                injected_failure = True
                raise ValueError("second batch interrupted before writing")
        return {
            "contexts": [],
            "work_items": [{"uri": state["uri"], "version": 1}],
            "work_item_coverage": [{"uri": state["uri"], "source_message_ids": ids}],
        }

    compressor = SimpleNamespace(extract_long_term_memories=AsyncMock(side_effect=extract))

    def wire_phase2(target):
        target._session_compressor = compressor
        monkeypatch.setattr(target, "_run_usage_reporting", AsyncMock(return_value=[]))
        monkeypatch.setattr(target, "_merge_and_save_commit_meta", AsyncMock())
        monkeypatch.setattr(
            target._tool_outputs,
            "hydrate_for_extraction",
            AsyncMock(side_effect=lambda values: values),
        )
        monkeypatch.setattr(
            target,
            "_generate_archive_summary_async",
            AsyncMock(side_effect=AssertionError("work_item must not invoke a summary LLM")),
        )

    wire_phase2(session)
    policy = {"working_memory": {"mode": "work_item"}, "memory_types": []}
    batching = {"message_count_threshold": 1, "pending_token_threshold": 0}
    await session._run_memory_extraction(
        "batch-task", uri, messages, "1", "2", policy, auto_commit_policy=batching
    )
    if restart_after_second_batch_failure:
        tracker.fail.assert_awaited_once()
        assert f"{uri}/.done" not in fs.files
        meta = json.loads(fs.files[f"{uri}/.meta.json"])
        assert meta["completed_memory_steps"]["long_term"] == ["1"]
        assert meta["work_items"] == [{"uri": state["uri"], "version": 1}]
        session = Session(
            viking_fs=fs,
            session_id="work-items",
            session_uri=SESSION_URI,
            vikingdb_manager=versioned_index(),
        )
        session._meta.memory_policy = policy
        wire_phase2(session)
        await session._run_memory_extraction(
            "retry-task", uri, messages, "1", "2", policy, auto_commit_policy=batching
        )
        assert calls == [(["1"], []), (["2"], [state["uri"]]), (["2"], [state["uri"]])]
    else:
        tracker.fail.assert_not_awaited()
        assert calls == [(["1"], []), (["2"], [state["uri"]])]
    tracker.complete.assert_awaited_once()
    wait_tracker.wait_for_request.assert_not_awaited()
    published = await session.get_session_archive("archive_001")
    assert published["status"] == "ready"
    assert published["checkpoint"]["residual"] == []
    assert [row["message_id"] for row in published["checkpoint"]["coverage"]] == ["1", "2"]
    meta = json.loads(fs.files[f"{uri}/.meta.json"])
    assert meta["completed_memory_steps"]["long_term"] == ["1", "2"]


@pytest.mark.asyncio
async def test_refreshed_state_exceeding_request_budget_retains_uncovered_raw_tail():
    from copy import deepcopy

    session, fs = session_with_fs()
    original = item("A")
    _persist_canonical_work_item(fs, original)
    archive(fs, 1, [message("1")], _checkpoint_with_items([original]))
    pending = message("pending", "uncovered constraint: keep the old endpoint")
    live = message("live", "uncovered user question: has the migration finished?")
    archive(fs, 2, [pending], failed=True)
    session._messages = [live]
    before = await session.get_session_context(5000)
    request_budget = before["estimatedTokens"]
    updated = deepcopy(original)
    updated["version"] = 2
    updated["fields"]["current_state"] = "new verified progress " * 150
    _persist_canonical_work_item(fs, updated)
    stored_before = dict(fs.files)

    insufficient = await session.get_session_context(request_budget)
    assert insufficient["status"] == "budget_insufficient"
    assert insufficient["requiredTokens"] > request_budget
    assert "checkpoint" not in insufficient
    assert fs.files == stored_before
    recovered = await session.get_session_context(10000)
    assert recovered["status"] == "ready"
    assert recovered["messages"] == [pending.to_dict(), live.to_dict()]
    assert recovered["checkpoint"]["work_items"][0]["version"] == 2
    assert updated["fields"]["current_state"] in recovered["latest_archive_overview"]


@pytest.mark.asyncio
async def test_projection_growth_with_unindexed_eviction_replays_all_raw_history(monkeypatch):
    from copy import deepcopy

    session, fs = session_with_fs()
    original_items = [item(name) for name in ("A", "B", "C")]
    for state in original_items:
        _persist_canonical_work_item(fs, state)
    covered = message("covered", "initial instructions for A, B and C")
    residual = message("residual", "still-unassigned constraint")
    published = _checkpoint_with_items(original_items)
    published["residual"] = [residual.to_dict()]
    archive(fs, 1, [covered, residual], published)
    pending = message("pending", "new raw task detail")
    live = message("live", "latest user request")
    archive(fs, 2, [pending])
    session._messages = [live]
    index_check = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "openviking.session.memory.work_item_index.work_item_index_ready", index_check
    )
    before = await session.get_session_context(10000)
    assert before["status"] == "ready"
    assert len(before["checkpoint"]["active_work_items"]) == 3
    index_check.assert_not_awaited()

    for state in original_items:
        updated = deepcopy(state)
        updated["version"] = 2
        # Each updated item is legal on its own, but their combined projection
        # cannot fit. Eviction is forbidden while its vector version is pending.
        updated["fields"]["current_state"] = "进" * 740
        _persist_canonical_work_item(fs, updated)
    stored_before = dict(fs.files)
    refreshed_archive = await session.get_session_archive("archive_001")
    assert refreshed_archive["status"] == "not_ready"
    result = await session.get_session_context(10000)
    assert result["status"] == "not_ready"
    assert result["checkpoint"] is None
    assert result["latest_archive_overview"] == ""
    assert result["messages"] == [value.to_dict() for value in (covered, residual, pending, live)]
    assert index_check.await_count >= 2
    assert fs.files == stored_before


@pytest.mark.asyncio
async def test_residual_summary_publishes_and_carries_forward_with_auditable_sources():
    session, fs = session_with_fs()
    source = message("long-message", "background details " * 2000)
    uri = archive(fs, 1, [source])
    fs.files[f"{uri}/.meta.json"] = json.dumps(
        {
            "continuation_coverage": [
                {
                    "source_message_ids": [source.id],
                    "summary": "User still requires a written answer; do not deploy.",
                    "reason": "",
                }
            ]
        }
    )
    published = await session._prepare_work_item_checkpoint(uri, [source], {})
    assert published["compact_ready"]
    assert published["coverage"][0]["summary"].endswith("do not deploy.")
    assert published["residual"][0]["role"] == "assistant"
    await session._write_done_file(uri, source.id, source.id, checkpoint=published)
    previous, uncovered = await session._work_item_history()
    assert uncovered == []
    carried = [Message.from_dict(value) for value in previous["residual"]]
    next_uri = archive(fs, 2, [message("next", "Continue")])
    next_checkpoint = await session._prepare_work_item_checkpoint(next_uri, carried, previous)
    assert "do not deploy" in wi.residual_text(next_checkpoint["residual"])
    assert fs.files[f"{uri}/messages.jsonl"] == source.to_jsonl()


@pytest.mark.asyncio
async def test_overflow_keeps_coverage_diagnostics_without_publishing():
    session, fs = session_with_fs()
    source = message("unassigned-long", "unclassified constraint " * 2000)
    uri = archive(fs, 1, [source])
    with pytest.raises(ValueError, match="residual_tokens=.*message_ids=.*unassigned-long"):
        await session._prepare_work_item_checkpoint(uri, [source], {})
    meta = json.loads(fs.files[f"{uri}/.meta.json"])
    assert meta["residual_tokens"] > meta["residual_token_budget"]
    assert meta["coverage"][0]["residual_uri"] == f"{uri}/.done"
    assert f"{uri}/.done" not in fs.files
    assert fs.files[f"{uri}/messages.jsonl"] == source.to_jsonl()


@pytest.mark.asyncio
async def test_long_partial_tools_can_publish_without_copying_raw_outputs_into_residual():
    from openviking.session.memory.memory_updater import ExtractContext
    from openviking.session.memory.session_extract_context_provider import (
        SessionExtractContextProvider,
    )

    session, fs = session_with_fs()
    messages = [message(f"text-{i}", "Investigate the checkpoint failure") for i in range(9)]
    messages.extend(
        Message(
            id=f"tool-{i}",
            role="assistant",
            parts=[
                ToolPart(
                    tool_name="bash",
                    tool_output="diagnostic output " * 1000,
                    tool_output_ref=f"{SESSION_URI}/tool-results/{i}",
                )
            ],
        )
        for i in range(22)
    )
    uri = archive(fs, 1, messages)
    transcript = fs.files[f"{uri}/messages.jsonl"]
    provider = SessionExtractContextProvider(messages)
    provider._build_work_item_tool_evidence()
    assert len(provider.work_item_partial_tool_message_ids) == 22
    summary = "Checkpoint investigation pending; verify full results at " + uri + "/messages.jsonl"
    classified = wi.resolve_continuation_coverage(
        ExtractContext(messages), [{"ranges": "0-30", "summary": summary}]
    )
    fs.files[f"{uri}/.meta.json"] = json.dumps({"continuation_coverage": classified})

    published = await session._prepare_work_item_checkpoint(uri, messages, {})
    await session._write_done_file(uri, messages[0].id, messages[-1].id, checkpoint=published)

    assert published["compact_ready"]
    assert len(published["coverage"]) == 31
    assert all(entry["summary"] == summary for entry in published["coverage"])
    assert len(published["residual"]) == 1
    assert estimate_text_tokens(wi.residual_text(published["residual"])) <= 1000
    assert estimate_text_tokens(fs.files[f"{uri}/.overview.md"]) <= 3000
    assert fs.files[f"{uri}/messages.jsonl"] == transcript


@pytest.mark.asyncio
async def test_replay_preserves_batch_boundary_after_limit_changes():
    from openviking.session.extraction_batch import ExtractionBatchLimits

    session, _ = session_with_fs()
    session._viking_fs = None
    messages = [message(str(index)) for index in range(4)]
    calls = []

    async def extract(batch):
        calls.append([value.id for value in batch])
        return []

    async def record(name, step, batch, operation):
        return await operation()

    await session._extract_long_term_memories_with_batching(
        messages=messages,
        limits=ExtractionBatchLimits(max_messages=1),
        archive_uri="archive",
        extract_batch=extract,
        record_batch=record,
        replay_message_ids=[["0", "1"]],
    )
    assert calls == [["0", "1"], ["2"], ["3"]]


@pytest.mark.asyncio
async def test_next_archive_inherits_partial_write_plan_and_success_receipts():
    session, fs = session_with_fs()
    original = archive(fs, 1, [message("1"), message("2")], failed=True)
    new = archive(fs, 2, [message("3")])
    plan = {"extraction_id": "wi-replay-example", "message_ids": ["2"], "operations": []}
    receipt = {"uri": item("A")["uri"], "source_message_ids": ["1"]}
    fs.files[f"{original}/.meta.json"] = json.dumps(
        {
            "completed_memory_steps": {"long_term": ["1"]},
            "work_item_coverage": [receipt],
            "work_item_replays": [plan],
        }
    )
    completed = {}
    await session._inherit_work_item_progress(new, {}, completed)
    await session._inherit_work_item_progress(new, {}, completed)
    meta = json.loads(fs.files[f"{new}/.meta.json"])
    assert completed == {"long_term": {"1"}}
    assert meta["work_item_coverage"] == [receipt]
    assert meta["work_item_replays"] == [plan]
