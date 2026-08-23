// Pure extension-update-check helpers shared between background.js and unit tests.
//
// The extension is sideloaded from GitHub Release zips (not the Chrome Web
// Store), so Chrome never auto-updates it. background.js polls the GitHub
// "latest release" API on a throttle and the sidepanel shows a reminder banner
// when the installed version is behind. Everything here is pure so vitest can
// cover the version arithmetic without a Chrome runtime.

(function installUpdateCheckCore(root) {
  if (!root || root.WV2NUpdateCheckCore) return;

  // "v3.2.0" / "3.2.0" / "3.2.0.1" -> [3, 2, 0(, 1)]. Returns null for
  // anything that is not a plain dotted-integer tag (e.g. "3.3.0-rc1") —
  // the repo convention is vX.Y.0 only, so a fancier tag means "don't nag".
  function parseVersion(input) {
    if (typeof input !== 'string') return null;
    let s = input.trim();
    if (s[0] === 'v' || s[0] === 'V') s = s.slice(1);
    if (!s) return null;
    const parts = s.split('.');
    if (parts.length > 4) return null;
    const nums = [];
    for (const part of parts) {
      if (!/^\d{1,9}$/.test(part)) return null;
      nums.push(parseInt(part, 10));
    }
    return nums;
  }

  // -1 / 0 / 1, or null when either side is unparseable. Missing segments
  // count as 0 ("3.2" === "3.2.0").
  function compareVersions(a, b) {
    const va = parseVersion(a);
    const vb = parseVersion(b);
    if (!va || !vb) return null;
    const len = Math.max(va.length, vb.length);
    for (let i = 0; i < len; i++) {
      const x = va[i] || 0;
      const y = vb[i] || 0;
      if (x !== y) return x > y ? 1 : -1;
    }
    return 0;
  }

  function isNewerVersion(candidate, current) {
    return compareVersions(candidate, current) === 1;
  }

  // GitHub "latest release" fields -> { latestVersion, releaseUrl } | null.
  // releaseUrl only trusts html_url when it sits under the expected repo
  // prefix (a tampered/proxied response must not steer the banner link
  // somewhere else); otherwise the static releases page is used.
  function normalizeReleaseInfo({ tagName, htmlUrl, allowedUrlPrefix, fallbackUrl }) {
    const parsed = parseVersion(tagName);
    if (!parsed) return null;
    let releaseUrl = fallbackUrl || '';
    if (typeof htmlUrl === 'string' && typeof allowedUrlPrefix === 'string'
        && allowedUrlPrefix && htmlUrl.startsWith(allowedUrlPrefix)) {
      releaseUrl = htmlUrl;
    }
    return { latestVersion: parsed.join('.'), releaseUrl };
  }

  // Throttle decision on the persisted check state. Failed checks retry
  // sooner than successful ones; a lastCheckedAt in the future (clock moved
  // backwards) re-checks immediately instead of stalling until it catches up.
  function shouldCheckForUpdate(state, now, { okIntervalMs, errorIntervalMs }) {
    if (!state || typeof state.lastCheckedAt !== 'number') return true;
    if (state.lastCheckedAt > now) return true;
    const interval = state.lastResult === 'ok' ? okIntervalMs : errorIntervalMs;
    return now - state.lastCheckedAt >= interval;
  }

  root.WV2NUpdateCheckCore = Object.freeze({
    parseVersion,
    compareVersions,
    isNewerVersion,
    normalizeReleaseInfo,
    shouldCheckForUpdate,
  });
}((typeof globalThis !== 'undefined' && globalThis) || (typeof window !== 'undefined' && window) || this));
