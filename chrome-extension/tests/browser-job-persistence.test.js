// Codex review #12: browser-job persistence + SW-boot watchdog tests.
//
// MV3 evicts service workers aggressively. Multi-minute HLS/DASH
// downloads can outlive the SW that started them. Without persistence,
// the in-memory promise + active-jobs map dies; the only code path
// that removes DNR rules and aborts the server-side staging job is
// gone. Persisted state + boot-time watchdog ensures stale jobs get
// cleaned up next time the SW initialises.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';
import path from 'node:path';

const BACKGROUND_SCRIPT = path.resolve(__dirname, '..', 'background.js');


function makeChromeStub({ storageInitial = {}, abortFetchOk = true, dnrFn } = {}) {
  const storage = { local: { ...storageInitial } };
  const noop = () => {};
  return {
    runtime: {
      sendMessage: vi.fn(), id: 'test-extension-id',
      onMessage: { addListener: noop, removeListener: noop },
      onInstalled: { addListener: noop }, lastError: null,
      openOptionsPage: noop, getManifest: () => ({ version: '2.5.0' }),
      getURL: (p) => `chrome-extension://test-extension-id/${p}`,
    },
    storage: {
      local: {
        get: vi.fn(async (key) => {
          if (typeof key === 'string') {
            return key in storage.local ? { [key]: storage.local[key] } : {};
          }
          return { ...storage.local };
        }),
        set: vi.fn(async (obj) => { Object.assign(storage.local, obj); }),
        remove: vi.fn(async (key) => { delete storage.local[key]; }),
        _state: storage.local,
      },
      sync: { get: (_keys, cb) => cb && cb({}), set: async () => {} },
      onChanged: { addListener: noop },
    },
    webRequest: {
      onBeforeRequest: { addListener: noop },
      onSendHeaders: { addListener: noop },
      onHeadersReceived: { addListener: noop },
    },
    webNavigation: { onCommitted: { addListener: noop } },
    action: {
      setBadgeText: noop, setBadgeBackgroundColor: noop,
      onClicked: { addListener: noop },
    },
    tabs: {
      onRemoved: { addListener: noop }, onUpdated: { addListener: noop },
      onActivated: { addListener: noop },
      query: (_q, cb) => cb && cb([]), get: (_id, cb) => cb && cb(null),
    },
    contextMenus: { create: noop, onClicked: { addListener: noop } },
    notifications: {
      create: vi.fn(), clear: noop,
      onClicked: { addListener: noop }, onClosed: { addListener: noop },
    },
    cookies: { getAll: async () => [] },
    sidePanel: { setOptions: async () => {}, open: async () => {} },
    declarativeNetRequest: {
      updateSessionRules: dnrFn || vi.fn().mockResolvedValue(),
    },
    offscreen: {
      hasDocument: vi.fn().mockResolvedValue(false),
      createDocument: vi.fn().mockResolvedValue(),
      closeDocument: vi.fn().mockResolvedValue(),
    },
  };
}


function loadBackground({ chrome, fetchStub }) {
  return loadScriptIntoContext(BACKGROUND_SCRIPT, {
    chrome,
    fetch: fetchStub || vi.fn().mockResolvedValue({ ok: true }),
    AbortController, AbortSignal,
    Promise, Map, Set, Error, JSON, RegExp, Math, Date, Number, Array, Object,
  });
}


