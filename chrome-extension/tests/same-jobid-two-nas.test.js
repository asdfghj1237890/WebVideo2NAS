import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The scenario the composite identity exists for.
//
// A NAS job id is unique only within that NAS. Two NAS restored from the same
// database backup hand out the same ids, and a profile switch can leave both
// in play at once — the merge deliberately retains the previous snapshot's
// active browser rows. With identity keyed on a bare id, one NAS's live
// progress lands on the other's row, and a cancel aimed at one aborts the
// other's upload.

const NAS_A = 'http://nas-a.example:52052';
const NAS_B = 'http://nas-b.example:52052';
const SHARED_ID = 'job-42';   // the same id on both, as a restored backup gives

function identity() {
  const ctx = loadScriptIntoContext('nasIdentity.js', { window: {} });
  return ctx.WV2NNasIdentity;
}

function makeSidePanel() {
  const sent = [];
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async () => {} },
        session: { get: async () => ({}) },
        onChanged: { addListener() {} },
      },
      runtime: {
        onMessage: { addListener() {} },
        openOptionsPage: () => {},
        sendMessage: async (msg) => { sent.push(msg); },
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
    fetch: async (url, options) => {
      sent.push({ http: (options && options.method) || 'GET', url: String(url) });
      return { ok: true, status: 200, json: async () => ({}) };
    },
  });
  ctx.renderJobs = () => {};
  ctx.refreshOnboardingCoach = () => {};
  ctx.showToast = () => {};
  ctx.loadRecentJobs = async () => {};
  return { ctx, sent };
}

describe('identity keys', () => {
  const id = identity();

  it('separates the same job id on two NAS', () => {
    expect(id.browserJobKey(NAS_A, SHARED_ID)).not.toBe(id.browserJobKey(NAS_B, SHARED_ID));
  });

  it('is stable for the same NAS and id', () => {
    expect(id.browserJobKey(NAS_A, SHARED_ID)).toBe(id.browserJobKey(NAS_A, SHARED_ID));
  });

  it('refuses to make a key that names no NAS', () => {
    expect(id.browserJobKey('', SHARED_ID)).toBeNull();
    expect(id.browserJobKey(NAS_A, '')).toBeNull();
    expect(id.browserJobKey(undefined, SHARED_ID)).toBeNull();
  });

  it('recognises pre-identity keys so they can be dropped rather than guessed', () => {
    expect(id.isLegacyBrowserJobKey(SHARED_ID)).toBe(true);
    expect(id.isLegacyBrowserJobKey(id.browserJobKey(NAS_A, SHARED_ID))).toBe(false);
  });
});

describe('live progress does not cross between NAS', () => {
  it('keeps A progress off a B row carrying the same job id', () => {
    const { ctx } = makeSidePanel();
    const id = identity();

    // One row, owned by B.
    ctx.__eval(`jobs = [{
      id: '${SHARED_ID}', status: 'browser_uploading', progress: 0,
      __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb')
    }];`);

    // Progress arrives from A for the same id.
    ctx.handleBrowserJobProgress({
      jobId: SHARED_ID, nasScope: NAS_A, done: 9, total: 10,
    });

    // It is recorded under A, not merged onto B's row.
    const liveA = ctx.__eval(`liveBrowserProgress.get(${JSON.stringify(id.browserJobKey(NAS_A, SHARED_ID))})`);
    expect(liveA).toBeTruthy();
    expect(ctx.__eval('jobs[0].progress')).toBe(0);
  });

  it('applies progress that does belong to the row', () => {
    const { ctx } = makeSidePanel();

    ctx.__eval(`jobs = [{
      id: '${SHARED_ID}', status: 'browser_uploading', progress: 0,
      __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb')
    }];`);

    ctx.handleBrowserJobProgress({
      jobId: SHARED_ID, nasScope: NAS_B, done: 9, total: 10,
    });

    expect(ctx.__eval('jobs[0].progress')).toBeGreaterThan(0);
  });

  it('drops a progress event that names no NAS rather than guessing one', () => {
    const { ctx } = makeSidePanel();
    ctx.__eval(`jobs = [{
      id: '${SHARED_ID}', status: 'browser_uploading', progress: 0,
      __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb')
    }];`);

    ctx.handleBrowserJobProgress({ jobId: SHARED_ID, done: 9, total: 10 });

    expect(ctx.__eval('jobs[0].progress')).toBe(0);
    expect(ctx.__eval('liveBrowserProgress.size')).toBe(0);
  });
});

describe('cancel names the NAS it is aborting on', () => {
  it('sends B scope when cancelling a B row that shares an id with A', async () => {
    const { ctx, sent } = makeSidePanel();

    ctx.__eval(`jobs = [{
      id: '${SHARED_ID}', status: 'browser_uploading',
      __nasTarget: sidepanelCore.nasTarget('${NAS_B}', 'kb')
    }];`);
    // settings still point at A, as they would right after a switch back.
    ctx.__eval(`settings = { nasEndpoint: '${NAS_A}', apiKey: 'ka' };`);

    await ctx.cancelJob(SHARED_ID);

    const cancel = sent.find((m) => m && m.type === 'CANCEL_BROWSER_JOB');
    expect(cancel).toBeTruthy();
    // The uploader matches on NAS + id, so an unqualified payload could abort
    // A's transfer of the identically numbered job.
    expect(cancel.payload.nasScope).toBe(NAS_B);
    expect(cancel.payload.jobId).toBe(SHARED_ID);

    // And the DELETE went to B, the NAS that owns the row.
    const del = sent.find((m) => m && m.http === 'DELETE');
    expect(del.url).toContain('nas-b.example');
  });
});
