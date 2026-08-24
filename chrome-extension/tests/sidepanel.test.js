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

function loadLocalizedSidepanel(lang = 'en', overrides = {}) {
  const window = {};
  const document = overrides.document || makeDocumentStub();
  document.documentElement ||= {};
  const ctx = loadScriptIntoContext('i18n.js', {
    chrome: overrides.chrome || makeChromeStub(),
    document,
    navigator: overrides.navigator || {
      language: lang,
      clipboard: { writeText: async () => {} },
    },
    fetch: overrides.fetch || (async () => ({ ok: true, json: async () => [] })),
    window,
  });
  window.WV2N_I18N.setLanguage(lang);
  ctx.importScripts('sidepanel.js');
  return ctx;
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

  it('formats compact browser CDN/NAS transfer metrics with detailed timing', () => {
    const ctx = loadLocalizedSidepanel('en');

    const meta = ctx.browserTransferMeta({
      browserConcurrency: 12,
      browserTransferTimings: {
        cdn: {
          bytes: 8 * 1024 * 1024,
          attemptedBytes: 10 * 1024 * 1024,
          requests: 3,
          failures: 2,
          inFlight: 1,
          requestMs: 4000,
          activeMs: 2000,
          mbPerSecond: 4,
        },
        nas: {
          bytes: 8 * 1024 * 1024,
          attemptedBytes: 8 * 1024 * 1024,
          requests: 1,
          failures: 0,
          inFlight: 0,
          requestMs: 200,
          activeMs: 100,
          mbPerSecond: 80,
        },
      },
    });

    expect(meta.label).toBe('CDN 4.00 MB/s · 2.0s | NAS 80.0 MB/s · 0.1s');
    expect(meta.title).toContain('CDN: 8.0 MB successful / 10.0 MB attempted');
    expect(meta.title).toContain('requests 3 · failures 2 · in flight 1');
    expect(meta.title).toContain('NAS: 8.0 MB successful / 8.0 MB attempted');
    expect(meta.title).toContain('Concurrency: 12');
    expect(meta.title).toContain('Eligible failures are retried automatically.');

    const failedBeforeSuccess = ctx.browserTransferMeta({
      browserTransferTimings: {
        cdn: { bytes: 0, attemptedBytes: 1024, requests: 1, failures: 1, inFlight: 0 },
        nas: { bytes: 0, attemptedBytes: 0, requests: 0, failures: 0, inFlight: 0 },
      },
    });
    expect(failedBeforeSuccess).not.toBeNull();
    expect(failedBeforeSuccess.title).toContain('failures 1');
  });

  it('localizes transfer metrics and browser-mode tooltip in zh-TW', () => {
    const ctx = loadLocalizedSidepanel('zh-TW');
    const job = {
      id: 'job-1',
      mode: 'browser',
      title: 'Example',
      status: 'browser_uploading',
      progress: 50,
      browserConcurrency: 12,
      browserTransferTimings: {
        cdn: { bytes: 1024 * 1024, attemptedBytes: 2 * 1024 * 1024, requests: 2, failures: 1, inFlight: 1, requestMs: 2000, activeMs: 1000, mbPerSecond: 1 },
        nas: { bytes: 1024 * 1024, attemptedBytes: 1024 * 1024, requests: 1, failures: 0, inFlight: 0, requestMs: 1000, activeMs: 500, mbPerSecond: 2 },
      },
    };

    const meta = ctx.browserTransferMeta(job);
    const html = ctx.getJobInnerHtml(job);
    expect(meta.title).toContain('來源 CDN：成功 1.0 MB／嘗試 2.0 MB');
    expect(meta.title).toContain('請求 2 · 失敗 1 · 進行中 1');
    expect(meta.title).toContain('並行數：12');
    expect(meta.title).toContain('符合條件的失敗會自動重試。');
    expect(ctx.window.WV2N_I18N.t('job.browserMode.title')).toBe('由瀏覽器工作階段下載');
    expect(html).toContain('<details class="job-transfer"');
    expect(html).toContain('<summary data-transfer-label');
    expect(html).toContain('data-transfer-detail');
    expect(html.indexOf('class="job-transfer"')).toBeGreaterThan(html.indexOf('class="job-meta'));
  });

  it('monotonically merges out-of-order browser progress snapshots', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });
    const stage = (bytes, requests, inFlight) => ({
      bytes,
      attemptedBytes: bytes,
      requests,
      failures: 0,
      inFlight,
      requestMs: requests * 100,
      activeMs: requests * 50,
      mbPerSecond: 1,
    });
    const newer = ctx.mergeLiveBrowserProgress(null, {
      done: 7,
      total: 10,
      percent: 70,
      concurrency: 12,
      transferTimings: { cdn: stage(700, 7, 2), nas: stage(600, 6, 1) },
    });
    const merged = ctx.mergeLiveBrowserProgress(newer, {
      done: 3,
      total: 10,
      percent: 30,
      concurrency: 6,
      transferTimings: { cdn: stage(300, 3, 6), nas: stage(200, 2, 5) },
    });

    expect(merged.done).toBe(7);
    expect(merged.percent).toBe(70);
    expect(merged.concurrency).toBe(12);
    expect(merged.transferTimings.cdn.bytes).toBe(700);
    expect(merged.transferTimings.cdn.inFlight).toBe(2);
  });

  it('does not let a late live event overwrite an API terminal job', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });
    ctx.__eval(`jobs = [{ id: 'j1', title: 'Job', status: 'failed', progress: 0 }];`);

    ctx.handleBrowserJobProgress({
      jobId: 'j1',
      done: 8,
      total: 10,
      concurrency: 12,
      transferTimings: {
        cdn: { bytes: 8, attemptedBytes: 9, requests: 9, failures: 1, inFlight: 0 },
        nas: { bytes: 8, attemptedBytes: 8, requests: 8, failures: 0, inFlight: 0 },
      },
    });

    expect(ctx.__eval('jobs[0].status')).toBe('failed');
    expect(ctx.__eval('jobs[0].progress')).toBe(0);
    expect(ctx.__eval('jobs[0].browserTransferTimings.cdn.failures')).toBe(1);
    expect(ctx.__eval('liveBrowserProgress.get("j1").terminal')).toBe(true);
  });

  it('monotonically overlays session progress through browser phases and keeps terminal metrics', async () => {
    let apiJobs = [{ id: 'j1', title: 'Job', mode: 'browser', status: 'browser_pending', progress: 0 }];
    let snapshot = {
      done: 4,
      total: 10,
      percent: 40,
      concurrency: 12,
      ts: Date.now(),
      terminal: false,
      transferTimings: {
        cdn: { bytes: 4 * 1024 * 1024, attemptedBytes: 5 * 1024 * 1024, requests: 5, failures: 1, inFlight: 2, requestMs: 5000, activeMs: 3000, mbPerSecond: 4 / 3 },
        nas: { bytes: 4 * 1024 * 1024, attemptedBytes: 4 * 1024 * 1024, requests: 4, failures: 0, inFlight: 1, requestMs: 1000, activeMs: 800, mbPerSecond: 5 },
      },
    };
    const chrome = makeChromeStub();
    chrome.storage.session = {
      get: async () => ({ wv2nasBrowserTransferSnapshots: { j1: snapshot } }),
    };
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome,
      document: makeDocumentStub(),
      window: {},
      fetch: async () => ({ ok: true, json: async () => apiJobs }),
    });
    ctx.__eval(`settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };`);

    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('browser_uploading');
    expect(ctx.__eval('jobs[0].progress')).toBe(40);

    apiJobs = [{ id: 'j1', title: 'Job', mode: 'browser', status: 'browser_finalizing', progress: 0 }];
    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('browser_finalizing');
    expect(ctx.__eval('jobs[0].progress')).toBe(40);

    apiJobs = [{ id: 'j1', title: 'Job', mode: 'browser', status: 'pending', progress: 0 }];
    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('pending');
    expect(ctx.__eval('jobs[0].progress')).toBe(40);

    snapshot = { ...snapshot, done: 10, percent: 100, terminal: true, ts: Date.now() + 1 };
    apiJobs = [{ id: 'j1', title: 'Job', mode: 'browser', status: 'completed', progress: 100 }];
    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('completed');
    expect(ctx.__eval('jobs[0].progress')).toBe(100);
    expect(ctx.__eval('jobs[0].browserConcurrency')).toBe(12);
    expect(ctx.__eval('jobs[0].browserTransferTimings.cdn.requests')).toBe(5);
    expect(ctx.__eval('liveBrowserProgress.get("j1").terminal')).toBe(true);

    // A newly opened sidepanel has no in-memory progress; terminal session
    // snapshots must still decorate the completed API row with diagnostics.
    const reopened = loadScriptIntoContext('sidepanel.js', {
      chrome,
      document: makeDocumentStub(),
      window: {},
      fetch: async () => ({ ok: true, json: async () => apiJobs }),
    });
    reopened.__eval(`settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };`);
    await reopened.loadRecentJobs();
    expect(reopened.__eval('jobs[0].status')).toBe('completed');
    expect(reopened.__eval('jobs[0].browserTransferTimings.cdn.requests')).toBe(5);
  });

  it('renders a persisted terminal browser failure over a lagging upload API row', async () => {
    let apiStatus = 'browser_uploading';
    const chrome = makeChromeStub();
    chrome.storage.session = {
      get: async () => ({
        wv2nasBrowserTransferSnapshots: {
          j1: {
            done: 2,
            total: 8,
            percent: 25,
            concurrency: 6,
            terminal: true,
            failed: true,
            ts: Date.now(),
            transferTimings: {
              cdn: { bytes: 2, attemptedBytes: 3, requests: 3, failures: 1, inFlight: 0 },
              nas: { bytes: 2, attemptedBytes: 2, requests: 2, failures: 0, inFlight: 0 },
            },
          },
        },
      }),
    };
    const ctx = loadLocalizedSidepanel('en', {
      chrome,
      // Use jsdom here because this regression also verifies the dynamic
      // status/error strings emitted through sidepanelCore.escapeHtml().
      document,
      fetch: async () => ({
        ok: true,
        json: async () => [{
          id: 'j1', title: 'Job', mode: 'browser', status: apiStatus, progress: 0,
        }],
      }),
    });
    ctx.__eval(`settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };`);

    await ctx.loadRecentJobs();

    expect(ctx.__eval('jobs[0].status')).toBe('failed');
    expect(ctx.__eval('jobs[0].progress')).toBe(25);
    expect(ctx.__eval('liveBrowserProgress.get("j1").failed')).toBe(true);
    const failedHtml = ctx.getJobInnerHtml(ctx.__eval('jobs[0]'));
    expect(failedHtml).toContain('status-failed');
    expect(failedHtml).toContain('No error details available');
    expect(failedHtml).not.toContain('data-job-bar');

    // Once the API confirms finalize/queue progress, it is authoritative;
    // the browser failure snapshot only corrects stale browser_* phases.
    apiStatus = 'pending';
    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('pending');
  });

  it('restores terminal transfer diagnostics beyond the five-minute live-progress TTL', async () => {
    const chrome = makeChromeStub();
    chrome.storage.session = {
      get: async () => ({
        wv2nasBrowserTransferSnapshots: {
          j1: {
            done: 10, total: 10, percent: 100, concurrency: 4,
            terminal: true, ts: Date.now() - 10 * 60 * 1000,
            transferTimings: {
              cdn: { bytes: 100, attemptedBytes: 100, requests: 10, failures: 0, inFlight: 0 },
              nas: { bytes: 100, attemptedBytes: 110, requests: 11, failures: 1, inFlight: 0 },
            },
          },
        },
      }),
    };
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome,
      document: makeDocumentStub(),
      window: {},
      fetch: async () => ({
        ok: true,
        json: async () => [{ id: 'j1', title: 'Job', status: 'completed', progress: 100 }],
      }),
    });
    ctx.__eval(`settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };`);

    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].browserConcurrency')).toBe(4);
    expect(ctx.__eval('jobs[0].browserTransferTimings.nas.failures')).toBe(1);
  });

  it('keeps a live browser row through a transient empty API poll', async () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
      fetch: async () => ({ ok: true, json: async () => [] }),
    });
    ctx.__eval(`
      settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };
      jobs = [{ id: 'j1', title: 'Job', status: 'browser_uploading', progress: 30 }];
      liveBrowserProgress.set('j1', { done: 3, total: 10, percent: 30, updatedAt: Date.now() });
    `);

    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs.length')).toBe(1);
    expect(ctx.__eval('jobs[0].status')).toBe('browser_uploading');
    expect(ctx.__eval('liveBrowserProgress.has("j1")')).toBe(true);
  });

  it('does not let API polling renew an expired nonterminal overlay', async () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
      fetch: async () => ({
        ok: true,
        json: async () => [{ id: 'j1', title: 'Job', status: 'browser_pending', progress: 0 }],
      }),
    });
    ctx.__eval(`
      settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };
      liveBrowserProgress.set('j1', {
        done: 8, total: 10, percent: 80,
        updatedAt: Date.now() - 10 * 60 * 1000,
      });
    `);

    await ctx.loadRecentJobs();
    expect(ctx.__eval('jobs[0].status')).toBe('browser_pending');
    expect(ctx.__eval('jobs[0].progress')).toBe(0);
    expect(ctx.__eval('liveBrowserProgress.has("j1")')).toBe(false);
  });

  it('discards an older overlapping jobs poll response', async () => {
    const resolvers = [];
    const fetch = () => new Promise((resolve) => resolvers.push(resolve));
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
      fetch,
    });
    ctx.__eval(`settings = { nasEndpoint: 'http://nas.local', apiKey: 'key' };`);

    const older = ctx.loadRecentJobs();
    await Promise.resolve();
    const newer = ctx.loadRecentJobs();
    await Promise.resolve();
    resolvers[1]({ ok: true, json: async () => [{ id: 'j1', title: 'Job', status: 'browser_finalizing', progress: 100 }] });
    await newer;
    resolvers[0]({ ok: true, json: async () => [{ id: 'j1', title: 'Job', status: 'browser_pending', progress: 0 }] });
    await older;

    expect(ctx.__eval('jobs[0].status')).toBe('browser_finalizing');
    expect(ctx.__eval('jobs[0].progress')).toBe(100);
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

  it('never play-first-locks complete manifest-less JSON DASH rows', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    ctx.__eval('settings = { useBrowserSide: true };');
    const item = {
      url: 'https://cdn.example.com/video-1080.m4s?token=fresh',
      detectedFormat: 'mpd',
      isNowPlaying: false,
      playbackObserved: false,
      directDash: {
        video: { url: 'https://cdn.example.com/video-1080.m4s?token=fresh' },
        audio: { url: 'https://cdn.example.com/audio.m4s?token=fresh' },
      },
    };

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

  it('shows quality filters below the bulk-mode threshold when qualities are mixed', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    expect(ctx.shouldShowDetectedToolbar([
      { url: 'https://cdn.example.com/video/1080p/playlist.m3u8' },
      { url: 'https://cdn.example.com/video/720p/playlist.m3u8' },
    ])).toBe(true);

    expect(ctx.shouldShowDetectedToolbar([
      { url: 'https://cdn.example.com/video/1080p/a.m3u8' },
      { url: 'https://cdn.example.com/video/1080p/b.m3u8' },
    ])).toBe(false);

    expect(ctx.shouldShowDetectedToolbar([
      { url: 'https://cdn.example.com/video/1080p/a.m3u8' },
    ], '1080')).toBe(true);

    // Manifest-less DASH track filenames often contain only representation
    // IDs, so filtering must use the structural height captured from JSON.
    expect(ctx.shouldShowDetectedToolbar([
      { url: 'https://cdn.example.com/track-100026.m4s', qualityHeight: 1080 },
      { url: 'https://cdn.example.com/track-100023.m4s', qualityHeight: 720 },
    ])).toBe(true);
  });

  it('shows immediate send feedback and surfaces background submission errors', async () => {
    const events = [];
    const chrome = makeChromeStub();
    chrome.storage.sync.get = async () => ({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
    });
    chrome.runtime.sendMessage = async () => {
      events.push('message');
      return {
        success: false,
        error: 'The NAS API does not support JSON DASH yet.',
      };
    };
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome,
      document: makeDocumentStub(),
      navigator: { clipboard: { writeText: async () => {} } },
      fetch: async () => ({ ok: true, json: async () => ({}) }),
      window: {},
      testEvents: events,
    });
    ctx.__eval(`
      activeTabId = 1;
      showToast = (message) => testEvents.push('toast:' + message);
    `);

    const url = 'https://cdn.example.com/video.m4s';
    ctx.__eval(`selected.add(${JSON.stringify(url)});`);
    const result = await ctx.flyToNAS(null, url, 'https://example.com/watch');

    expect(result.success).toBe(false);
    expect(events[0]).toBe('toast:toast.sending');
    expect(events[1]).toBe('message');
    expect(events[2]).toContain('toast:toast.failedToSend: The NAS API does not support JSON DASH yet.');
    expect(ctx.__eval(`sentUrls.has(${JSON.stringify(url)})`)).toBe(false);
    expect(ctx.__eval(`selected.has(${JSON.stringify(url)})`)).toBe(true);
  });

  it('updates the progress bar colour when browser upload leaves pending', () => {
    const ctx = loadScriptIntoContext('sidepanel.js', {
      chrome: makeChromeStub(),
      document: makeDocumentStub(),
      window: {},
    });

    const statusText = { textContent: '' };
    const meta = {
      className: '',
      querySelector: (selector) => (
        selector === '[data-status-text]' ? statusText : null
      ),
    };
    const arc = {
      attrs: {},
      setAttribute(name, value) { this.attrs[name] = value; },
    };
    const fill = { style: { background: 'var(--warn)' } };
    const el = {
      querySelector: (selector) => {
        if (selector === '.job-meta') return meta;
        if (selector === '[data-ring-arc]') return arc;
        if (selector === '[data-bar-fill]') return fill;
        return null;
      },
    };

    ctx.updateJobElement(el, {
      id: 1,
      title: 'Example',
      status: 'browser_uploading',
      progress: 42,
    });

    expect(arc.attrs.stroke).toBe('var(--accent)');
    expect(fill.style.background).toBe('var(--accent)');
  });
});
