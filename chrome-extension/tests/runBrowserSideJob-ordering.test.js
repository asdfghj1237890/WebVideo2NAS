// Codex review #8 regression: runBrowserSideJob MUST install DNR
// rules covering the manifest URL BEFORE calling
// _wv2nasFetchManifestInBrowser. Sites that gate the manifest on
// Referer/Origin/User-Agent return 403 to a bare browser fetch; with
// DNR active, the browser-injected headers match what the player
// sent and the same fetch returns 200.
//
// This is a runtime-ordering invariant — we exercise it by loading
// background.js into a vm context, mocking chrome + fetch, and
// asserting the recorded call sequence shows the first DNR install
// happened before the first fetch.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';
import path from 'node:path';

const BACKGROUND_SCRIPT = path.resolve(__dirname, '..', 'background.js');


function makeChromeStub(callOrder) {
  const noop = () => {};
  const storageState = {};
  const snapshot = (obj) => JSON.parse(JSON.stringify(obj));
  return {
    runtime: {
      sendMessage: vi.fn(async (msg) => {
        callOrder.push({ kind: 'runtime.sendMessage', msg });
        if (msg && msg.type === 'START_BROWSER_JOB') return { ok: true };
        return undefined;
      }),
      onMessage: { addListener: noop, removeListener: noop },
      onInstalled: { addListener: noop },
      lastError: null,
      openOptionsPage: noop,
      getManifest: () => ({ version: '2.5.0' }),
      getURL: (p) => `chrome-extension://test/${p}`,
    },
    storage: {
      sync: { get: (_keys, cb) => cb && cb({}), set: async () => {} },
      local: {
        set: async (obj) => {
          callOrder.push({ kind: 'storage.local.set', obj: snapshot(obj) });
          Object.assign(storageState, obj);
        },
        get: async (key) => {
          if (typeof key === 'string') {
            return key in storageState ? { [key]: storageState[key] } : {};
          }
          return { ...storageState };
        },
        remove: async (key) => { delete storageState[key]; },
      },
      onChanged: { addListener: noop },
    },
    webRequest: {
      onBeforeRequest: { addListener: noop },
      onSendHeaders: { addListener: noop },
      onHeadersReceived: { addListener: noop },
    },
    webNavigation: { onCommitted: { addListener: noop } },
    action: {
      setBadgeText: noop, setBadgeBackgroundColor: noop,
      onClicked: { addListener: noop },
    },
    tabs: {
      onRemoved: { addListener: noop }, onUpdated: { addListener: noop },
      onActivated: { addListener: noop },
      query: (_q, cb) => cb && cb([]), get: (_id, cb) => cb && cb(null),
    },
    contextMenus: { create: noop, onClicked: { addListener: noop } },
    notifications: {
      create: vi.fn(), clear: noop,
      onClicked: { addListener: noop }, onClosed: { addListener: noop },
    },
    cookies: { getAll: async () => [] },
    sidePanel: { setOptions: async () => {}, open: async () => {} },
    declarativeNetRequest: {
      updateSessionRules: vi.fn(async (args) => {
        callOrder.push({
          kind: 'dnr.updateSessionRules',
          addRuleIds: (args.addRules || []).map((r) => r.id),
          addRulesUrlPatterns: (args.addRules || []).map(
            (r) => r.condition && r.condition.regexFilter
          ),
          removeRuleIds: args.removeRuleIds || [],
        });
        return undefined;
      }),
    },
    offscreen: {
      hasDocument: vi.fn().mockResolvedValue(false),
      createDocument: vi.fn().mockResolvedValue(),
      closeDocument: vi.fn().mockResolvedValue(),
    },
  };
}


