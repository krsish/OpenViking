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

The current V1 limits use OpenViking's token estimator:

| Content | Limit |
| --- | --- |
| All short fields of one work item | 1,200 estimated tokens |
| Rendered canonical body | 4,000 estimated tokens |
| Working-memory projection, including residual and recovery hint | 3,000 estimated tokens |
| Active items in one projection | At most 3 |
| Continuation summaries and unassigned original messages | 1,000 estimated tokens |

Whole item blocks are selected; constraints are not character-truncated to fit. The caller's context budget must also accommodate the uncovered raw tail. Oversized updates fail without replacing the previous canonical body.

## Checkpoint publication and recovery

Background commit reuses the memory extractor. Each source message may be attributed to successfully updated work items, retained in a bounded continuation summary, or explicitly discarded with a reason that nothing remains to continue. Unclassified messages remain verbatim. A message can contribute both work-item state and additional continuation constraints. Summaries come from the same extraction response (`sdk.continuation` in Python or `continuation_coverage` in JSON), without an extra model call. They carry forward as assistant background and cannot authorize reopening terminal work. Original transcripts remain intact; the coverage ledger records their sources.

When several successfully written items account for different chunks of one raw message, their ranges are combined before classifying the whole message. Failed writes contribute no coverage. Tool evidence uses bounded previews and original references; partially read tool messages cannot claim full coverage. Ranges and classifications are model-provided attribution: this ledger does **not** formally prove that every continuation-relevant fact was preserved. Missing attribution never grants permission to discard the message; excessive residual prevents publication, with actual tokens, budget and per-message destinations retained in archive metadata for diagnosis.

Before applying work-item writes, the archive saves operations with allocated IDs in existing metadata. Retries reuse that plan and source attribution across partial writes or changed batch limits, rather than assigning IDs from a reordered model response. Already-applied versions are not rewritten; concurrent session updates still require version checks. A batch is complete only after all planned work-item operations succeed. The archive builds the bounded projection and publishes `.done` last via a temporary file and rename. An overview alone is not a ready checkpoint. Before an unfinished item leaves the hot projection, its vector record must cover the required canonical version. Terminal items still index asynchronously but do not block publication. This is a per-item cold-eviction check, not a global embedding barrier; active items do not wait for unrelated indexing. Canonical/archive storage and vector discovery serve different purposes.

Pending and failed archives after the last ready checkpoint remain raw continuation. If refreshing canonical state cannot produce a valid view, the server withholds a usable checkpoint and retains raw history for fallback. If a requested context budget is insufficient, the response is `budget_insufficient` with no checkpoint boundary; the caller must keep its transcript or use a larger budget.

Existing local and cuvs vector collections receive the `work_item_version` field through normal schema migration. Work-item session creation and commits verify that the actual collection schema contains this int64 field; missing, incompatible, or unverifiable metadata returns `FAILED_PRECONDITION` before archive creation. Legacy mode remains available. Existing remote collections require an out-of-band migration (default 0 for the added field) and reindexing of work items. Volcengine API-key data-plane metadata is a local expected schema, so it cannot pass this check; work-item mode requires a connection with actual collection-schema access, such as AK/SK control-plane access.

Pi uses two fallback steps when compaction is requested:

1. Reuse a valid earlier checkpoint plus the complete raw Pi transcript through the requested cut; Pi retains the tail after that cut.
2. If readiness, branch continuity, refresh, or the complete context budget check fails, use Pi's normal compaction.

The hook refreshes a cached checkpoint once and does not add an LLM call or wait for indexing/readiness. A late checkpoint cannot overwrite a newer Pi compaction.

V1 provides no dedicated proactive work-item write API, dependency graph, automatic migration of legacy sessions, or guarantee of perfect semantic matching. Generic body writes cannot bypass work-item update checks. Cold work remains discoverable through existing recall; archives retain original evidence for `archive_search` and read-back.
