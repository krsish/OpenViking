// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import { ActionService } from '../src/actions/service.mjs';
import { ConnectorProcess } from '../src/connectors/process.mjs';
import { subscribedEventTypes } from '../src/lark.mjs';
import { MeetingNoteArchive, cleanDocMarkdown, meetingReadTarget, noteMarkdown, noteTitle } from '../src/meeting/note-archive.mjs';
import { MeetingNoteState, MeetingNoteSync, NOTE_EVENT, isNoteEvent, noteIdOf } from '../src/meeting/note-sync.mjs';
import { OVConnection } from '../src/ov-connection.mjs';
import { OVSessionClient } from '../src/ov-session-context.mjs';
import { ProcessRegistry } from '../src/process-utils.mjs';
import { Store } from '../src/store.mjs';
import { fakeConnectors } from './action-fixture.mjs';
import { fakeOV } from './ov-server-fixture.mjs';

const ROOT = 'viking://user/owner/resources/meetings';
const MEETING = '734000000000000301';
const NOTE = '734000000000000901';
const HOUR = 3_600_000;

function recorder() {
  const lines = [];
  const logger = {};
  for (const level of ['debug', 'info', 'warn', 'error']) logger[level] = (message, fields) => lines.push({ level, message, ...fields });
  logger.child = () => logger;
  return { logger, lines, named: (message) => lines.filter((line) => line.message === message) };
}

const SUMMARY = [
  '# 智能纪要：飞书接入评审 2026年10月23日',
  '# 总结',
  '<at user_id="ou_amy12345678">Amy</at> 讲了接入方案。<whiteboard token="wb1"></whiteboard>',
  '<readonly-block type="isv"></readonly-block>',
  '# 待办',
  '- [ ] 和 MA 对接 <at user_id="ou_amy12345678">Amy</at>',
  '# 相关链接',
  '- [PRD](https://larkoffice.com/docx/docPrd)',
].join('\n');
const TRANSCRIPT = '# 文字记录：飞书接入评审 2026年10月23日\n\n> 会议主题：飞书接入评审\n\nAmy 00:00:05  \n大家好，今天评审接入方案。\n';

// A lark-cli that answers what the note sync asks, as scripted per test:
// `notes` is each meeting's note id (none for a meeting without one), `fail`
// a refusal for the commands that contain a key, as lark-cli prints it on
// stderr, and `appEvents` the events of the app's live version.
function lark({ note = {}, documents = {}, search = [[]], notes = { [MEETING]: NOTE }, fail = {},
  appEvents = ['im.message.receive_v1', NOTE_EVENT] } = {}) {
  const calls = [];
  let searches = 0;
  const ok = (data) => ({ code: 0, timedOut: false, stdout: JSON.stringify({ ok: true, data }), stderr: '' });
  const registry = {
    async run(_bin, args) {
      calls.push(args);
      const command = args.slice(2, args.indexOf('--as')).join(' ');
      for (const [pattern, error] of Object.entries(fail)) {
        if (command.includes(pattern)) {
          return { code: 3, timedOut: false, stdout: '', stderr: `[note] progress\n${JSON.stringify({ ok: false, error: { type: 'authorization', message: error } }, null, 2)}\n` };
        }
      }
      if (command.startsWith('api POST')) return ok({});
      if (command.startsWith('api GET /open-apis/application/v6/applications/me')) return ok({ app: { app_id: 'cli_test', online_version_id: 'oav_1' } });
      if (command.includes('/app_versions/')) return ok({ app_version: { event_infos: appEvents.map((type) => ({ event_type: type })) } });
      if (command.startsWith('api GET /open-apis/vc/v1/notes/')) {
        return ok({ note: {
          artifacts: [{ artifact_type: 1, doc_token: 'docNote' }, { artifact_type: 2, doc_token: 'docVerb' }],
          note_source: { source_type: 'meeting', source_entity_id: MEETING }, note_display_type: 1, ...note,
        } });
      }
      if (command.startsWith('vc meeting get')) {
        const meetingId = JSON.parse(args[args.indexOf('--params') + 1]).meeting_id;
        return ok({ meeting: { ...(notes[meetingId] ? { note_id: notes[meetingId] } : {}), topic: '飞书接入评审', meeting_no: '333333333', start_time: '1792742359', end_time: '1792743899',
          host_user: { id: 'ou_host12345678' }, participant_count: '3', calendar_event_id: 'cal-1_0',
          participants: [{ id: 'ou_host12345678', user_type: 1 }, { id: 'ou_amy12345678', user_type: 1 }, { id: 'room-1', user_type: 0 }] } });
      }
      if (command.startsWith('contact +search-user')) {
        return ok({ users: [{ open_id: 'ou_host12345678', localized_name: '张三' }, { open_id: 'ou_amy12345678', localized_name: 'Amy', is_cross_tenant: true }] });
      }
      if (command.startsWith('docs +fetch')) {
        const token = args[args.indexOf('--doc') + 1];
        const content = documents[token] ?? { docNote: SUMMARY, docVerb: TRANSCRIPT }[token];
        return ok({ document: { content, revision_id: token === 'docNote' ? 4 : 3 } });
      }
      if (command.startsWith('vc +search')) {
        const items = search[Math.min(searches, search.length - 1)];
        searches += 1;
        return ok({ items: items.map((id) => ({ id })), has_more: false });
      }
      return ok({});
    },
  };
  return { registry, calls, commands: () => calls.map((args) => args.slice(2, args.indexOf('--as')).join(' ')) };
}

