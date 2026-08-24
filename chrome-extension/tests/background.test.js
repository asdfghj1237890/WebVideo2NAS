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

  it('registers paired JSON DASH tracks as one MPD tile and refreshes signed URLs by stable key', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const base = {
      pageUrl: 'https://page.example/watch',
      duration: 120,
      video: {
        url: 'https://cdn.example.com/video-1080.m4s?token=old',
        mimeType: 'video/mp4',
        codecs: 'avc1.640028',
        width: 1920,
        height: 1080,
      },
      audio: {
        url: 'https://cdn.example.com/audio.m4s?token=old',
        mimeType: 'audio/mp4',
        codecs: 'mp4a.40.2',
      },
    };
    expect(ctx.registerDirectDashDetection(7, base)).toBe(true);
    expect(ctx.registerDirectDashDetection(7, {
      ...base,
      video: { ...base.video, url: 'https://cdn.example.com/video-1080.m4s?token=fresh' },
      audio: { ...base.audio, url: 'https://cdn.example.com/audio.m4s?token=fresh' },
    })).toBe(true);

    const rows = ctx.__eval('currentTabUrls[7]');
    expect(rows).toHaveLength(1);
    expect(rows[0].detectedFormat).toBe('mpd');
    expect(rows[0].qualityHeight).toBe(1080);
    expect(rows[0].url).toContain('token=fresh');
    expect(rows[0].directDash.audio.url).toContain('token=fresh');
  });

  it('scopes detected DASH metadata and titles to the source tab', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const sharedUrl = 'https://cdn.example.com/video.m4s?shared=1';
    ctx.__eval(`
      currentTabUrls[7] = [{
        url: ${JSON.stringify(sharedUrl)},
        pageTitle: 'Tab Seven',
        detectedFormat: 'mpd',
        directDash: { audio: { url: 'https://cdn.example.com/audio-a.m4s' } },
      }];
      currentTabUrls[8] = [{
        url: ${JSON.stringify(sharedUrl)},
        pageTitle: 'Tab Eight',
        detectedFormat: 'mpd',
        directDash: { audio: { url: 'https://cdn.example.com/audio-b.m4s' } },
      }];
    `);

    expect(ctx.getDetectedUrlInfo(sharedUrl, 7).directDash.audio.url).toContain('audio-a');
    expect(ctx.getDetectedUrlInfo(sharedUrl, 8).directDash.audio.url).toContain('audio-b');
    expect(ctx.getDetectedUrlInfo(sharedUrl, 9)).toBe(null);
    expect(ctx.getStoredPageTitle(sharedUrl, 7)).toBe('Tab Seven');
    expect(ctx.getStoredPageTitle(sharedUrl, 8)).toBe('Tab Eight');
  });

  it('does not use shared DASH audio to select every quality, including request-before-detection races', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const audio = { url: 'https://cdn.example.com/audio.m4s?sig=1', mimeType: 'audio/mp4' };
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://cdn.example.com/audio.m4s?sig=played-before-detection',
    });
    for (const height of [1080, 720]) {
      ctx.registerDirectDashDetection(7, {
        pageUrl: 'https://page.example/watch',
        video: { url: `https://cdn.example.com/video-${height}.m4s?sig=1`, mimeType: 'video/mp4', height },
        audio,
      });
    }

    const matches = ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/audio.m4s?sig=refreshed',
    });
    expect(matches).toHaveLength(0);
    expect(ctx.__eval('currentTabUrls[7].every((row) => !row.playbackObserved)')).toBe(true);

    const videoMatches = ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/video-720.m4s?sig=played',
    });
    expect(videoMatches).toHaveLength(1);
    expect(videoMatches[0].qualityHeight).toBe(720);
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 1080).playbackObserved')).not.toBe(true);
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 720).playbackObserved')).toBe(true);
  });

  it('uses DASH audio as weak playback evidence when the tab has one video candidate', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video-1080.m4s?sig=1',
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio: {
        url: 'https://cdn.example.com/audio.m4s?sig=1',
        mimeType: 'audio/mp4',
      },
    });

    const matches = ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/audio.m4s?sig=played',
    });
    expect(matches).toHaveLength(1);
    expect(matches[0].qualityHeight).toBe(1080);
    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).toBe(true);
  });

  it('backfills weak audio evidence when audio starts before the sole JSON candidate', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://cdn.example.com/audio.m4s?sig=played-before-json',
    });
    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video.m4s?qn=1080&sig=fresh',
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio: {
        url: 'https://cdn.example.com/audio.m4s?sig=fresh',
        mimeType: 'audio/mp4',
      },
    });

    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).toBe(true);
    expect(ctx.__eval('currentTabUrls[7][0].directDashPlaybackEvidence')).toBe('audio');
  });

  it('uses the exact query to distinguish qualities sharing one m4s path', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const audio = { url: 'https://cdn.example.com/audio.m4s?sig=1', mimeType: 'audio/mp4' };
    for (const height of [1080, 720]) {
      ctx.registerDirectDashDetection(7, {
        pageUrl: 'https://page.example/watch',
        video: {
          url: `https://cdn.example.com/video.m4s?quality=${height}&sig=shared`,
          mimeType: 'video/mp4',
          height,
        },
        audio,
      });
    }

    const matches = ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/video.m4s?quality=720&sig=shared',
    });
    expect(matches).toHaveLength(1);
    expect(matches[0].qualityHeight).toBe(720);
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 1080).playbackObserved')).not.toBe(true);
  });

  it('revokes provisional path evidence when a later JSON candidate owns the same path', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://cdn.example.com/video.m4s?quality=720&sig=shared',
    });
    const audio = { url: 'https://cdn.example.com/audio.m4s?sig=1', mimeType: 'audio/mp4' };

    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video.m4s?quality=1080&sig=shared',
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio,
    });
    expect(ctx.__eval('currentTabUrls[7][0].directDashPlaybackEvidence')).toBe('video-path');

    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video.m4s?quality=720&sig=shared',
        mimeType: 'video/mp4',
        height: 720,
      },
      audio,
    });

    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 720).playbackObserved')).toBe(true);
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 720).directDashPlaybackEvidence')).toBe('video');
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 1080).playbackObserved')).not.toBe(true);
  });

  it('revokes weak DASH audio evidence if another quality registers later', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const audio = {
      url: 'https://cdn.example.com/audio.m4s?sig=1',
      mimeType: 'audio/mp4',
    };
    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video-1080.m4s?sig=1',
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio,
    });
    ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/audio.m4s?sig=played',
    });
    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).toBe(true);

    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video-720.m4s?sig=1',
        mimeType: 'video/mp4',
        height: 720,
      },
      audio,
    });

    expect(ctx.__eval('currentTabUrls[7].every((row) => !row.playbackObserved)')).toBe(true);
    expect(ctx.__eval('currentTabUrls[7].every((row) => !row.directDashPlaybackEvidence)')).toBe(true);

    ctx.markExistingDirectDashPlaybackFromTrack({
      tabId: 7,
      url: 'https://cdn.example.com/video-1080.m4s?sig=played',
    });
    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video-480.m4s?sig=1',
        mimeType: 'video/mp4',
        height: 480,
      },
      audio,
    });
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 1080).playbackObserved')).toBe(true);
    expect(ctx.__eval('currentTabUrls[7].find((row) => row.qualityHeight === 1080).directDashPlaybackEvidence')).toBe('video-path');
  });

  it('backfills DASH playback when the first m4s request arrives before JSON detection', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const now = 5_000_000;
    withFixedNow(ctx, now);

    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://backup.example.com/video-1080.m4s?token=played',
    });
    ctx.registerDirectDashDetection(7, {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://primary.example.com/video-1080.m4s?token=fresh',
        backupUrls: ['https://backup.example.com/video-1080.m4s?token=fresh'],
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio: {
        url: 'https://primary.example.com/audio.m4s?token=fresh',
        mimeType: 'audio/mp4',
      },
    });

    const [row] = ctx.__eval('currentTabUrls[7]');
    expect(row.playbackObserved).toBe(true);
    expect(row.lastSegmentAt).toBe(now);
  });

  it('does not backfill DASH playback across tabs, representations, or expired requests', () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({ ok: true, json: async () => ({}) }),
    });
    const base = {
      pageUrl: 'https://page.example/watch',
      video: {
        url: 'https://cdn.example.com/video-1080.m4s?token=fresh',
        mimeType: 'video/mp4',
        height: 1080,
      },
      audio: {
        url: 'https://cdn.example.com/audio.m4s?token=fresh',
        mimeType: 'audio/mp4',
      },
    };

    withFixedNow(ctx, 1_000_000);
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 8,
      url: 'https://cdn.example.com/video-1080.m4s?token=other-tab',
    });
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://cdn.example.com/video-720.m4s?token=other-quality',
    });
    ctx.registerDirectDashDetection(7, base);
    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).toBe(false);

    ctx.__eval('currentTabUrls[7] = []; currentTabUrlKeys[7] = new Set();');
    ctx.rememberRecentDirectDashTrackRequest({
      tabId: 7,
      url: 'https://cdn.example.com/video-1080.m4s?token=expired',
    });
    withFixedNow(ctx, 1_120_001);
    ctx.registerDirectDashDetection(7, base);
    expect(ctx.__eval('currentTabUrls[7][0].playbackObserved')).toBe(false);
  });

  it('probes complete DASH track length with a one-byte Range request', async () => {
    let requestInit = null;
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async (_url, init) => {
        requestInit = init;
        return {
          status: 206,
          headers: {
            get(name) {
              if (String(name).toLowerCase() === 'content-range') return 'bytes 0-0/12345678';
              if (String(name).toLowerCase() === 'content-length') return '1';
              return null;
            },
          },
          arrayBuffer: async () => new Uint8Array([0]).buffer,
        };
      },
    });

    await expect(ctx._wv2nasProbeDirectDashLength(
      { url: 'https://cdn.example.com/video.m4s' },
      { Authorization: 'Bearer token', Cookie: 'secret' },
    )).resolves.toBe(12345678);
    expect(requestInit.headers.Range).toBe('bytes=0-0');
    expect(requestInit.headers.Authorization).toBe('Bearer token');
    expect(requestInit.headers.Cookie).toBeUndefined();
    expect(requestInit.redirect).toBe('error');
  });

  it('rejects a DASH length probe that omits Content-Range', async () => {
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => ({
        status: 206,
        headers: {
          get(name) {
            return String(name).toLowerCase() === 'content-length' ? '1' : null;
          },
        },
        arrayBuffer: async () => new Uint8Array([0]).buffer,
      }),
    });

    await expect(ctx._wv2nasProbeDirectDashLength({
      url: 'https://cdn.example.com/video.m4s',
      contentLength: 12345678,
    }, {})).rejects.toThrow(/Content-Range/);
  });

  it('never treats a complete JSON DASH m4s track as MPD text during metadata probing', async () => {
    let fetchCalls = 0;
    const ctx = loadScriptIntoContext('background.js', {
      chrome: makeChromeStub(),
      fetch: async () => {
        fetchCalls += 1;
        throw new Error('binary track must not be fetched by probeMpd');
      },
    });
    await ctx.probeVideoMeta({
      url: 'https://cdn.example.com/video.m4s',
      detectedFormat: 'mpd',
      directDash: { video: {}, audio: {} },
      duration: 90,
    }, 7);
    expect(fetchCalls).toBe(0);
    expect(ctx.__eval("videoMetaByUrl['https://cdn.example.com/video.m4s'].duration")).toBe(90);
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
