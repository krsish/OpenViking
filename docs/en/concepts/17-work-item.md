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

The default projection budget allows three full-sized items, a full continuation block, and formatting overhead. These are ceilings, not targets for generated text. Source IDs and per-message destinations stay in the archive's coverage ledger; that ledger is not rendered into working memory. Each archive's `continuation-provenance.json` records direct inputs to its continuation summaries. Historical sources are followed through checkpoint references instead of recursively copied into hot state. Legacy source-ID lists are externalized even below the body budget; compaction prompts contain content and necessary references only.

Whole item blocks are selected; constraints are not character-truncated to fit. The caller's context budget must also accommodate the uncovered raw tail. Oversized updates fail without replacing the previous canonical body.

## Checkpoint publication and recovery

Background commit runs ordinary memory and work-item extraction as independent LLM passes with separate schemas, prompts and retrieval scopes. The passes run concurrently and reuse the existing update pipeline. Ordinary memory receives neither work-item instructions nor tool evidence. Each pass can still read or repair output through the existing ExtractLoop, so this does not guarantee exactly two underlying model requests.

After a successful extraction pass, new original messages not selected for work-item state or continuation are marked `archive_only`. Their complete content remains in `messages.jsonl` and referenced tool-result storage, without being copied into the hot view. A message can contribute both work-item state and additional continuation constraints.

Continuation is a list of current issues with stable server-assigned identities, not a growing collection of previous summaries. Each extraction pass considers existing issues together with new evidence. An unchanged issue keeps its content; an update replaces that issue's current text while retaining its identity. A resolved issue requires an explicit reason and leaves the hot view. Promotion to a work item removes the issue only after the corresponding canonical write succeeds. Omitted issues remain unchanged, so an incomplete model response cannot implicitly close them. Historical contents and transitions remain in the archive. The projection uses one shared background heading rather than copying that heading into every issue.

The extraction protocol supports `create`, `keep`, `update`, `resolve`, and `promote` actions. Existing issues are addressed by `continuation_id`; creating an issue does not accept a model-invented ID. `create`, `update`, and `promote` require ranges with current non-checkpoint evidence. An already-settled issue can instead be resolved using its supplied ID and a reason grounded in its current state; `resolve` does not require new messages or ranges. The server binds that decision to the current text fingerprint, so a stale resolution cannot close a newer state. Promotion names a work-item binding in Python (`work_item=...`) or its page ID in JSON (`work_item_page_id`). Merely selecting or reading a work item is insufficient: its successful write must account for the continuation issue being transferred.

`keep` requires a reason naming what remains relevant: an outstanding action or question, a still-applicable constraint, a continuing commitment, or an unverified outcome. It does not require ranges or repeated summary text. Having no immediate next action does not settle a standing constraint or an item waiting for a response. Uncertain outcomes stay active; silence and omission are not completion evidence. For example, a delivered one-off answer with no remaining obligation can be resolved, while "do not push without approval" remains active even when no push is currently planned. Reasons are retained in the archive's transition ledger, not appended to the hot summary. Already-frozen extraction plans remain replayable, including older `keep` actions without a reason; newly generated actions follow the current contract.

Ordinary continuation updates come from the work-item extraction response (`sdk.continuation` in Python or `continuation_coverage` in JSON), without an additional summary call. Stable continuation identities are distinct from original message identities. Issues carry source references as assistant background and cannot authorize reopening terminal work. An explicit successful empty extraction result can leave new messages archive-only while retaining existing continuation issues. An exception, missing result, or extraction that never ran cannot advance the checkpoint merely because the raw archive exists.

### Idle continuation and cold storage

An item's lifecycle (`active`, `resolved`, or `promoted`) is separate from whether it is in working memory (`hot` or `cold`). Idle eviction only changes residency: it does not claim that an unfinished item has been completed. Each checkpoint references an archive-local `continuation-store.json` through `continuation_store_uri`. The store records the latest complete message for each stable continuation ID, lifecycle, residency, last related activity, protection and eviction reason. This is session storage, not a new vector-indexed memory type alongside work items. Historical archive snapshots and source messages remain available.

