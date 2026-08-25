import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The jobs poll invalidated its own answers. loadRecentJobs() opens
// a new generation on the 'jobs' stream and the response checks it to discard an older
// overlapping poll — sound on its own, but it was driven by an unconditional
// 2s setInterval. A NAS answering slower than 2s therefore had every in-flight
// request superseded by the next tick, so each response arrived to find the
// sequence moved on and was dropped as stale. The list stopped updating
// entirely, and the 10s bound let roughly five requests overlap.
//
// Deterministically broken, not a rare race: any steady response time above
// the poll interval reproduces it.

function makeCtx() {
  const fetches = [];
  // One resolver per request, so a test can answer the request that was in
  // flight rather than whichever happened to start last. Sharing a single
  // resolver would let the stale-response bug pass unnoticed: the last
  // request always carries the current sequence number.
  const pendings = [];
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
        sendMessage: async () => {},
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
      createElement: () => ({ classList: { add() {} } }),
      documentElement: { setAttribute() {} },
      body: { appendChild() {} },
    },
    window: {},
    navigator: { clipboard: {} },
    fetch: (url) => {
      fetches.push(String(url));
      return new Promise((resolve) => { pendings.push(resolve); });
    },
  });
  ctx.__eval("settings = { nasEndpoint: 'http://nas.example:52052', apiKey: 'k' };");
  ctx.renderJobs = () => {};
  ctx.refreshOnboardingCoach = () => {};
  return {
    ctx,
    fetches,
    // Answer request #i, standing in for a NAS that took longer than the
    // poll interval to reply. Defaults to the first one — the request that was
    // already in flight when the extra ticks arrived.
    respond: (body, i = 0) => pendings[i]({ ok: true, status: 200, json: async () => body }),
  };
}

const settle = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };

describe('jobs poll is single-flight', () => {
  it('skips ticks that land while a request is still out', async () => {
    const h = makeCtx();

    h.ctx.pollRecentJobs();
    await settle();
    expect(h.fetches.length).toBe(1);

    // Four more interval ticks while the NAS is still thinking.
    h.ctx.pollRecentJobs();
    h.ctx.pollRecentJobs();
    h.ctx.pollRecentJobs();
    h.ctx.pollRecentJobs();
    await settle();

    // Previously each of these started a request and invalidated the one
    // before it.
    expect(h.fetches.length).toBe(1);
  });

  it('renders a response that took longer than the poll interval', async () => {
    const h = makeCtx();

    h.ctx.pollRecentJobs();
    await settle();
    // Ticks keep arriving while the request is out.
    h.ctx.pollRecentJobs();
    h.ctx.pollRecentJobs();
    await settle();

    h.respond([{ id: 'job-1', status: 'completed', title: 'a', progress: 100 }]);
    await settle();

    // The whole point: the slow answer is kept, not discarded as stale.
    const rendered = h.ctx.__eval('jobs');
    expect(rendered.length).toBe(1);
    expect(String(rendered[0].id)).toBe('job-1');
  });

  it('starts a fresh request once the previous one finishes', async () => {
    const h = makeCtx();

    h.ctx.pollRecentJobs();
    await settle();
    h.respond([]);
    await settle();

    h.ctx.pollRecentJobs();
    await settle();
    expect(h.fetches.length).toBe(2);
  });

  it('lets an explicit caller supersede an in-flight poll', async () => {
    // The refresh button, a profile change and post-cancel all call
    // loadRecentJobs directly. Those genuinely mean "replace what is in
    // flight", which is what the sequence check exists for.
    const h = makeCtx();

    h.ctx.pollRecentJobs();
    await settle();
    expect(h.fetches.length).toBe(1);

    h.ctx.loadRecentJobs();
    await settle();
    expect(h.fetches.length).toBe(2);
  });
});
