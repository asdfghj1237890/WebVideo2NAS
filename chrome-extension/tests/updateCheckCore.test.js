import { describe, expect, it } from 'vitest';

import '../updateCheckCore.js';

const core = globalThis.WV2NUpdateCheckCore;

describe('parseVersion', () => {
  it('parses plain and v-prefixed dotted-integer versions', () => {
    expect(core.parseVersion('3.2.0')).toEqual([3, 2, 0]);
    expect(core.parseVersion('v3.2.0')).toEqual([3, 2, 0]);
    expect(core.parseVersion('V3.2.0')).toEqual([3, 2, 0]);
    expect(core.parseVersion('3')).toEqual([3]);
    expect(core.parseVersion('1.2.3.4')).toEqual([1, 2, 3, 4]);
    expect(core.parseVersion(' 3.2.0 ')).toEqual([3, 2, 0]);
    expect(core.parseVersion('03.2.0')).toEqual([3, 2, 0]);
  });

  it('rejects anything that is not a plain dotted-integer tag', () => {
    expect(core.parseVersion('')).toBeNull();
    expect(core.parseVersion('v')).toBeNull();
    expect(core.parseVersion('3.2.0-beta')).toBeNull();
    expect(core.parseVersion('3..0')).toBeNull();
    expect(core.parseVersion('3.2.0.1.9')).toBeNull();
    expect(core.parseVersion('abc')).toBeNull();
    expect(core.parseVersion(null)).toBeNull();
    expect(core.parseVersion(320)).toBeNull();
  });
});

describe('compareVersions / isNewerVersion', () => {
  it('compares numerically, not lexicographically', () => {
    expect(core.compareVersions('3.10.0', '3.9.9')).toBe(1);
    expect(core.compareVersions('3.9.9', '3.10.0')).toBe(-1);
    expect(core.compareVersions('3.2.0', 'v3.2.0')).toBe(0);
  });

  it('treats missing segments as zero', () => {
    expect(core.compareVersions('3.2', '3.2.0')).toBe(0);
    expect(core.compareVersions('3.2.0.1', '3.2.0')).toBe(1);
  });

  it('returns null when either side is unparseable', () => {
    expect(core.compareVersions('3.2.0-rc1', '3.2.0')).toBeNull();
    expect(core.compareVersions('3.2.0', undefined)).toBeNull();
  });

  it('isNewerVersion is true only for a strictly newer, parseable candidate', () => {
    expect(core.isNewerVersion('3.3.0', '3.2.0')).toBe(true);
    expect(core.isNewerVersion('3.2.0', '3.2.0')).toBe(false);
    expect(core.isNewerVersion('3.1.0', '3.2.0')).toBe(false);
    expect(core.isNewerVersion('garbage', '3.2.0')).toBe(false);
    expect(core.isNewerVersion('3.3.0', 'garbage')).toBe(false);
  });
});

describe('normalizeReleaseInfo', () => {
  const base = {
    allowedUrlPrefix: 'https://github.com/asdfghj1237890/WebVideo2NAS/',
    fallbackUrl: 'https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest',
  };

  it('normalizes the tag and keeps an html_url under the repo prefix', () => {
    const info = core.normalizeReleaseInfo({
      ...base,
      tagName: 'v3.3.0',
      htmlUrl: 'https://github.com/asdfghj1237890/WebVideo2NAS/releases/tag/v3.3.0',
    });
    expect(info).toEqual({
      latestVersion: '3.3.0',
      releaseUrl: 'https://github.com/asdfghj1237890/WebVideo2NAS/releases/tag/v3.3.0',
    });
  });

  it('falls back to the releases page for a missing or foreign html_url', () => {
    expect(core.normalizeReleaseInfo({ ...base, tagName: 'v3.3.0' }).releaseUrl)
      .toBe(base.fallbackUrl);
    expect(core.normalizeReleaseInfo({
      ...base,
      tagName: 'v3.3.0',
      htmlUrl: 'https://evil.example/releases/tag/v3.3.0',
    }).releaseUrl).toBe(base.fallbackUrl);
    expect(core.normalizeReleaseInfo({
      ...base,
      tagName: 'v3.3.0',
      htmlUrl: 'http://github.com/asdfghj1237890/WebVideo2NAS/releases/tag/v3.3.0',
    }).releaseUrl).toBe(base.fallbackUrl);
  });

  it('returns null for an unrecognized tag', () => {
    expect(core.normalizeReleaseInfo({ ...base, tagName: 'nightly-2026-08-23' })).toBeNull();
    expect(core.normalizeReleaseInfo({ ...base, tagName: undefined })).toBeNull();
  });
});

describe('shouldCheckForUpdate', () => {
  const intervals = { okIntervalMs: 12 * 60 * 60 * 1000, errorIntervalMs: 60 * 60 * 1000 };
  const NOW = 1_000_000_000_000;

  it('checks when there is no usable prior state', () => {
    expect(core.shouldCheckForUpdate(null, NOW, intervals)).toBe(true);
    expect(core.shouldCheckForUpdate({}, NOW, intervals)).toBe(true);
    expect(core.shouldCheckForUpdate({ lastCheckedAt: 'yesterday' }, NOW, intervals)).toBe(true);
  });

  it('throttles a fresh successful check and re-checks after the ok interval', () => {
    const fresh = { lastResult: 'ok', lastCheckedAt: NOW - 1000 };
    expect(core.shouldCheckForUpdate(fresh, NOW, intervals)).toBe(false);
    const stale = { lastResult: 'ok', lastCheckedAt: NOW - intervals.okIntervalMs };
    expect(core.shouldCheckForUpdate(stale, NOW, intervals)).toBe(true);
  });

  it('retries failed checks on the shorter error interval', () => {
    const fresh = { lastResult: 'error', lastCheckedAt: NOW - 1000 };
    expect(core.shouldCheckForUpdate(fresh, NOW, intervals)).toBe(false);
    const stale = { lastResult: 'error', lastCheckedAt: NOW - intervals.errorIntervalMs };
    expect(core.shouldCheckForUpdate(stale, NOW, intervals)).toBe(true);
  });

  it('re-checks when lastCheckedAt is in the future (clock moved backwards)', () => {
    expect(core.shouldCheckForUpdate(
      { lastResult: 'ok', lastCheckedAt: NOW + 60_000 }, NOW, intervals,
    )).toBe(true);
  });
});
