// Background Service Worker for Video Detection and Download Management

// Store detected video URLs (m3u8, mpd, mp4)
let detectedUrls = new Set();
let currentTabUrls = {};
let currentTabUrlKeys = {};
let lastNotifyAtByTab = {};
let deepHitsByTab = {};

// Some sites fetch media via Service Worker / browser context where tabId is -1.
// Keep these "orphan" detections and later attach them to the active tab by initiator/documentUrl.
let orphanUrlInfos = [];
let orphanUrlKeys = new Set();

// Store captured request headers for m3u8 URLs
let capturedHeaders = {};

// Track user-clicked video per tab (for accurate "Now Playing" detection)
// Key: tabId, Value: { videoSrc, videoIndex, videoCount, pageUrl, timestamp, matchedUrl }
let userClickedVideoByTab = {};

// Cached thumbnails per tab from content.js
// Key: tabId, Value: { pageUrl, pageThumbnail, posters: [{ poster, src, index }] }
let pageThumbnailsByTab = {};

// Cached video duration / live status per URL
// Key: url, Value: { duration: number|null, isLive: bool, ts: number }
let videoMetaByUrl = {};
let videoMetaProbing = new Set();
const VIDEO_META_MAX_ENTRIES = 500;

// Hidden-mode AV-task pipeline: tabs we opened on the user's behalf so the
// site's own JS can produce a fresh m3u8. Keyed by tabId; expires after the
// timeout regardless of outcome so the user gets feedback.
// Value: { code, requestedAt, fired: bool, timeoutHandle, historyId }
let avPendingTabs = {};
const AV_TASK_TIMEOUT_MS = 60_000; // 60s for the page to load + emit a manifest
const AV_TASK_AUTOCLOSE_DELAY_MS = 4_000; // give the worker a chance to grab fresh cookies
const AV_HISTORY_MAX = 200; // bounded history kept in chrome.storage.local.avTaskHistory

// User settings
let userSettings = {
  autoDetect: true,
  showNotifications: true
};

function scoreUrlInfo(info) {
  const now = Date.now();
  const ts = Number(info?.timestamp) || 0;
  const ageMs = now - ts;

  let score = 0;

  const rawUrl = String(info?.url || '');
  const urlLower = rawUrl.toLowerCase();

  // Strongly prefer very recent URLs (what the player is actively fetching).
  if (ageMs < 10_000) score += 10;
  else if (ageMs < 30_000) score += 8;
  else if (ageMs < 120_000) score += 4;

  // Prefer manifests / single-file videos over segments.
  if (urlLower.includes('.m3u8')) score += 4;
  if (urlLower.includes('.mpd')) score += 4;
  if (urlLower.includes('.mp4')) score += 1;
  if (urlLower.includes('.mov')) score += 1;
  const fmt = String(info?.detectedFormat || '').toLowerCase();
  if (fmt === 'mpd' || fmt === 'm3u8') score += 4;

  // Request type hint (Chrome categorizes actual playback as "media" on many sites).
  const rt = String(info?.requestType || '').toLowerCase();
  if (rt === 'media') score += 6;

  // MP4 playback often uses Range requests. If we saw any, it's a strong signal.
  const rangeHits = Number(info?.rangeHitCount) || 0;
  if (rangeHits > 0) score += 12;

  // Repeated hits usually means the player is actively using this URL.
  const hits = Number(info?.hitCount) || 0;
  if (hits >= 3) score += 2;
  if (hits >= 10) score += 2;

  return score;
}

// Check if a detected URL matches user-clicked video
function matchesUserClickedVideo(detectedUrl, clickedInfo) {
  if (!clickedInfo || !clickedInfo.videoSrc) return false;

  const detected = String(detectedUrl || '').toLowerCase();
  const clicked = String(clickedInfo.videoSrc || '').toLowerCase();

  // Direct match
  if (detected === clicked) return true;

  // For blob: URLs, we can't match directly. The user clicked a video
  // using MediaSource/blob, so we need to rely on page URL + timing.
  // This case is handled by the caller checking if ANY video was clicked on this page.

  // Partial match: check if the detected URL's path is contained in clicked or vice versa
  try {
    const detectedPath = new URL(detectedUrl).pathname;
    const clickedPath = new URL(clickedInfo.videoSrc).pathname;
    // Check if they share the same base filename (common for adaptive streaming)
    const detectedFile = detectedPath.split('/').pop()?.split('?')[0] || '';
    const clickedFile = clickedPath.split('/').pop()?.split('?')[0] || '';
    if (detectedFile && clickedFile && detectedFile === clickedFile) return true;
  } catch (_) {
    // URL parsing failed, skip partial matching
  }

  return false;
}

function getSortedUrlsForTab(tabId) {
  const list = Array.isArray(currentTabUrls[tabId]) ? currentTabUrls[tabId] : [];
  const scored = list.map((u) => {
    const score = scoreUrlInfo(u);
    return { ...u, score, isNowPlaying: false };
  });

  scored.sort((a, b) => (b.score - a.score) || ((b.timestamp || 0) - (a.timestamp || 0)));

  // Check for user-clicked video first (most accurate signal)
  const clickedInfo = userClickedVideoByTab[tabId];
  const clickAge = clickedInfo ? (Date.now() - (clickedInfo.timestamp || 0)) : Infinity;
  const isClickRecent = clickAge <= 10 * 60_000; // Click valid for 10 minutes

  if (isClickRecent && clickedInfo) {
    // Method 1: Use matchedUrl if we already associated a URL with this click
    if (clickedInfo.matchedUrl) {
      for (const item of scored) {
        if (item.url === clickedInfo.matchedUrl) {
          item.isNowPlaying = true;
          return scored;
        }
      }
    }

    // Fallback: Direct src matching (for videos with direct mp4/m3u8 src)
    if (clickedInfo.videoSrc && !clickedInfo.videoSrc.startsWith('blob:')) {
      for (const item of scored) {
        if (matchesUserClickedVideo(item.url, clickedInfo)) {
          item.isNowPlaying = true;
          return scored;
        }
      }
    }

    // Last resort: if only one URL, mark it
    if (scored.length === 1) {
      scored[0].isNowPlaying = true;
      return scored;
    }
  }

  // No user click info or no match = no "Now Playing" badge
  return scored;
}

function safeOrigin(u) {
  try {
    return new URL(u).origin;
  } catch (_) {
    return null;
  }
}

function tryGetUrl(u) {
  try { return new URL(u); } catch (_) { return null; }
}

function hasCookieHeader(headers) {
  if (!headers) return false;
  for (const k of Object.keys(headers)) {
    if (typeof k === 'string' && k.toLowerCase() === 'cookie') return true;
  }
  return false;
}

function hasQueryString(url) {
  const u = tryGetUrl(url);
  return !!(u && u.search && u.search.length > 1);
}

function shouldKeepClickedDetectedSignedUrl(targetUrl, targetInfo, best) {
  if (!targetInfo || !targetInfo.detectedFormat || !best || best.url === targetUrl) return false;
  if (!hasQueryString(targetUrl)) return false;

  const targetTs = Number(targetInfo.timestamp) || 0;
  const bestTs = Number(best.entry && best.entry.timestamp) || 0;
  if (!targetTs) return false;

  return !bestTs || targetTs >= bestTs;
}

const _WV2NAS_SIGNED_REFRESH_QUERY_KEYS = [
  'v', 'exp', 'expires', 'auth', 'token', 'signature', 'sig',
];
const _WV2NAS_SIGNED_EXP_QUERY_KEYS = ['exp', 'expires'];
const _WV2NAS_SIGNED_AUTH_QUERY_KEYS = ['auth', 'token', 'signature', 'sig'];

function _wv2nasUrlDir(u) {
  return u.pathname.replace(/[^/]*$/, '');
}

function _wv2nasQueryNumber(u, keys) {
  for (const key of keys) {
    if (!u.searchParams.has(key)) continue;
    const n = Number(u.searchParams.get(key));
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function _wv2nasHasAnyQueryKey(u, keys) {
  return keys.some((key) => u.searchParams.has(key));
}

function _wv2nasRefreshSignedUrlFromAnchor(url, anchorUrl) {
  const target = tryGetUrl(url);
  const anchor = tryGetUrl(anchorUrl);
  if (!target || !anchor) return url;
  if (!_wv2nasHasAnyQueryKey(target, _WV2NAS_SIGNED_EXP_QUERY_KEYS)) return url;
  if (!_wv2nasHasAnyQueryKey(target, _WV2NAS_SIGNED_AUTH_QUERY_KEYS)) return url;
  if (!_wv2nasHasAnyQueryKey(anchor, _WV2NAS_SIGNED_EXP_QUERY_KEYS)) return url;
  if (!_wv2nasHasAnyQueryKey(anchor, _WV2NAS_SIGNED_AUTH_QUERY_KEYS)) return url;
  if (_wv2nasUrlDir(target) !== _wv2nasUrlDir(anchor)) return url;

  const targetExp = _wv2nasQueryNumber(target, _WV2NAS_SIGNED_EXP_QUERY_KEYS);
  const anchorExp = _wv2nasQueryNumber(anchor, _WV2NAS_SIGNED_EXP_QUERY_KEYS);
  if (targetExp == null || anchorExp == null || targetExp >= anchorExp) return url;

  let changed = false;
  for (const key of _WV2NAS_SIGNED_REFRESH_QUERY_KEYS) {
    if (!target.searchParams.has(key) || !anchor.searchParams.has(key)) continue;
    const nextValue = anchor.searchParams.get(key);
    if (target.searchParams.get(key) !== nextValue) {
      target.searchParams.set(key, nextValue);
      changed = true;
    }
  }
  return changed ? target.href : url;
}

function _wv2nasRefreshSignedUrlFromAnchors(url, anchors) {
  let out = url;
  for (const anchor of anchors || []) {
    out = _wv2nasRefreshSignedUrlFromAnchor(out, anchor);
  }
  return out;
}

function _wv2nasRefreshSignedPlanUrls(plan, anchors) {
  if (!plan || !anchors || anchors.length === 0) return plan;
  const refresh = (url) => (
    typeof url === 'string' ? _wv2nasRefreshSignedUrlFromAnchors(url, anchors) : url
  );

  plan.source_url = refresh(plan.source_url);
  plan.selected_variant_url = refresh(plan.selected_variant_url);
  plan.init_segment_url = refresh(plan.init_segment_url);

  for (const track of Object.values(plan.tracks || {})) {
    if (!track) continue;
    track.init_segment_url = refresh(track.init_segment_url);
    for (const segment of track.segments || []) {
      if (!segment) continue;
      segment.url = refresh(segment.url);
      if (segment.key && segment.key.uri) {
        segment.key.uri = refresh(segment.key.uri);
      }
    }
  }

  return plan;
}

// Pick the best captured-headers entry to substitute in for `targetUrl`
// when sending to NAS. The substitute lets us re-key from a "clean" URL the
// user clicked to the tokenized URL the player actually fetched (with the
// matching cookies/Referer captured at fetch time).
//
// Same-tab hard filter — the substitute MUST come from the same tab that
// the user clicked Send from. Without this, when tabs A/B/C are all on the
// same site, every capture matches the origin prefix and the
// most-recent-timestamp tie-breaker silently swaps in another tab's video
// URL — so sending from tab B/C ends up downloading tab A's video.
//
// `sourceTabId` is the tab the user clicked Send from. When unknown
// (orphan/service-worker capture path), fall back to strict initiator
// equality, which is still tighter than the old origin-prefix scoring
// because a full URL doesn't match a different page on the same origin.
function findBestCapturedEntry(targetUrl, sourcePageUrl, sourceTabId) {
  const t = tryGetUrl(targetUrl);
  if (!t) return null;

  let best = null;
  const hasSourceTab = (typeof sourceTabId === 'number' && sourceTabId >= 0);

  for (const [k, entry] of Object.entries(capturedHeaders)) {
    const ku = tryGetUrl(k);
    if (!ku || !entry) continue;

    // Only consider manifest captures (m3u8/mpd or Content-Type detected)
    const kl = k.toLowerCase();
    const isManifestByExt = kl.includes('.m3u8') || kl.includes('.mpd');
    const isManifestByFormat = !!getDetectedFormat(k);
    if (!isManifestByExt && !isManifestByFormat) continue;

    if (hasSourceTab) {
      if (entry.tabId !== sourceTabId) continue;
    } else {
      if (!sourcePageUrl) continue;
      if (entry.initiator !== sourcePageUrl) continue;
    }

    let score = 10; // anchor (same-tab or same-page confirmed by hard filter)
    if (ku.origin === t.origin) score += 5;
    if (ku.pathname === t.pathname) score += 2;
    // Prefer tokenized URLs (query params) as they often map to full playlists
    if (ku.search && ku.search.length > 1) score += 3;
    // Prefer captured requests that already carried Cookie headers
    if (hasCookieHeader(entry.headers)) score += 3;
    if (entry.timestamp && (Date.now() - entry.timestamp) < 60_000) score += 1;

    if (!best || score > best.score ||
        (score === best.score && (entry.timestamp || 0) > (best.entry.timestamp || 0))) {
      best = { url: k, entry, score };
    }
  }
  return best;
}

// Attach a thumbnail (poster match → page og:image) to each URL row.
function enrichWithThumbnails(rows, tabId) {
  const cache = pageThumbnailsByTab[tabId];
  const posters = (cache && Array.isArray(cache.posters)) ? cache.posters : [];
  const fallback = (cache && cache.pageThumbnail) || null;
  return rows.map((row) => {
    if (!row || !row.url) return row;
    let next = row;

    // --- thumbnail ---
    if (cache) {
      let thumb = null;
      for (const p of posters) {
        if (!p || !p.poster) continue;
        if (p.src && p.src === row.url) { thumb = p.poster; break; }
      }
      if (!thumb && fallback) thumb = fallback;
      if (thumb) next = { ...next, thumbnail: thumb };
    }

    // --- duration / live ---
    const meta = videoMetaByUrl[row.url];
    if (meta) {
      if (meta.isLive) next = { ...next, isLive: true };
      else if (meta.duration != null) next = { ...next, duration: meta.duration };
    }
    return next;
  });
}

// ---- Duration / live probe (m3u8 + mpd) ----
function rememberVideoMeta(url, value, tabId) {
  videoMetaByUrl[url] = { ...value, ts: Date.now() };
  // Bound the cache: simple FIFO trim by insertion order.
  const keys = Object.keys(videoMetaByUrl);
  if (keys.length > VIDEO_META_MAX_ENTRIES) {
    const drop = keys.slice(0, keys.length - VIDEO_META_MAX_ENTRIES);
    for (const k of drop) delete videoMetaByUrl[k];
  }
  if (tabId != null && tabId >= 0) {
    notifyDetectedUrlsUpdated(tabId);
  }
}

function parseISO8601Duration(s) {
  // Subset: PT[H][M][S], floats allowed on seconds.
  const m = /^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$/.exec(String(s || ''));
  if (!m) return null;
  return (parseFloat(m[1] || 0) * 3600) + (parseFloat(m[2] || 0) * 60) + parseFloat(m[3] || 0);
}

async function probeM3u8(url, depth) {
  if (depth > 1) return null;
  let res;
  try { res = await fetch(url, { credentials: 'omit', cache: 'no-store' }); }
  catch (_) { return null; }
  if (!res.ok) return null;
  const text = await res.text();
  const lines = text.split(/\r?\n/);

  // Master playlist? — descend into the first variant.
  const streamIdx = lines.findIndex(l => l.startsWith('#EXT-X-STREAM-INF'));
  if (streamIdx >= 0) {
    let pick = null;
    for (let i = streamIdx; i < lines.length - 1; i++) {
      if (lines[i].startsWith('#EXT-X-STREAM-INF')) {
        pick = (lines[i + 1] || '').trim();
        if (pick && !pick.startsWith('#')) break;
      }
    }
    if (!pick) return null;
    let absolute;
    try { absolute = new URL(pick, url).href; } catch (_) { return null; }
    return probeM3u8(absolute, depth + 1);
  }

  // Media playlist — sum #EXTINF; require ENDLIST for VOD, otherwise treat as live.
  const hasEndlist = lines.some(l => l.startsWith('#EXT-X-ENDLIST'));
  let total = 0;
  for (const line of lines) {
    const m = /^#EXTINF:([0-9]+(?:\.[0-9]+)?)/.exec(line);
    if (m) total += parseFloat(m[1]);
  }
  if (!hasEndlist) return { duration: null, isLive: true };
  if (total <= 0) return null;
  return { duration: total, isLive: false };
}

async function probeMpd(url) {
  let res;
  try { res = await fetch(url, { credentials: 'omit', cache: 'no-store' }); }
  catch (_) { return null; }
  if (!res.ok) return null;
  const text = await res.text();
  // Live profile? Most live MPDs have type="dynamic".
  const isLive = /\btype\s*=\s*["']dynamic["']/.test(text);
  if (isLive) return { duration: null, isLive: true };
  const m = /\bmediaPresentationDuration\s*=\s*["']([^"']+)["']/.exec(text);
  if (!m) return null;
  const d = parseISO8601Duration(m[1]);
  if (d == null || d <= 0) return null;
  return { duration: d, isLive: false };
}

async function probeVideoMeta(row, tabId) {
  if (!row || !row.url) return;
  const url = row.url;
  // hasOwnProperty so a stored "failed" entry still counts as cached.
  if (Object.prototype.hasOwnProperty.call(videoMetaByUrl, url)) return;
  if (videoMetaProbing.has(url)) return;

  const lower = url.toLowerCase();
  const fmt = String(row.detectedFormat || '').toLowerCase();
  let probeFn = null;
  if (lower.includes('.m3u8') || fmt === 'm3u8') probeFn = () => probeM3u8(url, 0);
  else if (lower.includes('.mpd') || fmt === 'mpd') probeFn = () => probeMpd(url);
  else return; // mp4 etc. — no cheap probe

  videoMetaProbing.add(url);
  try {
    const result = await probeFn();
    if (result) {
      console.log('[WV2N probe] duration =', result.duration, 'live =', result.isLive, '←', url);
      rememberVideoMeta(url, result, tabId);
    } else {
      console.log('[WV2N probe] no duration found ←', url);
      // Negative cache so we don't refetch on every refresh.
      videoMetaByUrl[url] = { duration: null, isLive: false, ts: Date.now() };
    }
  } catch (e) {
    console.log('[WV2N probe] failed:', e && e.message, '←', url);
    videoMetaByUrl[url] = { duration: null, isLive: false, ts: Date.now() };
  } finally {
    videoMetaProbing.delete(url);
  }
}

function scheduleProbesForRows(rows, tabId) {
  for (const row of rows) {
    if (row && row.url) probeVideoMeta(row, tabId);
  }
}

function pruneOrphans() {
  // Keep this list bounded to avoid unbounded growth.
  const MAX = 200;
  const MAX_AGE_MS = 5 * 60_000;
  const now = Date.now();

  orphanUrlInfos = orphanUrlInfos
    .filter(x => x && typeof x.url === 'string' && x.url && (now - (Number(x.timestamp) || 0)) <= MAX_AGE_MS)
    .slice(-MAX);

  orphanUrlKeys = new Set(orphanUrlInfos.map(x => x.dedupeKey || x.url));
}

function getSortedUrlsForTabWithOrphans(tabId, tabUrl) {
  pruneOrphans();

  const tabList = Array.isArray(currentTabUrls[tabId]) ? currentTabUrls[tabId] : [];

  const merged = tabList.slice();
  const seen = new Set(merged.map(x => x && x.url).filter(Boolean));

  // Orphans (service-worker / no-tabId requests) attach to a tab ONLY when
  // the orphan's recorded page URL exactly matches this tab's current URL.
  // The previous code matched by origin — which leaked across tabs whenever
  // the user had two pages of the same site open (the canonical multi-tab
  // bulk-send case for this extension). Switching from Tab 1 to Tab 2 still
  // showed Tab 1's video URLs because both tabs shared an origin and both
  // pulled the same orphans from the global store. Strict per-tab now: if
  // the orphan can't be tied to a *specific* tab via its pageUrl, it stays
  // invisible (acceptable trade-off — orphans are rare; PWAs that capture a
  // pageUrl still attach to exactly one tab; sites without a captured
  // pageUrl simply don't get detection through this path).
  if (tabUrl) {
    for (const info of orphanUrlInfos) {
      if (!info || !info.url) continue;
      if (seen.has(info.url)) continue;
      if (info.pageUrl && info.pageUrl === tabUrl) {
        merged.push(info);
        seen.add(info.url);
      }
    }
  }

  const scored = merged.map((u) => {
    const score = scoreUrlInfo(u);
    return { ...u, score, isNowPlaying: false };
  });

  scored.sort((a, b) => (b.score - a.score) || ((b.timestamp || 0) - (a.timestamp || 0)));

  // Check for user-clicked video first (most accurate signal)
  const clickedInfo = userClickedVideoByTab[tabId];
  const clickAge = clickedInfo ? (Date.now() - (clickedInfo.timestamp || 0)) : Infinity;
  const isClickRecent = clickAge <= 10 * 60_000; // Click valid for 10 minutes

  if (isClickRecent && clickedInfo) {
    // Method 1: Use matchedUrl if we already associated a URL with this click
    if (clickedInfo.matchedUrl) {
      for (const item of scored) {
        if (item.url === clickedInfo.matchedUrl) {
          item.isNowPlaying = true;
          return scored;
        }
      }
    }

    // Fallback: Direct src matching (for videos with direct mp4/m3u8 src)
    if (clickedInfo.videoSrc && !clickedInfo.videoSrc.startsWith('blob:')) {
      for (const item of scored) {
        if (matchesUserClickedVideo(item.url, clickedInfo)) {
          item.isNowPlaying = true;
          return scored;
        }
      }
    }

    // Last resort: if only one URL, mark it
    if (scored.length === 1) {
      scored[0].isNowPlaying = true;
      return scored;
    }
  }

  // No user click info or no match = no "Now Playing" badge
  return scored;
}

function notifyDetectedUrlsUpdated(tabId) {
  if (tabId == null || typeof tabId !== 'number' || tabId < 0) return;
  const now = Date.now();
  const last = Number(lastNotifyAtByTab[tabId]) || 0;
  if (now - last < 1000) return; // throttle to avoid spamming UI
  lastNotifyAtByTab[tabId] = now;

  try {
    chrome.runtime.sendMessage({ action: 'detectedUrlsUpdated', tabId }, () => {
      // Ignore "no receiver" errors when sidepanel isn't open.
      void chrome.runtime.lastError;
    });
  } catch (_) {
    // Ignore (service worker may be shutting down)
  }
}

function pruneDeepHitsForTab(tabId) {
  const MAX = 20;
  const MAX_AGE_MS = 10 * 60_000;
  const now = Date.now();
  const list = Array.isArray(deepHitsByTab[tabId]) ? deepHitsByTab[tabId] : [];
  deepHitsByTab[tabId] = list
    .filter(x => x && (now - (Number(x.timestamp) || 0)) <= MAX_AGE_MS)
    .slice(-MAX);
  return deepHitsByTab[tabId];
}

function registerDeepHit(tabId, payload) {
  if (tabId == null || typeof tabId !== 'number' || tabId < 0) return;
  const list = pruneDeepHitsForTab(tabId);
  const key = [
    payload.kind || 'unknown',
    payload.format || '',
    payload.source || '',
    payload.url || '',
    payload.mime || '',
  ].join('|');
  const now = Date.now();
  const existing = list.find(x => x && x.key === key);
  if (existing) {
    existing.timestamp = now;
    existing.hitCount = (Number(existing.hitCount) || 0) + 1;
    existing.pageUrl = payload.pageUrl || existing.pageUrl || '';
  } else {
    list.push({
      key,
      kind: payload.kind || 'unknown',
      format: payload.format || null,
      source: payload.source || 'deepsearch',
      url: payload.url || null,
      mime: payload.mime || null,
      pageUrl: payload.pageUrl || '',
      timestamp: Number(payload.timestamp) || now,
      hitCount: 1,
    });
  }
  pruneDeepHitsForTab(tabId);
  notifyDetectedUrlsUpdated(tabId);
}

function getDeepHitsForTab(tabId) {
  return pruneDeepHitsForTab(tabId)
    .slice()
    .sort((a, b) => (Number(b.timestamp) || 0) - (Number(a.timestamp) || 0))
    .map(({ key, ...rest }) => rest);
}

// Load settings on startup
chrome.storage.sync.get(['autoDetect', 'showNotifications'], (result) => {
  if (result.autoDetect !== undefined) userSettings.autoDetect = result.autoDetect;
  if (result.showNotifications !== undefined) userSettings.showNotifications = result.showNotifications;
});

// Listen for settings changes
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === 'sync') {
    if (changes.autoDetect !== undefined) userSettings.autoDetect = changes.autoDetect.newValue;
    if (changes.showNotifications !== undefined) userSettings.showNotifications = changes.showNotifications.newValue;
  }
});

function isCandidateVideoUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') return false;

  const urlLower = rawUrl.toLowerCase();
  if (!(urlLower.includes('.m3u8') || urlLower.includes('.mpd') || urlLower.includes('.mp4') || urlLower.includes('.mov'))) return false;

  // Reject obvious non-video resources even if they contain ".mp4" or ".m3u8" in the name.
  // Example: "preview_720p.mp4.jpg" is an image, not a real mp4.
  const nonVideoFinalExts = [
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico',
    '.css', '.js', '.mjs',
    '.html', '.htm',
    '.json', '.txt'
  ];

  let pathnameLower = '';
  try {
    const u = new URL(rawUrl);
    pathnameLower = (u.pathname || '').toLowerCase();
  } catch (_) {
    // Fallback: strip query/fragment and treat it as a path-like string
    pathnameLower = rawUrl.split(/[?#]/)[0].toLowerCase();
  }

  const lastSegment = pathnameLower.split('/').pop() || '';
  // Filter out streaming segments. They create lots of "similar" URLs and are not
  // what users want to send to NAS (they are only small chunks).
  if (lastSegment.endsWith('.ts') || lastSegment.endsWith('.m4s')) return false;
  if (nonVideoFinalExts.some(ext => lastSegment.endsWith(ext))) return false;

  return true;
}

function inferHlsManifestFromSegmentUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') return null;

  let u;
  try {
    u = new URL(rawUrl);
  } catch (_) {
    return null;
  }

  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;

  const pathParts = u.pathname.split('/');
  const last = pathParts[pathParts.length - 1] || '';
  const match = /^seg-\d+-(v\d+)(?:-(a\d+))?\.ts$/i.exec(last);
  if (!match) return null;

  pathParts[pathParts.length - 1] = `index-${match[1]}${match[2] ? '-' + match[2] : ''}.m3u8`;
  u.pathname = pathParts.join('/');
  u.hash = '';

  const stable = new URL(u.href);
  stable.search = '';
  stable.hash = '';
  return {
    url: u.href,
    dedupeKey: stable.href,
  };
}

function mergeDetectedUrlExtra(existing, extra) {
  if (!existing || !extra) return;
  if (extra.detectedFormat && !existing.detectedFormat) {
    existing.detectedFormat = extra.detectedFormat;
  }
  if (extra.playbackObserved) {
    existing.playbackObserved = true;
  }
}

// Capture the tab's current title and stamp it onto the urlInfo. Async because
// chrome.tabs.get is async — by the time we resolve, the urlInfo is already in
// the store, so we mutate the live object. Best-effort; missing title is OK.
function attachTabTitle(urlInfo, tabId) {
  if (tabId == null || tabId < 0) return;
  try {
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || !tab) return;
      const title = tab.title;
      // Only overwrite if we got a real title — keeps a previously-captured
      // good title if a later fetch races with a transient empty state.
      if (title && title.trim()) urlInfo.pageTitle = title;
    });
  } catch (_) { /* ignore */ }
}

// Register a detected video URL into per-tab and orphan stores.
// `extra` may carry additional fields like `detectedFormat`.
function registerDetectedUrl(details, extra) {
  const isRealTab = (details.tabId != null && typeof details.tabId === 'number' && details.tabId >= 0);
  const detectionKey = (extra && extra.dedupeKey) || details.url;

  const urlInfo = {
    url: details.url,
    dedupeKey: detectionKey,
    tabId: isRealTab ? details.tabId : -1,
    timestamp: Date.now(),
    pageUrl: details.initiator || details.documentUrl,
    pageTitle: '',  // populated async by attachTabTitle below
    requestType: details.type,
    frameId: details.frameId,
    method: details.method,
    hitCount: 1,
    rangeHitCount: 0,
    ...(extra || {})
  };

  detectedUrls.add(detectionKey);

  if (isRealTab) {
    if (!currentTabUrls[details.tabId]) {
      currentTabUrls[details.tabId] = [];
    }
    if (!currentTabUrlKeys[details.tabId]) {
      currentTabUrlKeys[details.tabId] = new Set();
    }

    if (!currentTabUrlKeys[details.tabId].has(detectionKey)) {
      currentTabUrlKeys[details.tabId].add(detectionKey);
      currentTabUrls[details.tabId].push(urlInfo);
      attachTabTitle(urlInfo, details.tabId);
      // If this tab was opened by the hidden-mode AV-task pipeline, this
      // is the fresh manifest we were waiting for — auto-send it once
      // (only the FIRST eligible URL, to avoid firing on every quality
      // variant the player probes).
      maybeFireAvTaskAutoSend(details.tabId, details.url);
    } else {
      const list = currentTabUrls[details.tabId];
      const existing = list.find(item => item && ((item.dedupeKey || item.url) === detectionKey));
      if (existing) {
        existing.url = urlInfo.url;
        existing.dedupeKey = detectionKey;
        existing.timestamp = urlInfo.timestamp;
        existing.pageUrl = urlInfo.pageUrl;
        existing.requestType = urlInfo.requestType;
        existing.frameId = urlInfo.frameId;
        existing.method = urlInfo.method;
        existing.hitCount = (Number(existing.hitCount) || 0) + 1;
        mergeDetectedUrlExtra(existing, extra);
        // Refresh title in case the first capture raced with a transient
        // empty-title state (loading SPA, etc).
        if (!existing.pageTitle) attachTabTitle(existing, details.tabId);
        notifyDetectedUrlsUpdated(details.tabId);
      }
    }
  } else {
    if (!orphanUrlKeys.has(detectionKey)) {
      orphanUrlKeys.add(detectionKey);
      orphanUrlInfos.push(urlInfo);
      pruneOrphans();
    } else {
      const existing = orphanUrlInfos.find(item => item && ((item.dedupeKey || item.url) === detectionKey));
      if (existing) {
        existing.url = urlInfo.url;
        existing.dedupeKey = detectionKey;
        existing.timestamp = urlInfo.timestamp;
        existing.pageUrl = urlInfo.pageUrl;
        existing.requestType = urlInfo.requestType;
        existing.frameId = urlInfo.frameId;
        existing.method = urlInfo.method;
        existing.hitCount = (Number(existing.hitCount) || 0) + 1;
        mergeDetectedUrlExtra(existing, extra);
        pruneOrphans();
      }
    }
  }

  if (isRealTab) updateBadge(details.tabId);
  chrome.storage.local.set({ detectedUrls: Array.from(detectedUrls) });
}

// Listen for web requests to detect video URLs by extension (m3u8, mpd, mp4)
chrome.webRequest.onBeforeRequest.addListener(
  function(details) {
    if (!userSettings.autoDetect) return;

    if (isCandidateVideoUrl(details.url)) {
      console.log('Detected video URL:', details.url);
      registerDetectedUrl(details);
      return;
    }

    const inferredManifest = inferHlsManifestFromSegmentUrl(details.url);
    if (inferredManifest) {
      console.log('Inferred HLS manifest from segment:', details.url, '->', inferredManifest.url);
      registerDetectedUrl(
        { ...details, url: inferredManifest.url },
        { detectedFormat: 'm3u8', playbackObserved: true, dedupeKey: inferredManifest.dedupeKey }
      );
    }
  },
  { urls: ["<all_urls>"] }
);

// Detect video manifests by response Content-Type header.
// Catches DASH/HLS manifests served from URLs without .mpd/.m3u8 extensions
// (e.g. API endpoints like /api/video/xxx that return MPD XML).
const MANIFEST_CONTENT_TYPES = {
  'application/dash+xml': 'mpd',
  'video/vnd.mpeg.dash.mpd': 'mpd',
  'application/vnd.apple.mpegurl': 'm3u8',
  'application/x-mpegurl': 'm3u8',
  'audio/mpegurl': 'm3u8',
  'audio/x-mpegurl': 'm3u8',
};

chrome.webRequest.onHeadersReceived.addListener(
  function(details) {
    if (!userSettings.autoDetect) return;
    if (isCandidateVideoUrl(details.url)) return;

    const ctHeader = (details.responseHeaders || [])
      .find(h => h.name.toLowerCase() === 'content-type');
    if (!ctHeader || !ctHeader.value) return;

    const ct = ctHeader.value.toLowerCase().split(';')[0].trim();
    const format = MANIFEST_CONTENT_TYPES[ct];
    if (!format) return;

    console.log('Detected video manifest by Content-Type:', details.url, '->', format);
    registerDetectedUrl(details, { detectedFormat: format });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// Capture actual request headers sent by the browser for video URLs
// This includes cookies that Chrome would send to the video domain
chrome.webRequest.onSendHeaders.addListener(
  function(details) {
    const urlLower = details.url.toLowerCase();
    
    // Capture headers for video URLs (by extension or Content-Type detection)
    if (isCandidateVideoUrl(details.url) || getDetectedFormat(details.url)) {
      // Convert headers array to object
      const headersObj = {};
      const SINGLETON_HEADERS = new Set(['User-Agent', 'Referer', 'Origin']);

      function mergeHeaderValue(existing, incoming, joiner) {
        const a = (existing ?? '').toString().trim();
        const b = (incoming ?? '').toString().trim();
        if (!a) return b;
        if (!b) return a;
        if (a === b) return a;
        return `${a}${joiner}${b}`;
      }

      if (details.requestHeaders) {
        for (const header of details.requestHeaders) {
          // Skip some internal headers
          if (!header.name.toLowerCase().startsWith(':')) {
            // Normalize common header casing so later lookups are reliable
            const nameLower = header.name.toLowerCase();
            let key = header.name;
            if (nameLower === 'cookie') key = 'Cookie';
            if (nameLower === 'referer') key = 'Referer';
            if (nameLower === 'origin') key = 'Origin';
            if (nameLower === 'user-agent') key = 'User-Agent';

            // Avoid data loss if Chrome ever sends duplicated headers with different casing.
            // Merge where it is safe/expected; otherwise keep the first seen value.
            if (headersObj[key] === undefined) {
              headersObj[key] = header.value;
            } else if (key === 'Cookie') {
              headersObj[key] = mergeHeaderValue(headersObj[key], header.value, '; ');
            } else if (!SINGLETON_HEADERS.has(key)) {
              headersObj[key] = mergeHeaderValue(headersObj[key], header.value, ', ');
            }
          }
        }
      }

      // Mark MP4 Range requests as a strong "actively playing" signal.
      const hasRange = Object.keys(headersObj).some(k => typeof k === 'string' && k.toLowerCase() === 'range');
      if (hasRange && details.tabId != null && typeof details.tabId === 'number' && details.tabId >= 0) {
        const tabList = currentTabUrls[details.tabId];
        if (Array.isArray(tabList)) {
          const item = tabList.find(x => x && x.url === details.url);
          if (item) {
            item.rangeHitCount = (Number(item.rangeHitCount) || 0) + 1;
            item.timestamp = Date.now();
            item.requestType = item.requestType || details.type;
            notifyDetectedUrlsUpdated(details.tabId);
          }
        }
      }
      
      // Store headers keyed by URL
      capturedHeaders[details.url] = {
        headers: headersObj,
        timestamp: Date.now(),
        initiator: details.initiator || details.documentUrl,
        tabId: details.tabId
      };
      
      console.log('Captured headers for:', details.url);
      console.log('Headers:', headersObj);
      console.log('Has Cookie header:', !!headersObj['Cookie']);
      if (headersObj['Cookie']) {
        console.log('Cookie value:', headersObj['Cookie']);
      }
      
      // Clean up old entries (keep only last 100)
      const keys = Object.keys(capturedHeaders);
      if (keys.length > 100) {
        const oldest = keys.sort((a, b) => 
          capturedHeaders[a].timestamp - capturedHeaders[b].timestamp
        ).slice(0, keys.length - 100);
        oldest.forEach(k => delete capturedHeaders[k]);
      }
    }
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders", "extraHeaders"]
);

// Update badge with count of detected URLs
function updateBadge(tabId) {
  const count = currentTabUrls[tabId]?.length || 0;
  if (count > 0) {
    chrome.action.setBadgeText({ text: count.toString(), tabId: tabId });
    chrome.action.setBadgeBackgroundColor({ color: '#4CAF50', tabId: tabId });
  }
}

// Clean up when tab is closed
chrome.tabs.onRemoved.addListener((tabId) => {
  delete currentTabUrls[tabId];
  delete currentTabUrlKeys[tabId];
  delete deepHitsByTab[tabId];
  delete userClickedVideoByTab[tabId];
  delete pageThumbnailsByTab[tabId];
});

// Clear detected URLs when page navigates or reloads
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) { // Only for main frame
    // Clear URLs for this tab on navigation/reload
    currentTabUrls[details.tabId] = [];
    currentTabUrlKeys[details.tabId] = new Set();
    deepHitsByTab[details.tabId] = [];
    delete userClickedVideoByTab[details.tabId];
    delete pageThumbnailsByTab[details.tabId];
    updateBadge(details.tabId);
    chrome.storage.local.set({ detectedUrls: Array.from(detectedUrls) });
  }
});

