import test from "node:test";
import assert from "node:assert/strict";
import {
  TakeoverCore,
  TAKEOVER_ENTRY_TYPE,
  OVERVIEW_MARKER,
  estimateTokens,
  projectContextEntries,
} from "../lib/takeover-core.mjs";

const historyUri = "viking://user/u/sessions/s/history";
const archiveUri = (round) => `${historyUri}/archive_${String(round).padStart(3, "0")}`;
let timestamp = 0;
const message = (id, role, content, extra = {}) => ({
  id, type: "message", message: { role, content, timestamp: ++timestamp, ...extra },
});
const turn = (round) => [
  message(`u${round}`, "user", `Continue task ${round}`),
  message(`a${round}`, "assistant", `Progress ${round}`),
];
const preparation = (firstKeptEntryId, overrides = {}) => ({
  firstKeptEntryId, contextWindow: 16000, reserveTokens: 1000,
  overheadTokens: 200, tokensBefore: 14000, ...overrides,
});

function makeCore({ captureAssistantTurns = true } = {}) {
  const isCapturedEntry = (entry) => entry.type === "message" &&
    ["user", ...(captureAssistantTurns ? ["assistant"] : []), "toolResult", "tool_result", "tool"].includes(entry.message?.role);
  return new TakeoverCore({
    config: {
      workingMemoryMode: "work_item", takeoverKeepRecentTurns: 1,
      takeoverOverviewBudget: 1000,
    },
    io: {
      captureCount: (entries) => entries.filter(isCapturedEntry).length,
      isCapturedEntry,
    },
  });
}

function ready(core, branch, through, round, overview = `CURRENT_WM_${round}`, extra = {}) {
  core.restore([{ type: "custom", customType: TAKEOVER_ENTRY_TYPE, data: {
    workingMemoryMode: "work_item",
    readyCheckpoint: {
      mode: "work_item", version: 1, compact_ready: true,
      starting_message_id: `ov-start-${round}`, ending_message_id: `ov-end-${round}`,
      work_items: [{ uri: "viking://user/u/memories/work_items/task.md", version: round }],
    },
    readyCompactionEntryId: projectContextEntries(branch).find((entry) => entry.type === "compaction")?.id || "",
    coveredThroughEntryId: through, coveredUserTurns: 1,
    overview, archiveUri: archiveUri(round), historyUri,
    ...extra,
  } }]);
}

function compact(core, branch, firstKeptEntryId, round) {
  const result = core.workItemCompaction(preparation(firstKeptEntryId), branch);
  assert.ok(result, `OV compact should fit the fixed context budget in round ${round}`);
  // Pi serializes the result in its session log. Include that round trip so
  // retries and restarts cannot rely on references held in an in-memory cache.
  const entry = JSON.parse(JSON.stringify({
    id: `c${round}`, type: "compaction", ...result.compaction,
  }));
  branch.push(entry);
  return entry;
}

function contextOf(branch) {
  return projectContextEntries(branch).flatMap((entry) => {
    if (entry.type === "message") return [entry.message];
    if (entry.type === "compaction") return [{ role: "compactionSummary", summary: entry.summary, timestamp: 0 }];
    if (entry.type === "branch_summary") return [{ role: "branchSummary", summary: entry.summary, timestamp: 0 }];
    if (entry.type === "custom_message") return [{ role: "custom", content: entry.content, timestamp: 0 }];
    return [];
  });
}

function occurrences(text, marker) {
  return text.split(marker).length - 1;
}

