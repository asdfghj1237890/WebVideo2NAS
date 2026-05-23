// Unit tests for the v2.5 browser-side segment downloader.
//
// jsdom doesn't ship a full WebCrypto SubtleCrypto, but Node 20+ exposes
// `globalThis.crypto.subtle` from the node:crypto module — vitest hoists
// it into the jsdom environment automatically, so SubtleCrypto AES-CBC
// works in these tests.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { runJob, _internals } from '../segmentDownloader.js';

const {
  hexToBytes, ivFromSequence, withRetry, runWithConcurrency,
  KeyCache, isTrustedForCredentials, scopedRequestHeaders,
  byteRangeHeader, mediaFetchHeaders, readBodyWithCap, buildNasUrl,
  MAX_SEGMENT_BYTES, MAX_KEY_BYTES,
} = _internals;


describe('hexToBytes', () => {
  it('covers supported and rejected hex forms', () => {
    const cases = [
      { name: 'plain hex', input: '0001020304', expected: [0, 1, 2, 3, 4] },
      { name: '0x prefix', input: '0xff00', expected: [0xff, 0x00] },
      { name: 'odd length', input: 'abc', expected: null },
      { name: 'non-hex chars', input: 'zz00', expected: null },
      { name: 'empty string', input: '', expected: null },
      { name: 'null', input: null, expected: null },
    ];

    for (const { name, input, expected } of cases) {
      const out = hexToBytes(input);
      if (expected === null) {
        expect(out, name).toBeNull();
      } else {
        expect(Array.from(out), name).toEqual(expected);
      }
    }
  });
});


describe('ivFromSequence', () => {
  it('encodes sequence numbers as 16-byte big-endian IVs', () => {
    const cases = [
      {
        name: 'seq=1',
        seq: 1,
        expected: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
      },
      {
        name: 'seq=256',
        seq: 256,
        expected: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
      },
      {
        name: 'seq=2^32+1',
        seq: 0x1_0000_0001,
        expected: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1],
      },
    ];

    for (const { name, seq, expected } of cases) {
      const iv = ivFromSequence(seq);
      expect(iv.length, name).toBe(16);
      expect(Array.from(iv), name).toEqual(expected);
    }
  });
});