// Create context menu
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "sendToNAS",
    title: "Send to NAS",
    contexts: ["link", "page"]
  });
});

// Handle context menu clicks
chrome.contextMenus.onClicked.addListener((info, tab) => {
  let url = info.linkUrl || info.pageUrl;
  
  // Check if it's a video URL (m3u8, mpd, or mp4)
  const urlLower = url ? url.toLowerCase() : '';
  const isVideoUrl = url && isCandidateVideoUrl(url);
  if (isVideoUrl) {
    sendToNAS(url, tab.title, tab.url, tab && tab.id != null ? tab.id : null);
  } else {
    // Try to find video URL in current tab
    const tabUrls = currentTabUrls[tab.id];
    if (tabUrls && tabUrls.length > 0) {
      // Send the best candidate (prefer "now playing" heuristics)
      const best = getSortedUrlsForTab(tab.id)[0];
      sendToNAS(best.url, tab.title, tab.url, tab.id);
    } else {
      showNotification('Error', 'No video URL found on this page');
    }
  }
});

function getDetectedUrlInfo(url) {
  for (const tabId of Object.keys(currentTabUrls)) {
    const list = currentTabUrls[tabId];
    if (!Array.isArray(list)) continue;
    const item = list.find(x => x && x.url === url);
    if (item) return item;
  }
  for (const item of orphanUrlInfos) {
    if (item && item.url === url) return item;
  }
  return null;
}

// Check if a URL was detected via Content-Type (stored in per-tab or orphan lists)
function getDetectedFormat(url) {
  const item = getDetectedUrlInfo(url);
  return (item && item.detectedFormat) ? item.detectedFormat : null;
}

// Find the page title that was captured when a URL was first detected.
// This is the source of truth for "what tab/page this URL came from" — using
// it avoids the multi-tab bug where the active tab at click-time could be
// different from the tab the URL was actually detected on.
function getStoredPageTitle(url) {
  for (const tabId of Object.keys(currentTabUrls)) {
    const list = currentTabUrls[tabId];
    if (!Array.isArray(list)) continue;
    const item = list.find(x => x && x.url === url);
    if (item && item.pageTitle) return item.pageTitle;
  }
  for (const item of orphanUrlInfos) {
    if (item && item.url === url && item.pageTitle) return item.pageTitle;
  }
  return null;
}