test("100 OV compactions replace the previous WM and keep uncaptured entries once", (t) => {
  const core = makeCore();
  const branch = [
    message("s0", "system", "SYSTEM_MARKER: approval is required"),
    { id: "x0", type: "custom_message", customType: "notice", content: 'CUSTOM_MARKER: quoted "path" C:\\tmp\\file\nsecond line' },
    { id: "b0", type: "branch_summary", summary: "BRANCH_MARKER: earlier branch context" },
    ...turn(0), ...turn(1),
  ];
  const lengths = [];
  const persistedLengths = [];
  for (let round = 1; round <= 100; round++) {
    ready(core, branch, `a${round - 1}`, round);
    const entry = compact(core, branch, `u${round}`, round);
    for (const marker of [OVERVIEW_MARKER, "SYSTEM_MARKER", "CUSTOM_MARKER", "BRANCH_MARKER"]) {
      assert.equal(occurrences(entry.summary, marker), 1, `${marker} at round ${round}`);
    }
    assert.match(entry.summary, new RegExp(`CURRENT_WM_${round}(?:\\s|$)`));
    if (round > 1) assert.doesNotMatch(entry.summary, new RegExp(`CURRENT_WM_${round - 1}(?:\\s|$)`));
    lengths.push(estimateTokens(entry.summary));
    persistedLengths.push(JSON.stringify(entry).length);
    branch.push(...turn(round + 1));
  }
  assert.ok(Math.max(...lengths) - Math.min(...lengths) < 40, `summary token range: ${Math.min(...lengths)}..${Math.max(...lengths)}`);
  assert.ok(Math.max(...persistedLengths) - Math.min(...persistedLengths) < 300,
    "structured details must not move the recursive summary growth into session metadata");
  t.diagnostic(`100 rounds: summary ${Math.min(...lengths)}..${Math.max(...lengths)} estimated tokens; ` +
    `serialized entry ${Math.min(...persistedLengths)}..${Math.max(...persistedLengths)} bytes`);
});

test("a carried complete tool transcript leaves the summary once a later checkpoint covers it", () => {
  const core = makeCore();
  const branch = [
    ...turn(0),
    message("request", "user", "Inspect the pending task"),
    message("call", "assistant", [{ type: "toolCall", id: "read-1", name: "read", arguments: { path: "/tmp/full" } }]),
    message("result", "toolResult", [{ type: "text", text: "TOOL_RESULT_MARKER: full result" }], {
      toolCallId: "read-1", toolName: "read", details: { line: 48 }, isError: false,
    }),
    ...turn(1),
  ];
  ready(core, branch, "a0", 1);
  const first = compact(core, branch, "u1", 1);
  assert.equal(occurrences(first.summary, "TOOL_RESULT_MARKER"), 1);
  assert.match(first.summary, /"arguments":\{"path":"\/tmp\/full"\}/);
  assert.match(first.summary, /"toolCallId":"read-1"/);
  assert.match(first.summary, /"details":\{"line":48\}/);
  branch.push(...turn(2));
  ready(core, branch, "a1", 2);
  const second = compact(core, branch, "u2", 2);
  assert.doesNotMatch(second.summary, /TOOL_RESULT_MARKER|read-1|CURRENT_WM_1/);
  assert.match(second.summary, /CURRENT_WM_2/);
});

test("ordinary context replacement removes the old WM while preserving its uncaptured payload", () => {
  const core = makeCore();
  const branch = [
    { id: "x0", type: "custom_message", customType: "notice", content: "KEEP_CUSTOM_MARKER" },
    ...turn(0), ...turn(1),
  ];
  ready(core, branch, "a0", 1);
  compact(core, branch, "u1", 1);
  branch.push(...turn(2));
  ready(core, branch, "a1", 2);
  const output = core.transformContext(contextOf(branch), branch);
  const text = JSON.stringify(output);
  assert.doesNotMatch(text, /CURRENT_WM_1/);
  assert.equal(occurrences(text, "CURRENT_WM_2"), 1);
  assert.equal(occurrences(text, "KEEP_CUSTOM_MARKER"), 1);
  assert.ok(output.includes(branch.find((entry) => entry.id === "u2").message));
  assert.ok(output.includes(branch.find((entry) => entry.id === "a2").message));
});

