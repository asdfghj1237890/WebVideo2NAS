import { afterEach, describe, expect, it, vi } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// offscreen.html loads nasIdentity.js as a classic script before the module,
// so globalThis carries it by the time offscreen.js runs. Mirror that here.
function installNasIdentity() {
  const ctx = loadScriptIntoContext('nasIdentity.js', { window: {} });
  globalThis.WV2NNasIdentity = ctx.WV2NNasIdentity;
}

// Job ids are namespaced by NAS — see nasIdentity.js.
const NAS = 'http://nas.example:52052';


async function loadOffscreen({ runJobImpl, sendMessageImpl }) {
  vi.resetModules();
  const listeners = [];
  globalThis.chrome = {
    runtime: {
      sendMessage: vi.fn(sendMessageImpl || (async () => undefined)),
      onMessage: {
        addListener: (fn) => listeners.push(fn),
      },
    },
  };
  vi.doMock('../segmentDownloader.js', () => ({
    runJob: vi.fn(runJobImpl || (async () => ({ totalSegments: 1 }))),
  }));
  installNasIdentity();
  await import('../offscreen.js');
  return { chrome: globalThis.chrome, listeners };
}


function sendStart(listeners, payload) {
  let response;
  for (const listener of listeners) {
    listener(
      { type: 'START_BROWSER_JOB', target: 'offscreen', payload },
      {},
      (value) => { response = value; },
    );
  }
  return response;
}


function sendCancel(listeners, jobId, payload = {}) {
  let response;
  for (const listener of listeners) {
    listener(
      // Cancel matches on NAS + id, so the scope has to travel with it.
      { type: 'CANCEL_BROWSER_JOB', target: 'offscreen', payload: { jobId, nasScope: NAS, ...payload } },
      {},
      (value) => { response = value; },
    );
  }
  return response;
}


