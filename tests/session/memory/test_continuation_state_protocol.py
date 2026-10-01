# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Both extraction languages preserve stable continuation state transitions."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from openviking.message import Message, TextPart
from openviking.prompts.manager import PromptManager
from openviking.server.identity import RequestContext, Role
from openviking.session import work_items as wi
from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.extraction_output_protocol import (
    ExtractionOutputContext,
    create_extraction_output_protocol,
)
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import ExtractContext
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.schema_model_generator import (
    ContinuationCoverage,
    SchemaModelGenerator,
)
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def context():
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory" / "work_item.yaml")
    )
    schema = registry.get("work_item")
    config = SimpleNamespace(memory=SimpleNamespace(link_enabled=False))
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=config):
        model = SchemaModelGenerator([schema]).create_structured_operations_model()
    return ExtractionOutputContext(
        operations_model=model,
        schemas=(schema,),
        page_id_map=PageIdMap(),
        read_file_contents={},
        link_enabled=False,
    )


def _call(fields):
    return "sdk.continuation(" + ", ".join(f"{k}={v!r}" for k, v in fields.items()) + ")"


@pytest.mark.parametrize("protocol_name", ["json", "python"])
@pytest.mark.parametrize(
    "entry",
    [
        {"action": "create", "ranges": "0", "summary": "Await test results."},
        {
            "action": "update",
            "continuation_id": "c-test",
            "ranges": "1",
            "summary": "Tests passed; update the documentation next.",
        },
        {"action": "keep", "continuation_id": "c-approval"},
        {
            "action": "resolve",
            "continuation_id": "c-test",
            "ranges": "2",
            "reason": "The requested test results were delivered.",
        },
        {"ranges": "0", "summary": "Legacy continuation remains supported."},
        {"ranges": "0", "reason": "Legacy archive-only classification."},
    ],
)
def test_state_and_legacy_actions_parse_in_both_languages(context, protocol_name, entry):
    protocol = create_extraction_output_protocol(protocol_name)
    source = (
        json.dumps({"continuation_coverage": [entry]}) if protocol_name == "json" else _call(entry)
    )

    operations, error = protocol.parse(source, context)

    assert error is None
    assert len(operations.continuation_coverage) == 1
    actual = operations.continuation_coverage[0].model_dump()
    assert all(actual[key] == value for key, value in entry.items())
    assert not operations.is_empty()


@pytest.mark.parametrize(
    "entry",
    [
        {"action": "create", "ranges": "0", "summary": "New", "continuation_id": "invented"},
        {"action": "create", "summary": "No source"},
        {"action": "update", "ranges": "1", "summary": "Missing stable ID"},
        {"action": "update", "continuation_id": "c-test", "summary": "No new evidence"},
        {"action": "keep", "continuation_id": "c-test", "summary": "Changed by keep"},
        {"action": "resolve", "continuation_id": "c-test", "ranges": "1"},
        {"action": "resolve", "continuation_id": "c-test", "reason": "No evidence"},
        {
            "action": "promote",
            "continuation_id": "c-test",
            "ranges": "1",
            "reason": "No target",
        },
        {"action": "keep", "continuation_id": "c-test", "work_item_page_id": 100},
        {"action": "discard", "continuation_id": "c-test"},
        {"ranges": "0", "summary": "Legacy cannot smuggle an ID", "continuation_id": "c-test"},
    ],
)
def test_invalid_state_transitions_fail_shared_schema_validation(entry):
    with pytest.raises(ValidationError):
        ContinuationCoverage.model_validate(entry)


@pytest.mark.parametrize("existing", [False, True])
def test_promotion_binds_new_and_existing_work_items_equivalently_to_json(context, existing):
    python = create_extraction_output_protocol("python")
    fields = {field.name: "" for field in context.schemas[0].fields if field.name != "work_item_id"}
    fields.update(title="Verify rollout", goal="Complete verification", status="open", ranges="0-1")
    if existing:
        uri = "viking://user/alice/memories/work_item/wi-existing.md"
        context.read_file_contents[uri] = MemoryFile(
            uri=uri, memory_type="work_item", extra_fields={"work_item_id": "wi-existing", **fields}
        )
        page_id = context.page_id_map.get_page_id(uri)
        python.render_new_bindings(context, source="read")
        task = python.binding_name(uri)
        prefix = f"{task}.update(ranges='0-1')\n"
    else:
        page_id = 100
        task = "task"
        arguments = ", ".join(f"{key}={value!r}" for key, value in fields.items())
        prefix = f"task = sdk.create_work_item({arguments})\n"
    promotion = {
        "action": "promote",
        "continuation_id": "c-rollout",
        "ranges": "1",
        "reason": "The task now preserves the pending verification.",
    }
    call = _call(promotion)[:-1] + f", work_item={task})"

    operations, error = python.parse(prefix + call, context)
    json_operations, json_error = create_extraction_output_protocol("json").parse(
        json.dumps({"continuation_coverage": [{**promotion, "work_item_page_id": page_id}]}),
        context,
    )

    assert error is None
    assert json_error is None
    assert operations.continuation_coverage == json_operations.continuation_coverage
    assert operations.work_item[0].page_id == page_id


