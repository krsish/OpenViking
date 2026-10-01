# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared work-item limits for storage validation, extraction, and projection."""

from dataclasses import dataclass

from openviking_cli.utils.config import get_openviking_config


@dataclass(frozen=True)
class WorkItemBudgets:
    work_item_token_budget: int = 10000
    continuation_token_budget: int = 10000
    projection_token_budget: int = 42000


def get_work_item_budgets() -> WorkItemBudgets:
    """Read current configuration, including defaults for standalone schema use."""
    defaults = WorkItemBudgets()
    try:
        memory = get_openviking_config().memory
    except FileNotFoundError:
        return defaults
    return WorkItemBudgets(
        work_item_token_budget=getattr(
            memory, "work_item_token_budget", defaults.work_item_token_budget
        ),
        continuation_token_budget=getattr(
            memory, "continuation_token_budget", defaults.continuation_token_budget
        ),
        projection_token_budget=getattr(
            memory, "work_item_projection_token_budget", defaults.projection_token_budget
        ),
    )


def work_item_budget_instruction() -> str:
    budgets = get_work_item_budgets()
    return (
        f"Each work_item's fields and rendered body must fit within "
        f"{budgets.work_item_token_budget} estimated tokens, including heading overhead. "
        f"Keep the complete continuation, including still-valid existing items, within "
        f"{budgets.continuation_token_budget} estimated tokens including formatting. "
        "These are maximum budgets, not length targets; keep only useful current state."
    )


def continuation_selection_instruction() -> str:
    budget = get_work_item_budgets().continuation_token_budget
    return (
        "Select continuation needed beyond the work_items: unresolved requests, constraints, "
        "commitments, pending verification and necessary references. Maintain one stable item "
        "per matter, using its supplied continuation_id. For every existing item emit keep "
        "(unchanged), update (complete latest summary), resolve (reason with new evidence), "
        "or promote (reason plus a specific work_item that now preserves it). Supply current "
        "supporting ranges for every action except keep. Create only genuinely new items, "
        "with ranges and summary and without assigning an ID. Never recreate an existing item "
        "under a new ID or append a round-by-round summary. Progress, a completed substep, "
        "a new phase, or a changed next action within the same matter requires update with "
        "the same continuation_id while any follow-up remains. Never use resolve plus create "
        "as a substitute for update; completing the previous next step does not resolve the "
        "whole matter. New original messages not selected "
        "for work_items or "
        "continuation remain archive_only; their original text is stored, not copied into WM. "
        "Previous continuation remains active by default: omitted items keep their ID and "
        "content. Omission is not resolution. A promotion is effective only after the target "
        "work_item has successfully saved this item's state; include the old continuation "
        "(every source index when split into chunks) and new evidence in that work_item's "
        "create/update ranges. Reading or activating a work_item alone cannot promote an "
        "item. Example: old item at source index 2 plus new evidence at index 3 requires "
        "the work_item's ranges='2-3'; ranges='3' alone does not transfer the old item. "
        "Do not transfer unrelated constraints. "
        "Use keep for unchanged items instead of rewriting their content. Use resolve only "
        "when new evidence explicitly settles every pending part of that item and no "
        "continuation of the same matter remains. "
        "Partial tool previews may be classified; preserve pending verification and references "
        "when outcomes are unclear, without inventing unseen results. "
        f"All continuation together, including previous state and formatting, must fit within "
        f"{budget} estimated tokens."
    )
