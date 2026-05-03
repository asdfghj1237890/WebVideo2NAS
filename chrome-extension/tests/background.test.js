import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

function makeChromeStub() {
  return {
    runtime: {
      sendMessage: () => {},
      lastError: null,
      onInstalled: { addListener: () => {} },
      onMessage: { addListener: () => {} },
      openOptionsPage: () => {},
      getManifest: () => ({ version: '0.0.0' }),
    },
    storage: {
      sync: {
        get: (_keys, cb) => cb({}),
        set: async () => {},
      },
      local: {
        set: async () => {},
        get: async () => ({}),
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
      get: (_id, cb) => cb(null),
    },
    webNavigation: {
      onCommitted: { addListener: () => {} },
    },
    contextMenus: {
      create: () => {},
      onClicked: { addListener: () => {} },
    },
    notifications: {
      create: () => {},
    },
    sidePanel: {
      open: async () => {},
    },
    cookies: {
      getAll: async () => [],
    },
  };
}

function withFixedNow(ctx, nowMs) {
  ctx.Date = class extends Date {
    static now() {
      return nowMs;
    }
  };
}

describe('background.js pure helpers', () => {
  it('isCandidateVideoUrl accepts m3u8/mpd/mp4/mov and rejects obvious non-video', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    expect(ctx.isCandidateVideoUrl('https://a/b/c.m3u8')).toBe(true);
    expect(ctx.isCandidateVideoUrl('https://a/b/c.mpd')).toBe(true);
    expect(ctx.isCandidateVideoUrl('https://a/b/manifest.mpd?token=abc')).toBe(true);
    expect(ctx.isCandidateVideoUrl('https://a/b/c.mp4')).toBe(true);
    expect(ctx.isCandidateVideoUrl('https://a/b/c.mov')).toBe(true);
    expect(ctx.isCandidateVideoUrl('https://lurl6.lurl.cc/20260501/abc.mov')).toBe(true);

    // segments
    expect(ctx.isCandidateVideoUrl('https://a/b/seg0001.ts')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/seg0001.m4s')).toBe(false);

    // false positives
    expect(ctx.isCandidateVideoUrl('https://a/b/preview_720p.mp4.jpg')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/playlist.m3u8.png')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/app.js?video=1.mp4')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/preview.mov.jpg')).toBe(false);
  });

  it('scoreUrlInfo prefers recent + range hits + media type', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });

    const now = 1_000_000;
    withFixedNow(ctx, now);

    const base = {
      url: 'https://cdn.example.com/v/video.mp4',
      timestamp: now - 5_000,
      requestType: 'media',
      hitCount: 1,
      rangeHitCount: 0,
    };

    const s1 = ctx.scoreUrlInfo(base);
    const s2 = ctx.scoreUrlInfo({ ...base, rangeHitCount: 1 });
    const s3 = ctx.scoreUrlInfo({ ...base, rangeHitCount: 1, hitCount: 10 });

    expect(s2).toBeGreaterThan(s1);
    expect(s3).toBeGreaterThan(s2);
  });

  it('getSortedUrlsForTab does not mark now playing without user click', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });

    const now = 2_000_000;
    withFixedNow(ctx, now);

    const tabId = 123;
    ctx.__eval(`currentTabUrls[${tabId}] = ${JSON.stringify([
      {
        url: 'https://cdn.example.com/v/low.m3u8',
        timestamp: now - 60_000,
        requestType: 'xmlhttprequest',
        hitCount: 1,
        rangeHitCount: 0,
      },
      {
        url: 'https://cdn.example.com/v/high.mp4',
        timestamp: now - 2_000,
        requestType: 'media',
        hitCount: 3,
        rangeHitCount: 2,
      },
    ])};`);

    const sorted = ctx.getSortedUrlsForTab(tabId);
    // Without user click, no item should be marked as now playing
    expect(sorted[0].url).toContain('high.mp4');
    expect(sorted[0].isNowPlaying).toBe(false);
    expect(sorted[1].isNowPlaying).toBe(false);
  });

  it('getSortedUrlsForTab marks video as now playing when user clicked', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });

    const now = 2_000_000;
    withFixedNow(ctx, now);

    const tabId = 123;
    ctx.__eval(`currentTabUrls[${tabId}] = ${JSON.stringify([
      {
        url: 'https://cdn.example.com/v/low.m3u8',
        timestamp: now - 60_000,
        requestType: 'xmlhttprequest',
        hitCount: 1,
        rangeHitCount: 0,
      },
      {
        url: 'https://cdn.example.com/v/high.mp4',
        timestamp: now - 2_000,
        requestType: 'media',
        hitCount: 3,
        rangeHitCount: 2,
      },
    ])};`);

    // Simulate user clicking on the second video (index 1)
    ctx.__eval(`userClickedVideoByTab[${tabId}] = {
      videoIndex: 1,
      videoCount: 2,
      timestamp: ${now - 1000},
      matchedUrl: 'https://cdn.example.com/v/high.mp4'
    };`);

    const sorted = ctx.getSortedUrlsForTab(tabId);
    expect(sorted[0].url).toContain('high.mp4');
    expect(sorted[0].isNowPlaying).toBe(true);
    expect(sorted[1].isNowPlaying).toBe(false);
  });

  it('safeOrigin returns null on invalid URL', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });

    expect(ctx.safeOrigin('https://example.com/a')).toBe('https://example.com');
    expect(ctx.safeOrigin('not a url')).toBe(null);
  });

  it('getStoredPageTitle pins the title to the URL\'s source tab (multi-tab regression)', () => {
    // Regression: previously the side panel passed the *active* tab's title
    // when sending to NAS, so a URL detected in tab A would get tab B's
    // title if the user switched tabs before clicking Send. Now background
    // looks the title up from the urlInfo that was registered when the URL
    // was first detected.
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });

    const tabA = 100;
    const tabB = 200;
    ctx.__eval(`currentTabUrls[${tabA}] = ${JSON.stringify([
      { url: 'https://cdn.example.com/v/episode-1.m3u8', pageTitle: 'Anime · Episode 1', timestamp: 1000 },
    ])};`);
    ctx.__eval(`currentTabUrls[${tabB}] = ${JSON.stringify([
      { url: 'https://cdn.example.com/v/episode-2.m3u8', pageTitle: 'News · Top Story', timestamp: 1000 },
    ])};`);

    expect(ctx.getStoredPageTitle('https://cdn.example.com/v/episode-1.m3u8')).toBe('Anime · Episode 1');
    expect(ctx.getStoredPageTitle('https://cdn.example.com/v/episode-2.m3u8')).toBe('News · Top Story');
    // Unknown URL → null (caller falls back to whatever the message had)
    expect(ctx.getStoredPageTitle('https://cdn.example.com/v/unknown.m3u8')).toBe(null);
  });
});