// Format a FastAPI error response (`{detail: ...}`) into a readable string.
// `detail` may be a string (HTTPException) or an array of {loc, msg, type}
// validator errors (422). Without this, Array→Error stringifies to
// "[object Object]" and the user sees nothing useful.
function formatApiErrorDetail(errorJson, httpStatus) {
  const detail = errorJson && errorJson.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map(err => {
        const loc = Array.isArray(err.loc) ? err.loc.filter(p => p !== 'body').join('.') : '';
        const msg = err.msg || err.message || JSON.stringify(err);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join('; ');
  }
  if (detail && typeof detail === 'object') {
    try { return JSON.stringify(detail); } catch (_) { /* fall through */ }
  }
  return `HTTP ${httpStatus || 'error'} from NAS`;
}

// ---------- Hidden-mode AV-task pipeline ----------

// Persistent task history. Single source of truth for both the side panel's
// "recent tasks" list and the options page's full table. Lives in
// chrome.storage.local (per-device — avTaskHistory can grow large with URLs,
// stays out of the 100KB chrome.storage.sync ceiling).
//
// Row shape: { id, code, url, status, message?, submittedAt, sentAt?,
//              manifestUrl?, jobId?, jobTitle? }
// Newest first. Bounded to AV_HISTORY_MAX so the table stays snappy.
//
// Serialised through a single-slot promise chain so concurrent
// fetch+update bursts (user mashing Enter on different codes) don't lose
// rows the way a naive read-modify-write would.
let _avHistoryChain = Promise.resolve();
function _avHistoryDo(work) {
  const next = _avHistoryChain.then(work);
  _avHistoryChain = next.catch((err) => {
    console.error('avTaskHistory mutation failed:', err);
  });
  return next;
}

function avHistoryAppend(entry) {
  return _avHistoryDo(async () => {
    const stored = await chrome.storage.local.get(['avTaskHistory']);
    const list = Array.isArray(stored.avTaskHistory) ? stored.avTaskHistory : [];
    list.unshift(entry);
    if (list.length > AV_HISTORY_MAX) list.length = AV_HISTORY_MAX;
    await chrome.storage.local.set({ avTaskHistory: list });
    return entry;
  });
}

function avHistoryUpdate(historyId, patch) {
  return _avHistoryDo(async () => {
    const stored = await chrome.storage.local.get(['avTaskHistory']);
    const list = Array.isArray(stored.avTaskHistory) ? stored.avTaskHistory : [];
    const idx = list.findIndex(x => x && x.id === historyId);
    if (idx === -1) return null;
    list[idx] = { ...list[idx], ...patch };
    await chrome.storage.local.set({ avTaskHistory: list });
    return list[idx];
  });
}

function _newHistoryId() {
  // Crypto-strong random suffix; avoids collisions when the user fires
  // tasks faster than ms-resolution timestamps.
  return `av_${Date.now()}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// Two-phase AV-task pipeline:
//   Phase 1 — missav template (`hidden_mode.url_template`, default
//     `https://missav.ws/dm18/{code}`) in an INACTIVE background tab. Fully
//     automatic: the site's JS produces a fresh signed m3u8 under a real
//     Chrome browsing context and the existing detection pipeline
//     (registerDetectedUrl → maybeFireAvTaskAutoSend) ships it to NAS
//     without bothering the user. This is the v2.2.0 behaviour — most
//     codes resolve here.
//   Phase 2 — jav101 search page in an ACTIVE (foreground) tab. Only
//     reached if phase 1 doesn't produce a manifest inside
//     AV_TASK_TIMEOUT_MS. The user manually clicks the download button and
//     solves the reCAPTCHA; the unblocked request to
//     dl*.jav101.com/<file>.mp4 is picked up by the same pipeline and
//     shipped as a single direct mp4 (no HLS, no segment auth). Foreground
//     because captcha solve requires user interaction.
// The history row stays `pending` across the transition; its `url` field
// updates from missav → jav101 so the table reflects which site is
// currently being attempted.
async function handleAvTaskFetch(request) {
  const missavUrl = request && request.url;
  const code = request && request.code;
  if (!missavUrl || !code) {
    return { error: 'avTaskFetch missing url/code' };
  }
  const historyId = _newHistoryId();
  await avHistoryAppend({
    id: historyId,
    code,
    url: missavUrl,
    status: 'pending',
    submittedAt: Date.now(),
  });

  try {
    const tab = await chrome.tabs.create({ url: missavUrl, active: false });
    if (!tab || tab.id == null) {
      return launchAvTaskJav101Fallback(historyId, code);
    }
    setupAvPendingTab(tab.id, {
      code,
      historyId,
      phase: 'missav',
      onTimeout: () => {
        console.warn('AV task missav phase timed out, falling back to jav101:', code);
        // Remove the helper tab BEFORE opening jav101. cleanupAvTask (called
        // inside setupAvPendingTab's timeout wrapper) already deleted the
        // pending entry, so chrome.tabs.onRemoved sees nothing and won't
        // mark the row as failed mid-transition.
        try { chrome.tabs.remove(tab.id, () => void chrome.runtime.lastError); } catch (_) {}
        launchAvTaskJav101Fallback(historyId, code);
      },
    });
    return { success: true, tabId: tab.id, historyId };
  } catch (e) {
    return launchAvTaskJav101Fallback(historyId, code);
  }
}

async function launchAvTaskJav101Fallback(historyId, code) {
  const jav101Url = `https://jav101.com/search/${encodeURIComponent(code)}`;
  await avHistoryUpdate(historyId, { url: jav101Url });
  try {
    // Active tab — the user needs to see the page to solve the reCAPTCHA
    // and click the download button. If they're elsewhere, the tab pops to
    // the front to flag that the task needs their attention.
    const tab = await chrome.tabs.create({ url: jav101Url, active: true });
    if (!tab || tab.id == null) {
      const msg = 'failed to open fallback tab';
      await avHistoryUpdate(historyId, { status: 'failed', message: msg });
      broadcastAvTaskUpdate(code, 'failed', msg);
      return { error: msg };
    }
    setupAvPendingTab(tab.id, {
      code,
      historyId,
      phase: 'jav101',
      onTimeout: () => {
        console.warn('AV task jav101 fallback timed out:', code, jav101Url);
        const msg = 'Timed out on missav + jav101 — no manifest detected.';
        avHistoryUpdate(historyId, { status: 'failed', message: msg });
        broadcastAvTaskUpdate(code, 'failed', msg);
      },
    });
    return { success: true, tabId: tab.id, historyId };
  } catch (e) {
    const msg = (e && e.message) || 'tabs.create threw on jav101 fallback';
    await avHistoryUpdate(historyId, { status: 'failed', message: msg });
    broadcastAvTaskUpdate(code, 'failed', msg);
    return { error: msg };
  }
}

function setupAvPendingTab(tabId, { code, historyId, phase, onTimeout }) {
  const handle = setTimeout(() => {
    const pending = avPendingTabs[tabId];
    if (!pending || pending.fired) return;
    cleanupAvTask(tabId);
    onTimeout();
  }, AV_TASK_TIMEOUT_MS);
  avPendingTabs[tabId] = {
    code,
    requestedAt: Date.now(),
    fired: false,
    timeoutHandle: handle,
    historyId,
    phase,
  };
}

// jav101 play pages fire several .mp4 requests on load (preview clips,
// thumbnails, ads). Only the post-captcha SIGNED download lives on a
// dl*.jav101.com subdomain — restrict the auto-send to those so we don't
// accidentally ship a 30-second preview to the NAS.
function isJav101DownloadUrl(url) {
  try {
    const u = new URL(url);
    // .endsWith('.jav101.com') matches dl3.jav101.com etc. but not the
    // apex jav101.com itself, which is what serves the page chrome.
    return u.hostname.endsWith('.jav101.com');
  } catch (_) {
    return false;
  }
}

// Fast-fail the missav phase when it's clearly never going to produce a
// manifest: HTTP 4xx/5xx on the main_frame response (e.g.
// https://missav.ws/dm18/orecz-214 returns 404 because the code path
// doesn't exist on missav). Without this, the user waits the full 60s
// timeout for a page that's already declared "not found" in <100ms.
// Filter to type:'main_frame' so that ad subframes / API errors don't
// trigger spurious failovers. jav101 phase isn't included — its search
// page returns 200 with empty results when a code is unknown, so HTTP
// status alone can't classify success/failure there.
chrome.webRequest.onHeadersReceived.addListener(
  function(details) {
    if (details.type !== 'main_frame') return;
    const pending = avPendingTabs[details.tabId];
    if (!pending || pending.fired) return;
    if (pending.phase !== 'missav') return;
    const status = details.statusCode || 0;
    if (status < 400) return;  // 2xx success or 3xx redirect — let it ride

    console.warn(
      'AV task missav phase got HTTP', status,
      '— failing over to jav101 immediately:', pending.code
    );
    const { historyId, code } = pending;
    cleanupAvTask(details.tabId);
    try { chrome.tabs.remove(details.tabId, () => void chrome.runtime.lastError); } catch (_) {}
    launchAvTaskJav101Fallback(historyId, code);
  },
  { urls: ['<all_urls>'], types: ['main_frame'] }
);

// Called from registerDetectedUrl when a NEW (not duplicate) manifest is
// captured for a tab. If that tab is one we opened for an AV task, fire
// sendToNAS for that exact URL — same path the user-clicked Send takes.
function maybeFireAvTaskAutoSend(tabId, manifestUrl) {
  const pending = avPendingTabs[tabId];
  if (!pending || pending.fired) return;

  // Only trigger on URLs we'd accept from a normal Send — avoids firing on
  // every byte-range probe or unrelated subresource.
  if (!isCandidateVideoUrl(manifestUrl) && !getDetectedFormat(manifestUrl)) return;

  // Phase-specific filter: jav101 pages emit preview .mp4s on page load
  // (and possibly ad mp4s); we only want the signed dl*.jav101.com download
  // that fires after the user solves the captcha. Anything else on the
  // jav101 phase is ignored — the timeout will fall back to missav if the
  // user never reaches the download.
  if (pending.phase === 'jav101' && !isJav101DownloadUrl(manifestUrl)) return;

  pending.fired = true;
  if (pending.timeoutHandle) clearTimeout(pending.timeoutHandle);

  // The capture pipeline writes capturedHeaders[manifestUrl] just before
  // emitting registerDetectedUrl, but on some sites the headers race lands
  // immediately after; sendToNAS()'s findBestCapturedEntry() handles either
  // ordering, so we can fire right away.
  //
  // Title resolution priority (best → worst):
  //   1. tab.title at THIS moment — set by the page's <title> by the time
  //      the player JS has fired the m3u8 request; on missav this is the
  //      full video name (e.g. "NTTR-015 - 寝取られ NTR... - MissAV").
  //   2. getStoredPageTitle() cache — earlier registerDetectedUrl invocation
  //      may have populated this via attachTabTitle's async tabs.get.
  //   3. `[code]` placeholder — last resort so the job is at least
  //      identifiable in /api/jobs.
  // We deliberately prefer (1) over (2) because the cached title might
  // have been captured during initial loading (before the SPA's title
  // update) whereas the at-send-time read reflects the page's settled state.
  chrome.tabs.get(tabId, (tab) => {
    const pageUrl = tab && tab.url ? tab.url : '';
    const liveTitle = (tab && tab.title) ? String(tab.title).trim() : '';
    const cachedTitle = getStoredPageTitle(manifestUrl);
    const title = liveTitle || cachedTitle || `[${pending.code}]`;
    sendToNAS(manifestUrl, title, pageUrl, tabId)
      .then(() => {
        avHistoryUpdate(pending.historyId, {
          status: 'sent',
          sentAt: Date.now(),
          manifestUrl,
          jobTitle: title,
        });
        broadcastAvTaskUpdate(pending.code, 'sent');
        // Auto-close the helper tab a few seconds after Send so any
        // late header captures (cookies refreshed by player JS) still
        // land. Best-effort — survive errors silently.
        setTimeout(() => {
          try { chrome.tabs.remove(tabId, () => void chrome.runtime.lastError); } catch (_) {}
          cleanupAvTask(tabId);
        }, AV_TASK_AUTOCLOSE_DELAY_MS);
      })
      .catch((err) => {
        const msg = (err && err.message) || 'sendToNAS failed';
        avHistoryUpdate(pending.historyId, { status: 'failed', message: msg });
        broadcastAvTaskUpdate(pending.code, 'failed', msg);
        cleanupAvTask(tabId);
      });
  });
}

function cleanupAvTask(tabId) {
  const pending = avPendingTabs[tabId];
  if (pending && pending.timeoutHandle) clearTimeout(pending.timeoutHandle);
  delete avPendingTabs[tabId];
}

// Notify any listening sidepanel about the task's progress. Use sendMessage;
// missing-listener errors are expected (sidepanel may be closed) and silenced.
function broadcastAvTaskUpdate(code, status, message) {
  try {
    chrome.runtime.sendMessage(
      { action: 'avTaskUpdate', code, status, message },
      () => void chrome.runtime.lastError  // silence "no receivers" complaint
    );
  } catch (_) { /* ignore */ }
}

// If the user closes the helper tab manually, clean up our tracking.
chrome.tabs.onRemoved.addListener((tabId) => {
  if (avPendingTabs[tabId]) {
    const pending = avPendingTabs[tabId];
    if (!pending.fired) {
      const msg = 'Tab closed before manifest detected.';
      avHistoryUpdate(pending.historyId, { status: 'failed', message: msg });
      broadcastAvTaskUpdate(pending.code, 'failed', msg);
    }
    cleanupAvTask(tabId);
  }
});

// Send URL to NAS. `sourceTabId` is the tab the user clicked Send from
// (sidepanel passes its activeTabId; context-menu passes tab.id; AV-task
// auto-send passes the helper tab id). It anchors the captured-header
// substitution to that exact tab so multi-tab same-site sessions don't leak
// each other's video URLs through findBestCapturedEntry. Optional — when
// missing, substitution falls back to strict initiator equality.
async function sendToNAS(url, pageTitle, pageUrl, sourceTabId) {
  try {
    // Codex review (P1): `getDetectedFormat()` only returns a value
    // when the URL was detected via Content-Type sniffing. Most
    // streams are detected via URL-pattern matching in
    // onBeforeRequest, where `detectedFormat` stays unset → null.
    // The browser-side router below then keeps `useBrowserSide`
    // false and the cookie/IP-bound streams this whole feature
    // exists for fall back to NAS-direct.
    //
    // Fall back to URL-suffix sniffing for the common .m3u8 / .mpd
    // case so the routing decision matches user intent. Same
    // pattern as `probeVideoMeta`, which already does this.
    let formatHint = getDetectedFormat(url);
    if (!formatHint) {
      const lower = url.toLowerCase();
      if (lower.includes('.m3u8')) formatHint = 'm3u8';
      else if (lower.includes('.mpd')) formatHint = 'mpd';
    }
    if (!isCandidateVideoUrl(url) && !formatHint) {
      showNotification('Error', 'Not a valid video URL');
      return;
    }

    // Get settings
    const settings = await chrome.storage.sync.get([
      'nasEndpoint', 'apiKey', 'nasOutputSubdir', 'useBrowserSide',
      'trustedCdnSuffixes',
    ]);

    if (!settings.nasEndpoint || !settings.apiKey) {
      showNotification('Configuration Required', 'Please configure NAS settings in extension options');
      chrome.runtime.openOptionsPage();
      return;
    }
    
    // Extract title from page title or URL
    let title = pageTitle || 'Untitled Video';
    // Clean up title
    title = title.replace(/[<>:"/\\|?*]/g, '').substring(0, 100);
    
    // First, try to use captured headers for this exact URL.
    // If not found (or if the user clicked an older detected URL), pick the best recent
    // captured m3u8 request from the active tab. Many players request the *real* m3u8
    // with expiring tokens or different bitrate paths, so exact matching is often too strict.
    let finalHeaders = {};
    let urlToSend = url;

    const targetInfo = getDetectedUrlInfo(url);
    let captured = capturedHeaders[url];
    const best = findBestCapturedEntry(url, pageUrl, sourceTabId);
    const keepClickedDetectedSignedUrl = shouldKeepClickedDetectedSignedUrl(url, targetInfo, best);

    // Use the best captured m3u8 when it's a strong match, even if we have
    // an exact key hit. Exact matches are often "clean" URLs (no token)
    // while the real player request contains query params. The same-tab
    // hard filter inside findBestCapturedEntry guarantees `best` (when
    // present) came from the source tab — no cross-origin or cross-tab
    // substitution is possible at this point.
    const shouldUseBest =
      !!best &&
      !keepClickedDetectedSignedUrl &&
      (captured == null || best.score >= 15 || (best.entry && best.entry.timestamp && captured.timestamp && best.entry.timestamp > captured.timestamp));

    if (shouldUseBest) {
      captured = best.entry;
      urlToSend = best.url;
      console.log('Using best captured manifest for this URL\'s source tab:', urlToSend);
    } else if (keepClickedDetectedSignedUrl) {
      console.log('Keeping clicked detected signed manifest over older captured entry:', urlToSend);
    }
    
    if (captured && captured.headers) {
      console.log('Using captured browser headers for:', urlToSend);
      finalHeaders = { ...captured.headers };
      
      // Remove headers that shouldn't be forwarded
      // (Handle common case variants because captured headers aren't guaranteed casing)
      delete finalHeaders['Host'];
      delete finalHeaders['host'];
      delete finalHeaders['Connection'];
      delete finalHeaders['connection'];
      delete finalHeaders['Content-Length'];
      delete finalHeaders['content-length'];
      delete finalHeaders['Accept-Encoding']; // Let the worker handle compression
      delete finalHeaders['accept-encoding'];
    }
    
    // Also get cookies for the source page domain (fallback)
    try {
      const pageUrlObj = new URL(pageUrl);
      const pageCookies = await chrome.cookies.getAll({ url: pageUrl });
      
      console.log(`Getting cookies for source page: ${pageUrl}`);
      console.log(`Found ${pageCookies.length} cookies`);
      
      // If we don't have captured cookies, use page cookies
      if (!finalHeaders['Cookie'] && pageCookies.length > 0) {
        finalHeaders['Cookie'] = pageCookies.map(cookie => `${cookie.name}=${cookie.value}`).join('; ');
        console.log('Using source page cookies as fallback');
      }
    } catch (error) {
      console.error('Failed to get page cookies:', error);
    }
    
    // Also get cookies for the m3u8 URL domain
    try {
      const m3u8UrlObj = new URL(urlToSend);
      const topLevelSite = (() => {
        try {
          return new URL(pageUrl).origin;
        } catch (_) {
          return null;
        }
      })();

      // Note: Some sites use partitioned cookies (CHIPS). In that case, getAll({url})
      // can return 0 even though Chrome will send cookies during playback.
      let m3u8Cookies = await chrome.cookies.getAll({ url: urlToSend });
      if (m3u8Cookies.length === 0 && topLevelSite) {
        try {
          const partitioned = await chrome.cookies.getAll({
            url: urlToSend,
            partitionKey: { topLevelSite }
          });
          if (partitioned && partitioned.length > 0) {
            m3u8Cookies = partitioned;
            console.log('Found partitioned cookies for m3u8 domain');
          }
        } catch (e) {
          console.warn('Failed to get partitioned cookies:', e);
        }
      }
      
      console.log(`Getting cookies for m3u8 URL: ${urlToSend}`);
      console.log(`Found ${m3u8Cookies.length} cookies for m3u8 domain`);
      
      if (m3u8Cookies.length > 0) {
        const m3u8CookieStr = m3u8Cookies.map(cookie => `${cookie.name}=${cookie.value}`).join('; ');
        // Merge with existing cookies if any
        if (finalHeaders['Cookie']) {
          // Combine both, avoiding duplicates
          const existingCookies = new Set(finalHeaders['Cookie'].split('; '));
          m3u8CookieStr.split('; ').forEach(c => existingCookies.add(c));
          finalHeaders['Cookie'] = Array.from(existingCookies).join('; ');
        } else {
          finalHeaders['Cookie'] = m3u8CookieStr;
        }
        console.log('Added m3u8 domain cookies');
      }
    } catch (error) {
      console.error('Failed to get m3u8 domain cookies:', error);
    }
    
    // Prepare request body
    const requestBody = {
      url: urlToSend,
      title: title,
      source_page: pageUrl,
      referer: pageUrl,
      headers: finalHeaders
    };
    if (formatHint) {
      requestBody.format = formatHint;
    }
    if (settings.nasOutputSubdir && settings.nasOutputSubdir.trim()) {
      requestBody.output_subdir = settings.nasOutputSubdir.trim();
    }
    
    console.log('Sending to NAS:');
    console.log('  URL:', requestBody.url);
    console.log('  Title:', requestBody.title);
    console.log('  Referer:', requestBody.referer);
    console.log('  Headers keys:', Object.keys(requestBody.headers));
    console.log('  Has Cookie:', !!requestBody.headers.Cookie);
    if (requestBody.headers.Cookie) {
      console.log('  Cookie preview:', requestBody.headers.Cookie.substring(0, 200));
    }
    
    // v2.5: route HLS/DASH through browser-side path when the user has it
    // enabled (default on for hls/dash). The browser-side path fetches
    // segments in this extension's session — solves expired-token / IP-bound
    // / cookie-bound URLs that nas-direct can't reach. MP4 still goes
    // through nas-direct: it's a single GET, the NAS pipeline already
    // handles range requests, and there's no payoff in browser-side fetch.
    let useBrowserSide =
      (settings.useBrowserSide !== false) &&
      (formatHint === 'm3u8' || formatHint === 'mpd');

    // Pre-check the master URL against the browser-side safety gate
    // before attempting browser-side mode. Private/metadata/local or
    // split-horizon-looking rejections are terminal: silently sending
    // them to /api/download would bypass the always-on browser-side
    // gate because the legacy NAS SSRF guard is environment-gated.
    // The only compatibility fallback we keep is a public-looking
    // same-site plain-HTTP legacy stream, which browser-side rejects
    // solely because it cannot use HTTPS DNS-rebinding protection.
    if (useBrowserSide && requestBody && requestBody.url) {
      const masterSafety = _wv2nasIsManifestUrlSafeForBrowser(
        requestBody.url,
        pageUrl || requestBody.source_page || null,
        settings.trustedCdnSuffixes,
      );
      if (!masterSafety.safe) {
        if (_wv2nasCanUseNasDirectForBrowserUnsafeUrl(
          requestBody.url,
          pageUrl || requestBody.source_page || null,
        )) {
          console.log(
            `[wv2nas] Browser-side skipped for ${requestBody.url} `
            + `(${masterSafety.reason}); routing HTTP legacy stream to NAS-direct`,
          );
          useBrowserSide = false;
        } else {
          throw new Error(
            `Unsafe browser-side manifest URL refused: ${masterSafety.reason}`,
          );
        }
      }
    }

    if (useBrowserSide) {
      let browserSideErr = null;
      try {
        await runBrowserSideJob({
          nasEndpoint: settings.nasEndpoint,
          apiKey: settings.apiKey,
          requestBody,
          title,
          pageUrl,
          formatHint,
          trustedCdnSuffixes: settings.trustedCdnSuffixes,
        });
        return;
      } catch (err) {
        browserSideErr = err;
      }
      // Codex adversarial-review: legacy compat fallback is now
      // gated to 404 only (legacy NAS without /api/jobs/init). Other
      // 4xx — especially 422 from the URL safety gate — are TERMINAL
      // because /api/download's SSRF guard is env-gated and would
      // re-open the exact intranet/metadata access the gate blocks.
      // The proactive master-URL pre-check above already handles
      // legitimate HTTP-only streams without going through 422.
      if (!browserSideErr.fallbackable) {
        console.error('Browser-side job failed:', browserSideErr);
        showNotification(
          'Browser-side job failed',
          browserSideErr.message || String(browserSideErr),
        );
        return;
      }
      console.warn(
        '[wv2nas] Browser-side init returned 404 (legacy NAS?); '
        + 'falling back to NAS-direct:',
        browserSideErr.message,
      );
      // Fall through to the NAS-direct submission below.
    }

    // Send to NAS API
    const response = await fetch(`${settings.nasEndpoint}/api/download`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${settings.apiKey}`
      },
      body: JSON.stringify(requestBody)
    });
    
    if (!response.ok) {
      // FastAPI returns `detail` either as a plain string (HTTPException) or as
      // a list of validator-error objects (422 Pydantic errors). Naively passing
      // the latter to `new Error()` produced "[object Object]" notifications.
      const errorJson = await response.json().catch(() => ({}));
      const detail = formatApiErrorDetail(errorJson, response.status);
      const err = new Error(detail);
      // Tag rate-limit errors so the catch can use a more visible notification.
      // Easy to miss "Error: Rate limit exceeded ..." among 10+ stacked
      // system notifications during a bulk send — the user just sees clicks
      // disappearing into the void.
      if (response.status === 429) err.isRateLimit = true;
      throw err;
    }
    
    const result = await response.json();
    console.log('Download submitted:', result);
    
    // Show success notification
    if (userSettings.showNotifications) {
      showNotification(
        'Download Submitted',
        `"${title}" has been sent to NAS\nJob ID: ${result.id.substring(0, 8)}...`
      );
    }
    
    // Store job info
    storeJob(result);
    
  } catch (error) {
    console.error('Error sending to NAS:', error);
    if (error && error.isRateLimit) {
      // Hard-to-miss notification for rate-limit hits — the underlying API
      // message already names the env var to raise (RATE_LIMIT_PER_MINUTE).
      // requireInteraction keeps it on screen until the user dismisses it,
      // priority 2 floats it above other notifications, and we use a
      // dedicated id so multiple 429s in a burst collapse into one card
      // instead of stacking 10+ identical toasts.
      showNotification('NAS rate limit hit', error.message, {
        id: 'wv2nas-rate-limit',
        priority: 2,
        requireInteraction: true,
      });
    } else {
      showNotification('Error', error.message);
    }
  }
}

// Show notification. `opts` lets specific call sites override the defaults
// (id for collapsing duplicate cards, priority/requireInteraction for the
// must-not-miss case like rate-limit hits).
function showNotification(title, message, opts) {
  const o = opts || {};
  const options = {
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title: title,
    message: message,
    priority: typeof o.priority === 'number' ? o.priority : 0,
    requireInteraction: !!o.requireInteraction,
  };
  if (o.id) {
    chrome.notifications.create(o.id, options);
  } else {
    chrome.notifications.create(options);
  }
}

// Store job information.
// Serialised through a single-slot promise queue so concurrent sendToNAS
// completions don't overlap their read-modify-write of `jobs`. Without the
// queue, two parallel storeJob() calls each read the same snapshot, append
// their job, and the second write clobbers the first — so when several tabs
// finished submitting at once, only one of their jobs survived the local
// list. (Local storage is internal bookkeeping today, but the symptom would
// resurface the moment any UI starts reading from it.)
let _storeJobChain = Promise.resolve();
function storeJob(job) {
  const next = _storeJobChain.then(async () => {
    const jobs = await chrome.storage.local.get(['jobs']);
    const jobList = jobs.jobs || [];

    jobList.unshift({
      id: job.id,
      title: job.title,
      url: job.url,
      status: job.status,
      progress: job.progress,
      created_at: new Date().toISOString()
    });

    // Keep only last 50 jobs
    if (jobList.length > 50) {
      jobList.pop();
    }

    await chrome.storage.local.set({ jobs: jobList });
  });
  // Don't let one failure poison the chain — log and continue.
  _storeJobChain = next.catch((err) => {
    console.error('storeJob failed:', err);
  });
  return next;
}

// Listen for action clicks to open sidepanel
chrome.action.onClicked.addListener(async (tab) => {
  // Open sidepanel when extension icon is clicked
  await chrome.sidePanel.open({ tabId: tab.id });
});

// Listen for messages from sidepanel and content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  // Handle thumbnails scraped from the page (og:image + <video poster>)
  if (request.action === 'pageThumbnails') {
    const tabId = sender.tab?.id;
    if (tabId != null && tabId >= 0) {
      pageThumbnailsByTab[tabId] = {
        pageUrl: request.pageUrl || '',
        pageThumbnail: request.pageThumbnail || null,
        posters: Array.isArray(request.videoPosters) ? request.videoPosters : [],
      };
    }
    sendResponse({ success: true });
    return;
  }

  // Handle manifest detected by inject.js (fetch/XHR interception)
  if (request.action === 'manifestDetected') {
    const tabId = sender.tab?.id;
    const url = request.url;
    const format = request.format;
    if (url && format) {
      console.log('Manifest detected by content interception:', url, '->', format);
      const details = {
        url: url,
        tabId: (tabId != null && tabId >= 0) ? tabId : -1,
        initiator: request.pageUrl,
        documentUrl: request.pageUrl,
        type: 'xmlhttprequest',
        frameId: 0,
        method: 'GET'
      };
      registerDetectedUrl(details, { detectedFormat: format });
    }
    sendResponse({ success: true });
    return;
  }

  if (request.action === 'deepDetected') {
    const tabId = sender.tab?.id;
    registerDeepHit(tabId, {
      kind: request.kind,
      format: request.format,
      source: request.source,
      url: request.url,
      mime: request.mime,
      pageUrl: request.pageUrl,
      timestamp: request.timestamp,
    });
    sendResponse({ success: true });
    return;
  }

  // Handle user clicking on a video element (from content script)
  if (request.action === 'userClickedVideo' || request.action === 'videoStartedPlaying') {
    const tabId = sender.tab?.id;
    if (tabId != null && tabId >= 0) {
      const videoIndex = request.videoIndex;
      
      // Try to immediately associate with a URL based on video index
      let matchedUrl = null;
      
      // First, try direct src matching
      if (request.videoSrc && !request.videoSrc.startsWith('blob:')) {
        const list = currentTabUrls[tabId] || [];
        for (const item of list) {
          if (item.url === request.videoSrc) {
            matchedUrl = item.url;
            break;
          }
        }
      }
      
      // If no direct match, use video index to map to detection order
      if (!matchedUrl && typeof videoIndex === 'number' && videoIndex >= 0) {
        const list = currentTabUrls[tabId] || [];
        // Filter to m3u8 or mp4 URLs in detection order
        const m3u8Urls = list.filter(u => String(u.url || '').toLowerCase().includes('.m3u8'));
        const mpdUrls = list.filter(u => String(u.url || '').toLowerCase().includes('.mpd'));
        const mp4Urls = list.filter(u => String(u.url || '').toLowerCase().includes('.mp4'));
        const urlsInOrder = m3u8Urls.length > 0 ? m3u8Urls : (mpdUrls.length > 0 ? mpdUrls : mp4Urls);
        
        if (urlsInOrder.length > 0 && videoIndex < urlsInOrder.length) {
          matchedUrl = urlsInOrder[videoIndex].url;
        }
      }
      
      userClickedVideoByTab[tabId] = {
        videoSrc: request.videoSrc,
        videoIndex: request.videoIndex,
        videoCount: request.videoCount,
        pageUrl: request.pageUrl,
        timestamp: request.timestamp || Date.now(),
        matchedUrl: matchedUrl
      };
      
      console.log('User interacted with video in tab', tabId, 
        '- index:', request.videoIndex, 'of', request.videoCount,
        '- src:', request.videoSrc || '(blob/MediaSource)',
        '- matchedUrl:', matchedUrl || '(none)');
      notifyDetectedUrlsUpdated(tabId);
    }
    sendResponse({ success: true });
    return;
  }

  if (request.action === 'getDetectedUrls') {
    const requestedTabId = (request && typeof request.tabId === 'number') ? request.tabId : null;

    // If caller provided tabId, return that tab's URLs deterministically.
    if (requestedTabId != null && requestedTabId >= 0) {
      chrome.tabs.get(requestedTabId, (tab) => {
        // If the tab no longer exists, fall back to per-tab list only.
        const tabUrl = tab && tab.url ? tab.url : '';
        const urls = enrichWithThumbnails(
          getSortedUrlsForTabWithOrphans(requestedTabId, tabUrl),
          requestedTabId
        );
        scheduleProbesForRows(urls, requestedTabId);
        sendResponse({ urls, deepHits: getDeepHitsForTab(requestedTabId) });
      });
      return true;
    }

    // Fallback: Return URLs for current active tab
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.get(tabs[0].id, (tab) => {
          const tabUrl = tab && tab.url ? tab.url : (tabs[0].url || '');
          const urls = enrichWithThumbnails(
            getSortedUrlsForTabWithOrphans(tabs[0].id, tabUrl),
            tabs[0].id
          );
          scheduleProbesForRows(urls, tabs[0].id);
          sendResponse({ urls, deepHits: getDeepHitsForTab(tabs[0].id) });
        });
      } else {
        sendResponse({ urls: [], deepHits: [] });
      }
    });
    return true; // Keep channel open for async response
  }

  if (request.action === 'sendToNAS') {
    // Prefer the title that was captured when this URL was first detected —
    // that pins the title to the URL's source tab and survives the user
    // switching tabs before clicking Send. Fall back to whatever the caller
    // sent (right-click context menu provides the correct tab.title), then
    // to a generic placeholder.
    const titleToUse =
      getStoredPageTitle(request.url) || request.title || 'Untitled Video';
    // Anchor the captured-header substitution to the tab the user clicked
    // Send from. Without this, same-site multi-tab sessions leak each
    // other's URLs through findBestCapturedEntry's origin scoring (see the
    // hard filter there for the full story).
    const sourceTabId = (typeof request.tabId === 'number' && request.tabId >= 0)
      ? request.tabId : null;

    // Hold the message channel open until sendToNAS settles. Without `return
    // true` + a deferred sendResponse, Chrome considers this handler done the
    // moment we return synchronously, and the MV3 service worker becomes
    // eligible for shutdown between the awaits inside sendToNAS (storage.get
    // → cookies.getAll → fetch). When the user fires Send across multiple
    // tabs in quick succession, the first 1–2 land but the later ones lose
    // their in-flight chains to SW termination and never reach the NAS.
    sendToNAS(request.url, titleToUse, request.pageUrl, sourceTabId)
      .then(() => sendResponse({ success: true }))
      .catch((err) => {
        console.error('sendToNAS failed:', err);
        sendResponse({ success: false, error: err && err.message });
      });
    return true;
  }

  if (request.action === 'avTaskFetch') {
    // Hidden-mode quick-input: try the user's missav template first
    // (background tab, fully automatic — most codes resolve here), then
    // fall back to jav101 (foreground tab — user solves reCAPTCHA, signed
    // dl*.jav101.com mp4 fires) on timeout. Both phases feed the same
    // registerDetectedUrl → maybeFireAvTaskAutoSend pipeline.
    handleAvTaskFetch(request)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ error: err && err.message }));
    return true;
  }

  if (request.action === 'clearDetected') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        currentTabUrls[tabs[0].id] = [];
        updateBadge(tabs[0].id);
      }
    });
    sendResponse({ success: true });
  }

});