describe('withRetry', () => {
  it('succeeds on first try without delay', async () => {
    const fn = vi.fn().mockResolvedValue('ok');
    const result = await withRetry(fn, 'test');
    expect(result).toBe('ok');
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('retries up to MAX_RETRIES then throws', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('boom'));
    const start = Date.now();
    await expect(withRetry(fn, 'test')).rejects.toThrow(/failed after 3 attempts/);
    expect(fn).toHaveBeenCalledTimes(3);
    // Backoff: 500 + 1000 = 1500ms minimum; allow 200ms slack.
    expect(Date.now() - start).toBeGreaterThanOrEqual(1300);
  }, 10000);

  it('returns first success after transient failure', async () => {
    let calls = 0;
    const fn = vi.fn(async () => {
      calls++;
      if (calls < 2) throw new Error('transient');
      return 'recovered';
    });
    expect(await withRetry(fn, 'test')).toBe('recovered');
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('aborts on signal without retrying', async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    const fn = vi.fn();
    await expect(withRetry(fn, 'test', ctrl.signal)).rejects.toThrow(/cancelled/);
    expect(fn).not.toHaveBeenCalled();
  });
});


describe('runWithConcurrency', () => {
  it('runs all tasks and returns results in order', async () => {
    const tasks = [1, 2, 3, 4].map((n) => async () => n * 10);
    const results = await runWithConcurrency(tasks, 2);
    expect(results).toEqual([10, 20, 30, 40]);
  });

  it('honors concurrency cap', async () => {
    let active = 0;
    let maxActive = 0;
    const tasks = Array.from({ length: 10 }, () => async () => {
      active++;
      maxActive = Math.max(maxActive, active);
      await new Promise((r) => setTimeout(r, 20));
      active--;
      return active;
    });
    await runWithConcurrency(tasks, 3);
    expect(maxActive).toBeLessThanOrEqual(3);
    expect(maxActive).toBeGreaterThanOrEqual(2);
  });

  it('throws on first task failure', async () => {
    const tasks = [
      async () => 1,
      async () => { throw new Error('boom'); },
      async () => 3,
    ];
    await expect(runWithConcurrency(tasks, 2)).rejects.toThrow('boom');
  });

  it('handles empty task list', async () => {
    const results = await runWithConcurrency([], 5);
    expect(results).toEqual([]);
  });
});


describe('AES-CBC SubtleCrypto wiring (NIST F.2.1 vector)', () => {
  // NIST SP 800-38A, F.2.1 CBC-AES128.Encrypt
  // Key:        2b7e151628aed2a6abf7158809cf4f3c
  // IV:         000102030405060708090a0b0c0d0e0f
  // Plaintext:  6bc1bee22e409f96e93d7e117393172a (16 bytes — 1 block)
  // Ciphertext: 7649abac8119b246cee98e9b12e9197d
  // We verify SubtleCrypto round-trip through decrypt: encrypt the same
  // plaintext via the same algorithm and confirm we get the expected
  // ciphertext when padding is disabled (PKCS7 ciphertext on 1 block of
  // plaintext = ciphertext + an extra padding block; we verify decrypt
  // round-trip instead).
  it('decrypts ciphertext encrypted with same key', async () => {
    const key = hexToBytes('2b7e151628aed2a6abf7158809cf4f3c');
    const iv = hexToBytes('000102030405060708090a0b0c0d0e0f');
    const plaintext = hexToBytes('6bc1bee22e409f96e93d7e117393172a');

    const cryptoKey = await crypto.subtle.importKey(
      'raw', key, { name: 'AES-CBC' }, false, ['encrypt', 'decrypt'],
    );
    const ciphertext = await crypto.subtle.encrypt(
      { name: 'AES-CBC', iv }, cryptoKey, plaintext,
    );
    const recovered = await crypto.subtle.decrypt(
      { name: 'AES-CBC', iv }, cryptoKey, ciphertext,
    );
    // SubtleCrypto adds PKCS#7 padding on encrypt; decrypt strips it.
    expect(new Uint8Array(recovered)).toEqual(plaintext);
  });
});


describe('KeyCache', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  it('fetches key once and reuses cryptokey', async () => {
    const keyBytes = new Uint8Array(16);
    keyBytes.fill(0x42);
    fetch.mockResolvedValue({
      ok: true,
      arrayBuffer: async () => keyBytes.buffer,
    });
    const cache = new KeyCache();
    const k1 = await cache.getKey('https://k.example.com/k', {});
    const k2 = await cache.getKey('https://k.example.com/k', {});
    expect(k1).toBe(k2);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('throws on non-16-byte key', async () => {
    fetch.mockResolvedValue({
      ok: true,
      arrayBuffer: async () => new Uint8Array(8).buffer, // wrong size
    });
    const cache = new KeyCache();
    await expect(cache.getKey('https://k.example.com/short', {})).rejects.toThrow(/!= 16 bytes/);
  });

  it('throws on fetch failure', async () => {
    fetch.mockResolvedValue({ ok: false, status: 403 });
    const cache = new KeyCache();
    await expect(cache.getKey('https://k.example.com/forbidden', {})).rejects.toThrow(/Key fetch failed/);
  });
});


// Codex review #9: credentials must be scoped to the manifest's
// trust domain. Without this, a hostile playlist pointing segments
// at gmail.com / accounts.google.com / corp intranet would have the
// extension send the user's session cookies to those origins, then
// (with DNR CORS-relax) read the responses and upload them to NAS.
describe('isTrustedForCredentials (cross-origin trust scoping)', () => {
  // Codex adversarial-review: the previous code trusted "upward" —
  // a manifest on player.example.com authorizing credentialed reads
  // from example.com. That's exactly the attack vector: any compromised
  // or user-controlled subdomain (attacker.example.com) could weaponize
  // the extension to fetch from the parent domain with cookies.
  // The test now codifies the secure semantics: parent-domain segments
  // are NOT trusted via a subdomain manifest. Legitimate streams should
  // serve segments from same-origin or from a same-or-subdomain CDN.
  it('covers trusted, untrusted, and fail-safe URL pairs', () => {
    const cases = [
      {
        name: 'exact origin match',
        segmentUrl: 'https://example.com/v/seg.ts',
        trustedBase: 'https://example.com/master.m3u8',
        expected: true,
      },
      {
        name: 'subdomain of base',
        segmentUrl: 'https://cdn.example.com/v/seg.ts',
        trustedBase: 'https://example.com/master.m3u8',
        expected: true,
      },
      {
        name: 'base is subdomain of segment',
        segmentUrl: 'https://example.com/v/seg.ts',
        trustedBase: 'https://player.example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'subdomain manifest cannot reach parent',
        segmentUrl: 'https://example.com/auth/leak',
        trustedBase: 'https://attacker.example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'sibling subdomains are not trusted',
        segmentUrl: 'https://accounts.example.com/api/some-leak',
        trustedBase: 'https://media.example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'different registrable domain',
        segmentUrl: 'https://accounts.google.com/api/some-leak',
        trustedBase: 'https://attacker.example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'confusable suffix attack',
        segmentUrl: 'https://example.com.attacker.net/seg.ts',
        trustedBase: 'https://example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'non-http(s) scheme',
        segmentUrl: 'ftp://example.com/seg.ts',
        trustedBase: 'https://example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'null trustedBase',
        segmentUrl: 'https://example.com/seg.ts',
        trustedBase: null,
        expected: false,
      },
      {
        name: 'malformed segment URL',
        segmentUrl: 'not a url',
        trustedBase: 'https://example.com/master.m3u8',
        expected: false,
      },
      {
        name: 'same private IP host',
        segmentUrl: 'http://10.0.0.1/seg.ts',
        trustedBase: 'http://10.0.0.1/master.m3u8',
        expected: true,
      },
    ];

    for (const { name, segmentUrl, trustedBase, expected } of cases) {
      expect(isTrustedForCredentials(segmentUrl, trustedBase), name).toBe(expected);
    }
  });
});


// Codex adversarial-review (high): captured auth headers used to ride
// on EVERY segment/key/init/variant fetch unconditionally. A
// malicious or compromised manifest pointing a URI at attacker-
// controlled `evil.com` would exfiltrate the captured Authorization /
// X-* tokens. The fix: scope captured headers per-URL, mirroring the
// existing cookie scoping.

describe('scopedRequestHeaders (per-URL auth-header scoping)', () => {
  const captured = {
    'Authorization': 'Bearer site-token-XYZ',
    'X-Auth-Token': 'tok-123',
    'X-Site-Session': 'sess-abc',
  };

  it('returns captured headers only for trusted URL pairs', () => {
    const cases = [
      {
        name: 'trusted same-origin URL',
        segmentUrl: 'https://cdn.example.com/v/seg.ts',
        trustedBase: 'https://cdn.example.com/master.m3u8',
        headers: captured,
        expected: captured,
      },
      {
        name: 'trusted subdomain',
        segmentUrl: 'https://media.example.com/seg.ts',
        trustedBase: 'https://example.com/master.m3u8',
        headers: captured,
        expected: captured,
      },
      {
        name: 'untrusted foreign origin',
        segmentUrl: 'https://evil.com/exfil',
        trustedBase: 'https://cdn.example.com/master.m3u8',
        headers: captured,
        expected: {},
      },
      {
        name: 'parent-of-base upward URL',
        segmentUrl: 'https://example.com/leak',
        trustedBase: 'https://attacker.example.com/master.m3u8',
        headers: captured,
        expected: {},
      },
      {
        name: 'null requestHeaders',
        segmentUrl: 'https://cdn.example.com/seg.ts',
        trustedBase: 'https://cdn.example.com/master.m3u8',
        headers: null,
        expected: {},
      },
      {
        name: 'undefined requestHeaders',
        segmentUrl: 'https://cdn.example.com/seg.ts',
        trustedBase: 'https://cdn.example.com/master.m3u8',
        headers: undefined,
        expected: {},
      },
      {
        name: 'null trustedBase',
        segmentUrl: 'https://cdn.example.com/seg.ts',
        trustedBase: null,
        headers: captured,
        expected: {},
      },
      {
        name: 'malformed URL',
        segmentUrl: 'not a url',
        trustedBase: 'https://cdn.example.com/master.m3u8',
        headers: captured,
        expected: {},
      },
    ];

    for (const { name, segmentUrl, trustedBase, headers, expected } of cases) {
      expect(scopedRequestHeaders(segmentUrl, trustedBase, headers), name).toEqual(expected);
    }
  });

  it('strips forbidden fetch headers even for trusted URLs', () => {
    const headers = scopedRequestHeaders(
      'https://cdn.example.com/seg.ts',
      'https://cdn.example.com/master.m3u8',
      {
        Authorization: 'Bearer keep-me',
        'X-Site-Token': 'tok',
        Referer: 'https://player.example.com/watch',
        Origin: 'https://player.example.com',
        'User-Agent': 'fake-ua',
        Cookie: 'sid=secret',
        Range: 'bytes=0-1',
        'Accept-Encoding': 'gzip',
        'Sec-Fetch-Site': 'same-origin',
        'Proxy-Authorization': 'Basic secret',
      },
    );

    expect(headers.Authorization).toBe('Bearer keep-me');
    expect(headers['X-Site-Token']).toBe('tok');
    const lowercased = Object.fromEntries(
      Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v])
    );
    expect(lowercased.referer).toBeUndefined();
    expect(lowercased.origin).toBeUndefined();
    expect(lowercased['user-agent']).toBeUndefined();
    expect(lowercased.cookie).toBeUndefined();
    expect(lowercased.range).toBeUndefined();
    expect(lowercased['accept-encoding']).toBeUndefined();
    expect(lowercased['sec-fetch-site']).toBeUndefined();
    expect(lowercased['proxy-authorization']).toBeUndefined();
  });
});


describe('HLS byte ranges', () => {
  it('builds Range headers and strips captured browser Range probes', () => {
    expect(byteRangeHeader({ offset: 10, length: 5 })).toBe('bytes=10-14');

    const headers = mediaFetchHeaders(
      'https://cdn.example.com/v/seg.ts',
      'https://cdn.example.com/master.m3u8',
      {
        Authorization: 'Bearer token',
        Range: 'bytes=0-1',
        range: 'bytes=2-3',
      },
      { offset: 10, length: 5 },
    );

    expect(headers).toEqual({
      Authorization: 'Bearer token',
      Range: 'bytes=10-14',
    });
  });
});


