import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// onboardingCompleted is one flag shared by the options page and the side
// panel, and the onboarding flow itself guarantees both are open at once:
// install opens options, and step 3 tells the user to open the panel. Reading
// the flag only at init therefore breaks the dismiss button's literal promise
// — press "don't show this again" in options and the already-open panel keeps
// coaching on its next repaint, and vice versa.
//
// Both pages route their storage listener through applyOnboardingFlagChange,
// which is what these tests drive. The listener body is a single delegating
// call, so what is left untested is the registration, not the behaviour.

function makeElement() {
  return { hidden: false, textContent: '', title: '', disabled: false, classList: { add() {}, remove() {}, contains: () => false } };
}

function makeDomStub(ids) {
  const elements = new Map(ids.map((id) => [id, makeElement()]));
  return {
    elements,
    document: {
      addEventListener: () => {},
      getElementById: (id) => elements.get(id) || null,
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => makeElement(),
      documentElement: { setAttribute: () => {} },
      body: { appendChild: () => {} },
    },
  };
}

function loadSidePanel() {
  const dom = makeDomStub(['onbCoach', 'onbCoachText', 'onbCoachAction', 'onbCoachDismiss', 'connectionStatus']);
  const writes = [];
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async (obj) => { writes.push(obj); } },
        onChanged: { addListener: () => {} },
      },
      runtime: {
        onMessage: { addListener: () => {} },
        openOptionsPage: () => {},
        sendMessage: (_msg, cb) => cb && cb({ urls: [] }),
        lastError: null,
      },
      tabs: {
        query: (_q, cb) => cb([{ id: 1, title: 't', url: 'https://example.com' }]),
        onUpdated: { addListener: () => {} },
        onActivated: { addListener: () => {} },
      },
    },
    document: dom.document,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: async () => ({ ok: true, json: async () => [] }),
    window: {},
  });
  return { ctx, dom, writes };
}

function loadOptions() {
  const dom = makeDomStub(['onbNavTodo', 'onbDoneRow', 'onbDoneBtn', 'onbStatus1', 'onbStatus2', 'onbStatus3', 'onbStatus4']);
  const ctx = loadScriptIntoContext('options/options.js', {
    chrome: {
      runtime: { getManifest: () => ({ version: '0.0.0' }), openOptionsPage: () => {}, lastError: null },
      storage: {
        sync: { get: async () => ({}), set: async () => {} },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener: () => {} },
      },
    },
    document: dom.document,
    performance: { now: () => 0 },
    window: {},
  });
  return { ctx, dom };
}

const change = (value) => ({ onboardingCompleted: { newValue: value } });

describe('side panel picks up a flag written by the options page', () => {
  it('hides the coach strip when the other page completes onboarding', () => {
    const { ctx, dom } = loadSidePanel();
    // Panel is mid-onboarding: not done, connected, nothing detected yet.
    ctx.__eval('onboardingDone = false;');
    dom.elements.get('onbCoach').hidden = false;

    const handled = ctx.applyOnboardingFlagChange('local', change(true));

    expect(handled).toBe(true);
    expect(ctx.__eval('onboardingDone')).toBe(true);
    expect(dom.elements.get('onbCoach').hidden).toBe(true);
  });

  it('ignores changes in the sync area and unrelated local keys', () => {
    const { ctx } = loadSidePanel();
    ctx.__eval('onboardingDone = false;');

    expect(ctx.applyOnboardingFlagChange('sync', change(true))).toBe(false);
    expect(ctx.applyOnboardingFlagChange('local', { detectedUrls: { newValue: [] } })).toBe(false);
    expect(ctx.__eval('onboardingDone')).toBe(false);
  });

  it('honours a reset back to false', () => {
    const { ctx } = loadSidePanel();
    ctx.__eval('onboardingDone = true;');

    expect(ctx.applyOnboardingFlagChange('local', change(false))).toBe(true);
    expect(ctx.__eval('onboardingDone')).toBe(false);
  });
});

describe('options page picks up a flag written by the side panel', () => {
  it('stops presenting the checklist as outstanding', () => {
    const { ctx, dom } = loadOptions();
    ctx.__eval('onboardingDone = false;');
    dom.elements.get('onbNavTodo').hidden = false;

    const handled = ctx.applyOnboardingFlagChange('local', change(true));

    expect(handled).toBe(true);
    expect(ctx.__eval('onboardingDone')).toBe(true);
    // The sidebar "!" badge is hidden once onboarding is done, whatever the
    // individual step states say.
    expect(dom.elements.get('onbNavTodo').hidden).toBe(true);
    // The dismiss control is hidden rather than greyed once there is nothing
    // left to dismiss.
    expect(dom.elements.get('onbDoneRow').hidden).toBe(true);
  });

  it('ignores changes in the sync area and unrelated local keys', () => {
    const { ctx } = loadOptions();
    ctx.__eval('onboardingDone = false;');

    expect(ctx.applyOnboardingFlagChange('sync', change(true))).toBe(false);
    expect(ctx.applyOnboardingFlagChange('local', { avTaskHistory: { newValue: [] } })).toBe(false);
    expect(ctx.__eval('onboardingDone')).toBe(false);
  });
});
