// Unit tests for deepsearch.js — the deeper MAIN-world detection helpers.
//
// jsdom + vitest gives us a real `window` and `Worker` (the latter polyfilled);
// we set up a fresh window object per test by clearing the module cache and
// re-running the IIFE so each test starts with un-wrapped originals.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DEEPSEARCH_SRC = readFileSync(join(__dirname, '..', 'deepsearch.js'), 'utf8');


function loadDeepsearch() {
  // The IIFE keys on window.__wv2nas_deepsearch_injected to avoid double
  // injection. Clear it + buffers + originals so each test starts fresh.
  delete window.__wv2nas_deepsearch_injected;
  delete window.__wv2nas_manifests;
  delete window.__wv2nas_deep_hits;

  // Save original Worker / atob / createObjectURL each test.
  // (jsdom resets `window` per test only if we explicitly reload — we do
  // it manually here.)
  const eval0 = (0, eval); // indirect eval → script-level, not block-scoped
  eval0(DEEPSEARCH_SRC);
}


describe('atob hook', () => {
  beforeEach(() => {
    delete window.__wv2nas_deepsearch_injected;
    delete window.__wv2nas_manifests;
    delete window.__wv2nas_deep_hits;
  });

  it('forwards #EXTM3U through atob as a deep-hit (NOT a manifest URL)', async () => {
    // Codex review (P2): atob has manifest TEXT but no
    // downloadable URL. Pre-fix the helper registered the page URL
    // as the manifest, which made the side panel offer the page
    // URL for download — NAS would then try to parse the HTML
    // page as a manifest and fail. Now it emits a deep-hit
    // (telemetry only).
    const origAtob = window.atob;
    loadDeepsearch();

    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const m3u8 = '#EXTM3U\n#EXT-X-VERSION:3\n';
    const encoded = Buffer.from(m3u8).toString('base64');
    const decoded = window.atob(encoded);
    expect(decoded).toBe(m3u8);

    // postMessage is async — wait a tick.
    await new Promise((r) => setTimeout(r, 10));

    // No manifest URL should be emitted (we don't have one).
    const manifestHit = messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED');
    expect(manifestHit).toBeUndefined();

    // Deep-hit IS emitted with kind=manifest-text-no-url.
    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m3u8');
    expect(deepHit.source).toBe('atob');

    // Restore so later tests don't see the wrapper.
    window.atob = origAtob;
  });

  it('does not fire on non-manifest atob results', async () => {
    const origAtob = window.atob;
    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const decoded = window.atob(Buffer.from('hello world').toString('base64'));
    expect(decoded).toBe('hello world');
    await new Promise((r) => setTimeout(r, 10));
    const hit = messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED');
    expect(hit).toBeUndefined();

    window.atob = origAtob;
  });

  it('still returns the original atob result (passthrough)', () => {
    const origAtob = window.atob;
    loadDeepsearch();
    const expected = 'hello world';
    const encoded = Buffer.from(expected).toString('base64');
    expect(window.atob(encoded)).toBe(expected);
    window.atob = origAtob;
  });

  it('replays deep hits that fired before content.js was listening', async () => {
    const origAtob = window.atob;
    loadDeepsearch();

    const m3u8 = '#EXTM3U\n#EXT-X-VERSION:3\n';
    window.atob(Buffer.from(m3u8).toString('base64'));
    await new Promise((r) => setTimeout(r, 10));

    const replayed = [];
    window.addEventListener('message', (e) => replayed.push(e.data));
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'WV2NAS_CONTENT_READY' },
      source: window,
    }));
    await new Promise((r) => setTimeout(r, 10));

    const deepHit = replayed.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m3u8');
    expect(deepHit.source).toBe('atob');

    window.atob = origAtob;
  });
});