describe('runJob argument validation', () => {
  it('rejects invalid required args', async () => {
    const cases = [
      { name: 'missing required args', args: {}, error: /missing required args/ },
      {
        name: 'plan with no tracks',
        args: {
          jobId: 'j',
          nasEndpoint: 'http://nas/',
          apiKey: 'k',
          plan: { tracks: {} },
        },
        error: /no tracks/,
      },
    ];

    for (const { name, args, error } of cases) {
      await expect(runJob(args), name).rejects.toThrow(error);
    }
  });
});


// Codex review #4: this is THE invariant for the two-generals fix.
// runJob MUST annotate any thrown error with err.finalizeAttempted so
// the caller (offscreen.js → BROWSER_JOB_FAILED → background.js) can
// decide whether the server-side abort is safe.
//   * Failure BEFORE finalize POST attempt → finalizeAttempted=false
//     → caller may call /abort to wipe staging.
//   * Failure AT-OR-AFTER finalize POST attempt → finalizeAttempted=true
//     → caller MUST NOT call /abort; the server may have committed
//       even if the client got a timeout/network error.
describe('runJob: finalizeAttempted error annotation (two-generals invariant)', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  function setFetchRouter(handler) {
    globalThis.fetch = vi.fn(async (url, opts) => {
      const u = String(url);
      const result = handler(u, opts);
      if (result instanceof Error) throw result;
      return result;
    });
  }

  function ok(text, status = 200, type = 'text/plain') {
    return {
      ok: status < 400,
      status,
      text: async () => text,
      arrayBuffer: async () => new TextEncoder().encode(text).buffer,
      headers: new Map([['content-type', type]]),
    };
  }

  it('fetches byte-range init and media with Range headers', async () => {
    setFetchRouter((url) => {
      if (url.endsWith('/init.mp4')) return ok('INIT', 206);
      if (url.endsWith('/seg0.ts')) return ok('SG', 206);
      return ok('OK', 200);
    });

    await runJob({
      jobId: '88888888-8888-8888-8888-888888888888',
      nasEndpoint: 'http://nas/',
      apiKey: 'k',
      requestHeaders: {
        Authorization: 'Bearer token',
        Range: 'bytes=0-1',
      },
      plan: {
        source_url: 'https://cdn.example.com/master.m3u8',
        init_segment_url: 'https://cdn.example.com/init.mp4',
        init_segment_byte_range: { offset: 100, length: 4 },
        tracks: {
          video: {
            init_segment_url: 'https://cdn.example.com/init.mp4',
            init_segment_byte_range: { offset: 100, length: 4 },
            segments: [{
              seq: 0,
              url: 'https://cdn.example.com/seg0.ts',
              byte_range: { offset: 20, length: 2 },
            }],
          },
        },
      },
    });

    const initFetch = fetch.mock.calls.find(([url]) => String(url).endsWith('/init.mp4'));
    const segFetch = fetch.mock.calls.find(([url]) => String(url).endsWith('/seg0.ts'));
    expect(initFetch[1].headers.Range).toBe('bytes=100-103');
    expect(segFetch[1].headers.Range).toBe('bytes=20-21');
  });

  it('annotates err.finalizeAttempted=false when failure is in segment fetch (pre-finalize)', async () => {
    // Setup: 1 segment whose fetch returns 403. The runJob should throw
    // BEFORE reaching the finalize POST.
    setFetchRouter((url) => {
      if (url.endsWith('/seg0.ts')) return ok('', 403);
      // Catch-all: any other fetch shouldn't happen in this test path.
      return ok('OK');
    });

    let caught = null;
    try {
      await runJob({
        jobId: '11111111-1111-1111-1111-111111111111',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(caught.finalizeAttempted).toBe(false);
  });

  it('annotates err.finalizeAttempted=true when finalize POST fails', async () => {
    // Setup: segment fetch + PUT all OK. Finalize POST returns 500.
    setFetchRouter((url, opts) => {
      if (url.includes('/finalize')) {
        return ok('server exploded', 500);
      }
      // PUT segment endpoint or GET segment URL — both succeed.
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: '22222222-2222-2222-2222-222222222222',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Finalize failed/);
    // CRITICAL: finalize was attempted, so the SW must NOT call abort.
    expect(caught.finalizeAttempted).toBe(true);
  });

  // Codex review #17: client retries finalize POST on transient 5xx
  // (Redis blip / queue push failure) so a single brief outage
  // doesn't strand a fully-uploaded job at browser_finalizing.
  it('retries finalize POST on 5xx and succeeds on a later attempt', async () => {
    let finalizeAttempts = 0;
    setFetchRouter((url) => {
      if (url.includes('/finalize')) {
        finalizeAttempts++;
        if (finalizeAttempts < 2) {
          return ok('redis transient blip', 503);
        }
        return ok('OK', 200);
      }
      return ok('OK', 200);
    });

    const result = await runJob({
      jobId: '99999999-1111-1111-1111-999999999999',
      nasEndpoint: 'http://nas/',
      apiKey: 'k',
      plan: {
        tracks: {
          video: {
            segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
          },
        },
      },
    });

    expect(result.jobId).toBe('99999999-1111-1111-1111-999999999999');
    // Two attempts: first 503, second 200.
    expect(finalizeAttempts).toBe(2);
  });

  it('does NOT retry finalize on 4xx (caller bug, not transient) AND downgrades finalizeAttempted', async () => {
    // Codex adversarial-review: a RECEIVED 4xx response means the
    // server got the request and rejected it pre-commit (verify
    // failures explicitly roll the job back to browser_uploading;
    // 401/403/404 short-circuit before any state change). The SW
    // must be allowed to call /abort to reclaim staging — so
    // finalizeAttempted is downgraded to false on 4xx. Reserve the
    // suppression for genuinely ambiguous cases (5xx, network err,
    // timeout) where the response could come from after rpush.
    let finalizeAttempts = 0;
    setFetchRouter((url) => {
      if (url.includes('/finalize')) {
        finalizeAttempts++;
        return ok('Job state browser_pending no longer accepts finalize', 409);
      }
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: '99999999-2222-2222-2222-999999999999',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Finalize failed \(409\)/);
    // 4xx is not transient; only one attempt.
    expect(finalizeAttempts).toBe(1);
    // RECEIVED 4xx → finalizeAttempted=false → SW will call /abort.
    expect(caught.finalizeAttempted).toBe(false);
  });

  it('gives up after 3 failed finalize attempts', async () => {
    let finalizeAttempts = 0;
    setFetchRouter((url) => {
      if (url.includes('/finalize')) {
        finalizeAttempts++;
        return ok('still down', 503);
      }
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: '99999999-3333-3333-3333-999999999999',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(finalizeAttempts).toBe(3);
    expect(caught.finalizeAttempted).toBe(true);
  }, 15000);  // longer timeout: 3 retries with backoff up to ~6s

  it('annotates err.finalizeAttempted=true when finalize POST times out (simulated)', async () => {
    // Simulates the two-generals scenario: client side sees TypeError
    // (network drop) right at finalize. We don't actually invoke the
    // timeout machinery — we throw at finalize boundary directly.
    setFetchRouter((url) => {
      if (url.includes('/finalize')) {
        return new TypeError('failed to fetch');
      }
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: '33333333-3333-3333-3333-333333333333',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    // finalizeAttempted is set BEFORE the await so even a thrown
    // TypeError from inside fetch carries the flag — the server may
    // have received the request even though we got a network error.
    expect(caught.finalizeAttempted).toBe(true);
  });

  it('finalizeAttempted=false when cancelled before finalize request is sent', async () => {
    const ctrl = new AbortController();
    let finalizeAttempts = 0;
    setFetchRouter((url) => {
      if (url.includes('/segments/')) {
        ctrl.abort();
        return ok('OK', 200);
      }
      if (url.includes('/finalize')) {
        finalizeAttempts++;
        return ok('should not happen', 200);
      }
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: '44444444-4444-4444-4444-444444444444',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        signal: ctrl.signal,
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }

    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Finalize cancelled before request/);
    expect(caught.finalizeAttempted).toBe(false);
    expect(finalizeAttempts).toBe(0);
  });

  // Codex adversarial-review: distinguish RECEIVED 4xx (server got
  // the request, rejected pre-commit) from ambiguous failures (5xx,
  // network errors). For received 4xx, the SW MUST be allowed to
  // call /abort and reclaim staging; for ambiguous, it MUST NOT
  // because the request might have committed.

  it('finalizeAttempted=false on 401 (auth pre-commit)', async () => {
    // Auth failure short-circuits before any state change — SW can
    // safely call /abort.
    setFetchRouter((url) => {
      if (url.includes('/finalize')) return ok('Unauthorized', 401);
      return ok('OK', 200);
    });
    let caught = null;
    try {
      await runJob({
        jobId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: { tracks: { video: { segments: [
          { seq: 0, url: 'https://cdn.example.com/seg0.ts' },
        ] } } },
      });
    } catch (err) { caught = err; }
    expect(caught).not.toBeNull();
    expect(caught.finalizeAttempted).toBe(false);
  });

  it('finalizeAttempted=false on 404 (job not found, no commit possible)', async () => {
    setFetchRouter((url) => {
      if (url.includes('/finalize')) return ok('Not Found', 404);
      return ok('OK', 200);
    });
    let caught = null;
    try {
      await runJob({
        jobId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: { tracks: { video: { segments: [
          { seq: 0, url: 'https://cdn.example.com/seg0.ts' },
        ] } } },
      });
    } catch (err) { caught = err; }
    expect(caught).not.toBeNull();
    expect(caught.finalizeAttempted).toBe(false);
  });

  it('finalizeAttempted=false on verify-failure 409 (server rolled back to browser_uploading)', async () => {
    // The Codex regression scenario: API's _verify_staging_complete
    // throws 409 BEFORE rpush and explicitly rolls the job back to
    // 'browser_uploading'. The SW must call /abort to clean staging.
    setFetchRouter((url) => {
      if (url.includes('/finalize')) {
        return ok(JSON.stringify({
          detail: { error: "Upload still in flight; retry finalize after current uploads complete" },
        }), 409);
      }
      return ok('OK', 200);
    });
    let caught = null;
    try {
      await runJob({
        jobId: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: { tracks: { video: { segments: [
          { seq: 0, url: 'https://cdn.example.com/seg0.ts' },
        ] } } },
      });
    } catch (err) { caught = err; }
    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Finalize failed \(409\)/);
    expect(caught.finalizeAttempted).toBe(false);
  });

  it('finalizeAttempted=true preserved across 5xx exhaustion (could-have-committed)', async () => {
    // 5xx: response could come from after rpush — request may have
    // committed server-side. Skipping abort is the safe default;
    // the worker / stale reaper handles the row.
    setFetchRouter((url) => {
      if (url.includes('/finalize')) return ok('Internal Server Error', 500);
      return ok('OK', 200);
    });
    let caught = null;
    try {
      await runJob({
        jobId: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: { tracks: { video: { segments: [
          { seq: 0, url: 'https://cdn.example.com/seg0.ts' },
        ] } } },
      });
    } catch (err) { caught = err; }
    expect(caught).not.toBeNull();
    expect(caught.finalizeAttempted).toBe(true);
  }, 15000);
});


// Codex review #18b: client-side bounded read. Without these caps a
// hostile manifest pointing at a multi-GB response would buffer the
// whole body in offscreen-document memory before the server's
// MAX_SEGMENT_BYTES rejection on PUT could trigger.
describe('readBodyWithCap (bounded streaming reader)', () => {
  function makeStreamingResp(chunks, { contentLength = null, hasArrayBuffer = false } = {}) {
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
        if (cancelled) return { value: undefined, done: true };
        if (i >= chunks.length) return { value: undefined, done: true };
        return { value: chunks[i++], done: false };
      },
      cancel() { cancelled = true; },
    };
    const body = {
      getReader: () => reader,
      cancel() { cancelled = true; },
    };
    const resp = {
      ok: true, status: 200, headers, body,
      get _cancelled() { return cancelled; },
    };
    if (hasArrayBuffer) {
      // Concat for fallback path.
      const total = chunks.reduce((s, c) => s + c.byteLength, 0);
      const merged = new Uint8Array(total);
      let off = 0;
      for (const c of chunks) { merged.set(c, off); off += c.byteLength; }
      resp.arrayBuffer = async () => merged.buffer;
    }
    return resp;
  }

  it('streams a small body within the cap and returns its bytes', async () => {
    const chunk = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]);
    const resp = makeStreamingResp([chunk]);
    const buf = await readBodyWithCap(resp, 1024, 'tiny');
    expect(new Uint8Array(buf)).toEqual(chunk);
  });

  it('rejects upfront when Content-Length declares > cap (does not even read body)', async () => {
    const chunk = new Uint8Array([1, 2, 3]);
    const resp = makeStreamingResp([chunk], { contentLength: 1024 * 1024 * 1024 });
    await expect(
      readBodyWithCap(resp, 1024, 'huge-declared'),
    ).rejects.toThrow(/Content-Length .* exceeds cap/);
    // Body was cancelled (resource freed).
    expect(resp._cancelled).toBe(true);
  });

  it('aborts mid-stream when accumulated bytes exceed cap', async () => {
    // Two chunks: first 800 bytes (under cap of 1000), second 400 bytes
    // would push total to 1200 — must trigger mid-stream abort.
    const c1 = new Uint8Array(800);
    const c2 = new Uint8Array(400);
    const resp = makeStreamingResp([c1, c2]);
    await expect(
      readBodyWithCap(resp, 1000, 'overflow-stream'),
    ).rejects.toThrow(/exceeded cap.*mid-stream/);
    expect(resp._cancelled).toBe(true);
  });

  it('falls back to arrayBuffer when body.getReader is unavailable (test mocks)', async () => {
    const data = new Uint8Array([9, 9, 9]);
    const resp = {
      ok: true, status: 200,
      headers: { get: () => null },
      // no body field
      arrayBuffer: async () => data.buffer,
    };
    const buf = await readBodyWithCap(resp, 1024, 'fallback');
    expect(new Uint8Array(buf)).toEqual(data);
  });

  it('fallback path also enforces cap post-buffering', async () => {
    const data = new Uint8Array(2048);
    const resp = {
      ok: true, status: 200,
      headers: { get: () => null },
      arrayBuffer: async () => data.buffer,
    };
    await expect(
      readBodyWithCap(resp, 1024, 'fallback-too-big'),
    ).rejects.toThrow(/size 2048 exceeds cap 1024/);
  });

  it('exposes MAX_SEGMENT_BYTES matching server-side default (500 MB)', () => {
    // Sanity: keep client cap aligned with api/main.py MAX_SEGMENT_BYTES.
    expect(MAX_SEGMENT_BYTES).toBe(500 * 1024 * 1024);
  });

  it('exposes MAX_KEY_BYTES tight enough to block a hostile key blob', () => {
    expect(MAX_KEY_BYTES).toBeLessThanOrEqual(64 * 1024);
    // Still allow normal AES-128 keys (16 bytes) plus header noise.
    expect(MAX_KEY_BYTES).toBeGreaterThanOrEqual(16);
  });
});


// Codex review #18b: integration through processOneSegment — the
// downloader must reject an oversized segment (Content-Length-declared
// or mid-stream) BEFORE attempting to PUT to NAS. Without this guard,
// a hostile manifest can exhaust the offscreen document's heap before
// server-side MAX_SEGMENT_BYTES kicks in.
describe('runJob: segment size enforcement (cap-before-buffer)', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  it('fails the job with a cap-exceeded message when segment Content-Length too big', async () => {
    let putAttempts = 0;
    globalThis.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.endsWith('/seg0.ts')) {
        // Hostile Content-Length: 1 GB
        return {
          ok: true,
          status: 200,
          headers: new Map([['content-length', String(1024 * 1024 * 1024)]]),
          body: {
            getReader: () => ({
              async read() { return { done: true }; },
              cancel() {},
            }),
            cancel() {},
          },
        };
      }
      if (u.includes('/segments/')) {
        putAttempts++;
        return { ok: true, status: 200, text: async () => 'OK', headers: new Map() };
      }
      // Default: OK
      return { ok: true, status: 200, text: async () => 'OK', headers: new Map() };
    });

    let caught = null;
    try {
      await runJob({
        jobId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        plan: {
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (err) {
      caught = err;
    }
    expect(caught).not.toBeNull();
    expect(caught.message).toMatch(/Content-Length .* exceeds cap/);
    // Critical: NO PUT to NAS — we rejected before even reading the body.
    expect(putAttempts).toBe(0);
    // Pre-finalize failure → finalizeAttempted false (caller may abort).
    expect(caught.finalizeAttempted).toBe(false);
  }, 15000);
});


// Codex review #19b: API URLs must preserve any path prefix on
// `nasEndpoint` (reverse-proxy/base-path deployments). The previous
// `new URL('/api/...', endpoint)` form discarded the prefix because
// the leading slash made the path absolute at the origin root.
describe('buildNasUrl (Codex #19b: preserve NAS endpoint path prefix)', () => {
  it('normalizes endpoints, paths, and query strings', () => {
    const cases = [
      {
        name: 'plain endpoint without prefix',
        endpoint: 'https://host',
        path: '/api/jobs/X/segments/0',
        query: { track: 'video' },
        expected: 'https://host/api/jobs/X/segments/0?track=video',
      },
      {
        name: 'base-path prefix',
        endpoint: 'https://host/webvideo2nas',
        path: '/api/jobs/X/segments/0',
        query: { track: 'video' },
        expected: 'https://host/webvideo2nas/api/jobs/X/segments/0?track=video',
      },
      {
        name: 'trailing slash stripped',
        endpoint: 'https://host/webvideo2nas/',
        path: '/api/jobs/X/finalize',
        expected: 'https://host/webvideo2nas/api/jobs/X/finalize',
      },
      {
        name: 'multiple trailing slashes stripped',
        endpoint: 'https://host///',
        path: '/api/jobs/X/finalize',
        expected: 'https://host/api/jobs/X/finalize',
      },
      {
        name: 'empty query omitted',
        endpoint: 'https://host',
        path: '/api/jobs/X/finalize',
        query: {},
        expected: 'https://host/api/jobs/X/finalize',
      },
      {
        name: 'undefined query omitted',
        endpoint: 'https://host',
        path: '/api/jobs/X/finalize',
        expected: 'https://host/api/jobs/X/finalize',
      },
      {
        name: 'null and undefined query values skipped',
        endpoint: 'https://host',
        path: '/api/jobs/X/seg/0',
        query: { track: 'audio', skip: null, missing: undefined },
        expected: 'https://host/api/jobs/X/seg/0?track=audio',
      },
      {
        name: 'query values URL-encoded',
        endpoint: 'https://host',
        path: '/api/jobs/X/seg/0',
        query: { note: 'a b&c=d' },
        expected: 'https://host/api/jobs/X/seg/0?note=a+b%26c%3Dd',
      },
      {
        name: 'missing leading slash added',
        endpoint: 'https://host',
        path: 'api/jobs/X/finalize',
        expected: 'https://host/api/jobs/X/finalize',
      },
      {
        name: 'existing path query preserved',
        endpoint: 'https://host',
        path: '/api/jobs/X/seg/0?existing=1',
        query: { track: 'video' },
        expected: 'https://host/api/jobs/X/seg/0?existing=1&track=video',
      },
      {
        name: 'endpoint with port',
        endpoint: 'http://192.168.1.50:52052',
        path: '/api/jobs/X/finalize',
        expected: 'http://192.168.1.50:52052/api/jobs/X/finalize',
      },
      {
        name: 'port plus base path',
        endpoint: 'http://192.168.1.50:52052/wv2nas',
        path: '/api/jobs/X/segments/3',
        query: { track: 'video' },
        expected: 'http://192.168.1.50:52052/wv2nas/api/jobs/X/segments/3?track=video',
      },
    ];

    for (const { name, endpoint, path, query, expected } of cases) {
      const actual = query === undefined
        ? buildNasUrl(endpoint, path)
        : buildNasUrl(endpoint, path, query);
      expect(actual, name).toBe(expected);
    }
  });
});


// Integration: verify uploadSegment / uploadInit / finalize all hit
// the prefixed path. End-to-end check of the wiring (a regression
// where one site forgets to use buildNasUrl would surface here).
describe('runJob: URLs preserve NAS endpoint base path', () => {
  it('uploadSegment / uploadInit / finalize all include the base-path prefix', async () => {
    const observed = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      observed.push({ url: String(url), method: opts && opts.method });
      const u = String(url);
      // Segment URL fetched from CDN (not NAS) — return small body.
      if (u.startsWith('https://cdn.example.com/')) {
        return {
          ok: true, status: 200,
          headers: new Map([['content-length', '4']]),
          body: {
            getReader: (() => {
              let done = false;
              return () => ({
                async read() {
                  if (done) return { done: true };
                  done = true;
                  return { value: new Uint8Array([1, 2, 3, 4]), done: false };
                },
                cancel() {},
              });
            })(),
            cancel() {},
          },
        };
      }
      // PUT init / segment / POST finalize on NAS — all return ok.
      return { ok: true, status: 200, text: async () => 'OK', headers: new Map() };
    });

    await runJob({
      jobId: 'jobX',
      nasEndpoint: 'https://host/webvideo2nas',
      apiKey: 'k',
      plan: {
        tracks: {
          video: {
            init_segment_url: 'https://cdn.example.com/init.mp4',
            segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts' }],
          },
        },
      },
    });

    const initPut = observed.find((c) => c.method === 'PUT' && c.url.includes('/init'));
    const segPut = observed.find((c) => c.method === 'PUT' && c.url.includes('/segments/'));
    const finalize = observed.find((c) => c.method === 'POST' && c.url.includes('/finalize'));

    expect(initPut).toBeDefined();
    expect(segPut).toBeDefined();
    expect(finalize).toBeDefined();

    // CRITICAL: every NAS-bound URL preserves the /webvideo2nas prefix.
    expect(initPut.url).toBe('https://host/webvideo2nas/api/jobs/jobX/init?track=video');
    expect(segPut.url).toBe('https://host/webvideo2nas/api/jobs/jobX/segments/0?track=video');
    expect(finalize.url).toBe('https://host/webvideo2nas/api/jobs/jobX/finalize');
  });
});


// Codex review (P2): the previous fetch helper cleared the abort timer
// as soon as headers arrived, leaving body reads unbounded. A CDN that
// stalls mid-body would pin one concurrency worker forever and the
// whole job would never reach finalize/abort. The new
// fetchBytesWithTimeout keeps both the fetch AND the body read inside
// a single timer scope.

describe('fetchBytesWithTimeout: timeout covers body read', () => {
  const { fetchWithTimeout, fetchBytesWithTimeout } = _internals;

  function makeStreamingResp({ chunks = [], stallForever = false } = {}) {
    let cancelled = false;
    let i = 0;
    const reader = {
      async read() {
        if (cancelled) {
          throw new Error('reader cancelled');
        }
        if (stallForever) {
          // Return a never-resolving promise — but it must reject
          // when the body is cancelled. Use a manual deferred.
          return new Promise((_resolve, reject) => {
            const tick = setInterval(() => {
              if (cancelled) {
                clearInterval(tick);
                reject(new Error('reader cancelled'));
              }
            }, 5);
          });
        }
        if (i >= chunks.length) return { value: undefined, done: true };
        return { value: chunks[i++], done: false };
      },
      cancel() { cancelled = true; },
    };
    const body = {
      getReader: () => reader,
      cancel() { cancelled = true; },
    };
    return {
      ok: true, status: 200,
      headers: { get: () => null },
      body,
      get _cancelled() { return cancelled; },
    };
  }

  it('happy path: returns {resp, bytes}', async () => {
    const chunk = new Uint8Array([1, 2, 3]);
    const fakeFetch = vi.fn(async () => makeStreamingResp({ chunks: [chunk] }));
    // Inject our fetch via a module-level shim. The implementation
    // calls global `fetch` — we monkeypatch it for the duration.
    const origFetch = globalThis.fetch;
    globalThis.fetch = fakeFetch;
    try {
      const result = await fetchBytesWithTimeout(
        'https://x/seg', {}, 5000, null, 1024, 'seg',
      );
      expect(result.resp.ok).toBe(true);
      expect(new Uint8Array(result.bytes)).toEqual(chunk);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('non-ok response: returns bytes=null and cancels body to free socket', async () => {
    let bodyCancelled = false;
    const fakeFetch = vi.fn(async () => ({
      ok: false, status: 403,
      headers: { get: () => null },
      body: { cancel: () => { bodyCancelled = true; } },
    }));
    const origFetch = globalThis.fetch;
    globalThis.fetch = fakeFetch;
    try {
      const { resp, bytes } = await fetchBytesWithTimeout(
        'https://x/seg', {}, 5000, null, 1024, 'seg',
      );
      expect(resp.status).toBe(403);
      expect(bytes).toBeNull();
      expect(bodyCancelled).toBe(true);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('body stalls forever: timer fires, reader gets cancelled, caller gets error', async () => {
    // Use a short timeout (50 ms) so the test doesn't wait long.
    const stalled = makeStreamingResp({ stallForever: true });
    const origFetch = globalThis.fetch;
    // Real Chrome fetch propagates AbortController.abort() to the
    // response body's stream — when abort fires, reader.read()
    // rejects. Our mock has to do this explicitly: listen for
    // signal-abort and cancel the body so the polling reader breaks
    // out.
    globalThis.fetch = vi.fn(async (_url, opts) => {
      if (opts && opts.signal) {
        if (opts.signal.aborted) {
          stalled.body.cancel();
        } else {
          opts.signal.addEventListener('abort', () => stalled.body.cancel());
        }
      }
      return stalled;
    });
    try {
      const start = Date.now();
      let caught = null;
      try {
        await fetchBytesWithTimeout(
          'https://x/seg', {}, 100, null, 1024, 'seg',
        );
      } catch (err) {
        caught = err;
      }
      const elapsed = Date.now() - start;
      // Reader must have been cancelled by the timeout firing.
      expect(stalled._cancelled).toBe(true);
      // Caught error: expect SOMETHING (cancellation throw).
      expect(caught).not.toBeNull();
      // Test sanity: didn't hang anywhere near the test timeout.
      expect(elapsed).toBeLessThan(2000);
    } finally {
      globalThis.fetch = origFetch;
    }
  }, 5000);

  it('external signal abort cancels both fetch and body read', async () => {
    const stalled = makeStreamingResp({ stallForever: true });
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (_url, opts) => {
      if (opts && opts.signal) {
        if (opts.signal.aborted) {
          stalled.body.cancel();
        } else {
          opts.signal.addEventListener('abort', () => stalled.body.cancel());
        }
      }
      return stalled;
    });
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 80);
    try {
      let caught = null;
      try {
        await fetchBytesWithTimeout(
          'https://x/seg', {}, 60000, ctrl.signal, 1024, 'seg',
        );
      } catch (err) {
        caught = err;
      }
      expect(stalled._cancelled).toBe(true);
      expect(caught).not.toBeNull();
    } finally {
      globalThis.fetch = origFetch;
    }
  }, 5000);

  it('fetchWithTimeout starts aborted when external signal is already aborted', async () => {
    const origFetch = globalThis.fetch;
    const ctrl = new AbortController();
    ctrl.abort();
    globalThis.fetch = vi.fn(async (_url, opts) => {
      expect(opts.signal.aborted).toBe(true);
      throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    });
    try {
      await expect(
        fetchWithTimeout('https://x/upload', {}, 60000, ctrl.signal),
      ).rejects.toThrow('aborted');
      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('fetchBytesWithTimeout starts aborted when external signal is already aborted', async () => {
    const origFetch = globalThis.fetch;
    const ctrl = new AbortController();
    ctrl.abort();
    globalThis.fetch = vi.fn(async (_url, opts) => {
      expect(opts.signal.aborted).toBe(true);
      throw Object.assign(new Error('aborted'), { name: 'AbortError' });
    });
    try {
      await expect(
        fetchBytesWithTimeout('https://x/seg', {}, 60000, ctrl.signal, 1024, 'seg'),
      ).rejects.toThrow('aborted');
      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('exposes the helper via _internals', () => {
    expect(typeof fetchBytesWithTimeout).toBe('function');
  });
});


// Codex adversarial-review (high) — end-to-end: drive runJob with a
// plan that mixes trusted (same-origin) and untrusted (foreign-origin)
// URLs. Verify the captured Authorization / X-* headers are sent ONLY
// to the trusted hosts. Untrusted segment / init / key URLs receive
// an empty header set.

describe('runJob: per-URL captured-header scoping (Codex adversarial-review)', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  function ok(text, status = 200, type = 'text/plain') {
    return {
      ok: status < 400,
      status,
      text: async () => text,
      arrayBuffer: async () => new TextEncoder().encode(text).buffer,
      headers: new Map([['content-type', type]]),
    };
  }

  it('untrusted segment URL receives NO captured Authorization header', async () => {
    const captured = {
      'Authorization': 'Bearer site-token-XYZ',
      'X-Auth-Token': 'tok-123',
    };
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), headers: opts && opts.headers });
      // For PUT (upload) and finalize: succeed.
      if (String(url).includes('/api/jobs/')) return ok('{}', 200);
      // For segment GETs: succeed regardless of host.
      return ok('seg-bytes', 200);
    });

    try {
      await runJob({
        jobId: '99999999-aaaa-aaaa-aaaa-999999999999',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        requestHeaders: captured,
        plan: {
          // Trust boundary: cdn.example.com (same as trusted segment).
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              segments: [
                // Trusted (same origin).
                { url: 'https://cdn.example.com/seg0.ts' },
                // UNTRUSTED — foreign origin.
                { url: 'https://evil.example.org/seg1.ts' },
              ],
            },
          },
        },
      });
    } catch (_e) { /* upload PUT path is ok-stubbed; ignore any aborts */ }

    // Find each segment fetch.
    const trustedFetch = recorded.find(
      (r) => r.url === 'https://cdn.example.com/seg0.ts',
    );
    const untrustedFetch = recorded.find(
      (r) => r.url === 'https://evil.example.org/seg1.ts',
    );
    expect(trustedFetch).toBeDefined();
    expect(untrustedFetch).toBeDefined();
    // Trusted: captured Authorization rides along.
    expect(trustedFetch.headers.Authorization).toBe('Bearer site-token-XYZ');
    expect(trustedFetch.headers['X-Auth-Token']).toBe('tok-123');
    // Untrusted: NEITHER token is sent.
    expect(untrustedFetch.headers.Authorization).toBeUndefined();
    expect(untrustedFetch.headers['X-Auth-Token']).toBeUndefined();
    // Empty object is the expected shape.
    expect(Object.keys(untrustedFetch.headers || {})).toEqual([]);
  });

  it('untrusted AES key URI receives NO captured Authorization header', async () => {
    // The most insidious vector: AES key URIs are fetched with creds
    // AND the response is used as decrypt material. A malicious
    // manifest sending Authorization tokens to an attacker key host
    // is a credential-exfiltration channel.
    const captured = { 'Authorization': 'Bearer secret' };
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), headers: opts && opts.headers });
      const u = String(url);
      // AES key — return 16 bytes so KeyCache resolves.
      if (u.endsWith('/k1')) {
        return {
          ok: true, status: 200,
          arrayBuffer: async () => new Uint8Array(16).buffer,
          text: async () => '',
          headers: new Map(),
        };
      }
      if (u.includes('/api/jobs/')) return ok('{}', 200);
      return ok('seg-bytes', 200);
    });

    try {
      await runJob({
        jobId: '99999999-bbbb-bbbb-bbbb-999999999999',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        requestHeaders: captured,
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              segments: [{
                url: 'https://cdn.example.com/seg0.ts',
                key: { uri: 'https://evil.example.org/k1', method: 'AES-128' },
              }],
            },
          },
        },
      });
    } catch (_e) { /* expected: decrypt fails on garbage key, that's fine */ }

    const keyFetch = recorded.find(
      (r) => r.url === 'https://evil.example.org/k1',
    );
    expect(keyFetch).toBeDefined();
    // Captured Authorization MUST NOT have been sent to the foreign
    // key host.
    expect(keyFetch.headers && keyFetch.headers.Authorization).toBeUndefined();
  });

  it('untrusted init segment URL receives NO captured Authorization header', async () => {
    const captured = { 'Authorization': 'Bearer secret', 'X-Token': 'abc' };
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), headers: opts && opts.headers });
      if (String(url).includes('/api/jobs/')) return ok('{}', 200);
      return ok('init-bytes', 200);
    });

    try {
      await runJob({
        jobId: '99999999-cccc-cccc-cccc-999999999999',
        nasEndpoint: 'http://nas/',
        apiKey: 'k',
        requestHeaders: captured,
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              // Init segment on a FOREIGN origin.
              init_segment_url: 'https://evil.example.org/init.mp4',
              segments: [{ url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (_e) { /* ignore */ }

    const initFetch = recorded.find(
      (r) => r.url === 'https://evil.example.org/init.mp4',
    );
    expect(initFetch).toBeDefined();
    expect(initFetch.headers && initFetch.headers.Authorization).toBeUndefined();
    expect(initFetch.headers && initFetch.headers['X-Token']).toBeUndefined();
  });
});


// Codex review (P1): browser-side fetches must refuse to follow
// redirects. The trust decision is made for the ORIGINAL URL; a
// 30x to a foreign / private host bypasses that boundary and would
// leak captured auth headers + cookies to the redirect target. We
// pass `redirect: 'error'` to fetch so any 30x throws TypeError.

describe('runJob: redirect: error guard (Codex P1)', () => {
  let originalFetch;
  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  function ok(text, status = 200) {
    return {
      ok: status < 400, status,
      text: async () => text,
      arrayBuffer: async () => new TextEncoder().encode(text).buffer,
      headers: new Map([['content-type', 'application/octet-stream']]),
    };
  }

  it('segment fetch passes redirect: "error" to fetch', async () => {
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), opts: opts || {} });
      if (String(url).includes('/api/jobs/')) return ok('{}', 200);
      return ok('seg-bytes', 200);
    });

    try {
      await runJob({
        jobId: 'aaaaaaaa-redir-redir-redir-aaaaaaaaaaaa',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              segments: [{ url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (_e) { /* upload PUT ok-stubbed; ignore */ }

    const segFetch = recorded.find(
      (r) => r.url === 'https://cdn.example.com/seg0.ts',
    );
    expect(segFetch).toBeDefined();
    expect(segFetch.opts.redirect).toBe('error');
  });

  it('init segment fetch passes redirect: "error"', async () => {
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), opts: opts || {} });
      if (String(url).includes('/api/jobs/')) return ok('{}', 200);
      return ok('init-bytes', 200);
    });

    try {
      await runJob({
        jobId: 'bbbbbbbb-redir-redir-redir-bbbbbbbbbbbb',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              init_segment_url: 'https://cdn.example.com/init.mp4',
              segments: [{ url: 'https://cdn.example.com/seg0.ts' }],
            },
          },
        },
      });
    } catch (_e) { /* ignore */ }

    const initFetch = recorded.find(
      (r) => r.url === 'https://cdn.example.com/init.mp4',
    );
    expect(initFetch).toBeDefined();
    expect(initFetch.opts.redirect).toBe('error');
  });

  it('AES key fetch passes redirect: "error"', async () => {
    const recorded = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      recorded.push({ url: String(url), opts: opts || {} });
      const u = String(url);
      // AES key — return 16 bytes for KeyCache.
      if (u.endsWith('/k1')) {
        return {
          ok: true, status: 200,
          arrayBuffer: async () => new Uint8Array(16).buffer,
          text: async () => '',
          headers: new Map(),
        };
      }
      if (u.includes('/api/jobs/')) return ok('{}', 200);
      return ok('seg-bytes', 200);
    });

    try {
      await runJob({
        jobId: 'cccccccc-redir-redir-redir-cccccccccccc',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              segments: [{
                url: 'https://cdn.example.com/seg0.ts',
                key: { uri: 'https://cdn.example.com/k1', method: 'AES-128' },
              }],
            },
          },
        },
      });
    } catch (_e) { /* decrypt fails on garbage key; ignore */ }

    const keyFetch = recorded.find(
      (r) => r.url === 'https://cdn.example.com/k1',
    );
    expect(keyFetch).toBeDefined();
    expect(keyFetch.opts.redirect).toBe('error');
  });

  it('segment fetch with redirect rejection surfaces sanitized URL context', async () => {
    // Simulate Chrome behavior: fetch throws TypeError when the
    // server returns 30x with redirect:'error'.
    globalThis.fetch = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/seg0.ts')) {
        throw new TypeError('Failed to fetch (redirect refused)');
      }
      if (u.includes('/api/jobs/')) return ok('{}', 200);
      return ok('OK', 200);
    });

    let caught = null;
    try {
      await runJob({
        jobId: 'dddddddd-redir-redir-redir-dddddddddddd',
        nasEndpoint: 'http://nas/', apiKey: 'k',
        plan: {
          source_url: 'https://cdn.example.com/master.m3u8',
          tracks: {
            video: {
              segments: [{ seq: 0, url: 'https://cdn.example.com/seg0.ts?token=secret' }],
            },
          },
        },
      });
    } catch (err) { caught = err; }
    expect(caught).not.toBeNull();
    expect(caught.message).toContain('https://cdn.example.com/seg0.ts');
    expect(caught.message).not.toContain('token=secret');
  }, 15000);
});