// ---------------------------------------------------------------------------
// v2.5 browser-side job orchestrator.
//
// SW-side responsibility:
//   1. POST /api/jobs/init with manifest URL or text + headers we captured
//      → server returns the segment plan (URLs + AES key URI + IV).
//   2. Build DNR session rules so segment fetches in offscreen carry the
//      Referer/Origin/UA we recorded from the player.
//   3. Open offscreen.html and post START_BROWSER_JOB with the plan.
//   4. Wait for BROWSER_JOB_DONE / BROWSER_JOB_FAILED — clean up DNR rules
//      and close the offscreen document either way.
//
// dnrRules.js / segmentDownloader.js are full ES modules with their own
// vitest coverage; we don't import them here because background.js is a
// classic service worker. The DNR helpers below mirror dnrRules.js's
// shape — keep them in sync if the ruleset evolves.
// ---------------------------------------------------------------------------

// DNR ID allocation strategy (post-Codex review): each concurrent
// browser-side job claims a unique slot in the rule ID space. Without
// this, two parallel jobs would build rules at the same constant base
// and either silently overwrite each other's spoof rules (via
// updateSessionRules) or have one job's cleanup remove the other's
// active rules. MV3 caps session rules at 5000; we cap at 50 slots ×
// 100 IDs/slot = 5000.
const _BROWSER_DNR_BASE_ID = 10000;
const _BROWSER_DNR_PER_JOB_RANGE = 100;
const _BROWSER_DNR_RESPONSE_OFFSET = 50; // within a slot, response rules at base+50
const _BROWSER_DNR_MAX_SLOTS = 50;
const _BROWSER_DNR_MAX_PACKED_REGEX_FILTER_LENGTH = 1800;
const _wv2nasUsedDnrSlots = new Set();

// Per-job runtime state (Codex review #2): registered when a job starts,
// deleted in cleanup. Used to ref-count the offscreen document so a
// finishing job doesn't close offscreen out from under another that's
// still mid-segment.
const _wv2nasActiveBrowserJobs = new Map(); // jobId -> { dnrSlot, ruleIds }

function _wv2nasAllocateDnrSlot() {
  for (let slot = 0; slot < _BROWSER_DNR_MAX_SLOTS; slot++) {
    if (!_wv2nasUsedDnrSlots.has(slot)) {
      _wv2nasUsedDnrSlots.add(slot);
      return slot;
    }
  }
  throw new Error(
    `Browser-side DNR slots exhausted (max ${_BROWSER_DNR_MAX_SLOTS} concurrent jobs).`
  );
}

function _wv2nasReleaseDnrSlot(slot) {
  _wv2nasUsedDnrSlots.delete(slot);
}

function _wv2nasUrlsToFilters(urls) {
  const groups = new Map();
  for (const u of urls) {
    let parsed;
    try { parsed = new URL(u); } catch (_) { continue; }
    const origin = `${parsed.protocol}//${parsed.host}`;
    const dir = parsed.pathname.replace(/[^/]*$/, '');
    const key = origin + dir;
    if (!groups.has(key)) groups.set(key, { origin, dir });
  }
  const filters = [];
  for (const { origin, dir } of groups.values()) {
    const escaped = (origin + dir).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    filters.push(`^${escaped}.*`);
  }
  return filters;
}

function _wv2nasRegexFilterToPackedTerm(regexFilter) {
  if (regexFilter.startsWith('^') && regexFilter.endsWith('.*')) {
    return `${regexFilter.slice(1, -2)}.*`;
  }
  return regexFilter.startsWith('^') ? regexFilter.slice(1) : regexFilter;
}

function _wv2nasPackedRegexLength(terms) {
  if (terms.length === 1) return 1 + terms[0].length;
  return '^(?:'.length + terms.join('|').length + ')'.length;
}

function _wv2nasPackDnrFilters(filters) {
  const packed = [];
  let current = [];

  function flush() {
    if (current.length === 0) return;
    packed.push(current.length === 1 ? `^${current[0]}` : `^(?:${current.join('|')})`);
    current = [];
  }

  for (const filter of Array.from(new Set(filters))) {
    const term = _wv2nasRegexFilterToPackedTerm(filter);
    if (
      current.length > 0
      && _wv2nasPackedRegexLength([...current, term]) > _BROWSER_DNR_MAX_PACKED_REGEX_FILTER_LENGTH
    ) {
      flush();
    }
    current.push(term);
  }
  flush();

  return packed;
}

// `idBase` is the starting rule ID for THIS job's slot — the caller
// allocates it via _wv2nasAllocateDnrSlot. Within each slot we have
// _BROWSER_DNR_RESPONSE_OFFSET (50) request-rule slots and 50 paired
// response-rule slots. Trusted URL prefixes above that count are packed
// into anchored alternation filters before we spend rule IDs.
//
// Codex review #10: CORS-relax response rules ONLY apply to
// `trustedSegmentUrls` (subset of `segmentUrls` inside the manifest's
// trusted host boundary). Foreign origins get neither
// header-spoof nor CORS-relax; fetch uses default browser headers and
// the response remains unreadable cross-origin, defeating credential
// leakage and DNS-rebinding exfil.
function _wv2nasBuildDnrRules({
  segmentUrls, trustedSegmentUrls, referer, origin, userAgent, idBase,
  initiatorDomain,
}) {
  const trustedFilters = new Set(_wv2nasUrlsToFilters(
    trustedSegmentUrls === undefined ? segmentUrls : trustedSegmentUrls
  ));
  let filters = _wv2nasUrlsToFilters(segmentUrls)
    .filter((filter) => trustedFilters.has(filter));
  if (filters.length === 0) return [];
  if (filters.length > _BROWSER_DNR_RESPONSE_OFFSET) {
    const originalFilterCount = filters.length;
    filters = _wv2nasPackDnrFilters(filters);
    if (filters.length > _BROWSER_DNR_RESPONSE_OFFSET) {
      // Don't silently truncate — fail loudly so the caller knows something
      // about the segment URL distribution is unusual.
      throw new Error(
        `Too many trusted segment URL groups (${originalFilterCount}) for DNR slot; `
        + `packed to ${filters.length}, max ${_BROWSER_DNR_RESPONSE_OFFSET}`
      );
    }
  }

  const reqHeaders = [];
  if (referer) reqHeaders.push({ header: 'referer', operation: 'set', value: referer });
  if (origin) reqHeaders.push({ header: 'origin', operation: 'set', value: origin });
  if (userAgent) reqHeaders.push({ header: 'user-agent', operation: 'set', value: userAgent });

  // Codex review #11: scope conditions to the extension's own initiator
  // (chrome.runtime.id) so unrelated tabs that fetch a matching URL
  // during the download window do NOT benefit from the CORS rewrite.
  // Without this, a malicious page that learns or guesses a CDN segment
  // URL while a job is active could read responses cross-origin.
  function makeCondition(regexFilter) {
    const cond = {
      regexFilter,
      resourceTypes: ['xmlhttprequest', 'media', 'other'],
    };
    if (initiatorDomain) {
      cond.initiatorDomains = [initiatorDomain];
    }
    return cond;
  }

  const rules = [];
  let id = idBase;
  for (const regexFilter of filters) {
    // Request header-spoof: ONLY for trusted origin groups.
    // Codex adversarial-review: foreign hosts no longer receive the
    // captured Referer/Origin/User-Agent — Referers for player pages
    // can carry signed URLs / session tokens. Must stay in lockstep
    // with dnrRules.js::buildHeaderRules.
    if (reqHeaders.length > 0) {
      rules.push({
        id,
        priority: 1,
        action: { type: 'modifyHeaders', requestHeaders: reqHeaders },
        condition: makeCondition(regexFilter),
      });
    }
    // Response CORS-relax: ONLY for trusted origin groups. Codex #10.
    rules.push({
      id: id + _BROWSER_DNR_RESPONSE_OFFSET,
      priority: 1,
      action: {
        type: 'modifyHeaders',
        responseHeaders: [
          { header: 'access-control-allow-origin', operation: 'set', value: '*' },
          { header: 'access-control-allow-credentials', operation: 'remove' },
        ],
      },
      condition: makeCondition(regexFilter),
    });
    id += 1;
  }
  return rules;
}


// Codex review #10: per-segment trust check matching segmentDownloader's
// isTrustedForCredentials. Same protection boundary applied at
// DNR-rule-building time so foreign origins don't receive CORS-relax
// response rewrites.
//
// Codex adversarial-review: the predicate must not trust upward —
// rejecting the case where a manifest on a subdomain (e.g.
// attacker.example.com) claims trust over its parent host
// (example.com). See segmentDownloader.isTrustedForCredentials for
// the threat-model rationale. Both predicates MUST stay in lockstep,
// otherwise DNR rules diverge from fetch-time behavior.
function _wv2nasIsTrustedDnrUrl(segmentUrl, trustedBase) {
  if (!trustedBase) return false;
  let segHost, baseHost, segOrigin, baseOrigin;
  try {
    const seg = new URL(segmentUrl);
    const base = new URL(trustedBase);
    if (seg.protocol !== 'https:' && seg.protocol !== 'http:') return false;
    segHost = seg.hostname.toLowerCase();
    baseHost = base.hostname.toLowerCase();
    segOrigin = seg.origin;
    baseOrigin = base.origin;
  } catch (_) {
    return false;
  }
  if (segOrigin === baseOrigin) return true;
  if (segHost.endsWith('.' + baseHost)) return true;
  return false;
}

// Collect every URL DNR rules need to cover for a job: init segments,
// media segments, AND AES-128 key URIs (Codex review #3 — without keys
// in the DNR scope, segment fetches succeed but the subsequent key
// fetch hits the protected origin without our Referer/Origin/UA spoof
// or CORS relaxation, and decryption fails). Use a Set to dedup —
// keys typically rotate per-segment with the SAME URI, but this also
// guards against duplicates from rare per-segment-key streams.
function _wv2nasPlanSegmentUrls(plan) {
  const out = new Set();
  if (plan.init_segment_url) out.add(plan.init_segment_url);
  for (const trackName of Object.keys(plan.tracks || {})) {
    const t = plan.tracks[trackName];
    if (t.init_segment_url) out.add(t.init_segment_url);
    for (const s of t.segments || []) {
      if (s.url) out.add(s.url);
      // AES key URI (HLS EXT-X-KEY URI=...) — extension's offscreen
      // segmentDownloader fetches this in browser context too. Some
      // streams put keys on a different origin (CDN auth gateway)
      // making this critical.
      if (s.key && s.key.uri) out.add(s.key.uri);
    }
  }
  return Array.from(out);
}

let _browserOffscreenCreating = null;
// Codex adversarial-review (high): the offscreen document's
// chrome.runtime.onMessage listener registers asynchronously when the
// document loads — `chrome.offscreen.createDocument` resolves as soon
// as the document is created, NOT once its listener is ready. Without
// a handshake, the very next chrome.runtime.sendMessage with
// target:'offscreen' can race the listener registration and fail with
// "Could not establish connection. Receiving end does not exist." On
// cold offscreen creation the SW would then bail out of the job after
// /api/jobs/init has already allocated server staging + DNR rules.
// `_browserOffscreenReady` is set whenever a creation is in flight and
// resolves when offscreen.js's OFFSCREEN_READY ping reaches the SW.
// Existing-doc fast path resolves immediately (the listener was
// registered when the doc loaded earlier).
let _browserOffscreenReady = null;
let _browserOffscreenReadyResolve = null;
const _BROWSER_OFFSCREEN_READY_TIMEOUT_MS = 10_000;

function _markOffscreenReady() {
  if (_browserOffscreenReadyResolve) {
    _browserOffscreenReadyResolve();
    _browserOffscreenReadyResolve = null;
  } else {
    // No creation in flight (existing doc just sent a duplicate ping,
    // or a stale ack arrived after closeDocument). Pre-resolve so the
    // next _ensureOffscreenDocument call returns immediately.
    _browserOffscreenReady = Promise.resolve();
  }
}

async function _ensureOffscreenDocument() {
  const url = chrome.runtime.getURL('offscreen.html');
  if (chrome.offscreen.hasDocument) {
    const exists = await chrome.offscreen.hasDocument();
    if (exists) {
      // Document already alive (e.g. concurrent job kept it open, or
      // SW restarted with offscreen still up). Its listener has been
      // registered since the original load — safe to send immediately.
      if (!_browserOffscreenReady) _browserOffscreenReady = Promise.resolve();
      return _browserOffscreenReady;
    }
  }
  // Coalesce concurrent calls so we don't race two creations.
  if (_browserOffscreenCreating) {
    await _browserOffscreenCreating;
    return _browserOffscreenReady || Promise.resolve();
  }
  _browserOffscreenReady = new Promise((resolve, reject) => {
    _browserOffscreenReadyResolve = resolve;
    setTimeout(() => {
      if (_browserOffscreenReadyResolve) {
        _browserOffscreenReadyResolve = null;
        reject(new Error(
          `offscreen document did not signal ready within ` +
          `${_BROWSER_OFFSCREEN_READY_TIMEOUT_MS}ms`
        ));
      }
    }, _BROWSER_OFFSCREEN_READY_TIMEOUT_MS);
  });
  _browserOffscreenCreating = chrome.offscreen.createDocument({
    url,
    reasons: ['BLOBS', 'WORKERS'],
    justification: 'Run long-form HLS/DASH segment downloader for browser-side jobs',
  }).finally(() => { _browserOffscreenCreating = null; });
  await _browserOffscreenCreating;
  return _browserOffscreenReady;
}

async function _closeOffscreenDocument() {
  try {
    if (chrome.offscreen.hasDocument) {
      const exists = await chrome.offscreen.hasDocument();
      if (!exists) return;
    }
    await chrome.offscreen.closeDocument();
  } catch (e) {
    console.warn('[wv2nas] closeOffscreenDocument failed:', e);
  } finally {
    // Reset the readiness gate so the NEXT createDocument waits for
    // a fresh OFFSCREEN_READY ack (the new document re-registers its
    // listener from scratch).
    _browserOffscreenReady = null;
    _browserOffscreenReadyResolve = null;
  }
}

// Pick the highest-BANDWIDTH variant URL from an HLS master playlist.
// Mirrors `_plan_hls_from_text`'s server-side variant pick so the extension
// and server agree on which media playlist gets used. Returns null when no
// parsable variants are found.
function _wv2nasPickBestHlsVariant(masterText, masterUrl) {
  const lines = masterText.split(/\r?\n/);
  let bestBw = -1;
  let bestUrl = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith('#EXT-X-STREAM-INF:')) {
      const bwMatch = line.match(/BANDWIDTH=(\d+)/);
      const bw = bwMatch ? parseInt(bwMatch[1], 10) : 0;
      // The very next non-comment line is the variant URI per RFC 8216.
      let j = i + 1;
      while (j < lines.length && (lines[j].trim() === '' || lines[j].trim().startsWith('#'))) j++;
      if (j < lines.length) {
        try {
          const u = new URL(lines[j].trim(), masterUrl).href;
          if (bw > bestBw) {
            bestBw = bw;
            bestUrl = u;
          }
        } catch (_) {}
      }
    }
  }
  return bestUrl;
}

// Fetch the manifest in browser context (with credentials, so cookies +
// session ride along) and resolve HLS master → variant ourselves so the
// server never has to retry the fetch from its IP. Returns
// {manifest_text, base_url} on success, null when browser fetch fails
// (caller falls back to NAS-side URL fetch). Codex review #1: this is
// the critical path — without it, /api/jobs/init still relies on the
// NAS being able to reach the manifest, defeating the whole browser-side
// design for IP-bound / short-TTL / session-bound playlists.
// Codex review: filter captured request headers down to a set safe for
// `fetch(url, { headers })`. Forbidden headers (Cookie, Origin, Referer,
// User-Agent, Host, etc.) are either silently dropped by fetch or are
// already handled by DNR / credentials: 'include'. Authorization,
// X-* auth tokens, and similar custom headers DO ride along — without
// this filtering pass the manifest fetch would 401/403 on protected
// streams that need a header token, even though the legacy NAS-direct
// path passes the same headers via the requestBody.
function _wv2nasFilterFetchHeaders(rawHeaders) {
  const out = {};
  if (!rawHeaders || typeof rawHeaders !== 'object') return out;
  const skip = new Set([
    'accept-charset',
    'accept-encoding',// fetch sets this automatically
    'access-control-request-headers',
    'access-control-request-method',
    'connection',     // forbidden
    'content-length', // forbidden
    'cookie',         // forbidden — credentials: 'include' carries cookies
    'date',
    'dnt',
    'expect',
    'host',           // forbidden
    'keep-alive',
    'origin',         // forbidden — set via DNR
    'permissions-policy',
    'range',          // set explicitly only for byte-range media fetches
    'referer',        // forbidden — set via DNR
    'te',
    'trailer',
    'transfer-encoding',
    'upgrade',
    'user-agent',     // forbidden in some browsers — set via DNR
    'via',
    'x-http-method',
    'x-http-method-override',
    'x-method-override',
    'sec-ch-ua',      // forbidden (UA client hints)
    'sec-ch-ua-mobile',
    'sec-ch-ua-platform',
  ]);
  for (const [k, v] of Object.entries(rawHeaders)) {
    const lower = k.toLowerCase();
    if (!skip.has(lower) && !lower.startsWith('sec-')
        && !lower.startsWith('proxy-') && v != null) {
      out[k] = String(v);
    }
  }
  return out;
}


// Codex review (P2): bound manifest body reads. Server-side cap on
// `manifest_text` is 10 MB (api/main.py JobInitRequest); align the
// client cap so an oversize misdetected URL or a hostile manifest
// can't fill SW memory before the server-side rejection. Streaming
// read with mid-stream abort, mirroring readBodyWithCap in
// segmentDownloader.js (which exists in the offscreen document — we
// can't share helpers across SW and offscreen).
const _WV2NAS_MAX_MANIFEST_BYTES = 10 * 1024 * 1024;


