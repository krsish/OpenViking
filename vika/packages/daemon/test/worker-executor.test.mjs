// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { WorkerExecutor } from '../src/worker-executor.mjs';
import { PI_MCP_EXTENSION } from '../src/pi-runtime.mjs';

// A CLI stand-in: records its argv, cwd and the agent env, then prints the
// JSON lines the test gives it and exits with the given code.
function script(dir, name, source) {
  const file = path.join(dir, `${name}.cjs`);
  fs.writeFileSync(file, `#!/usr/bin/env node\n${source}`, { mode: 0o755 });
  return file;
}

function fakeCli(dir, name, lines, code = 0) {
  return script(dir, name, `
const fs = require('node:fs');
const requests = [];
function record(stdin) {
  fs.appendFileSync(${JSON.stringify(path.join(dir, `${name}.calls`))}, JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), stdin, requests,
    mcp: process.env.PERSONAL_AGENT_MCP_CONFIG ?? null, authority: process.env.PERSONAL_AGENT_AUTHORITY ?? null,
    shellLimit: process.env.PERSONAL_AGENT_SHELL_LIMIT_S ?? null }) + '\\n');
}
const emit = value => process.stdout.write(JSON.stringify(value) + '\\n');
if (process.argv.includes('app-server')) {
  require('node:readline').createInterface({ input: process.stdin }).on('line', line => {
    const request = JSON.parse(line);
    requests.push(request);
    if (request.id != null) emit({id: request.id, result:
      request.method.startsWith('thread/') ? {thread: {id: 'th_123'}} :
      request.method === 'turn/start' ? {turn: {id: 'turn_1'}} : {}});
    if (request.method === 'turn/start') {
      record(request.params.input[0].text);
      for (const event of ${JSON.stringify(lines)}) emit(event);
    }
  });
} else {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { input += chunk; });
  process.stdin.on('end', () => {
    record(input);
    for (const line of ${JSON.stringify(lines)}) emit(line);
    ${code ? `process.stderr.write('upstream said HTTP 429'); process.exitCode = ${code};` : ''}
  });
}
`);
}

const calls = (dir, name) => fs.readFileSync(path.join(dir, `${name}.calls`), 'utf8').trim().split('\n').map((line) => JSON.parse(line));

function setup(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'worker exec 中文-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const previous = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = path.join(dir, 'pi-agent');
  t.after(() => { if (previous === undefined) delete process.env.PI_CODING_AGENT_DIR; else process.env.PI_CODING_AGENT_DIR = previous; });
  const config = {
    dataDir: path.join(dir, 'data'), workspace: path.join(dir, 'workspace'), sendTimeoutMs: 30_000,
    actionSocketPath: path.join(dir, 'data', 'actions.sock'), actionMcpPath: '/core/actions/mcp.mjs',
    agentName: 'Vika', profile: 'test-profile',
  };
  const task = (fields = {}) => ({ id: 't1', runtime: 'pi', session_id: '6d0f2a51-0000-4000-8000-000000000001', native_session: null,
    workdir: path.join(dir, 'workspace', 'tasks', 't1'), turns: 1, ...fields });
  return { dir, config, task };
}

