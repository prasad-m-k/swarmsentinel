import type { Trace } from '@workspace/api-client-react';

export type ProvenanceFilter = 'all' | NonNullable<Trace['executionProvenance']> | 'unobserved';
export type OutcomeFilter = 'all' | NonNullable<Trace['executionStatus']> | 'unobserved';

type EvidenceEvent = Pick<Trace, 'action' | 'executionProvenance' | 'executionStatus'>;

/** Evidence filters apply only to tools; absence is unknown, never failure. */
export function matchesToolEvidence(event: EvidenceEvent, provenance: ProvenanceFilter, outcome: OutcomeFilter): boolean {
  if (provenance === 'all' && outcome === 'all') return true;
  return event.action === 'tool'
    && (provenance === 'all' || (event.executionProvenance ?? 'unobserved') === provenance)
    && (outcome === 'all' || (event.executionStatus ?? 'unobserved') === outcome);
}

/** Count matching tools before the feed's display cap, not global admissions. */
export function countToolEvidence(events: EvidenceEvent[]) {
  const outcomes = { 'not-started': 0, succeeded: 0, failed: 0, unobserved: 0 };
  const provenance = { 'engine-observed': 0, 'caller-reported': 0, unobserved: 0 };
  for (const event of events) {
    if (event.action !== 'tool') continue;
    outcomes[event.executionStatus ?? 'unobserved']++;
    provenance[event.executionProvenance ?? 'unobserved']++;
  }
  return { outcomes, provenance };
}

/** Receipts replace rows; reconnect replay must not count an action twice. */
export function mergeLiveEvents<T extends { id: string }>(current: T[], fresh: T[], updates: T[] = []): T[] {
  const replacements = new Map(updates.map((event) => [event.id, event]));
  const known = new Set(current.map((event) => event.id));
  const merged = current.map((event) => replacements.get(event.id) ?? event);
  for (const event of fresh) {
    if (!known.has(event.id)) {
      merged.push(replacements.get(event.id) ?? event);
      known.add(event.id);
    }
  }
  return merged;
}

/** Retry dropped connections, not expired auth or deleted/inaccessible sessions. */
export function canReconnectLive(error: unknown): boolean {
  if (error instanceof TypeError) return true; // fetch failed before an HTTP response
  if (!error || typeof error !== 'object' || !('status' in error)) return false;
  return typeof error.status === 'number' && error.status >= 500 && error.status <= 599;
}