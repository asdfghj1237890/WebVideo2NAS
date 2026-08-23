import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

function makeChromeStub() {
  const onMessageListeners = [];
  return {
    runtime: {
      sendMessage: () => {},
      lastError: null,
      onInstalled: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      onMessage: { addListener: (fn) => onMessageListeners.push(fn) },
      __onMessageListeners: onMessageListeners,
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
      get: (_id, cb) => cb({ id: _id, url: 'https://page.example/watch' }),
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
    expect(ctx.isCandidateVideoUrl('https://a/b/token/playlist.m3u8/key_b5000000.m3u8key')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/app.js?video=1.mp4')).toBe(false);
    expect(ctx.isCandidateVideoUrl('https://a/b/preview.mov.jpg')).toBe(false);
  });

  it('infers an HLS variant playlist from segment URLs without treating segments as videos', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    const segment = 'https://cdn.example.com/hls/videos/202402/15/448181161/720P_4000K_448181161.mp4/seg-13-v1-a1.ts?h=tok%2Bsig&e=1778524057&f=1';
    expect(ctx.isCandidateVideoUrl(segment)).toBe(false);
    expect(ctx.inferHlsManifestFromSegmentUrl(segment))
      .toEqual({
        url: 'https://cdn.example.com/hls/videos/202402/15/448181161/720P_4000K_448181161.mp4/index-v1-a1.m3u8?h=tok%2Bsig&e=1778524057&f=1',
        dedupeKey: 'https://cdn.example.com/hls/videos/202402/15/448181161/720P_4000K_448181161.mp4/index-v1-a1.m3u8',
      });

    expect(ctx.inferHlsManifestFromSegmentUrl('https://cdn.example.com/hls/seg-9-v2.m4s?x=1'))
      .toBeNull();
    expect(ctx.inferHlsManifestFromSegmentUrl('https://cdn.example.com/hls/random.ts?x=1'))
      .toBeNull();
  });

  it('infers bitrate chunklists while preserving path-embedded auth tokens', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    const segment = 'https://cdn.example.com/video/1080p/hdntl=exp%3D999~hmac%3Dabc/media_b5000000_324.ts';
    expect(ctx.inferHlsManifestFromSegmentUrl(segment)).toEqual({
      url: 'https://cdn.example.com/video/1080p/hdntl=exp%3D999~hmac%3Dabc/chunklist_b5000000.m3u8',
      dedupeKey: 'https://cdn.example.com/video/1080p/hdntl=exp%3D999~hmac%3Dabc/chunklist_b5000000.m3u8',
    });
  });

  it('marks the closest observed HLS manifest as played for unknown segment names', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    const now = 4_000_000;
    withFixedNow(ctx, now);
    ctx.__eval(`currentTabUrls[7] = [
      {
        url: 'https://cdn.example.com/video/master.m3u8',
        detectedFormat: 'm3u8',
        timestamp: 10
      },
      {
        url: 'https://cdn.example.com/video/1080p/token/chunklist.m3u8',
        detectedFormat: 'm3u8',
        timestamp: 20
      },
      {
        url: 'https://other.example/video/1080p/token/chunklist.m3u8',
        detectedFormat: 'm3u8',
        timestamp: 30
      }
    ]`);

    const marked = ctx.markExistingHlsPlaybackFromSegment({
      tabId: 7,
      url: 'https://cdn.example.com/video/1080p/token/part-0042.ts',
    });
    expect(marked.url).toBe('https://cdn.example.com/video/1080p/token/chunklist.m3u8');
    expect(marked.playbackObserved).toBe(true);
    expect(marked.lastSegmentAt).toBe(now);

    const rows = ctx.__eval('currentTabUrls[7]');
    expect(rows[0].playbackObserved).not.toBe(true);
    expect(rows[1].playbackObserved).toBe(true);
    expect(rows[2].playbackObserved).not.toBe(true);
  });

  it('does not treat unrelated files as HLS playback evidence', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    ctx.__eval(`currentTabUrls[7] = [{
      url: 'https://cdn.example.com/video/chunklist.m3u8',
      detectedFormat: 'm3u8'
    }]`);

    expect(ctx.markExistingHlsPlaybackFromSegment({
      tabId: 7,
      url: 'https://cdn.example.com/video/app.js',
    })).toBeNull();
    expect(ctx.markExistingHlsPlaybackFromSegment({
      tabId: 7,
      url: 'https://other.example/video/part-1.ts',
    })).toBeNull();
    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).not.toBe(true);
  });

  it('dedupes inferred HLS manifests by stable playlist key while keeping the latest token', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    const first = ctx.inferHlsManifestFromSegmentUrl('https://cdn.example.com/hls/seg-1-v1-a1.ts?token=one');
    const second = ctx.inferHlsManifestFromSegmentUrl('https://cdn.example.com/hls/seg-2-v1-a1.ts?token=two');
    expect(first.dedupeKey).toBe(second.dedupeKey);

    const details = {
      tabId: 7,
      initiator: 'https://page.example/watch',
      documentUrl: 'https://page.example/watch',
      type: 'media',
      frameId: 0,
      method: 'GET',
    };
    ctx.registerDetectedUrl(
      { ...details, url: first.url },
      { detectedFormat: 'm3u8', playbackObserved: true, dedupeKey: first.dedupeKey },
    );
    ctx.registerDetectedUrl(
      { ...details, url: second.url },
      { detectedFormat: 'm3u8', playbackObserved: true, dedupeKey: second.dedupeKey },
    );

    const rows = ctx.__eval(`
      currentTabUrls[7].map(({ url, dedupeKey, hitCount, playbackObserved }) => ({
        url, dedupeKey, hitCount, playbackObserved
      }))
    `);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      url: second.url,
      dedupeKey: second.dedupeKey,
      hitCount: 2,
      playbackObserved: true,
    });
    expect(ctx.__eval('currentTabUrlKeys[7].size')).toBe(1);
  });

  it('merges direct and inferred detections for the same manifest URL', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });

    const manifestUrl = 'https://cdn.example.com/hls/videos/202402/15/448181161/720P_4000K_448181161.mp4/index-v1-a1.m3u8?token=one';
    const inferred = ctx.inferHlsManifestFromSegmentUrl(
      'https://cdn.example.com/hls/videos/202402/15/448181161/720P_4000K_448181161.mp4/seg-1-v1-a1.ts?token=one',
    );
    expect(inferred.url).toBe(manifestUrl);

    const details = {
      tabId: 7,
      initiator: 'https://page.example/watch',
      documentUrl: 'https://page.example/watch',
      type: 'media',
      frameId: 0,
      method: 'GET',
    };
    ctx.registerDetectedUrl({ ...details, url: manifestUrl }, { detectedFormat: 'm3u8' });
    ctx.registerDetectedUrl(
      { ...details, url: inferred.url },
      { detectedFormat: 'm3u8', playbackObserved: true, dedupeKey: inferred.dedupeKey },
    );

    const rows = ctx.__eval(`
      currentTabUrls[7].map(({ url, dedupeKey, hitCount, playbackObserved }) => ({
        url, dedupeKey, hitCount, playbackObserved
      }))
    `);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      url: manifestUrl,
      dedupeKey: inferred.dedupeKey,
      hitCount: 2,
      playbackObserved: true,
    });
    expect(ctx.__eval('currentTabUrlKeys[7].size')).toBe(2);
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

  it('marks only the freshest recently requested HLS rendition as now playing', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_000_000;
    withFixedNow(ctx, now);
    const tabId = 124;
    ctx.__eval(`currentTabUrls[${tabId}] = ${JSON.stringify([
      {
        url: 'https://cdn.example.com/v/720/chunklist.m3u8',
        timestamp: now - 5_000,
        lastSegmentAt: now - 8_000,
        playbackObserved: true,
      },
      {
        url: 'https://cdn.example.com/v/1080/chunklist.m3u8',
        timestamp: now - 2_000,
        lastSegmentAt: now - 1_000,
        playbackObserved: true,
      },
      {
        url: 'https://cdn.example.com/v/old/chunklist.m3u8',
        timestamp: now - 60_000,
        lastSegmentAt: now - 30_001,
        playbackObserved: true,
      },
    ])};`);

    const sorted = ctx.getSortedUrlsForTab(tabId);
    expect(sorted.find(x => x.url.includes('/1080/')).isNowPlaying).toBe(true);
    expect(sorted.find(x => x.url.includes('/720/')).isNowPlaying).toBe(false);
    expect(sorted.find(x => x.url.includes('/old/')).isNowPlaying).toBe(false);
  });

  it('does not resurrect a stale/ad manifest after the player pauses', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_100_000;
    withFixedNow(ctx, now);
    const tabId = 125;

    ctx.__eval(`currentTabUrls[${tabId}] = ${JSON.stringify([
      {
        url: 'https://cdn.example.com/ad/welcome/playlist.m3u8',
        timestamp: now - 60_000,
        lastSegmentAt: now - 2_000,
        playbackObserved: true,
      },
      {
        url: 'https://cdn.example.com/episode/1080/chunklist.m3u8',
        timestamp: now - 30_000,
        lastSegmentAt: now - 31_000,
        playbackObserved: true,
      },
    ])};`);
    ctx.__eval(`userClickedVideoByTab[${tabId}] = {
      videoSrc: 'blob:https://page.example/player',
      videoIndex: 0,
      timestamp: ${now - 1_000},
      matchedUrl: 'https://cdn.example.com/ad/welcome/playlist.m3u8'
    };`);
    ctx.__eval(`videoPlaybackStateByTab[${tabId}] = {
      isPlaying: false,
      videoIndex: 0,
      frameId: 0,
      timestamp: ${now - 500}
    };`);

    const sorted = ctx.getSortedUrlsForTab(tabId);
    expect(sorted.every(item => item.isNowPlaying === false)).toBe(true);
  });

  it('ignores segments buffered before the latest resume event', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
    });
    const now = 5_200_000;
    const rows = [
      { url: 'https://cdn.example.com/old.m3u8', lastSegmentAt: now - 2_000, isNowPlaying: false },
      { url: 'https://cdn.example.com/new.m3u8', lastSegmentAt: now - 200, isNowPlaying: false },
    ];

    expect(ctx.markFreshSegmentNowPlaying(rows, now, {
      isPlaying: true,
      timestamp: now - 500,
    })).toBe(true);
    expect(rows[0].isNowPlaying).toBe(false);
    expect(rows[1].isNowPlaying).toBe(true);
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

  it('stores deepDetected messages separately from downloadable URLs', () => {
    const chrome = makeChromeStub();
    loadScriptIntoContext('background.js', {
      chrome,
    });
    const listener = chrome.runtime.__onMessageListeners[0];
    expect(listener).toBeDefined();

    const responses = [];
    listener({
      action: 'deepDetected',
      kind: 'manifest-text-no-url',
      format: 'm3u8',
      source: 'atob',
      pageUrl: 'https://page.example/watch',
      timestamp: Date.now(),
    }, { tab: { id: 55 } }, (response) => responses.push(response));

    expect(responses[0]).toEqual({ success: true });

    let detectedResponse = null;
    const keepAlive = listener({
      action: 'getDetectedUrls',
      tabId: 55,
    }, {}, (response) => {
      detectedResponse = response;
    });

    expect(keepAlive).toBe(true);
    expect(detectedResponse.urls).toEqual([]);
    expect(detectedResponse.deepHits).toEqual([
      expect.objectContaining({
        kind: 'manifest-text-no-url',
        format: 'm3u8',
        source: 'atob',
        pageUrl: 'https://page.example/watch',
        hitCount: 1,
      }),
    ]);
  });

  it('does not map a blob video index to the first detected manifest', () => {
    const chrome = makeChromeStub();
    const ctx = loadScriptIntoContext('background.js', { chrome });
    const tabId = 56;
    ctx.__eval(`currentTabUrls[${tabId}] = ${JSON.stringify([
      { url: 'https://cdn.example.com/ad/welcome/playlist.m3u8' },
      { url: 'https://cdn.example.com/episode/1080/chunklist.m3u8' },
    ])};`);

    const listener = chrome.runtime.__onMessageListeners[0];
    listener({
      action: 'videoStartedPlaying',
      videoSrc: 'blob:https://page.example/player',
      videoIndex: 0,
      videoCount: 1,
      pageUrl: 'https://page.example/watch',
      timestamp: 10_000,
    }, { tab: { id: tabId }, frameId: 0 }, () => {});

    expect(ctx.__eval(`userClickedVideoByTab[${tabId}].matchedUrl`)).toBe(null);
    expect(ctx.__eval(`videoPlaybackStateByTab[${tabId}].isPlaying`)).toBe(true);
  });

  it('keeps the tab playing while another video element pauses', () => {
    const chrome = makeChromeStub();
    const ctx = loadScriptIntoContext('background.js', { chrome });
    const listener = chrome.runtime.__onMessageListeners[0];
    const tabId = 57;
    const sender = { tab: { id: tabId }, frameId: 0 };
    const send = (action, videoElementId, timestamp) => listener({
      action,
      videoElementId,
      videoIndex: videoElementId === 'video-a' ? 0 : 1,
      timestamp,
    }, sender, () => {});

    send('videoStartedPlaying', 'video-a', 1_000);
    send('videoStartedPlaying', 'video-b', 2_000);
    send('videoPaused', 'video-b', 3_000);
    expect(ctx.__eval(`videoPlaybackStateByTab[${tabId}].isPlaying`)).toBe(true);
    expect(ctx.__eval(`videoPlaybackStateByTab[${tabId}].videoElementId`)).toBe('video-a');

    send('videoEnded', 'video-a', 4_000);
    expect(ctx.__eval(`videoPlaybackStateByTab[${tabId}].isPlaying`)).toBe(false);
  });
});

