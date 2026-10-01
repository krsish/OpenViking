# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Continuation attribution must preserve unresolved context without reviving work."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openviking.message import Message, TextPart, ToolPart
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
from openviking.session.memory.schema_model_generator import SchemaModelGenerator
from openviking.session.memory.work_item import validate_work_item_update
from openviking.utils.token_estimation import estimate_text_tokens
from openviking_cli.session.user_id import UserIdentifier

ARCHIVE = "viking://user/alice/sessions/test/history/archive_001"
CREATED_AT = "2026-09-30T12:00:00Z"
CONSTRAINT = "Keep the original invoices; do not send email without approval."


def message(identity, text, role="user"):
    return Message(id=identity, role=role, parts=[TextPart(text)], created_at=CREATED_AT)


def all_ranges(context):
    return f"0-{len(context.messages) - 1}"


def output_context(*, work_item=True):
    registry = MemoryTypeRegistry(load_schemas=False)
    memory_type = "work_item" if work_item else "preferences"
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory" / f"{memory_type}.yaml")
    )
    schemas = [registry.get(memory_type)]
    config = SimpleNamespace(memory=SimpleNamespace(link_enabled=False))
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=config):
        operations_model = SchemaModelGenerator(schemas).create_structured_operations_model()
    return ExtractionOutputContext(
        operations_model=operations_model,
        schemas=tuple(schemas),
        page_id_map=PageIdMap(),
        read_file_contents={},
        link_enabled=False,
    )


def test_long_unassigned_conversation_can_publish_after_explicit_continuation_summary():
    raw = message("long-question", "Invoice history. " * 800 + CONSTRAINT)
    context = ExtractContext([raw])
    summaries = wi.resolve_continuation_coverage(
        context, [{"ranges": all_ranges(context), "summary": CONSTRAINT}]
    )

    residual, ledger = wi.coverage_report([raw], [], ARCHIVE, summaries)
    projection, active = wi.build_projection([], residual)

    assert not active
    assert CONSTRAINT in projection
    assert "Invoice history. " * 800 not in projection
    assert estimate_text_tokens(projection) <= wi.PROJECTION_TOKEN_BUDGET
    assert ledger[0]["message_id"] == raw.id
    assert ledger[0]["summary"] == CONSTRAINT
    assert f"Source coverage: {ARCHIVE}/.done" in projection


def test_unselected_long_message_has_an_archive_destination_without_entering_hot_context():
    raw = message("unselected-long", "Historical diagnostic output. " * 30000)

    residual, ledger = wi.coverage_report([raw], [], ARCHIVE)
    projection, _ = wi.build_projection([], residual)

    assert residual == []
    assert ledger[0]["message_id"] == raw.id
    assert ledger[0]["disposition"] == "archive_only"
    assert ledger[0]["archive_uri"] == f"{ARCHIVE}/messages.jsonl"
    assert "Historical diagnostic output" not in projection


@pytest.mark.parametrize(
    "fields",
    [{}, {"reason": " "}, {"summary": " "}, {"summary": "keep", "reason": "drop"}],
)
def test_discard_requires_nonempty_reason_and_cannot_compete_with_summary(fields):
    context = ExtractContext([message("m", CONSTRAINT)])

    with pytest.raises(ValueError, match="exactly one"):
        wi.resolve_continuation_coverage(context, [{"ranges": "0", **fields}])


def test_explicit_discard_is_auditable_and_does_not_drop_previous_constraint():
    messages = [message("hello", "Hello!"), message("constraint", CONSTRAINT)]
    classified = wi.resolve_continuation_coverage(
        ExtractContext(messages),
        [{"ranges": "0", "reason": "Greeting only; no unresolved question or commitment."}],
    )

    residual, ledger = wi.coverage_report(
        messages, [], ARCHIVE, classified, previous_residual=[messages[1].to_dict()]
    )

    assert residual == [messages[1].to_dict()]
    assert ledger[0]["explicitly_dropped"].startswith("Greeting only")
    assert "explicitly_dropped" not in ledger[1]


def test_work_item_attribution_does_not_discard_additional_continuation_from_same_message():
    raw = message("mixed", "Track the invoice task. " + CONSTRAINT)
    summary = wi.resolve_continuation_coverage(
        ExtractContext([raw]), [{"ranges": "0", "summary": CONSTRAINT}]
    )
    item_uri = "viking://user/alice/memories/work_item/wi-invoice.md"

    residual, ledger = wi.coverage_report(
        [raw], [{"uri": item_uri, "source_message_ids": [raw.id]}], ARCHIVE, summary
    )
    projection, _ = wi.build_projection([], residual)

    assert CONSTRAINT in projection
    assert ledger[0]["work_item_uris"] == [item_uri]
    assert ledger[0]["summary"] == CONSTRAINT