async function _wv2nasReadManifestText(response, maxBytes, label) {
  // Upfront content-length gate — saves the wire bytes when the
  // server is honest about its size.
  const cl = response.headers && typeof response.headers.get === 'function'
    ? response.headers.get('content-length')
    : null;
  if (cl) {
    const n = parseInt(cl, 10);
    if (Number.isFinite(n) && n > maxBytes) {
      try {
        if (response.body && typeof response.body.cancel === 'function') {
          response.body.cancel();
        }
      } catch (_) { /* best-effort */ }
      throw new Error(
        `${label}: Content-Length ${n} exceeds cap ${maxBytes}`,
      );
    }
  }

  // Streaming path — production Chrome always exposes resp.body.
  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let total = 0;
    let text = '';
    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!value || value.byteLength === 0) continue;
        total += value.byteLength;
        if (total > maxBytes) {
          try { reader.cancel(); } catch (_) { /* best-effort */ }
          throw new Error(
            `${label}: response exceeded cap ${maxBytes} bytes mid-stream`,
          );
        }
        text += decoder.decode(value, { stream: true });
      }
      // Flush any trailing pending bytes from the decoder.
      text += decoder.decode();
    } catch (err) {
      try { reader.cancel(); } catch (_) { /* best-effort */ }
      throw err;
    }
    return text;
  }

  // Fallback for test mocks without a streaming body. Still post-
  // checks size to bound memory at one full response.
  const fallback = await response.text();
  if (fallback.length > maxBytes) {
    throw new Error(
      `${label}: response size ${fallback.length} exceeds cap ${maxBytes}`,
    );
  }
  return fallback;
}


function _wv2nasClassifyIpv4Literal(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const octets = [1, 2, 3, 4].map((i) => parseInt(m[i], 10));
  if (octets.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) {
    return { safe: false, reason: 'invalid IPv4 literal' };
  }
  const [a, b, c] = octets;
  if (a === 0) return { safe: false, reason: 'unspecified (0/8)' };
  if (a === 10) return { safe: false, reason: 'RFC 1918 (10/8)' };
  if (a === 127) return { safe: false, reason: 'IPv4 loopback (127/8)' };
  if (a === 169 && b === 254) {
    return { safe: false, reason: 'link-local (169.254/16) - incl. AWS metadata' };
  }
  if (a === 172 && b >= 16 && b <= 31) {
    return { safe: false, reason: 'RFC 1918 (172.16/12)' };
  }
  if (a === 192 && b === 168) {
    return { safe: false, reason: 'RFC 1918 (192.168/16)' };
  }
  if (a === 100 && b >= 64 && b <= 127) {
    return { safe: false, reason: 'shared CGN (100.64/10)' };
  }
  if (a === 192 && b === 0 && c === 0) {
    return { safe: false, reason: 'IETF special-use (192.0.0/24)' };
  }
  if (a === 192 && b === 0 && c === 2) return { safe: false, reason: 'TEST-NET-1' };
  if (a === 198 && (b === 18 || b === 19)) {
    return { safe: false, reason: 'benchmarking (198.18/15)' };
  }
  if (a === 198 && b === 51 && c === 100) return { safe: false, reason: 'TEST-NET-2' };
  if (a === 203 && b === 0 && c === 113) return { safe: false, reason: 'TEST-NET-3' };
  if (a >= 224) return { safe: false, reason: 'multicast/reserved (>=224)' };
  return { safe: true };
}


function _wv2nasParseIpv4Octets(raw) {
  const classified = _wv2nasClassifyIpv4Literal(raw);
  if (!classified) return null;
  if (!classified.safe && classified.reason === 'invalid IPv4 literal') return null;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(raw);
  if (!m) return null;
  return [1, 2, 3, 4].map((i) => parseInt(m[i], 10));
}


function _wv2nasExpandIpv6Literal(inner) {
  if (!inner || inner.includes('%')) return null;
  let text = inner.toLowerCase();
  const lastColon = text.lastIndexOf(':');
  const maybeIpv4Tail = lastColon >= 0 ? text.slice(lastColon + 1) : '';
  if (maybeIpv4Tail.includes('.')) {
    const octets = _wv2nasParseIpv4Octets(maybeIpv4Tail);
    if (!octets) return null;
    const high = ((octets[0] << 8) | octets[1]).toString(16);
    const low = ((octets[2] << 8) | octets[3]).toString(16);
    text = `${text.slice(0, lastColon)}:${high}:${low}`;
  }

  const compressed = text.includes('::');
  if ((text.match(/::/g) || []).length > 1) return null;
  const sides = text.split('::');
  const left = sides[0] ? sides[0].split(':') : [];
  const right = compressed && sides[1] ? sides[1].split(':') : [];
  if (left.some((g) => !g) || right.some((g) => !g)) return null;

  let groups;
  if (compressed) {
    const fill = 8 - left.length - right.length;
    if (fill < 1) return null;
    groups = [...left, ...Array(fill).fill('0'), ...right];
  } else {
    groups = left;
    if (groups.length !== 8) return null;
  }

  if (groups.length !== 8) return null;
  const parsed = [];
  for (const group of groups) {
    if (!/^[0-9a-f]{1,4}$/.test(group)) return null;
    parsed.push(parseInt(group, 16));
  }
  return parsed;
}


function _wv2nasClassifyIpv6Literal(inner) {
  const groups = _wv2nasExpandIpv6Literal(inner);
  if (!groups) return { safe: false, reason: 'invalid IPv6 literal' };

  // IPv4-mapped IPv6 (::ffff:a.b.c.d / ::ffff:hhhh:hhhh) should follow
  // the exact IPv4 literal policy, not a separate IPv6 shortcut.
  if (groups.slice(0, 5).every((g) => g === 0) && groups[5] === 0xffff) {
    const ip4 = [
      (groups[6] >> 8) & 0xff, groups[6] & 0xff,
      (groups[7] >> 8) & 0xff, groups[7] & 0xff,
    ].join('.');
    return _wv2nasClassifyIpv4Literal(ip4) || {
      safe: false,
      reason: 'invalid IPv4-mapped IPv6 literal',
    };
  }

  if (groups.every((g) => g === 0)) {
    return { safe: false, reason: 'IPv6 unspecified' };
  }
  if (groups.slice(0, 7).every((g) => g === 0) && groups[7] === 1) {
    return { safe: false, reason: 'IPv6 loopback' };
  }
  if ((groups[0] & 0xfe00) === 0xfc00) {
    return { safe: false, reason: 'IPv6 unique-local' };
  }
  if ((groups[0] & 0xffc0) === 0xfe80) {
    return { safe: false, reason: 'IPv6 link-local' };
  }
  if ((groups[0] & 0xffc0) === 0xfec0) {
    return { safe: false, reason: 'IPv6 site-local' };
  }
  if ((groups[0] & 0xff00) === 0xff00) {
    return { safe: false, reason: 'IPv6 multicast' };
  }
  if (groups[0] === 0x2001 && groups[1] === 0x0db8) {
    return { safe: false, reason: 'IPv6 documentation range (2001:db8/32)' };
  }
  if (groups[0] === 0x2001 && groups[1] <= 0x01ff) {
    return { safe: false, reason: 'IPv6 IETF special-use (2001::/23)' };
  }
  if (groups[0] === 0x3ffe) {
    return { safe: false, reason: 'IPv6 6bone documentation/deprecated range' };
  }
  if ((groups[0] & 0xe000) !== 0x2000) {
    return { safe: false, reason: 'IPv6 literal is not global unicast' };
  }
  return { safe: true };
}


// User-configured cross-site CDN trust list. Strict suffix match on
// the dotted hostname — a configured `phncdn.com` matches `phncdn.com`
// and `kv-h.phncdn.com` but NOT `evilphncdn.com` (substring matches
// would expose typosquats). Empty / missing list → no host matches,
// behavior unchanged from the strict same-site policy.
function _wv2nasMatchesTrustedCdnSuffix(host, suffixes) {
  if (!Array.isArray(suffixes) || suffixes.length === 0) return false;
  if (typeof host !== 'string' || !host) return false;
  const h = host.toLowerCase();
  for (const raw of suffixes) {
    if (typeof raw !== 'string') continue;
    // Trim whitespace and any leading dots so users writing
    // ".phncdn.com" still get the intuitive match.
    const s = raw.trim().toLowerCase().replace(/^\.+/, '');
    if (!s) continue;
    if (h === s || h.endsWith('.' + s)) return true;
  }
  return false;
}


// Codex adversarial-review (high): pre-validate any URL we're about
// to fetch in the extension context with credentials. The server's
// _enforce_plan_url_safety only runs AFTER `manifest_text` has been
// posted from the browser, so a forged or misdetected manifest URL
// could otherwise drive a credentialed fetch to localhost / private
// IPs / metadata services on the user's machine and forward the
// response to the NAS API. Mirror the server policy here:
//   - HTTPS only (DNS rebinding mitigation rides on cert-name mismatch)
//   - reject localhost / IP literals in private / loopback / link-local
//     / shared-CGN / TEST-NET / reserved ranges
//   - DNS-resolvable hostnames are accepted (we can't do DNS lookups
//     client-side; the HTTPS gate is the safety net)
function _wv2nasIsManifestUrlSafeForBrowser(url, pageUrl, trustedCdnSuffixes) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_) {
    return { safe: false, reason: 'malformed URL' };
  }
  if (parsed.protocol !== 'https:') {
    return {
      safe: false,
      reason: `plain ${parsed.protocol} rejected (HTTPS-only — DNS rebinding mitigation)`,
    };
  }
  const host = parsed.hostname.toLowerCase();
  if (!host) return { safe: false, reason: 'no host' };
  if (host === 'localhost' || host === '0.0.0.0'
      || host === 'broadcasthost' || host.endsWith('.localhost')) {
    return { safe: false, reason: `local hostname ${host}` };
  }
  // IPv6 literal — URL.hostname returns it WITH the brackets.
  if (host.startsWith('[') && host.endsWith(']')) {
    return _wv2nasClassifyIpv6Literal(host.slice(1, -1));
  }
  // IPv4 literal.
  const ipv4Safety = _wv2nasClassifyIpv4Literal(host);
  if (ipv4Safety) return ipv4Safety;
  // DNS name — needs a trust anchor. The HTTPS-only requirement
  // catches DNS-rebinding to a public-cert-bound private IP, but
  // CANNOT defeat split-horizon DNS where a public hostname has an
  // internal-CA cert that the corporate machine trusts (e.g.
  // `https://internal.corp.example/`). Without DNS resolution we
  // can't distinguish that from a real public host.
  //
  // Codex adversarial-review: require same-site relationship to
  // the page that surfaced this URL. Legitimate cookie-bound
  // streams (Vimeo private videos, paid streaming sites) are
  // typically same-site by design — that's where the cookies live.
  //
  // Cross-site CDN streams (page on brand domain, manifest/segments
  // on a separate CDN eTLD+1) with IP-bound HMAC tokens that NAS-side
  // can't reach are the exception — they need browser-side AND require
  // the user to explicitly pre-declare the CDN host suffix as trusted
  // via `trustedCdnSuffixes`. The user shoulders the split-horizon-DNS
  // / internal-CA risk for those specific suffixes. Empty list →
  // strict same-site policy (default).
  //
  // When pageUrl is not provided (back-compat for unit tests of
  // this helper), we fall back to the prior behavior — accept any
  // public-resolving DNS name. Production runBrowserSideJob always
  // passes pageUrl through dnrContext.
  if (pageUrl) {
    if (!_wv2nasIsTrustedDnrUrl(url, pageUrl)
        && !_wv2nasMatchesTrustedCdnSuffix(host, trustedCdnSuffixes)) {
      return {
        safe: false,
        reason: (
          `host ${host} is not same-site with page (${pageUrl}); `
          + `refusing browser fetch — split-horizon DNS / internal-CA `
          + `hosts cannot be distinguished client-side from public hosts. `
          + `If this is a known cross-site CDN, add its host suffix to `
          + `'Trusted cross-site CDN suffixes' in extension options.`
        ),
      };
    }
  }
  return { safe: true };
}


function _wv2nasHttpsEquivalent(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    parsed.protocol = 'https:';
    return parsed.href;
  } catch (_) {
    return null;
  }
}


function _wv2nasCanUseNasDirectForBrowserUnsafeUrl(url, pageUrl) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch (_) {
    return false;
  }
  if (parsed.protocol !== 'http:') return false;

  const httpsUrl = _wv2nasHttpsEquivalent(url);
  if (!httpsUrl) return false;
  const httpsPageUrl = pageUrl ? _wv2nasHttpsEquivalent(pageUrl) : null;
  const hostSafety = _wv2nasIsManifestUrlSafeForBrowser(
    httpsUrl,
    httpsPageUrl,
  );
  return !!hostSafety.safe;
}


async function _wv2nasFetchManifestInBrowser(url, dnrContext = null) {
  // Codex adversarial-review: gate the credentialed fetch on the same
  // URL safety policy the server runs on plan URLs. Without this, a
  // forged manifest URL pointing at intranet/metadata hosts gets
  // fetched with the user's cookies before any server check runs.
  // Pass dnrContext.pageUrl so the gate can also reject DNS hostnames
  // that aren't same-site with the page (split-horizon mitigation),
  // plus dnrContext.trustedCdnSuffixes for user-allowlisted cross-site
  // CDN bypass (option E — opt-in per host suffix).
  const safety = _wv2nasIsManifestUrlSafeForBrowser(
    url,
    dnrContext && dnrContext.pageUrl,
    dnrContext && dnrContext.trustedCdnSuffixes,
  );
  if (!safety.safe) {
    console.warn(
      `[wv2nas] Refusing browser-side manifest fetch for ${url}: ${safety.reason}`,
    );
    return { safetyRejected: true, reason: safety.reason, url };
  }
  // Codex review: a manifest gated on Authorization or X-* auth tokens
  // would 401/403 here without the captured headers. DNR can only
  // spoof Referer/Origin/UA; everything else has to ride the fetch
  // call directly. Filter forbidden ones so fetch doesn't reject.
  const fetchHeaders = dnrContext
    ? _wv2nasFilterFetchHeaders(dnrContext.headers)
    : {};

  let response;
  try {
    response = await fetch(url, {
      credentials: 'include',
      headers: fetchHeaders,
      // Codex review (P1): refuse to follow redirects on browser-side
      // manifest fetches. The safety gate only validates the
      // ORIGINAL URL — a 30x to a different origin or to an
      // intranet/metadata host would otherwise be followed
      // automatically with `credentials: 'include'`, leaking cookies
      // to the redirect target and pulling its response body back
      // for upload to /api/jobs/init as `manifest_text`.
      // `redirect: 'error'` causes fetch to throw TypeError on any
      // 30x; caller catches and falls back to NAS-side planning
      // where _safe_fetch performs per-hop SSRF revalidation.
      redirect: 'error',
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
  } catch (e) {
    console.warn('[wv2nas] In-browser manifest fetch failed; will fall back to NAS-fetch:', e);
    return null;
  }

  let text;
  try {
    text = await _wv2nasReadManifestText(
      response, _WV2NAS_MAX_MANIFEST_BYTES, `manifest ${url}`,
    );
  } catch (e) {
    console.warn('[wv2nas] manifest body read failed:', e);
    return null;
  }

  // HLS master playlist: resolve to the chosen variant + fetch its body
  // too, so the server only sees a media playlist (no further fetches
  // needed on its side).
  if (text.includes('#EXT-X-STREAM-INF')) {
    const variantUrl = _wv2nasPickBestHlsVariant(text, url);
    if (variantUrl && variantUrl !== url) {
      // Codex adversarial-review (high): the variant URL came from
      // server-controlled master playlist text — apply the same
      // pre-fetch safety gate as the master to refuse credentialed
      // requests against intranet/metadata hosts. The variant URL
      // is already exercised by the server-side
      // _enforce_plan_url_safety after init, but only if init RUNS;
      // we never want a malicious master to drive a private-IP
      // browser fetch BEFORE the server sees the plan. The pageUrl
      // anchor is the master URL itself (we already accepted it
      // above) — variant must stay within master's trust boundary.
      const variantSafety = _wv2nasIsManifestUrlSafeForBrowser(
        variantUrl, url,
      );
      if (!variantSafety.safe) {
        console.warn(
          `[wv2nas] Refusing variant fetch for ${variantUrl}: ${variantSafety.reason}`,
        );
        return {
          safetyRejected: true,
          reason: variantSafety.reason,
          url: variantUrl,
        };
      }
      // Codex adversarial-review: scope cookies AND captured auth
      // headers to the master URL's trust boundary. A malicious or
      // misdetected master could point its highest-bandwidth variant
      // at attacker-controlled `evil.com`; without scoping, the
      // captured Authorization / X-* tokens (and session cookies)
      // would leak to that host. `_wv2nasIsTrustedDnrUrl` matches
      // the offscreen-side `isTrustedForCredentials` semantics —
      // same-origin or deeper-subdomain only.
      const variantTrusted = _wv2nasIsTrustedDnrUrl(variantUrl, url);
      // Codex review: the variant URL may sit on a different directory
      // or origin from the master, so the phase-1 DNR rule (built
      // from the master URL only) won't match the variant fetch. Sites
      // that gate variant playlists on Referer/Origin/UA would 403
      // here. Install a complementary header-spoof rule for the
      // variant URL before fetching. New rule IDs are appended to
      // `dnrContext.ruleIds` so the caller's phase-2 swap removes
      // them atomically alongside the phase-1 rules.
      //
      // Codex adversarial-review (high): pass the trust flag through
      // to the rule builder. CORS-relax must NOT fire for an
      // untrusted variant — otherwise the extension could read the
      // variant body cross-origin and exfiltrate it as manifest_text.
      if (dnrContext) {
        try {
          await _wv2nasInstallVariantDnrRule(variantUrl, dnrContext, variantTrusted);
        } catch (e) {
          console.warn('[wv2nas] Variant DNR install failed:', e);
        }
      }
      try {
        const variantResp = await fetch(variantUrl, {
          credentials: variantTrusted ? 'include' : 'omit',
          headers: variantTrusted ? fetchHeaders : {},
          // Codex review (P1): same redirect gate as the master
          // fetch. A trusted variant URL that 30x's to a foreign /
          // private host would otherwise leak credentials and pull
          // back arbitrary content for upload as manifest_text.
          redirect: 'error',
        });
        if (!variantResp.ok) {
          throw new Error(`HTTP ${variantResp.status}`);
        }
        // Codex review (P2): same size-capped streaming read as the
        // master — variant URL might be on a different origin and
        // could also be misdetected/hostile.
        const variantText = await _wv2nasReadManifestText(
          variantResp, _WV2NAS_MAX_MANIFEST_BYTES, `variant ${variantUrl}`,
        );
        return { manifest_text: variantText, base_url: variantUrl };
      } catch (e) {
        console.warn(
          '[wv2nas] Variant fetch failed; submitting master text + URL:', e
        );
        // Fall through with master text — server's plan_from_text will
        // try to fetch the variant itself; that may also fail but at
        // least we surfaced the master.
      }
    }
  }

  return { manifest_text: text, base_url: url };
}


// Codex review: extend phase-1 DNR coverage to a variant URL whose
// origin/path falls outside the master URL's filter. Reuses the same
// per-job slot (id range [idBase, idBase+100)) — phase-1 used
// idBase+0 (request) and idBase+50 (response). We use idBase+1 and
// idBase+51 for the variant pair, leaving 48 free slots in the same
// per-job range. Phase-2's removeRuleIds covers the full ruleIds
// list, so the variant rules get cleaned up atomically with phase-1
// when full segment coverage replaces them.
async function _wv2nasInstallVariantDnrRule(variantUrl, dnrContext, trusted = false) {
  const { referer, origin, userAgent, idBase, ruleIds } = dnrContext;
  const variantIdBase = idBase + 1;
  const variantRules = _wv2nasBuildDnrRules({
    segmentUrls: [variantUrl],
    // Codex adversarial-review: explicit trusted set. When the
    // variant isn't same-site with master, neither header-spoof
    // nor CORS-relax should emit — header leaks Referer signed
    // tokens, CORS-relax lets the extension read response body
    // cross-origin (exfiltration channel).
    trustedSegmentUrls: trusted ? [variantUrl] : [],
    referer,
    origin,
    userAgent,
    idBase: variantIdBase,
    initiatorDomain: chrome.runtime.id,
  });
  if (variantRules.length === 0) return;
  const variantRuleIds = variantRules.map((r) => r.id);
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: variantRuleIds,
    addRules: variantRules,
  });
  // Caller's `ruleIds` is the same array that the phase-2 swap will
  // pass as removeRuleIds. Mutate in place so our IDs are part of
  // that atomic remove-then-add.
  ruleIds.push(...variantRuleIds);
}

