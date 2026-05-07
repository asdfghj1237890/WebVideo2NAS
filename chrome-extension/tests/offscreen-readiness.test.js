// Codex adversarial-review (high): cold offscreen startup must wait
// for the document's OFFSCREEN_READY ack before the SW posts
// START_BROWSER_JOB. Otherwise sendMessage races the listener
// registration in offscreen.js and fails with "Could not establish
// connection," after /api/jobs/init has already allocated NAS staging
// + DNR rules.
//
// We exercise this by loading background.js into a vm context, calling
// `_ensureOffscreenDocument()` against a stubbed chrome.offscreen, and
// verifying that:
//   1. The returned promise does NOT resolve until OFFSCREEN_READY is
//      fed through the SW's runtime.onMessage listener.
//   2. After close + recreate, the SW waits for a fresh OFFSCREEN_READY
//      (no stale-resolve from the prior cycle).
//   3. The hot-path (existing doc) resolves immediately without
//      needing a fresh ack.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';
import path from 'node:path';

const BACKGROUND_SCRIPT = path.resolve(__dirname, '..', 'background.js');


function makeChromeStub({ hasDocumentImpl } = {}) {
  const noop = () => {};
  const messageListeners = [];
  return {
    runtime: {
      sendMessage: vi.fn(async () => undefined),
      onMessage: {
        addListener: (fn) => messageListeners.push(fn),
        removeListener: (fn) => {
          const i = messageListeners.indexOf(fn);
          if (i >= 0) messageListeners.splice(i, 1);
        },
      },
      onInstalled: { addListener: noop },
      lastError: null,
      openOptionsPage: noop,
      getManifest: () => ({ version: '2.5.0' }),
      getURL: (p) => `chrome-extension://test/${p}`,
    },
    storage: {
      sync: { get: (_keys, cb) => cb && cb({}), set: async () => {} },
      local: { set: async () => {}, get: async () => ({}), remove: async () => {} },
      onChanged: { addListener: noop },
    },
    webRequest: {
      onBeforeRequest: { addListener: noop },
      onSendHeaders: { addListener: noop },
      onHeadersReceived: { addListener: noop },
    },
    webNavigation: { onCommitted: { addListener: noop } },
    action: {
      setBadgeText: noop, setBadgeBackgroundColor: noop,
      onClicked: { addListener: noop },
    },
    tabs: {
      onRemoved: { addListener: noop }, onUpdated: { addListener: noop },
      onActivated: { addListener: noop },
      query: (_q, cb) => cb && cb([]), get: (_id, cb) => cb && cb(null),
    },
    contextMenus: { create: noop, onClicked: { addListener: noop } },
    notifications: {
      create: vi.fn(), clear: noop,
      onClicked: { addListener: noop }, onClosed: { addListener: noop },
    },
    cookies: { getAll: async () => [] },
    sidePanel: { setOptions: async () => {}, open: async () => {} },
    declarativeNetRequest: {
      updateSessionRules: vi.fn(async () => undefined),
    },
    offscreen: {
      hasDocument: vi.fn(hasDocumentImpl || (() => Promise.resolve(false))),
      createDocument: vi.fn().mockResolvedValue(),
      closeDocument: vi.fn().mockResolvedValue(),
    },
    __messageListeners: messageListeners,
  };
}


function fireMessage(chrome, msg) {
  for (const fn of chrome.__messageListeners) {
    try { fn(msg, {}, () => {}); } catch (_) {}
  }
}


describe('_ensureOffscreenDocument: waits for OFFSCREEN_READY ack', () => {
  let ctx;
  let chrome;

  beforeEach(() => {
    chrome = makeChromeStub();
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: vi.fn(),
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
  });

  it('does NOT resolve before OFFSCREEN_READY arrives, then resolves once it does', async () => {
    const ensure = ctx.__eval('_ensureOffscreenDocument()');

    let settled = false;
    ensure.then(() => { settled = true; }, () => { settled = true; });
    // Drain microtasks/macrotasks so the synchronous-up-to-first-await
    // portion of _ensureOffscreenDocument runs and reaches the
    // hasDocument()/createDocument() checks.
    await new Promise((r) => setTimeout(r, 10));

    // createDocument was called.
    expect(chrome.offscreen.createDocument).toHaveBeenCalledOnce();
    // But ensure is still waiting for READY.
    expect(settled).toBe(false);

    // Fire the READY ack the way offscreen.js would.
    fireMessage(chrome, { type: 'OFFSCREEN_READY', target: 'service-worker' });
    await ensure;
    expect(settled).toBe(true);
  });

  it('hot-path (existing doc) resolves immediately without waiting for READY', async () => {
    chrome = makeChromeStub({ hasDocumentImpl: () => Promise.resolve(true) });
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: vi.fn(),
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    // Should resolve without anyone firing OFFSCREEN_READY — listener
    // was registered when the doc originally loaded earlier.
    await ctx.__eval('_ensureOffscreenDocument()');
    expect(chrome.offscreen.createDocument).not.toHaveBeenCalled();
  });

  it('after close + recreate, the SW waits for a fresh READY ack', async () => {
    // First creation cycle: kick off ensure, drain microtasks, fire READY.
    const ensure1 = ctx.__eval('_ensureOffscreenDocument()');
    await new Promise((r) => setTimeout(r, 10));
    fireMessage(chrome, { type: 'OFFSCREEN_READY', target: 'service-worker' });
    await ensure1;

    // Close the doc — should reset the readiness gate.
    await ctx.__eval('_closeOffscreenDocument()');

    // Make hasDocument return false so the next ensure goes through
    // createDocument again.
    chrome.offscreen.hasDocument = vi.fn(() => Promise.resolve(false));
    chrome.offscreen.createDocument = vi.fn().mockResolvedValue();

    const ensure2 = ctx.__eval('_ensureOffscreenDocument()');
    let settled = false;
    ensure2.then(() => { settled = true; }, () => { settled = true; });
    await new Promise((r) => setTimeout(r, 10));
    // Critical: did NOT carry over the prior cycle's READY.
    expect(settled).toBe(false);

    fireMessage(chrome, { type: 'OFFSCREEN_READY', target: 'service-worker' });
    await ensure2;
    expect(settled).toBe(true);
  });
});
