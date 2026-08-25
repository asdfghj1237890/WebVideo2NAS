import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The NAS commits a cancel before it answers, so a DELETE whose response is
// lost — request timeout, dropped connection — tells the panel nothing about
// whether it took effect. Reporting failure there left browser-mode uploads
// running against a job the NAS had already cancelled, because
// CANCEL_BROWSER_JOB is only sent on the success path. Retrying used to make
// it worse: DELETE answered 404 for an already-cancelled row, so the retry
// looked like a failure too.
//
// The panel now asks what actually happened before reporting failure, and the
// API answers a repeat DELETE idempotently (see the API test suite).

function makeCtx({ deleteImpl, statusImpl }) {
  const toasts = [];
  const offscreenMessages = [];
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener() {} },
      },
      runtime: {
        onMessage: { addListener() {} },
        openOptionsPage: () => {},
        sendMessage: async (msg) => { offscreenMessages.push(msg); },
        lastError: null,
      },
      tabs: {
        query: (_q, cb) => cb([]),
        onUpdated: { addListener() {} },
        onActivated: { addListener() {} },
      },
    },
    document: {
      addEventListener() {},
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => ({ classList: { add() {} }, remove() {} }),
      documentElement: { setAttribute() {} },
      body: { appendChild() {} },
    },
    window: {},
    navigator: { clipboard: {} },
    fetch: async (url, options) => {
      const method = (options && options.method) || 'GET';
      if (method === 'DELETE') return deleteImpl(url, options);
      return statusImpl(url, options);
    },
  });
  ctx.__eval("settings = { nasEndpoint: 'http://nas.example:52052', apiKey: 'k' };");
  ctx.showToast = (msg) => { toasts.push(msg); };
  ctx.loadRecentJobs = async () => {};
  return { ctx, toasts, offscreenMessages };
}

const rejects = () => Promise.reject(Object.assign(new Error('The operation was aborted'), { name: 'AbortError' }));
const jsonResponse = (status, body) => async () => ({ ok: status >= 200 && status < 300, status, json: async () => body });

describe('cancelJob when the DELETE response never arrives', () => {
  it('stops the browser-side upload if the NAS did cancel the job', async () => {
    const { ctx, offscreenMessages, toasts } = makeCtx({
      deleteImpl: rejects,
      statusImpl: jsonResponse(200, { id: 'job-1', status: 'cancelled' }),
    });

    await ctx.cancelJob('job-1');

    // This is the whole point: without reconciliation the offscreen upload
    // kept running against a job the NAS had already cancelled.
    expect(offscreenMessages).toContainEqual({
      target: 'offscreen',
      type: 'CANCEL_BROWSER_JOB',
      payload: { jobId: 'job-1', userCancelled: true },
    });
    expect(toasts.length).toBe(1);
  });

  it('reports failure when the job is genuinely still running', async () => {
    const { ctx, offscreenMessages, toasts } = makeCtx({
      deleteImpl: rejects,
      statusImpl: jsonResponse(200, { id: 'job-1', status: 'downloading' }),
    });

    await ctx.cancelJob('job-1');

    expect(offscreenMessages).toEqual([]);
    expect(toasts.length).toBe(1);
  });

  it('reports failure when the status check cannot be reached either', async () => {
    // Any doubt keeps the pre-existing behaviour rather than claiming a
    // cancel that may not have happened.
    const { ctx, offscreenMessages } = makeCtx({
      deleteImpl: rejects,
      statusImpl: rejects,
    });

    await ctx.cancelJob('job-1');

    expect(offscreenMessages).toEqual([]);
  });
});

