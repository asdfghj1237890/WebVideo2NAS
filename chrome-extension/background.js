// Background Service Worker for Video Detection and Download Management

// Store detected video URLs (m3u8, mp4)
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
  if (urlLower.includes('.mp4')) score += 1;

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
  const tabOrigin = safeOrigin(tabUrl);
  if (!tabOrigin) return getSortedUrlsForTab(tabId);

  // Collect origins already known to belong to this tab (CDNs, media domains, etc.).
  const tabKnownOrigins = new Set([tabOrigin]);
  for (const item of tabList) {
    const o = item && item.url ? safeOrigin(item.url) : null;
    if (o) tabKnownOrigins.add(o);
  }

  const merged = tabList.slice();
  const seen = new Set(merged.map(x => x && x.url).filter(Boolean));

  for (const info of orphanUrlInfos) {
    if (!info || !info.url) continue;
    if (seen.has(info.url)) continue;

    // Only attach orphans that likely belong to this tab (same initiator/documentUrl origin).
    const pageOrigin = safeOrigin(info.pageUrl);
    const urlOrigin = safeOrigin(info.url);

    // Primary: page origin matches current tab.
    // Fallback: if page origin is missing/unknown, allow matching against known origins
    // already seen in this tab (helps SW-based and CDN-hosted manifests).
    const belongsToTab =
      (pageOrigin && pageOrigin === tabOrigin) ||
      (!pageOrigin && urlOrigin && tabKnownOrigins.has(urlOrigin));

    if (belongsToTab) {
      merged.push(info);
      seen.add(info.url);
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
  if (!(urlLower.includes('.m3u8') || urlLower.includes('.mp4'))) return false;

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

// Listen for web requests to detect video URLs (m3u8, mp4)
chrome.webRequest.onBeforeRequest.addListener(
  function(details) {
    if (!userSettings.autoDetect) return;

    if (isCandidateVideoUrl(details.url)) {
      console.log('Detected video URL:', details.url);
      
      const isRealTab = (details.tabId != null && typeof details.tabId === 'number' && details.tabId >= 0);

      // Store URL with tab info (preserve original URL case)
      const urlInfo = {
        url: details.url,
        tabId: isRealTab ? details.tabId : -1,
        timestamp: Date.now(),
        pageUrl: details.initiator || details.documentUrl,
        requestType: details.type,
        frameId: details.frameId,
        method: details.method,
        hitCount: 1,
        rangeHitCount: 0
      };
      
      // Add to detected URLs
      // Deduplicate globally by exact URL string
      detectedUrls.add(details.url);
      
      // Store for current tab
      if (isRealTab) {
        if (!currentTabUrls[details.tabId]) {
          currentTabUrls[details.tabId] = [];
        }
        if (!currentTabUrlKeys[details.tabId]) {
          currentTabUrlKeys[details.tabId] = new Set();
        }

        // Deduplicate per-tab by exact URL string: only show once in sidepanel
        if (!currentTabUrlKeys[details.tabId].has(details.url)) {
          currentTabUrlKeys[details.tabId].add(details.url);
          currentTabUrls[details.tabId].push(urlInfo);
        } else {
          // Update the existing entry's metadata (keep ordering stable)
          const list = currentTabUrls[details.tabId];
          const existing = list.find(item => item && item.url === details.url);
          if (existing) {
            existing.timestamp = urlInfo.timestamp;
            existing.pageUrl = urlInfo.pageUrl;
            existing.requestType = urlInfo.requestType;
            existing.frameId = urlInfo.frameId;
            existing.method = urlInfo.method;
            existing.hitCount = (Number(existing.hitCount) || 0) + 1;
            notifyDetectedUrlsUpdated(details.tabId);
          }
        }
      } else {
        // Orphan request: keep it so sidepanel can still show m3u8 for SW-based sites.
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
            pruneOrphans();
          }
        }
      }
      
      // Update badge
      if (isRealTab) updateBadge(details.tabId);
      
      // Store in chrome.storage for popup access
      chrome.storage.local.set({ detectedUrls: Array.from(detectedUrls) });
    }
  },
  { urls: ["<all_urls>"] }
);