// runJob's onProgress callback feeds the offscreen → SW → sidepanel
// progress pipeline. NAS API doesn't track upload-phase progress for
// browser-side jobs; the extension is the source of truth. The hook
// must:
//   - fire exactly once per MEDIA segment (init segments excluded)
//   - report monotonically-increasing `done` from 1..N
//   - report `total` = number of media segments scheduled
//   - tolerate callback exceptions (must never abort the upload)

describe('runJob: onProgress callback (browser-side progress pipeline)', () => {
  let originalFetch;
  beforeEach(() => { originalFetch = globalThis.fetch; });

  function ok(text, status = 200, type = 'text/plain') {
    return {
      ok: status < 400, status,
      text: async () => text,
      arrayBuffer: async () => new TextEncoder().encode(text).buffer,
      headers: new Map([['content-type', type]]),
    };
  }
  function setFetchRouter(handler) {
    globalThis.fetch = vi.fn(async (url, opts) => {
      const u = String(url);
      const result = handler(u, opts);
      if (result instanceof Error) throw result;
      return result;
    });
  }

  it('reports done=1..N, total=N for a single-track plan', async () => {
    setFetchRouter(() => ok('OK'));
    const events = [];
    await runJob({
      jobId: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
      nasEndpoint: 'http://nas/', apiKey: 'k',
      plan: {
        source_url: 'https://cdn.example.com/m.m3u8',
        tracks: {
          video: {
            segments: [
              { url: 'https://cdn.example.com/s0.ts' },
              { url: 'https://cdn.example.com/s1.ts' },
              { url: 'https://cdn.example.com/s2.ts' },
            ],
          },
        },
      },
      onProgress: (info) => events.push(info),
      concurrency: 1,  // serialize so order is deterministic
    });
    expect(events).toHaveLength(3);
    expect(events.map(e => e.done)).toEqual([1, 2, 3]);
    expect(events.every(e => e.total === 3)).toBe(true);
    expect(events.map(e => e.track)).toEqual(['video', 'video', 'video']);
  });

  it('total counts MEDIA segments only (init segments excluded)', async () => {
    setFetchRouter(() => ok('OK'));
    const events = [];
    await runJob({
      jobId: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
      nasEndpoint: 'http://nas/', apiKey: 'k',
      plan: {
        source_url: 'https://cdn.example.com/m.m3u8',
        init_segment_url: 'https://cdn.example.com/init.mp4',
        tracks: {
          video: {
            init_segment_url: 'https://cdn.example.com/init.mp4',
            segments: [
              { url: 'https://cdn.example.com/s0.ts' },
              { url: 'https://cdn.example.com/s1.ts' },
            ],
          },
        },
      },
      onProgress: (info) => events.push(info),
      concurrency: 1,
    });
    // 2 media segments scheduled; init segment was processed but not counted.
    expect(events.map(e => e.done)).toEqual([1, 2]);
    expect(events.every(e => e.total === 2)).toBe(true);
  });

  it('counts segments across multiple tracks (video + audio)', async () => {
    setFetchRouter(() => ok('OK'));
    const events = [];
    await runJob({
      jobId: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
      nasEndpoint: 'http://nas/', apiKey: 'k',
      plan: {
        source_url: 'https://cdn.example.com/m.m3u8',
        tracks: {
          video: { segments: [
            { url: 'https://cdn.example.com/v0.ts' },
            { url: 'https://cdn.example.com/v1.ts' },
          ] },
          audio: { segments: [
            { url: 'https://cdn.example.com/a0.ts' },
          ] },
        },
      },
      onProgress: (info) => events.push(info),
      concurrency: 1,
    });
    expect(events).toHaveLength(3);
    expect(events.every(e => e.total === 3)).toBe(true);
    // done values across tracks form a contiguous 1..N sequence (some
    // ordering of [1,2,3]) — total events match scheduled segments.
    expect(new Set(events.map(e => e.done))).toEqual(new Set([1, 2, 3]));
  });

  it('callback exception does not abort upload (next segment still runs)', async () => {
    setFetchRouter(() => ok('OK'));
    let secondEventReceived = false;
    await runJob({
      jobId: 'dddddddd-dddd-dddd-dddd-dddddddddddd',
      nasEndpoint: 'http://nas/', apiKey: 'k',
      plan: {
        source_url: 'https://cdn.example.com/m.m3u8',
        tracks: {
          video: {
            segments: [
              { url: 'https://cdn.example.com/s0.ts' },
              { url: 'https://cdn.example.com/s1.ts' },
            ],
          },
        },
      },
      onProgress: ({ done }) => {
        if (done === 1) throw new Error('callback bug');
        if (done === 2) secondEventReceived = true;
      },
      concurrency: 1,
    });
    expect(secondEventReceived).toBe(true);  // upload completed despite throw
  });

  it('omitting onProgress is fine — runJob runs to completion', async () => {
    setFetchRouter(() => ok('OK'));
    await expect(runJob({
      jobId: 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
      nasEndpoint: 'http://nas/', apiKey: 'k',
      plan: {
        source_url: 'https://cdn.example.com/m.m3u8',
        tracks: {
          video: {
            segments: [{ url: 'https://cdn.example.com/s0.ts' }],
          },
        },
      },
      // no onProgress
      concurrency: 1,
    })).resolves.toBeDefined();
  });
});
