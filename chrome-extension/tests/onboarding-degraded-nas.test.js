import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The coach strip is for the user whose NAS is not answering yet, so its
// initialisation must not sit behind a NAS request. It used to: init awaited
// loadRecentJobs(), whose /api/jobs fetch had no timeout, so a configured but
// unreachable NAS stalled DOMContentLoaded before the strip was wired. The
// strip stayed hidden with no dismiss or settings handler, and because
// onboardingDone defaults to true nothing later could reveal it — the worse
// the connection, the less likely the guidance appeared.
//
// These tests drive the real DOMContentLoaded handler with a fetch that never
// settles, which is what a dropped-packet NAS looks like from the panel.

function makeElement(id) {
  return {
    id,
    hidden: true,
    textContent: '',
    innerHTML: '',
    title: '',
    value: '',
    disabled: false,
    style: {},
    dataset: {},
    onclick: null,
    classList: {
      _set: new Set(),
      add(...c) { c.forEach((x) => this._set.add(x)); },
      remove(...c) { c.forEach((x) => this._set.delete(x)); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, bottom: 0, left: 0, right: 0 }),
    closest: () => null,
    setAttribute() {},
    removeAttribute() {},
  };
}

function makeHarness({ storedLocal = {}, syncSettings = {} } = {}) {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  let domReady = null;

  const document = {
    addEventListener: (evt, fn) => { if (evt === 'DOMContentLoaded') domReady = fn; },
    getElementById: getEl,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag) => makeElement(tag),
    documentElement: { setAttribute() {}, lang: '' },
    body: { appendChild() {}, classList: makeElement('body').classList },
  };

  const fetchArgs = [];
  const chrome = {
    storage: {
      sync: { get: async () => ({ ...syncSettings }), set: async () => {} },
      local: {
        get: async (keys) => {
          const out = {};
          for (const k of [].concat(keys)) {
            if (Object.prototype.hasOwnProperty.call(storedLocal, k)) out[k] = storedLocal[k];
          }
          return out;
        },
        set: async () => {},
      },
      onChanged: { addListener() {} },
    },
    runtime: {
      onMessage: { addListener() {} },
      openOptionsPage: () => {},
      sendMessage: (_msg, cb) => { if (cb) cb({}); },
      lastError: null,
      getManifest: () => ({ version: '0.0.0' }),
    },
    tabs: {
      query: (_q, cb) => cb([{ id: 1, title: 't', url: 'https://example.com' }]),
      onUpdated: { addListener() {} },
      onActivated: { addListener() {} },
    },
  };

  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome,
    document,
    window: {},
    navigator: { clipboard: { writeText: async () => {} }, language: 'en' },
    // A NAS that accepts the connection and then says nothing. Never settles,
    // so any `await` on it hangs forever.
    fetch: (url, options) => { fetchArgs.push({ url, options }); return new Promise(() => {}); },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
  });

  return { ctx, getEl, run: () => domReady && domReady(), fetchArgs };
}

const settle = async () => {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
};

describe('onboarding survives an unreachable NAS', () => {
  it('shows the coach strip even though every NAS request hangs', async () => {
    const h = makeHarness({
      storedLocal: {}, // no flag -> first run
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();

    const coach = h.getEl('onbCoach');
    expect(coach.hidden).toBe(false);
    expect(h.getEl('onbCoachText').textContent).not.toBe('');
  });

  it('attaches the dismiss and settings handlers despite the hang', async () => {
    const h = makeHarness({
      storedLocal: {},
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();

    // Both are assigned with onclick, so a stalled init is directly visible as
    // a null handler rather than as a silently dead button.
    expect(typeof h.getEl('onbCoachDismiss').onclick).toBe('function');
    expect(typeof h.getEl('onbCoachAction').onclick).toBe('function');
  });

  it('still hangs on the NAS request — the strip is early, not the network', async () => {
    // Guards the premise: if the fetch stub ever started resolving, the first
    // two tests would pass for the wrong reason.
    const h = makeHarness({
      storedLocal: {},
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();
    expect(h.fetchArgs.length).toBeGreaterThan(0);
  });

  it('stays quiet for a user who already finished onboarding', async () => {
    const h = makeHarness({
      storedLocal: { onboardingCompleted: true },
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();

    expect(h.getEl('onbCoach').hidden).toBe(true);
  });
});

describe('NAS requests made during init are bounded', () => {
  // /api/health always had a 5s bound; the job endpoints did not, so an
  // unresponsive NAS left them outstanding for as long as the socket stayed
  // open. Reordering init stops that from blocking onboarding, but the request
  // itself should still give up rather than hang indefinitely.
  it('every request carries an abort signal', async () => {
    const h = makeHarness({
      storedLocal: {},
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();

    expect(h.fetchArgs.length).toBeGreaterThan(0);
    const unbounded = h.fetchArgs.filter(({ options }) => !options || !options.signal);
    expect(unbounded.map(({ url }) => url)).toEqual([]);
  });

  it('bounds the jobs list specifically', async () => {
    const h = makeHarness({
      storedLocal: {},
      syncSettings: { nasEndpoint: 'http://192.168.1.99:52052', apiKey: 'k' },
    });
    h.run();
    await settle();

    const jobsCall = h.fetchArgs.find(({ url }) => String(url).includes('/api/jobs'));
    expect(jobsCall).toBeTruthy();
    expect(jobsCall.options.signal).toBeTruthy();
  });
});
