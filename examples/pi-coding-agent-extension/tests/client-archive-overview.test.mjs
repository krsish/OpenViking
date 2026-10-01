import test from "node:test";
import assert from "node:assert/strict";
import { OVClient } from "../client.ts";

function makeClient() {
  return new OVClient({
    endpoint: "http://127.0.0.1:1933",
    apiKey: "",
    account: "",
    user: "",
    authMode: "trusted",
    sendIdentityHeaders: false,
    peerId: "",
    userAgent: "test",
  });
}

test("createSession sends the configured working-memory policy", async () => {
  await withFetch(async () => ({ body: { status: "ok", result: { session_id: "s" } } }), async (calls) => {
    const client = makeClient();
    client.cfg.workingMemoryMode = "work_item";
    assert.equal((await client.createSession("s")).ok, true);
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      session_id: "s", memory_policy: { working_memory: { mode: "work_item" } },
    });
  });
});

test("readArchiveCheckpoint requires a matching published archive and never reads raw overview", async () => {
  const valid = { archive_id: "archive_007", overview: "---\ntitle: view\n---\n\nReady hot view",
    checkpoint: { mode: "work_item", version: 1, compact_ready: true, archive_id: "archive_007",
      starting_message_id: "s1", ending_message_id: "s2", work_items: [] } };
  for (const [result, ready] of [
    [valid, true],
    [{ ...valid, checkpoint: { ...valid.checkpoint, continuation_version: 2 } }, true],
    [{ ...valid, status: "not_ready" }, false],
    [{ ...valid, archive_id: "archive_006" }, false],
    [{ ...valid, checkpoint: { ...valid.checkpoint, archive_id: "archive_006" } }, false],
    [{ ...valid, checkpoint: { ...valid.checkpoint, compact_ready: false } }, false],
    [{ ...valid, checkpoint: { ...valid.checkpoint, mode: "legacy" } }, false],
    [{ ...valid, overview: "" }, false],
    [{ overview: "Exists without marker" }, false],
  ]) {
    await withFetch(async () => ({ body: { status: "ok", result } }), async (calls) => {
      const value = await makeClient().readArchiveCheckpoint("viking://user/u/sessions/s/history/archive_007");
      assert.equal(Boolean(value), ready);
      if (ready) assert.equal(value.overview, "Ready hot view");
      assert.equal(calls.length, 1);
      assert.match(calls[0].url, /\/sessions\/s\/archives\/archive_007$/);
    });
  }
});

test("archive checkpoint read errors do not fall through to overview readiness", async () => {
  await withFetch(async () => ({ status: 404, body: { status: "error" } }), async (calls) => {
    assert.equal(await makeClient().readArchiveCheckpoint("viking://user/u/sessions/s/history/archive_007"), null);
    assert.equal(calls.length, 1);
  });
  await withFetch(async () => ({ status: 500, body: { status: "error", error: { message: "storage" } } }), async () => {
    await assert.rejects(() => makeClient().readArchiveCheckpoint("viking://user/u/sessions/s/history/archive_007"), /storage/);
  });
});

async function withFetch(handler, fn) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const value = await handler(url, init);
    return new Response(JSON.stringify(value.body), {
      status: value.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    return await fn(calls);
  } finally {
    globalThis.fetch = original;
  }
}

test("readArchiveOverview reads only the requested archive and strips OKF frontmatter", async () => {
  await withFetch(async () => ({
    body: { status: "ok", result: "---\ntitle: archive\n---\n\n# Working Memory\nbody\n" },
  }), async (calls) => {
    const uri = "viking://user/u/sessions/s/history/archive_007";
    assert.equal(await makeClient().readArchiveOverview(uri), "# Working Memory\nbody\n");
    assert.equal(calls.length, 1);
    assert.ok(calls[0].url.includes(encodeURIComponent(`${uri}/.overview.md`)));
  });
});

test("readArchiveOverview treats 404 and empty bodies as not ready", async () => {
  await withFetch(async () => ({ status: 404, body: { status: "error", error: { message: "missing" } } }), async () => {
    assert.equal(await makeClient().readArchiveOverview("viking://archive/1"), null);
  });
  await withFetch(async () => ({ body: { status: "ok", result: "---\ntitle: empty\n---\n\n" } }), async () => {
    assert.equal(await makeClient().readArchiveOverview("viking://archive/1"), null);
  });
});

test("readArchiveOverview surfaces non-404 read failures", async () => {
  await withFetch(async () => ({ status: 500, body: { status: "error", error: { message: "storage failed" } } }), async () => {
    await assert.rejects(
      () => makeClient().readArchiveOverview("viking://archive/1"),
      /archive overview read failed: storage failed/,
    );
  });
});

test("getArchiveState reads the server's terminal markers for one archive", async () => {
  const archive = "viking://user/u/sessions/s/history/archive_003";
  const files = new Map();
  await withFetch(async (url) => {
    const uri = new URL(String(url)).searchParams.get("uri");
    if (uri === "viking://user/u/sessions/broken/history/archive_001/.done") {
      return { status: 500, body: { status: "error", error: { code: "INTERNAL", message: "boom" } } };
    }
    return files.has(uri)
      ? { body: { status: "ok", result: files.get(uri) } }
      : { status: 404, body: { status: "error", error: { code: "NOT_FOUND", message: "missing" } } };
  }, async (calls) => {
    const client = makeClient();
    assert.equal(await client.getArchiveState(archive), "pending");
    files.set(`${archive}/.failed.json`, "{\"error\":\"llm\"}");
    assert.equal(await client.getArchiveState(archive), "failed");
    files.set(`${archive}/.done`, "{\"working_memory_enabled\":false}");
    assert.equal(await client.getArchiveState(`${archive}/`), "completed");
    assert.equal(await client.getArchiveState("viking://user/u/sessions/broken/history/archive_001"), null);
    assert.equal(await client.getArchiveState(""), null);
    assert.match(calls[0].url, /content\/read\?uri=.*archive_003%2F\.done$/);
  });
});
