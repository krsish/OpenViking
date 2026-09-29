import test from "node:test";
import assert from "node:assert/strict";
import { TakeoverCore, TAKEOVER_ENTRY_TYPE, estimateTokens } from "../lib/takeover-core.mjs";

const archive = (n) => `viking://user/u/sessions/s/history/archive_00${n}`;
const checkpoint = {
  mode: "work_item", version: 1, compact_ready: true,
  starting_message_id: "ov-start", ending_message_id: "ov-end",
  work_items: [{ uri: "viking://user/u/memories/work_items/a.md", version: 2 }],
};
const message = (id, role, content) => ({ id, type: "message", message: { role, content, timestamp: Number(id.slice(1)) } });
const branch = () => [
  message("m1", "user", "Work on A"), message("m2", "assistant", "A is waiting for review"),
  message("m3", "user", "Now work on B"),
  message("m4", "assistant", [{ type: "toolCall", id: "call-1", name: "read", arguments: { path: "/tmp/B" } }]),
  { ...message("m5", "toolResult", [{ type: "text", text: "Complete result — no preview" }]),
    message: { role: "toolResult", toolCallId: "call-1", toolName: "read", isError: false,
      content: [{ type: "text", text: "Complete result — no preview" }], details: { line: 48 }, timestamp: 5 } },
  message("m6", "user", "Continue B"), message("m7", "assistant", "B is in progress"),
];
const prep = (overrides = {}) => ({ firstKeptEntryId: "m6", contextWindow: 16000,
  reserveTokens: 1000, overheadTokens: 200, tokensBefore: 14000, ...overrides });

function makeCore(overrides = {}) {
  const calls = { checkpoint: [], overview: 0, commit: 0, sleep: 0, persist: [] };
  const core = new TakeoverCore({
    config: { workingMemoryMode: "work_item", takeoverTokenThreshold: 10,
      takeoverKeepRecentTurns: 1, takeoverOverviewBudget: 1000, ...overrides.config },
    io: {
      readArchiveCheckpoint: async (uri) => {
        calls.checkpoint.push(uri);
        return overrides.readCheckpoint ? overrides.readCheckpoint(uri) :
          uri === archive(1) ? { overview: "A: waiting for review.", checkpoint } : null;
      },
      readArchiveOverview: async () => { calls.overview++; return "unpublished overview"; },
      archiveState: async () => overrides.archiveState || "pending",
      commit: async () => { calls.commit++; return { status: "accepted", archive_uri: archive(3) }; },
      captureCount: (entries) => entries.filter((entry) => entry.type === "message").length,
      persistEntry: (_type, data) => calls.persist.push(data),
      sleep: async () => { calls.sleep++; },
      ...overrides.io,
    },
  });
  return { core, calls };
}

function restoreReady(core, { pending = true, data = {} } = {}) {
  core.restore([{ type: "custom", customType: TAKEOVER_ENTRY_TYPE, data: {
    workingMemoryMode: "work_item", readyCheckpoint: checkpoint,
    coveredThroughEntryId: "m2", coveredUserTurns: 1, overview: "A: waiting for review.",
    pendingTokens: 200, archiveUri: archive(1), historyUri: "viking://user/u/sessions/s/history",
    pendingArchive: pending ? { archiveUri: archive(2), taskId: "task2", historyUri: "",
      coveredThroughEntryId: "m5", coveredUserTurns: 2, frozenTokens: 100 } : null,
    ...data,
  } }]);
}