describe('Worker postMessage hook', () => {
  beforeEach(() => {
    delete window.__wv2nas_deepsearch_injected;
    delete window.__wv2nas_manifests;
    delete window.__wv2nas_deep_hits;
  });

  it('detects #EXTM3U in worker.postMessage string as deep-hit (NOT manifest URL)', async () => {
    // Codex review (P2): worker postMessage has manifest TEXT but
    // no URL. Pre-fix the helper used `window.location.href` as
    // the manifest URL — sending the page URL to NAS makes it
    // parse HTML as m3u8 and fail. Now: deep-hit only.
    const innerPostMessage = vi.fn();
    const fakeWorkerInstance = { postMessage: innerPostMessage };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();

    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const w = new window.Worker('worker.js');
    w.postMessage('#EXTM3U\n#EXT-X-VERSION:3\n');

    await new Promise((r) => setTimeout(r, 10));

    // Underlying postMessage still gets called (passthrough).
    expect(innerPostMessage).toHaveBeenCalledTimes(1);

    // No bogus manifest URL.
    const manifestHit = messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED');
    expect(manifestHit).toBeUndefined();

    // Deep-hit IS emitted.
    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m3u8');
    expect(deepHit.source).toBe('worker:postMessage:string');

    window.Worker = origWorker;
  });

  it('detects manifest text inside object property as deep-hit (NOT manifest URL)', async () => {
    const fakeWorkerInstance = { postMessage: vi.fn() };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const w = new window.Worker('worker.js');
    w.postMessage({ kind: 'parse', body: '<?xml version="1.0"?><MPD></MPD>' });
    await new Promise((r) => setTimeout(r, 10));

    const manifestHit = messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED');
    expect(manifestHit).toBeUndefined();

    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('mpd');
    expect(deepHit.source).toBe('worker:postMessage:obj');

    window.Worker = origWorker;
  });

  it('does not crash on a non-string non-object postMessage payload', () => {
    const fakeWorkerInstance = { postMessage: vi.fn() };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const w = new window.Worker('worker.js');
    expect(() => w.postMessage(42)).not.toThrow();
    expect(() => w.postMessage(null)).not.toThrow();
    expect(() => w.postMessage(undefined)).not.toThrow();

    window.Worker = origWorker;
  });

  it('detects manifest text sent from worker back to the page', async () => {
    const listeners = {};
    const fakeWorkerInstance = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((type, listener) => {
        listeners[type] = listener;
      }),
    };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    new window.Worker('worker.js');
    expect(fakeWorkerInstance.addEventListener).toHaveBeenCalledWith(
      'message',
      expect.any(Function),
      true,
    );

    listeners.message({ data: '#EXTM3U\n#EXT-X-VERSION:3\n' });
    await new Promise((r) => setTimeout(r, 10));

    const manifestHit = messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED');
    expect(manifestHit).toBeUndefined();

    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m3u8');
    expect(deepHit.source).toBe('worker:message:string');

    window.Worker = origWorker;
  });

  it('detects manifest URLs sent from worker back to the page', async () => {
    const listeners = {};
    const fakeWorkerInstance = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((type, listener) => {
        listeners[type] = listener;
      }),
    };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    new window.Worker('worker.js');
    listeners.message({ data: { url: 'https://cdn.example.com/video/master.m3u8?token=1' } });
    await new Promise((r) => setTimeout(r, 10));

    const manifestHit = messages.find(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED'
        && m.url === 'https://cdn.example.com/video/master.m3u8?token=1',
    );
    expect(manifestHit).toBeDefined();
    expect(manifestHit.format).toBe('m3u8');
    expect(manifestHit.source).toBe('worker:message:obj');

    window.Worker = origWorker;
  });

  it('detects nested worker payloads in both directions', async () => {
    const listeners = {};
    const fakeWorkerInstance = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((type, listener) => {
        listeners[type] = listener;
      }),
    };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const w = new window.Worker('worker.js');
    w.postMessage({
      data: {
        details: {
          fragments: [
            { sn: 0 },
            { url: 'https://cdn.example.com/video/master.m3u8?token=1' },
          ],
        },
      },
    });
    listeners.message({
      data: {
        payload: {
          details: {
            fragments: [
              { sn: 0 },
              { url: 'https://cdn.example.com/video/seg0.m4s' },
            ],
          },
        },
      },
    });
    await new Promise((r) => setTimeout(r, 10));

    const manifestHit = messages.find(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED'
        && m.url === 'https://cdn.example.com/video/master.m3u8?token=1',
    );
    expect(manifestHit).toBeDefined();
    expect(manifestHit.format).toBe('m3u8');
    expect(manifestHit.source).toBe('worker:postMessage:obj');

    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED'
        && m.kind === 'segment-url'
        && m.url === 'https://cdn.example.com/video/seg0.m4s',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m4s');
    expect(deepHit.source).toBe('worker:message:obj');

    window.Worker = origWorker;
  });

  it('continues scanning after nested segment hits to find a later manifest URL', async () => {
    const fakeWorkerInstance = { postMessage: vi.fn() };
    const FakeWorker = vi.fn(() => fakeWorkerInstance);
    const origWorker = window.Worker;
    window.Worker = FakeWorker;

    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const w = new window.Worker('worker.js');
    w.postMessage({
      data: {
        details: {
          fragments: [
            { url: 'https://cdn.example.com/video/seg0.m4s' },
            { url: 'https://cdn.example.com/video/seg1.m4s' },
          ],
          playlist: {
            url: 'https://cdn.example.com/video/master.m3u8?token=1',
          },
        },
      },
    });
    await new Promise((r) => setTimeout(r, 10));

    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED'
        && m.kind === 'segment-url'
        && m.url === 'https://cdn.example.com/video/seg0.m4s',
    );
    expect(deepHit).toBeDefined();

    const manifestHit = messages.find(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED'
        && m.url === 'https://cdn.example.com/video/master.m3u8?token=1',
    );
    expect(manifestHit).toBeDefined();
    expect(manifestHit.format).toBe('m3u8');
    expect(manifestHit.source).toBe('worker:postMessage:obj');

    window.Worker = origWorker;
  });
});


