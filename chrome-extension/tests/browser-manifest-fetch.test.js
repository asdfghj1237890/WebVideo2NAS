// Tests for v2.5 Codex review fix #1 — extension fetches the manifest in
// browser context and resolves HLS master → variant itself, so /api/jobs/init
// can plan from `manifest_text` instead of relying on NAS to reach a
// protected manifest URL. The key helpers live as top-level functions in
// background.js; we load that file in a vm context and pull them off.

import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';
import path from 'node:path';

const BACKGROUND_SCRIPT = path.resolve(__dirname, '..', 'background.js');


function makeChromeStub() {
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
    },
    storage: {
      sync: { get: (_keys, cb) => cb && cb({}), set: async () => {} },
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
      query: (_q, cb) => cb && cb([]), get: (_id, cb) => cb && cb(null),
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


let ctx;
beforeAll(() => {
  ctx = loadScriptIntoContext(BACKGROUND_SCRIPT, {
    chrome: makeChromeStub(),
    fetch: () => { throw new Error('fetch must be stubbed per-test'); },
    AbortController, AbortSignal,
    Promise, Map, Set, Error, JSON, RegExp, Math,
    // Codex review (P2) test support: the new manifest size cap uses
    // streaming reads + TextDecoder to handle UTF-8 chunk boundaries.
    // The vm context doesn't auto-inherit built-ins; inject explicitly.
    TextDecoder, TextEncoder, parseInt, Number, Object, Array,
    globalThis: undefined, // let vm provide
  });
});


describe('_wv2nasPickBestHlsVariant', () => {
  it('picks variants across bandwidth, URL, and malformed playlist cases', () => {
    const cases = [
      {
        name: 'max BANDWIDTH variant from a master playlist',
        master: `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=480000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
mid.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4800000,RESOLUTION=1920x1080
high.m3u8
`,
        expected: 'https://cdn.example.com/high.m3u8',
      },
      {
        name: 'absolute variant URL',
        master: `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000
https://other.example.com/v/main.m3u8
`,
        expected: 'https://other.example.com/v/main.m3u8',
      },
      {
        name: 'comment lines between #EXT-X-STREAM-INF and URL',
        master: `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
# vendor comment
variant.m3u8
`,
        expected: 'https://cdn.example.com/variant.m3u8',
      },
      {
        name: 'no parsable variants',
        master: '#EXTM3U\n#EXT-X-VERSION:3\n',
        baseUrl: 'https://cdn.example.com/x.m3u8',
        expected: null,
      },
      {
        name: 'missing BANDWIDTH defaults to 0; first variant wins',
        master: `#EXTM3U
#EXT-X-STREAM-INF:RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:RESOLUTION=1920x1080
high.m3u8
`,
        expected: 'https://cdn.example.com/low.m3u8',
      },
    ];

    for (const { name, master, baseUrl = 'https://cdn.example.com/master.m3u8', expected } of cases) {
      expect(ctx._wv2nasPickBestHlsVariant(master, baseUrl), name).toBe(expected);
    }
  });
});


describe('_wv2nasFetchManifestInBrowser', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = ctx.fetch;
  });

  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url) => {
      const r = responses[i++];
      if (typeof r === 'function') return r(url);
      if (r instanceof Error) throw r;
      return r;
    });
  }

  function ok(text, status = 200) {
    return { ok: status < 400, status, text: async () => text };
  }

  it('media playlist: single fetch, returns text + url', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8'
    );
    expect(result).toEqual({
      manifest_text: '#EXTM3U\n#EXTINF:10\nseg.ts\n',
      base_url: 'https://cdn.example.com/playlist.m3u8',
    });
    expect(ctx.fetch).toHaveBeenCalledTimes(1);
  });

  it('master playlist: chases variant + returns variant text + variant URL', async () => {
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
hi.m3u8
`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);

    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8'
    );
    expect(result).toEqual({
      manifest_text: variant,
      base_url: 'https://cdn.example.com/hi.m3u8',
    });
    expect(ctx.fetch).toHaveBeenCalledTimes(2);
    // Second call uses the resolved variant URL.
    expect(ctx.fetch.mock.calls[1][0]).toBe('https://cdn.example.com/hi.m3u8');
  });

  it('master + variant fetch fails: falls back to master text', async () => {
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
hi.m3u8
`;
    setFetchSeq([ok(master), ok('', 403)]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8'
    );
    expect(result).toEqual({
      manifest_text: master,
      base_url: 'https://cdn.example.com/master.m3u8',
    });
  });

  it('DASH MPD: single fetch, no variant chase', async () => {
    const mpd = '<?xml version="1.0"?><MPD></MPD>';
    setFetchSeq([ok(mpd)]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/manifest.mpd'
    );
    expect(result).toEqual({
      manifest_text: mpd,
      base_url: 'https://cdn.example.com/manifest.mpd',
    });
  });

  it('initial fetch HTTP error: returns null (caller falls back to NAS-fetch)', async () => {
    setFetchSeq([ok('', 403)]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8'
    );
    expect(result).toBeNull();
  });

  it('initial fetch network error: returns null', async () => {
    setFetchSeq([new TypeError('failed to fetch')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8'
    );
    expect(result).toBeNull();
  });

  it('credentials:include is set on both fetches (cookies + session must ride)', async () => {
    const master = `#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n`;
    const variant = `#EXTM3U\n#EXTINF:10\nseg.ts\n`;
    setFetchSeq([ok(master), ok(variant)]);
    await ctx._wv2nasFetchManifestInBrowser('https://cdn.example.com/master.m3u8');
    expect(ctx.fetch.mock.calls[0][1]).toMatchObject({ credentials: 'include' });
    expect(ctx.fetch.mock.calls[1][1]).toMatchObject({ credentials: 'include' });
  });
});