@pytest.mark.parametrize(
    "classification",
    [
        {"summary": "Read preview received; verify the full result at tool-results/result."},
        {"reason": "Intermediate read; the subsequent answer resolved the request."},
    ],
)
def test_partial_tool_result_can_be_classified_by_summary_or_reason(classification):
    raw = Message(
        id="partial-tool",
        role="assistant",
        created_at=CREATED_AT,
        parts=[
            ToolPart(
                tool_name="read",
                tool_output="A bounded preview",
                tool_output_ref="viking://user/alice/sessions/test/tool-results/result",
                tool_output_truncated=True,
            )
        ],
    )
    classified = wi.resolve_continuation_coverage(
        ExtractContext([raw]),
        [{"ranges": "0", **classification}],
    )

    residual, ledger = wi.coverage_report([raw], [], ARCHIVE, classified)

    assert classified[0]["source_message_ids"] == [raw.id]
    if "summary" in classification:
        projection, _ = wi.build_projection([], residual)
        assert classification["summary"] in projection
        assert ledger[0]["summary"] == classification["summary"]
    else:
        assert residual == []
        assert ledger[0]["explicitly_dropped"] == classification["reason"]


def test_partial_chunks_do_not_falsely_claim_full_source_summary_coverage():
    raw = message("chunked", "An unresolved detail remains. " * 500 + CONSTRAINT)
    context = ExtractContext([raw])
    assert len(context.messages) > 1

    partial = wi.resolve_continuation_coverage(context, [{"ranges": "0", "summary": "One detail."}])
    residual, ledger = wi.coverage_report([raw], [], ARCHIVE, partial)

    assert partial == []
    assert residual == []
    assert ledger[0]["disposition"] == "archive_only"
    assert "summary" not in ledger[0]
    complete = wi.resolve_continuation_coverage(
        context, [{"ranges": all_ranges(context), "summary": CONSTRAINT}]
    )
    assert complete[0]["source_message_ids"] == [raw.id]


def test_ranges_covering_one_message_and_part_of_next_only_attribute_complete_source():
    short = message("short", "Wait for invoice approval.")
    long = message("long", "Still needed detail. " * 500 + CONSTRAINT)
    context = ExtractContext([short, long])
    assert len(context.messages) > 2
    classified = wi.resolve_continuation_coverage(
        context, [{"ranges": "0-1", "summary": "Wait for invoice approval."}]
    )

    residual, ledger = wi.coverage_report([short, long], [], ARCHIVE, classified)

    assert classified[0]["source_message_ids"] == [short.id]
    assert residual[0]["role"] == "assistant"
    assert len(residual) == 1
    assert "summary" in ledger[0]
    assert "summary" not in ledger[1]
    assert ledger[1]["disposition"] == "archive_only"


def test_overlapping_classifications_are_rejected_before_coverage_changes():
    context = ExtractContext([message("m", CONSTRAINT)])

    with pytest.raises(ValueError, match="overlap"):
        wi.resolve_continuation_coverage(
            context,
            [{"ranges": "0", "summary": CONSTRAINT}, {"ranges": "0", "reason": "Done"}],
        )


@pytest.mark.parametrize("protocol_name", ["json", "python"])
@pytest.mark.parametrize("field, value", [("summary", CONSTRAINT), ("reason", "Greeting only.")])
def test_both_protocols_preserve_continuation_without_memory_writes(protocol_name, field, value):
    context = output_context()
    protocol = create_extraction_output_protocol(protocol_name)
    protocol.render_contract(context)
    assert "continuation" in protocol.render_final_instruction(context)
    payload = (
        json.dumps({"continuation_coverage": [{"ranges": "0", field: value}]})
        if protocol_name == "json"
        else f"sdk.continuation(ranges='0', {field}={value!r})\nsdk.commit()"
    )

    operations, error = protocol.parse(payload, context)

    assert error is None
    assert operations.work_item == []
    assert not operations.is_empty()
    classified = wi.resolve_continuation_coverage(
        ExtractContext([message("source", value)]), operations.continuation_coverage
    )
    assert classified[0][field] == value
    assert classified[0]["source_message_ids"] == ["source"]


@pytest.mark.parametrize("protocol_name", ["json", "python"])
def test_continuation_cannot_change_legacy_extraction(protocol_name):
    context = output_context(work_item=False)
    assert "continuation_coverage" not in context.operations_model.model_fields
    protocol = create_extraction_output_protocol(protocol_name)
    contract = protocol.render_contract(context)
    assert "sdk.continuation" not in contract
    payload = (
        '{"continuation_coverage": [{"ranges": "0", "reason": "Greeting only."}]}'
        if protocol_name == "json"
        else "sdk.continuation(ranges='0', reason='Greeting only.')\nsdk.commit()"
    )

    operations, error = protocol.parse(payload, context)

    if protocol_name == "python":
        assert operations is None
        assert "only with work_item" in error
    else:
        # Legacy JSON deliberately ignores unknown fields; no new coverage may result.
        assert error is None
        assert operations.is_empty()
        assert not hasattr(operations, "continuation_coverage")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_name", ["json", "python"])
