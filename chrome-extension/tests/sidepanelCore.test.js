import { describe, expect, it } from 'vitest';

import '../sidepanelCore.js';

const core = globalThis.WV2NSidepanelCore;

describe('sidepanelCore shared helpers', () => {
  it('exposes pure helpers used by sidepanel.js rendering code', () => {
    expect(core).toBeDefined();
    expect(typeof core.parseTrustedCdnSuffixesInput).toBe('function');
    expect(typeof core.deriveTrustedCdnSuffix).toBe('function');
    expect(typeof core.hostMatchesAnyTrustedSuffix).toBe('function');
    expect(typeof core.formatDuration).toBe('function');
  });

  it('normalizes trusted CDN inputs without guessing eTLD+1', () => {
    expect(core.parseTrustedCdnSuffixesInput(
      ' https://cdn.example.co.uk/path, .media.example.net\ncdn.example.co.uk '
    )).toEqual(['cdn.example.co.uk', 'media.example.net']);

    expect(core.deriveTrustedCdnSuffix('https://cdn.example.co.uk/video/master.m3u8'))
      .toBe('cdn.example.co.uk');
    expect(core.hostMatchesAnyTrustedSuffix('a.media.example.net', ['.media.example.net']))
      .toBe(true);
  });
});