// ---------------------------------------------------------------------------
// Codex review #12: browser-job persistence for SW-restart resilience.
//
// MV3 service workers are evicted aggressively (after ~30s of inactivity)
// and can be cold-started by the browser at any time. Without persistence:
//   - Long downloads (multi-minute HLS/DASH) outlive the SW
//   - The runBrowserSideJob in-memory promise dies with the SW
//   - DNR session rules survive (browser-session-scoped) but nothing in
//     this extension knows about them anymore
//   - Server-side staging stays allocated; only the worker's startup
//     stale reaper eventually cleans it up (6h)
//
// Fix: persist {jobId, ruleIds, dnrSlot, startedAt, ...} to
// chrome.storage.local at every meaningful transition. On SW boot,
// `_wv2nasRecoverStaleBrowserJobs` walks persisted entries; any older
// than the watchdog threshold is treated as orphaned and cleaned up
// (DNR remove + abort POST + slot release + entry delete).
// ---------------------------------------------------------------------------

const _BROWSER_JOB_PERSIST_KEY = 'wv2nasBrowserJobs';
// Codex review #16: jobs are only reaped if their last liveness signal
// is older than this. With heartbeats from offscreen at 10s cadence,
// a 5-minute miss is a strong "offscreen is dead" signal. The previous
// design used `startedAt` age alone (1h) as the liveness signal — which
// destroyed legitimate long downloads (slow 4-hour movies) where the
// SW restarted past 1h while offscreen was still actively uploading.
const _BROWSER_JOB_HEARTBEAT_TIMEOUT_MS = 5 * 60 * 1000;
// Fallback for jobs that NEVER heartbeated (legacy persisted entries
// from before this fix, or jobs whose offscreen died before sending
// the first heartbeat). 1h is long enough that a normal start-of-job
// stall doesn't trip it.
const _BROWSER_JOB_STALE_MS = 60 * 60 * 1000;

async function _wv2nasReadPersistedBrowserJobs() {
  try {
    const cur = await chrome.storage.local.get(_BROWSER_JOB_PERSIST_KEY);
    return cur[_BROWSER_JOB_PERSIST_KEY] || {};
  } catch (e) {
    console.warn('[wv2nas] read persisted browser jobs failed:', e);
    return {};
  }
}

async function _wv2nasWritePersistedBrowserJobs(jobs) {
  try {
    await chrome.storage.local.set({ [_BROWSER_JOB_PERSIST_KEY]: jobs });
  } catch (e) {
    console.warn('[wv2nas] write persisted browser jobs failed:', e);
  }
}

// Codex adversarial-review: persistence is a whole-object R-M-W on a
// single chrome.storage.local key. Two concurrent jobs (or one job's
// heartbeat racing another job's state transition) can each read the
// same snapshot, then each write back a different object — losing the
// other's entry. If the SW restarts after a lost entry, recovery
// can't remove the dropped job's DNR rules or abort its server-side
// staging, which defeats the whole restart-safety mechanism.
//
// Fix: serialize all MUTATIONS through a chained promise. Reads stay
// unsynchronized — they observe whatever state is committed at the
// time, which is fine because the only consumer (recovery sweep)
// resolves any ambiguity by deleting per-job through the serialized
// unpersist path. What we cannot allow is two writers concurrently
// overwriting the whole map.
let _wv2nasPersistMutex = Promise.resolve();

function _wv2nasWithPersistLock(fn) {
  // Chain after any in-flight work; failures don't poison subsequent
  // callers (each branch falls through to fn()).
  const next = _wv2nasPersistMutex.then(fn, fn);
  _wv2nasPersistMutex = next.then(() => undefined, () => undefined);
  return next;
}

async function _wv2nasPersistBrowserJob(jobId, data) {
  return _wv2nasWithPersistLock(async () => {
    const jobs = await _wv2nasReadPersistedBrowserJobs();
    jobs[jobId] = { ...(jobs[jobId] || {}), ...data, jobId };
    await _wv2nasWritePersistedBrowserJobs(jobs);
  });
}

async function _wv2nasPersistBrowserJobHeartbeat(jobId, ts) {
  return _wv2nasWithPersistLock(async () => {
    const jobs = await _wv2nasReadPersistedBrowserJobs();
    if (!jobs[jobId]) return false;
    jobs[jobId] = {
      ...jobs[jobId],
      jobId,
      lastHeartbeat: Number(ts) || Date.now(),
    };
    await _wv2nasWritePersistedBrowserJobs(jobs);
    return true;
  });
}

async function _wv2nasUnpersistBrowserJob(jobId) {
  return _wv2nasWithPersistLock(async () => {
    const jobs = await _wv2nasReadPersistedBrowserJobs();
    if (jobs[jobId]) {
      delete jobs[jobId];
      await _wv2nasWritePersistedBrowserJobs(jobs);
    }
  });
}

async function _wv2nasPersistAbortRetry(jobId, reason, { dnrCleaned = false } = {}) {
  const patch = {
    abortPending: true,
    abortReason: (reason || 'Browser-side job failed before finalize').slice(0, 500),
    abortRequestedAt: Date.now(),
    // The offscreen job is done; do not let a recent heartbeat make boot
    // recovery preserve this entry as alive instead of retrying abort.
    lastHeartbeat: 0,
  };
  if (dnrCleaned) {
    // DNR rules were already removed and the slot may be reused. Keeping
    // old rule ids would let a later abort retry remove a new job's rules.
    patch.ruleIds = [];
    patch.dnrSlot = null;
  }
  await _wv2nasPersistBrowserJob(jobId, patch);
}

// Codex review #15: durable completion handler. Registered at SW
// boot (via the top-level chrome.runtime.onMessage at the bottom of
// this file), this handles BROWSER_JOB_DONE / BROWSER_JOB_FAILED
// messages for ANY persisted job — including ones whose
// per-runBrowserSideJob in-memory listener died with a previous SW
// instance. Without this, an offscreen completion that arrives after
// a SW restart was silently dropped, leaving DNR rules + slot
// reservations + persisted entries to leak indefinitely (until the
// 1h boot watchdog noticed).
//
// Coordination with the per-job in-memory listener: in the SW-alive
// case both fire. All cleanup operations are idempotent (DNR remove
// of already-removed IDs is a no-op; Set.delete of missing slot is
// a no-op; chrome.storage.local entry already missing is a no-op).
// Worst case: cleanup runs twice. Cheap.
async function _wv2nasHandleDurableCompletion(msg) {
  const jobId = msg && msg.payload && msg.payload.jobId;
  if (!jobId) return;
  // Codex review #19a: block until the SW-boot recovery finishes
  // populating `_wv2nasActiveBrowserJobs` so the offscreen-close
  // decision below sees ALL alive jobs, not just the one we're
  // completing now. Without this, a second concurrent job that's
  // still mid-download can be silently torn down because its
  // recovery hasn't run yet.
  await _wv2nasInitRecovery();
  const jobs = await _wv2nasReadPersistedBrowserJobs();
  const data = jobs[jobId];
  if (!data) {
    // In-memory listener already cleaned up (normal path) — nothing
    // to do. Or the job_id is bogus; either way safe to ignore.
    return;
  }

  let dnrCleanupOk = true;
  if (Array.isArray(data.ruleIds) && data.ruleIds.length > 0) {
    try {
      await chrome.declarativeNetRequest.updateSessionRules({
        removeRuleIds: data.ruleIds,
      });
    } catch (e) {
      dnrCleanupOk = false;
      console.warn(`[wv2nas] durable DNR cleanup for ${jobId} failed:`, e);
    }
  }

  if (typeof data.dnrSlot === 'number' && dnrCleanupOk) {
    _wv2nasUsedDnrSlots.delete(data.dnrSlot);
  }
  _wv2nasActiveBrowserJobs.delete(jobId);

  if (_wv2nasActiveBrowserJobs.size === 0) {
    await _closeOffscreenDocument();
  }

  // For FAILED with no finalize attempt, tell the server to clean
  // staging. (For DONE, server already received finalize and cleanup
  // happens worker-side; for FAILED-after-finalize-attempt, the
  // server may have committed already — Codex #4.)
  const userCancelled = !!(msg.payload && msg.payload.userCancelled);
  const needsAbort = msg.type === 'BROWSER_JOB_FAILED'
    && !msg.payload.finalizeAttempted
    && !userCancelled
    && data.nasEndpoint && data.apiKey;
  if (needsAbort) {
    const reason = msg.payload.error || 'Durable handler abort';
    await _wv2nasPersistAbortRetry(jobId, reason, { dnrCleaned: dnrCleanupOk });
    const abortOk = await _wv2nasAbortBrowserJob(
      data.nasEndpoint, data.apiKey, jobId, reason,
    );
    if (abortOk) {
      await _wv2nasUnpersistBrowserJob(jobId);
    }
  } else {
    await _wv2nasUnpersistBrowserJob(jobId);
  }
}


// Codex review #19a: gate durable completion handling on recovery
// finishing first.
//
// At SW boot, two things race:
//   1. _wv2nasRecoverStaleBrowserJobs walks chrome.storage.local and
//      repopulates the in-memory `_wv2nasActiveBrowserJobs` map with
//      survivor jobs (so the offscreen-close decision can see them).
//   2. The runtime.onMessage listener immediately starts handling
//      BROWSER_JOB_DONE / BROWSER_JOB_FAILED via the durable handler.
//
// If a completion message lands BEFORE recovery has populated the
// map, the durable handler sees `_wv2nasActiveBrowserJobs.size === 0`
// and closes the offscreen document — even though OTHER recovered
// jobs are about to be added to the map and are still actively
// downloading via the same offscreen. The torn-down jobs then get
// reaped by the watchdog hours later.
//
// Fix: capture the recovery promise once at SW boot and have the
// durable handler `await` it before reading the map. The promise's
// catch() converts errors to resolutions so a failed recovery
// doesn't deadlock subsequent completions.
let _wv2nasRecoveryComplete = null;

function _wv2nasInitRecovery() {
  if (_wv2nasRecoveryComplete === null) {
    _wv2nasRecoveryComplete = _wv2nasRecoverStaleBrowserJobs().catch((e) => {
      console.warn('[wv2nas] stale browser-job recovery at SW boot failed:', e);
    });
  }
  return _wv2nasRecoveryComplete;
}


// Top-level call (runs on every SW boot, including cold-starts after
// MV3 eviction). Reads persisted jobs, recovers anything stranded.
async function _wv2nasRecoverStaleBrowserJobs() {
  const jobs = await _wv2nasReadPersistedBrowserJobs();
  const now = Date.now();
  // Codex adversarial-review: track stale job ids and delete per-job
  // through the serialized unpersist path. The previous version wrote
  // a `survivors` map back wholesale, which raced with concurrent
  // heartbeat/persist calls (possible during boot if a queued
  // BROWSER_JOB_DONE/FAILED message wakes the SW alongside this
  // sweep) and dropped their entries.
  const staleJobIds = [];
  let recovered = 0;

  for (const [jobId, data] of Object.entries(jobs)) {
    const startedAt = Number(data.startedAt) || 0;
    const lastHeartbeat = Number(data.lastHeartbeat) || 0;
    const now2 = now;  // local alias for clarity below

    // Codex review #16: prefer heartbeat-based liveness when available.
    // A job that's actively heartbeating is alive regardless of how
    // old startedAt is. Without this, legitimate slow downloads
    // (multi-hour 4K movies) get reaped after 1h while still uploading.
    const abortPending = data.abortPending === true;
    let isAlive;
    if (abortPending) {
      isAlive = false;
    } else if (lastHeartbeat > 0) {
      isAlive = (now2 - lastHeartbeat) < _BROWSER_JOB_HEARTBEAT_TIMEOUT_MS;
    } else {
      // No heartbeat yet — either pre-fix legacy entry or offscreen
      // died before sending one. Fall back to startedAt age.
      isAlive = (now2 - startedAt) < _BROWSER_JOB_STALE_MS;
    }
    const age = now - startedAt;

    if (isAlive) {
      // Recent job — could still be in-flight on offscreen. Leave it
      // for the in-memory listener (or a later boot) to handle.
      // Note: if SW restart happened, the in-memory listener is gone;
      // the next boot's stale-recovery sweep catches it eventually.
      // Codex review #14: SW restart leaves _wv2nasUsedDnrSlots empty.
      // If we don't re-reserve the survivor's slot, a brand-new job
      // can allocate the SAME slot — its updateSessionRules call would
      // overwrite the in-flight survivor's DNR rules, breaking the
      // running download. Restore the reservation so allocation
      // skips this slot. Same with the active-jobs map for offscreen
      // ref-counting.
      if (typeof data.dnrSlot === 'number') {
        _wv2nasUsedDnrSlots.add(data.dnrSlot);
      }
      _wv2nasActiveBrowserJobs.set(jobId, {
        dnrSlot: data.dnrSlot,
        ruleIds: Array.isArray(data.ruleIds) ? data.ruleIds : [],
      });
      continue;
    }

    recovered += 1;
    console.warn(
      `[wv2nas] recovering stale browser job ${jobId} (age ${Math.round(age / 60000)}min)`
    );

    // Remove DNR rules (idempotent — stale IDs are silently skipped).
    let dnrCleanupOk = true;
    if (Array.isArray(data.ruleIds) && data.ruleIds.length > 0) {
      try {
        await chrome.declarativeNetRequest.updateSessionRules({
          removeRuleIds: data.ruleIds,
        });
      } catch (e) {
        dnrCleanupOk = false;
        console.warn(`[wv2nas] DNR cleanup for stale ${jobId} failed:`, e);
      }
    }

    // Abort the server-side job + wipe staging.
    let abortOk = true;
    if (data.nasEndpoint && data.apiKey) {
      abortOk = await _wv2nasAbortBrowserJob(
        data.nasEndpoint, data.apiKey, jobId,
        data.abortReason || 'Stale browser job recovered at SW boot',
      );
    }

    // Release the slot so the next boot doesn't see it as taken.
    if (typeof data.dnrSlot === 'number' && dnrCleanupOk) {
      _wv2nasUsedDnrSlots.delete(data.dnrSlot);
    }
    if (abortOk) {
      staleJobIds.push(jobId);
    } else {
      await _wv2nasPersistAbortRetry(
        jobId,
        data.abortReason || 'Stale browser job recovered at SW boot',
        { dnrCleaned: dnrCleanupOk },
      );
    }
  }

  // Per-job deletion through the mutex — survivors and any concurrently
  // added entries (e.g. a heartbeat that arrived during the sweep)
  // are preserved. Each unpersist re-reads the latest state under the
  // lock, so we never accidentally resurrect an entry that was being
  // added concurrently.
  for (const jobId of staleJobIds) {
    await _wv2nasUnpersistBrowserJob(jobId);
  }
  if (recovered > 0) {
    console.warn(`[wv2nas] recovered ${recovered} stale browser job(s) at SW boot`);
  }
}

// Best-effort abort helper. Tells the NAS to mark the job failed +
// wipe its staging dir (Codex review #3). Returns true only when the
// caller can safely forget its persisted abort-retry state.
async function _wv2nasAbortBrowserJob(nasEndpoint, apiKey, jobId, reason) {
  try {
    const resp = await fetch(`${nasEndpoint}/api/jobs/${encodeURIComponent(jobId)}/abort`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify({ reason: (reason || '').slice(0, 500) }),
    });
    if (!resp.ok) {
      console.warn(`[wv2nas] abort ${jobId} returned ${resp.status}`);
      return resp.status === 404;
    }
    return true;
  } catch (e) {
    console.warn(`[wv2nas] abort ${jobId} request failed:`, e);
    return false;
  }
}

