import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// These primitives replace nine passes' worth of hand-applied snapshots. The
// point of moving them here is that the invariants become directly testable
// rather than re-derived at every call site, so this file states them.

function core() {
  const ctx = loadScriptIntoContext('sidepanelCore.js', {
    document: { createElement: () => ({ textContent: '', innerHTML: '' }) },
    window: {},
  });
  return ctx.WV2NSidepanelCore;
}

const A = ['http://nas-a.example:52052', 'key-a'];
const B = ['http://nas-b.example:52052', 'key-b'];

describe('nasTarget', () => {
  const C = core();

  it('is frozen, because drift is the entire bug class', () => {
    const t = C.nasTarget(...A);
    expect(Object.isFrozen(t)).toBe(true);
    expect(() => { 'use strict'; t.endpoint = 'http://elsewhere'; }).toThrow();
  });

  it('has no half-configured form', () => {
    expect(C.nasTarget('http://nas', '')).toBeNull();
    expect(C.nasTarget('', 'key')).toBeNull();
    expect(C.nasTarget('   ', '  ')).toBeNull();
    expect(C.nasTarget(null, undefined)).toBeNull();
  });

  it('compares by identity, ignoring incidental whitespace', () => {
    expect(C.sameNasTarget(C.nasTarget(...A), C.nasTarget('  http://nas-a.example:52052 ', ' key-a '))).toBe(true);
    expect(C.sameNasTarget(C.nasTarget(...A), C.nasTarget(...B))).toBe(false);
  });

  it('treats a changed key as a different NAS, not the same one', () => {
    // Same host, rotated credential: results fetched with the old key must not
    // be attributed to the new configuration.
    const before = C.nasTarget('http://nas.example:52052', 'old');
    const after = C.nasTarget('http://nas.example:52052', 'new');
    expect(C.sameNasTarget(before, after)).toBe(false);
  });

  it('never matches a missing target', () => {
    expect(C.sameNasTarget(null, C.nasTarget(...A))).toBe(false);
    expect(C.sameNasTarget(C.nasTarget(...A), null)).toBe(false);
    expect(C.sameNasTarget(null, null)).toBe(false);
  });
});

describe('createRequestGuard', () => {
  const C = core();

  it('lets only the newest request of a stream apply', () => {
    const guard = C.createRequestGuard();
    const target = C.nasTarget(...A);
    const first = guard.begin('jobs', target);
    const second = guard.begin('jobs', target);

    expect(first(target)).toBe(false);
    expect(second(target)).toBe(true);
  });

  it('refuses a response whose NAS is no longer the current one', () => {
    const guard = C.createRequestGuard();
    const issued = guard.begin('jobs', C.nasTarget(...A));
    // The user switched profile while it was in flight.
    expect(issued(C.nasTarget(...B))).toBe(false);
  });

  it('keeps streams independent', () => {
    const guard = C.createRequestGuard();
    const target = C.nasTarget(...A);
    const jobs = guard.begin('jobs', target);
    guard.begin('connection', target);
    guard.begin('connection', target);

    // A connection check must not invalidate an in-flight jobs poll.
    expect(jobs(target)).toBe(true);
  });

  it('invalidates in-flight work when configuration is cleared', () => {
    const guard = C.createRequestGuard();
    const issued = guard.begin('jobs', C.nasTarget(...A));
    // begin() with no target still opens a generation, superseding the old one.
    guard.begin('jobs', null);
    expect(issued(C.nasTarget(...A))).toBe(false);
  });

  it('never applies a request that had no target to begin with', () => {
    const guard = C.createRequestGuard();
    const issued = guard.begin('jobs', null);
    expect(issued(C.nasTarget(...A))).toBe(false);
    expect(issued(null)).toBe(false);
  });
});

describe('cancel outcome table', () => {
  const C = core();

  it('classifies definite success', () => {
    for (const status of [200, 204]) {
      expect(C.classifyCancelResponse(status)).toBe(C.CANCEL_OUTCOME.CANCELLED);
    }
  });

  it('classifies answers that prove nothing as unknown', () => {
    // A proxy can answer 504 after the backend committed, and the API commits
    // the status flip before it reads metadata, so 500 can follow a cancel
    // that did happen.
    for (const status of [408, 429, 500, 502, 503, 504]) {
      expect(C.classifyCancelResponse(status)).toBe(C.CANCEL_OUTCOME.UNKNOWN);
    }
  });

  it('classifies deterministic refusals as rejected', () => {
    // Safe because DELETE is idempotent: an already-cancelled job answers 2xx,
    // so a 404 really is an unknown job.
    for (const status of [400, 401, 403, 404, 409]) {
      expect(C.classifyCancelResponse(status)).toBe(C.CANCEL_OUTCOME.REJECTED);
    }
  });

  it('treats a request that never answered as unknown', () => {
    expect(C.classifyCancelFailure()).toBe(C.CANCEL_OUTCOME.UNKNOWN);
  });

  it('only stops the browser upload on a confirmed cancellation', () => {
    const { CANCELLED, REJECTED, UNKNOWN } = C.CANCEL_OUTCOME;
    expect(C.resolveCancelAction(CANCELLED, null)).toBe('apply');
    expect(C.resolveCancelAction(REJECTED, true)).toBe('report');   // no reconcile is even asked for
    expect(C.resolveCancelAction(UNKNOWN, true)).toBe('apply');
    expect(C.resolveCancelAction(UNKNOWN, false)).toBe('report');
    // null = could not ask. Not the same as "not cancelled", but it is equally
    // not evidence, so it must not claim success.
    expect(C.resolveCancelAction(UNKNOWN, null)).toBe('report');
  });
});
