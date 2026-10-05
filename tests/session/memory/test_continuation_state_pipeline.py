# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Stable continuation state across the actual Python parser, resolver and writer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.message import Message, TextPart
from openviking.session import work_items as wi
from openviking.session.continuation_state import confirm_continuation_promotions
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from tests.session.memory.test_work_item_python_pipeline import pipeline as pipeline


def source(identity, text):
    return Message(
        id=identity, role="user", parts=[TextPart(text)], created_at="2026-10-01T00:00:00Z"
    )


def apply_coverage(extracted, messages, number, previous=()):
    written = set(extracted.result.written_uris + extracted.result.edited_uris)
    receipts = [
        {"uri": uri, "source_message_ids": operation.source_message_ids}
        for operation in extracted.operations.upsert_operations
        for uri in operation.uris
        if uri in written
    ]
    return wi.coverage_report(
        messages,
        receipts,
        f"viking://user/alice/sessions/continuation-state/history/archive_{number:03d}",
        confirm_continuation_promotions(extracted.operations.continuation_coverage, receipts),
        previous_residual=list(previous),
    )


@pytest.mark.asyncio
async def test_real_python_pipeline_updates_omits_resolves_and_promotes_stable_issues(pipeline):
    first = [
        source("import", "Keep the import investigation pending."),
        source("approval", "Do not deploy before approval."),
    ]
    extracted = await pipeline.run(
        "sdk.continuation(action='create', ranges='0', summary='Import investigation pending.')\n"
        "sdk.continuation(action='create', ranges='1', summary='Do not deploy before approval.')\n"
        "sdk.commit()",
        first,
    )
    current, _ = apply_coverage(extracted, first, 1)
    assert len(current) == 2
    identities = [value["id"] for value in current]
    assert identities[0] != identities[1]

    for number in (2, 3):
        messages = [Message.from_dict(value) for value in current] + [
            source(f"progress-{number}", f"Import investigation reached step {number}.")
        ]
        extracted = await pipeline.run(
            f"sdk.continuation(action='update', continuation_id={identities[0]!r}, "
            f"ranges='2', summary='Import investigation step {number}; follow-up pending.')\n"
            "sdk.commit()",
            messages,
        )
        current, _ = apply_coverage(extracted, messages, number, current)
        assert {value["id"] for value in current} == set(identities)
        assert len(current) == 2
        text = wi.residual_text(current)
        assert f"Import investigation step {number}; follow-up pending." in text
        assert "Do not deploy before approval." in text
        assert "Import investigation pending." not in text

    # Turn one issue into a real on-disk task and explicitly resolve the other.
    # Only the scripted model is mocked: IDs, DSL binding, canonical writes and
    # the transition reducer all run through the same code used by extraction.
    current.sort(key=lambda value: identities.index(value["id"]))
    messages = [Message.from_dict(value) for value in current] + [
        source(
            "approved",
            "Approval is granted. Track the import investigation as a long-running task; "
            "the fix and regression test are still pending.",
        )
    ]
    extracted = await pipeline.run(
        "task = sdk.create_work_item("
        "title='Fix import', scope='repository', goal='Imports complete without errors', "
        "status='in_progress', current_state='Investigation done; fix pending', "
        "next_action='Implement fix and add regression test', constraints='', waiting_for='', "
        "decisions='', refs='', ranges='0,2', reopen_reason='')\n"
        f"sdk.continuation(action='promote', continuation_id={identities[0]!r}, "
        "ranges='0,2', reason='Now tracked as a durable task', work_item=task)\n"
        f"sdk.continuation(action='resolve', continuation_id={identities[1]!r}, "
        "ranges='2', reason='The user explicitly granted approval')\n"
        "sdk.commit()",
        messages,
    )
    final, ledger = apply_coverage(extracted, messages, 4, current)
    assert final == []
    assert len(extracted.result.written_uris) == 1
    task_uri = extracted.result.written_uris[0]
    canonical = MemoryFileUtils.read(pipeline.fs.path(task_uri).read_text(), uri=task_uri)
    assert canonical.extra_fields["work_item_id"].startswith("wi-")
    assert canonical.extra_fields["status"] == "in_progress"
    assert "regression test" in canonical.extra_fields["next_action"]
    assert any(task_uri in row.get("work_item_uris", []) for row in ledger)
    assert not pipeline.fs.path(task_uri).name.startswith(identities[0])


