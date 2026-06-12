// Pure sidepanel helpers shared between the UI script and unit tests.

(function installSidepanelCore(root) {
  if (!root || root.WV2NSidepanelCore) return;

  function parseTrustedCdnSuffixesInput(raw) {
    if (typeof raw !== 'string') return [];
    const seen = new Set();
    for (const part of raw.split(/[,\n]+/)) {
      let s = part.trim();
      if (!s) continue;
      if (s.includes('://')) {
        try { s = new URL(s).hostname; } catch (_e) { /* keep raw on parse fail */ }
      }
      s = s.replace(/^\.+/, '').toLowerCase();
      if (s) seen.add(s);
    }
    return Array.from(seen);
  }

  function deriveTrustedCdnSuffix(urlOrHost) {
    if (typeof urlOrHost !== 'string' || !urlOrHost) return null;
    const raw = urlOrHost.trim();
    if (!raw) return null;
    let host = raw;
    try {
      if (raw.includes('://')) {
        host = new URL(raw).hostname;
      } else if (raw.includes('/') || raw.includes(':')) {
        host = new URL(`https://${raw}`).hostname;
      }
    } catch (_e) {
      return null;
    }
    if (!host) return null;
    host = host.toLowerCase().replace(/^\.+|\.+$/g, '');
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return null;
    if (host.startsWith('[')) return null;
    const parts = host.split('.').filter(Boolean);
    if (parts.length < 2) return null;
    if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host)) return null;
    return host;
  }

  function hostMatchesAnyTrustedSuffix(host, suffixes) {
    if (!Array.isArray(suffixes) || suffixes.length === 0) return false;
    if (typeof host !== 'string' || !host) return false;
    const h = host.toLowerCase();
    for (const raw of suffixes) {
      if (typeof raw !== 'string') continue;
      const s = raw.trim().toLowerCase().replace(/^\.+/, '');
      if (!s) continue;
      if (h === s || h.endsWith('.' + s)) return true;
    }
    return false;
  }

  function extractQualitiesFromUrl(url) {
    const raw = String(url || '');
    const lower = raw.toLowerCase();
    if (!lower) return [];

    const allowed = new Set([2160, 1440, 1080, 720, 540, 480, 360, 240]);
    const found = new Set();

    const pMatches = lower.matchAll(/(?<![0-9])([0-9]{3,4})p(?![a-z0-9])/g);
    for (const m of pMatches) {
      const n = Number(m[1]);
      if (allowed.has(n)) found.add(n);
    }

    const qMatches = lower.matchAll(/[?&](?:res|resolution|quality|q|height|h)=([0-9]{3,4})\b/g);
    for (const m of qMatches) {
      const n = Number(m[1]);
      if (allowed.has(n)) found.add(n);
    }

    return Array.from(found).sort((a, b) => b - a).map((n) => `${n}p`);
  }

  function getMaxQualityNumber(url) {
    const qualities = extractQualitiesFromUrl(url);
    if (!qualities.length) return -1;
    let max = -1;
    for (const q of qualities) {
      const n = parseInt(String(q).replace(/[^0-9]/g, ''), 10);
      if (Number.isFinite(n) && n > max) max = n;
    }
    return max;
  }

  function formatDuration(totalSeconds) {
    const sec = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (n) => String(n).padStart(2, '0');
    if (h > 0) return `${pad(h)}:${pad(m)}:${pad(s)}`;
    return `${pad(m)}:${pad(s)}`;
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function containsIpAddress(url) {
    const ipv4QueryPattern = /[?&]ip=(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/;
    return ipv4QueryPattern.test(url);
  }

  root.WV2NSidepanelCore = Object.freeze({
    parseTrustedCdnSuffixesInput,
    deriveTrustedCdnSuffix,
    hostMatchesAnyTrustedSuffix,
    extractQualitiesFromUrl,
    getMaxQualityNumber,
    formatDuration,
    escapeHtml,
    containsIpAddress,
  });
}((typeof globalThis !== 'undefined' && globalThis) || (typeof window !== 'undefined' && window) || this));