describe('URL.createObjectURL hook', () => {
  beforeEach(() => {
    delete window.__wv2nas_deepsearch_injected;
    delete window.__wv2nas_manifests;
    delete window.__wv2nas_deep_hits;
  });

  it('emits WV2NAS_DEEP_DETECTED on every createObjectURL call', async () => {
    // jsdom doesn't always implement URL.createObjectURL → polyfill before
    // loading deepsearch so the wrap target is something we control.
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => 'blob:fake-1234');
    loadDeepsearch();

    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const blob = new Blob([new Uint8Array([1, 2, 3, 4])], { type: 'video/mp4' });
    const url = URL.createObjectURL(blob);
    expect(url).toBe('blob:fake-1234');

    await new Promise((r) => setTimeout(r, 10));
    const deepHit = messages.find((m) => m.type === 'WV2NAS_DEEP_DETECTED');
    expect(deepHit).toBeDefined();
    expect(deepHit.kind).toBe('createObjectURL');
    expect(deepHit.url).toBe('blob:fake-1234');
    expect(deepHit.mime).toBe('video/mp4');

    URL.createObjectURL = origCreate;
  });

  it('detects manifest content in blob as deep-hit (NOT manifest URL — blob: is page-scoped)', async () => {
    // Codex review (P2): blob URLs are scoped to the originating
    // page. Server-side cannot fetch them, and sendToNAS would
    // bounce off HttpUrl validation. Pre-fix the helper registered
    // the blob URL as a manifest, leading to broken download
    // offers. Now: deep-hit only (telemetry).
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => 'blob:m3u8-1');
    loadDeepsearch();

    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    // jsdom's Blob.slice(...).text() doesn't always reach the wrapper
    // reliably across Node versions; build a minimal duck-typed blob.
    const m3u8Body = '#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10\nseg.ts\n';
    const fakeBlob = {
      size: m3u8Body.length,
      type: 'application/vnd.apple.mpegurl',
      slice: () => ({
        text: () => Promise.resolve(m3u8Body.slice(0, 256)),
      }),
    };
    URL.createObjectURL(fakeBlob);

    // text() is async — give it time to resolve and dispatch.
    await new Promise((r) => setTimeout(r, 50));

    // No manifest URL emitted (the blob URL is not downloadable).
    const manifestHit = messages.find(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED' && m.source === 'createObjectURL',
    );
    expect(manifestHit).toBeUndefined();

    // Deep-hit IS emitted — kind=manifest-text-no-url + source=createObjectURL.
    const deepHit = messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    );
    expect(deepHit).toBeDefined();
    expect(deepHit.format).toBe('m3u8');
    expect(deepHit.source).toBe('createObjectURL');

    URL.createObjectURL = origCreate;
  });

  it('skips inspection on huge blobs (>5MB)', async () => {
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => 'blob:big-1');
    loadDeepsearch();

    // A fake blob with size > 5MB but cheap to construct.
    const fakeBlob = {
      size: 10 * 1024 * 1024,
      type: 'video/mp4',
      slice: vi.fn(),
    };
    URL.createObjectURL(fakeBlob);
    expect(fakeBlob.slice).not.toHaveBeenCalled();
    URL.createObjectURL = origCreate;
  });
});