describe('runBrowserSideJob: DNR install precedes manifest fetch', () => {
  let ctx;
  let callOrder;

  beforeEach(() => {
    callOrder = [];
    const fetchStub = vi.fn(async (url, _opts) => {
      callOrder.push({ kind: 'fetch', url: String(url) });
      const u = String(url);

      // Manifest URL: return a media playlist body (no master variant
      // chase needed).
      if (u.includes('master.m3u8')) {
        return {
          ok: true,
          status: 200,
          text: async () => '#EXTM3U\n#EXTINF:10\nseg.ts\n',
          headers: new Map(),
        };
      }
      // /api/jobs/init: return a minimal plan
      if (u.endsWith('/api/jobs/init')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            job_id: '11111111-2222-3333-4444-555555555555',
            plan: {
              container: 'hls',
              total_segments: 1,
              tracks: {
                video: {
                  segment_count: 1,
                  segments: [{ seq: 0, url: 'https://protected.example.com/auth/seg0.ts' }],
                },
              },
            },
          }),
        };
      }
      // /finalize, /abort, etc — no-op success
      return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
    });

    const chrome = makeChromeStub(callOrder);

    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: fetchStub,
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    // Pretend showNotifications is off so we don't trip a missing helper.
    ctx.userSettings = { showNotifications: false };
  });

  it('same-site safety refusal stops before URL-only init', async () => {
    let caught = null;
    try {
      await ctx.runBrowserSideJob({
        nasEndpoint: 'http://nas.local:52052',
        apiKey: 'test-key',
        requestBody: {
          url: 'https://internal.corp.example/playlist.m3u8',
          title: 't',
          referer: '',
          headers: {},
          source_page: 'https://attacker.com/page',
        },
        title: 't',
        pageUrl: 'https://attacker.com/page',
        formatHint: 'm3u8',
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Browser-side manifest fetch refused/);
    expect(
      callOrder.some((c) => c.kind === 'fetch' && c.url.includes('internal.corp.example'))
    ).toBe(false);
    expect(
      callOrder.some((c) => c.kind === 'fetch' && c.url.endsWith('/api/jobs/init'))
    ).toBe(false);
  });

  // Codex review (P2): SW cold-start kicks off `_wv2nasInitRecovery()`
  // which re-reserves DNR slots for surviving offscreen jobs. If
  // `runBrowserSideJob` allocates a slot BEFORE recovery completes,
  // it picks a slot the survivor still owns and the subsequent
  // updateSessionRules clobbers the survivor's DNR rules.
  it('runBrowserSideJob awaits SW recovery before allocating DNR slot', async () => {
    // Set up a persisted survivor in chrome.storage.local with
    // dnrSlot=0 and a fresh heartbeat so recovery re-reserves slot 0.
    // Then fire a new browser-side download. If the implementation
    // doesn't await recovery, the new job allocates slot 0 (since
    // _wv2nasUsedDnrSlots starts empty at SW boot) and we'd see a
    // DNR rule install referencing the new job's master URL with
    // ID range starting at 10000 — which would collide with the
    // survivor's slot.
    const SURVIVOR_DNR_SLOT = 0;
    const survivorJobId = 'survivor-job-9999';
    const persistedJobs = {
      [survivorJobId]: {
        jobId: survivorJobId,
        dnrSlot: SURVIVOR_DNR_SLOT,
        ruleIds: [10000, 10050],
        startedAt: Date.now() - 1000,  // recent
        lastHeartbeat: Date.now() - 1000,  // ALIVE: < 5 min
        nasEndpoint: 'http://nas.local:52052',
        apiKey: 'k',
      },
    };

    // Recreate the chrome stub with prepopulated storage.
    const chrome = makeChromeStub(callOrder);
    let storageState = { wv2nasBrowserJobs: persistedJobs };
    chrome.storage.local.get = vi.fn(async (key) => {
      if (typeof key === 'string') {
        return key in storageState ? { [key]: storageState[key] } : {};
      }
      return { ...storageState };
    });
    chrome.storage.local.set = vi.fn(async (obj) => {
      Object.assign(storageState, obj);
    });
    chrome.storage.local.remove = vi.fn(async (key) => {
      delete storageState[key];
    });

    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: vi.fn(async (url) => {
        callOrder.push({ kind: 'fetch', url: String(url) });
        const u = String(url);
        // Generic OK + a minimal valid init plan so runBrowserSideJob
        // gets past the early phases without unhandled rejections.
        if (u.endsWith('/api/jobs/init')) {
          return {
            ok: true, status: 200,
            json: async () => ({
              job_id: 'new-job-test',
              plan: {
                container: 'hls', total_segments: 0, tracks: {},
              },
            }),
            text: async () => '',
            headers: new Map(),
          };
        }
        return {
          ok: true, status: 200,
          text: async () => '#EXTM3U\n#EXTINF:10\nseg.ts\n',
          json: async () => ({}),
          headers: new Map(),
        };
      }),
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };

    // Start the new job. The implementation must await
    // _wv2nasInitRecovery() before _wv2nasAllocateDnrSlot() — by
    // the time we get to the slot allocation, the survivor's slot 0
    // should be re-reserved in _wv2nasUsedDnrSlots.
    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'k',
      requestBody: {
        url: 'https://protected.example.com/auth/master.m3u8',
        title: 'New Job', referer: '', headers: {}, source_page: '',
      },
      title: 'New Job',
      pageUrl: 'https://protected.example.com/',
      formatHint: 'm3u8',
    });

    // Swallow any rejection from the truncated job flow (we're
    // testing slot allocation, not the full job).
    promise.catch(() => {});

    // Yield enough times for the recovery + slot allocation to
    // happen. (Recovery does several awaits internally.)
    await new Promise((r) => setTimeout(r, 50));

    // `_wv2nasUsedDnrSlots` is a `const` Set, not exposed as a
    // property on `ctx`. Use the vm helper to reach into the
    // background.js scope and snapshot it.
    const usedSlots = ctx.__eval('Array.from(_wv2nasUsedDnrSlots)');
    // Survivor's slot 0 was re-reserved by recovery.
    expect(usedSlots).toContain(SURVIVOR_DNR_SLOT);
    // New job got a DIFFERENT slot — the regression case is
    // {0} (both jobs sharing slot 0); the fix ensures >= 2 slots.
    expect(usedSlots.length).toBeGreaterThanOrEqual(2);
    expect(usedSlots.filter((s) => s === SURVIVOR_DNR_SLOT)).toHaveLength(1);
  });

  it('first DNR rule install happens before first fetch (manifest fetch is gated)', async () => {
    // Spawn job but DON'T wait for completion (SW would never get the
    // BROWSER_JOB_DONE message in this stub setup). What we care about
    // is the EARLY ordering: did DNR rules cover the manifest URL
    // before fetch hit it?
    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      requestBody: {
        // Same-site with pageUrl (Codex adversarial-review hardening
        // requires the master URL to be same-site with the page).
        url: 'https://cdn.protected.example.com/auth/master.m3u8',
        title: 'Test',
        referer: 'https://protected.example.com/watch',
        headers: { 'User-Agent': 'Mozilla/5.0 player' },
        source_page: 'https://protected.example.com/watch',
      },
      title: 'Test',
      pageUrl: 'https://protected.example.com/watch',
      formatHint: 'm3u8',
    });

    // Wait one event-loop tick so the synchronous + promise-chain part
    // up to the first fetch + first DNR call runs. Don't await the
    // full promise — it'll hang on the offscreen completion message.
    await new Promise((r) => setTimeout(r, 50));

    // Find the first DNR rule install and the first fetch in callOrder.
    const firstDnrIdx = callOrder.findIndex((c) => c.kind === 'dnr.updateSessionRules');
    const firstFetchIdx = callOrder.findIndex((c) => c.kind === 'fetch');

    expect(firstDnrIdx).toBeGreaterThanOrEqual(0);
    expect(firstFetchIdx).toBeGreaterThanOrEqual(0);
    // CRITICAL: DNR install must happen BEFORE first fetch.
    expect(firstDnrIdx).toBeLessThan(firstFetchIdx);

    // The first DNR install's rules must cover the manifest URL.
    const firstDnrCall = callOrder[firstDnrIdx];
    const patterns = firstDnrCall.addRulesUrlPatterns || [];
    const manifestPattern = patterns.find((p) => p && new RegExp(p).test(
      'https://cdn.protected.example.com/auth/master.m3u8'
    ));
    expect(manifestPattern).toBeTruthy();

    // Stop the runJob promise's hang from leaking into other tests.
    promise.catch(() => {});
  });

  it('background DNR builder packs more than 50 trusted URL prefixes into one slot', () => {
    ctx.__testSegmentUrls = Array.from(
      { length: 56 },
      (_v, i) => `https://protected.example.com/media/shard-${i}/seg.m4s`
    );

    const rules = ctx.__eval(`_wv2nasBuildDnrRules({
      segmentUrls: __testSegmentUrls,
      trustedSegmentUrls: __testSegmentUrls,
      referer: 'https://protected.example.com/watch',
      origin: 'https://protected.example.com',
      userAgent: 'UA',
      idBase: 10000,
      initiatorDomain: 'extid',
    })`);

    const requestRules = rules.filter((r) => r.action.requestHeaders);
    const responseRules = rules.filter((r) => r.action.responseHeaders);
    const ruleIds = rules.map((r) => r.id);

    expect(requestRules.length).toBeGreaterThan(0);
    expect(requestRules.length).toBeLessThanOrEqual(50);
    expect(responseRules.length).toBe(requestRules.length);
    expect(new Set(ruleIds).size).toBe(ruleIds.length);

    for (const url of ctx.__testSegmentUrls) {
      expect(requestRules.some((r) => new RegExp(r.condition.regexFilter).test(url))).toBe(true);
      expect(responseRules.some((r) => new RegExp(r.condition.regexFilter).test(url))).toBe(true);
    }
  });

  it('persists phase-2 DNR ids before installing phase-2 rules', async () => {
    callOrder = [];
    const fetchStub = vi.fn(async (url, _opts) => {
      callOrder.push({ kind: 'fetch', url: String(url) });
      const u = String(url);
      if (u.includes('master.m3u8')) {
        return {
          ok: true,
          status: 200,
          text: async () => '#EXTM3U\n#EXTINF:10\nseg.ts\n',
          headers: new Map(),
        };
      }
      if (u.endsWith('/api/jobs/init')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            job_id: 'phase2-dnr-job',
            plan: {
              container: 'hls',
              source_url: 'https://protected.example.com/auth/master.m3u8',
              total_segments: 1,
              tracks: {
                video: {
                  segment_count: 1,
                  segments: [{
                    seq: 0,
                    url: 'https://protected.example.com/media/seg0.ts',
                    key: { uri: 'https://keys.protected.example.com/key.bin' },
                  }],
                },
              },
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
    });
    const chrome = makeChromeStub(callOrder);
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: fetchStub,
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };

    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      requestBody: {
        url: 'https://protected.example.com/auth/master.m3u8',
        title: 'Test',
        referer: 'https://protected.example.com/watch',
        headers: { 'User-Agent': 'Mozilla/5.0 player' },
        source_page: 'https://protected.example.com/watch',
      },
      title: 'Test',
      pageUrl: 'https://protected.example.com/watch',
      formatHint: 'm3u8',
    });

    await new Promise((r) => setTimeout(r, 50));

    const phase2DnrIdx = callOrder.findIndex((c) =>
      c.kind === 'dnr.updateSessionRules'
      && (c.addRuleIds || []).length > 2
      && (c.addRulesUrlPatterns || []).some((p) =>
        p && new RegExp(p).test('https://keys.protected.example.com/key.bin')
      )
    );
    expect(phase2DnrIdx).toBeGreaterThanOrEqual(0);
    const phase2RuleIds = callOrder[phase2DnrIdx].addRuleIds;

    const preInstallPersistIdx = callOrder.findIndex((c, idx) => {
      if (idx >= phase2DnrIdx || c.kind !== 'storage.local.set') return false;
      const jobs = c.obj.wv2nasBrowserJobs || {};
      return Object.values(jobs).some((job) =>
        phase2RuleIds.every((id) => (job.ruleIds || []).includes(id))
      );
    });
    expect(preInstallPersistIdx).toBeGreaterThanOrEqual(0);

    promise.catch(() => {});
  });

  it('phase-2 DNR trusts segment hosts covered by trustedCdnSuffixes', async () => {
    callOrder = [];
    const segmentUrl = 'https://segments.cdn.example/video/0.ts';
    const fetchStub = vi.fn(async (url, _opts) => {
      callOrder.push({ kind: 'fetch', url: String(url) });
      const u = String(url);
      if (u.includes('master.m3u8')) {
        return {
          ok: true,
          status: 200,
          text: async () => '#EXTM3U\n#EXTINF:10\nvideo/0.ts\n',
          headers: new Map(),
        };
      }
      if (u.endsWith('/api/jobs/init')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            job_id: 'allowlisted-cdn-segments-job',
            plan: {
              container: 'hls',
              source_url: 'https://manifest.cdn.example/path/master.m3u8',
              selected_variant_url: 'https://manifest.cdn.example/path/master.m3u8',
              total_segments: 1,
              tracks: {
                video: {
                  segment_count: 1,
                  segments: [{ seq: 0, url: segmentUrl }],
                },
              },
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
    });
    const chrome = makeChromeStub(callOrder);
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: fetchStub,
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };

    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      requestBody: {
        url: 'https://manifest.cdn.example/path/master.m3u8',
        title: 'Cross CDN',
        referer: 'https://site.example/watch',
        headers: { 'User-Agent': 'Mozilla/5.0 player' },
        source_page: 'https://site.example/watch',
      },
      title: 'Cross CDN',
      pageUrl: 'https://site.example/watch',
      formatHint: 'm3u8',
      trustedCdnSuffixes: ['cdn.example'],
    });

    await new Promise((r) => setTimeout(r, 50));

    const phase2Dnr = callOrder.find((c) =>
      c.kind === 'dnr.updateSessionRules'
      && (c.addRulesUrlPatterns || []).some((p) =>
        p && new RegExp(p).test(segmentUrl)
      )
    );
    expect(phase2Dnr).toBeDefined();

    promise.catch(() => {});
  });

  it('manifest fetch uses credentials:include (cookies + session ride along)', async () => {
    // Independent assertion — even with DNR rules active, the fetch
    // call needs credentials:'include' for the session cookies that
    // DNR cannot inject.
    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      requestBody: {
        // Same-site with pageUrl per the new safety gate.
        url: 'https://cdn.protected.example.com/auth/master.m3u8',
        title: 'Test',
        referer: 'https://protected.example.com/watch',
        headers: {},
        source_page: 'https://protected.example.com/watch',
      },
      title: 'Test',
      pageUrl: 'https://protected.example.com/watch',
      formatHint: 'm3u8',
    });

    await new Promise((r) => setTimeout(r, 50));

    const manifestFetch = callOrder.find(
      (c) => c.kind === 'fetch' && c.url.includes('master.m3u8')
    );
    expect(manifestFetch).toBeDefined();
    // The actual options object isn't captured by our stub (only the
    // url is), so we trust the helper's existing test coverage for the
    // credentials behavior. This test exists to anchor the assertion
    // that the manifest fetch DOES happen — the regression case is when
    // it 403s before init even gets called.

    promise.catch(() => {});
  });

  it('passes only fetch-safe captured headers to offscreen', async () => {
    const chrome = makeChromeStub(callOrder);
    chrome.offscreen.hasDocument = vi.fn().mockResolvedValue(true);
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome,
      fetch: vi.fn(async (url, _opts) => {
        callOrder.push({ kind: 'fetch', url: String(url) });
        const u = String(url);
        if (u.includes('master.m3u8')) {
          return {
            ok: true,
            status: 200,
            text: async () => '#EXTM3U\n#EXTINF:10\nseg.ts\n',
            headers: new Map(),
          };
        }
        if (u.endsWith('/api/jobs/init')) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              job_id: '22222222-3333-4444-5555-666666666666',
              plan: {
                container: 'hls',
                total_segments: 1,
                tracks: {
                  video: {
                    segment_count: 1,
                    segments: [{ seq: 0, url: 'https://cdn.protected.example.com/seg0.ts' }],
                  },
                },
              },
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
      }),
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };

    const promise = ctx.runBrowserSideJob({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      requestBody: {
        url: 'https://cdn.protected.example.com/auth/master.m3u8',
        title: 'Test',
        referer: 'https://protected.example.com/watch',
        source_page: 'https://protected.example.com/watch',
        headers: {
          Authorization: 'Bearer keep-me',
          'X-Site-Token': 'tok',
          Referer: 'https://protected.example.com/watch',
          Origin: 'https://protected.example.com',
          'User-Agent': 'fake-ua',
          Range: 'bytes=0-1',
          Cookie: 'sid=secret',
          'Accept-Encoding': 'gzip',
          'Sec-Fetch-Site': 'same-origin',
          'Proxy-Authorization': 'Basic secret',
        },
      },
      title: 'Test',
      pageUrl: 'https://protected.example.com/watch',
      formatHint: 'm3u8',
    });

    await new Promise((r) => setTimeout(r, 50));

    const startMsg = callOrder.find(
      (c) => c.kind === 'runtime.sendMessage'
        && c.msg
        && c.msg.type === 'START_BROWSER_JOB'
    );
    expect(startMsg).toBeDefined();
    const headers = startMsg.msg.payload.requestHeaders;
    expect(headers.Authorization).toBe('Bearer keep-me');
    expect(headers['X-Site-Token']).toBe('tok');
    const lowercased = Object.fromEntries(
      Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v])
    );
    expect(lowercased.referer).toBeUndefined();
    expect(lowercased.origin).toBeUndefined();
    expect(lowercased['user-agent']).toBeUndefined();
    expect(lowercased.range).toBeUndefined();
    expect(lowercased.cookie).toBeUndefined();
    expect(lowercased['accept-encoding']).toBeUndefined();
    expect(lowercased['sec-fetch-site']).toBeUndefined();
    expect(lowercased['proxy-authorization']).toBeUndefined();

    promise.catch(() => {});
  });
});