// Codex review: phase-1 DNR rule was built only from the master URL,
// so a variant on a different directory/origin would fetch without
// the spoofed Referer/Origin/UA and 403 on protected sites. The
// helper now accepts an optional dnrContext and installs a
// complementary rule for the variant URL before fetching.

describe('_wv2nasFetchManifestInBrowser variant DNR coverage', () => {
  function ok(text, status = 200) {
    return { ok: status < 400, status, text: async () => text };
  }
  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url) => {
      const r = responses[i++];
      if (typeof r === 'function') return r(url);
      if (r instanceof Error) throw r;
      return r;
    });
  }

  function makeDnrContext() {
    return {
      referer: 'https://player.example.com/',
      origin: 'https://player.example.com',
      userAgent: 'Mozilla/5.0 test-ua',
      idBase: 10000,  // matches the slot-based base for slot=0
      ruleIds: [10000, 10050],  // existing phase-1 IDs
    };
  }

  it('deeper-subdomain variant (same-site): installs DNR rule covering the variant URL', async () => {
    // Codex adversarial-review (high) hardening: variants on a
    // different SITE from the master are now refused at the safety
    // gate (split-horizon mitigation). Variants on a deeper
    // subdomain still pass through and get DNR coverage.
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://variants.cdn.example.com/v/hi.m3u8
`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);
    const updateSpy = vi.fn().mockResolvedValue();
    ctx.chrome.declarativeNetRequest.updateSessionRules = updateSpy;

    const dnrContext = makeDnrContext();
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      dnrContext,
    );

    expect(result).toEqual({
      manifest_text: variant,
      base_url: 'https://variants.cdn.example.com/v/hi.m3u8',
    });
    // The new variant DNR rule must have been installed BEFORE the
    // variant fetch.
    expect(updateSpy).toHaveBeenCalled();
    const call = updateSpy.mock.calls[0][0];
    expect(Array.isArray(call.addRules)).toBe(true);
    expect(call.addRules.length).toBeGreaterThanOrEqual(1);
    // The rule's regex filter must reference the variant origin/path.
    const regex = call.addRules[0].condition.regexFilter;
    expect(regex).toMatch(/variants\\?\.cdn\\?\.example\\?\.com/);
    // Variant rule IDs were appended to the caller's ruleIds for
    // cleanup.
    expect(dnrContext.ruleIds).toContain(10001);  // idBase + 1
  });

  it('same-origin variant: installs DNR rule covering the variant URL', async () => {
    // Even when same-origin, the variant URL might be on a different
    // path the phase-1 regex doesn't match. Install the rule anyway.
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
hi.m3u8
`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);
    const updateSpy = vi.fn().mockResolvedValue();
    ctx.chrome.declarativeNetRequest.updateSessionRules = updateSpy;

    const dnrContext = makeDnrContext();
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      dnrContext,
    );

    expect(updateSpy).toHaveBeenCalled();
    expect(dnrContext.ruleIds.length).toBeGreaterThan(2);
  });

  it('no dnrContext: silent back-compat — no DNR install, fetch still works', async () => {
    const master = `#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);
    const updateSpy = vi.fn().mockResolvedValue();
    ctx.chrome.declarativeNetRequest.updateSessionRules = updateSpy;

    // Old call shape — no dnrContext.
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
    );

    expect(result.base_url).toBe('https://cdn.example.com/hi.m3u8');
    expect(updateSpy).not.toHaveBeenCalled();
  });

  it('media playlist (no variant): no extra DNR install', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const updateSpy = vi.fn().mockResolvedValue();
    ctx.chrome.declarativeNetRequest.updateSessionRules = updateSpy;

    const dnrContext = makeDnrContext();
    const ruleIdsBefore = [...dnrContext.ruleIds];
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      dnrContext,
    );

    expect(updateSpy).not.toHaveBeenCalled();
    expect(dnrContext.ruleIds).toEqual(ruleIdsBefore);
  });

  it('DNR install failure does not break the variant fetch', async () => {
    const master = `#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);
    ctx.chrome.declarativeNetRequest.updateSessionRules =
      vi.fn().mockRejectedValue(new Error('DNR limit exceeded'));

    const dnrContext = makeDnrContext();
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      dnrContext,
    );

    // Variant fetch went through (best-effort: maybe site doesn't
    // require Referer/Origin/UA spoof, or Chrome's default headers
    // happen to be acceptable).
    expect(result.manifest_text).toBe(variant);
  });
});