// Capture actual request headers sent by the browser for video URLs
// This includes cookies that Chrome would send to the video domain
chrome.webRequest.onSendHeaders.addListener(
  function(details) {
    const urlLower = details.url.toLowerCase();
    
    // Only capture headers for video URLs
    if (isCandidateVideoUrl(details.url)) {
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
});

// Clear detected URLs when page navigates or reloads
chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) { // Only for main frame
    // Clear URLs for this tab on navigation/reload
    currentTabUrls[details.tabId] = [];
    currentTabUrlKeys[details.tabId] = new Set();
    delete userClickedVideoByTab[details.tabId];
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
  
  // Check if it's a video URL (m3u8 or mp4)
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

// Send URL to NAS
async function sendToNAS(url, pageTitle, pageUrl) {
  try {
    if (!isCandidateVideoUrl(url)) {
      showNotification('Error', 'Not a valid video URL');
      return;
    }

    // Get settings
    const settings = await chrome.storage.sync.get(['nasEndpoint', 'apiKey']);
    
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

    async function getActiveTabId() {
      try {
        const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
        return tabs && tabs[0] ? tabs[0].id : null;
      } catch (_) {
        return null;
      }
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

    function findBestCapturedEntry(targetUrl, tabId, sourcePageUrl) {
      const t = tryGetUrl(targetUrl);
      if (!t) return null;

      let best = null;
      const sourceOrigin = originOf(sourcePageUrl);

      for (const [k, entry] of Object.entries(capturedHeaders)) {
        const ku = tryGetUrl(k);
        if (!ku || !entry) continue;

        // Only consider m3u8 captures
        if (!k.toLowerCase().includes('.m3u8')) continue;

        let score = 0;
        if (tabId != null && entry.tabId === tabId) score += 10;
        if (ku.origin === t.origin) score += 5;
        if (ku.pathname === t.pathname) score += 2;
        // Prefer tokenized URLs (query params) as they often map to full playlists
        if (ku.search && ku.search.length > 1) score += 3;
        // Prefer captured requests that already carried Cookie headers
        if (hasCookieHeader(entry.headers)) score += 3;
        if (sourceOrigin && entry.initiator && entry.initiator.startsWith(sourceOrigin)) score += 3;
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

    const tabId = await getActiveTabId();
    let captured = capturedHeaders[url];
    const best = findBestCapturedEntry(url, tabId, pageUrl);

    // Use the best captured m3u8 when it's a strong match for this tab+origin,
    // even if we have an exact key hit. Exact matches are often "clean" URLs
    // (no token) while the real player request contains query params.
    const shouldUseBest =
      !!best &&
      (captured == null || best.score >= 15 || (best.entry && best.entry.timestamp && captured.timestamp && best.entry.timestamp > captured.timestamp));

    if (shouldUseBest) {
      captured = best.entry;
      urlToSend = best.url;
      console.log('Using best captured m3u8 for this tab:', urlToSend);
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
      const error = await response.json();
      throw new Error(error.detail || 'Failed to submit download');
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
    showNotification('Error', error.message);
  }
}

// Show notification
function showNotification(title, message) {
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title: title,
    message: message
  });
}

// Store job information
async function storeJob(job) {
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
}

// Listen for action clicks to open sidepanel
chrome.action.onClicked.addListener(async (tab) => {
  // Open sidepanel when extension icon is clicked
  await chrome.sidePanel.open({ tabId: tab.id });
});

// Listen for messages from sidepanel and content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
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
        const mp4Urls = list.filter(u => String(u.url || '').toLowerCase().includes('.mp4'));
        const urlsInOrder = m3u8Urls.length > 0 ? m3u8Urls : mp4Urls;
        
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
        sendResponse({ urls: getSortedUrlsForTabWithOrphans(requestedTabId, tabUrl) });
      });
      return true;
    }

    // Fallback: Return URLs for current active tab
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.get(tabs[0].id, (tab) => {
          const tabUrl = tab && tab.url ? tab.url : (tabs[0].url || '');
          const urls = getSortedUrlsForTabWithOrphans(tabs[0].id, tabUrl);
          sendResponse({ urls });
        });
      } else {
        sendResponse({ urls: [] });
      }
    });
    return true; // Keep channel open for async response
  }

  if (request.action === 'sendToNAS') {
    sendToNAS(request.url, request.title, request.pageUrl);
    sendResponse({ success: true });
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
