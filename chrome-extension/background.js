// Background Service Worker for Video Detection and Download Management

// Store detected video URLs (m3u8, mpd, mp4)
let detectedUrls = new Set();
let currentTabUrls = {};
let currentTabUrlKeys = {};
let lastNotifyAtByTab = {};

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

  orphanUrlKeys = new Set(orphanUrlInfos.map(x => x.url));
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

  const urlInfo = {
    url: details.url,
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

  detectedUrls.add(details.url);

  if (isRealTab) {
    if (!currentTabUrls[details.tabId]) {
      currentTabUrls[details.tabId] = [];
    }
    if (!currentTabUrlKeys[details.tabId]) {
      currentTabUrlKeys[details.tabId] = new Set();
    }

    if (!currentTabUrlKeys[details.tabId].has(details.url)) {
      currentTabUrlKeys[details.tabId].add(details.url);
      currentTabUrls[details.tabId].push(urlInfo);
      attachTabTitle(urlInfo, details.tabId);
    } else {
      const list = currentTabUrls[details.tabId];
      const existing = list.find(item => item && item.url === details.url);
      if (existing) {
        existing.timestamp = urlInfo.timestamp;
        existing.pageUrl = urlInfo.pageUrl;
        existing.requestType = urlInfo.requestType;
        existing.frameId = urlInfo.frameId;
        existing.method = urlInfo.method;
        existing.hitCount = (Number(existing.hitCount) || 0) + 1;
        if (extra && extra.detectedFormat && !existing.detectedFormat) {
          existing.detectedFormat = extra.detectedFormat;
        }
        // Refresh title in case the first capture raced with a transient
        // empty-title state (loading SPA, etc).
        if (!existing.pageTitle) attachTabTitle(existing, details.tabId);
        notifyDetectedUrlsUpdated(details.tabId);
      }
    }
  } else {
    if (!orphanUrlKeys.has(details.url)) {
      orphanUrlKeys.add(details.url);
      orphanUrlInfos.push(urlInfo);
      pruneOrphans();
    } else {
      const existing = orphanUrlInfos.find(item => item && item.url === details.url);
      if (existing) {
        existing.timestamp = urlInfo.timestamp;
        existing.pageUrl = urlInfo.pageUrl;
        existing.requestType = urlInfo.requestType;
        existing.frameId = urlInfo.frameId;
        existing.method = urlInfo.method;
        existing.hitCount = (Number(existing.hitCount) || 0) + 1;
        if (extra && extra.detectedFormat && !existing.detectedFormat) {
          existing.detectedFormat = extra.detectedFormat;
        }
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
  delete userClickedVideoByTab[tabId];
  delete pageThumbnailsByTab[tabId];
});

// Clear detected URLs when page navigates or reloads
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) { // Only for main frame
    // Clear URLs for this tab on navigation/reload
    currentTabUrls[details.tabId] = [];
    currentTabUrlKeys[details.tabId] = new Set();
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
    sendToNAS(url, tab.title, tab.url);
  } else {
    // Try to find video URL in current tab
    const tabUrls = currentTabUrls[tab.id];
    if (tabUrls && tabUrls.length > 0) {
      // Send the best candidate (prefer "now playing" heuristics)
      const best = getSortedUrlsForTab(tab.id)[0];
      sendToNAS(best.url, tab.title, tab.url);
    } else {
      showNotification('Error', 'No video URL found on this page');
    }
  }
});

