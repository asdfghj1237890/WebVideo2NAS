import { afterEach, describe, expect, it, vi } from 'vitest';


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
      { type: 'CANCEL_BROWSER_JOB', target: 'offscreen', payload: { jobId, ...payload } },
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
      jobId: 'job-retry-done',
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
      jobId: 'job-retry-done',
      summary: { totalSegments: 3 },
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
      jobId: 'job-retry-done-long',
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
      jobId: 'job-retry-done-long',
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
      jobId: 'job-cancel-failed',
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
      jobId: 'job-cancel-failed',
      error: 'cancelled',
      finalizeAttempted: false,
      userCancelled: true,
    });
  });

  it('retries FAILED delivery with finalizeAttempted preserved', async () => {
    vi.useFakeTimers();
    const err = new Error('finalize ambiguous');
    err.finalizeAttempted = true;
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
      jobId: 'job-retry-failed',
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
      jobId: 'job-retry-failed',
      error: 'finalize ambiguous',
      finalizeAttempted: true,
    });
  });
});