describe('persistence helpers', () => {
  it('_wv2nasPersistBrowserJob writes to chrome.storage.local under the canonical key', async () => {
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome });
    await ctx._wv2nasPersistBrowserJob('job-A', {
      ruleIds: [10000, 15000], dnrSlot: 0, startedAt: 12345,
      nasEndpoint: 'http://nas/', apiKey: 'k',
    });
    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    expect(stored).toBeDefined();
    expect(stored['job-A']).toMatchObject({
      jobId: 'job-A',
      ruleIds: [10000, 15000],
      dnrSlot: 0,
      startedAt: 12345,
      nasEndpoint: 'http://nas/',
      apiKey: 'k',
    });
  });

  it('_wv2nasPersistBrowserJob merges into existing entry (partial updates)', async () => {
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome });
    await ctx._wv2nasPersistBrowserJob('job-A', {
      ruleIds: [10000], dnrSlot: 0, startedAt: 12345,
      nasEndpoint: 'http://nas/', apiKey: 'k',
    });
    // Partial update — should not lose nasEndpoint/apiKey/etc.
    await ctx._wv2nasPersistBrowserJob('job-A', { ruleIds: [10000, 10001, 15000, 15001] });
    const stored = chrome.storage.local._state.wv2nasBrowserJobs['job-A'];
    expect(stored.ruleIds).toEqual([10000, 10001, 15000, 15001]);
    expect(stored.nasEndpoint).toBe('http://nas/');
    expect(stored.apiKey).toBe('k');
    expect(stored.dnrSlot).toBe(0);
  });

  it('_wv2nasUnpersistBrowserJob removes entry; idempotent on missing', async () => {
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: { 'job-A': { jobId: 'job-A' }, 'job-B': { jobId: 'job-B' } },
      },
    });
    const ctx = loadBackground({ chrome });
    await ctx._wv2nasUnpersistBrowserJob('job-A');
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({
      'job-B': { jobId: 'job-B' },
    });
    // Idempotent: removing again doesn't throw or trash other entries.
    await ctx._wv2nasUnpersistBrowserJob('job-A');
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({
      'job-B': { jobId: 'job-B' },
    });
  });
});


