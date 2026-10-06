"""Host history must survive OV commits and local compaction failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.loop import AgentLoop
from vikingbot.config.schema import AgentsConfig, SessionKey
from vikingbot.session.manager import Session, SessionManager


@pytest.fixture
def local_session(tmp_path):
    manager = SessionManager(tmp_path)
    session = Session(key=SessionKey(type="test", channel_id="channel", chat_id="native"))
    for index in range(6):
        session.add_message("user", f"constraint {index}")
        session.add_message("assistant", f"answer {index}")
    session.metadata["openviking"] = {
        "last_synced_local_index": 11,
        "last_commit_local_index": 9,
    }
    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = manager
    loop.memory_window = 8
    loop.config = SimpleNamespace(ov_server=SimpleNamespace(is_available=lambda: False))
    loop._summarize_compact_chunk = AsyncMock(return_value="Keep constraint 0.")
    loop._merge_compact_summaries = AsyncMock(return_value="Merged host summary.")
    return loop, session


def test_session_context_defaults_off():
    assert AgentsConfig().session_context_enabled is False


@pytest.mark.asyncio
async def test_local_summary_and_raw_backup_survive_reload(local_session):
    loop, session = local_session
    await loop.sessions.save(session)
    original = session.clone()
    assert await loop._compact_local_session(session)
    reloaded = SessionManager(loop.sessions.bot_data_path)._load(session.key)
    assert reloaded.messages == original.messages[-4:]
    assert "constraint 0" in reloaded.get_history()[0]["content"]
    assert reloaded.metadata["openviking"]["last_synced_local_index"] == 3
    assert reloaded.metadata["openviking"]["last_commit_local_index"] == 1
    backups = list(loop.sessions.sessions_dir.glob("history/*/*.jsonl"))
    assert len(backups) == 1
    assert "constraint 0" in backups[0].read_text()
    assert "answer 5" in backups[0].read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "model", "save"])
async def test_compaction_failure_preserves_original_and_previous_summary(
    local_session, failure, monkeypatch
):
    loop, session = local_session
    session.metadata["conversation_summary"] = "Previous summary."
    await loop.sessions.save(session)
    original = session.clone()
    if failure == "empty":
        loop._summarize_compact_chunk.return_value = ""
    elif failure == "model":
        loop._summarize_compact_chunk.side_effect = RuntimeError("model unavailable")
    else:
        monkeypatch.setattr(
            loop.sessions, "_save_unlocked", lambda _: (_ for _ in ()).throw(OSError("disk full"))
        )
    assert not await loop._compact_local_session(session)
    assert session.messages == original.messages
    assert session.metadata == original.metadata
    assert (
        SessionManager(loop.sessions.bot_data_path)._load(session.key).messages == original.messages
    )


@pytest.mark.asyncio
async def test_capture_failure_preserves_unsent_history(local_session):
    loop, session = local_session
    loop.config.ov_server.is_available = lambda: True
    loop._submit_openviking_session = AsyncMock(return_value=False)
    await loop.sessions.save(session)
    original = session.clone()
    assert not await loop._compact_local_session(session)
    assert session.messages == original.messages
    loop._summarize_compact_chunk.assert_not_awaited()


@pytest.mark.asyncio
async def test_compaction_cannot_overwrite_concurrent_messages(local_session):
    loop, session = local_session
    await loop.sessions.save(session)
    original = session.clone()
    session.add_message("user", "new input")
    await loop.sessions.save(session)
    with pytest.raises(RuntimeError, match="Session changed"):
        await loop.sessions.save_compacted(session, original.messages, 8, "summary")
    assert session.messages[-1]["content"] == "new input"


@pytest.mark.asyncio
async def test_disabled_wm_confirmation_uses_local_history_without_context_request(local_session):
    loop, session = local_session
    loop._ov_session_context_enabled = lambda: True
    loop._get_ov_client = AsyncMock(side_effect=AssertionError("must not read OV history"))
    session.metadata["openviking"]["working_memory_confirmed"] = False
    assert len(await loop._build_prompt_history(session)) == 12
    loop._get_ov_client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_tail", [[], [{"role": "user", "content": "constraint 5"}]])
async def test_missing_archive_summary_keeps_local_summary_and_history(
    local_session, retained_tail
):
    loop, session = local_session
    loop._ov_session_context_enabled = lambda: True
    loop._get_ov_client = AsyncMock(
        return_value=SimpleNamespace(
            get_session_context=AsyncMock(
                return_value={
                    "latest_archive_overview": "",
                    "messages": retained_tail,
                    "stats": {"totalArchives": 1},
                }
            )
        )
    )
    session.metadata["conversation_summary"] = "Keep an earlier task constraint."
    session.metadata["openviking"]["working_memory_confirmed"] = True
    history = await loop._build_prompt_history(session)
    assert len(history) == 13
    assert "earlier task constraint" in history[0]["content"]
    assert history[1]["content"] == "constraint 0"
    assert session.metadata["openviking"]["working_memory_confirmed"] is False