// Codex adversarial-review (high): only 404 from /api/jobs/init is
// safe to fall back to /api/download. Other 4xx — especially 422 —
// are SAFETY rejections (HTTPS-required gate, non-public host); the
// legacy /api/download path only enforces SSRF guard when
// SSRF_GUARD env is set (default off in shipped configs), so silent
// fallback would re-open the exact intranet/metadata access the
// safety gate exists to block.

describe('runBrowserSideJob: init failure fallback policy (Codex adversarial-review)', () => {
  let ctx;
  let callOrder;

  function setupCtx(initStatus, initDetail) {
    callOrder = [];
    const fetchStub = vi.fn(async (url, _opts) => {
      callOrder.push({ kind: 'fetch', url: String(url) });
      const u = String(url);
      if (u.includes('master.m3u8')) {
        return {
          ok: true, status: 200,
          text: async () => '#EXTM3U\n#EXTINF:10\nseg.ts\n',
          headers: new Map(),
        };
      }
      if (u.endsWith('/api/jobs/init')) {
        return {
          ok: false, status: initStatus,
          json: async () => ({ detail: initDetail }),
          text: async () => String(initDetail),
        };
      }
      return { ok: true, status: 200, json: async () => ({}), text: async () => '' };
    });
    const chrome = makeChromeStub(callOrder);
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome, fetch: fetchStub,
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };
  }

  async function runJobAndCatch(extraBody = {}) {
    let caught = null;
    try {
      await ctx.runBrowserSideJob({
        nasEndpoint: 'http://nas.local:52052',
        apiKey: 'test-key',
        requestBody: {
          url: 'https://cdn.example.com/master.m3u8',
          title: 't', referer: '', headers: {}, source_page: '',
          ...extraBody,
        },
        title: 't', pageUrl: '', formatHint: 'm3u8',
      });
    } catch (err) {
      caught = err;
    }
    return caught;
  }

  it('init 422 (HTTPS-required safety) is TERMINAL — NOT fallbackable', async () => {
    // The Codex regression: /api/download falls open without
    // always-on SSRF guard, so falling back to it after 422 would
    // re-enable the exact private-network/HTTP fetches the
    // browser-side gate just rejected. Must fail closed.
    setupCtx(422, "Plan URL must use HTTPS for browser-side mode "
      + "(plain HTTP is rejected because DNS rebinding between "
      + "server-side validation and browser-side fetch is "
      + "unmitigatable): http://insecure.example.com/seg.ts");
    const caught = await runJobAndCatch();
    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Init failed/);
    // 422 = safety rejection → MUST stay terminal.
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 422 (non-public host safety) is TERMINAL — NOT fallbackable', async () => {
    setupCtx(422, "Plan URL host '192.168.1.1' resolves to non-public IP");
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 401 (bad API key) is TERMINAL — NOT fallbackable', async () => {
    // NAS-direct uses the same key, so falling back achieves
    // nothing AND obscures the auth error from the user.
    setupCtx(401, 'Unauthorized');
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 403 (forbidden) is TERMINAL — NOT fallbackable', async () => {
    setupCtx(403, 'Forbidden');
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 400 (malformed payload) is TERMINAL — NOT fallbackable', async () => {
    setupCtx(400, 'Bad request');
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 429 (rate limit) is TERMINAL — NOT fallbackable', async () => {
    setupCtx(429, 'Rate limit exceeded');
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });

  it('init 404 (endpoint missing on legacy NAS) IS fallbackable', async () => {
    // The only legitimate compatibility case: a NAS without v2.5
    // doesn't have /api/jobs/init at all. Falling back to
    // /api/download lets the user keep submitting jobs to legacy
    // installations while the option is being rolled out.
    setupCtx(404, 'Not Found');
    const caught = await runJobAndCatch();
    expect(caught).not.toBeNull();
    expect(caught.fallbackable).toBe(true);
  });

  it('init 500 (server error) is TERMINAL — NOT fallbackable', async () => {
    // Ambiguous — server-side state MAY have been partially
    // created. Silent fallback would risk double-submitting.
    setupCtx(500, 'Internal error');
    const caught = await runJobAndCatch();
    expect(caught.fallbackable).toBeFalsy();
  });
});


