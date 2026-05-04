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

  it('findBestCapturedEntry never crosses tabs even on same-origin sites (multi-tab regression)', () => {
    // Regression: with origin-prefix scoring, sending from tab B/C in a
    // multi-tab same-site session would pick up tab A's most-recent
    // captured manifest (because every capture matched the origin and the
    // tie-breaker was timestamp). Result: A/B/C all downloaded video A.
    // Now the substitution is hard-filtered by tabId.
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_000_000;
    withFixedNow(ctx, now);

    const tabA = 100;
    const tabB = 200;
    const tabC = 300;
    const videoA = 'https://cdn.missav.ws/v/code-A.m3u8?token=AAA';
    const videoB = 'https://cdn.missav.ws/v/code-B.m3u8?token=BBB';
    const videoC = 'https://cdn.missav.ws/v/code-C.m3u8?token=CCC';

    ctx.__eval(`capturedHeaders = ${JSON.stringify({
      [videoA]: {
        headers: { Cookie: 'a=1' },
        timestamp: now - 1_000,         // most recent — would have won under old scoring
        initiator: 'https://missav.ws',
        tabId: tabA,
      },
      [videoB]: {
        headers: { Cookie: 'b=1' },
        timestamp: now - 30_000,
        initiator: 'https://missav.ws',
        tabId: tabB,
      },
      [videoC]: {
        headers: { Cookie: 'c=1' },
        timestamp: now - 60_000,
        initiator: 'https://missav.ws',
        tabId: tabC,
      },
    })};`);

    // Sending from tab B must pick tab B's capture, not tab A's, despite A
    // being more recent and all three sharing origin.
    const fromB = ctx.findBestCapturedEntry(videoB, 'https://missav.ws', tabB);
    expect(fromB).not.toBeNull();
    expect(fromB.url).toBe(videoB);

    const fromC = ctx.findBestCapturedEntry(videoC, 'https://missav.ws', tabC);
    expect(fromC.url).toBe(videoC);

    const fromA = ctx.findBestCapturedEntry(videoA, 'https://missav.ws', tabA);
    expect(fromA.url).toBe(videoA);
  });

  it('findBestCapturedEntry without sourceTabId falls back to strict initiator equality', () => {
    // When the caller can't supply a tab anchor (orphan / service-worker
    // capture path), substitution must NOT use the old origin-prefix logic.
    // It must require entry.initiator === sourcePageUrl exactly, otherwise
    // same-origin different-page captures would still leak.
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_000_000;
    withFixedNow(ctx, now);

    const pageA = 'https://missav.ws/dm18/code-A';
    const pageB = 'https://missav.ws/dm18/code-B';
    const videoA = 'https://cdn.missav.ws/v/code-A.m3u8?token=AAA';
    const videoB = 'https://cdn.missav.ws/v/code-B.m3u8?token=BBB';

    ctx.__eval(`capturedHeaders = ${JSON.stringify({
      [videoA]: { headers: { Cookie: 'a=1' }, timestamp: now - 1_000, initiator: pageA, tabId: -1 },
      [videoB]: { headers: { Cookie: 'b=1' }, timestamp: now - 30_000, initiator: pageB, tabId: -1 },
    })};`);

    // No sourceTabId — must fall back to initiator equality. Sending
    // for tab-B's URL with pageB anchor must not pick videoA (different page).
    const fromB = ctx.findBestCapturedEntry(videoB, pageB, null);
    expect(fromB).not.toBeNull();
    expect(fromB.url).toBe(videoB);

    // No sourceTabId AND no sourcePageUrl — refuse substitution outright
    // rather than guessing across tabs.
    const ambiguous = ctx.findBestCapturedEntry(videoB, '', null);
    expect(ambiguous).toBeNull();
  });

  it('findBestCapturedEntry within one tab still re-keys clean URL → tokenized variant', () => {
    // Substitution's whole purpose: when the user clicks Send on the
    // detected clean URL, but the player actually fetched a tokenized
    // variant (whose Cookie/Referer we captured), we want to swap to the
    // tokenized one. Same-tab filter must NOT break this.
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_000_000;
    withFixedNow(ctx, now);

    const tab = 42;
    const cleanUrl   = 'https://cdn.example.com/v/master.m3u8';
    const tokenUrl   = 'https://cdn.example.com/v/master.m3u8?auth=abc&exp=999';

    ctx.__eval(`capturedHeaders = ${JSON.stringify({
      [cleanUrl]: { headers: {}, timestamp: now - 60_000, initiator: 'https://example.com', tabId: tab },
      [tokenUrl]: { headers: { Cookie: 'sid=1' }, timestamp: now - 1_000, initiator: 'https://example.com', tabId: tab },
    })};`);

    const best = ctx.findBestCapturedEntry(cleanUrl, 'https://example.com', tab);
    // Tokenized variant scores higher (search + cookie + recent), and the
    // same-tab filter doesn't disqualify it.
    expect(best.url).toBe(tokenUrl);
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
