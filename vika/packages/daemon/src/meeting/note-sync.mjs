// Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: AGPL-3.0

import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { opaqueRef, redactSensitive, safeError } from '../logger.mjs';
import { MeetingNoteArchive, meetingFolder, noteMarkdown, transcriptMarkdown } from './note-archive.mjs';

// Meeting notes into OpenViking, run by the Lark Connector of the one
// instance that has agents[].meetingNotes on. Feishu/Lark sends the owner's
// app `vc.note.generated_v1` for every AI note of a meeting the owner took
// part in, once the owner subscribed to it with their own login (the user
// identity, as for search). The note's id is all the event carries: what it
// belongs to and its documents are read back with that same login. Nothing
// reaches the Core or wakes the agent; the note is just in OV for recall.
//
// The long connection hands over an event only while it is connected, so a
// scan of the meetings since the last one (vc +search) finds what came while
// the daemon was down; the first scan reaches back FIRST_SCAN_MS. Each note
// is a row in a local queue, written before anything is fetched, and tried
// again on RETRY_DELAYS_MS when Feishu or OV fails.

export const NOTE_EVENT = 'vc.note.generated_v1';
const SUBSCRIBE_PATH = '/open-apis/vc/v1/notes/subscription';
const UNSUBSCRIBE_PATH = '/open-apis/vc/v1/notes/unsubscription';
const NOTE_ID = /^[A-Za-z0-9_-]{1,64}$/;
const MEETING_ID = /^\d{1,32}$/;
// Feishu writes the note's documents just before it says so; a moment later
// they are complete.
const EVENT_DELAY_MS = 60_000;
const RETRY_DELAYS_MS = [60_000, 300_000, 900_000, 3_600_000, 10_800_000, 21_600_000, 43_200_000];
const FIRST_SCAN_MS = 30 * 86_400_000;
// A scan starts this long before the last one ended, for a meeting that
// was still running then.
const SCAN_OVERLAP_MS = 6 * 3_600_000;
const SCAN_EVERY_MS = 6 * 3_600_000;
const RESUBSCRIBE_EVERY_MS = 86_400_000;
const RETRY_SUBSCRIBE_MS = 1_800_000;
const TICK_MS = 1_800_000;
const STOP_WAIT_MS = 2_000;
const MAX_SEARCH_PAGES = 40;
const DETAIL_BATCH = 20;
const NAME_BATCH = 50;
const DOC_STDOUT_LIMIT = 16_000_000;
const NOTE_ARTIFACT = 1;
const VERBATIM_ARTIFACT = 2;

export function isNoteEvent(raw) {
  return (raw?.header?.event_type ?? raw?.event_type ?? raw?.type) === NOTE_EVENT;
}

export function noteIdOf(raw) {
  const id = raw?.event?.note_id ?? raw?.note_id;
  return typeof id === 'string' && NOTE_ID.test(id) ? id : null;
}

const iso = (ms) => new Date(ms).toISOString();
const seconds = (value) => {
  const number = Number.parseInt(String(value ?? ''), 10);
  return Number.isSafeInteger(number) && number > 0 ? number * 1_000 : null;
};

// What the Connector keeps: one row per note, and when it last scanned and
// subscribed. Private to the daemon user.
export class MeetingNoteState {
  static path(dataDir) {
    return path.join(dataDir, 'meeting-notes', 'state.sqlite3');
  }