test("capture-disabled assistant messages survive both ordinary context replacement and compaction", () => {
  const core = makeCore({ captureAssistantTurns: false });
  const branch = [
    message("u0", "user", "COVERED_USER_MARKER"),
    message("a0", "assistant", "UNCAPTURED_ASSISTANT_MARKER: review still needed"),
    ...turn(1),
  ];
  ready(core, branch, "a0", 1);
  const output = core.transformContext(contextOf(branch), branch);
  assert.ok(output.includes(branch[1].message), "the uncaptured assistant stays model-visible in its original role");
  assert.doesNotMatch(JSON.stringify(output), /COVERED_USER_MARKER/);
  assert.equal(occurrences(JSON.stringify(output), "UNCAPTURED_ASSISTANT_MARKER"), 1);

  // Other extensions may rewrite context content while preserving Pi's stable
  // timestamp. Keep that actual context message instead of recreating it.
  const edited = JSON.parse(JSON.stringify(contextOf(branch)));
  edited[1].content = "EDITED_ASSISTANT_MARKER";
  const editedOutput = core.transformContext(edited, branch);
  assert.ok(editedOutput.includes(edited[1]));
  assert.equal(occurrences(JSON.stringify(editedOutput), "EDITED_ASSISTANT_MARKER"), 1);

  const entry = compact(core, branch, "u1", 1);
  assert.equal(occurrences(entry.summary, "UNCAPTURED_ASSISTANT_MARKER"), 1);
  assert.doesNotMatch(entry.summary, /COVERED_USER_MARKER/);
});

test("structured compaction carry survives a session serialization and core restart", () => {
  const core = makeCore();
  const branch = [
    { id: "x0", type: "custom_message", customType: "notice", content: "RESTART_CUSTOM_MARKER" },
    ...turn(0), ...turn(1),
  ];
  ready(core, branch, "a0", 1);
  compact(core, branch, "u1", 1);
  branch.push(...turn(2));
  ready(core, branch, "a1", 2);
  const persistedBranch = JSON.parse(JSON.stringify(branch));
  const persistedState = JSON.parse(JSON.stringify(core.persistedState()));
  const restarted = makeCore();
  restarted.restore([{ type: "custom", customType: TAKEOVER_ENTRY_TYPE, data: persistedState }]);
  const next = compact(restarted, persistedBranch, "u2", 2);
  assert.doesNotMatch(next.summary, /CURRENT_WM_1/);
  assert.equal(occurrences(next.summary, "RESTART_CUSTOM_MARKER"), 1);
});

test("a checkpoint from another Pi branch cannot replace a compaction summary", () => {
  const core = makeCore();
  const branch = [...turn(0), ...turn(1)];
  ready(core, branch, "a0", 1);
  compact(core, branch, "u1", 1);
  branch.push(...turn(2));
  ready(core, branch, "a1", 2);
  const otherBranch = JSON.parse(JSON.stringify(branch));
  otherBranch.find((entry) => entry.id === "c1").id = "other-compaction";
  assert.equal(core.workItemCompaction(preparation("u2"), otherBranch), undefined);
  const messages = contextOf(otherBranch);
  assert.equal(core.transformContext(messages, otherBranch), messages);
});

test("matching readyCompactionEntryId does not discard an unimported native Pi summary", () => {
  const core = makeCore();
  const branch = [
    ...turn(0), ...turn(1),
    { id: "native", type: "compaction", firstKeptEntryId: "u1", summary: 'NATIVE_MARKER: remember "A"', details: {} },
    ...turn(2),
  ];
  const lengths = [];
  for (let round = 2; round <= 25; round++) {
    ready(core, branch, `a${round - 1}`, round);
    const entry = compact(core, branch, `u${round}`, round);
    assert.equal(occurrences(entry.summary, "NATIVE_MARKER"), 1);
    assert.equal(occurrences(entry.summary, OVERVIEW_MARKER), 1);
    lengths.push(estimateTokens(entry.summary));
    branch.push(...turn(round + 1));
  }
  assert.ok(Math.max(...lengths) - Math.min(...lengths) < 40);
});

test("native summary exits only once its import anchor is covered by the ready checkpoint", () => {
  const core = makeCore();
  const native = { id: "native", type: "compaction", firstKeptEntryId: "u1",
    summary: "NATIVE_IMPORT_MARKER: A needs approval", details: {} };
  const branch = [...turn(0), ...turn(1), native, ...turn(2)];
  const receipt = { entryId: native.id, anchorEntryId: "a2", summary: native.summary };
  ready(core, branch, "a1", 2, "CURRENT_WM_2", { readyCompactionImports: [receipt] });
  const pending = compact(core, branch, "u2", 2);
  assert.match(pending.summary, /NATIVE_IMPORT_MARKER/, "an import after the covered boundary is not yet safe to discard");
  branch.push(...turn(3));
  ready(core, branch, "a2", 3, "CURRENT_WM_3: A needs approval", { readyCompactionImports: [receipt] });
  const covered = compact(core, branch, "u3", 3);
  assert.doesNotMatch(covered.summary, /NATIVE_IMPORT_MARKER/);
  assert.match(covered.summary, /A needs approval/);
});

