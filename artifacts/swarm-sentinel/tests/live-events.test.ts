import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canReconnectLive, countToolEvidence, matchesToolEvidence, mergeLiveEvents } from '../src/lib/live-events.ts';
import type { Trace } from '@workspace/api-client-react';

test('completion updates the same snapshot row without changing admission or counts', () => {
  const snapshot = [{ id: 'one', executed: true, executionStatus: '' }];
  const receipt = { ...snapshot[0], executionStatus: 'failed' };
  const result = mergeLiveEvents(snapshot, [], [receipt]);
  assert.deepEqual(result, [receipt]);
  assert.equal(snapshot[0].executionStatus, '');
  assert.equal(result.length, snapshot.length);
  assert.equal(result.filter((e) => e.executed).length, 1);
});

test('reconnect receipts and replayed events are deduplicated, preserving row order', () => {
  const snapshot = [{ id: 'one', status: 'succeeded' }, { id: 'two', status: '' }];
  const updates = [{ id: 'two', status: 'failed' }, { id: 'unknown', status: 'failed' }];
  const fresh = [{ id: 'two', status: '' }, { id: 'three', status: '' }, { id: 'three', status: '' }];
  const result = mergeLiveEvents(snapshot, fresh, updates);
  assert.deepEqual(result, [snapshot[0], updates[0], fresh[1]]);
  assert.deepEqual(mergeLiveEvents(result, fresh, updates), result);
});

test('reconnect retries offline and temporary server failures but not access errors', () => {
  assert.equal(canReconnectLive(new TypeError('Failed to fetch')), true);
  assert.equal(canReconnectLive({ status: 503 }), true);
  for (const status of [401, 403, 404, 409, 422]) {
    assert.equal(canReconnectLive({ status }), false);
  }
  assert.equal(canReconnectLive(new Error('unknown error')), false);
  assert.equal(canReconnectLive(null), false);
});

const tool = (id: string, evidence: Partial<Trace> = {}) => ({
  id, action: 'tool' as const, executed: true, ...evidence,
});

test('provenance and outcome compose; no evidence is unobserved, never failed', () => {
  const rows = [
    tool('engine-ok', { executionProvenance: 'engine-observed', executionStatus: 'succeeded' }),
    tool('caller-fail', { executionProvenance: 'caller-reported', executionStatus: 'failed' }),
    tool('blocked', { executed: false, executionProvenance: 'engine-observed', executionStatus: 'not-started' }),
    tool('pending'),
    { id: 'message', action: 'message' as const },
  ];
  const ids = (provenance: Parameters<typeof matchesToolEvidence>[1], outcome: Parameters<typeof matchesToolEvidence>[2]) =>
    rows.filter((e) => matchesToolEvidence(e, provenance, outcome)).map((e) => e.id);
  assert.deepEqual(ids('all', 'all'), rows.map((e) => e.id));
  assert.deepEqual(ids('engine-observed', 'all'), ['engine-ok', 'blocked']);
  assert.deepEqual(ids('caller-reported', 'failed'), ['caller-fail']);
  assert.deepEqual(ids('engine-observed', 'failed'), []);
  assert.deepEqual(ids('all', 'not-started'), ['blocked']);
  assert.deepEqual(ids('all', 'unobserved'), ['pending']);
  assert.deepEqual(ids('unobserved', 'all'), ['pending']);
  assert.deepEqual(ids('caller-reported', 'unobserved'), []);
});

test('a late receipt moves an existing action between filtered rows and counts exactly once', () => {
  const snapshot = [tool('pending'), tool('engine', {
    executionProvenance: 'engine-observed', executionStatus: 'succeeded',
  })];
  const receipt = tool('pending', { executionProvenance: 'caller-reported', executionStatus: 'failed' });
  const filtered = (rows: typeof snapshot, provenance: Parameters<typeof matchesToolEvidence>[1], outcome: Parameters<typeof matchesToolEvidence>[2]) =>
    rows.filter((e) => matchesToolEvidence(e, provenance, outcome));
  assert.equal(filtered(snapshot, 'all', 'unobserved').length, 1);
  assert.equal(filtered(snapshot, 'caller-reported', 'failed').length, 0);
  const updated = mergeLiveEvents(snapshot, [snapshot[0]], [receipt]);
  const replay = mergeLiveEvents(updated, snapshot, [receipt]);
  assert.deepEqual(replay, updated);
  assert.equal(updated.length, snapshot.length);
  assert.equal(updated.filter((e) => e.executed).length, 2);
  assert.deepEqual(updated.map((e) => e.id), snapshot.map((e) => e.id));
  assert.equal(filtered(updated, 'all', 'unobserved').length, 0);
  assert.deepEqual(filtered(updated, 'caller-reported', 'failed'), [receipt]);
  assert.deepEqual(countToolEvidence(filtered(updated, 'caller-reported', 'all')), {
    outcomes: { 'not-started': 0, succeeded: 0, failed: 1, unobserved: 0 },
    provenance: { 'engine-observed': 0, 'caller-reported': 1, unobserved: 0 },
  });
  assert.equal(countToolEvidence(snapshot).outcomes.unobserved, 1);
});

test('a receipt in the same batch as a new action replaces that row before filtering', () => {
  const fresh = tool('new');
  const receipt = tool('new', { executionProvenance: 'caller-reported', executionStatus: 'succeeded' });
  const rows = mergeLiveEvents([], [fresh, fresh], [receipt]);
  assert.deepEqual(rows, [receipt]);
  assert.equal(matchesToolEvidence(rows[0], 'caller-reported', 'succeeded'), true);
  assert.equal(countToolEvidence(rows).outcomes.succeeded, 1);
});

test('matching evidence counts include all rows before the display cap and ignore non-tools', () => {
  const rows = Array.from({ length: 405 }, (_, i) => tool(String(i), {
    executionProvenance: 'caller-reported', executionStatus: 'succeeded',
  }));
  const matching = rows.filter((e) => matchesToolEvidence(e, 'caller-reported', 'succeeded'));
  const counts = countToolEvidence([...matching, { action: 'message' }]);
  assert.equal(matching.slice(-400).length, 400);
  assert.equal(counts.outcomes.succeeded, 405);
  assert.equal(counts.provenance['caller-reported'], 405);
  assert.equal(counts.outcomes.unobserved, 0);
});