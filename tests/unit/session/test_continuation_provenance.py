# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
from copy import deepcopy

import pytest

from openviking.message import Message, TextPart
from openviking.session.continuation_provenance import prepare_continuation_provenance

ARCHIVE = "viking://user/u/sessions/s/history/archive_001"
CREATED_AT = "2026-10-01T00:00:00Z"


def checkpoint(identity="continuation-1", text="Deployment requires approval.", **extra):
    value = Message(
        id=identity,
        role="assistant",
        message_kind="checkpoint",
        parts=[TextPart(text)],
        created_at=CREATED_AT,
    ).to_dict()
    return {**value, **extra}


def test_large_legacy_source_lists_are_archived_without_growing_hot_state():
    sizes = []
    for count in [1, 1000, 10000]:
        source = checkpoint(
            source_message_ids=[f"message-{index}" for index in range(count)],
            source_continuation_ids=[f"old-summary-{index}" for index in range(count)],
            arbitrary_metadata={"history": "x" * count},
            peer_id="legacy-peer",
            turn_id="legacy-turn",
        )
        source["parts"][0]["arbitrary_metadata"] = "x" * count
        original = deepcopy(source)
        hot, ledger = prepare_continuation_provenance([source], [source], ARCHIVE, compacted=False)

        assert ledger["inputs"] == [original]
        assert ledger["outputs"] == {source["id"]: [source["id"]]}
        assert ledger["checkpoint_uri"] == f"{ARCHIVE}/.done"
        assert source == original
        assert set(hot[0]) == {
            "id",
            "role",
            "message_kind",
            "parts",
            "created_at",
            "source_checkpoint_uri",
        }
        assert set(hot[0]["parts"][0]) == {"type", "text"}
        sizes.append(len(json.dumps(hot)))
        ledger["inputs"][0]["source_message_ids"].clear()
        assert len(source["source_message_ids"]) == count
    assert len(set(sizes)) == 1


def test_repeated_compaction_records_direct_edges_without_expanding_history():
    source = checkpoint(source_message_ids=[f"raw-{index}" for index in range(1000)])
    snapshots = {}
    residual = [source]
    for step in range(1, 5):
        archive = ARCHIVE.replace("001", f"{step:03}")
        output = checkpoint(identity=f"summary-{step}")
        hot, ledger = prepare_continuation_provenance(residual, [output], archive, compacted=True)
        snapshots[ledger["checkpoint_uri"]] = ledger
        assert ledger["outputs"] == {output["id"]: [residual[0]["id"]]}
        assert ledger["inputs"] == residual
        if step > 1:
            assert "raw-" not in json.dumps(ledger)
            assert len(json.dumps(ledger)) < 1000
        residual = hot

    # Following direct edges still reaches the initial full source attribution.
    cursor = residual[0]
    for _ in range(4):
        ledger = snapshots[cursor["source_checkpoint_uri"]]
        source_id = ledger["outputs"][cursor["id"]][0]
        cursor = next(value for value in ledger["inputs"] if value["id"] == source_id)
    assert cursor == source
    assert len(cursor["source_message_ids"]) == 1000


def test_noncompacted_outputs_keep_individual_sources_and_compacted_outputs_merge_them():
    inputs = [checkpoint("a"), checkpoint("b")]
    _, direct = prepare_continuation_provenance(inputs, inputs, ARCHIVE, compacted=False)
    _, merged = prepare_continuation_provenance(
        inputs, [checkpoint("merged")], ARCHIVE, compacted=True
    )
    assert direct["outputs"] == {"a": ["a"], "b": ["b"]}
    assert merged["outputs"] == {"merged": ["a", "b"]}


def test_replaces_known_generated_suffix_but_preserves_references_in_body():
    previous_uri = f"{ARCHIVE}/.done"
    next_archive = ARCHIVE.replace("001", "002")
    body = (
        f"Inspect {previous_uri} before deployment.\n"
        "Source coverage: viking://user/u/resources/user-reference.md\n"
        "Approval is still required."
    )
    source = checkpoint(
        text=f"{body}\nSource coverage: {previous_uri}", source_checkpoint_uri=previous_uri
    )
    hot, _ = prepare_continuation_provenance([source], [source], next_archive, compacted=False)
    assert Message.from_dict(hot[0]).content == (f"{body}\nSource coverage: {next_archive}/.done")
    # A second normalization does not append a duplicate current suffix.
    again, _ = prepare_continuation_provenance(hot, hot, next_archive, compacted=False)
    assert again == hot


def test_keeps_unknown_source_coverage_suffix_written_in_body():
    body = "Approval required.\nSource coverage: viking://user/u/resources/manual.md"
    source = checkpoint(text=body)
    hot, _ = prepare_continuation_provenance([source], [source], ARCHIVE, compacted=False)
    assert Message.from_dict(hot[0]).content == f"{body}\nSource coverage: {ARCHIVE}/.done"


def test_preserves_all_text_parts_and_forces_historical_assistant_role():
    source = checkpoint(role="user", source_message_ids=["m1"])
    source["parts"].append({"type": "text", "text": "Run the tests first."})
    hot, _ = prepare_continuation_provenance([source], [source], ARCHIVE, compacted=False)
    assert hot[0]["role"] == "assistant"
    assert hot[0]["message_kind"] == "checkpoint"
    assert Message.from_dict(hot[0]).content == (
        f"Deployment requires approval.\nRun the tests first.\nSource coverage: {ARCHIVE}/.done"
    )


@pytest.mark.parametrize(
    ("inputs", "outputs", "compacted", "error"),
    [
        ([checkpoint()], [checkpoint(message_kind="user_query")], False, "checkpoint outputs"),
        ([checkpoint()], [checkpoint("unknown")], False, "matching source"),
        ([], [checkpoint()], True, "matching source"),
        ([checkpoint(), checkpoint()], [], False, "unique nonempty"),
        ([checkpoint()], [checkpoint(), checkpoint()], False, "unique nonempty"),
        (
            [checkpoint()],
            [checkpoint(parts=[{"type": "tool", "tool_output": "large log"}])],
            False,
            "only text",
        ),
    ],
)
def test_rejects_untraceable_or_noncheckpoint_outputs(inputs, outputs, compacted, error):
    with pytest.raises(ValueError, match=error):
        prepare_continuation_provenance(inputs, outputs, ARCHIVE, compacted=compacted)


def test_empty_continuation_has_empty_provenance():
    hot, ledger = prepare_continuation_provenance([], [], ARCHIVE, compacted=False)
    assert hot == []
    assert ledger == {
        "version": 1,
        "checkpoint_uri": f"{ARCHIVE}/.done",
        "inputs": [],
        "outputs": {},
    }