@pytest.mark.parametrize("partial", [False, True])
async def test_operation_resolution_preserves_continuation_without_writes(protocol_name, partial):
    context = output_context()
    protocol = create_extraction_output_protocol(protocol_name)
    payload = (
        json.dumps({"continuation_coverage": [{"ranges": "0", "summary": CONSTRAINT}]})
        if protocol_name == "json"
        else f"sdk.continuation(ranges='0', summary={CONSTRAINT!r})\nsdk.commit()"
    )
    operations, error = protocol.parse(payload, context)
    assert error is None
    source = Message(
        id="source",
        role="assistant",
        parts=[ToolPart(tool_name="read", tool_output=CONSTRAINT)],
        created_at=CREATED_AT,
    )
    extract_context = ExtractContext([source])
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)
    isolation = MemoryIsolationHandler(ctx, extract_context)
    isolation.prepare_messages()
    provider = SimpleNamespace(
        get_memory_schemas=lambda ctx: list(context.schemas),
        read_file_contents={},
        work_item_namespace="session",
        work_item_partial_tool_message_ids={source.id} if partial else set(),
    )
    loop = ExtractLoop(
        vlm=MagicMock(),
        viking_fs=MagicMock(),
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
    )
    loop._extract_context = extract_context

    resolved, _ = await loop.resolve_operations(operations)

    assert resolved.upsert_operations == []
    assert resolved.errors == []
    assert resolved.continuation_coverage == [
        {"source_message_ids": [source.id], "summary": CONSTRAINT, "reason": ""}
    ]


def test_summary_survives_next_round_as_background_context_with_constraints():
    raw = message("source", CONSTRAINT)
    classified = wi.resolve_continuation_coverage(
        ExtractContext([raw]), [{"ranges": "0", "summary": CONSTRAINT}]
    )
    previous, _ = wi.coverage_report([raw], [], ARCHIVE, classified)
    inherited = Message.from_dict(previous[0])
    new_message = message("new", "Continue checking the invoice.")

    residual, ledger = wi.coverage_report(
        [inherited, new_message], [], ARCHIVE + "-next", previous_residual=previous
    )
    projection, _ = wi.build_projection([], residual)

    assert inherited.role == "assistant"
    assert inherited.id != raw.id
    assert inherited.message_kind == "checkpoint"
    assert inherited.source_message_ids == [raw.id]
    assert inherited.created_at == raw.created_at
    assert CONSTRAINT in projection
    assert "Continue checking the invoice." not in projection
    assert ledger[1]["disposition"] == "archive_only"
    assert f"Source coverage: {ARCHIVE}/.done" in projection


def test_shared_summary_is_emitted_once_and_all_source_ids_remain_auditable():
    messages = [message("one", "Keep original invoices."), message("two", CONSTRAINT)]
    classified = wi.resolve_continuation_coverage(
        ExtractContext(messages), [{"ranges": "0-1", "summary": CONSTRAINT}]
    )

    residual, ledger = wi.coverage_report(messages, [], ARCHIVE, classified)
    restored = [Message.from_dict(value) for value in residual]
    next_residual, _ = wi.coverage_report(
        restored, [], ARCHIVE + "-next", previous_residual=residual
    )

    assert len(residual) == 1
    assert [row["message_id"] for row in ledger] == ["one", "two"]
    assert all(row["summary"] == CONSTRAINT for row in ledger)
    assert next_residual == residual
    assert f"Source coverage: {ARCHIVE}/.done" in restored[0].content
    assert restored[0].role == "assistant"


def test_omitted_previous_continuations_sort_before_new_transcript_summaries():
    previous = [
        wi.continuation_message("First old pending action.", ARCHIVE, ["old-1"], CREATED_AT),
        wi.continuation_message("Second old pending action.", ARCHIVE, ["old-2"], CREATED_AT),
    ]
    new_message = message("new", "Verify the newest fix before continuing.")
    classified = [
        {
            "source_message_ids": [new_message.id],
            "summary": "Verify the newest fix before continuing.",
            "reason": "",
        }
    ]

    residual, ledger = wi.coverage_report(
        [new_message],
        [],
        ARCHIVE + "-next",
        classified,
        previous_residual=previous,
    )

    assert residual[:2] == previous
    assert len(residual) == 3
    assert "Verify the newest fix" in Message.from_dict(residual[-1]).content
    assert {row["message_id"] for row in ledger} == {
        new_message.id,
        previous[0]["id"],
        previous[1]["id"],
    }