// Codex review: protected manifests gated on captured auth headers
// (Authorization, X-Token, etc.) used to 401/403 here because the
// in-browser fetch went out with credentials only — the captured
// headers from the original site request weren't passed through.
// The legacy NAS-direct path forwards them via requestBody.headers;
// this path now does the same.

describe('_wv2nasFetchManifestInBrowser captured auth headers', () => {
  function ok(text, status = 200) {
    return { ok: status < 400, status, text: async () => text };
  }
  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url, opts) => {
      const r = responses[i++];
      if (typeof r === 'function') return r(url, opts);
      if (r instanceof Error) throw r;
      return r;
    });
  }

  it('passes Authorization header through to the master fetch', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        referer: 'https://player.example.com/',
        origin: 'https://player.example.com',
        userAgent: 'UA',
        idBase: 10000,
        ruleIds: [],
        headers: { Authorization: 'Bearer site-token-XYZ' },
      },
    );
    expect(result.manifest_text).toContain('#EXTM3U');
    // Check the actual fetch options.
    const opts = ctx.fetch.mock.calls[0][1];
    expect(opts.headers).toBeDefined();
    expect(opts.headers.Authorization).toBe('Bearer site-token-XYZ');
    expect(opts.credentials).toBe('include');
  });

  it('passes Authorization header through to the variant fetch too', async () => {
    const master = `#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      {
        referer: 'https://player.example.com/',
        origin: 'https://player.example.com',
        userAgent: 'UA',
        idBase: 10000,
        ruleIds: [],
        headers: { Authorization: 'Bearer site-token-XYZ' },
      },
    );
    // Both calls receive the captured Authorization header.
    expect(ctx.fetch.mock.calls[0][1].headers.Authorization).toBe('Bearer site-token-XYZ');
    expect(ctx.fetch.mock.calls[1][1].headers.Authorization).toBe('Bearer site-token-XYZ');
  });

  it('passes custom X-* tokens', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        idBase: 10000,
        ruleIds: [],
        headers: {
          'X-Auth-Token': 'tok',
          'X-Site-Session': 'sess',
        },
      },
    );
    const opts = ctx.fetch.mock.calls[0][1];
    expect(opts.headers['X-Auth-Token']).toBe('tok');
    expect(opts.headers['X-Site-Session']).toBe('sess');
  });

  it('strips forbidden headers (Cookie, Origin, Referer, UA, Host) before fetch', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        idBase: 10000,
        ruleIds: [],
        headers: {
          // Forbidden — must be stripped (browsers reject or silently drop).
          'Cookie': 'session=secret',
          'Origin': 'https://player.example.com',
          'Referer': 'https://player.example.com/watch',
          'User-Agent': 'fake-ua',
          'Host': 'cdn.example.com',
          'Connection': 'keep-alive',
          'Content-Length': '0',
          'Accept-Encoding': 'gzip',
          'Range': 'bytes=0-1',
          'Sec-Fetch-Site': 'same-origin',
          'Proxy-Authorization': 'Basic secret',
          // Allowed — must survive.
          'Authorization': 'Bearer keep-me',
          'X-Foo': 'bar',
        },
      },
    );
    const opts = ctx.fetch.mock.calls[0][1];
    expect(opts.headers.Authorization).toBe('Bearer keep-me');
    expect(opts.headers['X-Foo']).toBe('bar');
    // Forbidden ones must not appear (case-insensitive check).
    const lowercased = Object.fromEntries(
      Object.entries(opts.headers).map(([k, v]) => [k.toLowerCase(), v])
    );
    expect(lowercased.cookie).toBeUndefined();
    expect(lowercased.origin).toBeUndefined();
    expect(lowercased.referer).toBeUndefined();
    expect(lowercased['user-agent']).toBeUndefined();
    expect(lowercased.host).toBeUndefined();
    expect(lowercased.connection).toBeUndefined();
    expect(lowercased['content-length']).toBeUndefined();
    expect(lowercased['accept-encoding']).toBeUndefined();
    expect(lowercased.range).toBeUndefined();
    expect(lowercased['sec-fetch-site']).toBeUndefined();
    expect(lowercased['proxy-authorization']).toBeUndefined();
  });

  it('no dnrContext: no headers param needed; fetch still works', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
    );
    expect(result.manifest_text).toContain('#EXTM3U');
    // Still credentialed.
    expect(ctx.fetch.mock.calls[0][1].credentials).toBe('include');
  });

  it('null headers in dnrContext: tolerated, no error', async () => {
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      { idBase: 10000, ruleIds: [], headers: null },
    );
    expect(result.manifest_text).toContain('#EXTM3U');
  });
});


// Codex review (P2): bound the manifest body read so a misdetected /
// hostile URL can't fill SW memory before the server-side 10 MB cap
// can reject. The new helper streams via getReader() and aborts
// mid-stream when accumulated bytes exceed the cap.

describe('manifest body size cap (Codex P2)', () => {
  function makeStreamingResp(chunks, { contentLength = null } = {}) {
    const headers = {
      get(name) {
        if (name.toLowerCase() === 'content-length' && contentLength != null) {
          return String(contentLength);
        }
        return null;
      },
    };
    let cancelled = false;
    let i = 0;
    const reader = {
      async read() {
        if (cancelled || i >= chunks.length) {
          return { value: undefined, done: true };
        }
        return { value: chunks[i++], done: false };
      },
      cancel() { cancelled = true; },
    };
    const body = {
      getReader: () => reader,
      cancel() { cancelled = true; },
    };
    return {
      ok: true, status: 200, headers, body,
      get _cancelled() { return cancelled; },
    };
  }

  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url) => {
      const r = responses[i++];
      if (r instanceof Error) throw r;
      return r;
    });
  }

  it('rejects upfront when Content-Length declares > 10 MB cap', async () => {
    // Caller-side: the helper returns null on read failure (caller
    // falls back to NAS-fetch), and cancels the body to free socket.
    const resp = makeStreamingResp(
      [new Uint8Array([1])],
      { contentLength: 20 * 1024 * 1024 },
    );
    setFetchSeq([resp]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/huge.m3u8',
    );
    expect(result).toBeNull();
    // Body cancelled — socket freed, no further bytes consumed.
    expect(resp._cancelled).toBe(true);
  });

  it('aborts mid-stream when accumulated bytes exceed cap', async () => {
    // 5 MB chunk + 6 MB chunk = 11 MB total — must trip the cap
    // before the second chunk is accumulated.
    const c1 = new TextEncoder().encode('A'.repeat(5 * 1024 * 1024));
    const c2 = new TextEncoder().encode('B'.repeat(6 * 1024 * 1024));
    const resp = makeStreamingResp([c1, c2]);
    setFetchSeq([resp]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/oversize.m3u8',
    );
    expect(result).toBeNull();
    expect(resp._cancelled).toBe(true);
  });

  it('happy path: small manifest within cap returns text', async () => {
    const small = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    const chunk = new TextEncoder().encode(small);
    setFetchSeq([makeStreamingResp([chunk])]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
    );
    expect(result).not.toBeNull();
    expect(result.manifest_text).toBe(small);
    expect(result.base_url).toBe('https://cdn.example.com/playlist.m3u8');
  });

  it('multi-byte UTF-8 across chunk boundary decodes correctly', async () => {
    // 中文字 in UTF-8 is 3 bytes — split across chunks to ensure
    // TextDecoder({stream:true}) handles partial codepoints.
    const enc = new TextEncoder();
    const full = enc.encode('#EXTM3U\n# 中文字\n#EXTINF:10\nseg.ts\n');
    // Split mid-codepoint.
    const splitAt = 10; // somewhere inside the 3-byte char
    const c1 = full.slice(0, splitAt);
    const c2 = full.slice(splitAt);
    setFetchSeq([makeStreamingResp([c1, c2])]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
    );
    expect(result.manifest_text).toContain('中文字');
  });

  it('variant fetch is also size-capped', async () => {
    // Master is small. Variant returns Content-Length > cap.
    const master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n';
    const masterResp = makeStreamingResp(
      [new TextEncoder().encode(master)],
    );
    const variantResp = makeStreamingResp(
      [new Uint8Array([1])],
      { contentLength: 20 * 1024 * 1024 },
    );
    setFetchSeq([masterResp, variantResp]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
    );
    // Variant fetch fails the cap → fall through to master text path.
    expect(result).not.toBeNull();
    expect(result.manifest_text).toContain('#EXT-X-STREAM-INF');
    expect(result.base_url).toBe('https://cdn.example.com/master.m3u8');
    // Variant body was cancelled.
    expect(variantResp._cancelled).toBe(true);
  });

  it('exposes _wv2nasReadManifestText for unit testing', () => {
    expect(typeof ctx._wv2nasReadManifestText).toBe('function');
  });
});


// Codex adversarial-review (high): the variant fetch in
// _wv2nasFetchManifestInBrowser used `credentials: 'include'` and
// `headers: fetchHeaders` unconditionally. A malicious or misdetected
// master pointing variant at attacker-controlled `evil.com` would
// exfiltrate the captured Authorization / X-* tokens. The fix: scope
// both cookies AND headers per-URL via _wv2nasIsTrustedDnrUrl.

describe('manifest variant fetch: per-URL header scoping (Codex adversarial-review)', () => {
  function ok(text, status = 200) {
    return { ok: status < 400, status, text: async () => text };
  }
  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url, opts) => {
      const r = responses[i++];
      if (typeof r === 'function') return r(url, opts);
      if (r instanceof Error) throw r;
      return r;
    });
  }

  it('cross-site variant fetch: REFUSED at safety gate (no fetch attempted)', async () => {
    // Codex adversarial-review (high): the variant URL must be
    // same-site with master to even reach the fetch step. Master
    // on cdn.example.com pointing variant at evil.example.org is
    // refused as terminal for browser-side mode; otherwise the
    // caller could continue with URL-only init and later fetch
    // segment URLs from the refused trust boundary.
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://evil.example.org/v/hi.m3u8
`;
    setFetchSeq([ok(master)]);  // ONLY master fetched

    const captured = {
      Authorization: 'Bearer site-token-XYZ',
      'X-Auth-Token': 'tok-123',
    };
    const dnrContext = {
      referer: 'https://player.example.com/',
      origin: 'https://player.example.com',
      userAgent: 'UA',
      idBase: 10000,
      ruleIds: [10000, 10050],
      headers: captured,
    };
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      dnrContext,
    );

    // Master fetch happened (trusted, headers rode).
    const masterCall = ctx.fetch.mock.calls[0];
    expect(masterCall[0]).toBe('https://cdn.example.com/master.m3u8');
    expect(masterCall[1].credentials).toBe('include');
    expect(masterCall[1].headers.Authorization).toBe('Bearer site-token-XYZ');
    // Variant fetch was REFUSED — no second fetch call.
    expect(ctx.fetch.mock.calls).toHaveLength(1);
    expect(result).toMatchObject({
      safetyRejected: true,
      url: 'https://evil.example.org/v/hi.m3u8',
    });
  });

  it('same-origin variant fetch keeps credentials + headers', async () => {
    // Variant on the same host as the master — auth headers are safe.
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
hi.m3u8
`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);

    const captured = { Authorization: 'Bearer keep-me' };
    const dnrContext = {
      referer: '', origin: '', userAgent: '',
      idBase: 10000, ruleIds: [],
      headers: captured,
    };
    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      dnrContext,
    );

    const variantCall = ctx.fetch.mock.calls[1];
    expect(variantCall[0]).toBe('https://cdn.example.com/hi.m3u8');
    expect(variantCall[1].credentials).toBe('include');
    expect(variantCall[1].headers.Authorization).toBe('Bearer keep-me');
  });

  it('subdomain variant inherits trust from base', async () => {
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://media.example.com/v/hi.m3u8
`;
    const variant = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    setFetchSeq([ok(master), ok(variant)]);

    const dnrContext = {
      idBase: 10000, ruleIds: [],
      headers: { Authorization: 'Bearer keep-me' },
    };
    await ctx._wv2nasFetchManifestInBrowser(
      'https://example.com/master.m3u8',
      dnrContext,
    );

    const variantCall = ctx.fetch.mock.calls[1];
    expect(variantCall[1].credentials).toBe('include');
    expect(variantCall[1].headers.Authorization).toBe('Bearer keep-me');
  });

  it('parent-of-base variant (upward) is REFUSED at safety gate', async () => {
    // Master on subdomain pointing variant at the apex domain — the
    // post-Codex hardening rejects this as the "subdomain claims
    // trust over parent" attack vector. With the additional
    // adversarial-review hardening, the variant safety check now
    // refuses before fetch. Note: the master URL itself is also
    // not same-site with the page (no pageUrl in this dnrContext),
    // but the helper accepts when pageUrl is missing (back-compat).
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://example.com/v/hi.m3u8
`;
    setFetchSeq([ok(master)]);  // ONLY master fetched

    const dnrContext = {
      idBase: 10000, ruleIds: [],
      headers: { Authorization: 'Bearer secret' },
    };
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://attacker.example.com/master.m3u8',
      dnrContext,
    );

    // Variant fetch was REFUSED — only master fetched.
    expect(ctx.fetch.mock.calls).toHaveLength(1);
    expect(result).toMatchObject({
      safetyRejected: true,
      url: 'https://example.com/v/hi.m3u8',
    });
  });
});


// Codex adversarial-review (high): the browser-side manifest fetch
// runs BEFORE the server's _enforce_plan_url_safety. A forged or
// misdetected URL pointing at intranet/metadata hosts could otherwise
// be fetched with the user's cookies and forwarded as manifest_text.
// The new pre-fetch gate refuses those URLs client-side.

describe('manifest fetch URL safety gate (Codex adversarial-review)', () => {
  function setFetchSeq(responses) {
    let i = 0;
    ctx.fetch = vi.fn(async (url) => {
      const r = responses[i++];
      if (r instanceof Error) throw r;
      return r;
    });
  }

  // Most disallowed URLs: helper returns a safetyRejected sentinel so
  // runBrowserSideJob can distinguish policy refusal from transient
  // fetch failure.
  // Crucially, the assertion is `ctx.fetch was NEVER called` — the
  // privileged credentialed browser fetch must not happen.

  it('refuses unsafe master URLs before any credentialed fetch', async () => {
    const urls = [
      'http://cdn.example.com/master.m3u8',
      'https://localhost/x.m3u8',
      'https://api.localhost/x.m3u8',
      'https://127.0.0.1/x.m3u8',
      'https://10.0.0.1/x.m3u8',
      'https://172.16.5.5/x.m3u8',
      'https://172.31.255.254/x.m3u8',
      'https://192.168.1.1/x.m3u8',
      'https://169.254.169.254/latest/meta-data/iam/',
      'https://100.64.0.1/x.m3u8',
      'https://192.0.2.1/x.m3u8',
      'https://198.51.100.1/x.m3u8',
      'https://203.0.113.1/x.m3u8',
      'https://198.18.0.1/x.m3u8',
      'https://192.0.0.8/x.m3u8',
      'https://[::1]/x.m3u8',
      'https://[fe80::1]/x.m3u8',
      'https://[fec0::1]/x.m3u8',
      'https://[fc00::1]/x.m3u8',
      'https://[fd12:3456:789a::1]/x.m3u8',
      'https://[2001:db8::1]/x.m3u8',
      'https://[2001:2::1]/x.m3u8',
      'https://[::ffff:10.0.0.1]/x.m3u8',
      'https://[::ffff:198.18.0.1]/x.m3u8',
      'not a url',
    ];

    for (const url of urls) {
      setFetchSeq([]);
      const result = await ctx._wv2nasFetchManifestInBrowser(url);
      expect(result, url).toMatchObject({ safetyRejected: true });
      expect(ctx.fetch, url).not.toHaveBeenCalled();
    }
  });

  it('allows public HTTPS (sanity — DNS-resolvable hostname)', async () => {
    // 8.8.8.8 isn't excluded by any of our rules; HTTPS-only is the
    // gate. (DNS rebinding to a private IP gets stopped by cert-name
    // mismatch — different layer.)
    function ok(text) {
      return { ok: true, status: 200, text: async () => text, headers: { get: () => null } };
    }
    setFetchSeq([ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')]);
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://8.8.8.8/playlist.m3u8',
    );
    expect(result).not.toBeNull();
    expect(ctx.fetch).toHaveBeenCalledOnce();
  });

  it('refuses a malicious master that points variant at metadata service', async () => {
    // Master itself is on a public host, but the highest-bandwidth
    // variant lives on an AWS metadata IP. Master fetch goes through;
    // the variant fetch must be refused by the gate.
    function ok(text) {
      return { ok: true, status: 200, text: async () => text, headers: { get: () => null } };
    }
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://169.254.169.254/latest/meta-data/iam/credentials.m3u8
`;
    setFetchSeq([ok(master)]);  // only ONE fetch should happen
    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
    );
    expect(result).toMatchObject({
      safetyRejected: true,
      url: 'https://169.254.169.254/latest/meta-data/iam/credentials.m3u8',
    });
    // Crucially, only the master fetch happened — variant URL was
    // refused before any credentialed fetch.
    expect(ctx.fetch).toHaveBeenCalledOnce();
  });

  it('exposes _wv2nasIsManifestUrlSafeForBrowser for unit testing', () => {
    expect(typeof ctx._wv2nasIsManifestUrlSafeForBrowser).toBe('function');
    // Spot-check the boundary cases at the unit level.
    expect(ctx._wv2nasIsManifestUrlSafeForBrowser(
      'https://cdn.example.com/x.m3u8',
    )).toEqual({ safe: true });
    const localhostResult = ctx._wv2nasIsManifestUrlSafeForBrowser(
      'https://localhost/x.m3u8',
    );
    expect(localhostResult.safe).toBe(false);
    expect(ctx._wv2nasIsManifestUrlSafeForBrowser(
      'https://[2001:4860:4860::8888]/x.m3u8',
    )).toEqual({ safe: true });
  });
});