async function runBrowserSideJob({ nasEndpoint, apiKey, requestBody, title, pageUrl, formatHint, trustedCdnSuffixes }) {
  // Codex adversarial-review: SW cold-start kicks off
  // `_wv2nasInitRecovery()` which re-reserves DNR slots for surviving
  // offscreen jobs (and re-populates `_wv2nasUsedDnrSlots`). That
  // recovery is async — if a new browser-side download fires before
  // it finishes, `_wv2nasAllocateDnrSlot()` reads an empty
  // `_wv2nasUsedDnrSlots` and reuses a slot that the survivor still
  // owns. The subsequent `updateSessionRules` call then removes /
  // overwrites the survivor's DNR rules and the in-flight download
  // breaks. Waiting for recovery is cheap (single boot pass) and
  // closes the race deterministically.
  await _wv2nasInitRecovery();

  // Codex review #8: header context for both DNR phases is computed
  // up-front so the manifest fetch can also benefit from the player's
  // captured Referer/Origin/UA. Without this, sites that gate the
  // manifest itself on header checks return 403 to the browser fetch,
  // and we fall back to URL-only init where NAS also can't reach the
  // protected URL — defeating browser-side mode for a core target case.
  const referer = requestBody.referer || pageUrl;
  let originValue = null;
  try { originValue = referer ? new URL(referer).origin : null; } catch (_) {}
  const userAgent = (requestBody.headers && (requestBody.headers['User-Agent'] || requestBody.headers['user-agent'])) || null;

  // Allocate a per-job DNR slot UP-FRONT (before init, before manifest
  // fetch) so we can install header-spoof rules that cover the manifest
  // URL itself. The same slot is reused for the post-init segment+key
  // rules, so there's no double-allocation cost.
  const dnrSlot = _wv2nasAllocateDnrSlot();
  const idBase = _BROWSER_DNR_BASE_ID + dnrSlot * _BROWSER_DNR_PER_JOB_RANGE;

  let ruleIds = [];
  let jobId = null;
  // Codex review #3: track whether the happy path completed. Anything
  // less means the server has a half-staged job + partial files, which
  // the abort endpoint cleans up.
  let completionSucceeded = false;
  let abortReason = null;
  // Codex review #4: track whether finalize was attempted. After the
  // finalize POST leaves the wire, the server may have committed the
  // queue push regardless of what the client sees; calling abort then
  // would destroy a queued job's staged segments. Only safe to abort
  // when both completion failed AND finalize was never attempted.
  let finalizeAttempted = false;
  let userCancelled = false;

  try {
    // Codex adversarial-review (high): determine whether the master
    // URL is same-site with the page that surfaced it. We use this
    // both to gate the browser-side manifest fetch (handled inside
    // _wv2nasFetchManifestInBrowser) and to scope phase-1 DNR rules.
    // Without explicit `trustedSegmentUrls`, _wv2nasBuildDnrRules
    // defaults to all-trusted and emits CORS-relax rules — which
    // would let the extension read a cross-origin response (e.g.
    // an internal split-horizon host) cross-origin and post it as
    // manifest_text to the NAS. Pass an explicit empty trusted set
    // for untrusted hosts so neither header-spoof nor CORS-relax
    // emits.
    const masterTrustAnchor = pageUrl || (requestBody && requestBody.source_page) || null;
    let masterUrlHost = null;
    try {
      masterUrlHost = new URL(requestBody.url).hostname.toLowerCase();
    } catch (_) { /* malformed URL caught earlier by safety gate */ }
    // User-allowlisted CDN suffix counts as same-site for the DNR
    // CORS-relax decision too — otherwise the response body comes back
    // opaque and `_wv2nasFetchManifestInBrowser` can't read it as
    // `manifest_text`. The user explicitly asserted trust for this
    // suffix in extension options.
    const masterTrustedForDnr = masterTrustAnchor && (
      _wv2nasIsTrustedDnrUrl(requestBody.url, masterTrustAnchor)
      || _wv2nasMatchesTrustedCdnSuffix(masterUrlHost, trustedCdnSuffixes)
    );

    // === Phase 1 DNR: cover the manifest URL ===
    // Codex review #8: install header-spoof rules for the manifest URL
    // BEFORE _wv2nasFetchManifestInBrowser runs. Sites that 403 on a
    // bare fetch will see Referer/Origin/UA matching what the player
    // sent and serve the manifest the same way.
    const phase1Rules = _wv2nasBuildDnrRules({
      segmentUrls: [requestBody.url],
      // Codex adversarial-review: explicit trusted set — empty when
      // the master URL isn't same-site with the page so CORS-relax
      // doesn't fire on an unvalidated host.
      trustedSegmentUrls: masterTrustedForDnr ? [requestBody.url] : [],
      referer, origin: originValue, userAgent, idBase,
      initiatorDomain: chrome.runtime.id,
    });
    if (phase1Rules.length > 0) {
      const phase1Ids = phase1Rules.map((r) => r.id);
      await chrome.declarativeNetRequest.updateSessionRules({
        removeRuleIds: phase1Ids,
        addRules: phase1Rules,
      });
      ruleIds = phase1Ids;
    }

    // Now fetch the manifest with DNR rules active.
    // Codex review: pass the dnrContext so the helper can (1) extend
    // DNR coverage to the variant URL when an HLS master resolves to
    // a variant on a different directory/origin, and (2) ride captured
    // auth headers (Authorization / X-* tokens) along with the fetch.
    // Without (1), variant fetches lack spoofed Referer/Origin/UA and
    // 403. Without (2), header-token-gated manifests 401 before init.
    const browserFetched = await _wv2nasFetchManifestInBrowser(
      requestBody.url,
      {
        referer,
        origin: originValue,
        userAgent,
        idBase,
        ruleIds,
        headers: requestBody.headers,
        // Codex adversarial-review: anchor for the same-site safety
        // check inside _wv2nasIsManifestUrlSafeForBrowser. The
        // master URL must be same-site with the page that surfaced
        // it, otherwise we fall through to NAS-side planning where
        // _safe_fetch's resolve+public-IP guard runs.
        pageUrl: pageUrl || requestBody.source_page || null,
        // User-allowlisted cross-site CDN suffixes (option E). When
        // the master URL host matches one, the same-site gate is
        // bypassed for THIS URL only.
        trustedCdnSuffixes,
      },
    );
    if (browserFetched && browserFetched.safetyRejected) {
      throw new Error(
        `Browser-side manifest fetch refused: ${browserFetched.reason || 'unsafe URL'}`
      );
    }
    const initPayload = {
      title: requestBody.title,
      referer: requestBody.referer,
      headers: requestBody.headers,
      source_page: requestBody.source_page,
      output_subdir: requestBody.output_subdir,
      container_hint: formatHint,
    };
    if (browserFetched) {
      initPayload.manifest_text = browserFetched.manifest_text;
      initPayload.base_url = browserFetched.base_url;
      // base_url already pinpoints the variant; don't also send url to
      // avoid the server running a redundant fetch path.
    } else {
      initPayload.url = requestBody.url;
    }

    const initResp = await fetch(`${nasEndpoint}/api/jobs/init`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify(initPayload),
    });
    if (!initResp.ok) {
      const errJson = await initResp.json().catch(() => ({}));
      const detail = formatApiErrorDetail(errJson, initResp.status);
      const initErr = new Error(`Init failed: ${detail}`);
      // Codex adversarial-review (high): only 404 is a clear
      // compatibility issue (the /api/jobs/init endpoint doesn't
      // exist on a legacy NAS that hasn't been upgraded to v2.5).
      //
      // Other 4xx — especially 422 — are SAFETY rejections that
      // MUST fail closed:
      //   * 422: HTTPS-required gate, non-public host (localhost /
      //     RFC 1918 / metadata IP). Falling back to /api/download
      //     would let the NAS worker fetch the rejected URL
      //     server-side. /api/download only enforces
      //     `_enforce_ssrf_guard` when SSRF_GUARD env is set
      //     (default off in shipped configs), so the fallback
      //     re-opens the exact intranet/metadata access this gate
      //     was designed to block.
      //   * 401/403: bad API key — NAS-direct uses the same key,
      //     fallback achieves nothing.
      //   * 400: malformed payload — fallback won't fix a client bug.
      //   * 429: rate limit — same NAS, same limit.
      // Keeping these terminal preserves the fail-closed boundary;
      // users running legitimate HTTP-only streams can disable the
      // browser-side mode option to use NAS-direct explicitly.
      if (initResp.status === 404) {
        initErr.fallbackable = true;
      }
      throw initErr;
    }
    const initJson = await initResp.json();
    jobId = initJson.job_id;
    const plan = initJson.plan;
    _wv2nasRefreshSignedPlanUrls(
      plan,
      [
        requestBody.url,
        browserFetched && browserFetched.base_url,
        plan && plan.source_url,
        plan && plan.selected_variant_url,
      ].filter(Boolean),
    );

    // Register active job BEFORE further side effects so the offscreen
    // ref-count guard sees this job in the map.
    _wv2nasActiveBrowserJobs.set(jobId, { dnrSlot, ruleIds });

    // Codex review #12: persist to chrome.storage.local so a SW
    // restart (MV3 evicts SWs aggressively) doesn't strand DNR rules
    // and server-side staging. The boot-time watchdog
    // _wv2nasRecoverStaleBrowserJobs walks this storage on every SW
    // init and aborts/cleans any job older than _BROWSER_JOB_STALE_MS.
    await _wv2nasPersistBrowserJob(jobId, {
      ruleIds, dnrSlot, startedAt: Date.now(),
      nasEndpoint, apiKey,
    });

    // === Phase 2 DNR: replace with full segment + key URI coverage ===
    // We've already used the slot for manifest rules; replace those
    // with the segment / init / AES-key URIs from the plan. Same slot
    // and same idBase, so updateSessionRules(removeRuleIds: ALL old IDs
    // + new IDs) cleans up phase 1 in one round trip.
    //
    // Codex review #10: filter URLs by trust to feed the new
    // trustedSegmentUrls parameter. CORS-relax only applies to URLs
    // sharing the manifest's trusted host boundary. Foreign origins get
    // no DNR rule, so they don't receive captured Referer/Origin/UA and
    // their responses stay unreadable to the extension.
    const segmentUrls = _wv2nasPlanSegmentUrls(plan);
    const trustedBaseForDnr = plan.selected_variant_url || plan.source_url || requestBody.url;
    const trustedSegmentUrls = segmentUrls.filter(
      (u) => _wv2nasIsTrustedDnrUrl(u, trustedBaseForDnr)
    );
    const rules = _wv2nasBuildDnrRules({
      segmentUrls,
      trustedSegmentUrls,
      referer, origin: originValue, userAgent, idBase,
      initiatorDomain: chrome.runtime.id,
    });
    const newIds = rules.map((r) => r.id);
    if (rules.length > 0) {
      // removeRuleIds must include both the OLD phase 1 IDs (from
      // ruleIds) and the new IDs we're about to add — DNR semantics
      // for updateSessionRules first applies removes then adds, so
      // including new IDs in removes is a defensive idempotent action.
      const idsToRemove = Array.from(new Set([...ruleIds, ...newIds]));
      // Persist the cleanup superset BEFORE the DNR mutation. If the
      // MV3 service worker is killed after updateSessionRules succeeds
      // but before the post-update persist, the boot watchdog still has
      // every rule ID it must remove.
      await _wv2nasPersistBrowserJob(jobId, { ruleIds: idsToRemove });
      await chrome.declarativeNetRequest.updateSessionRules({
        removeRuleIds: idsToRemove,
        addRules: rules,
      });
    } else if (ruleIds.length > 0) {
      // No phase 2 rules but we have phase 1 rules from earlier;
      // remove them — manifest-only fetch is done.
      await chrome.declarativeNetRequest.updateSessionRules({
        removeRuleIds: ruleIds,
      });
    }
    ruleIds = newIds;
    _wv2nasActiveBrowserJobs.get(jobId).ruleIds = ruleIds;
    // Codex review #12: keep persisted ruleIds in sync so the boot
    // watchdog removes the LATEST rule set (not the phase-1 stub).
    await _wv2nasPersistBrowserJob(jobId, { ruleIds });

    // 3) Ensure offscreen exists. Idempotent — reuses existing doc when
    // another concurrent job already opened it.
    await _ensureOffscreenDocument();

    const completion = new Promise((resolve, reject) => {
      function onMessage(msg) {
        if (!msg || msg.target !== 'service-worker') return;
        if (msg.payload && msg.payload.jobId !== jobId) return;
        if (msg.type === 'BROWSER_JOB_DONE') {
          chrome.runtime.onMessage.removeListener(onMessage);
          resolve(msg.payload.summary || {});
        } else if (msg.type === 'BROWSER_JOB_FAILED') {
          chrome.runtime.onMessage.removeListener(onMessage);
          // Codex review #4: surface finalizeAttempted so the abort
          // decision in the catch block can skip cleanup whenever the
          // failure happened at-or-after the finalize commit boundary.
          const e = new Error(msg.payload.error || 'browser job failed');
          e.finalizeAttempted = !!msg.payload.finalizeAttempted;
          e.userCancelled = !!msg.payload.userCancelled;
          reject(e);
        }
      }
      chrome.runtime.onMessage.addListener(onMessage);
    });

    // Strip headers fetch() refuses to set before handing captured
    // auth/custom headers to the offscreen document. DNR owns
    // Referer/Origin/UA spoofing; credentials:'include' owns Cookie.
    const requestHeaders = _wv2nasFilterFetchHeaders(requestBody.headers || {});

    // Codex adversarial-review (high): offscreen.js's listener replies
    // {ok: true} synchronously on accept (and {ok: false, error: ...}
    // for "job already running"). If sendMessage throws ("Could not
    // establish connection") OR returns a non-ack response, the
    // offscreen runtime is broken — surface as a job error rather than
    // silently waiting forever on `completion`. The READY handshake
    // above SHOULD have prevented the connection-loss case; this is
    // belt-and-braces.
    const ack = await chrome.runtime.sendMessage({
      target: 'offscreen',
      type: 'START_BROWSER_JOB',
      payload: {
        jobId,
        nasEndpoint,
        apiKey,
        plan,
        requestHeaders,
      },
    });
    if (!ack || ack.ok !== true) {
      const reason = (ack && ack.error) || 'no ack from offscreen';
      throw new Error(`offscreen rejected START_BROWSER_JOB: ${reason}`);
    }

    const summary = await completion;
    completionSucceeded = true;
    if (userSettings.showNotifications) {
      showNotification(
        'Browser-side download complete',
        `"${title}" — ${summary.totalSegments || '?'} segments staged. Worker is muxing.`
      );
    }
    storeJob({ id: jobId, url: requestBody.url, title, status: 'processing', progress: 50, mode: 'browser' });
  } catch (err) {
    abortReason = String(err && err.message || err);
    // err.finalizeAttempted is set by segmentDownloader.runJob and
    // propagated through offscreen → BROWSER_JOB_FAILED message →
    // completion promise reject.
    finalizeAttempted = !!(err && err.finalizeAttempted);
    userCancelled = !!(err && err.userCancelled);
    throw err;
  } finally {
    // Cleanup order: remove this job's rules → release slot → unregister
    // job → close offscreen ONLY IF this was the last active job.
    //
    // Codex review #8: jobId may be null (init failed before /jobs/init
    // returned a job_id, or _wv2nasFetchManifestInBrowser failed). The
    // DNR slot was allocated up-front so it always needs releasing;
    // ruleIds may have phase-1 (manifest URL) rules even without jobId.
    let dnrCleanupOk = true;
    if (ruleIds.length > 0) {
      try {
        await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ruleIds });
      } catch (e) {
        dnrCleanupOk = false;
        console.warn('[wv2nas] DNR cleanup failed:', e);
      }
    }
    if (dnrCleanupOk) {
      _wv2nasReleaseDnrSlot(dnrSlot);
    }
    if (jobId) {
      _wv2nasActiveBrowserJobs.delete(jobId);
    }
    if (_wv2nasActiveBrowserJobs.size === 0) {
      // Last active job — safe to free the offscreen document. If another
      // job arrived between our delete() and this check (not possible from
      // the SW's single-threaded message loop, but being explicit), the
      // ref-count check still holds because that job would have called
      // _ensureOffscreenDocument by now.
      await _closeOffscreenDocument();
    }
    // Codex review #3+#4+#8: tell the server to mark the job failed and
    // wipe its staging dir ONLY when:
    //   1. We have a jobId (init succeeded — server has staging),
    //   2. Completion did not succeed,
    //   3. Finalize was never attempted (post-finalize failures may
    //      have already committed server-side; abort would destroy
    //      a queued job's staged segments),
    //   4. The failure was not produced by an accepted user cancel
    //      (DELETE owns that state transition and staging cleanup).
    if (jobId && !completionSucceeded && !finalizeAttempted && !userCancelled) {
      await _wv2nasPersistAbortRetry(jobId, abortReason, { dnrCleaned: dnrCleanupOk });
      const abortOk = await _wv2nasAbortBrowserJob(nasEndpoint, apiKey, jobId, abortReason);
      if (abortOk) {
        await _wv2nasUnpersistBrowserJob(jobId);
      }
    } else if (jobId) {
      // Codex review #12: remove from chrome.storage so the boot
      // watchdog doesn't double-clean a job that already finished
      // normally. unpersist is idempotent (no-op if entry missing).
      await _wv2nasUnpersistBrowserJob(jobId);
    }
  }
}

console.log('WebVideo2NAS background service worker loaded');

// Codex review #12: run the stale-job watchdog at SW boot so MV3
// evictions don't permanently strand server staging + DNR rules.
// Codex review #19a: kicks off the shared recovery promise that the
// durable completion handler awaits, so a DONE/FAILED message at boot
// can't tear down the offscreen document before recovery has
// repopulated _wv2nasActiveBrowserJobs.
_wv2nasInitRecovery();

// Codex review #15: durable completion listener. Registered at
// top-level so it survives SW restarts and handles
// BROWSER_JOB_DONE/FAILED for jobs whose per-call in-memory listener
// is gone. Coexists with the per-job listener registered inside
// runBrowserSideJob — all cleanup ops are idempotent so double-fire
// in the SW-alive case is safe.
chrome.runtime.onMessage.addListener((msg, _sender, _sendResponse) => {
  if (!msg || msg.target !== 'service-worker') return false;
  if (msg.type === 'BROWSER_JOB_DONE' || msg.type === 'BROWSER_JOB_FAILED') {
    _wv2nasHandleDurableCompletion(msg).catch((e) => {
      console.warn('[wv2nas] durable completion handler failed:', e);
    });
  } else if (msg.type === 'BROWSER_JOB_HEARTBEAT') {
    // Codex review #16: persist the heartbeat so the next SW-boot
    // watchdog can use it as liveness signal. Fire-and-forget — a
    // missed heartbeat write isn't fatal (next one will land).
    const jobId = msg.payload && msg.payload.jobId;
    if (jobId) {
      // Only refresh known persisted jobs. A DONE/FAILED cleanup can race
      // with a queued heartbeat from the offscreen document; recreating the
      // deleted entry would make the next SW boot recover a phantom job.
      _wv2nasPersistBrowserJobHeartbeat(
        jobId,
        msg.payload && msg.payload.ts,
      ).catch(() => {});
    }
  } else if (msg.type === 'BROWSER_JOB_PROGRESS') {
    // Forward offscreen's per-segment progress to other extension
    // contexts (sidepanel listens for `action: 'browserJobProgress'`).
    // The NAS API doesn't track upload progress for browser-side jobs
    // — the extension owns this counter, so the SW just relays it.
    // Fire-and-forget; lastError swallows "no receiver" when no
    // sidepanel/options page is open.
    const p = msg.payload || {};
    if (p.jobId) {
      chrome.runtime.sendMessage({
        action: 'browserJobProgress',
        jobId: p.jobId,
        done: p.done,
        total: p.total,
      }, () => { void chrome.runtime.lastError; });
    }
  } else if (msg.type === 'OFFSCREEN_READY') {
    // Codex adversarial-review (high): offscreen.js sends this once its
    // chrome.runtime.onMessage listener is registered. Resolves the
    // gate _ensureOffscreenDocument() awaits, so START_BROWSER_JOB is
    // never posted before the listener exists.
    _markOffscreenReady();
  }
  return false;
});