describe('cancelJob reconciles ambiguous HTTP failures too', () => {
  // A response is not proof. A proxy can answer 504 after the backend
  // committed, and delete_job commits the status flip before it reads job
  // metadata, so a post-commit exception surfaces as 500 on a cancel that did
  // take effect. Only reconciling on a thrown request left exactly the split
  // state — NAS cancelled, browser still uploading — that the reconcile was
  // added to remove.
  for (const status of [408, 429, 500, 502, 503, 504]) {
    it(`treats ${status} as unknown and checks the real state`, async () => {
      const { ctx, offscreenMessages } = makeCtx({
        deleteImpl: jsonResponse(status, { detail: 'gateway timeout' }),
        statusImpl: jsonResponse(200, { status: 'cancelled' }),
      });

      await ctx.cancelJob('job-1');

      expect(offscreenMessages.length).toBe(1);
    });
  }

  for (const status of [400, 401, 403, 404]) {
    it(`takes ${status} at face value without reconciling`, async () => {
      const { ctx, offscreenMessages } = makeCtx({
        deleteImpl: jsonResponse(status, { detail: 'nope' }),
        // Would say cancelled if asked — the point is that it is not asked.
        statusImpl: jsonResponse(200, { status: 'cancelled' }),
      });

      await ctx.cancelJob('job-1');

      expect(offscreenMessages).toEqual([]);
    });
  }

  it('still reports failure when an ambiguous status did not cancel anything', async () => {
    const { ctx, offscreenMessages, toasts } = makeCtx({
      deleteImpl: jsonResponse(503, { detail: 'unavailable' }),
      statusImpl: jsonResponse(200, { status: 'downloading' }),
    });

    await ctx.cancelJob('job-1');

    expect(offscreenMessages).toEqual([]);
    expect(toasts.length).toBe(1);
  });
});

describe('cancelJob targets the NAS that served the row', () => {
  it('uses the snapshot source, not the profile switched to afterwards', async () => {
    // The rows stay on screen and clickable after a profile switch, until a
    // new list loads — indefinitely if the new NAS is unreachable. Sending
    // NAS A's job id to NAS B either cancels nothing or, when both were
    // restored from the same backup, cancels the wrong job irreversibly.
    const urls = [];
    const { ctx, offscreenMessages } = makeCtx({
      deleteImpl: async (url) => {
        urls.push(url);
        return { ok: true, status: 200, json: async () => ({}) };
      },
      statusImpl: async (url) => { urls.push(url); return { ok: true, status: 200, json: async () => ({}) }; },
    });

    // A list was served by NAS A...
    ctx.__eval("jobsTarget = sidepanelCore.nasTarget('http://nas-a.example:52052', 'ka');");
    // ...and then the user switched profile to NAS B.
    ctx.__eval("settings = { nasEndpoint: 'http://nas-b.example:52052', apiKey: 'kb' };");

    await ctx.cancelJob('job-1');

    expect(urls.length).toBe(1);
    expect(urls[0]).toContain('nas-a.example');
    expect(urls[0]).not.toContain('nas-b.example');
    expect(offscreenMessages.length).toBe(1);
  });
});

describe('cancelJob pins the NAS it is talking to', () => {
  it('reconciles against the original NAS after a profile switch mid-cancel', async () => {
    // settings is rewritten in place by storage.onChanged and by switching
    // profile. Reading it again for the reconcile would ask NAS B whether a
    // cancel sent to NAS A took effect — and B answers confidently about a job
    // id it knows nothing about.
    const urls = [];
    const { ctx, offscreenMessages } = makeCtx({
      deleteImpl: (url) => {
        urls.push(url);
        // The user switches profile while the DELETE is in flight.
        ctx.__eval("settings = { nasEndpoint: 'http://other-nas.example:52052', apiKey: 'k2' };");
        return rejects();
      },
      statusImpl: async (url) => {
        urls.push(url);
        return { ok: true, status: 200, json: async () => ({ status: 'cancelled' }) };
      },
    });

    await ctx.cancelJob('job-1');

    expect(urls.length).toBe(2);
    for (const url of urls) expect(url).toContain('nas.example:52052');
    for (const url of urls) expect(url).not.toContain('other-nas');
    expect(offscreenMessages.length).toBe(1);
  });
});

describe('cancelJob on the ordinary paths', () => {
  it('stops the upload when the DELETE is accepted', async () => {
    const { ctx, offscreenMessages } = makeCtx({
      deleteImpl: jsonResponse(200, { message: 'Job cancelled successfully' }),
      statusImpl: jsonResponse(200, {}),
    });

    await ctx.cancelJob('job-1');

    expect(offscreenMessages.length).toBe(1);
  });

  it('surfaces the API detail on a rejected cancel without reconciling', async () => {
    const { ctx, offscreenMessages, toasts } = makeCtx({
      deleteImpl: jsonResponse(404, { detail: 'Job not found or cannot be cancelled' }),
      statusImpl: jsonResponse(200, { status: 'cancelled' }),
    });

    await ctx.cancelJob('job-1');

    // A definitive answer is not a lost response — take it at face value.
    expect(offscreenMessages).toEqual([]);
    expect(toasts).toContain('Job not found or cannot be cancelled');
  });
});
