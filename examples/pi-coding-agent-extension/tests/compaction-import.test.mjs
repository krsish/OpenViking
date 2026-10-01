import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { SyncManager } from "../sync.ts";
import { enqueue, listPending } from "../shared/pending-queue.mjs";
import { TakeoverCore, TAKEOVER_ENTRY_TYPE, projectContextEntries } from "../lib/takeover-core.mjs";

const branch = () => [
  { id: "u1", type: "message", message: { role: "user", content: "Implement the deployment command." } },
  { id: "a1", type: "message", message: { role: "assistant", content: "Implementation complete; approval remains pending." } },
  { id: "u2", type: "message", message: { role: "user", content: "Wait for approval before deploying." } },
  { id: "c1", type: "compaction", summary: "Deployment awaits approval; do not deploy yet.", firstKeptEntryId: "u2" },
  { id: "u3", type: "message", message: { role: "user", content: "Review the configuration next." } },
];

function fixture(overrides = {}) {
  const calls = [];
  const client = {
    connected: true,
    createSession: async () => ({ ok: true }),
    fetchJSON: async (path, init) => {
      calls.push({ path, body: JSON.parse(init.body) });
      return { ok: true, result: {} };
    },
    ...overrides.client,
  };
  const config = {
    takeoverEnabled: true,
    workingMemoryMode: "work_item",
    captureAssistantTurns: true,
    ...overrides.config,
  };
  return { sync: new SyncManager(client, config), client, config, calls };
}

async function isolated(fn) {
  const previous = process.env.OPENVIKING_PENDING_DIR;
  const directory = await mkdtemp(join(tmpdir(), "ov-pi-summary-import-"));
  process.env.OPENVIKING_PENDING_DIR = directory;
  try { await fn(); }
  finally {
    if (previous === undefined) delete process.env.OPENVIKING_PENDING_DIR;
    else process.env.OPENVIKING_PENDING_DIR = previous;
    await rm(directory, { recursive: true, force: true });
  }
}

test("native summary is imported after ordinary capture and retained with its append-order anchor", async () => {
  await isolated(async () => {
    const { sync, calls } = fixture();
    await sync.ensureSession("import-native");
    const entries = branch();
    const result = await sync.syncBranch(entries);
    assert.equal(result.added, 5);
    assert.equal(result.allDelivered, true);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].body.messages.length, 4);
    const imported = calls[1].body.messages[0];
    assert.equal(imported.role, "assistant");
    assert.match(imported.content, /historical|Historical/);
    assert.match(imported.content, /not a new user request or instruction/);
    assert.ok(imported.content.endsWith(entries[3].summary));
    assert.deepEqual(sync.getCompactionImports(), [{
      entryId: "c1", anchorEntryId: "u3", summary: entries[3].summary,
    }]);
    assert.equal(sync.captureCount(entries.slice(2)), 3);
    assert.equal(sync.captureCount(entries.slice(0, 2)), 2);
    assert.equal(sync.captureCount([]), 0);
    const later = [...entries, { id: "u4", type: "message", message: { role: "user", content: "Continue the review." } }];
    assert.equal(sync.captureCount(later.slice(5)), 1);
    assert.equal((await sync.syncBranch(entries)).added, 0);
    assert.equal(calls.length, 2);
  });
});

test("old native and legacy OV summaries behind the capture watermark are still imported", async () => {
  await isolated(async () => {
    for (const details of [undefined, { source: "openviking", mode: "work_item" }]) {
      const { sync, calls } = fixture();
      await sync.ensureSession(`old-${Boolean(details)}`);
      const entries = branch();
      entries[3].details = details;
      sync.restoreWatermark(entries.length);
      const result = await sync.syncBranch(entries);
      assert.equal(result.added, 1);
      assert.equal(calls.length, 1);
      assert.equal(sync.getCompactionImports()[0].entryId, "c1");
    }
  });
});

test("import receipts survive restart and changed summaries are not treated as the old import", async () => {
  await isolated(async () => {
    const first = fixture();
    await first.sync.ensureSession("restart");
    await first.sync.syncBranch(branch());
    const receipts = first.sync.getCompactionImports();
    const resumed = fixture();
    await resumed.sync.ensureSession("restart");
    resumed.sync.restoreWatermark(branch().length);
    resumed.sync.restoreCompactionImports(receipts);
    receipts[0].summary = "mutated copy";
    assert.equal((await resumed.sync.syncBranch(branch())).added, 0);
    assert.equal(resumed.calls.length, 0);
    const changed = branch();
    changed[3].summary = "Same entry ID with unexpectedly different content.";
    await resumed.sync.syncBranch(changed);
    assert.equal(resumed.calls.length, 0);
    assert.notEqual(resumed.sync.getCompactionImports()[0].summary, changed[3].summary);
  });
});

