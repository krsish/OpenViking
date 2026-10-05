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
        "commitments, pending verification and references needed for ongoing work. Maintain "
        "one stable item per matter, using its supplied continuation_id. For every existing "
        "item emit keep (unchanged content plus a concrete retention reason), update "
        "(complete latest summary), resolve (reason explaining closure), or promote "
        "(reason plus a specific work_item that now preserves it). Supply current supporting "
        "ranges with new non-checkpoint evidence for create, update and promote. Ranges are "
        "optional for keep and resolve. Create only genuinely new items, "
        "with ranges and summary and without assigning an ID. Never recreate an existing item "
        "under a new ID or append a round-by-round summary. Progress, a completed substep, "
        "a new phase, or a changed next action within the same matter requires update with "
        "the same continuation_id while any follow-up remains. Never use resolve plus create "
        "as a substitute for update; completing the previous next step does not resolve the "
        "whole matter. New original messages not selected "
        "for work_items or "
        "continuation remain archive_only; their original text is stored, not copied into WM. "
        "Previous continuation is preserved by default: omitted items keep their ID and "
        "content. Omission is not resolution. keep retains an item in active working memory; "
        "resolve removes it only from active working memory. Both preserve its history and "
        "original evidence in the archive; neither deletes archive data. "
        "A promotion is effective only after the target "
        "work_item has successfully saved this item's state; include the old continuation "
        "(every source index when split into chunks) and new evidence in that work_item's "
        "create/update ranges. Reading or activating a work_item alone cannot promote an "
        "item. Example: old item at source index 2 plus new evidence at index 3 requires "
        "the work_item's ranges='2-3'; ranges='3' alone does not transfer the old item. "
        "Do not transfer unrelated constraints. "
        "For keep, reason must name a remaining request, still-valid constraints, a continuing "
        "commitment, a reference needed for ongoing work, or an uncertainty that needs "
        "verification. A still-valid constraint can require retention without any next action. "
        "Review each item's current state together with the new conversation. Resolve only "
        "when all its obligations are settled and no applicable constraints or necessary "
        "continuation remain. An already-settled state in the supplied item is sufficient "
        "evidence for resolve; no new messages or ranges are required. State that basis in "
        "reason. If completion or applicability is uncertain, keep and explain what needs "
        "verification. For completed Q&A or delivery records with no remaining obligations "
        "or applicable constraints, emit resolve even when no new messages concern them. "
        "Keeping an item solely for archival background is invalid: archiving is already "
        "done and does not require keeping it active. "
        "The server may move idle, unprotected items to cold storage without resolving them. "
        "An unanswered question or unfinished action remains active even if it is temporary, "
        "low priority, has no deadline, or needs no protection. Use keep without protection "
        "for such an unchanged item; leave cooling to the server. Lack of protection is "
        "not evidence of completion or cancellation. "
        "Only new related conversation evidence refreshes activity: cite its ranges on keep "
        "or update. Repeating keep, rewriting a summary, reading history, or seeing an item "
        "in this prompt does not refresh it. For a supplied cold item relevant to new "
        "messages, reuse its ID with keep or update and supporting new ranges to restore it; "
        "resolve it instead if already settled. Do not recreate it under a new ID. "
        "Register protection from idle eviction on create, update or keep using "
        "protection={kind, reason, ranges}: kind='constraint' for a still-applicable rule, "
        "'pinned' for an explicit user instruction to keep attention on this item, or "
        "'commitment' for a promise needed by ongoing work or an approaching due date. "
        "State the concrete reason and applicable scope, and cite complete supporting "
        "non-checkpoint source messages. Initial registration of an existing item's "
        "protection may instead cite all chunks of its own checkpoint; unrelated background "
        "cannot protect it. A vague keep reason or ordinary unfinished work does not warrant "
        "protection. Omit protection to preserve its prior value; use kind='none' with "
        "current non-checkpoint evidence and reason to remove it when no longer applicable. "
        "Protection does not bypass the working-memory token budget. "
        "Partial tool previews may be classified; preserve pending verification and references "
        "when outcomes are unclear, without inventing unseen results. "
        f"All continuation together, including previous state and formatting, must fit within "
        f"{budget} estimated tokens."
    )