@pytest.mark.asyncio
async def test_real_failed_task_write_cannot_complete_promotion(pipeline, monkeypatch):
    previous = [
        wi.continuation_message(
            "Import investigation pending.",
            "viking://user/alice/sessions/continuation-state/history/archive_001",
            ["original"],
            "2026-10-01T00:00:00Z",
        )
    ]
    identity = previous[0]["id"]
    messages = [
        Message.from_dict(previous[0]),
        source("promote", "Track the import investigation."),
    ]
    observed = {}
    apply_operations = MemoryUpdater.apply_operations

    async def capture_result(updater, operations, *args, **kwargs):
        result = await apply_operations(updater, operations, *args, **kwargs)
        observed.update(operations=operations, result=result)
        return result

    monkeypatch.setattr(MemoryUpdater, "apply_operations", capture_result)
    monkeypatch.setattr(
        pipeline.fs, "write_file", AsyncMock(side_effect=OSError("canonical storage unavailable"))
    )
    # The shared fixture asserts successful writes. Capture its real failure
    # result here without replacing the parser, resolver, or updater behavior.
    with pytest.raises(AssertionError):
        await pipeline.run(
            "task = sdk.create_work_item("
            "title='Fix import', scope='repository', goal='Imports complete without errors', "
            "status='open', current_state='Investigation pending', next_action='Reproduce', "
            "constraints='', waiting_for='', decisions='', refs='', ranges='0-1', reopen_reason='')\n"
            f"sdk.continuation(action='promote', continuation_id={identity!r}, "
            "ranges='0-1', reason='Move into a durable task', work_item=task)\nsdk.commit()",
            messages,
        )
    assert observed["result"].errors
    assert not observed["result"].written_uris
    assert not list(pipeline.fs.root.rglob("*.md"))
    current, ledger = apply_coverage(SimpleNamespace(**observed), messages, 2, previous)
    assert [value["id"] for value in current] == [identity]
    assert "Import investigation pending." in wi.residual_text(current)
    assert any(
        action.get("pending_promotion") is True
        for row in ledger
        for action in row.get("continuation_actions", [])
    )


@pytest.mark.asyncio
async def test_real_pipeline_closes_settled_background_but_keeps_standing_obligations(pipeline):
    summaries = {
        "answered": "The memory-policy question was answered and delivered; nothing remains open.",
        "constraint": "Do not push without explicit approval, even though no push is now planned.",
        "waiting": "The requested approval has not arrived; wait for the user's response.",
        "uncertain": "The test tool stopped, but its output is incomplete; success is unverified.",
    }
    previous = [
        wi.continuation_message(
            text,
            "viking://user/alice/sessions/continuation-state/history/archive_001",
            [f"original-{identity}"],
            "2026-10-01T00:00:00Z",
            continuation_id=identity,
        )
        for identity, text in summaries.items()
    ]
    reasons = {
        "answered": "The recorded answer was delivered and the question has no remaining obligation.",
        "constraint": "Still-applicable constraint: explicit approval is required before any push.",
        "waiting": "Pending: the user has not yet supplied the requested approval.",
        "uncertain": "Pending verification: incomplete tool output does not establish test success.",
    }
    messages = [Message.from_dict(value) for value in previous] + [source("greeting", "Hello.")]
    program = (
        "\n".join(
            f"sdk.continuation(action={'resolve' if identity == 'answered' else 'keep'!r}, "
            f"continuation_id={identity!r}, reason={reason!r})"
            for identity, reason in reasons.items()
        )
        + "\nsdk.commit()"
    )

    extracted = await pipeline.run(program, messages)
    current, ledger = apply_coverage(extracted, messages, 2, previous)

    assert {value["id"] for value in current} == {"constraint", "waiting", "uncertain"}
    assert {value["id"]: wi.continuation_content(value) for value in current} == {
        identity: text for identity, text in summaries.items() if identity != "answered"
    }
    assert not extracted.result.written_uris
    assert not extracted.result.edited_uris
    for identity, reason in reasons.items():
        row = next(value for value in ledger if value["message_id"] == identity)
        action = row["continuation_actions"][0]
        assert action["reason"] == reason
        assert action["state"] == ("resolved" if identity == "answered" else "active")
        assert action["source_message_ids"] == []
    assert all(
        action["continuation_fingerprint"] for action in extracted.operations.continuation_coverage
    )