describe('WorkerExecutor', () => {
  it('runs a Pi worker turn in json print mode in its own session, with the worker authority and policy', async (t) => {
    const { dir, config, task } = setup(t);
    config.piBin = fakeCli(dir, 'pi', [
      { type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: 'looking' }], usage: { input: 100, output: 5, cacheRead: 50 } } },
      { type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '原因是 A' }], usage: { input: 200, output: 20, cacheRead: 150 } } },
      { type: 'agent_settled' },
    ]);
    const executor = new WorkerExecutor({ config, settings: () => ({ runtime: 'pi', model: 'ark/deepseek', effort: 'medium' }) });
    const result = await executor.start({ task: task(), prompt: '[task t1] 查一下', authority: 'worker-token' }).done;
    assert.deepEqual(result, { text: '原因是 A', usage: { inputTokens: 300, cachedInputTokens: 200, outputTokens: 25 }, error: null, transient: false });
    const [call] = calls(dir, 'pi');
    assert.deepEqual(call.argv.slice(0, 3), ['--mode', 'json', '-p']);
    assert.equal(call.stdin, '[task t1] 查一下');
    assert.ok(!call.argv.includes(call.stdin));
    const arg = (flag) => call.argv[call.argv.indexOf(flag) + 1];
    assert.equal(arg('--session-id'), '6d0f2a51-0000-4000-8000-000000000001');
    assert.equal(arg('--session-dir'), path.join(config.dataDir, 'workers', 't1'));
    assert.equal(arg('--model'), 'ark/deepseek');
    assert.equal(arg('--thinking'), 'medium');
    assert.ok(call.argv.includes(PI_MCP_EXTENSION));
    assert.match(fs.readFileSync(arg('--append-system-prompt'), 'utf8'), /^## Worker\n\nYou are a worker for Vika\./);
    assert.equal(call.cwd, task().workdir);
    assert.deepEqual(JSON.parse(call.mcp).args.slice(-2), ['--authority', 'worker-token']);
    assert.equal(call.shellLimit, null, 'a worker shell is not capped like the router\'s');
  });

  it('reports a failed turn, and whether it is worth another try', async (t) => {
    const { dir, config, task } = setup(t);
    config.piBin = fakeCli(dir, 'pi', [], 1);
    const result = await new WorkerExecutor({ config }).start({ task: task(), prompt: 'p', authority: 'x' }).done;
    assert.match(result.error, /^exit 1: upstream said HTTP 429/);
    assert.equal(result.transient, true);
  });

  it('runs Codex over RPC, resuming the thread it named on the first turn', async (t) => {
    const { dir, config, task } = setup(t);
    config.codexBin = fakeCli(dir, 'codex', [
      { method: 'item/completed', params: { threadId: 'th_123', turnId: 'turn_1', item: { type: 'agentMessage', text: 'done it' } } },
      { method: 'turn/completed', params: { threadId: 'th_123', turn: { id: 'turn_1', status: 'completed', error: null } } },
    ]);
    const executor = new WorkerExecutor({ config, settings: () => ({ runtime: 'codex', model: 'gpt-6-astra', effort: 'high' }) });
    const first = await executor.start({ task: task({ runtime: 'codex' }), prompt: 'first', authority: 'tok' }).done;
    assert.equal(first.nativeSession, 'th_123');
    assert.equal(first.text, 'done it');
    await executor.start({ task: task({ runtime: 'codex', native_session: 'th_123', turns: 2 }), prompt: 'second', authority: 'tok' }).done;
    const [one, two] = calls(dir, 'codex');
    assert.deepEqual(one.argv.slice(0, 3), ['app-server', '--listen', 'stdio://']);
    const start = one.requests.find(r => r.method === 'thread/start').params;
    assert.match(start.developerInstructions, /^## Worker/);
    assert.ok(!one.argv.join(' ').includes('## Worker'));
    assert.equal(start.model, 'gpt-6-astra');
    assert.equal(one.stdin, 'first');
    assert.match(one.argv.join(' '), /mcp_servers\.personal_agent\.args=\[.*"--authority","tok"\]/);
    assert.equal(two.requests.find(r => r.method === 'thread/resume').params.threadId, 'th_123');
    assert.equal(two.stdin, 'second');
  });

  it('runs Claude Code with --print, resuming its session after the first turn', async (t) => {
    const { dir, config, task } = setup(t);
    config.claudeBin = fakeCli(dir, 'claude', [
      { type: 'assistant', message: { content: [{ type: 'text', text: 'thinking aloud' }] } },
      { type: 'result', result: '结果在这里', is_error: false, usage: { input_tokens: 7, cache_read_input_tokens: 3, output_tokens: 1 } },
    ]);
    const executor = new WorkerExecutor({ config, settings: () => ({ runtime: 'claude', model: 'sonnet', effort: 'medium' }) });
    const first = await executor.start({ task: task({ runtime: 'claude' }), prompt: 'one', authority: 'tok' }).done;
    assert.equal(first.text, '结果在这里');
    await executor.start({ task: task({ runtime: 'claude', native_session: task().session_id, turns: 2 }), prompt: 'two', authority: 'tok' }).done;
    const [one, two] = calls(dir, 'claude');
    const arg = (call, flag) => call.argv[call.argv.indexOf(flag) + 1];
    assert.equal(arg(one, '--session-id'), task().session_id);
    assert.equal(arg(two, '--resume'), task().session_id);
    const mcp = JSON.parse(fs.readFileSync(arg(one, '--mcp-config'), 'utf8'));
    assert.deepEqual(mcp.mcpServers.personal_agent.args.slice(-2), ['--authority', 'tok']);
  });

  it('starts a fresh Claude session after an attempt without a successful session checkpoint', async (t) => {
    const { dir, config, task } = setup(t);
    config.claudeBin = fakeCli(dir, 'claude', [{ type: 'result', result: 'ok', is_error: false }]);
    await new WorkerExecutor({ config }).start({ task: task({ runtime: 'claude', turns: 2 }), prompt: 'replayed original', authority: 'tok' }).done;
    const [call] = calls(dir, 'claude');
    assert.ok(!call.argv.includes('--resume'));
    assert.equal(call.argv[call.argv.indexOf('--session-id') + 1], task().session_id);
    assert.equal(call.stdin, 'replayed original');
  });

  it('runs only on the runtimes it knows, and a stopped turn ends as stopped', async (t) => {
    const { dir, config, task } = setup(t);
    const executor = new WorkerExecutor({ config });
    assert.deepEqual(['pi', 'codex', 'claude', 'durable', 'opencode', 'volc'].map((id) => executor.supports(id)),
      [true, true, true, false, false, false]);
    assert.throws(() => executor.start({ task: task({ runtime: 'durable' }), prompt: 'p', authority: 'x' }), /workers do not run on durable/);
    const slow = script(dir, 'slow-pi', 'setTimeout(() => {}, 60_000);');
    config.piBin = slow;
    const handle = executor.start({ task: task(), prompt: 'p', authority: 'x' });
    setTimeout(() => handle.stop(), 300);
    assert.equal((await handle.done).error, 'stopped');
  });
  for (const runtime of ['pi', 'codex', 'claude']) {
    it(`keeps long ${runtime} prompts and policy off argv through an npm shim`, async (t) => {
      const { dir, config, task } = setup(t);
      const events = runtime === 'codex'
        ? [{ method: 'turn/completed', params: { threadId: 'th_123', turn: { id: 'turn_1', status: 'completed' } } }]
        : [];
      const entry = fakeCli(dir, runtime, events);
      // Exercise the real npm-shim resolver on Windows, without cmd.exe expansion.
      const shim = path.join(dir, `${runtime}.cmd`);
      fs.writeFileSync(shim, `@ECHO off\r\n"node" "%dp0%\\${path.basename(entry)}" %*\r\n`);
      config[`${runtime}Bin`] = process.platform === 'win32' ? shim : entry;
      config.agentName = 'Vika '.repeat(12_000);
      const prompt = '--not-an-option 中文 😀 "quotes" %PATH% & | < > ^ ! \r\n'.repeat(3_000);
      const executor = new WorkerExecutor({ config });
      for (const native_session of [null, 'th_123']) {
        const result = await executor.start({ task: task({ runtime, native_session }), prompt, authority: 'tok' }).done;
        assert.equal(result.error, null);
      }
      for (const call of calls(dir, runtime)) {
        assert.equal(call.stdin, prompt);
        assert.ok(call.argv.join(' ').length < 8_000);
        assert.ok(!call.argv.join(' ').includes('--not-an-option'));
        if (runtime === 'codex') {
          const params = call.requests.find(r => r.method.startsWith('thread/')).params;
          assert.ok(params.developerInstructions.length > 60_000);
        }
      }
    });
  }

  it('reports a missing executable without an unhandled stdin error', async (t) => {
    const { dir, config, task } = setup(t);
    config.claudeBin = path.join(dir, 'missing.exe');
    const result = await new WorkerExecutor({ config }).start({ task: task({ runtime: 'claude' }), prompt: 'x'.repeat(200_000), authority: 'tok' }).done;
    assert.match(result.error, /ENOENT/);
  });

  it('terminates a persistent Codex worker when stdout exceeds the limit', { timeout: 10_000 }, async (t) => {
    const { dir, config, task } = setup(t);
    config.codexBin = script(dir, 'overflow-codex', `
      const emit = value => process.stdout.write(JSON.stringify(value) + '\\n');
      require('node:readline').createInterface({ input: process.stdin }).on('line', line => {
        const request = JSON.parse(line);
        if (request.id != null) emit({ id: request.id, result:
          request.method.startsWith('thread/') ? { thread: { id: 'overflow-thread' } } : {} });
        if (request.method === 'turn/start') {
          process.stdout.write('x'.repeat(20_000_001));
          emit({ method: 'turn/completed', params: { turn: { status: 'completed' } } });
        }
      });
    `);
    const handle = new WorkerExecutor({ config }).start({ task: task({ runtime: 'codex' }), prompt: 'p', authority: 'tok' });
    t.after(() => handle.stop());
    const result = await handle.done;
    assert.equal(result.error, 'worker stdout exceeded 20000000 bytes');
    assert.equal(result.transient, false);
  });

  it('stops a worker and its descendant before resolving', { timeout: 10_000 }, async (t) => {
    const { dir, config, task } = setup(t);
    const ready = path.join(dir, 'descendant-ready');
    const source = `require('node:fs').writeFileSync(${JSON.stringify(ready)}, 'ready'); setInterval(() => {}, 1000);`;
    config.claudeBin = script(dir, 'tree', `
      require('node:child_process').spawn(process.execPath, ['-e', ${JSON.stringify(source)}], { stdio: ['ignore', process.stdout, process.stderr] });
      setInterval(() => {}, 1000);
    `);
    const handle = new WorkerExecutor({ config }).start({ task: task({ runtime: 'claude' }), prompt: 'p', authority: 'tok' });
    t.after(() => handle.stop());
    const deadline = Date.now() + 5_000;
    while (!fs.existsSync(ready) && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(fs.existsSync(ready), 'descendant must be running before stop');
    handle.stop();
    // The descendant inherits stdout: close cannot arrive while it remains alive.
    assert.equal((await handle.done).error, 'stopped');
  });

});