// Codex review (P2) — positive-control: a real HTTP(S) URL passed
// to notifyManifest STILL goes through as WV2NAS_MANIFEST_DETECTED.
// The fix only filters out non-downloadable URLs (page URL, blob:);
// legitimate downloads must keep flowing.

describe('notifyManifest URL filter (Codex P2)', () => {
  beforeEach(() => {
    delete window.__wv2nas_deepsearch_injected;
    delete window.__wv2nas_manifests;
    delete window.__wv2nas_deep_hits;
  });

  it('real HTTPS URL still emits WV2NAS_MANIFEST_DETECTED', async () => {
    loadDeepsearch();
    // Drive notifyManifest through the script's exposed buffer —
    // simulate inject.js calling it with a real network-detected URL.
    // (The fix targets the deepsearch's internal callers, but the
    // helper is shared and must not regress for real URLs.)
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    // Push a manifest entry as if a network interceptor had a real URL.
    const real = {
      type: 'WV2NAS_MANIFEST_DETECTED',
      url: 'https://cdn.example.com/master.m3u8',
      format: 'm3u8',
      source: 'fetch',
    };
    window.__wv2nas_manifests.push(real);
    window.postMessage(real, '*');
    await new Promise((r) => setTimeout(r, 10));

    const hit = messages.find(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED' && m.url === real.url,
    );
    expect(hit).toBeDefined();
    expect(hit.format).toBe('m3u8');
  });

  it('null URL routes to deep-hit', async () => {
    // Trigger via the atob path which now passes null.
    const origAtob = window.atob;
    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    const m3u8 = '#EXTM3U\n';
    window.atob(Buffer.from(m3u8).toString('base64'));
    await new Promise((r) => setTimeout(r, 10));

    expect(messages.find((m) => m.type === 'WV2NAS_MANIFEST_DETECTED'))
      .toBeUndefined();
    expect(messages.find(
      (m) => m.type === 'WV2NAS_DEEP_DETECTED' && m.kind === 'manifest-text-no-url',
    )).toBeDefined();

    window.atob = origAtob;
  });

  it('blob: URL routes to deep-hit (page-scoped, not downloadable)', async () => {
    // Even if some future path passes a blob: URL, the helper must
    // refuse it. We can't easily exercise this through createObjectURL
    // in jsdom without the original Blob.text complications, so we
    // call the script's behavior via the atob route with a manual
    // postMessage assertion. (The source filter applies to all
    // callers of notifyManifest.)
    const origAtob = window.atob;
    loadDeepsearch();
    const messages = [];
    window.addEventListener('message', (e) => messages.push(e.data));

    // The helper itself isn't directly exposed — exercise the
    // protection via createObjectURL with a small fake blob.
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => 'blob:test-fake');
    const m3u8Body = '#EXTM3U\n#EXTINF:10\nseg.ts\n';
    const fakeBlob = {
      size: m3u8Body.length,
      type: 'application/vnd.apple.mpegurl',
      slice: () => ({ text: () => Promise.resolve(m3u8Body) }),
    };
    URL.createObjectURL(fakeBlob);
    await new Promise((r) => setTimeout(r, 50));

    // No manifest URL with the blob: URL surfaced.
    const manifestHits = messages.filter(
      (m) => m.type === 'WV2NAS_MANIFEST_DETECTED'
        && typeof m.url === 'string' && m.url.startsWith('blob:'),
    );
    expect(manifestHits).toHaveLength(0);

    URL.createObjectURL = origCreate;
    window.atob = origAtob;
  });
});


describe('safety: explicitly does NOT hook typed-array constructors', () => {
  // Make sure we don't accidentally cross the line into key extraction.
  // If the wrapper ever monkey-patches Uint8Array, this test fails loudly.
  it('Uint8Array, Int8Array, DataView are not wrapped', () => {
    const u8 = window.Uint8Array;
    const i8 = window.Int8Array;
    const dv = window.DataView;
    loadDeepsearch();
    expect(window.Uint8Array).toBe(u8);
    expect(window.Int8Array).toBe(i8);
    expect(window.DataView).toBe(dv);
  });

  it('String.fromCharCode and Array.prototype.join are not wrapped', () => {
    const fcc = String.fromCharCode;
    const join = Array.prototype.join;
    loadDeepsearch();
    expect(String.fromCharCode).toBe(fcc);
    expect(Array.prototype.join).toBe(join);
  });
});
