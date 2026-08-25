import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The play-first gate exists because IP- and cookie-bound CDN tokens are only
// minted once the page's player starts. It used to lift on isNowPlaying — a
// click-match heuristic with a ten-minute window — and on isLive, neither of
// which is evidence that a token was issued. A user could therefore send a URL
// that had never been made fetchable, from a page where nothing had played.
//
// Only observed playback lifts it now, and the rule is stated in the pane
// rather than living in a tooltip on a disabled button.

let hintEl;
let hintTextEl;
let listEl;

function makeCtx() {
  hintEl = { hidden: true };
  hintTextEl = { textContent: '' };
  listEl = {
    innerHTML: '',
    classList: { toggle() {}, add() {}, remove() {} },
    querySelectorAll: () => [], querySelector: () => null,
  };
  const ctx = loadScriptIntoContext('sidepanel.js', {
    chrome: {
      storage: {
        sync: { get: async () => ({}) },
        local: { get: async () => ({}), set: async () => {} },
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
      addEventListener() {},
      getElementById: (id) => {
        if (id === 'detectedUrlsList') return listEl;
        if (id === 'playFirstHint') return hintEl;
        if (id === 'playFirstHintText') return hintTextEl;
        return null;
      },
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => ({ classList: { add() {} } }),
      documentElement: { setAttribute() {} }, body: { appendChild() {} },
    },
    window: {}, navigator: { clipboard: {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => [] }),
  });
  ctx.renderQualityChips = () => {};
  ctx.updateBulkBar = () => {};
  return ctx;
}

const hls = (extra = {}) => ({
  url: 'https://cdn.example.com/v/720p/chunklist.m3u8',
  detectedFormat: 'm3u8',
  ...extra,
});

const withBrowserMode = (ctx, on) =>
  ctx.__eval(`settings = { useBrowserSide: ${on ? 'true' : 'false'} };`);

describe('the gate lifts only for observed playback', () => {
  it('holds a stream nothing has played', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    expect(ctx.urlInfoRequiresPlayFirst(hls())).toBe(true);
  });

  it('is not lifted by the click heuristic', () => {
    // The reported case: a click matched, so the panel believed it was playing.
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    expect(ctx.urlInfoRequiresPlayFirst(hls({ isNowPlaying: true }))).toBe(true);
  });

  it('is not lifted merely by the stream being live', () => {
    // Live is a property of the manifest, not of playback; a live stream needs
    // the same token as any other.
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    expect(ctx.urlInfoRequiresPlayFirst(hls({ isLive: true }))).toBe(true);
  });

  it('lifts once segments have actually been seen', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    expect(ctx.urlInfoRequiresPlayFirst(hls({ playbackObserved: true }))).toBe(false);
  });

  it('does not apply at all when browser mode is off', () => {
    // NAS-direct downloads do not need a per-session token.
    const ctx = makeCtx();
    withBrowserMode(ctx, false);
    expect(ctx.urlInfoRequiresPlayFirst(hls())).toBe(false);
  });

  it('does not apply to manifest-less JSON DASH rows', () => {
    // Those already carry freshly signed URLs; waiting for a second matching
    // request can deadlock.
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    expect(ctx.urlInfoRequiresPlayFirst(hls({ directDash: true }))).toBe(false);
  });
});

describe('the rule is visible while it is blocking something', () => {
  it('shows the hint when browser mode holds a detected stream', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    ctx.__eval(`detectedUrls = ${JSON.stringify([hls()])};`);

    ctx.renderDetectedUrls();

    expect(hintEl.hidden).toBe(false);
    expect(hintTextEl.textContent).not.toBe('');
  });

  it('hides it once playback has been observed', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    ctx.__eval(`detectedUrls = ${JSON.stringify([hls({ playbackObserved: true })])};`);

    ctx.renderDetectedUrls();

    expect(hintEl.hidden).toBe(true);
  });

  it('stays away when browser mode is off', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, false);
    ctx.__eval(`detectedUrls = ${JSON.stringify([hls()])};`);

    ctx.renderDetectedUrls();

    expect(hintEl.hidden).toBe(true);
  });

  it('stays away when nothing is detected', () => {
    const ctx = makeCtx();
    withBrowserMode(ctx, true);
    ctx.__eval('detectedUrls = [];');

    ctx.renderDetectedUrls();

    expect(hintEl.hidden).toBe(true);
  });
});