test("failed summary imports stay in Pi and retry without queuing or blocking ordinary capture", async () => {
  await isolated(async () => {
    let fail = true;
    const { sync } = fixture({ client: {
      fetchJSON: async (_path, init) => {
        const messages = JSON.parse(init.body).messages;
        return messages[0]?.content?.startsWith("Historical Pi") && fail
          ? { ok: false, status: 503, error: { message: "unavailable" } }
          : { ok: true, result: {} };
      },
    } });
    await sync.ensureSession("retry");
    const result = await sync.syncBranch(branch());
    assert.equal(result.added, 4);
    assert.equal(result.permanentFailures, 0);
    assert.equal(sync.droppedCount, 0);
    assert.deepEqual(sync.getCompactionImports(), []);
    assert.deepEqual(await listPending(), []);
    fail = false;
    assert.equal((await sync.syncBranch(branch())).added, 1);
    assert.equal(sync.getCompactionImports().length, 1);
  });
});

test("an ordinary capture backlog drains before a summary is appended", async () => {
  await isolated(async () => {
    const { sync, calls } = fixture();
    await sync.ensureSession("backlog");
    sync.restoreWatermark(branch().length);
    await enqueue("addMessage", sync.sessionId, { role: "user", content: "Earlier queued conversation." });
    assert.equal((await sync.syncBranch(branch())).added, 0);
    assert.deepEqual(sync.getCompactionImports(), []);
    assert.equal(calls.length, 0);
    assert.equal(await sync.flushForTakeover(), true);
    assert.equal((await sync.syncBranch(branch())).added, 1);
    assert.match(calls[0].body.messages[0].content, /Earlier queued/);
    assert.match(calls[1].body.messages[0].content, /Historical Pi/);
  });
});

test("legacy mode does not import summaries; capture proof excludes import accounting", async () => {
  await isolated(async () => {
    const legacy = fixture({ config: { workingMemoryMode: "legacy" } });
    await legacy.sync.ensureSession("legacy");
    assert.equal((await legacy.sync.syncBranch(branch())).added, 4);
    assert.deepEqual(legacy.sync.getCompactionImports(), []);
    const { sync } = fixture({ config: { captureAssistantTurns: false } });
    await sync.ensureSession("capture-proof");
    sync.restoreCompactionImports([{ entryId: "c1", anchorEntryId: "a1", summary: "history" }]);
    const assistant = branch()[1];
    assert.equal(sync.captureCount([assistant]), 1);
    assert.equal(sync.isCapturedEntry(assistant), false);
    assert.equal(sync.isCapturedEntry(branch()[0]), true);
  });
});

