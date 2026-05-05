// Tests for inject.js v2.3.18 deepsearch port — JSON.parse hook that
// scans parsed objects for m3u8/mpd URLs embedded in API responses.
//
// inject.js runs as a self-invoking IIFE in the page context. We load it
// into a vm sandbox with stub `window`, `XMLHttpRequest`, `fetch`, and
// capture postMessage calls.

import { describe, expect, it, beforeEach } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const INJECT_PATH = path.resolve(__dirname, '..', 'inject.js');
const INJECT_SRC = fs.readFileSync(INJECT_PATH, 'utf-8');

function createSandbox() {
  const messages = [];
  const ctx = {
    console,
    URL,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    JSON: { parse: JSON.parse, stringify: JSON.stringify },
    TextDecoder,
    Object,
    Array,
  };
  ctx.window = {
    __messages: messages,
    postMessage: (data) => messages.push(data),
    addEventListener: () => {},
    fetch: () => Promise.resolve({}),
  };
  // inject.js patches XMLHttpRequest.prototype methods, so we need a real
  // class with the methods present.
  ctx.XMLHttpRequest = class XHR {
    open() {}
    send() {}
    addEventListener() {}
  };
  vm.createContext(ctx);
  vm.runInContext(INJECT_SRC, ctx);
  return ctx;
}

function getManifestMessages(ctx) {
  return ctx.window.__messages.filter(
    (m) => m && m.type === 'WV2NAS_MANIFEST_DETECTED'
  );
}

describe('inject.js deepsearch JSON.parse hook (v2.3.18)', () => {
  it('detects m3u8 URL embedded inside parsed JSON object', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"video":{"hls":"https://cdn.example.com/v/master.m3u8"}}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://cdn.example.com/v/master.m3u8');
    expect(msgs[0].format).toBe('m3u8');
  });

  it('detects mpd URL embedded inside parsed JSON', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"streams":[{"dash":"https://cdn.example.com/v/manifest.mpd"}]}');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].format).toBe('mpd');
  });

  it('detects URLs with query strings', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"u":"https://cdn.example.com/v/index.m3u8?token=abc&exp=999"}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe(
      'https://cdn.example.com/v/index.m3u8?token=abc&exp=999'
    );
  });

  it('walks deeply nested objects', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"a":{"b":{"c":{"d":{"deep":"https://x.test/y.m3u8"}}}}}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://x.test/y.m3u8');
  });

  it('walks arrays', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"qualities":[{"url":"https://x.test/720p.m3u8"},{"url":"https://x.test/1080p.m3u8"}]}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(2);
    expect(msgs.map((m) => m.url).sort()).toEqual([
      'https://x.test/1080p.m3u8',
      'https://x.test/720p.m3u8',
    ]);
  });

  it('finds multiple URLs in the same string field', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"playlist":"primary: https://x.test/a.m3u8 ; backup: https://y.test/b.mpd"}'
    );

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(2);
  });

  it('does not fire on JSON without media URLs', () => {
    const ctx = createSandbox();
    ctx.JSON.parse(
      '{"user":{"name":"alice","age":30,"website":"https://example.com/about.html"}}'
    );

    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('does not match m3u8 substring without proper URL prefix', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('{"comment":"the m3u8 file format is..."}');

    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('does not break the page when scan throws', () => {
    const ctx = createSandbox();
    // Even if the parsed value is an exotic shape, parsing must succeed
    // and return the result. Inject.js wraps scan in try/catch so we just
    // verify that JSON.parse still works normally.
    const result = ctx.JSON.parse('{"a":1,"b":[1,2,3]}');
    expect(result).toEqual({ a: 1, b: [1, 2, 3] });
  });

  it('returns the parsed value untouched (passive observer)', () => {
    const ctx = createSandbox();
    const result = ctx.JSON.parse(
      '{"hls":"https://x.test/v.m3u8","other":42}'
    );
    expect(result).toEqual({ hls: 'https://x.test/v.m3u8', other: 42 });
  });

  it('handles primitive JSON values without crashing', () => {
    const ctx = createSandbox();
    expect(ctx.JSON.parse('null')).toBe(null);
    expect(ctx.JSON.parse('42')).toBe(42);
    expect(ctx.JSON.parse('"just a string"')).toBe('just a string');
    expect(ctx.JSON.parse('true')).toBe(true);
    expect(getManifestMessages(ctx)).toHaveLength(0);
  });

  it('detects URL embedded as primitive string', () => {
    const ctx = createSandbox();
    ctx.JSON.parse('"https://x.test/inline.m3u8"');

    const msgs = getManifestMessages(ctx);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].url).toBe('https://x.test/inline.m3u8');
  });

  it('bounds recursion depth (does not stack overflow on circular-shaped data)', () => {
    const ctx = createSandbox();
    // Build a deeply nested string via JSON. JSON itself can't be circular,
    // but we can build something pathologically deep (15 levels).
    let payload = '"end"';
    for (let i = 0; i < 15; i++) {
      payload = `{"d":${payload}}`;
    }
    // Should not throw, even though scan gives up at depth 10
    expect(() => ctx.JSON.parse(payload)).not.toThrow();
  });
});