describe('_wv2nasRecoverStaleBrowserJobs', () => {
  let dnrCalls;
  let fetchCalls;

  function makeFetchStub() {
    return vi.fn(async (url, opts) => {
      fetchCalls.push({ url: String(url), method: opts && opts.method });
      return { ok: true, status: 200, json: async () => ({}) };
    });
  }

  beforeEach(() => {
    dnrCalls = [];
    fetchCalls = [];
  });

  it('cleans up jobs older than the stale threshold', async () => {
    // Load with EMPTY storage first so the top-level boot-time
    // watchdog invocation has nothing to do. Then plant the stale
    // state and trigger the recovery explicitly.
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    const chrome = makeChromeStub({ dnrFn });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0)); // let boot watchdog drain

    const oldStart = Date.now() - 2 * 60 * 60 * 1000; // 2 hours ago
    chrome.storage.local._state.wv2nasBrowserJobs = {
      'old-job': {
        jobId: 'old-job', ruleIds: [10000, 15000], dnrSlot: 0,
        startedAt: oldStart,
        nasEndpoint: 'http://nas/', apiKey: 'k',
      },
    };
    dnrCalls.length = 0;
    fetchCalls.length = 0;

    await ctx._wv2nasRecoverStaleBrowserJobs();

    // DNR rules removed.
    expect(dnrCalls).toHaveLength(1);
    expect(dnrCalls[0]).toEqual({ removeRuleIds: [10000, 15000] });
    // Abort POST sent.
    const abortCall = fetchCalls.find((c) => c.url.endsWith('/old-job/abort'));
    expect(abortCall).toBeDefined();
    expect(abortCall.method).toBe('POST');
    // Persisted entry removed.
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
    // Slot released back to the pool. Use __eval since `const` doesn't
    // attach to vm context globalThis.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(0)')).toBe(false);
  });

  it('preserves recent jobs (under stale threshold)', async () => {
    const recentStart = Date.now() - 5 * 60 * 1000; // 5 minutes ago
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'recent-job': {
            jobId: 'recent-job', ruleIds: [10000], dnrSlot: 0,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });

    await ctx._wv2nasRecoverStaleBrowserJobs();

    // No DNR cleanup (recent job — leave alone).
    expect(chrome.declarativeNetRequest.updateSessionRules).not.toHaveBeenCalled();
    // No abort POST.
    expect(fetchCalls.find((c) => c.url.includes('/abort'))).toBeUndefined();
    // Entry preserved.
    expect(chrome.storage.local._state.wv2nasBrowserJobs['recent-job']).toBeDefined();
  });

  it('retries abortPending jobs even when heartbeat is recent', async () => {
    const recent = Date.now() - 30 * 1000;
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));
    fetchCalls.length = 0;
    chrome.storage.local._state.wv2nasBrowserJobs = {
      'pending-abort': {
        jobId: 'pending-abort',
        ruleIds: [],
        dnrSlot: null,
        startedAt: recent,
        lastHeartbeat: Date.now(),
        abortPending: true,
        abortReason: 'previous abort failed',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
      },
    };

    await ctx._wv2nasRecoverStaleBrowserJobs();

    const abortCall = fetchCalls.find((c) => c.url.endsWith('/pending-abort/abort'));
    expect(abortCall).toBeDefined();
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
  });

  it('handles empty / missing persisted state', async () => {
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    // Should not throw, should not error.
    await ctx._wv2nasRecoverStaleBrowserJobs();
    expect(fetchCalls).toEqual([]);
  });

  it('removes only stale entries; mixes recent + stale together', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    const chrome = makeChromeStub({ dnrFn });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    const oldStart = Date.now() - 3 * 60 * 60 * 1000;
    const recentStart = Date.now() - 1 * 60 * 1000;
    chrome.storage.local._state.wv2nasBrowserJobs = {
      'old': {
        jobId: 'old', ruleIds: [10000], dnrSlot: 0,
        startedAt: oldStart, nasEndpoint: 'http://nas/', apiKey: 'k',
      },
      'recent': {
        jobId: 'recent', ruleIds: [10100], dnrSlot: 1,
        startedAt: recentStart, nasEndpoint: 'http://nas/', apiKey: 'k',
      },
    };
    dnrCalls.length = 0;
    fetchCalls.length = 0;

    await ctx._wv2nasRecoverStaleBrowserJobs();

    expect(dnrCalls).toHaveLength(1);
    expect(dnrCalls[0]).toEqual({ removeRuleIds: [10000] });
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({
      'recent': expect.objectContaining({ jobId: 'recent' }),
    });
  });

  // Codex review #14: SW restart loses the _wv2nasUsedDnrSlots set.
  // If a non-stale persisted survivor's slot isn't re-reserved on
  // boot, a new job can allocate the same slot and call
  // updateSessionRules with overlapping IDs — clobbering the running
  // job's still-active DNR rules.
  it('restores DNR slot reservations for non-stale survivors', async () => {
    const recentStart = Date.now() - 2 * 60 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'survivor-A': {
            jobId: 'survivor-A', ruleIds: [10000], dnrSlot: 0,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
          'survivor-B': {
            jobId: 'survivor-B', ruleIds: [10100], dnrSlot: 1,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));  // let boot watchdog drain

    // Survivor slots 0 and 1 should now be reserved.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(0)')).toBe(true);
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(1)')).toBe(true);
    // Slot 2 still free.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(2)')).toBe(false);

    // Active jobs map also restored so offscreen ref-count counts the
    // survivors.
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.size")).toBe(2);
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('survivor-A')")).toBe(true);
  });

  it('does NOT restore slot when survivor lacks dnrSlot field', async () => {
    // Defensive: legacy persisted entries without dnrSlot shouldn't
    // crash recovery.
    const recentStart = Date.now() - 2 * 60 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'malformed-survivor': {
            jobId: 'malformed-survivor', ruleIds: [10000],
            startedAt: recentStart,
            nasEndpoint: 'http://nas/', apiKey: 'k',
            // dnrSlot intentionally missing
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // No slot was added (since none was persisted) — but the active
    // jobs entry still exists for offscreen ref-counting.
    expect(ctx.__eval("_wv2nasUsedDnrSlots.size")).toBe(0);
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('malformed-survivor')")).toBe(true);
  });

  // Codex review #15: durable completion handler for SW-restart
  // recovery. When the per-runBrowserSideJob in-memory listener died
  // with the previous SW instance, the offscreen's eventual completion
  // message must STILL trigger DNR cleanup + slot release + persistence
  // delete + offscreen close. Without this, surviving recent jobs leak
  // their state forever (until the next 1h-watchdog boot).
  it('durable handler cleans up DNR + slot + persistence on BROWSER_JOB_DONE', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    const recentStart = Date.now() - 3 * 60 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'survivor-job': {
            jobId: 'survivor-job',
            ruleIds: [10000, 15000],
            dnrSlot: 0,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/',
            apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    // Pretend an offscreen document exists (so _closeOffscreenDocument
    // actually invokes closeDocument when active map empties).
    chrome.offscreen.hasDocument = vi.fn().mockResolvedValue(true);

    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));  // boot watchdog drains

    // Verify recovery state: slot reserved, active map populated.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(0)')).toBe(true);
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('survivor-job')")).toBe(true);
    dnrCalls.length = 0;

    // Now simulate the offscreen completing. The per-job in-memory
    // listener is gone (lost with previous SW). The durable handler
    // should still clean up.
    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_DONE',
      payload: { jobId: 'survivor-job', summary: { totalSegments: 200 } },
    });

    // DNR rules removed.
    expect(dnrCalls).toHaveLength(1);
    expect(dnrCalls[0]).toEqual({ removeRuleIds: [10000, 15000] });
    // Slot released.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(0)')).toBe(false);
    // Active map cleared.
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('survivor-job')")).toBe(false);
    // Persisted entry gone.
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
    // Offscreen closed (no more active jobs).
    expect(chrome.offscreen.closeDocument).toHaveBeenCalled();
  });

  it('durable handler is idempotent (no-op when persisted entry already gone)', async () => {
    const chrome = makeChromeStub();  // empty storage
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // Send completion for a job that doesn't exist in storage —
    // should be a clean no-op.
    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_DONE',
      payload: { jobId: 'never-existed' },
    });

    // No DNR calls, no abort calls, no errors.
    expect(chrome.declarativeNetRequest.updateSessionRules).not.toHaveBeenCalled();
  });

  it('durable handler calls abort on FAILED + !finalizeAttempted', async () => {
    const dnrFn = vi.fn(async () => {});
    const recentStart = Date.now() - 30 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'failed-job': {
            jobId: 'failed-job',
            ruleIds: [10000],
            dnrSlot: 2,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/',
            apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));
    fetchCalls.length = 0;

    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_FAILED',
      payload: {
        jobId: 'failed-job',
        error: 'Segment 47 returned 403',
        finalizeAttempted: false,
      },
    });

    // Abort POST sent.
    const abortCall = fetchCalls.find((c) => c.url.endsWith('/failed-job/abort'));
    expect(abortCall).toBeDefined();
    expect(abortCall.method).toBe('POST');
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
  });

  it('durable handler preserves abort retry state when abort request fails', async () => {
    const dnrFn = vi.fn(async () => {});
    const fetchStub = vi.fn(async (url, opts) => {
      fetchCalls.push({ url: String(url), method: opts && opts.method });
      if (String(url).endsWith('/abort')) throw new Error('network');
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const recentStart = Date.now() - 30 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'failed-job': {
            jobId: 'failed-job',
            ruleIds: [10000],
            dnrSlot: 2,
            startedAt: recentStart,
            lastHeartbeat: Date.now(),
            nasEndpoint: 'http://nas/',
            apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub });
    await new Promise((r) => setTimeout(r, 0));
    fetchCalls.length = 0;

    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_FAILED',
      payload: {
        jobId: 'failed-job',
        error: 'Segment 47 returned 403',
        finalizeAttempted: false,
      },
    });

    const abortCall = fetchCalls.find((c) => c.url.endsWith('/failed-job/abort'));
    expect(abortCall).toBeDefined();
    const stored = chrome.storage.local._state.wv2nasBrowserJobs['failed-job'];
    expect(stored).toMatchObject({
      abortPending: true,
      abortReason: 'Segment 47 returned 403',
      lastHeartbeat: 0,
      ruleIds: [],
      dnrSlot: null,
    });
  });

  it('durable handler skips abort when FAILED came from an accepted user cancel', async () => {
    const dnrFn = vi.fn(async () => {});
    const recentStart = Date.now() - 30 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'cancelled-job': {
            jobId: 'cancelled-job',
            ruleIds: [10000],
            dnrSlot: 2,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/',
            apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));
    fetchCalls.length = 0;

    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_FAILED',
      payload: {
        jobId: 'cancelled-job',
        error: 'cancelled',
        finalizeAttempted: false,
        userCancelled: true,
      },
    });

    const abortCall = fetchCalls.find((c) => c.url.includes('/abort'));
    expect(abortCall).toBeUndefined();
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
  });

  it('durable handler does NOT abort on FAILED + finalizeAttempted (Codex #4 invariant)', async () => {
    const dnrFn = vi.fn(async () => {});
    const recentStart = Date.now() - 30 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'finalize-failed-job': {
            jobId: 'finalize-failed-job',
            ruleIds: [10000],
            dnrSlot: 3,
            startedAt: recentStart,
            nasEndpoint: 'http://nas/',
            apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));
    fetchCalls.length = 0;

    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_FAILED',
      payload: {
        jobId: 'finalize-failed-job',
        error: 'finalize timeout',
        finalizeAttempted: true,  // ← critical
      },
    });

    // No abort POST — server may have committed the queue push;
    // calling abort would destroy a queued job.
    const abortCall = fetchCalls.find((c) => c.url.includes('/abort'));
    expect(abortCall).toBeUndefined();
  });

  // Codex review #16: heartbeat-based liveness. A persisted job with
  // an OLD startedAt but a RECENT lastHeartbeat is still uploading —
  // watchdog must NOT reap it. Without this, slow downloads that span
  // longer than _BROWSER_JOB_STALE_MS through SW restarts get
  // destroyed mid-download.
  it('preserves jobs with recent heartbeat even if startedAt is old', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    // startedAt is 2h ago (well past stale threshold) but heartbeat
    // was 10s ago — offscreen is clearly alive.
    const oldStart = Date.now() - 2 * 60 * 60 * 1000;
    const recentHeartbeat = Date.now() - 10 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'slow-but-alive': {
            jobId: 'slow-but-alive', ruleIds: [10000], dnrSlot: 0,
            startedAt: oldStart, lastHeartbeat: recentHeartbeat,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // No DNR removal, no abort POST — job is alive.
    expect(dnrCalls).toEqual([]);
    expect(fetchCalls.find((c) => c.url.includes('/abort'))).toBeUndefined();
    // Persisted entry preserved.
    expect(chrome.storage.local._state.wv2nasBrowserJobs['slow-but-alive']).toBeDefined();
    // Slot still reserved.
    expect(ctx.__eval('_wv2nasUsedDnrSlots.has(0)')).toBe(true);
  });

  it('reaps jobs whose heartbeat is stale (offscreen presumed dead)', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    // startedAt 30 min ago (under STALE_MS), but heartbeat is 10 min
    // old (> HEARTBEAT_TIMEOUT_MS=5 min). Offscreen probably died.
    const recentStart = Date.now() - 30 * 60 * 1000;
    const oldHeartbeat = Date.now() - 10 * 60 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'heartbeat-died': {
            jobId: 'heartbeat-died', ruleIds: [10000], dnrSlot: 0,
            startedAt: recentStart, lastHeartbeat: oldHeartbeat,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // Reaped: DNR + abort + entry removed.
    expect(dnrCalls).toHaveLength(1);
    expect(fetchCalls.find((c) => c.url.includes('/abort'))).toBeDefined();
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
  });

  it('falls back to startedAt for jobs that never heartbeated (legacy entries)', async () => {
    // Pre-fix persisted entries don't have lastHeartbeat. Use startedAt
    // age to decide; old entries get reaped, new entries kept.
    const dnrFn = vi.fn(async () => {});
    const oldStart = Date.now() - 2 * 60 * 60 * 1000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'legacy-no-heartbeat': {
            jobId: 'legacy-no-heartbeat', ruleIds: [10000], dnrSlot: 0,
            startedAt: oldStart,  // no lastHeartbeat field
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // Reaped via legacy startedAt path.
    expect(chrome.storage.local._state.wv2nasBrowserJobs).toEqual({});
  });

  it('persists lastHeartbeat when BROWSER_JOB_HEARTBEAT message arrives', async () => {
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'tracker': {
            jobId: 'tracker', ruleIds: [10000], dnrSlot: 0,
            startedAt: Date.now(), nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });
    await new Promise((r) => setTimeout(r, 0));

    // Simulate offscreen sending a heartbeat.
    const ts = Date.now();
    await ctx._wv2nasPersistBrowserJob('tracker', { lastHeartbeat: ts });

    // Persisted state has lastHeartbeat.
    expect(chrome.storage.local._state.wv2nasBrowserJobs['tracker'].lastHeartbeat).toBe(ts);
  });

  it('late heartbeat for an unpersisted job does not recreate it', async () => {
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'done-job': {
            jobId: 'done-job', ruleIds: [10000], dnrSlot: 0,
            startedAt: Date.now(), nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });

    await ctx._wv2nasUnpersistBrowserJob('done-job');
    await ctx._wv2nasPersistBrowserJobHeartbeat('done-job', Date.now());

    expect(chrome.storage.local._state.wv2nasBrowserJobs['done-job']).toBeUndefined();
  });


  // Codex review #19a: durable completion handler must NOT decide
  // whether to close the offscreen document until SW-boot recovery
  // has finished populating _wv2nasActiveBrowserJobs. Without
  // gating, a concurrent second job can be torn down at boot
  // because the in-memory active map looks empty when the first
  // job's DONE message arrives.
  it('durable completion at boot waits for recovery to populate active map (concurrent jobs)', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    // TWO alive survivor jobs (recent heartbeat) — both should be
    // restored to _wv2nasActiveBrowserJobs during boot.
    const ts = Date.now();
    const recent = ts - 5_000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'job-A': {
            jobId: 'job-A', ruleIds: [10000], dnrSlot: 0,
            startedAt: ts, lastHeartbeat: recent,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
          'job-B': {
            jobId: 'job-B', ruleIds: [10100], dnrSlot: 1,
            startedAt: ts, lastHeartbeat: recent,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    chrome.offscreen.hasDocument = vi.fn().mockResolvedValue(true);

    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });

    // CRITICAL: do NOT drain microtasks yet. The durable handler is
    // invoked while boot recovery is still in flight. With the
    // gate, the handler awaits the same recovery promise that boot
    // kicked off, so by the time it inspects the active map BOTH
    // survivor jobs are present.
    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_DONE',
      payload: { jobId: 'job-A' },
    });

    // job-A cleanly removed.
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('job-A')")).toBe(false);
    // job-B preserved — recovery added it before the handler ran its
    // size check.
    expect(ctx.__eval("_wv2nasActiveBrowserJobs.has('job-B')")).toBe(true);
    // CORE INVARIANT: offscreen NOT closed because job-B is still
    // downloading. Without the gate, this would be called.
    expect(chrome.offscreen.closeDocument).not.toHaveBeenCalled();
  });

  it('durable completion still closes offscreen when ALL jobs finish (post-recovery race resolution)', async () => {
    // Sanity counter-test: with the gate, single-job completion
    // should still close offscreen normally.
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    const ts = Date.now();
    const recent = ts - 5_000;
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'lone-job': {
            jobId: 'lone-job', ruleIds: [10000], dnrSlot: 0,
            startedAt: ts, lastHeartbeat: recent,
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
      dnrFn,
    });
    chrome.offscreen.hasDocument = vi.fn().mockResolvedValue(true);

    const ctx = loadBackground({ chrome, fetchStub: makeFetchStub() });

    await ctx._wv2nasHandleDurableCompletion({
      target: 'service-worker',
      type: 'BROWSER_JOB_DONE',
      payload: { jobId: 'lone-job' },
    });

    expect(ctx.__eval("_wv2nasActiveBrowserJobs.size")).toBe(0);
    // Offscreen close IS called because the only job finished.
    expect(chrome.offscreen.closeDocument).toHaveBeenCalled();
  });

  it('continues recovery and keeps retry state when abort fails', async () => {
    const dnrFn = vi.fn(async (args) => { dnrCalls.push(args); });
    const fetchStub = vi.fn(async (url, _opts) => {
      if (String(url).endsWith('/abort')) throw new Error('network');
      return { ok: true, status: 200 };
    });
    const chrome = makeChromeStub({ dnrFn });
    const ctx = loadBackground({ chrome, fetchStub });
    await new Promise((r) => setTimeout(r, 0));

    const oldStart = Date.now() - 2 * 60 * 60 * 1000;
    chrome.storage.local._state.wv2nasBrowserJobs = {
      'a': { jobId: 'a', ruleIds: [10000], dnrSlot: 0, startedAt: oldStart,
             nasEndpoint: 'http://nas/', apiKey: 'k' },
      'b': { jobId: 'b', ruleIds: [10100], dnrSlot: 1, startedAt: oldStart,
             nasEndpoint: 'http://nas/', apiKey: 'k' },
    };
    dnrCalls.length = 0;

    await ctx._wv2nasRecoverStaleBrowserJobs();

    // Both DNR cleanups attempted (abort failure didn't short-circuit).
    expect(dnrCalls).toHaveLength(2);
    // Abort failures keep persisted retry entries so the next SW boot
    // can retry server-side staging cleanup.
    const jobs = chrome.storage.local._state.wv2nasBrowserJobs;
    expect(jobs.a).toMatchObject({
      abortPending: true,
      lastHeartbeat: 0,
      ruleIds: [],
      dnrSlot: null,
    });
    expect(jobs.b).toMatchObject({
      abortPending: true,
      lastHeartbeat: 0,
      ruleIds: [],
      dnrSlot: null,
    });
  });
});


