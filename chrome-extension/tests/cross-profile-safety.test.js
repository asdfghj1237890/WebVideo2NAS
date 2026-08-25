import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// Two failures that share one cause: mutable global settings read at response
// time, while the UI still holds state belonging to a different NAS.
//
// 1. The merge deliberately retains a previous snapshot's active browser rows
//    for up to the live-progress TTL. After switching A to B, the list holds
//    B's rows and retained A rows at once. A snapshot-wide source silently
//    re-attributed the retained ones to B, so cancelling an A row sent its
//    job id to B — nothing cancelled, or the wrong job cancelled outright if
//    both NAS were restored from the same database backup.
//
// 2. checkConnection had no request generation. An A check finishing after a
//    switch to B painted A's verdict onto B, and the reverse order marked a
//    verified B unreachable. The onboarding coach reads that chip to pick its
//    step, so a stale verdict advanced onboarding for a NAS never reached.

function makeCtx({ fetchImpl } = {}) {
  const calls = [];
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
    fetch: fetchImpl || (async (url, options) => {
      calls.push({ url: String(url), method: (options && options.method) || 'GET' });
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  });
  ctx.renderJobs = () => {};
  ctx.refreshOnboardingCoach = () => {};
  ctx.showToast = () => {};
  ctx.loadRecentJobs = async () => {};
  return { ctx, calls };
}

const NAS_A = 'http://nas-a.example:52052';
const NAS_B = 'http://nas-b.example:52052';

describe('a retained row cancels against its own NAS', () => {
  it('does not follow the profile switch that happened after it was fetched', async () => {
    const { ctx, calls } = makeCtx();

    // The list holds a fresh B row and a retained A row — the state the merge
    // produces after switching profile while a browser job is still active.
    ctx.__eval(`jobs = [
      { id: 'b-job', status: 'downloading', __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb') },
      { id: 'a-job', status: 'browser_uploading', __nasTarget: sidepanelCore.nasTarget('${NAS_A}', 'ka') }
    ];`);
    // Snapshot-wide source and live settings both point at B.
    ctx.__eval(`jobsTarget = sidepanelCore.nasTarget('${NAS_B}', 'kb');`);
    ctx.__eval(`settings = { nasEndpoint: '${NAS_B}', apiKey: 'kb' };`);

    await ctx.cancelJob('a-job');

    const deletes = calls.filter((c) => c.method === 'DELETE');
    expect(deletes.length).toBe(1);
    expect(deletes[0].url).toContain('nas-a.example');
    expect(deletes[0].url).not.toContain('nas-b.example');
  });

  it('sends a fresh row to the NAS that served it', async () => {
    const { ctx, calls } = makeCtx();
    ctx.__eval(`jobs = [
      { id: 'b-job', status: 'downloading', __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb') }
    ];`);
    ctx.__eval(`jobsTarget = sidepanelCore.nasTarget('${NAS_B}', 'kb');`);
    ctx.__eval(`settings = { nasEndpoint: '${NAS_B}', apiKey: 'kb' };`);

    await ctx.cancelJob('b-job');

    const deletes = calls.filter((c) => c.method === 'DELETE');
    expect(deletes[0].url).toContain('nas-b.example');
  });
});

describe('a stale connection check cannot paint the wrong profile', () => {
  it('discards an A result that lands after the switch to B', async () => {
    let releaseA = null;
    const { ctx } = makeCtx({
      fetchImpl: (url) => {
        if (String(url).includes('nas-a')) {
          return new Promise((resolve) => { releaseA = () => resolve({ ok: true, status: 200 }); });
        }
        return Promise.resolve({ ok: true, status: 200 });
      },
    });

    const painted = [];
    ctx.setConnectionState = (state, label) => { painted.push({ state, label }); };
    ctx.loadSettingsFromStorage = async () => {};

    ctx.__eval(`settings = { nasEndpoint: '${NAS_A}', apiKey: 'ka' };`);
    const aCheck = ctx.checkConnection();
    await new Promise((r) => setTimeout(r, 0));

    // User switches profile while A is still in flight.
    ctx.__eval(`settings = { nasEndpoint: '${NAS_B}', apiKey: 'kb' };`);
    releaseA();
    await aCheck;

    // A's verdict must never be painted as B's.
    const connected = painted.filter((p) => p.state === 'connected');
    expect(connected).toEqual([]);
  });

  it('still paints when nothing changed underneath it', async () => {
    const { ctx } = makeCtx();
    const painted = [];
    ctx.setConnectionState = (state) => { painted.push(state); };
    ctx.loadSettingsFromStorage = async () => {};
    ctx.shortHost = (h) => h;

    ctx.__eval(`settings = { nasEndpoint: '${NAS_A}', apiKey: 'ka' };`);
    await ctx.checkConnection();

    expect(painted).toContain('connected');
  });
});