test("pending checkpoint uses previous ready prefix and complete uncovered tool transcript", async () => {
  const { core, calls } = makeCore();
  restoreReady(core);
  const result = await core.handleBeforeCompact(prep(), branch());
  assert.ok(result);
  assert.equal(result.compaction.firstKeptEntryId, "m6");
  assert.match(result.compaction.summary, /A: waiting for review/);
  assert.match(result.compaction.summary, /"id":"m3"/);
  assert.match(result.compaction.summary, /"arguments":\{"path":"\/tmp\/B"\}/);
  assert.match(result.compaction.summary, /"toolCallId":"call-1"/);
  assert.match(result.compaction.summary, /"details":\{"line":48\}/);
  assert.doesNotMatch(result.compaction.summary, /Continue B/);
  assert.deepEqual(calls.checkpoint, [archive(2), archive(1)]);
  assert.equal(calls.commit, 0);
  assert.equal(calls.overview, 0);
  assert.equal(calls.sleep, 0);
  assert.equal(core.state.pendingArchive.nativeCompaction, true);
});

test("uncovered tail is never truncated to force a budget fit", async () => {
  const { core } = makeCore();
  restoreReady(core);
  const entries = branch();
  entries[4].message.content[0].text = "x".repeat(100000);
  assert.equal(await core.handleBeforeCompact(prep(), entries), undefined);
  assert.equal(core.state.coveredThroughEntryId, "m2");
  assert.equal(core.state.pendingArchive.nativeCompaction, true);
});

test("uncaptured custom and system context before the checkpoint is preserved", async () => {
  const { core } = makeCore();
  restoreReady(core);
  const entries = [message("s0", "system", "System constraint"),
    { id: "custom1", type: "custom_message", content: "Extension context", customType: "notice" },
    { id: "branch1", type: "branch_summary", summary: "Earlier branch result" }, ...branch()];
  const result = await core.handleBeforeCompact(prep(), entries);
  assert.match(result.compaction.summary, /System constraint/);
  assert.match(result.compaction.summary, /Extension context/);
  assert.match(result.compaction.summary, /Earlier branch result/);
});

test("ordinary work-item context replacement preserves uncaptured and unknown roles", () => {
  const { core } = makeCore();
  restoreReady(core);
  const extra = [
    { role: "system", content: "System rule" },
    { role: "custom", content: "Extension instruction" },
    { role: "branchSummary", content: "Earlier branch decision" },
    { role: "compactionSummary", content: "Earlier compacted facts" },
    { role: "futureExtensionRole", content: "Unknown context must survive" },
  ];
  const entries = branch();
  const messages = [...extra, ...entries.map((entry) => entry.message)];
  const result = core.transformContext(messages, entries);
  assert.deepEqual(result.slice(0, extra.length), extra);
  assert.equal(result.some((message) => message.content === "Work on A"), false);
  assert.equal(result.at(-1), messages.at(-1));
});

test("budget includes Pi retained tail, system/tools overhead and output reserve", async () => {
  for (const kind of ["retained", "overhead", "reserve", "unknown"]) {
    const { core } = makeCore();
    restoreReady(core);
    const entries = branch();
    const preparation = prep();
    if (kind === "retained") entries[6].message.content = "x".repeat(100000);
    if (kind === "overhead") preparation.overheadTokens = 16000;
    if (kind === "reserve") preparation.reserveTokens = 16000;
    if (kind === "unknown") delete preparation.contextWindow;
    assert.equal(await core.handleBeforeCompact(preparation, entries), undefined, kind);
  }
});

test("missing requested cut and wrong-branch ready boundary fail closed", async () => {
  const { core, calls } = makeCore();
  restoreReady(core);
  assert.equal(await core.handleBeforeCompact(prep({ firstKeptEntryId: "other-branch" }), branch()), undefined);
  assert.equal(calls.checkpoint.length, 0);
  const changed = branch().map((entry) => entry.id === "m2" ? { ...entry, id: "other2" } : entry);
  assert.equal(await core.handleBeforeCompact(prep(), changed), undefined);
});

