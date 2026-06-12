import { describe, expect, it } from 'vitest';

import '../browserPipelineCore.js';

const core = globalThis.WV2NASBrowserPipeline;

describe('browserPipelineCore shared helpers', () => {
  it('exposes DNR helpers for both module tests and classic background.js', () => {
    expect(core).toBeDefined();
    expect(core.RULE_ID_BASE).toBe(10_000);
    expect(typeof core.buildHeaderRules).toBe('function');
    expect(typeof core.buildDnrRules).toBe('function');
    expect(typeof core.matchesTrustedCdnSuffix).toBe('function');
    expect(typeof core.isManifestUrlSafeForBrowser).toBe('function');
    expect(core._internals.REQUEST_OVERRIDE_HEADERS)
      .toEqual(['referer', 'origin', 'user-agent']);
  });

  it('builds the same paired request/response DNR rules used by background jobs', () => {
    const rules = core.buildDnrRules({
      segmentUrls: ['https://cdn.example.com/v/seg0.ts'],
      trustedSegmentUrls: ['https://cdn.example.com/v/seg0.ts'],
      referer: 'https://site.example/watch',
      origin: 'https://site.example',
      userAgent: 'UA',
      idBase: 12_300,
      initiatorDomain: 'extension-id',
    });

    expect(rules).toHaveLength(2);
    expect(rules.map((r) => r.id)).toEqual([12_300, 12_350]);
    expect(rules[0].action.requestHeaders.map((h) => h.header))
      .toEqual(['referer', 'origin', 'user-agent']);
    expect(rules[1].action.responseHeaders.map((h) => h.header))
      .toEqual(['access-control-allow-origin', 'access-control-allow-credentials']);
    expect(rules[0].condition.initiatorDomains).toEqual(['extension-id']);
  });

  it('keeps the browser manifest safety gate centralized with the DNR trust boundary', () => {
    expect(core.isManifestUrlSafeForBrowser(
      'https://cdn.example.com/video/master.m3u8',
      'https://site.example/watch',
      [],
    )).toMatchObject({ safe: false });

    expect(core.isManifestUrlSafeForBrowser(
      'https://cdn.example.com/video/master.m3u8',
      'https://site.example/watch',
      ['cdn.example.com'],
    )).toEqual({ safe: true });
  });
});
