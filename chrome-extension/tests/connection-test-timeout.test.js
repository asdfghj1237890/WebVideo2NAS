import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// Onboarding step 2 tells the user to press [test]. That button used to be
// able to hang forever: both requests were bare fetches with no signal, the
// health body was read outside any bound, and the success path awaited a
// version probe its own comment called best-effort. A stalled NAS or proxy
// left the button disabled with no result and no error, so the step could
// never complete — the failure landing squarely on the users onboarding
// exists for.

function makeElement(id) {
  return {
    id, hidden: false, textContent: '', innerHTML: '', title: '', value: '',
    disabled: false, style: {}, dataset: {},
    classList: { _s: new Set(), add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    addEventListener() {}, appendChild() {}, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [],
  };
}

function loadOptions(fetchImpl) {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  const ctx = loadScriptIntoContext('options/options.js', {
    chrome: {
      runtime: { getManifest: () => ({ version: '0.0.0' }), openOptionsPage: () => {}, lastError: null },
      storage: {
        sync: { get: async () => ({}), set: async () => {} },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener() {} },
      },
    },
    document: {
      addEventListener() {},
      getElementById: getEl,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: (tag) => makeElement(tag),
      documentElement: { setAttribute() {} },
      body: { appendChild() {} },
      activeElement: null,
    },
    window: {},
    performance: { now: () => 0 },
    fetch: fetchImpl,
  });
  return { ctx, getEl };
}

const stalledBody = (url, options) => Promise.resolve({
  ok: true, status: 200,
  json: () => new Promise((_r, reject) => {
    if (options && options.signal) {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }
  }),
});

describe('options fetchJsonWithTimeout', () => {
  it('aborts a health body that never finishes arriving', async () => {
    const { ctx } = loadOptions(stalledBody);
    await expect(ctx.fetchJsonWithTimeout('http://nas.example/api/health', {}, 30))
      .rejects.toThrow(/abort/i);
  });

  it('aborts headers that never arrive', async () => {
    const { ctx } = loadOptions((url, options) => new Promise((_r, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }));
    await expect(ctx.fetchJsonWithTimeout('http://nas.example/api/health', {}, 30))
      .rejects.toThrow(/abort/i);
  });
});

describe('testConnection does not hang on the version probe', () => {
  it('finishes and re-enables the button while the version never arrives', async () => {
    const { ctx, getEl } = loadOptions(async () => ({ ok: true, status: 200, json: async () => ({}) }));
    getEl('nasEndpoint').value = 'http://nas.example:52052';
    getEl('apiKey').value = 'k';

    // Health answers; the version probe never does.
    ctx.fetchJsonWithTimeout = async (url) => {
      if (String(url).includes('/api/health')) {
        return { response: { ok: true, status: 200 }, data: { status: 'healthy' } };
      }
      return new Promise(() => {});
    };

    const done = await Promise.race([
      ctx.testConnection().then(() => 'done'),
      new Promise((r) => setTimeout(() => r('hung'), 2000)),
    ]);

    expect(done).toBe('done');
    // The whole point: the verdict and the button do not wait on a cosmetic
    // request.
    expect(getEl('testBtn').disabled).toBe(false);
  }, 8000);

  it('routes both requests through the bounded helper with real limits', async () => {
    const { ctx, getEl } = loadOptions(async () => ({ ok: true, status: 200, json: async () => ({}) }));
    getEl('nasEndpoint').value = 'http://nas.example:52052';
    getEl('apiKey').value = 'k';

    const calls = [];
    ctx.fetchJsonWithTimeout = async (url, options, timeoutMs) => {
      calls.push({ url: String(url), timeoutMs });
      if (String(url).includes('/api/health')) {
        return { response: { ok: true, status: 200 }, data: { status: 'healthy' } };
      }
      return { response: { ok: true, status: 200 }, data: { version: '9.9.9' } };
    };

    await ctx.testConnection();

    const health = calls.find((c) => c.url.includes('/api/health'));
    const version = calls.find((c) => !c.url.includes('/api/health'));
    expect(health).toBeTruthy();
    expect(version).toBeTruthy();
    expect(health.timeoutMs).toBeGreaterThan(0);
    expect(version.timeoutMs).toBeGreaterThan(0);
    // The cosmetic probe must not outlive the request that decides the verdict.
    expect(version.timeoutMs).toBeLessThan(health.timeoutMs);
  });
});