test("newly ready pending checkpoint wins over the older prefix", async () => {
  const { core, calls } = makeCore({ readCheckpoint: () => ({ overview: "A reviewed; B current.", checkpoint }) });
  restoreReady(core);
  const result = await core.handleBeforeCompact(prep(), branch());
  assert.match(result.compaction.summary, /A reviewed; B current/);
  assert.doesNotMatch(result.compaction.summary, /Uncheckpointed Pi transcript/);
  assert.equal(result.compaction.details.archiveUri, archive(2));
  assert.equal(core.state.pendingArchive, null);
  assert.equal(calls.sleep, 0);
});

test("publication between not-ready read and completed marker gets one bounded checkpoint recheck", async () => {
  let reads = 0;
  const { core, calls } = makeCore({ archiveState: "completed", readCheckpoint: () =>
    ++reads === 1 ? null : { overview: "Published just now", checkpoint } });
  restoreReady(core);
  assert.equal(await core.resumePending(branch()), true);
  assert.equal(core.state.overview, "Published just now");
  assert.equal(core.state.pendingArchive, null);
  assert.deepEqual(calls.checkpoint, [archive(2), archive(2)]);
  assert.equal(calls.sleep, 0);
});

test("publication race keeps pending when there is no budget for the second checkpoint read", async () => {
  let now = 0;
  const { core, calls } = makeCore({
    readCheckpoint: () => { now += 5000; return null; },
    io: { now: () => now, archiveState: async () => { now += 10000; return "completed"; } },
  });
  restoreReady(core);
  assert.equal(await core.resumePending(branch(), { deadline: 19000 }), false);
  assert.equal(core.state.pendingArchive.archiveUri, archive(2));
  assert.deepEqual(calls.checkpoint, [archive(2)]);
});

test("ordinary threshold commit does not advance on unpublished overview", async () => {
  const { core, calls } = makeCore();
  assert.equal(await core.onTurnSynced(100, branch()), false);
  assert.equal(core.state.coveredThroughEntryId, "");
  assert.equal(core.state.pendingArchive.archiveUri, archive(3));
  assert.equal(calls.overview, 0);
  assert.equal(calls.sleep, 0);
  assert.equal(calls.commit, 1);
});

test("ordinary threshold commit accepts one ready publication and persists its provenance", async () => {
  const { core, calls } = makeCore({ readCheckpoint: () => ({ overview: "A done. B next.", checkpoint }) });
  assert.equal(await core.onTurnSynced(100, branch()), true);
  assert.equal(core.state.coveredThroughEntryId, "m5");
  assert.equal(calls.persist.at(-1).readyCheckpoint.ending_message_id, "ov-end");
  assert.equal(calls.persist.at(-1).workingMemoryMode, "work_item");
  assert.equal(calls.checkpoint.length, 1);
});

test("native hook can enqueue existing phase-1 commit and fall back to the previous ready prefix", async () => {
  const { core, calls } = makeCore();
  restoreReady(core, { pending: false });
  const result = await core.handleBeforeCompact(prep(), branch());
  assert.ok(result);
  assert.equal(calls.commit, 1);
  assert.equal(calls.checkpoint.length, 2);
  assert.equal(calls.sleep, 0);
  assert.equal(core.state.pendingArchive.archiveUri, archive(3));
});

test("late checkpoint after successful OV compaction is observed without installing a boundary", async () => {
  let ready = false;
  const { core } = makeCore({ readCheckpoint: (uri) => uri === archive(1)
    ? { overview: "A: waiting for review.", checkpoint } : ready ? { overview: "Late state", checkpoint } : null });
  restoreReady(core);
  assert.ok(await core.handleBeforeCompact(prep(), branch()));
  ready = true;
  assert.equal(await core.resumePending(branch()), false);
  assert.equal(core.state.coveredThroughEntryId, "");
  assert.equal(core.state.overview, "A: waiting for review.");
  assert.equal(core.state.pendingArchive, null);
});

