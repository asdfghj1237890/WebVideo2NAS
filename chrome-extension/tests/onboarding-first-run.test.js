import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// background.js decides, once per install, whether a user sees first-run
// onboarding. Two things make that decision easy to get wrong:
//
//   * The extension is sideloaded from GitHub Release zips, so a fresh
//     unpacked load fires reason === 'install' every time. Gating on reason
//     alone reopens the options tab on every developer reload.
//   * An install that predates onboarding has no flag at all. If the upgrade
//     path leaves it absent, the side panel later reads "absent" as "first
//     run" and coaches a long-time user — and whether they see it ends up
//     depending on whether the NAS happens to return job history.
//
// These tests pin both, since neither is visible from reading a diff.

function makeChromeStub({ storedLocal = {} } = {}) {
  const installedListeners = [];
  const local = { ...storedLocal };
  const writes = [];
  const opened = [];
  return {
    __installedListeners: installedListeners,
    __localWrites: writes,
    __openedOptionsPages: opened,
    __local: local,
    runtime: {
      sendMessage: () => {},
      lastError: null,
      onInstalled: { addListener: (fn) => installedListeners.push(fn) },
      onStartup: { addListener: () => {} },
      onMessage: { addListener: () => {} },
      openOptionsPage: () => opened.push(true),
      getManifest: () => ({ version: '0.0.0' }),
    },
    storage: {
      sync: { get: (_keys, cb) => cb({}), set: async () => {} },
      local: {
        get: async (keys) => {
          // Mirror chrome's contract: absent keys are simply not present on the
          // result object, which is exactly the distinction under test.
          const out = {};
          for (const k of [].concat(keys)) {
            if (Object.prototype.hasOwnProperty.call(local, k)) out[k] = local[k];
          }
          return out;
        },
        set: async (obj) => {
          writes.push(obj);
          Object.assign(local, obj);
        },
      },
      onChanged: { addListener: () => {} },
    },
    webRequest: {
      onBeforeRequest: { addListener: () => {} },
      onSendHeaders: { addListener: () => {} },
      onHeadersReceived: { addListener: () => {} },
    },
    action: {
      setBadgeText: () => {},
      setBadgeBackgroundColor: () => {},
      onClicked: { addListener: () => {} },
    },
    tabs: {
      onRemoved: { addListener: () => {} },
      onUpdated: { addListener: () => {} },
      onActivated: { addListener: () => {} },
      query: (_q, cb) => cb([]),
      get: (_id, cb) => cb({ id: _id, url: 'https://page.example/watch' }),
    },
    webNavigation: { onCommitted: { addListener: () => {} } },
    contextMenus: { create: () => {}, onClicked: { addListener: () => {} } },
    notifications: { create: () => {} },
    sidePanel: { open: async () => {} },
    cookies: { getAll: async () => [] },
  };
}

async function runOnInstalled(details, storedLocal) {
  const chrome = makeChromeStub({ storedLocal });
  const ctx = loadScriptIntoContext('background.js', { chrome, fetch: async () => {
    throw new Error('network disabled in this test');
  } });
  expect(ctx.chrome.__installedListeners.length).toBeGreaterThan(0);
  for (const fn of ctx.chrome.__installedListeners) fn(details);
  // maybeOpenOnboarding is async and fire-and-forget from the listener.
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  return chrome;
}

const FLAG = 'onboardingCompleted';

describe('first-run onboarding gate', () => {
  it('opens the options page on a genuine first install', async () => {
    const chrome = await runOnInstalled({ reason: 'install' }, {});
    expect(chrome.__openedOptionsPages.length).toBe(1);
  });

  it('does not reopen once onboarding has been completed', async () => {
    // This is the sideload reload case: reason is 'install' again, but the
    // flag survives, so the tab must not come back.
    const chrome = await runOnInstalled({ reason: 'install' }, { [FLAG]: true });
    expect(chrome.__openedOptionsPages.length).toBe(0);
  });

  it('marks an upgrade of a pre-onboarding install as already onboarded', async () => {
    const chrome = await runOnInstalled({ reason: 'update' }, {});
    expect(chrome.__openedOptionsPages.length).toBe(0);
    // The flag must be written, not merely left absent — an absent flag is what
    // the side panel would misread as "first run".
    expect(chrome.__localWrites).toContainEqual({ [FLAG]: true });
    expect(chrome.__local[FLAG]).toBe(true);
  });

  it('leaves an already-recorded flag untouched on upgrade', async () => {
    const chrome = await runOnInstalled({ reason: 'update' }, { [FLAG]: true });
    expect(chrome.__openedOptionsPages.length).toBe(0);
    expect(chrome.__localWrites).not.toContainEqual({ [FLAG]: true });
  });

  it('does not resurrect onboarding for a user who explicitly dismissed it', async () => {
    // false is a recorded decision, not an absent flag: the upgrade path must
    // not overwrite it, and a reload must not reopen the tab for it either.
    // Scoped to the flag because the update check writes storage.local too.
    const chrome = await runOnInstalled({ reason: 'update' }, { [FLAG]: false });
    const flagWrites = chrome.__localWrites.filter((w) => FLAG in w);
    expect(flagWrites).toEqual([]);
    expect(chrome.__local[FLAG]).toBe(false);
    expect(chrome.__openedOptionsPages.length).toBe(0);
  });
});
