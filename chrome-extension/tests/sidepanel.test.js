import { describe, expect, it } from 'vitest';

import { loadScriptIntoContext } from './helpers/load-script.js';

function makeChromeStub() {
  return {
    storage: {
      sync: { get: async () => ({}) },
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
  };
}

function makeDocumentStub() {
  return {
    addEventListener: () => {},
    getElementById: () => null,
    createElement: () => ({
      textContent: '',
      innerHTML: '',
    }),
    body: { appendChild: () => {} },
  };
}

describe('sidepanel.js helper functions', () => {
  it('extractQualitiesFromUrl finds and sorts unique qualities', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      navigator: { clipboard: { writeText: async () => {} } },
      fetch: async () => ({ ok: true, json: async () => ({}) }),
      window: {},
    });

    const q = ctx.extractQualitiesFromUrl('https://a/b/video_720p_1080p.mp4?quality=720&res=2160');
    expect(q).toEqual(['2160p', '1080p', '720p']);
    expect(ctx.getMaxQualityNumber('https://a/b/480p/playlist.m3u8')).toBe(480);
  });

  it('formatDuration outputs mm:ss or hh:mm:ss', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    expect(ctx.formatDuration(59)).toBe('00:59');
    expect(ctx.formatDuration(61)).toBe('01:01');
    expect(ctx.formatDuration(3600)).toBe('01:00:00');
  });

  it('containsIpAddress detects ip= query parameter', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    expect(ctx.containsIpAddress('https://a/b/c?ip=114.24.18.78')).toBe(true);
    expect(ctx.containsIpAddress('https://a/b/114.24.18.78/video.mp4')).toBe(false);
  });

  it('connectionReasonFromError maps AbortError to timeout key when i18n is missing', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    const err = new Error('x');
    err.name = 'AbortError';
    expect(ctx.connectionReasonFromError(err)).toBe('error.timeout.type');
  });

  it('deriveTrustedCdnSuffix uses the full host instead of guessing eTLD+1', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    expect(ctx.deriveTrustedCdnSuffix('https://cdn.example.co.uk/video/master.m3u8'))
      .toBe('cdn.example.co.uk');
    expect(ctx.deriveTrustedCdnSuffix('media.example.net:443/path/master.m3u8'))
      .toBe('media.example.net');
    expect(ctx.deriveTrustedCdnSuffix('https://127.0.0.1/x.m3u8')).toBeNull();
    expect(ctx.deriveTrustedCdnSuffix('localhost')).toBeNull();
  });

  it('keeps play-first-gated HLS candidates visible but locked', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    ctx.__eval(`
      settings = { useBrowserSide: true };
      qualityFilter = 'all';
      searchQuery = '';
      detectedUrls = [{
        url: 'https://cdn.example.com/video/master.m3u8',
        detectedFormat: 'm3u8',
        isNowPlaying: false,
        timestamp: 1000,
      }];
    `);

    const [item] = ctx.visibleDetectedUrls();
    expect(item.url).toBe('https://cdn.example.com/video/master.m3u8');
    expect(ctx.urlInfoRequiresPlayFirst(item)).toBe(true);

    item.playbackObserved = true;
    expect(ctx.urlInfoRequiresPlayFirst(item)).toBe(false);
  });

  it('resets stale quality filters so detected URLs do not disappear behind hidden toolbar state', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    ctx.__eval(`
      qualityFilter = '720p';
      searchQuery = '';
      detectedUrls = [{
        url: 'https://hls.example.com/videos/hash/hash.m3u8?auth_key=abc&v=3&time=0',
        detectedFormat: 'm3u8',
        timestamp: 1000,
      }];
    `);

    const [item] = ctx.visibleDetectedUrls();
    expect(item.url).toBe('https://hls.example.com/videos/hash/hash.m3u8?auth_key=abc&v=3&time=0');
    expect(ctx.__eval('qualityFilter')).toBe('all');
  });
});