test("late checkpoint after Pi fallback cannot replace Pi's new compaction", async () => {
  let ready = false;
  const { core } = makeCore({ readCheckpoint: () => ready ? { overview: "Late state", checkpoint } : null });
  restoreReady(core);
  assert.equal(await core.handleBeforeCompact(prep({ contextWindow: 20 }), branch()), undefined);
  // Keep the old covered entry deliberately: entry existence alone is not a
  // sufficient guard against installing an old boundary over a new summary.
  const compacted = [...branch(), { id: "c1", type: "compaction", firstKeptEntryId: "m2", summary: "Pi's new summary" }];
  ready = true;
  assert.equal(await core.resumePending(compacted), false);
  const messages = [{ role: "compactionSummary", content: "Pi's new summary" }, ...branch().slice(1).map((entry) => entry.message)];
  assert.deepEqual(core.transformContext(messages, compacted), messages);
  assert.equal(core.state.overview, "A: waiting for review.");
});

test("restoring a legacy overview never makes a work-item checkpoint ready", async () => {
  const { core } = makeCore();
  restoreReady(core, { data: { workingMemoryMode: "legacy", readyCheckpoint: undefined } });
  assert.equal(core.state.coveredThroughEntryId, "");
  assert.equal(core.state.overview, "");
  assert.equal(await core.handleBeforeCompact(prep(), branch()), undefined);
});

test("malformed or oversized checkpoint never advances a boundary", async () => {
  for (const value of [
    { overview: "draft", checkpoint: { ...checkpoint, compact_ready: false } },
    { overview: "draft", checkpoint: { ...checkpoint, version: 2 } },
    { overview: "draft", checkpoint: { ...checkpoint, ending_message_id: "" } },
    { overview: "x".repeat(10000), checkpoint },
  ]) {
    const { core } = makeCore({ readCheckpoint: () => value });
    assert.equal(await core.onTurnSynced(100, branch()), false);
    assert.equal(core.state.overview, "");
  }
});

test("insufficient read budget cannot reuse an unchecked cached checkpoint", async () => {
  const { core, calls } = makeCore({ io: { now: () => 1000 } });
  restoreReady(core);
  assert.equal(await core.handleBeforeCompact(prep(), branch(), { deadline: 2000 }), undefined);
  assert.equal(calls.checkpoint.length, 0);
});

test("compact refreshes another session's canonical state while preserving checkpoint coverage", async () => {
  const { core, calls } = makeCore({ readCheckpoint: (uri) => uri === archive(1) ? {
    overview: "A: completed and verified in another session.",
    checkpoint: { ...checkpoint, work_items: [{ ...checkpoint.work_items[0], version: 3 }] },
  } : null });
  restoreReady(core);
  const result = await core.handleBeforeCompact(prep(), branch());
  assert.match(result.compaction.summary, /completed and verified in another session/);
  assert.doesNotMatch(result.compaction.summary, /waiting for review/);
  assert.match(result.compaction.summary, /Complete result — no preview/);
  assert.deepEqual(calls.checkpoint, [archive(2), archive(1)]);
  assert.equal(core.state.readyCheckpoint.work_items[0].version, 3);
});

test("failed or changed-coverage refresh never falls back to a stale cached view", async () => {
  for (const value of [null, { overview: "mismatched coverage", checkpoint: { ...checkpoint, ending_message_id: "other-end" } }]) {
    const { core } = makeCore({ readCheckpoint: (uri) => uri === archive(1) ? value : null });
    restoreReady(core);
    assert.equal(await core.handleBeforeCompact(prep(), branch()), undefined);
    assert.equal(core.state.overview, "A: waiting for review.");
  }
});

test("ready checkpoint and budget accounting survive a restart", async () => {
  const { core } = makeCore();
  restoreReady(core);
  const state = core.persistedState();
  const { core: resumed } = makeCore();
  resumed.restore([{ type: "custom", customType: TAKEOVER_ENTRY_TYPE, data: state }]);
  const result = await resumed.handleBeforeCompact(prep(), branch());
  assert.ok(result);
  assert.ok(result.compaction.estimatedTokensAfter > estimateTokens(result.compaction.summary));
});
