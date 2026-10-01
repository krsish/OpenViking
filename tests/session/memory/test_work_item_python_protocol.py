# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Work-item extraction must not ask the model to invent server-owned IDs."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openviking.prompts.manager import PromptManager
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.extraction_output_protocol import (
    ExtractionOutputContext,
    create_extraction_output_protocol,
)
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.page_id_map import PageIdMap
from openviking.session.memory.schema_model_generator import SchemaModelGenerator


@pytest.fixture
def work_item_context():
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.load_from_yaml(
        str(PromptManager._get_bundled_templates_dir() / "memory" / "work_item.yaml")
    )
    schema = registry.get("work_item")
    config = SimpleNamespace(memory=SimpleNamespace(link_enabled=False))
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=config):
        operations_model = SchemaModelGenerator([schema]).create_structured_operations_model()
    return ExtractionOutputContext(
        operations_model=operations_model,
        schemas=(schema,),
        page_id_map=PageIdMap(),
        read_file_contents={},
        link_enabled=False,
    )


def _work_item_fields():
    return {
        "title": "Restore rollout",
        "scope": "deployment service",
        "goal": "Fix the failed rollout and verify the deployment",
        "status": "in_progress",
        "current_state": "The deployment test fails on an invalid health check.",
        "next_action": "Fix the health check and rerun the deployment test.",
        "constraints": "Wait for user approval before deploying to production.",
        "waiting_for": "",
        "decisions": "",
        "refs": "deployment/health.py",
        "ranges": "0-2",
        "reopen_reason": "",
    }


def _create_call(fields):
    arguments = ", ".join(f"{key}={value!r}" for key, value in fields.items())
    return f"sdk.create_work_item({arguments})"


def test_work_item_create_contract_omits_server_id(work_item_context):
    protocol = create_extraction_output_protocol("python")

    contract = protocol.render_contract(work_item_context)

    signature = next(line for line in contract.splitlines() if "sdk.create_work_item(" in line)
    assert "work_item_id" not in signature
    assert "title: str" in signature
    assert "goal: str" in signature
    assert "ranges: str" in signature
    assert "work_item_id [immutable]" not in contract


def test_work_item_identity_contract_does_not_claim_singleton_scope(work_item_context):
    protocol = create_extraction_output_protocol("python")

    contract = protocol.render_contract(work_item_context)

    assert "server" in contract.lower()
    assert "fixed to self" not in contract
    assert (
        "Calls with identical identity field values address the same memory object" not in contract
    )
    assert "Identity fields (primary key): work_item_id" not in contract


@pytest.mark.parametrize("legacy_id", [None, "model-invented-id"])
def test_work_item_create_parses_without_a_model_supplied_id(work_item_context, legacy_id):
    protocol = create_extraction_output_protocol("python")
    fields = _work_item_fields()
    if legacy_id is not None:
        fields["work_item_id"] = legacy_id

    operations, error = protocol.parse(
        "task = " + _create_call(fields) + "\nsdk.commit()", work_item_context
    )

    assert error is None
    assert len(operations.work_item) == 1
    item = operations.work_item[0]
    assert item.page_id >= 100
    assert item.title == fields["title"]
    assert item.constraints == fields["constraints"]
    assert item.ranges == "0-2"
    assert "work_item_id" not in item.model_dump()
    assert operations.continuation_coverage == []


def test_json_and_python_create_produce_equivalent_operations_without_ids(work_item_context):
    fields = _work_item_fields()
    python_protocol = create_extraction_output_protocol("python")
    json_protocol = create_extraction_output_protocol("json")
    payload = {name: [] for name in work_item_context.operations_model.model_fields}
    payload["work_item"] = [{"page_id": 100, **fields}]

    python_operations, python_error = python_protocol.parse(
        "task = " + _create_call(fields) + "\nsdk.commit()", work_item_context
    )
    json_operations, json_error = json_protocol.parse(json.dumps(payload), work_item_context)

    assert python_error is None
    assert json_error is None
    assert python_operations.model_dump() == json_operations.model_dump()
    assert "work_item_id" not in python_operations.work_item[0].model_dump()


@pytest.mark.parametrize("missing_field", ["title", "goal", "ranges"])
def test_work_item_create_still_requires_complete_business_fields(work_item_context, missing_field):
    protocol = create_extraction_output_protocol("python")
    fields = _work_item_fields()
    del fields[missing_field]

    operations, error = protocol.parse("task = " + _create_call(fields), work_item_context)

    assert operations is None
    assert "requires complete memory fields" in error
    assert missing_field in error
    assert "work_item_id" not in error


def test_work_item_create_allocates_distinct_temporary_pages(work_item_context):
    protocol = create_extraction_output_protocol("python")
    first = _work_item_fields()
    second = {**first, "title": "Repair dashboard", "scope": "monitoring", "ranges": "3-4"}

    operations, error = protocol.parse(
        "first_task = "
        + _create_call(first)
        + "\nsecond_task = "
        + _create_call(second)
        + "\nsdk.commit()",
        work_item_context,
    )

    assert error is None
    assert len(operations.work_item) == 2
    assert {item.title for item in operations.work_item} == {first["title"], second["title"]}
    assert len({item.page_id for item in operations.work_item}) == 2
    assert all(item.page_id >= 100 for item in operations.work_item)


def test_existing_work_item_updates_use_the_binding_without_exposing_its_id(work_item_context):
    protocol = create_extraction_output_protocol("python")
    uri = "viking://user/alice/memories/work_item/wi-existing.md"
    existing = MemoryFile(
        uri=uri,
        memory_type="work_item",
        extra_fields={**_work_item_fields(), "work_item_id": "wi-existing", "version": 3},
    )
    work_item_context.read_file_contents[uri] = existing
    page_id = work_item_context.page_id_map.get_page_id(uri)

    declarations = protocol.render_new_bindings(work_item_context, source="prefetch")
    binding = protocol.binding_name(uri)
    operations, error = protocol.parse(
        f'{binding}.update(title="Health check fixed", ranges="3")\nsdk.commit()',
        work_item_context,
    )

    assert error is None
    assert "work_item_id" not in declarations
    assert len(operations.work_item) == 1
    item = operations.work_item[0]
    assert item.page_id == page_id
    assert item.title == "Health check fixed"
    assert item.ranges == "3"
    assert item.goal is None
    assert "work_item_id" not in item.model_dump()
