# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Default Python extraction must create durable tasks without model-assigned IDs.

The VLM is scripted and vector/search services are isolated; the extraction loop,
protocol parser, resolver, read tool, and canonical updater all run normally.
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart
from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.work_item import new_work_item_id
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.memory_config import MemoryConfig


class LocalMemoryFiles:
    """Minimal filesystem adapter that writes actual Markdown files under tmp_path."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, uri: str) -> Path:
        assert uri.startswith("viking://user/alice/memories/work_item/")
        return self.root / uri.removeprefix("viking://")

    async def read_file(self, uri: str, **kwargs):
        return self.path(uri).read_text(encoding="utf-8")

    async def write_file(self, uri: str, content: str, **kwargs):
        path = self.path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ScriptedVLM:
    model = "scripted-work-item-dsl"

    def __init__(self, program: str):
        self.program = program
        self.calls = []

    async def get_completion_async(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.program


def create_program(count: int) -> str:
    return (
        "\n".join(
            f"""sdk.create_work_item(
    title='Fix import', scope='repository-{index}', goal='Import completes without errors',
    status='open', current_state='Failure reproduced', next_action='Fix parser',
    constraints='Keep the public API unchanged', waiting_for='', decisions='', refs='',
    ranges='{index}', reopen_reason='',
)"""
            for index in range(count)
        )
        + "\nsdk.commit()"
    )


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    config = SimpleNamespace(memory=MemoryConfig(link_enabled=False, eager_prefetch=True))
    # Deliberately use MemoryConfig's real default, rather than forcing Python in
    # the loop or constructing resolved operations ahead of the protocol boundary.
    assert config.memory.extraction_output_format == "python"
    for module in (
        "openviking_cli.utils.config",
        "openviking.session.memory.extract_loop",
        "openviking.session.memory.session_extract_context_provider",
        "openviking.session.memory.utils.language",
        "openviking.session.work_item_budget",
    ):
        monkeypatch.setattr(f"{module}.get_openviking_config", lambda: config)
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory" / "work_item.yaml")
    )
    fs = LocalMemoryFiles(tmp_path)
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    updater = MemoryUpdater(registry=registry)
    updater._viking_fs = fs
    # These services run after canonical writes and are not part of the DSL bug.
    monkeypatch.setattr(updater, "_sync_resource_refs_for_result", AsyncMock())
    monkeypatch.setattr(updater, "_vectorize_memories", AsyncMock())
    monkeypatch.setattr(updater, "generate_overview", AsyncMock())

    async def extract_and_write(program, messages, *, existing=()):
        provider = SessionExtractContextProvider(
            messages,
            ctx=ctx,
            viking_fs=fs,
            memory_registry=registry,
            work_item_uris=list(existing),
            work_item_namespace="pipeline-session",
        )
        # No semantic-search backend is needed; exact existing-item reads remain real.
        monkeypatch.setattr(provider, "search_files", AsyncMock(return_value=[]))
        await provider.prepare_extraction_messages()
        context = provider.get_extract_context()
        isolation = MemoryIsolationHandler(ctx, context, allowed_memory_types={"work_item"})
        isolation.prepare_messages()
        provider._isolation_handler = isolation
        vlm = ScriptedVLM(program)
        loop = ExtractLoop(
            vlm=vlm,
            viking_fs=fs,
            ctx=ctx,
            context_provider=provider,
            isolation_handler=isolation,
            max_iterations=1,
        )
        operations, _ = await loop.run()
        assert len(vlm.calls) == 1, "A valid ID-free program must not require format repair"
        assert not operations.errors
        result = await updater.apply_operations(operations, ctx, context, isolation)
        assert not result.errors
        return SimpleNamespace(operations=operations, result=result, context=context, vlm=vlm)

    return SimpleNamespace(fs=fs, run=extract_and_write)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_default_python_creates_disk_backed_work_items_and_updates_stable_identity(
    pipeline, count
):
    messages = [
        Message(
            id=f"task-{index}",
            role="user",
            parts=[TextPart(text=f"Fix the import failure in repository-{index}.")],
        )
        for index in range(count)
    ]
    program = create_program(count)
    assert "work_item_id" not in program
    created = await pipeline.run(program, messages)
    assert len(created.result.written_uris) == count
    assert len(set(created.result.written_uris)) == count
    assert len(list(pipeline.fs.root.rglob("*.md"))) == count
    assert not created.operations.continuation_coverage

    for index, operation in enumerate(created.operations.upsert_operations):
        identity = new_work_item_id(created.context, str(index), index, "pipeline-session")
        uri = f"viking://user/alice/memories/work_item/{identity}.md"
        assert operation.uris == [uri]
        assert operation.source_message_ids == [messages[index].id]
        canonical = MemoryFileUtils.read(pipeline.fs.path(uri).read_text(encoding="utf-8"), uri=uri)
        assert canonical.memory_type == "work_item"
        assert canonical.extra_fields["work_item_id"] == identity
        assert canonical.extra_fields["scope"] == f"repository-{index}"
        assert canonical.extra_fields["version"] == 1
        assert "Import completes without errors" in canonical.content
        assert "Keep the public API unchanged" in canonical.content

    uri = created.result.written_uris[0]
    original = MemoryFileUtils.read(pipeline.fs.path(uri).read_text(encoding="utf-8"), uri=uri)
    update = await pipeline.run(
        "work_item_1.update(status='in_progress', current_state='Parser fix implemented', "
        "next_action='Run import tests', ranges='0')\nsdk.commit()",
        [
            Message(
                id="progress",
                role="user",
                parts=[TextPart(text="The parser fix is implemented; run the import tests next.")],
            )
        ],
        existing=[uri],
    )
    assert update.result.edited_uris == [uri]
    assert update.result.written_uris == []
    updated = MemoryFileUtils.read(pipeline.fs.path(uri).read_text(encoding="utf-8"), uri=uri)
    assert updated.extra_fields["work_item_id"] == original.extra_fields["work_item_id"]
    assert updated.extra_fields["version"] == 2
    assert updated.extra_fields["status"] == "in_progress"
    assert updated.extra_fields["constraints"] == "Keep the public API unchanged"
    assert "Parser fix implemented" in updated.content
    assert "Run import tests" in updated.content
    assert len(list(pipeline.fs.root.rglob("*.md"))) == count