// Codex adversarial-review (high): a malicious page surfacing
// `https://internal.corp.example/...` (split-horizon DNS, internal-CA
// cert) would previously pass the safety gate because it's a public-
// looking DNS name. The browser fetch with credentials + CORS-relax
// would then read intranet content cross-origin and post it to the
// NAS as `manifest_text` BEFORE server-side `_enforce_plan_url_safety`
// runs. Tighten the gate: DNS hostnames now require a same-site
// relationship with the page that surfaced the URL.

describe('manifest URL safety: same-site requirement (Codex adversarial-review)', () => {
  it('helper covers same-site, cross-site, and back-compat branches', () => {
    const cases = [
      {
        name: 'same-origin with page',
        manifestUrl: 'https://cdn.example.com/x.m3u8',
        pageUrl: 'https://cdn.example.com/watch',
        expectedSafe: true,
      },
      {
        name: 'deeper-subdomain of page',
        manifestUrl: 'https://cdn.example.com/x.m3u8',
        pageUrl: 'https://example.com/watch',
        expectedSafe: true,
      },
      {
        name: 'different site from page',
        manifestUrl: 'https://internal.corp.example/x.m3u8',
        pageUrl: 'https://attacker.com/page',
        expectedSafe: false,
        reason: 'not same-site',
      },
      {
        name: 'upward direction to parent of page',
        manifestUrl: 'https://example.com/x.m3u8',
        pageUrl: 'https://attacker.example.com/page',
        expectedSafe: false,
      },
      {
        name: 'no pageUrl back-compat',
        manifestUrl: 'https://cdn.example.com/x.m3u8',
        pageUrl: undefined,
        expectedSafe: true,
      },
      {
        name: 'public IP literal skips same-site check',
        manifestUrl: 'https://8.8.8.8/x.m3u8',
        pageUrl: 'https://example.com/page',
        expectedSafe: true,
      },
    ];

    for (const { name, manifestUrl, pageUrl, expectedSafe, reason } of cases) {
      const result = ctx._wv2nasIsManifestUrlSafeForBrowser(manifestUrl, pageUrl);
      expect(result.safe, name).toBe(expectedSafe);
      if (reason) expect(result.reason, name).toContain(reason);
    }
  });

  it('end-to-end: cross-site master URL with pageUrl in dnrContext is REFUSED', async () => {
    function ok(text) {
      return { ok: true, status: 200, text: async () => text, headers: { get: () => null } };
    }
    let i = 0;
    const responses = [ok('#EXTM3U\n#EXTINF:10\nseg.ts\n')];
    ctx.fetch = vi.fn(async () => {
      const r = responses[i++];
      return r;
    });

    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://internal.corp.example/playlist.m3u8',
      {
        idBase: 10000, ruleIds: [],
        pageUrl: 'https://attacker.com/page',
      },
    );
    expect(result).toMatchObject({ safetyRejected: true });
    expect(ctx.fetch).not.toHaveBeenCalled();
  });

  it('end-to-end: same-site master URL passes the gate', async () => {
    function ok(text) {
      return { ok: true, status: 200, text: async () => text, headers: { get: () => null } };
    }
    ctx.fetch = vi.fn(async () => ok('#EXTM3U\n#EXTINF:10\nseg.ts\n'));

    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        idBase: 10000, ruleIds: [],
        pageUrl: 'https://example.com/watch',
      },
    );
    expect(result).not.toBeNull();
    expect(result.manifest_text).toContain('#EXTM3U');
    expect(ctx.fetch).toHaveBeenCalledOnce();
  });
});


