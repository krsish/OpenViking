# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Continuation retention accepts explicit protection without inventing activity."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from openviking.message import Message, TextPart
from openviking.prompts.manager import PromptManager
from openviking.session.continuation_state import continuation_fingerprint
from openviking.session.memory.extraction_output_protocol import (
    ExtractionOutputContext,
    create_extraction_output_protocol,
)
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.memory_updater import ExtractContext
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.schema_model_generator import SchemaModelGenerator
from openviking.session.work_items import resolve_continuation_coverage
from openviking_cli.utils.config.memory_config import MemoryConfig


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


def _parse(context, protocol_name, entry):
    source = (
        json.dumps({"continuation_coverage": [entry]})
        if protocol_name == "json"
        else "sdk.continuation(" + ", ".join(f"{k}={v!r}" for k, v in entry.items()) + ")"
    )
    return create_extraction_output_protocol(protocol_name).parse(source, context)


def _keep(protection=None):
    entry = {
        "action": "keep",
        "continuation_id": "c-rule",
        "reason": "The user's no-push rule still applies to this session.",
    }
    if protection is not None:
        entry["protection"] = protection
    return entry


def _extraction():
    return ExtractContext(
        [
            Message(
                id="c-rule",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("Do not push commits during this session.")],
            ),
            Message(
                id="c-other",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("A different task is waiting for review.")],
            ),
            Message(id="new", role="user", parts=[TextPart("The no-push rule still applies.")]),
        ]
    )


@pytest.mark.parametrize("protocol_name", ["python", "json"])
@pytest.mark.parametrize("kind", ["constraint", "pinned", "commitment", "none"])
def test_protection_parses_identically_in_both_protocols(context, protocol_name, kind):
    protection = {"kind": kind, "reason": "The user specified its current scope.", "ranges": "2"}

    operations, error = _parse(context, protocol_name, _keep(protection))

    assert error is None
    assert operations.continuation_coverage[0].protection.model_dump() == protection


@pytest.mark.parametrize("protocol_name", ["python", "json"])
@pytest.mark.parametrize("action", ["create", "update"])
def test_protection_can_accompany_new_or_changed_state(context, protocol_name, action):
    entry = {
        "action": action,
        "summary": "Do not push commits during this session.",
        "ranges": "2",
        "protection": {"kind": "constraint", "reason": "The user set this rule.", "ranges": "2"},
    }
    if action == "update":
        entry["continuation_id"] = "c-rule"

    operations, error = _parse(context, protocol_name, entry)

    assert error is None
    actions = resolve_continuation_coverage(_extraction(), operations.continuation_coverage)
    assert actions[0]["protection"]["source_message_ids"] == ["new"]


@pytest.mark.parametrize("protocol_name", ["python", "json"])
@pytest.mark.parametrize(
    "protection",
    [
        {"kind": "constraint", "reason": "Still applies."},
        {"kind": "constraint", "reason": "   ", "ranges": "2"},
        {"kind": "constraint", "reason": "Still applies.", "ranges": "   "},
        {"kind": "forever", "reason": "Still applies.", "ranges": "2"},
    ],
)
def test_invalid_protection_requests_repair(context, protocol_name, protection):
    operations, error = _parse(context, protocol_name, _keep(protection))

    assert operations is None
    assert "protection" in error


@pytest.mark.parametrize("protocol_name", ["python", "json"])
@pytest.mark.parametrize("action", ["", "resolve"])
def test_protection_requires_a_state_preserving_action(context, protocol_name, action):
    entry = {
        "action": action,
        "reason": "The matter has finished.",
        "ranges": "2",
        "protection": {"kind": "constraint", "reason": "Still applies.", "ranges": "2"},
    }
    if action:
        entry["continuation_id"] = "c-rule"

    operations, error = _parse(context, protocol_name, entry)

    assert operations is None
    assert "protection" in error or "continuation() requires" in error


@pytest.mark.parametrize("kind", ["constraint", "pinned", "commitment", "none"])
def test_protection_freezes_actual_source_ids(kind):
    actions = resolve_continuation_coverage(
        _extraction(),
        [_keep({"kind": kind, "reason": " The user specified its current scope. ", "ranges": "2"})],
    )

    assert actions[0]["source_message_ids"] == []
    assert actions[0]["protection"] == {
        "kind": kind,
        "reason": "The user specified its current scope.",
        "source_message_ids": ["new"],
    }
    assert actions[0]["continuation_fingerprint"] == continuation_fingerprint(
        "Do not push commits during this session."
    )


