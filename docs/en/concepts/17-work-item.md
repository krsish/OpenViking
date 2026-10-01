# Work-item working memory

Work-item mode keeps a bounded working-memory view of the work needed to continue a conversation. Completed or inactive work can leave that view while its canonical memory and original session archives remain recoverable. It replaces the cumulative section-by-section summary only for sessions that opt in; `legacy` remains the default.

## Enable for a new session

Create a session with this policy through `POST /api/v1/sessions`:

```json
{
  "memory_policy": {
    "working_memory": {
      "enabled": true,
      "mode": "work_item"
    }
  }
}
```

The mode includes the `work_item` memory type in extraction. An explicit empty `memory_types` list still permits this required type, without enabling the other types. Use a new session when enabling the mode. Existing legacy sessions are not automatically migrated. [Session API](../api/05-sessions.md) describes creation and context reads; [Pi integration](../agent-integrations/11-pi.md) describes its client switch.

## Canonical state and the hot view

A work item is mutable Markdown at `viking://user/{user_id}/memories/work_item/{work_item_id}.md`. It belongs to the authenticated user, is shared across that user's sessions, and is not partitioned by workspace peer. A server-assigned identity survives title changes. Matching an existing item requires reading its canonical state; a similar title alone does not establish identity.

The short fields are `title`, `scope`, `goal`, `status`, `current_state`, `next_action`, `constraints`, `waiting_for`, `decisions`, and `refs`. Updates replace current state and clear resolved fields, rather than append a history. Status is `open`, `in_progress`, `waiting`, `blocked`, `done`, or `cancelled`. Reopening terminal work requires an explicit current user request dated after the recorded completion evidence. If that evidence lacks a usable timestamp, the server uses the terminal write time conservatively; delayed old requests cannot reopen it merely by reading its newest version. The shared update path checks the latest stored `version` under the item lock and rejects stale snapshots instead of overwriting another session's progress.

The session projection selects recent work, not every unfinished item. A request to resume A after working on B can retrieve A and bind it again. The same extraction pass reads at most three semantic candidates and explicitly selects a matching nonterminal item for activation. Activation alone does not write canonical state, increment its version, or claim message coverage; unrelated search hits remain inactive. Context and archive reads refresh active URIs from canonical state using templates, with no read-time LLM call. Another session's completed task therefore does not keep contributing an obsolete next action.

The default V1 limits use OpenViking's token estimator:

| Content | Limit |
| --- | --- |
| All short fields of one work item, and its rendered canonical body | 10,000 estimated tokens each |
| Working-memory projection, including continuation and recovery hint | 42,000 estimated tokens |
| Active items in one projection | At most 3 |
| Complete continuation block, including previous continuation and formatting | 10,000 estimated tokens |

Configure these limits in the server's `memory` configuration, separately from the session policy:

```json
{
  "memory": {
    "work_item_token_budget": 10000,
    "continuation_token_budget": 10000,
    "work_item_projection_token_budget": 42000
  }
}
```

The default projection budget allows three full-sized items, a full continuation block, and formatting overhead. These are ceilings, not targets for generated text. Source IDs and per-message destinations stay in the archive's coverage ledger; that ledger is not rendered into working memory.

Whole item blocks are selected; constraints are not character-truncated to fit. The caller's context budget must also accommodate the uncovered raw tail. Oversized updates fail without replacing the previous canonical body.

## Checkpoint publication and recovery

Background commit runs ordinary memory and work-item extraction as independent LLM passes with separate schemas, prompts and retrieval scopes. The passes run concurrently and reuse the existing update pipeline. Ordinary memory receives neither work-item instructions nor tool evidence. Each pass can still read or repair output through the existing ExtractLoop, so this does not guarantee exactly two underlying model requests.

After a successful extraction pass, new original messages not selected for work-item state or continuation are marked `archive_only`. Their complete content remains in `messages.jsonl` and referenced tool-result storage, without being copied into the hot view. A message can contribute both work-item state and additional continuation constraints. Previously selected continuation is kept by default: omission from a later response does not resolve it. The extractor must explicitly update, merge, resolve, or transfer that state to a work item before the old continuation can leave the view.

Ordinary continuation summaries come from the work-item extraction response (`sdk.continuation` in Python or `continuation_coverage` in JSON), without an additional summary call. They have separate checkpoint identities and carry source references as assistant background; they cannot authorize reopening terminal work. An explicit successful empty extraction result can leave new messages archive-only. An exception, missing result, or extraction that never ran cannot advance the checkpoint merely because the raw archive exists.