describe('offscreen completion delivery', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.resetModules();
    vi.clearAllMocks();
    delete globalThis.chrome;
  });

  it('retries DONE delivery before clearing the job pipeline', async () => {
    vi.useFakeTimers();
    let doneAttempts = 0;
    const { chrome, listeners } = await loadOffscreen({
      runJobImpl: async () => ({ totalSegments: 3 }),
      sendMessageImpl: async (msg) => {
        if (msg.type === 'BROWSER_JOB_DONE') {
          doneAttempts += 1;
          if (doneAttempts === 1) {
            throw new Error('service worker waking');
          }
        }
        return undefined;
      },
    });

    const ack = sendStart(listeners, {
      jobId: 'job-retry-done', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    });
    expect(ack).toEqual({ ok: true });

    await vi.runOnlyPendingTimersAsync();
    const doneMessages = () => chrome.runtime.sendMessage.mock.calls
      .map(([msg]) => msg)
      .filter((msg) => msg && msg.type === 'BROWSER_JOB_DONE');

    expect(doneMessages()).toHaveLength(2);
    expect(doneMessages()[1].payload).toEqual({
      jobId: 'job-retry-done', nasScope: NAS,
      summary: { totalSegments: 3 },
    });
  });

  it('retries completion when the service worker returns a negative ack', async () => {
    vi.useFakeTimers();
    let doneAttempts = 0;
    const { chrome, listeners } = await loadOffscreen({
      runJobImpl: async () => ({ totalSegments: 2 }),
      sendMessageImpl: async (msg) => {
        if (msg.type !== 'BROWSER_JOB_DONE') return undefined;
        doneAttempts += 1;
        return doneAttempts === 1
          ? { ok: false, error: 'snapshot write failed' }
          : { ok: true };
      },
    });

    expect(sendStart(listeners, {
      jobId: 'job-negative-ack', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    })).toEqual({ ok: true });

    await vi.runOnlyPendingTimersAsync();
    const doneMessages = chrome.runtime.sendMessage.mock.calls
      .map(([msg]) => msg)
      .filter((msg) => msg && msg.type === 'BROWSER_JOB_DONE');
    expect(doneMessages).toHaveLength(2);
  });

  it('forwards separate CDN/NAS timings and selected concurrency in progress', async () => {
    let resolveProgress;
    const progressMessage = new Promise((resolve) => { resolveProgress = resolve; });
    const transferTimings = {
      cdn: { bytes: 1024, requestMs: 90, activeMs: 50, mbPerSecond: 0.02 },
      nas: { bytes: 1024, requestMs: 10, activeMs: 5, mbPerSecond: 0.2 },
    };
    const { listeners } = await loadOffscreen({
      runJobImpl: async ({ onProgress }) => {
        onProgress({ done: 1, total: 2, concurrency: 12, transferTimings });
        return { totalSegments: 2, concurrency: 12, transferTimings };
      },
      sendMessageImpl: async (msg) => {
        if (msg.type === 'BROWSER_JOB_PROGRESS') resolveProgress(msg);
        return undefined;
      },
    });

    expect(sendStart(listeners, {
      jobId: 'job-progress-timing', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    })).toEqual({ ok: true });

    await expect(progressMessage).resolves.toMatchObject({
      type: 'BROWSER_JOB_PROGRESS',
      payload: {
        jobId: 'job-progress-timing', nasScope: NAS,
        done: 1,
        total: 2,
        concurrency: 12,
        transferTimings,
      },
    });
  });

  it('keeps liveness and retries DONE delivery after the first retry burst fails', async () => {
    vi.useFakeTimers();
    let doneAttempts = 0;
    const { chrome, listeners } = await loadOffscreen({
      runJobImpl: async () => ({ totalSegments: 5 }),
      sendMessageImpl: async (msg) => {
        if (msg.type === 'BROWSER_JOB_DONE') {
          doneAttempts += 1;
          if (doneAttempts <= 3) {
            throw new Error('service worker unavailable');
          }
        }
        return undefined;
      },
    });

    const ack = sendStart(listeners, {
      jobId: 'job-retry-done-long', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    });
    expect(ack).toEqual({ ok: true });

    await vi.advanceTimersByTimeAsync(500);

    const messages = () => chrome.runtime.sendMessage.mock.calls.map(([msg]) => msg);
    const doneMessages = () => messages().filter((msg) => msg && msg.type === 'BROWSER_JOB_DONE');
    const heartbeatMessages = () => messages().filter((msg) => msg && msg.type === 'BROWSER_JOB_HEARTBEAT');

    expect(doneMessages()).toHaveLength(3);
    const heartbeatCountAfterFailedBurst = heartbeatMessages().length;

    await vi.advanceTimersByTimeAsync(10_000);

    expect(doneMessages()).toHaveLength(4);
    expect(doneMessages()[3].payload).toEqual({
      jobId: 'job-retry-done-long', nasScope: NAS,
      summary: { totalSegments: 5 },
    });
    expect(heartbeatMessages().length).toBeGreaterThan(heartbeatCountAfterFailedBurst);
  });

  it('still delivers FAILED after the active job is cancelled', async () => {
    vi.useFakeTimers();
    const { chrome, listeners } = await loadOffscreen({
      runJobImpl: ({ signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('cancelled')), { once: true });
      }),
    });

    const ack = sendStart(listeners, {
      jobId: 'job-cancel-failed', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    });
    expect(ack).toEqual({ ok: true });

    expect(sendCancel(listeners, 'job-cancel-failed', { userCancelled: true })).toEqual({ ok: true });
    await vi.runOnlyPendingTimersAsync();

    const failedMessages = chrome.runtime.sendMessage.mock.calls
      .map(([msg]) => msg)
      .filter((msg) => msg && msg.type === 'BROWSER_JOB_FAILED');

    expect(failedMessages).toHaveLength(1);
    expect(failedMessages[0].payload).toMatchObject({
      jobId: 'job-cancel-failed', nasScope: NAS,
      error: 'cancelled',
      finalizeAttempted: false,
      userCancelled: true,
    });
  });

  it('retries FAILED delivery with finalizeAttempted preserved', async () => {
    vi.useFakeTimers();
    const err = new Error('finalize ambiguous');
    err.finalizeAttempted = true;
    err.done = 7;
    err.total = 8;
    err.concurrency = 4;
    err.transferTimings = { cdn: { failures: 1 }, nas: { failures: 0 } };
    let failedAttempts = 0;
    const { chrome, listeners } = await loadOffscreen({
      runJobImpl: async () => { throw err; },
      sendMessageImpl: async (msg) => {
        if (msg.type === 'BROWSER_JOB_FAILED') {
          failedAttempts += 1;
          if (failedAttempts === 1) {
            throw new Error('service worker waking');
          }
        }
        return undefined;
      },
    });

    const ack = sendStart(listeners, {
      jobId: 'job-retry-failed', nasScope: NAS,
      nasEndpoint: 'http://nas.local',
      apiKey: 'k',
      plan: { tracks: { video: { segments: [] } } },
    });
    expect(ack).toEqual({ ok: true });

    await vi.runOnlyPendingTimersAsync();
    const failedMessages = chrome.runtime.sendMessage.mock.calls
      .map(([msg]) => msg)
      .filter((msg) => msg && msg.type === 'BROWSER_JOB_FAILED');

    expect(failedMessages).toHaveLength(2);
    expect(failedMessages[1].payload).toMatchObject({
      jobId: 'job-retry-failed', nasScope: NAS,
      error: 'finalize ambiguous',
      finalizeAttempted: true,
      done: 7,
      total: 8,
      concurrency: 4,
      transferTimings: err.transferTimings,
    });
  });
});