@pytest.mark.parametrize("target", ["'wi-made-up'", "100", "None"])
def test_python_promotion_rejects_unbound_targets(context, target):
    operations, error = create_extraction_output_protocol("python").parse(
        "sdk.continuation(action='promote', continuation_id='c-test', ranges='1', "
        f"reason='Move into task', work_item={target})",
        context,
    )

    assert operations is None
    assert "requires a live work_item binding" in error


def test_python_rejects_target_deleted_after_promotion(context):
    python = create_extraction_output_protocol("python")
    uri = "viking://user/alice/memories/work_item/wi-existing.md"
    context.read_file_contents[uri] = MemoryFile(uri=uri, memory_type="work_item")
    context.page_id_map.get_page_id(uri)
    python.render_new_bindings(context, source="read")
    task = python.binding_name(uri)

    operations, error = python.parse(
        "sdk.continuation(action='promote', continuation_id='c-test', ranges='1', "
        f"reason='Move into task', work_item={task})\n{task}.delete()",
        context,
    )

    assert operations is None
    assert "cannot target a deleted work_item" in error


def test_contract_describes_stable_items_and_safe_promotion(context):
    for name in ("json", "python"):
        contract = create_extraction_output_protocol(name).render_contract(context)
        assert "continuation_id" in contract
        assert "Omission is not resolution" in contract
        assert "successfully saved" in contract
        assert "keep" in contract
        assert "resolve" in contract
        assert "promote" in contract
        assert "Never recreate an existing item" in contract
        assert "Never use resolve plus create" in contract
        assert "changed next action within the same matter requires update" in contract
        final_instruction = create_extraction_output_protocol(name).render_final_instruction(
            context
        )
        assert "never resolve plus create" in final_instruction


@pytest.mark.parametrize(
    "entry",
    [
        {"action": "update", "continuation_id": "c-test", "ranges": "1"},
        {"action": "resolve", "continuation_id": "c-test", "reason": "No evidence"},
        {"action": "create", "ranges": "0", "summary": "New", "continuation_id": "invented"},
        {"action": "resolve", "ranges": "0", "reason": "Missing stable ID"},
        {"action": "discard", "continuation_id": "c-test", "ranges": "0", "reason": "Unknown"},
        {"continuation_id": "c-test", "ranges": "0", "reason": "Missing action"},
    ],
)
def test_json_invalid_actions_request_repair_instead_of_being_silently_filtered(context, entry):
    operations, error = create_extraction_output_protocol("json").parse(
        json.dumps(
            {"continuation_coverage": [{"action": "keep", "continuation_id": "c-other"}, entry]}
        ),
        context,
    )

    assert operations is None
    assert "Invalid continuation_coverage[1] action" in error


def test_json_state_validation_keeps_json_repair_and_legacy_list_tolerance(context):
    protocol = create_extraction_output_protocol("json")
    # A fenced response with a trailing comma remains accepted; only the invalid
    # legacy item is filtered. A valid state action must survive that fallback.
    source = """```json
    {"continuation_coverage": [
      {"ranges": {}, "summary": "Invalid legacy field"},
      {"action": "keep", "continuation_id": "c-test"},
    ]}
    ```"""

    operations, error = protocol.parse(source, context)

    assert error is None
    assert len(operations.continuation_coverage) == 1
    assert operations.continuation_coverage[0].action == "keep"
    assert operations.continuation_coverage[0].continuation_id == "c-test"


def test_json_empty_response_still_uses_existing_empty_operation_tolerance(context):
    operations, error = create_extraction_output_protocol("json").parse("[]", context)

    assert error is None
    assert operations.is_empty()


def _long_checkpoint_context():
    return ExtractContext(
        [
            Message(
                id="c-long",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("A still-pending verification with important details. " * 1000)],
            ),
            Message(id="new-result", role="user", parts=[TextPart("Verification passed.")]),
        ]
    )