describe('extension update check (getUpdateStatus)', () => {
  function makeBackedLocalStorage() {
    const store = {};
    return {
      store,
      api: {
        get: async (keys) => {
          const out = {};
          for (const k of Array.isArray(keys) ? keys : [keys]) {
            if (k in store) out[k] = store[k];
          }
          return out;
        },
        set: async (obj) => { Object.assign(store, obj); },
      },
    };
  }

  function dispatchMessage(chromeStub, msg) {
    return new Promise((resolve) => {
      for (const fn of chromeStub.runtime.__onMessageListeners) {
        fn(msg, {}, resolve);
      }
    });
  }

  function loadWithRelease({ tagName, htmlUrl, failFetch = false }) {
    const chromeStub = makeChromeStub();
    const local = makeBackedLocalStorage();
    chromeStub.storage.local = local.api;
    const fetchCalls = [];
    loadScriptIntoContext('background.js', {
      chrome: chromeStub,
      fetch: async (url) => {
        fetchCalls.push(url);
        if (failFetch) throw new Error('network down');
        return {
          ok: true,
          json: async () => ({ tag_name: tagName, html_url: htmlUrl }),
        };
      },
    });
    return { chromeStub, local, fetchCalls };
  }

  it('reports updateAvailable for a newer release and caches the result', async () => {
    const releaseUrl = 'https://github.com/asdfghj1237890/WebVideo2NAS/releases/tag/v9.9.9';
    const { chromeStub, local, fetchCalls } = loadWithRelease({
      tagName: 'v9.9.9',
      htmlUrl: releaseUrl,
    });

    const resp = await dispatchMessage(chromeStub, { action: 'getUpdateStatus' });
    expect(resp.status).toMatchObject({
      updateAvailable: true,
      latestVersion: '9.9.9',
      currentVersion: '0.0.0',
      releaseUrl,
      lastResult: 'ok',
    });
    expect(fetchCalls).toHaveLength(1);
    expect(fetchCalls[0]).toContain('api.github.com');
    expect(local.store.extUpdateCheck).toMatchObject({
      latestVersion: '9.9.9',
      lastResult: 'ok',
    });

    // Second ask inside the throttle window: served from storage, no refetch.
    const resp2 = await dispatchMessage(chromeStub, { action: 'getUpdateStatus' });
    expect(resp2.status.updateAvailable).toBe(true);
    expect(fetchCalls).toHaveLength(1);
  });

  it('does not flag an update when the release is not newer', async () => {
    const { chromeStub } = loadWithRelease({
      tagName: 'v0.0.0',
      htmlUrl: 'https://github.com/asdfghj1237890/WebVideo2NAS/releases/tag/v0.0.0',
    });

    const resp = await dispatchMessage(chromeStub, { action: 'getUpdateStatus' });
    expect(resp.status.updateAvailable).toBe(false);
    expect(resp.status.latestVersion).toBe('0.0.0');
  });

  it('records a failed check without flagging an update, and throttles retries', async () => {
    const { chromeStub, local, fetchCalls } = loadWithRelease({ failFetch: true });

    const resp = await dispatchMessage(chromeStub, { action: 'getUpdateStatus' });
    expect(resp.status.updateAvailable).toBe(false);
    expect(resp.status.latestVersion).toBeUndefined();
    expect(resp.status.lastResult).toBe('error');
    expect(local.store.extUpdateCheck.lastResult).toBe('error');

    // Failed checks retry on the 1h interval, not immediately.
    await dispatchMessage(chromeStub, { action: 'getUpdateStatus' });
    expect(fetchCalls).toHaveLength(1);
  });
});