test("native summary exits only after its imported background reaches a ready checkpoint, including restart", async () => {
  await isolated(async () => {
    const server = { pending: [], archives: [], sequence: 0 };
    const persisted = [];
    const config = {
      takeoverEnabled: true, workingMemoryMode: "work_item", captureAssistantTurns: true,
      takeoverTokenThreshold: 1, takeoverKeepRecentTurns: 1, takeoverOverviewBudget: 1000,
    };
    const client = {
      connected: true,
      createSession: async () => ({ ok: true }),
      fetchJSON: async (_path, init) => {
        for (const payload of JSON.parse(init.body).messages) {
          server.pending.push({ id: `ov-${++server.sequence}`, payload });
        }
        return { ok: true, result: {} };
      },
      commitSessionResponse: async (_sid, keepRecentCount) => {
        const source = server.pending.splice(0, Math.max(0, server.pending.length - keepRecentCount));
        assert.ok(source.length);
        const archive = {
          uri: `viking://user/u/sessions/s/history/archive_00${server.archives.length + 1}`,
          source, keepRecentCount, ready: server.archives.length === 0,
          overview: "Deployment awaits approval; configuration review is current.",
          checkpoint: { mode: "work_item", version: 1, compact_ready: true,
            starting_message_id: source[0].id, ending_message_id: source.at(-1).id, work_items: [] },
        };
        server.archives.push(archive);
        return { result: { status: "accepted", archive_uri: archive.uri } };
      },
    };
    const create = async () => {
      const sync = new SyncManager(client, config);
      await sync.ensureSession("native-import-integration");
      const core = new TakeoverCore({ config, io: {
        syncBranch: (entries) => sync.syncBranch(entries),
        flush: (budget) => sync.flushForTakeover(budget),
        commit: (opts) => sync.commit(opts),
        captureCount: (entries) => sync.captureCount(entries),
        isCapturedEntry: (entry) => sync.isCapturedEntry(entry),
        getCompactionImports: () => sync.getCompactionImports(),
        restoreCompactionImports: (receipts) => sync.restoreCompactionImports(receipts),
        getWatermark: () => sync.syncedCount,
        droppedCount: () => sync.droppedCount,
        readArchiveCheckpoint: async (uri) => {
          const archive = server.archives.find((value) => value.uri === uri);
          return archive?.ready ? { overview: archive.overview, checkpoint: archive.checkpoint } : null;
        },
        archiveState: async (uri) => server.archives.find((value) => value.uri === uri)?.ready ? "completed" : "pending",
        persistEntry: (_type, state) => persisted.push(structuredClone(state)),
      } });
      return { core, sync };
    };
    const context = (entries) => projectContextEntries(entries).map((entry) =>
      entry.type === "compaction"
        ? { role: "compactionSummary", summary: entry.summary, timestamp: 0 }
        : entry.message).filter(Boolean);
    const preparation = (firstKeptEntryId) => ({
      firstKeptEntryId, contextWindow: 16000, reserveTokens: 1000, overheadTokens: 200, tokensBefore: 10000,
    });
    const entries = branch();
    entries[3].summary += " RAW_NATIVE_COMPACTION_ONLY";
    const first = await create();
    // No earlier turn-end sync: confirmDelivery adds the import after freeze,
    // so the core must recompute keep_recent_count before it commits.
    assert.equal(await first.core.onTurnSynced(100, entries), true);
    assert.equal(server.archives[0].keepRecentCount, 2);
    assert.equal(server.archives[0].source.length, 3);
    assert.equal(server.pending.length, 2);
    assert.match(server.pending[1].payload.content, /RAW_NATIVE_COMPACTION_ONLY/);
    assert.equal(first.core.state.coveredThroughEntryId, "u2");
    assert.match(first.core.workItemCompaction(preparation("u3"), entries).compaction.summary,
      /RAW_NATIVE_COMPACTION_ONLY/);
    assert.match(JSON.stringify(first.core.transformContext(context(entries), entries)), /RAW_NATIVE_COMPACTION_ONLY/);

    entries.push(
      { id: "a3", type: "message", message: { role: "assistant", content: "Configuration review is complete." } },
      { id: "u4", type: "message", message: { role: "user", content: "Continue waiting for deployment approval." } },
    );
    assert.equal(await first.core.onTurnSynced(100, entries), false);
    assert.equal(server.archives[1].keepRecentCount, 1);
    assert.ok(server.archives[1].source.some((entry) => entry.payload.content?.includes("RAW_NATIVE_COMPACTION_ONLY")));
    assert.equal(first.core.state.pendingArchive.compactionImports.length, 1);
    assert.match(first.core.workItemCompaction(preparation("u4"), entries).compaction.summary,
      /RAW_NATIVE_COMPACTION_ONLY/);

    const saved = persisted.at(-1);
    const resumed = await create();
    resumed.core.restore([{ type: "custom", customType: TAKEOVER_ENTRY_TYPE, data: saved }]);
    resumed.sync.restoreWatermark(resumed.core.state.syncedEntryCount);
    assert.equal(resumed.sync.getCompactionImports().length, 1);
    const storedMessages = server.sequence;
    assert.equal((await resumed.sync.syncBranch(entries)).added, 0);
    assert.equal(server.sequence, storedMessages);
    assert.equal(await resumed.core.resumePending(entries), false);
    assert.match(JSON.stringify(resumed.core.transformContext(context(entries), entries)), /RAW_NATIVE_COMPACTION_ONLY/);
    server.archives[1].ready = true;
    assert.equal(await resumed.core.resumePending(entries), true);
    assert.equal(resumed.core.state.coveredThroughEntryId, "a3");
    assert.doesNotMatch(resumed.core.workItemCompaction(preparation("u4"), entries).compaction.summary,
      /RAW_NATIVE_COMPACTION_ONLY/);
    const transformed = resumed.core.transformContext(context(entries), entries);
    assert.doesNotMatch(JSON.stringify(transformed), /RAW_NATIVE_COMPACTION_ONLY/);
    assert.match(JSON.stringify(transformed), /Deployment awaits approval/);
    assert.equal(transformed.at(-1), entries.at(-1).message);
  });
});