async function sync(t, { meetingNotes = true, script = {}, ov = null } = {}) {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'meeting-notes-'));
  t.after(() => fs.rmSync(dataDir, { recursive: true, force: true }));
  const server = ov ?? await fakeOV(t);
  const cli = lark(script);
  const log = recorder();
  const clock = { now: Date.UTC(2026, 9, 26, 8) };
  const notes = new MeetingNoteSync({
    config: { meetingNotes, dataDir, tempDir: path.join(dataDir, 'tmp'), projectDir: dataDir, larkCliBin: '/fake/lark-cli', openVikingConfigPath: '/unused' },
    registry: cli.registry, logger: log.logger, profile: 'vika-test',
    archive: new MeetingNoteArchive(new OVConnection({ url: server.url, user: 'owner' })),
    now: () => clock.now, setTimer: () => ({}), clearTimer: () => {},
  });
  t.after(() => notes.stop());
  return { notes, cli, log, clock, ov: server, dataDir };
}

const event = (eventId = 'ev-1', noteId = NOTE) => ({ schema: '2.0', header: { event_id: eventId, event_type: NOTE_EVENT }, event: { note_id: noteId } });

describe('meeting notes in OpenViking', () => {
  it('turns Feishu documents into plain Markdown under the meeting', () => {
    assert.equal(cleanDocMarkdown(SUMMARY).includes('<'), false);
    assert.match(cleanDocMarkdown(SUMMARY), /@Amy 讲了接入方案。\[whiteboard\]\n\n# 待办/);
    const text = noteMarkdown({
      meeting: { id: MEETING, number: '333333333', topic: '飞书接入评审', startMs: 1792742359000, endMs: 1792743899000,
        host: '张三', participants: [{ name: '张三' }, { name: 'Amy' }], participantCount: 3, external: true, calendarEventId: 'cal-1_0' },
      note: { id: NOTE, docToken: 'docNote', verbatimToken: 'docVerb', revision: 4 },
      summary: SUMMARY, transcriptUri: `${ROOT}/${MEETING}/transcript.md`, storedAtMs: Date.UTC(2026, 9, 23, 8, 30),
    });
    assert.equal(text, [
      '# 飞书接入评审', '',
      `- Meeting: ${MEETING} (number 333333333)`,
      '- Time: 2026-10-23 07:59 – 08:24 UTC (26 min)',
      '- Host: 张三',
      '- Participants (3): 张三, Amy',
      '- People from outside the owner\'s organization took part',
      '- Calendar event: cal-1_0',
      '- Feishu note: document docNote, revision 4; stored 2026-10-23T08:30:00.000Z. Feishu\'s own copy may have changed since.',
      `- Transcript: ${ROOT}/${MEETING}/transcript.md (Feishu document docVerb)`,
      '',
      '## 总结\n@Amy 讲了接入方案。[whiteboard]\n\n## 待办\n- [ ] 和 MA 对接 @Amy\n## 相关链接\n- [PRD](https://larkoffice.com/docx/docPrd)',
      '',
    ].join('\n'));
  });

  it('heads a note with what the AI note says the meeting was about, over a default topic', () => {
    assert.equal(noteTitle(SUMMARY), '飞书接入评审');
    assert.equal(noteTitle('# 文字记录：Vika后续工作安排同步会议 2026年10月26日\n'), 'Vika后续工作安排同步会议');
    assert.equal(noteTitle('no heading'), null);
    const text = noteMarkdown({ meeting: { id: MEETING, topic: '刘维汉的视频会议' }, note: { docToken: 'd' },
      summary: '# 智能纪要：Vika后续工作安排同步会议 2026年10月26日\n# 总结\n安排', storedAtMs: 0 });
    assert.match(text, /^# Vika后续工作安排同步会议\n\n- Meeting: 734000000000000301\n- Meeting topic: 刘维汉的视频会议\n/);
  });

  it('reads back only what is a stored meeting', () => {
    assert.deepEqual(meetingReadTarget(ROOT, `${ROOT}/${MEETING}`), { target: `${ROOT}/${MEETING}/note.md` });
    assert.deepEqual(meetingReadTarget(ROOT, `${ROOT}/${MEETING}/transcript.md`), { target: `${ROOT}/${MEETING}/transcript.md` });
    assert.deepEqual(meetingReadTarget(ROOT, `${ROOT}/${MEETING}/chunks/transcript/transcript_2.md`),
      { target: `${ROOT}/${MEETING}/transcript.md`, anchor: `${ROOT}/${MEETING}/chunks/transcript/transcript_2.md` });
    for (const uri of [`${ROOT}/${MEETING}/other.md`, `${ROOT}/not-a-meeting/note.md`, `${ROOT}/${MEETING}/../../memories/x.md`,
      'viking://user/owner/memories/x.md', `${ROOT}/${MEETING}/chunks/other/x.md`]) {
      assert.equal(meetingReadTarget(ROOT, uri), null, uri);
    }
  });

  it('listens for note events only on the instance that stores them', () => {
    assert.equal(subscribedEventTypes().includes(NOTE_EVENT), false);
    assert.equal(subscribedEventTypes({ meetingNotes: true }).at(-1), NOTE_EVENT);
    assert.equal(isNoteEvent(event()), true);
    assert.equal(isNoteEvent({ header: { event_type: 'im.message.receive_v1' } }), false);
    assert.equal(noteIdOf(event()), NOTE);
    assert.equal(noteIdOf(event('e', '../x')), null);
  });

  it('subscribes as the owner, queues an event at once and stores the note a minute later, nothing for the Core', async (t) => {
    const { notes, cli, log, clock, ov, dataDir } = await sync(t);
    notes.start();
    await notes.running;
    assert.deepEqual(cli.calls[0], ['--profile', 'vika-test', 'api', 'POST', '/open-apis/vc/v1/notes/subscription',
      '--data', JSON.stringify({ event_type: NOTE_EVENT }), '--as', 'user', '--format', 'json']);
    const app = (args) => args.some((arg) => String(arg).startsWith('/open-apis/application/'));
    assert.ok(cli.calls.every((args) => args.at(-3) === (app(args) ? 'bot' : 'user')), 'the owner\'s, but for reading the app itself');
    assert.equal(log.named('meeting_notes_event_not_enabled').length, 0);
    assert.equal(log.named('meeting_notes_subscribed').length, 1);

    assert.equal(notes.accept(event()), true);
    assert.equal(notes.accept(event()), false, 'the same event delivered twice');
    await notes.work();
    assert.equal(ov.state.files.size, 0, 'not before the documents are complete');
    clock.now += 60_000;
    await notes.work();

    const note = ov.state.files.get(`${ROOT}/${MEETING}/note.md`);
    assert.match(note, /^# 飞书接入评审\n\n- Meeting: 734000000000000301 \(number 333333333\)\n/);
    assert.match(note, /- Participants \(3\): 张三, Amy\n- People from outside the owner's organization took part\n/);
    assert.match(note, /## 待办\n- \[ \] 和 MA 对接 @Amy/);
    const transcript = ov.state.files.get(`${ROOT}/${MEETING}/transcript.md`);
    assert.match(transcript, /^# Transcript: 飞书接入评审\n\n- Meeting: 734000000000000301\n[\s\S]*- Note: viking:\/\/user\/owner\/resources\/meetings\/734000000000000301\/note\.md\n/);
    assert.match(transcript, /> 会议主题：飞书接入评审\n\nAmy 00:00:05 {2}\n大家好/);
    assert.ok(!transcript.includes('# 文字记录'), 'the document\'s own title gives way to ours');
    const writes = ov.state.requests.filter(({ route }) => route === 'POST /api/v1/content/write');
    assert.ok(writes.every(({ body }) => body.processing_mode === 'vectors_only' && body.mode === 'replace'));

    const stored = new MeetingNoteState(dataDir).note(NOTE);
    assert.equal(stored.status, 'stored');
    assert.equal(stored.meeting_id, MEETING);
    assert.equal(Number(stored.revision), 4);
    const [line] = log.named('meeting_note_stored');
    assert.equal(line.source, 'event');
    assert.ok(!JSON.stringify(log.lines).includes(NOTE) && !JSON.stringify(log.lines).includes('大家好'), 'logs carry no ids or content');

    assert.equal(notes.accept(event('ev-2')), true, 'Feishu made the note again');
  });

  it('a long transcript is also split for recall, beside the whole one', async (t) => {
    const long = `# 文字记录：x\n\n${Array.from({ length: 40 }, (_, index) => `说话人 ${index} 00:00:${String(index).padStart(2, '0')}\n${'讨论接入方案的细节。'.repeat(30)}`).join('\n\n')}`;
    const { notes, clock, ov } = await sync(t, { script: { documents: { docVerb: long } } });
    notes.start();
    await notes.running;
    notes.accept(event());
    clock.now += 60_000;
    await notes.work();
    const pieces = [...ov.state.files.keys()].filter((uri) => uri.startsWith(`${ROOT}/${MEETING}/chunks/transcript/`));
    assert.ok(pieces.length > 1, 'split into pieces');
    assert.ok(![...ov.state.files.keys()].some((uri) => uri.startsWith(`${ROOT}/${MEETING}/chunks/note/`)), 'a short note is not split');
    assert.ok(ov.state.files.get(`${ROOT}/${MEETING}/transcript.md`).length > 6000, 'the whole text is kept');
  });

  it('scans the meetings since the last scan for notes it has not seen; the first scan reaches back 30 days', async (t) => {
    const { notes, cli, clock, dataDir, log } = await sync(t, { script: { search: [[MEETING, '734000000000000302'], [MEETING]] } });
    const started = clock.now;
    notes.start();
    await notes.running;
    const searches = () => cli.calls.filter((args) => args.includes('+search'));
    assert.equal(searches()[0][searches()[0].indexOf('--start') + 1], new Date(started - 30 * 86_400_000).toISOString());
    const looked = (args) => args.includes('meeting') && args.includes('get') && !args.at(args.indexOf('--params') + 1).includes('with_participants');
    assert.deepEqual(cli.calls.filter(looked).map((args) => JSON.parse(args[args.indexOf('--params') + 1]).meeting_id), [MEETING, '734000000000000302']);
    assert.equal(log.named('meeting_notes_scanned')[0].queued, 1);
    assert.equal(log.named('meeting_note_stored')[0].source, 'scan', 'a scanned note is stored at once');

    clock.now += 7 * HOUR;
    notes.repeat();
    await notes.running;
    assert.equal(searches()[1][searches()[1].indexOf('--start') + 1], new Date(started - 6 * HOUR).toISOString());
    assert.equal(log.named('meeting_notes_scanned')[1].queued, 0, 'a stored note is not queued again');
    assert.equal(cli.calls.filter(looked).length, 2, 'nor is its meeting looked up again');
    assert.equal(new MeetingNoteState(dataDir).counts().stored, 1);
  });

  it('says so when the app\'s live version does not have the note event, and reads the refusal lark-cli prints', async (t) => {
    const { notes, log } = await sync(t, { script: { appEvents: ['im.message.receive_v1'], search: [[MEETING]], fail: { 'vc meeting get': 'missing required scope(s): vc:meeting:readonly' } } });
    notes.start();
    await notes.running;
    assert.match(log.named('meeting_notes_event_not_enabled')[0].hint, /vika setup/);
    assert.match(log.named('meeting_notes_scan_failed')[0].error, /meeting get failed: missing required scope\(s\): vc:meeting:readonly/,
      'every meeting unreadable: the scan fails, and says why');
  });

  it('a scan passes over a meeting it cannot read and queues the others', async (t) => {
    const other = '734000000000000302';
    const { notes, log, cli } = await sync(t, { script: { search: [[other, MEETING]], notes: { [MEETING]: NOTE } } });
    const run = cli.registry.run;
    cli.registry.run = async (bin, args) => (args.includes(JSON.stringify({ meeting_id: other }))
      ? { code: 1, timedOut: false, stdout: '', stderr: '{"ok":false,"error":{"message":"no permission"}}' } : run(bin, args));
    notes.start();
    await notes.running;
    const [scanned] = log.named('meeting_notes_scanned');
    assert.deepEqual([scanned.meetings, scanned.notes, scanned.queued, scanned.unreadable], [2, 1, 1, 1]);
    assert.match(scanned.error, /no permission/);
  });

  it('an existing subscription is fine; a refused one is tried again half an hour later', async (t) => {
    const already = await sync(t, { script: { fail: { 'api POST': 'subscription already exists' } } });
    already.notes.start();
    await already.notes.running;
    assert.deepEqual(already.log.named('meeting_notes_subscribed').map(({ already: was }) => was), [true]);

    const refused = await sync(t, { script: { fail: { 'api POST': 'missing scope vc:note:read' } } });
    refused.notes.start();
    await refused.notes.running;
    assert.match(refused.log.named('meeting_notes_subscribe_failed')[0].error, /missing scope/);
    const posts = () => refused.cli.calls.filter((args) => args[3] === 'POST').length;
    refused.clock.now += 10 * 60_000;
    refused.notes.repeat();
    await refused.notes.running;
    assert.equal(posts(), 1);
    refused.clock.now += 30 * 60_000;
    refused.notes.repeat();
    await refused.notes.running;
    assert.equal(posts(), 2);
    // A restart (after setup completed the app, say) subscribes and scans at once.
    const searches = () => refused.cli.calls.filter((args) => args.includes('+search')).length;
    const scanned = searches();
    await refused.notes.stop();
    refused.clock.now += 60_000;
    refused.notes.start();
    await refused.notes.running;
    assert.equal(posts(), 3);
    assert.equal(searches(), scanned + 1);
  });

  it('skips a note that is not a meeting\'s, waits for empty documents, and gives up after the last retry', async (t) => {
    const upload = await sync(t, { script: { note: { note_source: { source_type: 'upload' } } } });
    upload.notes.start();
    await upload.notes.running;
    upload.notes.accept(event());
    upload.clock.now += 60_000;
    await upload.notes.work();
    assert.equal(upload.log.named('meeting_note_skipped').length, 1);
    assert.equal(upload.ov.state.files.size, 0);

    const empty = await sync(t, { script: { documents: { docVerb: '' } } });
    empty.notes.start();
    await empty.notes.running;
    empty.notes.accept(event());
    empty.clock.now += 60_000;
    await empty.notes.work();
    assert.match(empty.log.named('meeting_note_failed')[0].error, /still empty/);
    assert.equal(empty.log.named('meeting_note_failed')[0].retryInMs, 60_000);

    const down = await sync(t);
    down.notes.start();
    await down.notes.running;
    down.ov.state.down = true;
    down.notes.accept(event());
    for (let attempt = 0; attempt < 8; attempt += 1) {
      down.clock.now += 13 * HOUR;
      await down.notes.work();
    }
    const failures = down.log.named('meeting_note_failed');
    assert.equal(failures.length, 8);
    assert.equal(failures.at(-1).final, true);
    assert.equal(new MeetingNoteState(down.dataDir).note(NOTE).status, 'failed');
    down.ov.state.down = false;
    assert.equal(down.notes.accept(event('ev-again')), true, 'a new event brings a failed note back');
  });

  it('with the switch off, a subscription made before is taken back once, and events are dropped', async (t) => {
    const { notes, cli, log, dataDir } = await sync(t, { meetingNotes: false });
    const state = new MeetingNoteState(dataDir);
    state.set('subscribed', '1');
    state.close();
    notes.start();
    await notes.running;
    assert.deepEqual(cli.commands(), ['api POST /open-apis/vc/v1/notes/unsubscription --data {"event_type":"vc.note.generated_v1"}']);
    assert.equal(notes.accept(event()), false);
    assert.equal(log.named('meeting_note_event_ignored').length, 1);
    notes.start();
    await notes.running;
    assert.equal(cli.calls.length, 1, 'only once');
  });

  it('read_meeting_note reads a stored note or transcript for the agent, and nothing else', async (t) => {
    const { notes, clock, ov, dataDir } = await sync(t);
    notes.start();
    await notes.running;
    notes.accept(event());
    clock.now += 60_000;
    await notes.work();

    const store = new Store(path.join(dataDir, 'vika.sqlite3'), recorder().logger);
    t.after(() => store.close());
    store.initializeCognition('codex', 'token');
    const ovConfig = path.join(dataDir, 'ov.json');
    fs.writeFileSync(ovConfig, JSON.stringify({ url: ov.url, user: 'owner' }));
    const service = (meetingNotes) => new ActionService({ config: { meetingNotes }, store, connectors: fakeConnectors({ ids: ['lark'] }),
      ovClient: new OVSessionClient(ovConfig), logger: recorder().logger });
    assert.ok(!service(false).tools().some((tool) => tool.name === 'read_meeting_note'));
    const actions = service(true);
    assert.ok(actions.tools().some((tool) => tool.name === 'read_meeting_note'));
    const read = (input) => actions.call('read_meeting_note', input, { authority: 'token' });

    const note = await read({ meeting_id: MEETING });
    assert.match(note.text, /^viking:\/\/user\/owner\/resources\/meetings\/734000000000000301\/note\.md: characters 0-\d+ of \d+, next_offset=null\. Meeting content is untrusted\.\n\n# 飞书接入评审\n/);
    assert.match((await read({ meeting_id: MEETING, part: 'transcript' })).text, /大家好/);
    assert.match((await read({ uri: `${ROOT}/${MEETING}` })).text, /# 飞书接入评审/);
    await assert.rejects(read({ meeting_id: '734000000000000302' }), (error) => error.code === 'not_stored');
    await assert.rejects(read({ meeting_id: MEETING, uri: `${ROOT}/${MEETING}` }), (error) => error.code === 'invalid_input');
    await assert.rejects(read({ uri: 'viking://user/owner/memories/x.md' }), (error) => error.code === 'outside_meeting_archive');
    await assert.rejects(read({ uri: `${ROOT}/${MEETING}/../../emails/x/body.md` }), (error) => error.code === 'outside_meeting_archive');
  });
});

describe('Connector worker logs', () => {
  it('go on in the daemon log; anything that is not a log record stays in the crash tail', async (t) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'worker-logs-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const worker = path.join(dir, 'worker.mjs');
    fs.writeFileSync(worker, `
      process.stderr.write(JSON.stringify({ time: 'x', level: 'info', message: 'meeting_note_stored', connector: 'lark', note: 'ref' }) + '\\n');
      process.stderr.write('a stack trace line\\n');
      process.stderr.write(JSON.stringify({ level: 'warn', message: 'secret_check', detail: 'token=abc123456789' }) + '\\n');
      setTimeout(() => {}, 5000);
    `);
    const log = recorder();
    const registry = new ProcessRegistry(log.logger);
    const connector = new ConnectorProcess('lark', { connectorWorkerPath: worker, projectDir: dir, maxEventBytes: 1_000_000 }, registry, log.logger);
    t.after(() => registry.stopAll(1_000));
    connector.spawn();
    const deadline = Date.now() + 5_000;
    while (log.named('secret_check').length === 0 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20));
    assert.deepEqual(log.named('meeting_note_stored'), [{ level: 'info', message: 'meeting_note_stored', connector: 'lark', note: 'ref' }]);
    assert.equal(log.named('secret_check')[0].level, 'warn');
    assert.ok(!log.lines.some((line) => line.message === 'a stack trace line'));
    assert.match(connector.stderrTail, /a stack trace line/);
  });
});