// Codex review (P1): the safety gate validates the ORIGINAL URL,
// but `fetch()`'s default redirect:'follow' would silently chase a
// 30x to a foreign / private host with credentials:'include' and
// hand the response back to be uploaded as `manifest_text`. The
// fix sets `redirect: 'error'` so any 30x throws TypeError before
// the extension reads the body.

describe('manifest fetch: redirect: error (Codex P1)', () => {
  it('master fetch passes redirect: "error" to fetch', async () => {
    let captured = null;
    ctx.fetch = vi.fn(async (_url, opts) => {
      captured = opts;
      return {
        ok: true, status: 200,
        text: async () => '#EXTM3U\n',
        headers: { get: () => null },
      };
    });

    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        idBase: 10000, ruleIds: [],
        pageUrl: 'https://example.com/watch',
      },
    );
    expect(captured).not.toBeNull();
    expect(captured.redirect).toBe('error');
  });

  it('master fetch redirect throws → returns null (caller falls back)', async () => {
    // Simulate Chrome's behavior: fetch with redirect: 'error'
    // throws TypeError when the server returns a 30x.
    ctx.fetch = vi.fn(async () => {
      throw new TypeError('Failed to fetch (redirect refused)');
    });

    const result = await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/playlist.m3u8',
      {
        idBase: 10000, ruleIds: [],
        pageUrl: 'https://example.com/watch',
      },
    );
    expect(result).toBeNull();
  });

  it('variant fetch also passes redirect: "error"', async () => {
    function ok(text) {
      return { ok: true, status: 200, text: async () => text, headers: { get: () => null } };
    }
    const calls = [];
    const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
hi.m3u8
`;
    ctx.fetch = vi.fn(async (url, opts) => {
      calls.push({ url: String(url), opts });
      return ok(String(url).includes('hi.m3u8') ? '#EXTM3U\n#EXTINF:10\nseg.ts\n' : master);
    });

    await ctx._wv2nasFetchManifestInBrowser(
      'https://cdn.example.com/master.m3u8',
      {
        idBase: 10000, ruleIds: [],
        pageUrl: 'https://example.com/watch',
      },
    );
    // Both calls (master + variant) carry redirect: 'error'.
    expect(calls.length).toBeGreaterThanOrEqual(2);
    for (const c of calls) {
      expect(c.opts.redirect).toBe('error');
    }
  });
});


// Codex review #3: AES key URIs MUST be in the DNR coverage so key
// fetches get the same Referer/Origin/UA spoof + CORS relaxation as
// segment fetches. Without this, encrypted streams (which are exactly
// what browser-side mode is meant to handle) can pass segment fetch
// and then 403 on the key.
describe('_wv2nasPlanSegmentUrls — AES key URI inclusion', () => {
  it('collects init, media, and AES key URLs across plan shapes', () => {
    const cases = [
      {
        name: 'plan-level init_segment_url + media URLs',
        plan: {
          init_segment_url: 'https://cdn.example.com/init.mp4',
          tracks: {
            video: {
              init_segment_url: 'https://cdn.example.com/v/init.mp4',
              segments: [
                { url: 'https://cdn.example.com/v/seg0.m4s' },
                { url: 'https://cdn.example.com/v/seg1.m4s' },
              ],
            },
          },
        },
        contains: [
          'https://cdn.example.com/init.mp4',
          'https://cdn.example.com/v/init.mp4',
          'https://cdn.example.com/v/seg0.m4s',
          'https://cdn.example.com/v/seg1.m4s',
        ],
      },
      {
        name: 'deduplicated AES key URI',
        plan: {
          tracks: {
            video: {
              segments: [
                {
                  url: 'https://cdn.example.com/v/seg0.ts',
                  key: { uri: 'https://cdn.example.com/v/key.bin', method: 'AES-128' },
                },
                {
                  url: 'https://cdn.example.com/v/seg1.ts',
                  key: { uri: 'https://cdn.example.com/v/key.bin', method: 'AES-128' },
                },
              ],
            },
          },
        },
        contains: ['https://cdn.example.com/v/key.bin'],
        counts: { 'https://cdn.example.com/v/key.bin': 1 },
      },
      {
        name: 'AES key URI on a different origin from segments',
        plan: {
          tracks: {
            video: {
              segments: [{
                url: 'https://cdn.example.com/v/seg0.ts',
                key: { uri: 'https://auth.example.com/keys/abc', method: 'AES-128' },
              }],
            },
          },
        },
        contains: [
          'https://auth.example.com/keys/abc',
          'https://cdn.example.com/v/seg0.ts',
        ],
      },
      {
        name: 'plain unencrypted plan',
        plan: {
          tracks: {
            video: {
              segments: [{ url: 'https://cdn.example.com/v/seg0.ts' }],
            },
          },
        },
        exact: ['https://cdn.example.com/v/seg0.ts'],
      },
      {
        name: 'segments missing url field',
        plan: {
          tracks: {
            video: {
              segments: [
                { url: 'https://cdn.example.com/seg0.ts' },
                { /* malformed */ },
                { url: '', key: { uri: 'https://k.example.com/key' } },
              ],
            },
          },
        },
        contains: [
          'https://cdn.example.com/seg0.ts',
          'https://k.example.com/key',
        ],
        notContains: [''],
      },
    ];

    for (const { name, plan, contains = [], notContains = [], counts = {}, exact } of cases) {
      const urls = ctx._wv2nasPlanSegmentUrls(plan);
      if (exact) expect(urls, name).toEqual(exact);
      for (const url of contains) expect(urls, `${name}: ${url}`).toContain(url);
      for (const url of notContains) expect(urls, `${name}: ${url}`).not.toContain(url);
      for (const [url, count] of Object.entries(counts)) {
        expect(urls.filter((u) => u === url), `${name}: ${url}`).toHaveLength(count);
      }
    }
  });
});