def test_long_checkpoint_keeps_its_stable_id_after_extraction_chunking():
    extraction = _long_checkpoint_context()
    assert len(extraction.messages) > 2

    actions = wi.resolve_continuation_coverage(
        extraction,
        [
            {
                "action": "update",
                "continuation_id": "c-long",
                "ranges": str(len(extraction.messages) - 1),
                "summary": "Verification passed; publishing results remains pending.",
            }
        ],
    )

    assert actions[0]["continuation_id"] == "c-long"
    assert actions[0]["source_message_ids"] == ["new-result"]


def test_checkpoint_chunks_alone_cannot_supply_new_resolution_evidence():
    extraction = _long_checkpoint_context()

    with pytest.raises(ValueError, match="current non-checkpoint evidence"):
        wi.resolve_continuation_coverage(
            extraction,
            [
                {
                    "action": "resolve",
                    "continuation_id": "c-long",
                    "ranges": f"0-{len(extraction.messages) - 2}",
                    "reason": "Treating the old summary as a new result is unsafe.",
                }
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("page_ids", [[100, 100], [7]])
async def test_json_promotion_uses_unambiguous_normalized_task_page_ids(context, page_ids):
    extraction = ExtractContext(
        [
            Message(
                id="c-test",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("Keep the verification pending.")],
            ),
            Message(id="new", role="user", parts=[TextPart("Track verification as a task.")]),
        ]
    )
    fields = {field.name: "" for field in context.schemas[0].fields if field.name != "work_item_id"}
    fields.update(goal="Complete verification", status="open", ranges="0-1")
    payload = {
        "work_item": [
            {**fields, "page_id": identity, "title": f"Task {index}", "scope": f"Scope {index}"}
            for index, identity in enumerate(page_ids)
        ],
        "continuation_coverage": [
            {
                "action": "promote",
                "continuation_id": "c-test",
                "ranges": "1",
                "reason": "Tracked in the saved task",
                "work_item_page_id": page_ids[0],
            }
        ],
    }
    operations, error = create_extraction_output_protocol("json").parse(
        json.dumps(payload), context
    )
    assert error is None
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    isolation = MemoryIsolationHandler(ctx, extraction)
    isolation.prepare_messages()
    provider = SimpleNamespace(
        get_memory_schemas=lambda ctx: list(context.schemas),
        read_file_contents={},
        work_item_namespace="normalization-test",
    )
    loop = ExtractLoop(
        vlm=MagicMock(),
        viking_fs=MagicMock(),
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
    )
    loop._extract_context = extraction

    if len(page_ids) > 1:
        with pytest.raises(ValueError, match="Ambiguous or invalid work_item promotion target"):
            await loop.resolve_operations(operations)
    else:
        resolved, _ = await loop.resolve_operations(operations)
        assert resolved.upsert_operations[0].page_id >= 100
        assert (
            resolved.continuation_coverage[0]["work_item_uri"]
            == resolved.upsert_operations[0].uris[0]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_extract_loop_repairs_invalid_continuation_resolution_once(
    context, monkeypatch, repair_succeeds
):
    extraction = ExtractContext(
        [
            Message(
                id="c-test",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("Keep the verification pending.")],
            ),
            Message(id="new", role="user", parts=[TextPart("Track verification as a task.")]),
        ]
    )
    fields = {field.name: "" for field in context.schemas[0].fields if field.name != "work_item_id"}
    fields.update(title="Verify rollout", goal="Complete verification", status="open", ranges="1")
    arguments = ", ".join(f"{key}={value!r}" for key, value in fields.items())
    broken = (
        f"task = sdk.create_work_item({arguments})\n"
        "sdk.continuation(action='promote', continuation_id='c-test', ranges='1', "
        "reason='Tracked in the saved task', work_item=task)\nsdk.commit()"
    )
    fixed = broken.replace("ranges='1'", "ranges='0-1'", 1)

    class ScriptedVLM:
        model = "continuation-repair-test"

        def __init__(self):
            self.calls = []

        async def get_completion_async(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            assert len(self.calls) <= 2, "A semantic continuation failure gets at most one repair"
            return fixed if repair_succeeds and len(self.calls) == 2 else broken

    config = SimpleNamespace(
        memory=SimpleNamespace(link_enabled=False, extraction_output_format="python")
    )
    for module in (
        "openviking.session.memory.extract_loop",
        "openviking_cli.utils.config",
        "openviking.session.work_item_budget",
    ):
        monkeypatch.setattr(f"{module}.get_openviking_config", lambda: config)
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    isolation = MemoryIsolationHandler(ctx, extraction)
    isolation.prepare_messages()
    provider = SimpleNamespace(
        get_memory_schemas=lambda ctx: list(context.schemas),
        get_output_language=lambda: "en",
        get_tools=lambda: [],
        get_extract_context=lambda: extraction,
        instruction=lambda: "Maintain the supplied continuation items using current evidence.",
        prefetch=AsyncMock(return_value=[]),
        execute_tool=AsyncMock(return_value={"error": "not found"}),
        read_file_contents={},
        work_item_namespace="continuation-repair-test",
    )
    storage = SimpleNamespace(write_file=AsyncMock())
    model = ScriptedVLM()
    loop = ExtractLoop(
        vlm=model,
        viking_fs=storage,
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
        max_iterations=1,
    )

    if repair_succeeds:
        resolved, _ = await loop.run()
        assert len(resolved.upsert_operations) == 1
        assert set(resolved.upsert_operations[0].source_message_ids) == {"c-test", "new"}
        assert (
            resolved.continuation_coverage[0]["work_item_uri"]
            == resolved.upsert_operations[0].uris[0]
        )
    else:
        from openviking.session.continuation_state import ContinuationResolutionError

        with pytest.raises(ContinuationResolutionError, match="include the complete continuation"):
            await loop.run()

    assert len(model.calls) == 2
    guidance = "\n".join(message["content"] for message in model.calls[1]["messages"])
    assert "complete continuation" in guidance
    assert "ranges" in guidance
    storage.write_file.assert_not_awaited()


def test_created_continuation_identity_survives_failed_archive_inheritance():
    prefix = "viking://user/alice/sessions/state-test/history/"
    messages = [
        Message(id="m1", role="user", parts=[TextPart("Investigate the issue.")]),
        Message(id="m2", role="user", parts=[TextPart("The initial investigation is complete.")]),
    ]
    create = {
        "action": "create",
        "continuation_id": "",
        "source_message_ids": ["m1"],
        "summary": "Issue investigation pending.",
        "reason": "",
    }
    original, _ = wi.coverage_report(messages[:1], [], prefix + "archive_001", [create])
    identity = original[0]["id"]
    update = {
        "action": "update",
        "continuation_id": identity,
        "source_message_ids": ["m2"],
        "summary": "Investigation complete; fix still pending.",
        "reason": "",
    }

    inherited, _ = wi.coverage_report(messages, [], prefix + "archive_002", [create, update])

    assert len(inherited) == 1
    assert inherited[0]["id"] == identity
    assert "fix still pending" in wi.continuation_content(inherited[0])


@pytest.mark.parametrize("uses_latest_background", [True, False])
def test_later_batch_promotion_checks_current_item_state_not_historical_raw_ids(
    uses_latest_background,
):
    from openviking.session.continuation_state import confirm_continuation_promotions

    archive = "viking://user/alice/sessions/state-test/history/archive_002"
    old = wi.continuation_message(
        "Issue investigation pending.",
        archive.replace("002", "001"),
        ["origin"],
        None,
        continuation_id="c-investigation",
    )
    first = Message(
        id="batch-one", role="user", parts=[TextPart("Investigation finished; fix pending.")]
    )
    second = Message(
        id="batch-two", role="user", parts=[TextPart("Track this fix as a work item.")]
    )
    first_messages = [Message.from_dict(old), first]
    update = wi.resolve_continuation_coverage(
        ExtractContext(first_messages),
        [
            {
                "action": "update",
                "continuation_id": old["id"],
                "ranges": "1",
                "summary": "Investigation complete; fix still pending.",
            }
        ],
    )[0]
    current, _ = wi.coverage_report(first_messages, [], archive, [update], previous_residual=[old])
    # The injected checkpoint carries its stable ID and latest body, not the
    # raw messages from earlier batches. A task consuming it need not reread B1.
    background = current[0] if uses_latest_background else old
    extraction = ExtractContext([Message.from_dict(background), second])
    task = ResolvedOperation(
        memory_type="work_item",
        memory_fields={"ranges": "0-1"},
        page_id=100,
        uris=["viking://user/alice/memories/work_item/wi-fix.md"],
        source_message_ids=[old["id"], second.id],
    )
    promotion = wi.resolve_continuation_coverage(
        extraction,
        [
            {
                "action": "promote",
                "continuation_id": old["id"],
                "ranges": "1",
                "reason": "Now tracked as a durable task.",
                "work_item_page_id": 100,
            }
        ],
        work_item_operations=[task],
    )
    receipt = [{"uri": task.uris[0], "source_message_ids": task.source_message_ids}]
    promotion = confirm_continuation_promotions(promotion, receipt)
    assert promotion[0]["promotion_receipt"]

    final, _ = wi.coverage_report(
        [*first_messages, second], receipt, archive, [update, *promotion], previous_residual=[old]
    )

    if uses_latest_background:
        assert final == []
    else:
        assert [value["id"] for value in final] == [old["id"]]
        assert "fix still pending" in wi.continuation_content(final[0])