// Check if a URL was detected via Content-Type (stored in per-tab or orphan lists)
function getDetectedFormat(url) {
  for (const tabId of Object.keys(currentTabUrls)) {
    const list = currentTabUrls[tabId];
    if (!Array.isArray(list)) continue;
    const item = list.find(x => x && x.url === url);
    if (item && item.detectedFormat) return item.detectedFormat;
  }
  for (const item of orphanUrlInfos) {
    if (item && item.url === url && item.detectedFormat) return item.detectedFormat;
  }
  return null;
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

// Send URL to NAS
async function sendToNAS(url, pageTitle, pageUrl) {
  try {
    const formatHint = getDetectedFormat(url);
    if (!isCandidateVideoUrl(url) && !formatHint) {
      showNotification('Error', 'Not a valid video URL');
      return;
    }

    // Get settings
    const settings = await chrome.storage.sync.get(['nasEndpoint', 'apiKey', 'nasOutputSubdir']);

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

    function tryGetUrl(u) {
      try { return new URL(u); } catch (_) { return null; }
    }

    function originOf(u) {
      const uu = tryGetUrl(u);
      return uu ? uu.origin : null;
    }

    function hasCookieHeader(headers) {
      if (!headers) return false;
      for (const k of Object.keys(headers)) {
        if (typeof k === 'string' && k.toLowerCase() === 'cookie') return true;
      }
      return false;
    }

    function findBestCapturedEntry(targetUrl, sourcePageUrl) {
      const t = tryGetUrl(targetUrl);
      if (!t) return null;

      let best = null;
      const sourceOrigin = originOf(sourcePageUrl);

      for (const [k, entry] of Object.entries(capturedHeaders)) {
        const ku = tryGetUrl(k);
        if (!ku || !entry) continue;

        // Only consider manifest captures (m3u8/mpd or Content-Type detected)
        const kl = k.toLowerCase();
        const isManifestByExt = kl.includes('.m3u8') || kl.includes('.mpd');
        const isManifestByFormat = !!getDetectedFormat(k);
        if (!isManifestByExt && !isManifestByFormat) continue;

        // Match on the SOURCE PAGE'S origin, not the currently active tab.
        // The previous +10 used `chrome.tabs.query({active:true})` which is the
        // tab the user happens to be looking at right now — not the tab the
        // URL was detected on. When the user switches tabs and then clicks a
        // tile from the previous tab, that mismatch caused another tab's
        // captured manifest to score highest and OVERWRITE the URL the user
        // actually clicked. Origin is intrinsic to the URL/page, so it
        // survives tab switches and tab close/reopen.
        let score = 0;
        if (sourceOrigin && entry.initiator && entry.initiator.startsWith(sourceOrigin)) score += 10;
        if (ku.origin === t.origin) score += 5;
        if (ku.pathname === t.pathname) score += 2;
        // Prefer tokenized URLs (query params) as they often map to full playlists
        if (ku.search && ku.search.length > 1) score += 3;
        // Prefer captured requests that already carried Cookie headers
        if (hasCookieHeader(entry.headers)) score += 3;
        if (entry.timestamp && (Date.now() - entry.timestamp) < 60_000) score += 1;

        if (!best) {
          best = { url: k, entry, score };
          continue;
        }

        if (score > best.score) {
          best = { url: k, entry, score };
          continue;
        }

        if (score === best.score && (entry.timestamp || 0) > (best.entry.timestamp || 0)) {
          best = { url: k, entry, score };
        }
      }
      return best;
    }

    let captured = capturedHeaders[url];
    const best = findBestCapturedEntry(url, pageUrl);

    // Use the best captured m3u8 when it's a strong match for this URL's
    // SOURCE page+origin, even if we have an exact key hit. Exact matches are
    // often "clean" URLs (no token) while the real player request contains
    // query params. Hard requirement: the substitute must be from the SAME
    // origin as the URL the user actually clicked — never substitute across
    // origins, which would silently send a video from a different site.
    const sourceOrigin = originOf(pageUrl);
    const targetOrigin = originOf(url);
    const bestUrlOrigin = best ? originOf(best.url) : null;
    const sameOrigin = !!best && (
      (sourceOrigin && best.entry.initiator && best.entry.initiator.startsWith(sourceOrigin)) ||
      (targetOrigin && bestUrlOrigin && bestUrlOrigin === targetOrigin)
    );

    const shouldUseBest =
      !!best && sameOrigin &&
      (captured == null || best.score >= 15 || (best.entry && best.entry.timestamp && captured.timestamp && best.entry.timestamp > captured.timestamp));

    if (shouldUseBest) {
      captured = best.entry;
      urlToSend = best.url;
      console.log('Using best captured manifest for this URL\'s source page:', urlToSend);
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
        sendResponse({ urls });
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
          sendResponse({ urls });
        });
      } else {
        sendResponse({ urls: [] });
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

    // Hold the message channel open until sendToNAS settles. Without `return
    // true` + a deferred sendResponse, Chrome considers this handler done the
    // moment we return synchronously, and the MV3 service worker becomes
    // eligible for shutdown between the awaits inside sendToNAS (storage.get
    // → cookies.getAll → fetch). When the user fires Send across multiple
    // tabs in quick succession, the first 1–2 land but the later ones lose
    // their in-flight chains to SW termination and never reach the NAS.
    sendToNAS(request.url, titleToUse, request.pageUrl)
      .then(() => sendResponse({ success: true }))
      .catch((err) => {
        console.error('sendToNAS failed:', err);
        sendResponse({ success: false, error: err && err.message });
      });
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

console.log('WebVideo2NAS background service worker loaded');
