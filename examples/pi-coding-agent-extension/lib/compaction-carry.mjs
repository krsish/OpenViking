// A Pi compaction is a container, not a new piece of conversation. Keep its
// replaceable OV view separate from original entries that still need carrying.
export const OVERVIEW_MARKER = "[OpenViking Session Context]";
const FORMAT_VERSION = 1;

export function transcriptEntry(entry) {
  if (entry?.type === "message") return { id: entry.id, type: entry.type, message: entry.message };
  if (["custom_message", "branch_summary", "compaction"].includes(entry?.type)) return entry;
  return null;
}

export function transcriptText(entries) {
  return entries.map(transcriptEntry).filter(Boolean).map((entry) => JSON.stringify(entry)).join("\n");
}

export function renderWorkItemSummary(overview, entries, recoveryHint = "") {
  const raw = transcriptText(entries);
  return `${OVERVIEW_MARKER}\n${overview}` +
    (raw ? `\n\nUncheckpointed Pi transcript (verbatim JSONL; historical context):\n${raw}` : "") +
    recoveryHint;
}

function archiveLocation(uri) {
  const match = /^(.*\/history)\/archive_(\d+)$/.exec(String(uri || ""));
  return match ? { history: match[1], index: Number(match[2]) } : null;
}

function validEntry(entry) {
  return typeof entry?.id === "string" && Boolean(entry.id) &&
    (entry.type === "message" ? entry.message && typeof entry.message.role === "string" :
      ["custom_message", "branch_summary", "compaction"].includes(entry.type));
}

/** Unknown/old formats stay opaque; never infer a replaceable view from a label. */
export function readCompactionContinuation(entry) {
  const details = entry?.details;
  const state = details?.continuation;
  if (entry?.type !== "compaction" || details?.source !== "openviking" ||
      details.mode !== "work_item" || state?.version !== FORMAT_VERSION ||
      !archiveLocation(details.archiveUri) ||
      typeof details.checkpointEndingMessageId !== "string" || !details.checkpointEndingMessageId ||
      typeof state.coveredThroughEntryId !== "string" || !state.coveredThroughEntryId ||
      typeof state.overview !== "string" || !state.overview ||
      typeof state.recoveryHint !== "string" || !Array.isArray(state.entries) ||
      !state.entries.every(validEntry)) return null;
  // Context edits, partial writes and future formats cannot silently lose text
  // by supplying metadata that no longer describes the actual saved summary.
  if (renderWorkItemSummary(state.overview, state.entries, state.recoveryHint) !== entry.summary) return null;
  return state;
}

export function compactionContinuation(overview, entries, recoveryHint, coveredThroughEntryId) {
  return {
    version: FORMAT_VERSION, overview, recoveryHint, coveredThroughEntryId,
    entries: JSON.parse(JSON.stringify(entries)),
  };
}

/** Opaque summaries are imported as historical assistant context, never new user evidence. */
export function collectCompactionImports(branch) {
  const latest = [...branch].reverse().find((entry) => entry?.type === "compaction");
  if (!latest) return [];
  const state = readCompactionContinuation(latest);
  return (state ? state.entries : [latest]).filter((entry) =>
    entry?.type === "compaction" && typeof entry.id === "string" && entry.id &&
    typeof entry.summary === "string" && entry.summary);
}

function sameEntry(a, b) {
  return JSON.stringify(transcriptEntry(a)) === JSON.stringify(transcriptEntry(b));
}

/**
 * Rebuild from original entries, never from a previously rendered OV summary.
 * The caller has verified the ready checkpoint, delivery barrier and active Pi
 * compaction ID. Raw branch positions prove which carried messages it covers.
 * null means the saved view cannot safely be replaced on this branch/session.
 */
export function assembleCompactionCarry({
  branch, entries, cut, through, archiveUri, checkpoint, isCapturedEntry,
  compactionImports = [],
}) {
  const rawThrough = branch.findIndex((entry) => entry?.id === entries[through]?.id);
  if (rawThrough < 0) return null;
  const positions = new Map();
  for (let i = 0; i < branch.length; i++) {
    if (typeof branch[i]?.id === "string") {
      if (positions.has(branch[i].id)) return null;
      positions.set(branch[i].id, i);
    }
  }
  const carry = [];
  const byId = new Map();
  const replacedCompactions = new Set();
  const add = (entry) => {
    const value = transcriptEntry(entry);
    if (!value) return true;
    const key = value.id;
    if (typeof key !== "string" || !key) return false;
    if (byId.has(key)) return sameEntry(byId.get(key), value);
    byId.set(key, value);
    carry.push(value);
    return true;
  };
  const imported = (entry) => compactionImports.some((receipt) =>
    receipt.entryId === entry.id && receipt.summary === entry.summary &&
    positions.has(receipt.anchorEntryId) && positions.get(receipt.anchorEntryId) <= rawThrough);
  const covered = (entry) => {
    const index = positions.get(entry.id);
    return index !== undefined && index <= rawThrough && sameEntry(branch[index], entry) &&
      entry.type === "message" && isCapturedEntry(entry);
  };
  for (let i = 0; i < cut; i++) {
    const entry = entries[i];
    if (entry?.type === "compaction") {
      if (imported(entry)) {
        replacedCompactions.add(entry.id);
        continue;
      }
      const state = readCompactionContinuation(entry);
      if (state) {
        const old = archiveLocation(entry.details.archiveUri);
        const next = archiveLocation(archiveUri);
        const oldThrough = positions.get(state.coveredThroughEntryId);
        const compactionIndex = positions.get(entry.id);
        if (!next || old.history !== next.history || old.index > next.index ||
            (old.index === next.index && entry.details.checkpointEndingMessageId !== checkpoint.ending_message_id) ||
            oldThrough === undefined || oldThrough > rawThrough ||
            compactionIndex === undefined || oldThrough >= compactionIndex) return null;
        for (const original of state.entries) {
          if (covered(original) || (original.type === "compaction" && imported(original))) continue;
          if (!add(original)) return null;
        }
        replacedCompactions.add(entry.id);
      } else if (!add(entry)) return null;
    } else if (i > through || !isCapturedEntry(entry)) {
      if (!add(entry)) return null;
    }
  }
  return { entries: carry, replacedCompactions };
}