  constructor(dataDir) {
    if (!path.isAbsolute(dataDir)) throw new Error('Meeting note data directory must be absolute');
    const databasePath = MeetingNoteState.path(dataDir);
    fs.mkdirSync(path.dirname(databasePath), { recursive: true, mode: 0o700 });
    try { fs.chmodSync(path.dirname(databasePath), 0o700); } catch {}
    this.db = new DatabaseSync(databasePath);
    try { fs.chmodSync(databasePath, 0o600); } catch {}
    this.db.exec('PRAGMA busy_timeout = 5000');
    this.db.exec('PRAGMA journal_mode = WAL');
    this.db.exec('PRAGMA synchronous = FULL');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS notes (
        note_id TEXT PRIMARY KEY,
        meeting_id TEXT,
        status TEXT NOT NULL,
        source TEXT NOT NULL,
        event_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_at_ms INTEGER NOT NULL,
        received_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        stored_at_ms INTEGER,
        revision INTEGER,
        error TEXT
      );
      CREATE INDEX IF NOT EXISTS notes_due ON notes(status, next_at_ms);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    `);
  }

  get(key) {
    return this.db.prepare('SELECT value FROM meta WHERE key = ?').get(key)?.value ?? null;
  }

  set(key, value) {
    this.db.prepare('INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value')
      .run(key, String(value));
  }

  note(noteId) {
    return this.db.prepare('SELECT * FROM notes WHERE note_id = ?').get(noteId) ?? null;
  }

  // A note to store. A scan only adds notes it has not seen; an event also
  // brings back one already handled (Feishu made it again), unless it is
  // the same event delivered twice. Resolves with whether it is queued now.
  enqueue(noteId, { meetingId = null, source, eventId = null, atMs, delayMs = 0 }) {
    const row = this.note(noteId);
    if (!row) {
      this.db.prepare(`INSERT INTO notes(note_id, meeting_id, status, source, event_id, next_at_ms, received_at_ms, updated_at_ms)
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)`).run(noteId, meetingId, source, eventId, atMs + delayMs, atMs, atMs);
      return true;
    }
    if (source !== 'event' || row.status === 'pending' || (eventId && row.event_id === eventId)) return false;
    this.db.prepare(`UPDATE notes SET status = 'pending', source = ?, event_id = ?, attempts = 0, next_at_ms = ?,
      received_at_ms = ?, updated_at_ms = ?, error = NULL WHERE note_id = ?`)
      .run(source, eventId, atMs + delayMs, atMs, atMs, noteId);
    return true;
  }

  due(atMs) {
    return this.db.prepare(`SELECT * FROM notes WHERE status = 'pending' AND next_at_ms <= ?
      ORDER BY next_at_ms, note_id LIMIT 1`).get(atMs) ?? null;
  }

  nextAt() {
    const row = this.db.prepare("SELECT MIN(next_at_ms) AS at FROM notes WHERE status = 'pending'").get();
    return row?.at === null || row?.at === undefined ? null : Number(row.at);
  }

  settle(noteId, { status, meetingId = null, revision = null, error = null, attempts, nextAtMs = null, atMs }) {
    this.db.prepare(`UPDATE notes SET status = ?, meeting_id = COALESCE(?, meeting_id), revision = COALESCE(?, revision),
      error = ?, attempts = ?, next_at_ms = COALESCE(?, next_at_ms), updated_at_ms = ?,
      stored_at_ms = CASE WHEN ? = 'stored' THEN ? ELSE stored_at_ms END WHERE note_id = ?`)
      .run(status, meetingId, revision, error, attempts, nextAtMs, atMs, status, atMs, noteId);
  }

  counts() {
    return Object.fromEntries(this.db.prepare('SELECT status, COUNT(*) AS n FROM notes GROUP BY status').all()
      .map(({ status, n }) => [status, Number(n)]));
  }

  close() {
    try { this.db.close(); } catch {}
  }
}

// A failure that says why without what Feishu or OV answered.
function failure(error) {
  return redactSensitive(safeError(error)).replace(/\b(?:om|oc|ou|on|cli)_[A-Za-z0-9_-]{6,}\b/g, '[LARK_ID]').slice(0, 300);
}

export class MeetingNoteSync {
  // The worker's own: on when the instance stores meeting notes, otherwise
  // only there to take back a subscription it made before.
  static forWorker({ config, registry, logger, profile }) {
    return new MeetingNoteSync({ config, registry, logger: logger.child?.({ component: 'meeting-notes' }) ?? logger, profile });
  }

  constructor({
    config, registry, logger, profile, archive = null, state = null,
    now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout,
  }) {
    Object.assign(this, { config, registry, logger, archive, state, now, setTimer, clearTimer });
    this.profile = typeof profile === 'function' ? profile : () => profile;
    this.enabled = Boolean(config.meetingNotes);
    this.timer = null;
    this.ticker = null;
    this.running = null;
    this.stopped = true;
  }

  start() {
    this.stopped = false;
    if (!this.enabled) {
      this.after(() => this.retire());
      return;
    }
    if (!this.config.openVikingConfigPath) {
      this.logger.error('meeting_notes_unavailable', { reason: 'no OpenViking configuration' });
      return;
    }
    try {
      this.state ??= new MeetingNoteState(this.config.dataDir);
      this.archive ??= MeetingNoteArchive.fromConfigFile(this.config.openVikingConfigPath, { logger: this.logger });
    } catch (error) {
      this.logger.error('meeting_notes_unavailable', { reason: failure(error) });
      this.state?.close();
      this.state = null;
      return;
    }
    this.logger.info('meeting_notes_started', { queue: this.state.counts() });
    this.after(() => this.tick({ starting: true }));
    this.ticker = this.setTimer(() => this.repeat(), TICK_MS);
    this.ticker?.unref?.();
  }

  // Runs `work` after whatever runs now; a failure is logged, never thrown.
  after(work) {
    this.running = Promise.resolve(this.running).then(() => (this.stopped ? null : work())).catch((error) => {
      this.logger.error('meeting_notes_error', { error: failure(error) });
    });
    return this.running;
  }

  repeat() {
    if (this.stopped) return;
    this.after(() => this.tick());
    this.ticker = this.setTimer(() => this.repeat(), TICK_MS);
    this.ticker?.unref?.();
  }

  // Subscribe and scan when they are due, and always at a start (a restart
  // after the app was completed, or after time away), then work the queue.
  async tick({ starting = false } = {}) {
    const now = this.now();
    const subscribed = Number(this.state.get('subscribed_at_ms')) || 0;
    const failed = Number(this.state.get('subscribe_failed_at_ms')) || 0;
    if (starting || (now - subscribed >= RESUBSCRIBE_EVERY_MS && now - failed >= RETRY_SUBSCRIBE_MS)) await this.subscribe();
    if (this.stopped) return;
    const scanned = Number(this.state.get('scanned_at_ms')) || 0;
    if (starting || now - scanned >= SCAN_EVERY_MS) await this.scan().catch((error) => {
      this.logger.warn('meeting_notes_scan_failed', { error: failure(error) });
    });
    await this.work();
  }

  // A note being stored stays pending until it settles, so a stop waits only
  // briefly for it; the worker's process group takes its lark-cli along.
  async stop() {
    this.stopped = true;
    if (this.timer) this.clearTimer(this.timer);
    if (this.ticker) this.clearTimer(this.ticker);
    this.timer = null;
    this.ticker = null;
    let waited;
    await Promise.race([Promise.resolve(this.running), new Promise((resolve) => { waited = setTimeout(resolve, STOP_WAIT_MS); })]);
    clearTimeout(waited);
    this.state?.close();
    this.state = null;
  }

  // An event from the long connection: queued before anything is fetched.
  accept(raw) {
    if (!this.enabled || !this.state) {
      this.logger.info('meeting_note_event_ignored', { reason: this.enabled ? 'not started' : 'meeting notes are off' });
      return false;
    }
    const noteId = noteIdOf(raw);
    if (!noteId) {
      this.logger.warn('meeting_note_event_invalid', { eventKeys: Object.keys(raw?.event ?? {}).slice(0, 20) });
      return false;
    }
    const eventId = typeof raw?.header?.event_id === 'string' ? raw.header.event_id.slice(0, 128) : null;
    const queued = this.state.enqueue(noteId, { source: 'event', eventId, atMs: this.now(), delayMs: EVENT_DELAY_MS });
    this.logger.info('meeting_note_event', { note: opaqueRef(noteId), queued });
    this.arm();
    return queued;
  }

  async cli(args, label, { timeoutMs = 60_000, stdoutLimit, cwd = this.config.projectDir } = {}) {
    const profile = this.profile();
    if (!profile) throw new Error('no lark-cli profile');
    const result = await this.registry.run(this.config.larkCliBin, ['--profile', profile, ...args, '--as', 'user', '--format', 'json'],
      { cwd, timeoutMs, ...(stdoutLimit ? { stdoutLimit } : {}) });
    if (result.timedOut) throw new Error(`lark-cli ${label} timed out`);
    let parsed = null;
    try { parsed = JSON.parse(result.stdout); } catch {}
    if (result.code !== 0 || parsed?.ok === false || !parsed) {
      const error = parsed?.error ?? {};
      const detail = error.message || result.stderr.trim().split('\n').at(-1) || `exit ${result.code}`;
      throw Object.assign(new Error(`lark-cli ${label} failed${error.code ? ` (${error.code})` : ''}: ${detail}`),
        { code: error.code ?? null, subtype: error.subtype ?? null });
    }
    return parsed.data ?? {};
  }

  // Asks Feishu/Lark to send this app the owner's note events. Done again
  // at each start and each day, in case a subscription lapses; one that
  // exists already is fine.
  async subscribe() {
    const at = this.now();
    try {
      await this.cli(['api', 'POST', SUBSCRIBE_PATH, '--data', JSON.stringify({ event_type: NOTE_EVENT })], 'note subscription');
      this.logger.info('meeting_notes_subscribed', {});
    } catch (error) {
      if (this.stopped) return false;
      if (!/already|exist/i.test(error.message)) {
        this.state.set('subscribe_failed_at_ms', at);
        this.logger.warn('meeting_notes_subscribe_failed', { error: failure(error), code: error.code ?? undefined });
        return false;
      }
      this.logger.info('meeting_notes_subscribed', { already: true });
    }
    if (this.stopped) return false;
    this.state.set('subscribed_at_ms', at);
    this.state.set('subscribed', '1');
    return true;
  }

  // With the switch off, an instance that subscribed before (the switch
  // moved to another instance) takes its subscription back, once.
  async retire() {
    const file = MeetingNoteState.path(this.config.dataDir);
    if (!fs.existsSync(file)) return;
    const state = new MeetingNoteState(this.config.dataDir);
    try {
      if (state.get('subscribed') !== '1') return;
      await this.cli(['api', 'POST', UNSUBSCRIBE_PATH, '--data', JSON.stringify({ event_type: NOTE_EVENT })], 'note unsubscription');
      state.set('subscribed', '0');
      this.logger.info('meeting_notes_unsubscribed', {});
    } catch (error) {
      this.logger.warn('meeting_notes_unsubscribe_failed', { error: failure(error) });
    } finally {
      state.close();
    }
  }

  // The meetings since the last scan, and which have a note not seen yet.
  async scan() {
    const now = this.now();
    const until = Number(this.state.get('scanned_until_ms')) || 0;
    const from = until ? Math.max(until - SCAN_OVERLAP_MS, now - FIRST_SCAN_MS) : now - FIRST_SCAN_MS;
    const meetings = [];
    let pageToken = null;
    for (let page = 0; page < MAX_SEARCH_PAGES && !this.stopped; page += 1) {
      const data = await this.cli(['vc', '+search', '--start', iso(from), '--end', iso(now), '--page-size', '30',
        ...(pageToken ? ['--page-token', pageToken] : [])], 'meeting search');
      for (const item of data.items ?? []) if (MEETING_ID.test(String(item?.id ?? ''))) meetings.push(String(item.id));
      if (!data.has_more || !data.page_token) break;
      pageToken = data.page_token;
    }
    let notes = 0;
    let queued = 0;
    const ids = [...new Set(meetings)];
    for (let index = 0; index < ids.length && !this.stopped; index += DETAIL_BATCH) {
      const data = await this.cli(['vc', '+detail', '--meeting-ids', ids.slice(index, index + DETAIL_BATCH).join(',')],
        'meeting detail', { timeoutMs: 180_000 });
      for (const meeting of data.meetings ?? []) {
        const noteId = String(meeting?.note_id ?? '');
        if (!NOTE_ID.test(noteId)) continue;
        notes += 1;
        if (this.state.enqueue(noteId, { meetingId: String(meeting.meeting_id ?? '') || null, source: 'scan', atMs: now })) queued += 1;
      }
    }
    if (this.stopped) return;
    this.state.set('scanned_until_ms', now);
    this.state.set('scanned_at_ms', now);
    this.logger.info('meeting_notes_scanned', { since: iso(from), meetings: ids.length, notes, queued });
  }

  arm() {
    if (this.stopped || !this.state) return;
    if (this.timer) this.clearTimer(this.timer);
    const next = this.state.nextAt();
    if (next === null) return;
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.after(() => this.work());
    }, Math.min(Math.max(0, next - this.now()), TICK_MS));
    this.timer?.unref?.();
  }

  // The due notes, one at a time.
  async work() {
    try {
      while (!this.stopped && this.state) {
        const row = this.state.due(this.now());
        if (!row) break;
        await this.store(row);
      }
    } finally {
      this.arm();
    }
  }

  async store(row) {
    const attempts = Number(row.attempts) + 1;
    try {
      const result = await this.archiveNote(row);
      if (this.stopped) return;
      const atMs = this.now();
      if (result.skipped) {
        this.state.settle(row.note_id, { status: 'skipped', error: result.skipped, attempts, atMs });
        this.logger.info('meeting_note_skipped', { note: opaqueRef(row.note_id), reason: result.skipped });
        return;
      }
      this.state.settle(row.note_id, { status: 'stored', meetingId: result.meetingId, revision: result.revision, attempts, atMs });
      this.logger.info('meeting_note_stored', {
        note: opaqueRef(row.note_id), meeting: opaqueRef(result.meetingId), source: row.source, attempts,
        noteChars: result.noteChars, transcriptChars: result.transcriptChars, chunked: result.chunked,
        lagMs: atMs - Number(row.received_at_ms),
      });
    } catch (error) {
      if (this.stopped) return;
      const delay = RETRY_DELAYS_MS[attempts - 1];
      const atMs = this.now();
      this.state.settle(row.note_id, {
        status: delay === undefined ? 'failed' : 'pending', error: failure(error), attempts,
        nextAtMs: delay === undefined ? null : atMs + delay, atMs,
      });
      this.logger.warn('meeting_note_failed', {
        note: opaqueRef(row.note_id), attempts, error: failure(error), ...(delay === undefined ? { final: true } : { retryInMs: delay }),
      });
    }
  }

  // Reads one note and what it belongs to, and stores them. Resolves with
  // { skipped } for a note that is not a meeting's.
  async archiveNote(row) {
    const { note = {} } = await this.cli(['api', 'GET', `/open-apis/vc/v1/notes/${encodeURIComponent(row.note_id)}`], 'note detail');
    const source = note.note_source ?? {};
    if (source.source_type && source.source_type !== 'meeting') return { skipped: `the note comes from ${String(source.source_type).slice(0, 40)}, not a meeting` };
    const meetingId = String(source.source_entity_id ?? row.meeting_id ?? '');
    if (!MEETING_ID.test(meetingId)) return { skipped: 'the note names no meeting' };
    const artifact = (type) => (note.artifacts ?? []).find((item) => item?.artifact_type === type && item.doc_token)?.doc_token ?? null;
    const docToken = artifact(NOTE_ARTIFACT);
    const verbatimToken = artifact(VERBATIM_ARTIFACT);
    if (!docToken && !verbatimToken) throw new Error('the note has no documents yet');
    const meeting = await this.meeting(meetingId);
    const summary = docToken ? await this.document(docToken) : null;
    let transcript = verbatimToken ? (await this.document(verbatimToken)).content : null;
    // A document that exists but is still empty is not written yet.
    if ((summary && !summary.content.trim()) || (verbatimToken && !transcript.trim())) throw new Error('the note documents are still empty');
    if (!transcript) transcript = await this.unifiedTranscript(row.note_id);
    const folder = meetingFolder(this.archive.root, meetingId);
    const transcriptUri = transcript ? `${folder}/transcript.md` : null;
    const noteText = noteMarkdown({
      meeting, summary: summary?.content ?? null, transcriptUri, storedAtMs: this.now(),
      note: { id: row.note_id, docToken, verbatimToken, revision: summary?.revision ?? null },
    });
    const transcriptText = transcript ? transcriptMarkdown({ meeting, transcript, noteUri: `${folder}/note.md` }) : null;
    const stored = await this.archive.store(meetingId, { note: noteText, transcript: transcriptText });
    return {
      meetingId, revision: summary?.revision ?? null, chunked: stored.chunked,
      noteChars: noteText.length, transcriptChars: transcriptText?.length ?? 0,
    };
  }

  // The meeting as the note's header shows it. What cannot be read leaves
  // just the id: the note is stored anyway.
  async meeting(meetingId) {
    let meeting;
    try {
      ({ meeting = {} } = await this.cli(['vc', 'meeting', 'get', '--params', JSON.stringify({ meeting_id: meetingId, with_participants: true })], 'meeting get'));
    } catch (error) {
      this.logger.warn('meeting_note_meeting_unreadable', { meeting: opaqueRef(meetingId), error: failure(error) });
      return { id: meetingId };
    }
    const people = (meeting.participants ?? []).filter((person) => person?.user_type === 1 && /^ou_[A-Za-z0-9_-]+$/.test(String(person.id)));
    const host = /^ou_[A-Za-z0-9_-]+$/.test(String(meeting.host_user?.id ?? '')) ? meeting.host_user.id : null;
    const names = await this.names([...new Set([...people.map((person) => person.id), ...(host ? [host] : [])])]);
    return {
      id: meetingId,
      number: /^\d{1,20}$/.test(String(meeting.meeting_no ?? '')) ? String(meeting.meeting_no) : null,
      topic: meeting.topic ?? null,
      startMs: seconds(meeting.start_time),
      endMs: seconds(meeting.end_time),
      host: host ? names.get(host)?.name ?? null : null,
      participants: people.map((person) => ({ name: names.get(person.id)?.name ?? null })),
      participantCount: Number.parseInt(String(meeting.participant_count ?? ''), 10) || people.length || null,
      external: (meeting.participants ?? []).some((person) => person?.is_external) || [...names.values()].some((entry) => entry.external),
      calendarEventId: typeof meeting.calendar_event_id === 'string' && meeting.calendar_event_id ? meeting.calendar_event_id.slice(0, 200) : null,
    };
  }

  // open_id -> { name, external }, for as many as the owner can look up.
  async names(ids) {
    const found = new Map();
    for (let index = 0; index < ids.length; index += NAME_BATCH) {
      try {
        const { users = [] } = await this.cli(['contact', '+search-user', '--user-ids', ids.slice(index, index + NAME_BATCH).join(',')], 'user lookup');
        for (const user of users) {
          if (user?.open_id && user.localized_name) found.set(user.open_id, { name: String(user.localized_name), external: Boolean(user.is_cross_tenant) });
        }
      } catch (error) {
        this.logger.warn('meeting_note_names_unavailable', { error: failure(error) });
        break;
      }
    }
    return found;
  }

  async document(token) {
    const { document = {} } = await this.cli(['docs', '+fetch', '--doc', token, '--doc-format', 'im-markdown'], 'document fetch',
      { timeoutMs: 120_000, stdoutLimit: DOC_STDOUT_LIMIT });
    const revision = Number.parseInt(String(document.revision_id ?? ''), 10);
    return { content: typeof document.content === 'string' ? document.content : '', revision: Number.isSafeInteger(revision) ? revision : null };
  }

  // A note shown as one page has its transcript in the note itself, which
  // only `note +transcript` can write out. No transcript leaves just the note.
  async unifiedTranscript(noteId) {
    const directory = path.join(this.config.tempDir || path.join(this.config.dataDir, 'tmp'), 'meeting-notes');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const name = `${noteId}.md`;
    try {
      await this.cli(['note', '+transcript', '--note-id', noteId, '--output', `./${name}`, '--overwrite', '--transcript-format', 'markdown'],
        'note transcript', { cwd: directory, timeoutMs: 120_000 });
      const text = fs.readFileSync(path.join(directory, name), 'utf8');
      return text.trim() ? text : null;
    } catch (error) {
      this.logger.info('meeting_note_without_transcript', { note: opaqueRef(noteId), error: failure(error) });
      return null;
    } finally {
      fs.rmSync(path.join(directory, name), { force: true });
    }
  }
}