def test_existing_constraint_can_register_from_its_own_complete_checkpoint():
    actions = resolve_continuation_coverage(
        _extraction(),
        [_keep({"kind": "constraint", "reason": "The session rule still applies.", "ranges": "0"})],
    )

    assert actions[0]["protection"]["source_message_ids"] == ["c-rule"]
    assert actions[0]["source_message_ids"] == []
    assert actions[0]["continuation_fingerprint"]


@pytest.mark.parametrize(
    ("kind", "ranges"), [("constraint", "1"), ("constraint", "0-1"), ("none", "0")]
)
def test_unrelated_background_cannot_protect_and_old_state_cannot_remove_protection(kind, ranges):
    with pytest.raises(ValueError, match="complete current non-checkpoint evidence"):
        resolve_continuation_coverage(
            _extraction(), [_keep({"kind": kind, "reason": "Still applies.", "ranges": ranges})]
        )


def test_partial_checkpoint_cannot_register_protection():
    extraction = ExtractContext(
        [
            Message(
                id="c-rule",
                role="assistant",
                message_kind="checkpoint",
                parts=[TextPart("Do not push commits during this session. " * 1000)],
            )
        ]
    )
    assert len(extraction.messages) > 1
    protection = {"kind": "constraint", "reason": "The session rule still applies.", "ranges": "0"}

    with pytest.raises(ValueError, match="complete current non-checkpoint evidence"):
        resolve_continuation_coverage(extraction, [_keep(protection)])

    protection["ranges"] = f"0-{len(extraction.messages) - 1}"
    actions = resolve_continuation_coverage(extraction, [_keep(protection)])
    assert actions[0]["protection"]["source_message_ids"] == ["c-rule"]


@pytest.mark.parametrize("message_kind", ["checkpoint", "content"])
def test_partial_unrelated_source_cannot_be_hidden_by_own_complete_checkpoint(message_kind):
    extraction = _extraction()
    extraction = ExtractContext(
        [
            extraction.messages[0],
            Message(
                id="long-other",
                role="assistant",
                message_kind=message_kind,
                parts=[TextPart("Unrelated contents. " * 3000)],
            ),
        ]
    )
    assert len(extraction.messages) > 2

    with pytest.raises(ValueError, match="complete current non-checkpoint evidence"):
        resolve_continuation_coverage(
            extraction,
            [_keep({"kind": "constraint", "reason": "An incomplete reference.", "ranges": "0-1"})],
        )


def test_keep_alone_neither_invents_activity_nor_changes_protection():
    actions = resolve_continuation_coverage(_extraction(), [_keep()])

    assert actions[0]["source_message_ids"] == []
    assert "protection" not in actions[0]

    actions = resolve_continuation_coverage(_extraction(), [{**_keep(), "ranges": "2"}])
    assert actions[0]["source_message_ids"] == ["new"]


@pytest.mark.parametrize("protocol_name", ["python", "json"])
def test_contract_explains_cold_restoration_and_evidence_backed_protection(context, protocol_name):
    protocol = create_extraction_output_protocol(protocol_name)
    contract = protocol.render_contract(context)

    assert "cold storage without resolving" in contract
    assert "protection" in contract
    assert "current non-checkpoint evidence" in contract
    assert "does not bypass the working-memory token budget" in contract
    assert "repeating keep does neither" in protocol.render_final_instruction(context)


def test_retention_defaults_and_configuration_roundtrip():
    config = MemoryConfig()
    assert config.continuation_ttl_enabled is True
    assert config.continuation_idle_turns == 30
    assert config.continuation_idle_days == 7
    assert config.continuation_min_idle_turns == 5

    config = MemoryConfig(
        continuation_ttl_enabled=False,
        continuation_idle_turns=12,
        continuation_idle_days=2.5,
        continuation_min_idle_turns=3,
    )
    assert MemoryConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    "field", ["continuation_idle_turns", "continuation_idle_days", "continuation_min_idle_turns"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_retention_thresholds_must_be_positive(field, value):
    with pytest.raises(ValidationError):
        MemoryConfig(**{field: value})
