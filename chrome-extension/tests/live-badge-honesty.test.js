import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

// The tile badge claimed LIVE for two unrelated facts.
//
// isLive means the manifest carries no ENDLIST — an actual live stream.
// isNowPlaying is a click-match heuristic with a ten-minute window: the user
// clicked something whose URL resembles this one. Merging them meant a
// seven-second VOD ad, on a page still showing its age gate with nothing
// playing, was labelled LIVE.
//
// playbackObserved is the real signal — media segments actually seen. These
// tests hold the badge to saying only what is true.

function makeCtx() {
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
      getElementById: (id) => (id === 'detectedUrlsList' ? listStub : null),
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => ({ classList: { add() {} }, textContent: '', innerHTML: '' }),
      documentElement: { setAttribute() {} }, body: { appendChild() {} },
    },
    window: {}, navigator: { clipboard: {} },
    fetch: async () => ({ ok: true, status: 200, json: async () => [] }),
  });
  return ctx;
}

// renderDetectedUrls writes into this element's innerHTML.
let listStub;
function renderWith(urlInfo) {
  listStub = { innerHTML: '', classList: { toggle() {}, add() {}, remove() {} }, querySelectorAll: () => [], querySelector: () => null };
  const ctx = makeCtx();
  ctx.renderQualityChips = () => {};
  ctx.updateBulkBar = () => {};
  ctx.__eval(`detectedUrls = ${JSON.stringify([urlInfo])};`);
  ctx.renderDetectedUrls();
  return listStub.innerHTML;
}

const base = {
  url: 'https://cdn.example.com/ad/welcome/720p/chunklist.m3u8',
  detectedFormat: 'm3u8',
  pageUrl: 'https://example.com/watch',
};

describe('the tile badge only claims what is true', () => {
  it('says nothing for a detected video that is neither live nor playing', () => {
    // The reported case: an ad manifest detected while the age gate is still
    // up. isNowPlaying is set by the click heuristic; nothing is playing.
    const html = renderWith({ ...base, duration: 7, isNowPlaying: true });
    expect(html).not.toContain('LIVE');
    expect(html).not.toContain('PLAYING');
  });

  it('says LIVE only for an actual live stream', () => {
    const html = renderWith({ ...base, isLive: true });
    expect(html).toContain('LIVE');
    expect(html).not.toContain('PLAYING');
  });

  it('says PLAYING once segments have actually been seen', () => {
    const html = renderWith({ ...base, duration: 7, playbackObserved: true });
    expect(html).toContain('PLAYING');
    expect(html).not.toContain('>LIVE<');
  });

  it('prefers LIVE when a live stream is also being played', () => {
    // Both true is the normal case for a live stream in progress; one label is
    // enough, and live is the more specific claim.
    const html = renderWith({ ...base, isLive: true, playbackObserved: true });
    expect(html).toContain('LIVE');
    expect(html).not.toContain('PLAYING');
  });

  it('does not let the click heuristic alone produce either label', () => {
    const html = renderWith({ ...base, duration: 7, isNowPlaying: true, playbackObserved: false });
    expect(html).not.toContain('LIVE');
    expect(html).not.toContain('PLAYING');
  });
});
