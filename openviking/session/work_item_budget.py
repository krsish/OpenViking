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
        f"Keep the complete continuation, including still-valid previous summaries, within "
        f"{budgets.continuation_token_budget} estimated tokens including formatting. "
        "These are maximum budgets, not length targets; keep only useful current state."
    )


def continuation_selection_instruction() -> str:
    budget = get_work_item_budgets().continuation_token_budget
    return (
        "Select continuation needed beyond the work_items: unresolved requests, constraints, "
        "commitments, pending verification and necessary references. Supply ranges and exactly "
        "one of summary or reason. New original messages not selected for work_items or "
        "continuation remain archive_only; their original text is stored, not copied into WM. "
        "Previous continuation remains active by default: preserve it unless explicitly updated, "
        "merged, resolved, or transferred into a specific work_item. Omission is not resolution. "
        "Use reason only to explain why selected continuation is no longer needed. "
        "Partial tool previews may be classified; preserve pending verification and references "
        "when outcomes are unclear, without inventing unseen results. "
        f"All continuation together, including previous state and formatting, must fit within "
        f"{budget} estimated tokens."
    )
