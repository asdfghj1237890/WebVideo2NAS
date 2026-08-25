import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// fetch() resolves as soon as the response *headers* arrive. fetchWithTimeout
// clears its timer at that point, so anything read afterwards — .json() on a
// body the server never finishes sending — is unbounded. The jobs poll runs
// every 2s, so one stalled body would sit forever while new requests stacked
// up behind it and the list stopped updating.
//
// An earlier test only asserted that requests carried an abort signal, which
// this failure mode sails straight past: the signal is present, it just is not
// armed any more by the time the body is read. These tests exercise the body.

function loadSidePanel(fetchImpl) {
  return loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener() {} },
      },
      runtime: {
        onMessage: { addListener() {} },
        openOptionsPage: () => {},
        sendMessage: (_m, cb) => cb && cb({}),
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
      createElement: () => ({}),
      documentElement: { setAttribute() {} },
      body: { appendChild() {} },
    },
    window: {},
    navigator: { clipboard: {} },
    fetch: fetchImpl,
  });
}

// A response whose headers arrive immediately and whose body never completes,
// modelling a real one by rejecting the read when the signal aborts.
const stalledBody = (url, options) => Promise.resolve({
  ok: true,
  status: 200,
  json: () => new Promise((_resolve, reject) => {
    if (options && options.signal) {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }
  }),
});

describe('fetchJsonWithTimeout', () => {
  it('aborts a body that never finishes arriving', async () => {
    const ctx = loadSidePanel(stalledBody);
    // Short bound so the test does not sit on the production 10s value.
    await expect(ctx.fetchJsonWithTimeout('http://nas.example/api/jobs', {}, 30))
      .rejects.toThrow(/abort/i);
  });

  it('returns the parsed body when it arrives in time', async () => {
    const ctx = loadSidePanel(async () => ({
      ok: true,
      status: 200,
      json: async () => [{ id: 1 }],
    }));
    const { response, data } = await ctx.fetchJsonWithTimeout('http://nas.example/api/jobs', {}, 1000);
    expect(response.ok).toBe(true);
    expect(data).toEqual([{ id: 1 }]);
  });

  it('parses an error body too, since its detail is what gets shown', async () => {
    const ctx = loadSidePanel(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'Job not found or cannot be cancelled' }),
    }));
    const { response, data } = await ctx.fetchJsonWithTimeout('http://nas.example/api/jobs/1', {}, 1000);
    expect(response.status).toBe(404);
    expect(data.detail).toBe('Job not found or cannot be cancelled');
  });

  it('yields null rather than throwing when a body is not JSON', async () => {
    const ctx = loadSidePanel(async () => ({
      ok: false,
      status: 502,
      json: async () => { throw new Error('Unexpected token < in JSON'); },
    }));
    const { response, data } = await ctx.fetchJsonWithTimeout('http://nas.example/api/jobs', {}, 1000);
    // A proxy's HTML error page must not mask the status it arrived with.
    expect(response.status).toBe(502);
    expect(data).toBeNull();
  });

  it('still propagates an abort even though parse errors are swallowed', async () => {
    // The swallow above must not turn a timed-out request into a quiet
    // success with an empty body.
    const ctx = loadSidePanel(stalledBody);
    await expect(ctx.fetchJsonWithTimeout('http://nas.example/api/jobs', {}, 30))
      .rejects.toThrow(/abort/i);
  });
});

describe('loadRecentJobs uses the body-bounded helper', () => {
  // Waiting out the real 10s bound would put 10s on every suite run, so this
  // asserts the wiring instead: the poll must go through the helper that keeps
  // the timer armed across the parse, not fetch-then-json by hand.
  it('routes the jobs request through fetchJsonWithTimeout', async () => {
    const bareFetches = [];
    const ctx = loadSidePanel((url, options) => {
      bareFetches.push(url);
      return stalledBody(url, options);
    });
    ctx.__eval("settings = { nasEndpoint: 'http://nas.example:52052', apiKey: 'k' };");

    const calls = [];
    ctx.fetchJsonWithTimeout = async (url, options, timeoutMs) => {
      calls.push({ url, timeoutMs });
      return { response: { ok: true, status: 200 }, data: [] };
    };

    await ctx.loadRecentJobs();

    expect(calls.length).toBe(1);
    expect(calls[0].url).toContain('/api/jobs');
    // A real bound, not undefined — passing undefined would make setTimeout
    // fire immediately and abort every poll.
    expect(typeof calls[0].timeoutMs).toBe('number');
    expect(calls[0].timeoutMs).toBeGreaterThan(0);
    // And it did not reach for fetch directly.
    expect(bareFetches).toEqual([]);
  });
});
