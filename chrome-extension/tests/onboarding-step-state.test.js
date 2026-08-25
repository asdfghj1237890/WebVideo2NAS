import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The getting_started checklist has to describe one configuration, not two.
//
// An earlier cut judged step 1 ("NAS address set") on the saved snapshot but
// step 2 ("connection tested") on the live input fields. Saving an invalid
// endpoint, typing a valid one and pressing Test without saving then ticked
// both steps green and hid the sidebar badge, while the side panel carried on
// dialling the invalid endpoint it had actually been given. Both steps are now
// judged against the saved credentials, which is what the rest of the
// extension reads.

function loadOptions() {
  return loadScriptIntoContext('options/options.js', {
    window: {},
    performance: { now: () => 0 },
    document: {
      addEventListener: () => {},
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      documentElement: { setAttribute: () => {} },
    },
    chrome: {
      runtime: { getManifest: () => ({ version: '0.0.0' }), openOptionsPage: () => {}, lastError: null },
      storage: {
        sync: { get: async () => ({}), set: async () => {} },
        local: { get: async () => ({}), set: async () => {} },
        onChanged: { addListener: () => {} },
      },
    },
  });
}

const fp = (endpoint, apiKey) => JSON.stringify([endpoint, apiKey]);

const GOOD = fp('http://192.168.1.10:52052', 'good-key');
const BAD = fp('http://192.168.1.99:52052', 'stale-key');

describe('onboarding step states', () => {
  const ctx = loadOptions();
  const states = (saved, pingOkFor) => ctx.onboardingStepStates(saved, pingOkFor);

  it('nothing saved yet: both steps outstanding', () => {
    expect(states(null, null)).toEqual({ step1: 'todo', step2: 'todo', badgeHidden: false });
  });

  it('saved but never tested: step 1 only', () => {
    expect(states(GOOD, null)).toEqual({ step1: 'done', step2: 'todo', badgeHidden: false });
  });

  it('saved and that same config tested: both done, badge hidden', () => {
    expect(states(GOOD, GOOD)).toEqual({ step1: 'done', step2: 'done', badgeHidden: true });
  });

  // The regression the adversarial review caught.
  it('testing credentials that were never saved does not tick step 2', () => {
    // saved = the bad config the side panel will actually use;
    // pingOkFor = the good config the user typed in and tested without saving.
    const result = states(BAD, GOOD);
    expect(result.step2).toBe('todo');
    expect(result.badgeHidden).toBe(false);
    // Step 1 stays done — an address *is* saved. It just is not the tested one.
    expect(result.step1).toBe('done');
  });

  it('changing the saved config after a successful test un-ticks step 2', () => {
    expect(states(GOOD, GOOD).step2).toBe('done');
    expect(states(BAD, GOOD).step2).toBe('todo');
  });

  it('a pass recorded before saving counts once those credentials are saved', () => {
    // Test-then-save is the natural order, so the recorded pass must survive
    // the save rather than being discarded for arriving early.
    expect(states(null, GOOD).step2).toBe('todo');
    expect(states(GOOD, GOOD).step2).toBe('done');
  });
});

describe('onboarding fingerprint source', () => {
  const ctx = loadOptions();

  it('derives from the saved snapshot, not the input fields', () => {
    ctx.__eval("savedSnapshot = { nasEndpoint: 'http://nas.example:52052', apiKey: 'k' };");
    expect(ctx.onbSavedFingerprint()).toBe(fp('http://nas.example:52052', 'k'));
  });

  it('is null until both halves are present', () => {
    ctx.__eval("savedSnapshot = { nasEndpoint: 'http://nas.example:52052', apiKey: '' };");
    expect(ctx.onbSavedFingerprint()).toBeNull();
    ctx.__eval("savedSnapshot = { nasEndpoint: '', apiKey: 'k' };");
    expect(ctx.onbSavedFingerprint()).toBeNull();
  });

  it('trims, so whitespace edits do not read as a different config', () => {
    ctx.__eval("savedSnapshot = { nasEndpoint: '  http://nas.example:52052  ', apiKey: ' k ' };");
    expect(ctx.onbSavedFingerprint()).toBe(fp('http://nas.example:52052', 'k'));
  });
});
