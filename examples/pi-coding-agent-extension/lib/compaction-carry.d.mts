import type { CompactionContinuation, CompactionImportReceipt, WorkItemCheckpoint } from "./takeover-core.mjs";

export const OVERVIEW_MARKER: "[OpenViking Session Context]";
export function transcriptEntry(entry: any): any;
export function transcriptText(entries: any[]): string;
export function renderWorkItemSummary(overview: string, entries: any[], recoveryHint?: string): string;
export function readCompactionContinuation(entry: any): CompactionContinuation | null;
export function compactionContinuation(overview: string, entries: any[], recoveryHint: string, coveredThroughEntryId: string): CompactionContinuation;
export function collectCompactionImports(branch: any[]): any[];
export function assembleCompactionCarry(args: {
  branch: any[];
  entries: any[];
  cut: number;
  through: number;
  archiveUri: string;
  checkpoint: WorkItemCheckpoint;
  isCapturedEntry: (entry: any) => boolean;
  compactionImports?: CompactionImportReceipt[];
}): { entries: any[]; replacedCompactions: Set<string> } | null;