Idle eviction is enabled by default for work-item sessions. Its settings belong to the server's `memory` configuration:

```json
{
  "memory": {
    "continuation_ttl_enabled": true,
    "continuation_idle_turns": 30,
    "continuation_idle_days": 7,
    "continuation_min_idle_turns": 5
  }
}
```

An unprotected active item becomes cold after 30 user turns without related activity, or after seven days without related activity when at least five user turns have occurred. One actual user request and its assistant/tool processing count as one logical turn. Tool replies, archive splits and replayed extraction do not create extra user turns. Eviction is evaluated during checkpoint preparation after processing current evidence, rather than by a wall-clock timer. An inactive session is therefore not emptied merely because the user returns from a holiday. Existing items without recorded activity receive a migration grace period.

Only new related evidence advances activity: for example, a follow-up request, approval or relevant tool result. A `keep` action can cite such evidence in `ranges` without repeating the summary. Repeated `keep` without evidence, summary rewriting, being supplied as model background and reading stored history do not refresh the item. The server validates message identities and the model supplies semantic relevance. A structured `protection` value can exempt a still-applicable constraint, explicitly pinned item or ongoing commitment from idle eviction; it needs a concrete applicability reason and source ranges. A generic keep reason does not grant protection. This exemption does not bypass the total continuation budget.

Both extraction protocols accept `protection: {kind, reason, ranges}` on `create`, `update` and `keep`. The kinds are `constraint`, `pinned`, `commitment` and `none` (remove protection); `reason` states the concrete basis and applicable scope. Omission preserves existing protection. Normally ranges must include complete current non-checkpoint evidence. Initial registration of a previously unprotected historical item may instead cite its own complete checkpoint, with a matching fingerprint; that exception does not refresh activity. Removing protection always requires fresh evidence.

The server enforces registered protection; it does not infer protection from summary keywords. A model can still omit a constraint's protection or incorrectly resolve unfinished work. Having no long-term task, deadline or protection requirement does not establish completion. Original evidence and state history remain archived, but these semantic decisions still need observation with the configured model.

The hot view carries one cold-store recovery reference rather than one stub per archived item. When a user resumes an old subject, extraction can inspect at most three complete active/cold candidates within 2,000 estimated tokens. Exact IDs in the latest real user request or its tool results take priority; otherwise conservative English-word/Chinese-bigram matching uses that request. This is lexical candidate selection, not vector retrieval or guaranteed semantic recall. Greetings and broad tool-log keyword matches do not trigger it. A candidate is only background: restoring it requires an explicit `keep` or `update` using the same ID and relevant fresh evidence. Merely reading it or omitting it from extraction leaves it cold. Resolved and promoted entries are not automatically restored from old snapshots.

Candidate bodies are never truncated. An entry larger than the recall budget stays in storage even when referenced by exact ID; the agent can use the recovery reference to read it directly. The recovery hint asks the agent to consult the latest stored state before related work, but it is not an execution gate. The model can still miss a match or misjudge relevance.

When several successfully written items account for different chunks of one raw message, their ranges are combined before classifying the whole message. Failed writes contribute no coverage. Each tool input/output field retains its first 2,000 characters plus a truncation notice when needed. The total tool-evidence budget is 16,000 estimated tokens, prioritizing recent results. Partial previews remain eligible for work-item attribution and continuation classification. State updates may use visible evidence, but must not invent unseen outcomes or equate a finished tool with a finished task; unclear outcomes retain pending verification and references. Ranges and classifications are model-provided attribution: this ledger does **not** formally prove that every continuation-relevant fact was preserved. Archive-only information remains recoverable from storage, but the model can still fail to select an important new detail for the hot view. Storage preservation is not a guarantee of omission-free hot memory.

Before applying work-item writes, the archive saves operations with allocated IDs in existing metadata. Retries preserve task identities, the original message batch, source attribution, and a separate snapshot of the continuation background used to interpret ranges. New batches receive the latest folded continuation even when earlier evidence has already been extracted; replayed batches retain their frozen background. Ordinary-memory extraction does not receive this extra background. Each successful write has its own receipt, so a crash before batch progress is saved, or a subsequent edit from another session, does not cause that write to be repeated. Promotion requires the current extraction plan's successful write receipt; a historical write to the same task cannot confirm a newer transfer. The canonical file carries only one pending receipt; historical receipts live under the originating archive's `work-item-receipts/`, keeping canonical metadata bounded.