// Codex adversarial-review: persistence is a R-M-W on a single
// chrome.storage.local key. Two concurrent jobs (or job + heartbeat)
// can each read the same snapshot, then each write back — losing
// the other's entry. The mutex chain serializes mutations so the
// second writer reads the first writer's commit before doing its
// own R-M-W. These tests reproduce the race against the mock storage
// (which behaves the same way as real chrome.storage with respect to
// promise interleaving — both await on get/set).

describe('persistence concurrency safety (Codex adversarial-review)', () => {
  it('concurrent persist of two jobs preserves BOTH entries', async () => {
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome });

    // Pre-fix: both calls await get() in parallel, both observe {}, both
    // write back their own single-entry object — last write wins, first
    // job is dropped. With the mutex, the second call waits for the
    // first to commit before reading.
    await Promise.all([
      ctx._wv2nasPersistBrowserJob('job-A', { ruleIds: [10000], dnrSlot: 0 }),
      ctx._wv2nasPersistBrowserJob('job-B', { ruleIds: [10100], dnrSlot: 1 }),
    ]);

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    expect(stored['job-A']).toBeDefined();
    expect(stored['job-B']).toBeDefined();
    expect(stored['job-A'].dnrSlot).toBe(0);
    expect(stored['job-B'].dnrSlot).toBe(1);
    expect(Object.keys(stored).sort()).toEqual(['job-A', 'job-B']);
  });

  it('many concurrent persists of distinct jobs all survive', async () => {
    const chrome = makeChromeStub();
    const ctx = loadBackground({ chrome });

    const ids = Array.from({ length: 12 }, (_, i) => `job-${i}`);
    await Promise.all(ids.map((jid, i) =>
      ctx._wv2nasPersistBrowserJob(jid, { dnrSlot: i, startedAt: 1000 + i })
    ));

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    for (const jid of ids) {
      expect(stored[jid]).toBeDefined();
    }
    expect(Object.keys(stored)).toHaveLength(12);
  });

  it('concurrent heartbeat updates to two jobs preserve both', async () => {
    // The exact scenario Codex flagged: each job's heartbeat is a
    // partial persist (only updating lastHeartbeat). Without
    // serialization, two simultaneous heartbeats race.
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'job-A': { jobId: 'job-A', startedAt: 1, lastHeartbeat: 100, dnrSlot: 0 },
          'job-B': { jobId: 'job-B', startedAt: 2, lastHeartbeat: 200, dnrSlot: 1 },
        },
      },
    });
    const ctx = loadBackground({ chrome });

    await Promise.all([
      ctx._wv2nasPersistBrowserJob('job-A', { lastHeartbeat: 5000 }),
      ctx._wv2nasPersistBrowserJob('job-B', { lastHeartbeat: 5000 }),
    ]);

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    // Both heartbeats applied; neither job's metadata lost.
    expect(stored['job-A'].lastHeartbeat).toBe(5000);
    expect(stored['job-B'].lastHeartbeat).toBe(5000);
    expect(stored['job-A'].dnrSlot).toBe(0);
    expect(stored['job-B'].dnrSlot).toBe(1);
  });

  it('concurrent persist + unpersist serializes correctly', async () => {
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'existing': { jobId: 'existing', dnrSlot: 0 },
        },
      },
    });
    const ctx = loadBackground({ chrome });

    await Promise.all([
      ctx._wv2nasPersistBrowserJob('new', { dnrSlot: 1 }),
      ctx._wv2nasUnpersistBrowserJob('existing'),
    ]);

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    expect(stored['new']).toBeDefined();
    expect(stored['existing']).toBeUndefined();
    expect(Object.keys(stored)).toEqual(['new']);
  });

  it('a failing persist does not poison the chain for subsequent calls', async () => {
    const chrome = makeChromeStub();
    // Make set() throw on the first call only.
    let setCalls = 0;
    chrome.storage.local.set = vi.fn(async (obj) => {
      setCalls += 1;
      if (setCalls === 1) {
        throw new Error('disk full');
      }
      Object.assign(chrome.storage.local._state, obj);
    });
    const ctx = loadBackground({ chrome });

    // First persist's underlying set throws. The helper swallows the
    // error (per _wv2nasWritePersistedBrowserJobs), so the await
    // resolves; downstream callers must still proceed.
    await ctx._wv2nasPersistBrowserJob('A', { dnrSlot: 0 });
    await ctx._wv2nasPersistBrowserJob('B', { dnrSlot: 1 });

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    // 'A' is lost (the throw blew away its write); 'B' must still be
    // present — that's the chain-not-poisoned property.
    expect(stored['B']).toBeDefined();
  });

  it('recovery sweep preserves a heartbeat written concurrently', async () => {
    // Setup: an old stale job that should be reaped, plus a brand-new
    // entry that lands DURING the recovery sweep (simulating a
    // BROWSER_JOB_DONE message handler firing alongside boot recovery).
    // Pre-fix code wrote `survivors` wholesale, dropping the new
    // entry. New code per-job-unpersists the stale one through the
    // mutex, leaving the concurrent entry intact.
    const oldStart = Date.now() - 90 * 60 * 1000;  // 90 min ago — stale
    const chrome = makeChromeStub({
      storageInitial: {
        wv2nasBrowserJobs: {
          'stale': {
            jobId: 'stale', dnrSlot: 0, startedAt: oldStart,
            lastHeartbeat: 0, ruleIds: [],
            nasEndpoint: 'http://nas/', apiKey: 'k',
          },
        },
      },
    });
    const ctx = loadBackground({ chrome, fetchStub: vi.fn().mockResolvedValue({ ok: true }) });

    // Fire recovery and a concurrent persist at the same time. Both
    // must win — recovery removes 'stale', concurrent persist adds
    // 'fresh'.
    await Promise.all([
      ctx._wv2nasRecoverStaleBrowserJobs(),
      ctx._wv2nasPersistBrowserJob('fresh', { dnrSlot: 2, startedAt: Date.now() }),
    ]);

    const stored = chrome.storage.local._state.wv2nasBrowserJobs;
    expect(stored['stale']).toBeUndefined();
    expect(stored['fresh']).toBeDefined();
    expect(stored['fresh'].dnrSlot).toBe(2);
  });
});
