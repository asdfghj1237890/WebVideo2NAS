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