test("legacy OV summaries without structured carry remain readable across repeated compaction", () => {
  const core = makeCore();
  const branch = [
    ...turn(0), ...turn(1),
    { id: "legacy", type: "compaction", firstKeptEntryId: "u1",
      summary: 'LEGACY_MARKER: unparsed custom payload "approval required"',
      details: { source: "openviking", mode: "work_item", archiveUri: archiveUri(1) } },
    ...turn(2),
  ];
  const lengths = [];
  for (let round = 2; round <= 25; round++) {
    ready(core, branch, `a${round - 1}`, round);
    const entry = compact(core, branch, `u${round}`, round);
    assert.equal(occurrences(entry.summary, "LEGACY_MARKER"), 1);
    assert.equal(occurrences(entry.summary, OVERVIEW_MARKER), 1);
    lengths.push(estimateTokens(entry.summary));
    branch.push(...turn(round + 1));
  }
  assert.ok(Math.max(...lengths) - Math.min(...lengths) < 40);
});

test("invalid continuation metadata and foreign history never authorize dropping opaque context", () => {
  const mutations = {
    "unknown format": (entry) => { entry.details.continuation.version = 999; },
    "malformed entries": (entry) => { entry.details.continuation.entries = {}; },
    "foreign session": (entry) => { entry.details.archiveUri = "viking://user/u/sessions/other/history/archive_001"; },
    "rewritten summary": (entry) => { entry.summary += "\nAdded after generation"; },
    "unknown boundary": (entry) => { entry.details.continuation.coveredThroughEntryId = "missing"; },
  };
  for (const [label, mutate] of Object.entries(mutations)) {
    const core = makeCore();
    const branch = [
      { id: "x0", type: "custom_message", customType: "notice", content: "OPAQUE_CUSTOM_MARKER" },
      ...turn(0), ...turn(1),
    ];
    ready(core, branch, "a0", 1);
    const first = compact(core, branch, "u1", 1);
    assert.ok(first.details.continuation, "generated summaries must carry a versioned continuation");
    mutate(first);
    branch.push(...turn(2));
    ready(core, branch, "a1", 2);
    const next = core.workItemCompaction(preparation("u2"), branch);
    if (next) {
      assert.match(next.compaction.summary, /OPAQUE_CUSTOM_MARKER/, label);
      assert.match(next.compaction.summary, /CURRENT_WM_1/, `${label}: an unverified old view must stay opaque`);
    }
    const output = JSON.stringify(core.transformContext(contextOf(branch), branch));
    assert.match(output, /OPAQUE_CUSTOM_MARKER/, label);
    assert.match(output, /CURRENT_WM_1/, label);
  }
});

test("missing or rewritten original carried messages cannot silently become covered", () => {
  for (const change of ["missing", "rewritten"]) {
    const core = makeCore();
    const branch = [
      ...turn(0), message("uncovered", "user", "UNAVAILABLE_RAW_MARKER"), ...turn(1),
    ];
    ready(core, branch, "a0", 1);
    compact(core, branch, "u1", 1);
    const rawIndex = branch.findIndex((entry) => entry.id === "uncovered");
    if (change === "missing") branch.splice(rawIndex, 1);
    else branch[rawIndex].message.content = "Edited content with the same entry ID";
    branch.push(...turn(2));
    ready(core, branch, "a1", 2);
    const next = core.workItemCompaction(preparation("u2"), branch);
    if (next) assert.match(next.compaction.summary, /UNAVAILABLE_RAW_MARKER/, change);
    assert.match(JSON.stringify(core.transformContext(contextOf(branch), branch)), /UNAVAILABLE_RAW_MARKER/, change);
  }
});
