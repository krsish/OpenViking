# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Progress and bounded conflict recovery for a durable work-item decision batch."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Awaitable, Callable

from openviking.session.memory.dataclass import ResolvedOperations
from openviking.session.memory.utils.memory_file_utils import (
    MemoryFileUtils,
    memory_version_from_fields,
)
from openviking.session.memory.work_item_receipts import read_work_item_receipt
from openviking_cli.exceptions import ConflictError, NotFoundError


class WorkItemReplayState:
    def __init__(
        self,
        plan: dict[str, Any],
        save: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ):
        self.plan = deepcopy(plan)
        self.save_callback = save
        self.operations = ResolvedOperations.model_validate(self.plan["operations"])
        self.uris: set[str] = set()
        for operation in self.operations.upsert_operations:
            if operation.memory_type != "work_item" or len(operation.uris) != 1:
                raise ValueError("work_item replay requires one canonical URI per operation")
            uri = operation.uris[0]
            if uri in self.uris:
                raise ValueError("work_item replay requires one decision per canonical URI")
            self.uris.add(uri)
        self.completed = set(self.plan.get("completed_uris", []))
        self.conflicts = set(self.plan.get("conflict_uris", [])) - self.completed
        if not (self.completed | self.conflicts) <= self.uris:
            raise ValueError("work_item replay progress refers to an unknown operation")

    @property
    def archive_uri(self) -> str | None:
        return self.plan.get("receipt_archive_uri")

    @property
    def pending_operations(self) -> list[Any]:
        return [
            operation
            for operation in self.operations.upsert_operations
            if operation.uris[0] not in self.completed
        ]

    async def save(self) -> None:
        if self.save_callback is None:
            return
        self.plan.update(
            revision=self.plan.get("revision", 0) + 1,
            operations=self.operations.model_dump(mode="json"),
            completed_uris=sorted(self.completed),
            conflict_uris=sorted(self.conflicts),
        )
        # A callback may hold references; neither it nor a failed attempt may
        # mutate the last durable decision without another successful save.
        await self.save_callback(deepcopy(self.plan))

    async def inspect(self, fs: Any, ctx: Any) -> None:
        """Recover writes after a crash and find obsolete snapshots in old plans."""
        for operation in self.pending_operations:
            uri = operation.uris[0]
            if self.archive_uri and await read_work_item_receipt(
                fs, ctx, self.archive_uri, self.plan["extraction_id"], uri
            ):
                self.completed.add(uri)
                self.conflicts.discard(uri)
                continue
            try:
                canonical = MemoryFileUtils.read(await fs.read_file(uri, ctx=ctx), uri=uri)
            except (FileNotFoundError, NotFoundError):
                canonical = None
            snapshot = operation.old_memory_file_content
            expected = memory_version_from_fields(snapshot.extra_fields) if snapshot else 0
            actual = memory_version_from_fields(canonical.extra_fields) if canonical else 0
            # Older plans have no sidecar receipts. Let the updater's guarded
            # idempotence check verify their same-source write under the lease.
            legacy_applied = (
                canonical is not None
                and canonical.extra_fields.get("source_extraction_id") == self.plan["extraction_id"]
                and actual == expected + 1
            )
            if actual == expected:
                # A conflicting create may have disappeared before the retry.
                # The original decision is valid again; no canonical exists to
                # reconcile against in that case.
                self.conflicts.discard(uri)
            elif not legacy_applied:
                self.conflicts.add(uri)
        if self.conflicts and self.save_callback is None:
            raise ConflictError("work_item conflict recovery requires a durable replay save")

    async def record_result(self, result: Any) -> None:
        successes = self.uris & (
            set(getattr(result, "written_uris", [])) | set(getattr(result, "edited_uris", []))
        )
        conflicts = {
            uri
            for uri, error in getattr(result, "errors", [])
            if uri in self.uris and isinstance(error, ConflictError)
        }
        completed = self.completed | successes
        pending_conflicts = (self.conflicts | conflicts) - completed
        if completed != self.completed or pending_conflicts != self.conflicts:
            self.completed = completed
            self.conflicts = pending_conflicts
            await self.save()