def test_combined_summary_recency_uses_latest_source_not_first_emission_position():
    previous = [wi.continuation_message(CONSTRAINT, ARCHIVE, ["old"], CREATED_AT)]
    inherited = Message.from_dict(previous[0])
    middle = message("middle", "Run the invoice parser tests.")
    latest = message("latest", "Approval is still pending; include the attachment too.")
    # The combined entry is encountered at the inherited checkpoint first. Its
    # newest evidence nevertheless comes after the independent test reminder.
    classified = [
        {
            "source_message_ids": [inherited.id, latest.id],
            "summary": "Approval is still pending; preserve the attachment.",
            "reason": "",
        },
        {
            "source_message_ids": [middle.id],
            "summary": "Run the invoice parser tests.",
            "reason": "",
        },
    ]

    residual, ledger = wi.coverage_report(
        [inherited, middle, latest],
        [],
        ARCHIVE + "-next",
        classified,
        previous_residual=previous,
    )

    assert len(residual) == 2
    assert "Run the invoice parser tests." in Message.from_dict(residual[0]).content
    assert "Approval is still pending" in Message.from_dict(residual[1]).content
    assert residual[1]["source_message_ids"] == [inherited.id, latest.id]
    assert ledger[0]["summary"] == ledger[2]["summary"]


def test_previous_continuation_is_kept_until_explicitly_resolved():
    original = message("constraint", CONSTRAINT)
    classified = wi.resolve_continuation_coverage(
        ExtractContext([original]), [{"ranges": "0", "summary": CONSTRAINT}]
    )
    previous, _ = wi.coverage_report([original], [], ARCHIVE, classified)
    inherited = Message.from_dict(previous[0])
    later = message("approval", "The invoices are backed up and email approval is granted.")
    sources = [inherited, later]

    kept, _ = wi.coverage_report(sources, [], ARCHIVE + "-next", previous_residual=previous)
    assert kept == previous

    resolved = wi.resolve_continuation_coverage(
        ExtractContext(sources),
        [{"ranges": "0", "reason": "The newer approval resolves the previous restriction."}],
    )
    residual, ledger = wi.coverage_report(
        sources, [], ARCHIVE + "-next", resolved, previous_residual=previous
    )
    assert residual == []
    assert ledger[0]["explicitly_dropped"].startswith("The newer approval")
    assert ledger[1]["disposition"] == "archive_only"


def test_large_source_id_ledger_does_not_consume_continuation_projection_budget():
    messages = [
        message(f"source-{index:03d}-" + "identifier" * 15, "Invoice discussion.")
        for index in range(120)
    ]
    context = ExtractContext(messages)
    classified = wi.resolve_continuation_coverage(
        context, [{"ranges": all_ranges(context), "summary": CONSTRAINT}]
    )

    residual, ledger = wi.coverage_report(messages, [], ARCHIVE, classified)
    projection, _ = wi.build_projection([], residual)

    assert len(ledger) == 120
    assert {row["message_id"] for row in ledger} == {value.id for value in messages}
    assert len(residual) == 1
    assert CONSTRAINT in projection
    assert f"Source coverage: {ARCHIVE}/.done" in projection
    assert estimate_text_tokens(wi.residual_text(residual)) < wi.RESIDUAL_TOKEN_BUDGET
    assert messages[-1].id not in projection


def test_background_summary_cannot_authorize_reopening_a_terminal_work_item():
    request = "Please reopen this work."
    raw = message("source", request)
    classified = wi.resolve_continuation_coverage(
        ExtractContext([raw]), [{"ranges": "0", "summary": request}]
    )
    residual, _ = wi.coverage_report([raw], [], ARCHIVE, classified)
    uri = "viking://user/alice/memories/work_item/wi-closed.md"
    old = MemoryFile(
        uri=uri,
        memory_type="work_item",
        extra_fields={
            "work_item_id": "wi-closed",
            "title": "Invoice work",
            "goal": "Check invoices",
            "status": "done",
            "version": 1,
            "terminal_evidence_at": "2026-09-30T11:00:00Z",
        },
    )
    operation = ResolvedOperation(
        uris=[uri],
        memory_type="work_item",
        old_memory_file_content=old,
        memory_fields={
            "work_item_id": "wi-closed",
            "status": "open",
            "reopen_reason": request,
        },
        source_message_ids=[raw.id],
    )
    ctx = RequestContext(user=UserIdentifier("acme", "alice"), role=Role.USER)

    with pytest.raises(ValueError, match="current user request"):
        validate_work_item_update(
            operation, old, ctx, ExtractContext([Message.from_dict(residual[0])])
        )
    # The actual newer user request, in contrast, is sufficient evidence.
    assert validate_work_item_update(operation, old, ctx, ExtractContext([raw]))["status"] == "open"
