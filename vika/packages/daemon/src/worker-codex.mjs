// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import { safeError } from './logger.mjs';
import { turnErrorText } from './codex.mjs';

// Keep both the task and its developer policy off the command line. Unlike
// model_instructions_file, developerInstructions preserves Codex's base prompt.
// One app-server process still runs exactly one worker turn.
export function createCodexWorkerProtocol({ prompt, policy, task, model, effort, onComplete, handshakeTimeoutMs = 30_000 }) {
  const state = { text: '', usage: { inputTokens: 0, cachedInputTokens: 0, outputTokens: 0 }, error: null, nativeSession: null };
  const pending = new Map();
  let child;
  let nextId = 0;
  let finished = false;
  let completed = false;

  function dispose() {
    for (const request of pending.values()) clearTimeout(request.timer);
    pending.clear();
  }

  function finish(error = null) {
    if (finished) return;
    finished = true;
    state.error = error ? safeError(error) : null;
    dispose();
    onComplete(state.error);
  }

  function write(message) {
    if (finished) return;
    try { child.stdin.write(`${JSON.stringify(message)}\n`); }
    catch (error) { finish(error); }
  }

  function request(method, params, callback) {
    if (finished) return;
    const id = ++nextId;
    const timer = setTimeout(() => finish(new Error(`Codex worker ${method} timed out`)), handshakeTimeoutMs);
    timer.unref?.();
    pending.set(id, { callback, timer });
    write({ id, method, params });
  }

  function start(process) {
    child = process;
    child.stdin.on('error', (error) => finish(error));
    request('initialize', {
      clientInfo: { name: 'vika-worker', version: '1' },
      capabilities: { experimentalApi: true },
    }, () => {
      write({ method: 'initialized', params: {} });
      const params = {
        developerInstructions: policy,
        cwd: task.workdir,
        approvalPolicy: 'never',
        sandbox: 'danger-full-access',
        ...(model ? { model } : {}),
        ...(effort ? { config: { model_reasoning_effort: effort } } : {}),
      };
      request(task.native_session ? 'thread/resume' : 'thread/start', {
        ...params,
        ...(task.native_session ? { threadId: task.native_session } : {}),
      }, (result) => {
        const threadId = result?.thread?.id;
        if (!threadId) { finish(new Error('Codex worker returned no thread id')); return; }
        state.nativeSession = threadId;
        request('turn/start', {
          threadId,
          input: [{ type: 'text', text: prompt }],
          ...(model ? { model } : {}),
          ...(effort ? { effort } : {}),
        }, () => {});
      });
    });
  }

  function serverRequest({ id, method }) {
    let result;
    switch (method) {
      case 'item/commandExecution/requestApproval':
      case 'item/fileChange/requestApproval': result = { decision: 'decline' }; break;
      case 'execCommandApproval':
      case 'applyPatchApproval': result = { decision: 'denied' }; break;
      case 'item/permissions/requestApproval': result = { permissions: {}, scope: 'turn' }; break;
      case 'item/tool/requestUserInput': result = { answers: {} }; break;
      case 'mcpServer/elicitation/request': result = { action: 'cancel', content: null }; break;
      default: break;
    }
    write(result === undefined
      ? { id, error: { code: -32601, message: 'This unattended worker does not support this server request.' } }
      : { id, result });
  }

  function line(event) {
    if (finished || !event || typeof event !== 'object') return;
    if (event.id !== undefined) {
      if (typeof event.method === 'string') { serverRequest(event); return; }
      const entry = pending.get(event.id);
      if (!entry) return;
      pending.delete(event.id);
      clearTimeout(entry.timer);
      if (event.error) finish(new Error(event.error.message || 'Codex worker RPC failed'));
      else entry.callback(event.result);
      return;
    }
    const params = event.params ?? {};
    const threadId = params.threadId ?? params.turn?.threadId;
    if (threadId && state.nativeSession && threadId !== state.nativeSession) return;
    if (event.method === 'item/completed') {
      if (['agentMessage', 'agent_message'].includes(params.item?.type) && typeof params.item.text === 'string') {
        state.text = params.item.text.trim();
      }
    } else if (event.method === 'thread/tokenUsage/updated' && params.tokenUsage?.last) {
      const usage = params.tokenUsage.last;
      state.usage = {
        inputTokens: Number(usage.inputTokens ?? 0),
        cachedInputTokens: Number(usage.cachedInputTokens ?? 0),
        outputTokens: Number(usage.outputTokens ?? 0),
      };
    } else if (event.method === 'turn/completed') {
      completed = true;
      const turn = params.turn ?? params;
      finish(turn.status === 'completed' ? null : new Error(turnErrorText(turn.error) || `Codex worker turn ${turn.status || 'failed'}`));
    }
  }

  return { state, start, line, dispose, get completed() { return completed; } };
}