When several successfully written items account for different chunks of one raw message, their ranges are combined before classifying the whole message. Failed writes contribute no coverage. Each tool input/output field retains its first 2,000 characters plus a truncation notice when needed. The total tool-evidence budget is 16,000 estimated tokens, prioritizing recent results. Partial previews remain eligible for work-item attribution and continuation classification. State updates may use visible evidence, but must not invent unseen outcomes or equate a finished tool with a finished task; unclear outcomes retain pending verification and references. Ranges and classifications are model-provided attribution: this ledger does **not** formally prove that every continuation-relevant fact was preserved. Archive-only information remains recoverable from storage, but the model can still fail to select an important new detail for the hot view. Storage preservation is not a guarantee of omission-free hot memory.

Before applying work-item writes, the archive saves operations with allocated IDs in existing metadata. Retries preserve task identities, the original message batch, and source attribution. Each successful write has its own receipt, so a crash before batch progress is saved, or a subsequent edit from another session, does not cause that write to be repeated. The canonical file carries only one pending receipt; historical receipts live under the originating archive's `work-item-receipts/`, keeping canonical metadata bounded.

After a version conflict, the next background retry reads only the conflicted items' latest canonical states and re-extracts their updates using the original evidence. Completed writes, existing continuation classifications, and task identities are preserved. The revised plan must be saved before another version-checked write; later archives inherit the highest revision. Each attempt performs at most one reconciliation pass, retaining progress for another retry if concurrent edits cause another conflict. A batch is complete only after all planned operations succeed. Persistent model or storage failures, or continued contention, still fail safely without overwriting newer state or publishing an incomplete checkpoint.

Completed extraction and a ready checkpoint are separate states. If the combined continuation exceeds its budget, or a previous checkpoint still carries legacy raw residual, a separate background repair pass compacts that continuation. This exceptional path can call the LLM; normal projection, context reads, archive reads, and the Pi compact hook do not. Repair retries reuse completed canonical writes instead of rerunning those writes or freezing the same oversized continuation forever. A successful repair is persisted for reuse if later publication fails. A failed or still-oversized repair leaves the checkpoint unpublished, with its progress and diagnostics retained for another attempt.

The archive builds the bounded projection and publishes `.done` last via a temporary file and rename. An overview alone is not a ready checkpoint. Before an unfinished item leaves the hot projection, its vector record must cover the required canonical version. Terminal items still index asynchronously but do not block publication. This is a per-item cold-eviction check, not a global embedding barrier; active items do not wait for unrelated indexing. Canonical/archive storage and vector discovery serve different purposes.

Pending and failed archives after the last ready checkpoint remain raw continuation. If refreshing canonical state cannot produce a valid view, the server withholds a usable checkpoint and retains raw history for fallback. If a requested context budget is insufficient, the response is `budget_insufficient` with no checkpoint boundary; the caller must keep its transcript or use a larger budget.

Existing local and cuvs vector collections receive the `work_item_version` field through normal schema migration. Work-item session creation and commits verify that the actual collection schema contains this int64 field; missing, incompatible, or unverifiable metadata returns `FAILED_PRECONDITION` before archive creation. Legacy mode remains available. Existing remote collections require an out-of-band migration (default 0 for the added field) and reindexing of work items. Volcengine API-key data-plane metadata is a local expected schema, so it cannot pass this check; work-item mode requires a connection with actual collection-schema access, such as AK/SK control-plane access.

Pi uses two fallback steps when compaction is requested:

1. Reuse a valid earlier checkpoint plus the complete raw Pi transcript through the requested cut; Pi retains the tail after that cut.
2. If readiness, branch continuity, refresh, or the complete context budget check fails, use Pi's normal compaction.

The hook refreshes a cached checkpoint once and does not add an LLM call or wait for indexing/readiness. A late checkpoint cannot overwrite a newer Pi compaction.

V1 provides no dedicated proactive work-item write API, dependency graph, automatic migration of legacy sessions, or guarantee of perfect semantic matching. Generic body writes cannot bypass work-item update checks. Cold work remains discoverable through existing recall; original evidence can be recovered by listing session history and reading the relevant archive's `messages.jsonl` and referenced tool results in chunks.