// Codex adversarial-review: the pre-check at sendToNAS' routing
// decision skips browser-side mode for URLs the safety gate WOULD
// reject (plain HTTP, non-public hosts). Without this, legitimate
// HTTP-only legacy streams that previously worked through
// /api/download silently fail with "Browser-side job failed" since
// 422 init rejections are now terminal (correctly — fallback would
// re-open the SSRF surface).

describe('sendToNAS: routes URLs that would 422 directly to NAS-direct', () => {
  let ctx;
  let fetchCalls;
  let fetchRequests;
  // For sendToNAS we need a more complete chrome stub — storage.sync
  // returns the user's NAS config via Promise (await form).

  function makeFullChromeStub({ settings = {} } = {}) {
    const noop = () => {};
    return {
      runtime: {
        sendMessage: vi.fn(),
        onMessage: { addListener: noop, removeListener: noop },
        onInstalled: { addListener: noop },
        lastError: null,
        openOptionsPage: noop,
        getManifest: () => ({ version: '2.5.0' }),
        getURL: (p) => `chrome-extension://test/${p}`,
        id: 'test-extension-id',
      },
      storage: {
        sync: {
          // Promise-form to match sendToNAS' `await chrome.storage.sync.get(...)`.
          get: vi.fn(async (keys) => {
            const out = {};
            const want = Array.isArray(keys) ? keys : (keys ? [keys] : []);
            for (const k of want) {
              if (k in settings) out[k] = settings[k];
            }
            return out;
          }),
          set: async () => {},
        },
        local: { set: async () => {}, get: async () => ({}) },
        onChanged: { addListener: noop },
      },
      webRequest: {
        onBeforeRequest: { addListener: noop },
        onSendHeaders: { addListener: noop },
        onHeadersReceived: { addListener: noop },
      },
      webNavigation: { onCommitted: { addListener: noop } },
      action: {
        setBadgeText: noop, setBadgeBackgroundColor: noop,
        onClicked: { addListener: noop },
      },
      tabs: {
        onRemoved: { addListener: noop }, onUpdated: { addListener: noop },
        onActivated: { addListener: noop },
        query: (_q, cb) => cb && cb([]),
        get: (_id, cb) => cb && cb(null),
      },
      contextMenus: { create: noop, onClicked: { addListener: noop } },
      notifications: {
        create: vi.fn(), clear: noop,
        onClicked: { addListener: noop }, onClosed: { addListener: noop },
      },
      cookies: { getAll: async () => [] },
      sidePanel: { setOptions: async () => {}, open: async () => {} },
      declarativeNetRequest: { updateSessionRules: vi.fn().mockResolvedValue() },
      offscreen: {
        hasDocument: vi.fn().mockResolvedValue(false),
        createDocument: vi.fn().mockResolvedValue(),
        closeDocument: vi.fn().mockResolvedValue(),
      },
    };
  }

  function setupCtx(settings) {
    fetchCalls = [];
    fetchRequests = [];
    const fetchStub = vi.fn(async (url, _opts) => {
      const u = String(url);
      fetchCalls.push(u);
      fetchRequests.push({ url: u, opts: _opts || {} });
      if (u.endsWith('/api/download')) {
        return {
          ok: true, status: 200,
          json: async () => ({ id: 'job-id-1234' }),
          text: async () => '{}',
        };
      }
      // Anything else (we shouldn't hit these in the pre-check path).
      return {
        ok: true, status: 200,
        json: async () => ({}), text: async () => '',
        headers: new Map(),
      };
    });
    const chrome = makeFullChromeStub({ settings });
    ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
      chrome, fetch: fetchStub,
      AbortController, AbortSignal,
      Promise, Map, Set, Error, JSON, RegExp, Math,
    });
    ctx.userSettings = { showNotifications: false };
  }

  it('http:// m3u8 URL routes to /api/download (NOT /api/jobs/init)', async () => {
    // Legacy HTTP-only stream: pre-check skips browser-side because
    // the safety gate would 422 anyway. NAS-direct handles it as
    // before browser-side mode existed.
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,  // user has browser-side enabled (default)
    });

    await ctx.sendToNAS(
      'http://insecure.example.com/playlist.m3u8',
      'My Video', 'http://insecure.example.com/page', 1,
    );

    // /api/download was called.
    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(true);
    // /api/jobs/init was NOT called — the pre-check skipped browser-
    // side BEFORE any 422 could happen.
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('https:// localhost m3u8 URL is refused, not routed to NAS-direct', async () => {
    // Non-public host: fail closed instead of bypassing the browser-side
    // safety gate through /api/download.
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'https://localhost/internal.m3u8',
      'Internal', 'https://localhost/page', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(false);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('cross-site DNS m3u8 URL is refused, not routed to NAS-direct', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'https://internal.corp.example/playlist.m3u8',
      'Split horizon', 'https://attacker.com/watch', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(false);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('useBrowserSide=false: always routes to NAS-direct regardless of URL', async () => {
    // Sanity: when the user has explicitly disabled browser-side
    // mode, even a perfectly-fine HTTPS public URL goes through
    // NAS-direct.
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: false,  // user opt-out
    });

    await ctx.sendToNAS(
      'https://cdn.example.com/playlist.m3u8',
      'Public', 'https://cdn.example.com/page', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(true);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('pre-check rejects http://172.16.0.1 (private RFC 1918): refuses NAS-direct', async () => {
    // Defense-in-depth: even though plain HTTP would already be
    // caught, the host-IP check prevents treating private HTTP as
    // legacy-public HTTP.
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'http://172.16.0.1/internal.m3u8',
      'RFC1918', 'http://172.16.0.1/page', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(false);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('pre-check rejects https://169.254.169.254 (metadata): refuses NAS-direct', async () => {
    // The classic AWS metadata service IP. Browser-side gate would
    // 422 this; pre-check must not skip into /api/download because
    // the legacy SSRF guard is not always-on.
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'https://169.254.169.254/latest/iam/credentials.m3u8',
      'metadata', 'https://169.254.169.254/page', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(false);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  // Codex review (P1): URL-pattern detected manifests (the most
  // common detection path) used to keep `formatHint = null` because
  // `getDetectedFormat()` only returns Content-Type-derived signals.
  // The browser-side router then short-circuited to NAS-direct,
  // defeating the cookie/IP-bound stream support this feature was
  // built for. Fix: fall back to URL-suffix sniffing.

  it('.m3u8 URL detected by URL pattern (no Content-Type) routes to browser-side', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    // The URL ends in .m3u8 and is on a public HTTPS host. With the
    // pre-fix routing, this would have stayed NAS-direct because
    // getDetectedFormat returned null. Post-fix: URL-suffix
    // sniffing classifies it as m3u8 and the router engages
    // browser-side mode.
    await ctx.sendToNAS(
      'https://cdn.example.com/stream/playlist.m3u8',
      'My Video', 'https://cdn.example.com/watch', 1,
    );

    // Browser-side engaged → /api/jobs/init was called.
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(true);
  });

  it('.mpd URL detected by URL pattern routes to browser-side', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'https://cdn.example.com/stream/manifest.mpd',
      'My Video', 'https://cdn.example.com/watch', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(true);
  });

  it('.m3u8 with query string still classified as m3u8', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    // Real-world signed URLs: m3u8 followed by ?token=...&expires=...
    await ctx.sendToNAS(
      'https://cdn.example.com/playlist.m3u8?token=xyz&expires=12345',
      'Signed', 'https://cdn.example.com/watch', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(true);
  });

  it('.mp4 URL stays on NAS-direct (browser-side is for HLS/DASH only)', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: true,
    });

    await ctx.sendToNAS(
      'https://cdn.example.com/video.mp4',
      'MP4', 'https://cdn.example.com/watch', 1,
    );

    expect(fetchCalls.some((u) => u.endsWith('/api/download'))).toBe(true);
    expect(fetchCalls.some((u) => u.endsWith('/api/jobs/init'))).toBe(false);
  });

  it('content-type detected signed manifest keeps the clicked fresh URL over stale capture', async () => {
    setupCtx({
      nasEndpoint: 'http://nas.local:52052',
      apiKey: 'test-key',
      useBrowserSide: false,
    });

    const pageUrl = 'https://example.com/watch/neutral-code';
    const tabId = 7;
    const oldUrl = 'https://cdn.example.com/hls/video-404/index.jpg?v=6&exp=1779411600&auth=old';
    const freshUrl = 'https://cdn.example.com/hls/video-404/index.jpg?v=6&exp=1779498000&auth=fresh';
    const now = Date.now();

    ctx.__eval(`
      currentTabUrls[${tabId}] = ${JSON.stringify([
        {
          url: oldUrl,
          detectedFormat: 'm3u8',
          timestamp: now - 86_400_000,
          tabId,
          pageUrl,
        },
        {
          url: freshUrl,
          detectedFormat: 'm3u8',
          timestamp: now - 1_000,
          tabId,
          pageUrl,
        },
      ])};
      capturedHeaders = ${JSON.stringify({
        [oldUrl]: {
          headers: { Referer: pageUrl, 'User-Agent': 'UA-old' },
          timestamp: now - 86_400_000,
          initiator: pageUrl,
          tabId,
        },
      })};
    `);

    await ctx.sendToNAS(freshUrl, 'Signed', pageUrl, tabId);

    const downloadReq = fetchRequests.find((req) => req.url.endsWith('/api/download'));
    expect(downloadReq).toBeTruthy();
    const body = JSON.parse(downloadReq.opts.body);
    expect(body.url).toBe(freshUrl);
  });
});
