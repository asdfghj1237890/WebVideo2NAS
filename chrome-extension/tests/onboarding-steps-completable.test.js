import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// Steps 3 and 4 were labelled "tracked in the side panel" while nothing was
// tracked: options.js hardcoded them to a 'manual' state that had no path to
// 'done'. A four-step checklist that invites completion had two steps it was
// impossible to finish, and the label described a behaviour the code did not
// have.
//
// The side panel now records both — opening it, and the first confirmed send —
// and the options page reflects them live. These tests hold that: every step
// must be reachable, and the two that come from the panel must arrive without
// reloading the options page.

function makeElement(id) {
  return {
    id, hidden: false, textContent: '', disabled: false, value: '',
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {}, appendChild() {}, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [],
  };
}

function loadOptions() {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  const ctx = loadScriptIntoContext('options/options.js', {
    chrome: {
      runtime: { getManifest: () => ({ version: '0' }), openOptionsPage: () => {}, lastError: null },
      storage: {
        sync: { get: async () => ({}), set: async () => {} },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener() {} },
      },
    },
    document: {
      addEventListener() {}, getElementById: getEl,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: (tag) => makeElement(tag),
      documentElement: { setAttribute() {} }, body: { appendChild() {} },
      activeElement: null,
    },
    window: {}, performance: { now: () => 0 },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  });
  return { ctx, getEl };
}

const stepState = (getEl, n) => {
  const el = getEl('onbStatus' + n);
  return el.classList.contains('done') ? 'done'
    : el.classList.contains('todo') ? 'todo'
    : 'neither';
};

describe('every checklist step can reach done', () => {
  it('starts with all four outstanding', () => {
    const { ctx, getEl } = loadOptions();
    ctx.refreshOnboardingStatus();
    for (const n of [1, 2, 3, 4]) expect(stepState(getEl, n)).toBe('todo');
  });

  it('never leaves a step in a state that is neither done nor todo', () => {
    // The old 'manual' state was exactly that: rendered, but not a state the
    // user could move out of.
    const { ctx, getEl } = loadOptions();
    ctx.refreshOnboardingStatus();
    for (const n of [1, 2, 3, 4]) expect(stepState(getEl, n)).not.toBe('neither');
  });

  it('ticks step 3 when the side panel reports it was opened', () => {
    const { ctx, getEl } = loadOptions();
    ctx.refreshOnboardingStatus();
    expect(stepState(getEl, 3)).toBe('todo');

    ctx.applyOnboardingFlagChange('local', {
      onboardingSidePanelOpened: { newValue: true },
    });

    expect(stepState(getEl, 3)).toBe('done');
  });

  it('ticks step 4 when the side panel reports the first send', () => {
    const { ctx, getEl } = loadOptions();
    ctx.applyOnboardingFlagChange('local', {
      onboardingFirstSendDone: { newValue: true },
    });
    expect(stepState(getEl, 4)).toBe('done');
  });

  it('reaches all four done, which is what the checklist promises', () => {
    const { ctx, getEl } = loadOptions();
    ctx.__eval("savedSnapshot = { nasEndpoint: 'http://nas.example:52052', apiKey: 'k' };");
    ctx.__eval("onbPingOkFor = onbSavedFingerprint();");
    ctx.applyOnboardingFlagChange('local', {
      onboardingSidePanelOpened: { newValue: true },
      onboardingFirstSendDone: { newValue: true },
    });

    for (const n of [1, 2, 3, 4]) expect(stepState(getEl, n)).toBe('done');
    // And the sidebar badge stops nagging once nothing is left.
    expect(getEl('onbNavTodo').hidden).toBe(true);
  });

  it('does not tick step 4 merely because onboarding was dismissed', () => {
    // Dismissing hides the strip; it is not evidence the user sent anything.
    const { ctx, getEl } = loadOptions();
    ctx.applyOnboardingFlagChange('local', {
      onboardingCompleted: { newValue: true },
    });
    expect(stepState(getEl, 4)).toBe('todo');
  });
});

// The scenario reported after the previous fix: step 3 ticked, step 4 never
// did. Recording "the user sent their first video" lived inside
// refreshOnboardingCoach, which returns early once onboarding is marked done —
// so anyone who had dismissed the strip, or completed onboarding once, could
// never write the flag. A fact about usage must not be gated on the UI that
// displays it.
function loadSidePanel({ storedLocal = {} } = {}) {
  const writes = [];
  const local = { ...storedLocal };
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: {
          get: async (keys) => {
            const out = {};
            for (const k of [].concat(keys)) {
              if (Object.prototype.hasOwnProperty.call(local, k)) out[k] = local[k];
            }
            return out;
          },
          set: async (obj) => { writes.push(obj); Object.assign(local, obj); },
        },
        session: { get: async () => ({}) },
        onChanged: { addListener() {} },
      },
      runtime: {
        onMessage: { addListener() {} }, openOptionsPage: () => {},
        sendMessage: async () => {}, lastError: null,
      },
      tabs: {
        query: (_q, cb) => cb([]), onUpdated: { addListener() {} },
        onActivated: { addListener() {} },
      },
    },
    document: {
      addEventListener() {}, getElementById: () => null,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => ({ classList: { add() {} } }),
      documentElement: { setAttribute() {} }, body: { appendChild() {} },
    },
    window: {}, navigator: { clipboard: {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  });
  return { ctx, writes };
}

const settle = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve(); };
const FIRST_SEND = 'onboardingFirstSendDone';

describe('the first send is recorded regardless of the coach strip', () => {
  it('records it for a user who already dismissed onboarding', async () => {
    const { ctx, writes } = loadSidePanel({ storedLocal: { onboardingCompleted: true } });
    ctx.__eval('onboardingDone = true; firstSendRecorded = false;');

    await ctx.recordFirstSend();
    await settle();

    expect(writes.some((w) => w[FIRST_SEND] === true)).toBe(true);
  });

  it('records it for a first-time user too', async () => {
    const { ctx, writes } = loadSidePanel();
    ctx.__eval('onboardingDone = false; firstSendRecorded = false;');

    await ctx.recordFirstSend();
    await settle();

    expect(writes.some((w) => w[FIRST_SEND] === true)).toBe(true);
  });

  it('writes once, not on every subsequent send', async () => {
    const { ctx, writes } = loadSidePanel();
    ctx.__eval('onboardingDone = false; firstSendRecorded = false;');

    await ctx.recordFirstSend();
    await ctx.recordFirstSend();
    await ctx.recordFirstSend();
    await settle();

    expect(writes.filter((w) => w[FIRST_SEND] === true).length).toBe(1);
  });

  it('allows a retry when the write failed', async () => {
    // Swallowing a storage failure for good would lose the step silently.
    const { ctx } = loadSidePanel();
    ctx.__eval('onboardingDone = false; firstSendRecorded = false;');
    ctx.chrome.storage.local.set = async () => { throw new Error('storage full'); };

    await ctx.recordFirstSend();
    expect(ctx.__eval('firstSendRecorded')).toBe(false);
  });
});