After a version conflict, the next background retry reads only the conflicted items' latest canonical states and re-extracts their updates using the original evidence. Completed writes, existing continuation classifications, and task identities are preserved. The revised plan must be saved before another version-checked write; later archives inherit the highest revision. Each attempt performs at most one reconciliation pass, retaining progress for another retry if concurrent edits cause another conflict. A batch is complete only after all planned operations succeed. Persistent model or storage failures, or continued contention, still fail safely without overwriting newer state or publishing an incomplete checkpoint.

Completed extraction and a ready checkpoint are separate states. If the combined continuation exceeds its budget, or a previous checkpoint still carries legacy raw residual, a separate background repair pass compacts that continuation. This exceptional path can call the LLM; normal projection, context reads, archive reads, and the Pi compact hook do not. Repair retries reuse completed canonical writes. Successful repairs and fallback results are persisted for reuse after later publication failures.

When the model call fails or returns invalid or oversized state, fallback and idle eviction share the continuation store. Complete state must be durably saved before omitted entries can leave the hot view. Budget fallback retains whole entries that fit, prioritizing their most recent source messages and moving older entries out first. It never truncates characters within an entry. If every entry is too large, only the recovery notice remains. Headings, source references, and the notice all count toward continuation and total WM budgets. Archive or provenance writes that fail, or a budget too small for the notice, still prevent publication.

Budget fallback sets `continuation_degraded: true`; normal idle eviction alone does not. Context, archive and Pi compact views preserve the store recovery reference. Older checkpoints with `pending_continuation_uri` and linked `continuation-overflow.json` snapshots remain readable: the new store preserves that unresolved chain as `legacy_pending_continuation_uri` rather than automatically merging old snapshots into current state. Reading a snapshot does not automatically clear pending recovery or refresh activity. Only the latest item state determines whether it may return to the hot view.

The archive builds the bounded projection and publishes `.done` last via a temporary file and rename. An overview alone is not a ready checkpoint. Before an unfinished item leaves the hot projection, its vector record must cover the required canonical version. Terminal items still index asynchronously but do not block publication. This is a per-item cold-eviction check, not a global embedding barrier; active items do not wait for unrelated indexing. Canonical/archive storage and vector discovery serve different purposes.

Pending and failed archives after the last ready checkpoint remain raw continuation. If refreshing canonical state cannot produce a valid view, the server withholds a usable checkpoint and retains raw history for fallback. If a requested context budget is insufficient, the response is `budget_insufficient` with no checkpoint boundary; the caller must keep its transcript or use a larger budget.

Existing local and cuvs vector collections receive the `work_item_version` field through normal schema migration. Work-item session creation and commits verify that the actual collection schema contains this int64 field; missing, incompatible, or unverifiable metadata returns `FAILED_PRECONDITION` before archive creation. Legacy mode remains available. Existing remote collections require an out-of-band migration (default 0 for the added field) and reindexing of work items. Volcengine API-key data-plane metadata is a local expected schema, so it cannot pass this check; work-item mode requires a connection with actual collection-schema access, such as AK/SK control-plane access.

Pi uses two fallback steps when compaction is requested:

1. Reuse a valid earlier checkpoint plus the complete raw Pi transcript through the requested cut; Pi retains the tail after that cut.
2. If readiness, branch continuity, refresh, or the complete context budget check fails, use Pi's normal compaction.

The hook refreshes a cached checkpoint once and does not add an LLM call or wait for indexing/readiness. A late checkpoint cannot overwrite a newer Pi compaction.

V1 provides no dedicated proactive work-item write API, dependency graph, automatic migration of legacy sessions, or guarantee of perfect semantic matching. Generic body writes cannot bypass work-item update checks. Cold work remains discoverable through existing recall; original evidence can be recovered by listing session history and reading the relevant archive's `messages.jsonl` and referenced tool results in chunks.
