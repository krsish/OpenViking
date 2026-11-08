// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { it } from 'node:test';
import assert from 'node:assert/strict';
import { Writable } from 'node:stream';
import { createCodexWorkerProtocol } from '../src/worker-codex.mjs';

function setup(t, options = {}) {
  const sent = [];
  const completions = [];
  const child = { stdin: new Writable({ write(chunk, _encoding, callback) { sent.push(JSON.parse(String(chunk))); callback(); } }) };
  const protocol = createCodexWorkerProtocol({
    task: { workdir: 'C:\\Work space\\任务', native_session: null },
    policy: 'Developer policy', prompt: 'User request', model: 'test-model', effort: 'high',
    onComplete: (error) => completions.push(error), ...options,
  });
  t.after(() => { protocol.dispose(); child.stdin.destroy(); });
  protocol.start(child);
  const reply = (result) => protocol.line({ id: sent.at(-1).id, result });
  return { protocol, sent, completions, child, reply };
}

it('transports long policy and task by RPC, preserves developer role and resumes the task thread', (t) => {
  const policy = '策略\n'.repeat(20_000);
  const prompt = '正文 " %PATH% & \n'.repeat(20_000);
  const { protocol, sent, reply, completions } = setup(t, {
    policy, prompt, task: { workdir: 'C:\\Work space\\任务', native_session: 'task-thread' },
  });
  assert.equal(sent[0].method, 'initialize');
  reply({});
  assert.equal(sent[1].method, 'initialized');
  assert.equal(sent[2].method, 'thread/resume');
  assert.deepEqual(sent[2].params, {
    developerInstructions: policy, cwd: 'C:\\Work space\\任务', approvalPolicy: 'never',
    sandbox: 'danger-full-access', model: 'test-model', config: { model_reasoning_effort: 'high' }, threadId: 'task-thread',
  });
  reply({ thread: { id: 'task-thread' } });
  assert.equal(sent[3].method, 'turn/start');
  assert.deepEqual(sent[3].params, { threadId: 'task-thread', input: [{ type: 'text', text: prompt }], model: 'test-model', effort: 'high' });
  reply({ turn: { id: 'turn-1' } });
  protocol.line({ method: 'item/completed', params: { threadId: 'other-thread', item: { type: 'agentMessage', text: 'wrong' } } });
  assert.equal(protocol.state.text, '');
  protocol.line({ method: 'item/completed', params: { threadId: 'task-thread', item: { type: 'agentMessage', text: ' done ' } } });
  protocol.line({ method: 'thread/tokenUsage/updated', params: { threadId: 'task-thread', tokenUsage: { last: { inputTokens: 10, cachedInputTokens: 3, outputTokens: 2 } } } });
  protocol.line({ method: 'turn/completed', params: { threadId: 'task-thread', turn: { status: 'completed' } } });
  assert.deepEqual(completions, [null]);
  assert.equal(protocol.completed, true);
  assert.deepEqual(protocol.state, { text: 'done', nativeSession: 'task-thread', error: null, usage: { inputTokens: 10, cachedInputTokens: 3, outputTokens: 2 } });
});

it('starts a fresh thread and treats completion before the start acknowledgement as success', (t) => {
  const { protocol, sent, reply, completions } = setup(t);
  reply({});
  assert.equal(sent.at(-1).method, 'thread/start');
  assert.equal(sent.at(-1).params.threadId, undefined);
  reply({ thread: { id: 'new-thread' } });
  protocol.line({ method: 'turn/completed', params: { turn: { status: 'completed' } } });
  reply({ turn: { id: 'turn-1' } });
  assert.deepEqual(completions, [null]);
});

it('reports failed/interrupted turns without treating a successful handshake as completed work', (t) => {
  const { protocol, reply, completions } = setup(t);
  reply({});
  reply({ thread: { id: 'thread' } });
  reply({});
  protocol.line({ method: 'turn/completed', params: { turn: { status: 'failed', error: { message: 'HTTP 429' } } } });
  assert.deepEqual(completions, ['HTTP 429']);
  assert.equal(protocol.state.error, 'HTTP 429');
});

it('does not silently start a fresh thread when resume fails', (t) => {
  const { protocol, sent, reply, completions } = setup(t, { task: { workdir: '/task', native_session: 'missing' } });
  reply({});
  protocol.line({ id: sent.at(-1).id, error: { code: -1, message: 'thread not found' } });
  assert.deepEqual(completions, ['thread not found']);
  assert.equal(protocol.completed, false);
  assert.equal(sent.length, 3);
});

it('denies unattended approvals and answers unsupported requests without hanging', (t) => {
  const { protocol, sent } = setup(t);
  const cases = [
    ['item/commandExecution/requestApproval', { decision: 'decline' }],
    ['item/fileChange/requestApproval', { decision: 'decline' }],
    ['execCommandApproval', { decision: 'denied' }],
    ['applyPatchApproval', { decision: 'denied' }],
    ['item/permissions/requestApproval', { permissions: {}, scope: 'turn' }],
    ['item/tool/requestUserInput', { answers: {} }],
    ['mcpServer/elicitation/request', { action: 'cancel', content: null }],
  ];
  for (const [method, result] of cases) {
    protocol.line({ id: 100, method, params: {} });
    assert.deepEqual(sent.at(-1), { id: 100, result });
  }
  protocol.line({ id: 101, method: 'unknown', params: {} });
  assert.equal(sent.at(-1).error.code, -32601);
});

it('bounds a stalled handshake and reports a broken input pipe once', async (t) => {
  const { protocol, completions, child } = setup(t, { handshakeTimeoutMs: 10 });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.match(completions[0], /initialize timed out/);
  assert.equal(protocol.completed, false);
  child.stdin.emit('error', new Error('EPIPE'));
  assert.equal(completions.length, 1);
});

it('fails an input pipe error immediately and ignores late messages', (t) => {
  const { protocol, completions, child } = setup(t);
  child.stdin.emit('error', new Error('EPIPE'));
  protocol.line({ method: 'turn/completed', params: { turn: { status: 'completed' } } });
  assert.deepEqual(completions, ['EPIPE']);
  assert.equal(protocol.completed, false);
});

it('rejects a thread response with no id', (t) => {
  const { reply, completions } = setup(t);
  reply({});
  reply({});
  assert.match(completions[0], /no thread id/);
});
