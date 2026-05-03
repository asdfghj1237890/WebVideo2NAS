// Sidepanel Script for WebVideo2NAS — Thumbnail Grid design.
// Adapts from few (2-up + per-tile send) to many (3-up + multi-select + bulk bar).
// UX animations: tile enter (staggered), pulse on new detection, hover lift,
// flight ghost on send, checkbox bounce, bulk bar slide, smooth ring tween.

let settings = {};
let detectedUrls = [];
let jobs = [];
let expandedErrorIds = new Set();
let activeTabId = null;
let loadDetectedUrlsSeq = 0;

// Design state
let theme = 'dark';
let selected = new Set();         // selected detected-URL ids (URL strings)
let sentUrls = new Set();         // URLs that have been sent at least once (visual feedback)
let qualityFilter = 'all';
let searchQuery = '';
let prevDetectedIds = new Set();  // tracks which URLs were rendered last frame, for pulse-new
let firstRenderDone = false;
let jobSort = 'failed';           // 'failed' | 'active' — recent jobs sort mode (failed-first by default so token-expiry/abort jobs surface immediately)

const JOB_SORT_CYCLE = ['failed', 'active'];

const STATUS_RANK_ACTIVE = {
  downloading: 0, processing: 1, merging: 1,
  pending: 2,
  completed: 3,
  failed: 4, cancelled: 5,
};
const STATUS_RANK_FAILED = {
  failed: 0,
  downloading: 1, processing: 1, merging: 1,
  pending: 2,
  completed: 3,
  cancelled: 4,
};

function sortJobs(list, mode) {
  if (mode === 'time' || !mode) return list.slice();
  const rank = mode === 'failed' ? STATUS_RANK_FAILED : STATUS_RANK_ACTIVE;
  return list.map((j, i) => ({ j, i }))
    .sort((a, b) => {
      const ra = rank[a.j.status] ?? 99;
      const rb = rank[b.j.status] ?? 99;
      if (ra !== rb) return ra - rb;
      return a.i - b.i; // stable within same rank
    })
    .map(x => x.j);
}

// Per-job tween bookkeeping (RAF for smooth ring + bar fill)
const jobTweens = new Map();      // id -> { shown, raf, last }

// Many-mode threshold (matches the design)
const MANY_THRESHOLD = 6;

const i18n = (typeof window !== 'undefined' && window.WV2N_I18N) ? window.WV2N_I18N : null;
function t(key, vars) {
  if (!i18n) return key;
  return i18n.t(key, vars);
}
function tHtml(key, vars) {
  if (!i18n) return t(key, vars);
  return i18n.tHtml(key, vars);
}

async function loadSettingsFromStorage() {
  settings = await chrome.storage.sync.get(['nasEndpoint', 'apiKey', 'uiLanguage', 'uiTheme', 'jobSort']);
  if (settings.jobSort && JOB_SORT_CYCLE.includes(settings.jobSort)) {
    jobSort = settings.jobSort;
  } else {
    // Migrate stale value (e.g., legacy 'time' mode) → default 'failed' so failed jobs sort first.
    jobSort = 'failed';
  }
  return settings;
}

function applyJobSortLabel() {
  const labelEl = document.getElementById('sortBtnLabel');
  const btn = document.getElementById('sortBtn');
  const key = `sort.${jobSort}`;
  if (labelEl) labelEl.textContent = t(key);
  if (btn) btn.setAttribute('title', t('sort.tooltip'));
}

async function cycleJobSort() {
  const idx = JOB_SORT_CYCLE.indexOf(jobSort);
  jobSort = JOB_SORT_CYCLE[(idx + 1) % JOB_SORT_CYCLE.length];
  applyJobSortLabel();
  // Force re-render with new order: clear list so renderJobs rebuilds in sort order.
  const listElement = document.getElementById('recentJobsList');
  if (listElement) listElement.innerHTML = '';
  renderJobs();
  try { await chrome.storage.sync.set({ jobSort }); } catch (_) {}
}

function applyUiLanguage() {
  if (i18n) {
    i18n.setLanguage((settings.uiLanguage || '').trim());
  }
  localizeStaticText();
}

function localizeStaticText() {
  const refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) refreshBtn.setAttribute('title', t('btn.refresh.title'));
  const settingsBtn = document.getElementById('settingsBtn');
  if (settingsBtn) settingsBtn.setAttribute('title', t('btn.settings.title'));
  const themeBtn = document.getElementById('themeToggleBtn');
  if (themeBtn) themeBtn.setAttribute('title', t('btn.theme.title'));

  const detectedTitle = document.getElementById('detectedVideosTitle');
  if (detectedTitle) detectedTitle.textContent = t('section.detectedVideos');
  const recentTitle = document.getElementById('recentDownloadsTitle');
  if (recentTitle) recentTitle.textContent = t('section.recentDownloads');

  const searchInput = document.getElementById('searchInput');
  if (searchInput) searchInput.setAttribute('placeholder', t('toolbar.filter'));

  applyJobSortLabel();

  const statusText = document.getElementById('statusText');
  if (statusText && (statusText.textContent === 'Checking...' || statusText.textContent === '')) {
    statusText.textContent = t('status.checking');
  }

  // Empty states (initial render of either list)
  const detectedList = document.getElementById('detectedUrlsList');
  if (detectedList && detectedList.querySelector('.empty-state') && detectedUrls.length === 0) {
    renderDetectedUrls();
  }
  const jobsList = document.getElementById('recentJobsList');
  if (jobsList && jobsList.querySelector('.empty-state') && jobs.length === 0) {
    renderJobs();
  }
}

// ---------- Theme ----------
function applyTheme(next) {
  theme = next === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', theme);
}

async function setTheme(next) {
  applyTheme(next);
  try {
    await chrome.storage.sync.set({ uiTheme: theme });
  } catch (_) { /* ignore */ }
}

// ---------- Init ----------
document.addEventListener('DOMContentLoaded', async () => {
  await loadSettingsFromStorage();
  applyTheme(settings.uiTheme || 'dark');
  applyUiLanguage();

  checkConnection();
  loadDetectedUrls();
  await loadRecentJobs();

  setupEventListeners();

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.action === 'detectedUrlsUpdated') {
      if (activeTabId != null && message.tabId === activeTabId) {
        loadDetectedUrls();
      }
    }
  });
});

function setupEventListeners() {
  document.getElementById('settingsBtn').addEventListener('click', openSettings);
  document.getElementById('refreshBtn').addEventListener('click', () => {
    checkConnection();
    loadDetectedUrls();
    loadRecentJobs();
  });

  const themeBtn = document.getElementById('themeToggleBtn');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      setTheme(theme === 'light' ? 'dark' : 'light');
    });
  }

  const searchInput = document.getElementById('searchInput');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      searchQuery = (e.target.value || '').toLowerCase();
      renderDetectedUrls({ keepPulse: false });
    });
  }

  const bulkBtn = document.getElementById('bulkBtn');
  if (bulkBtn) {
    bulkBtn.addEventListener('click', () => {
      // Send is now strictly "send what's selected". The Select-all toggle is
      // the only path to a bulk send, so an accidental tap can't fan out an
      // 8-job NAS submit.
      if (selected.size === 0) return;
      const items = visibleDetectedUrls();
      const target = items.filter(it => selected.has(it.url));
      bulkSend(target);
    });
  }

  const bulkSelectBtn = document.getElementById('bulkSelectBtn');
  if (bulkSelectBtn) {
    bulkSelectBtn.addEventListener('click', () => {
      const items = visibleDetectedUrls();
      if (items.length === 0) return;
      const allSelected = items.every(it => selected.has(it.url));
      if (allSelected) {
        // Deselect all visible
        for (const it of items) selected.delete(it.url);
      } else {
        // Select all visible
        for (const it of items) selected.add(it.url);
      }
      // Re-render so per-tile checkboxes / selected state visually update
      renderDetectedUrls({ keepPulse: false });
      updateBulkBar();
    });
  }

  const sortBtn = document.getElementById('sortBtn');
  if (sortBtn) {
    sortBtn.addEventListener('click', cycleJobSort);
  }
  applyJobSortLabel();

  chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.status === 'complete') {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0] && tabs[0].id === tabId) {
          loadDetectedUrls();
        }
      });
    }
  });

  chrome.tabs.onActivated.addListener(() => {
    loadDetectedUrls();
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === 'local' && changes.detectedUrls) {
      loadDetectedUrls();
    }
    if (areaName === 'sync') {
      const needsUiUpdate = !!changes.uiLanguage;
      const needsConnUpdate = !!changes.nasEndpoint || !!changes.apiKey;
      const needsThemeUpdate = !!changes.uiTheme;

      if (needsUiUpdate) {
        settings.uiLanguage = changes.uiLanguage.newValue || '';
        applyUiLanguage();
        renderDetectedUrls({ keepPulse: false });
        const listElement = document.getElementById('recentJobsList');
        if (listElement) listElement.innerHTML = '';
        renderJobs();
      }

      if (needsThemeUpdate) {
        applyTheme(changes.uiTheme.newValue || 'dark');
      }

      if (needsConnUpdate) {
        if (changes.nasEndpoint) settings.nasEndpoint = changes.nasEndpoint.newValue || '';
        if (changes.apiKey) settings.apiKey = changes.apiKey.newValue || '';
      }

      if (needsUiUpdate || needsConnUpdate) {
        checkConnection();
        loadRecentJobs();
      }
    }
  });
}

function openSettings() {
  chrome.runtime.openOptionsPage();
}

// ---------- Connection ----------
function connectionReasonFromResponse(response) {
  if (!response) return '';
  if (response.status === 401) return t('options.status.invalidApiKey');
  if (response.status === 404) return t('options.status.apiNotFound');
  return `HTTP ${response.status}${response.statusText ? `: ${response.statusText}` : ''}`;
}

function connectionReasonFromError(error) {
  if (!error) return '';
  if (error.name === 'AbortError') return t('error.timeout.type');
  const msg = (error && error.message) ? String(error.message) : String(error);
  if (msg.includes('Failed to fetch') || msg.includes('NetworkError')) {
    return t('options.status.cannotReach');
  }
  return msg;
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function setConnectionState(state, label, tooltip) {
  const el = document.getElementById('connectionStatus');
  const txt = document.getElementById('statusText');
  if (!el || !txt) return;
  el.classList.remove('connected', 'disconnected', 'checking');
  el.classList.add(state);
  el.title = tooltip || '';
  txt.textContent = label;
}

async function checkConnection() {
  await loadSettingsFromStorage();

  if (!settings.nasEndpoint || !settings.apiKey) {
    setConnectionState('disconnected', t('status.notConfigured'), '');
    return;
  }

  setConnectionState('checking', t('status.checking'), settings.nasEndpoint);

  try {
    const url = `${settings.nasEndpoint}/api/health`;
    const response = await fetchWithTimeout(url, {
      method: 'GET',
      headers: { 'Authorization': `Bearer ${settings.apiKey}` }
    }, 5000);

    if (response.ok) {
      setConnectionState('connected', shortHost(settings.nasEndpoint),
        `${settings.nasEndpoint}\n/api/health: OK`);
    } else {
      const reason = connectionReasonFromResponse(response);
      setConnectionState('disconnected',
        reason ? `${t('status.disconnected')} - ${reason}` : t('status.disconnected'),
        `${settings.nasEndpoint}\n/api/health: ${reason}`);
    }
  } catch (error) {
    const reason = connectionReasonFromError(error);
    setConnectionState('disconnected',
      reason ? `${t('status.disconnected')} - ${reason}` : t('status.disconnected'),
      `${settings.nasEndpoint}\n/api/health: ${reason || t('status.disconnected')}`);
  }
}

function shortHost(endpoint) {
  try {
    return new URL(endpoint).host;
  } catch (_) {
    return endpoint;
  }
}

// ---------- Detected URLs ----------
function loadDetectedUrls() {
  const seq = ++loadDetectedUrlsSeq;

  detectedUrls = [];
  renderDetectedUrls({ keepPulse: false });

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (seq !== loadDetectedUrlsSeq) return;

    activeTabId = (tabs && tabs[0]) ? tabs[0].id : null;
    chrome.runtime.sendMessage({ action: 'getDetectedUrls', tabId: activeTabId }, (response) => {
      if (seq !== loadDetectedUrlsSeq) return;

      if (chrome.runtime.lastError) {
        detectedUrls = [];
        renderDetectedUrls({ keepPulse: false });
        return;
      }

      detectedUrls = (response && response.urls) ? response.urls : [];
      renderDetectedUrls({ keepPulse: true });
    });
  });
}

function sortedDetectedUrls() {
  return detectedUrls.slice().sort((a, b) => {
    const aQ = getMaxQualityNumber(a && a.url);
    const bQ = getMaxQualityNumber(b && b.url);
    if (aQ !== bQ) return bQ - aQ;
    const aScore = Number(a && a.score) || 0;
    const bScore = Number(b && b.score) || 0;
    if (aScore !== bScore) return bScore - aScore;
    const aTs = Number(a && a.timestamp) || 0;
    const bTs = Number(b && b.timestamp) || 0;
    return bTs - aTs;
  });
}

function classifyVideoType(url) {
  const lower = String(url || '').toLowerCase();
  if (!lower) return 'VIDEO';

  // Direct extension match (most reliable).
  if (lower.includes('.m3u8')) return 'M3U8';
  if (lower.includes('.mpd')) return 'MPD';
  if (lower.includes('.mp4')) return 'MP4';
  if (lower.includes('.webm')) return 'WEBM';
  if (/\.ts(?:[?#/]|$)/.test(lower)) return 'TS';

  // Substring fallback (URLs may have format hints without dot).
  if (lower.includes('m3u8') || lower.includes('/hls/') || lower.includes('hls=')) return 'M3U8';
  if (lower.includes('mpd') || lower.includes('/dash/') || lower.includes('dash=')) return 'MPD';

  // Path keywords commonly used by HLS players.
  if (/\/(playlist|master|index)(?:\.|\?|$)/.test(lower)) return 'M3U8';
  if (/\/(manifest)(?:\.|\?|$)/.test(lower)) return 'MPD';

  // Query format hint.
  const m = /[?&](?:format|type|kind)=([a-z0-9]+)/.exec(lower);
  if (m) {
    const v = m[1];
    if (v === 'hls' || v === 'm3u8') return 'M3U8';
    if (v === 'dash' || v === 'mpd') return 'MPD';
    if (v === 'mp4') return 'MP4';
  }

  return 'VIDEO';
}

function deriveTitle(urlInfo, idx) {
  const videoType = (urlInfo.detectedFormat || classifyVideoType(urlInfo.url)).toUpperCase();
  // Prefer page title if NAS-side hasn't normalized; otherwise just videoType + idx.
  // Keep behavior consistent with previous "VIDEO #N" pattern.
  return `${videoType} #${idx + 1}`;
}

// Format → hue mapping. Jobs/tiles of the same format share a colour
// so the user can scan by type at a glance.
const FORMAT_HUE = {
  M3U8: 155,   // mint — HLS, most common
  MPD:  240,   // blue — DASH
  MP4:  30,    // orange — direct file
  TS:   280,   // purple — transport stream
  WEBM: 320,   // magenta
};

// Stable hue from string → fallback for unknown / non-URL inputs.
function hashHue(str) {
  let h = 0;
  const s = String(str || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % 360;
}

function thumbColorForUrl(url) {
  const fmt = classifyVideoType(url);
  const isLight = theme === 'light';
  if (FORMAT_HUE[fmt] != null) {
    const hue = FORMAT_HUE[fmt];
    return isLight ? `oklch(78% 0.08 ${hue})` : `oklch(35% 0.07 ${hue})`;
  }
  // Unknown format → neutral grey so it clearly reads as "unidentified".
  return isLight ? 'oklch(80% 0.005 250)' : 'oklch(32% 0.005 250)';
}

function topQualityLabel(url) {
  const qs = extractQualitiesFromUrl(url);
  if (!qs.length) return null;
  const top = qs[0]; // sorted desc
  if (top === '2160p') return '4K';
  return top;
}

function uniqueQualityLabels(items) {
  const set = new Set();
  for (const it of items) {
    const top = topQualityLabel(it.url);
    if (top) set.add(top);
  }
  // Stable order: 4K, 1440p, 1080p, 720p, others
  const order = ['4K', '1440p', '1080p', '720p', '540p', '480p', '360p', '240p'];
  return order.filter(q => set.has(q));
}

function visibleDetectedUrls() {
  let items = sortedDetectedUrls();
  if (qualityFilter !== 'all') {
    items = items.filter(it => topQualityLabel(it.url) === qualityFilter);
  }
  if (searchQuery) {
    items = items.filter(it => String(it.url || '').toLowerCase().includes(searchQuery));
  }
  return items;
}

function renderDetectedUrls(opts) {
  opts = opts || {};
  const listElement = document.getElementById('detectedUrlsList');
  if (!listElement) return;

  const total = detectedUrls.length;
  const isMany = total > MANY_THRESHOLD;

  // Pane ratio: dynamic based on detected count.
  //   0       → empty-state height only
  //   1-3     → 35% (recent dominates)
  //   4-6     → 50% (default split)
  //   7+      → 60% (more grid space)
  const body = document.querySelector('.body');
  if (body) {
    body.classList.toggle('detected-empty', total === 0);
    body.classList.toggle('detected-few',   total >= 1 && total <= 3);
    body.classList.toggle('detected-many',  total >= 7);
  }

  // Header count badge
  const countBadge = document.getElementById('detectedCountBadge');
  const countText = document.getElementById('detectedCountText');
  if (countBadge && countText) {
    if (total > 0) {
      countBadge.hidden = false;
      countText.textContent = t('header.onPage', { n: total });
    } else {
      countBadge.hidden = true;
    }
  }

  // Toolbar visibility + chips
  const toolbar = document.getElementById('toolbar');
  if (toolbar) toolbar.hidden = !isMany;
  if (isMany) renderQualityChips();

  // Grid density
  listElement.classList.toggle('dense', isMany);

  // Section count suffix
  const visible = visibleDetectedUrls();
  const suffix = document.getElementById('detectedCountSuffix');
  if (suffix) {
    let s = String(visible.length);
    if (qualityFilter !== 'all') s += ' · ' + qualityFilter;
    suffix.textContent = s;
  }

  // Empty state
  if (total === 0) {
    listElement.innerHTML = `
      <div class="empty-state">
        <p>${escapeHtml(t('empty.noVideos.title'))}</p>
        <p class="hint">${escapeHtml(t('empty.noVideos.hint'))}</p>
      </div>
    `;
    selected.clear();
    updateBulkBar();
    prevDetectedIds = new Set();
    return;
  }

  // Prune selected/sent entries that no longer exist
  const allIds = new Set(detectedUrls.map(d => d.url));
  for (const id of Array.from(selected)) {
    if (!allIds.has(id)) selected.delete(id);
  }
  for (const id of Array.from(sentUrls)) {
    if (!allIds.has(id)) sentUrls.delete(id);
  }

  // Determine which IDs are "new" since last render — they get pulse animation
  const seenIds = new Set(visible.map(it => it.url));
  const isNew = (url) => firstRenderDone && opts.keepPulse !== false && !prevDetectedIds.has(url);

  // Build tile HTML
  const html = visible.map((urlInfo, index) => {
    const url = urlInfo.url;
    const hasIp = containsIpAddress(url);
    const videoType = (urlInfo.detectedFormat || classifyVideoType(url)).toUpperCase();
    const isSel = selected.has(url);
    const isSent = sentUrls.has(url);
    const newClass = isNew(url) ? ' pulse' : '';
    const selClass = isSel ? ' selected' : '';
    const sentClass = isSent ? ' sent' : '';
    const interactClass = isMany ? ' selectable' : '';
    const title = deriveTitle(urlInfo, index);
    const tone = thumbColorForUrl(url);
    const top = topQualityLabel(url);
    const isNowPlaying = !!urlInfo.isNowPlaying || !!urlInfo.isLive;

    const thumbnail = urlInfo.thumbnail || null;
    return `
      <div class="tile${newClass}${selClass}${sentClass}${interactClass}"
           data-url="${escapeHtml(url)}"
           data-page="${escapeHtml(urlInfo.pageUrl || '')}"
           style="--idx:${index}">
        <div class="thumb-wrap">
          <div class="thumb" style="background:${tone}">
            ${thumbnail ? `<img class="thumb-img" src="${escapeHtml(thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
            <span class="thumb-label">${escapeHtml(videoType)}</span>
            ${top ? `<span class="thumb-quality">${escapeHtml(top)}</span>` : ''}
            ${isNowPlaying ? `
              <span class="thumb-live">
                <span class="live-dot"></span>
                <span class="live-text">LIVE</span>
              </span>
            ` : ''}
          </div>
          ${isMany ? `
            <span class="sel-dot" aria-hidden="true">
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                   stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 8.5l3.5 3.5L13 5"/>
              </svg>
            </span>
          ` : `
            <button class="send-btn" type="button"
                    title="${escapeHtml(isSent ? t('url.sentResend') : t('url.sendToNas'))}"
                    data-action="send"
                    data-url="${escapeHtml(url)}"
                    data-page="${escapeHtml(urlInfo.pageUrl || '')}">
              <span class="send-icon icon-arrow" aria-hidden="true">
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                     stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 8h10M9 4l4 4-4 4"/>
                </svg>
              </span>
              <span class="send-icon icon-check" aria-hidden="true">
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 8.5l3.5 3.5L13 5"/>
                </svg>
              </span>
            </button>
          `}
        </div>
        ${hasIp ? `
          <div class="ip-warn"><strong>${escapeHtml(t('url.ipWarning.title'))}</strong><br>${tHtml('url.ipWarning.body')}</div>
        ` : ''}
        <div class="tile-body">
          <div class="tile-title" title="${escapeHtml(url)}">${escapeHtml(title)}</div>
          <div class="tile-meta" title="${escapeHtml(url)}">${buildMetaLine(urlInfo)}</div>
        </div>
      </div>
    `;
  }).join('');

  listElement.innerHTML = html;

  // Tile interactions
  listElement.querySelectorAll('.tile').forEach(tile => {
    if (isMany) {
      tile.addEventListener('click', (e) => {
        // Allow clicks on internal buttons to pass through (we have none in many-mode)
        if (e.target.closest('button')) return;
        const url = tile.dataset.url;
        toggleSelect(url, tile);
      });
    }
  });

  listElement.querySelectorAll('[data-action="send"]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const url = btn.dataset.url;
      const pageUrl = btn.dataset.page;
      const tile = btn.closest('.tile');
      flyToNAS(tile, url, pageUrl);
    });
  });

  // Hide broken thumbnail images so the placeholder color/label shows through.
  listElement.querySelectorAll('.thumb-img').forEach(img => {
    img.addEventListener('error', () => img.remove(), { once: true });
  });

  // Strip pulse class after animation so future re-renders don't re-trigger it
  setTimeout(() => {
    listElement.querySelectorAll('.tile.pulse').forEach(el => el.classList.remove('pulse'));
  }, 2400);

  prevDetectedIds = seenIds;
  firstRenderDone = true;
  updateBulkBar();
}

function renderQualityChips() {
  const el = document.getElementById('qualityChips');
  if (!el) return;

  const qualities = uniqueQualityLabels(detectedUrls);
  const all = ['all', ...qualities];

  el.innerHTML = all.map(q => {
    const count = q === 'all' ? detectedUrls.length :
      detectedUrls.filter(d => topQualityLabel(d.url) === q).length;
    const active = qualityFilter === q ? ' active' : '';
    const label = q === 'all' ? t('chip.all') : q;
    const countMarkup = q === 'all' ? '' : `<span class="chip-count">${count}</span>`;
    return `<button class="chip${active}" data-q="${escapeHtml(q)}" type="button">${escapeHtml(label)}${countMarkup}</button>`;
  }).join('');

  el.querySelectorAll('.chip').forEach(c => {
    c.addEventListener('click', () => {
      qualityFilter = c.dataset.q;
      renderDetectedUrls({ keepPulse: false });
    });
  });
}

function toggleSelect(url, tileEl) {
  if (selected.has(url)) {
    selected.delete(url);
    if (tileEl) tileEl.classList.remove('selected');
  } else {
    selected.add(url);
    if (tileEl) tileEl.classList.add('selected');
  }
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('bulkBar');
  const line1 = document.getElementById('bulkLine1');
  const line2 = document.getElementById('bulkLine2');
  const btn = document.getElementById('bulkBtn');
  const btnText = document.getElementById('bulkBtnText');
  const selBtn = document.getElementById('bulkSelectBtn');
  const selBtnText = document.getElementById('bulkSelectBtnText');
  if (!bar) return;

  const total = detectedUrls.length;
  const isMany = total > MANY_THRESHOLD;
  bar.hidden = !isMany;
  if (!isMany) return;

  const selCount = selected.size;
  const visible = visibleDetectedUrls();
  const allVisibleSelected = visible.length > 0 && visible.every(it => selected.has(it.url));

  if (line1) {
    line1.textContent = selCount > 0
      ? t('bulk.selected', { n: selCount })
      : t('bulk.detected', { n: total });
  }
  if (line2) {
    if (qualityFilter !== 'all' && selCount === 0) {
      line2.textContent = `${visible.length} · ${qualityFilter}`;
    } else {
      line2.textContent = '';
    }
  }
  // Send button: only active when something is selected. No more "send all N"
  // shortcut — bulk send requires explicit Select-all click first.
  if (btn) btn.disabled = selCount === 0;
  if (btnText) {
    btnText.textContent = selCount > 0
      ? t('bulk.sendSelected', { n: selCount })
      : t('bulk.sendDisabled');
  }
  // Toggle: "Select all" → "Clear" once everything visible is selected.
  if (selBtnText) {
    selBtnText.textContent = allVisibleSelected
      ? t('bulk.clearAll')
      : t('bulk.selectAll', { n: visible.length });
  }
  if (selBtn) selBtn.disabled = visible.length === 0;
}

// ---------- Send to NAS (single + bulk + flight ghost) ----------
function flyToNAS(tileEl, url, pageUrl) {
  if (!tileEl) {
    sendToNAS(url, pageUrl);
    return;
  }
  const target = document.getElementById('recentHeader');
  const a = tileEl.getBoundingClientRect();
  const b = target ? target.getBoundingClientRect() : null;

  const ghost = document.createElement('div');
  ghost.className = 'flight-ghost';
  ghost.style.left = a.left + 'px';
  ghost.style.top = a.top + 'px';
  ghost.style.width = a.width + 'px';
  ghost.style.height = a.height + 'px';
  const tone = thumbColorForUrl(url);
  ghost.style.background = tone;
  document.body.appendChild(ghost);

  // Hide the original immediately for the shrink-out illusion.
  tileEl.classList.add('sending');

  if (b) {
    const dx = (b.left + 18) - a.left;
    const dy = (b.top + 14) - a.top;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        ghost.style.transform = `translate(${dx}px, ${dy}px) scale(0.15)`;
        ghost.style.opacity = '0';
      });
    });
  } else {
    ghost.style.opacity = '0';
  }

  setTimeout(() => {
    ghost.remove();
    if (tileEl && tileEl.parentNode) {
      tileEl.classList.remove('sending');
      tileEl.classList.add('sent');
    }
    sentUrls.add(url);
    selected.delete(url);
    updateBulkBar();
    sendToNAS(url, pageUrl);
  }, 700);
}

function bulkSend(items) {
  if (!items || items.length === 0) return;
  // Stagger the visual sends so each tile gets its own flight; cap concurrency.
  const list = items.slice();
  const step = Math.max(40, Math.min(120, Math.floor(800 / list.length)));
  list.forEach((it, i) => {
    setTimeout(() => {
      const tile = document.querySelector(`.tile[data-url="${cssEscape(it.url)}"]`);
      if (tile) {
        flyToNAS(tile, it.url, it.pageUrl);
      } else {
        sendToNAS(it.url, it.pageUrl);
      }
    }, i * step);
  });
  selected.clear();
  setTimeout(updateBulkBar, 50);
}

function cssEscape(s) {
  if (typeof CSS !== 'undefined' && CSS.escape) return CSS.escape(s);
  return String(s).replace(/["\\]/g, '\\$&');
}

async function sendToNAS(url, pageUrl) {
  await loadSettingsFromStorage();

  if (!settings.nasEndpoint || !settings.apiKey) {
    showToast(t('alert.configureFirst'));
    chrome.runtime.openOptionsPage();
    return;
  }

  try {
    // Don't pass the active tab's title — in a multi-tab session the active
    // tab may not be the tab this URL came from, leading to mismatched titles.
    // Background looks up the title that was captured when this URL was first
    // detected (getStoredPageTitle) and falls back to a placeholder if
    // missing. We still pass i18n-aware fallback so the language matches.
    chrome.runtime.sendMessage({
      action: 'sendToNAS',
      url: url,
      title: t('video.untitled'),
      pageUrl: pageUrl || ''
    });

    showToast(t('toast.sending'));
    setTimeout(loadRecentJobs, 2000);
  } catch (error) {
    console.error('Error:', error);
    showToast(t('toast.failedToSend'));
  }
}

// ---------- Jobs ----------
async function loadRecentJobs() {
  if (!settings.nasEndpoint || !settings.apiKey) return;

  try {
    const response = await fetch(`${settings.nasEndpoint}/api/jobs?limit=20`, {
      headers: { 'Authorization': `Bearer ${settings.apiKey}` }
    });

    if (response.ok) {
      jobs = await response.json();
      renderJobs();
    }
  } catch (error) {
    console.error('Failed to load jobs:', error);
  }
}

function renderJobs() {
  const listElement = document.getElementById('recentJobsList');
  if (!listElement) return;

  // Apply sort mode (time keeps NAS order; active/failed groups by status rank).
  const sortedJobs = sortJobs(jobs, jobSort);
  const currentJobIds = new Set(sortedJobs.map(j => String(j.id)));

  if (sortedJobs.length === 0) {
    if (!listElement.querySelector('.empty-state')) {
      listElement.innerHTML = `
        <div class="empty-state">
          <p>${escapeHtml(t('empty.noJobs.short'))}</p>
        </div>
      `;
    } else {
      // Update existing empty-state translation (in case language changed)
      const p = listElement.querySelector('.empty-state p');
      if (p) p.textContent = t('empty.noJobs.short');
    }
    // Stop tweens on missing jobs
    for (const id of Array.from(jobTweens.keys())) {
      if (!currentJobIds.has(id)) stopTween(id);
    }
    return;
  }

  const emptyState = listElement.querySelector('.empty-state');
  if (emptyState) emptyState.remove();

  sortedJobs.forEach((job, index) => {
    const jobId = String(job.id);
    let itemEl = document.getElementById(`job-${jobId}`);

    if (!itemEl) {
      itemEl = document.createElement('div');
      itemEl.className = 'job-row';
      itemEl.id = `job-${jobId}`;
      itemEl.dataset.status = job.status;
      itemEl.dataset.progress = job.progress;

      const nextSibling = listElement.children[index];
      if (nextSibling) listElement.insertBefore(itemEl, nextSibling);
      else listElement.appendChild(itemEl);

      itemEl.innerHTML = getJobInnerHtml(job);
      bindJobEvents(itemEl, jobId);
      // Initialize tween state to current progress (no animation on first paint)
      if (isActiveStatus(job.status)) {
        jobTweens.set(jobId, { shown: Number(job.progress) || 0, raf: 0 });
        applyRing(itemEl, jobId, Number(job.progress) || 0);
      }
    } else {
      const oldStatus = itemEl.dataset.status;
      itemEl.dataset.status = job.status;
      itemEl.dataset.progress = job.progress;

      if (shouldFullRender(oldStatus, job.status)) {
        itemEl.innerHTML = getJobInnerHtml(job);
        bindJobEvents(itemEl, jobId);
      } else {
        updateJobElement(itemEl, job);
      }

      const currentIdx = Array.from(listElement.children).indexOf(itemEl);
      if (currentIdx !== index) {
        const nextSibling = listElement.children[index];
        if (nextSibling) listElement.insertBefore(itemEl, nextSibling);
        else listElement.appendChild(itemEl);
      }

      // Smoothly tween the ring/bar to the new value
      if (isActiveStatus(job.status)) {
        startTween(itemEl, jobId, Number(job.progress) || 0);
      } else {
        stopTween(jobId);
      }
    }
  });

  // Remove vanished jobs
  Array.from(listElement.children).forEach(child => {
    if (child.id && child.id.startsWith('job-')) {
      const id = child.id.replace('job-', '');
      if (!currentJobIds.has(id)) {
        stopTween(id);
        child.remove();
      }
    }
  });
}

function isActiveStatus(s) {
  return s === 'downloading' || s === 'processing' || s === 'merging';
}

function getJobInnerHtml(job) {
  const canCancel = ['pending', 'downloading', 'processing'].includes(job.status);
  const showProgress = isActiveStatus(job.status);
  const isFailed = job.status === 'failed';
  const errorInfo = isFailed ? getErrorInfo(job.error_message) : null;
  const statusTooltip = (job.status === 'completed' && typeof job.duration === 'number')
    ? t('job.duration', { duration: formatDuration(job.duration) })
    : '';
  // Colour by source URL format (m3u8/mp4/mpd…) so jobs of the same
   // type share a colour — see FORMAT_HUE.
  const tone = thumbColorForUrl(job.url || job.title || String(job.id));
  const ringMarkup = showProgress ? `
    <div class="mini-ring" data-job-ring>
      <svg viewBox="0 0 32 32" width="32" height="32">
        <circle cx="16" cy="16" r="13" fill="none" stroke="var(--glass-border)" stroke-width="2.2"/>
        <circle data-ring-arc cx="16" cy="16" r="13" fill="none"
                stroke="${ringStrokeForStatus(job.status)}"
                stroke-width="2.2" stroke-linecap="round"
                stroke-dasharray="0 81.68" transform="rotate(-90 16 16)"/>
      </svg>
      <span class="ring-pct" data-ring-pct>${Math.round(job.progress || 0)}</span>
    </div>
  ` : '';

  return `
    <div class="job-thumb" style="background:${tone}"></div>
    <div class="job-body">
      <div class="job-title" title="${escapeHtml(job.title)}"${statusTooltip ? ` data-tip="${escapeHtml(statusTooltip)}"` : ''}>${escapeHtml(job.title)}</div>
      <div class="job-meta status-${escapeHtml(job.status)}">
        <span class="status-text" data-status-text>${escapeHtml(jobStatusLabel(job))}</span>
        ${job.status === 'downloading' && job.speed != null ? `<span class="meta-sep"> · </span><span data-speed>${escapeHtml(String(job.speed))} MB/s</span>` : ''}
        ${job.size ? `<span class="meta-sep"> · </span><span>${escapeHtml(String(job.size))}</span>` : ''}
        ${job.when ? `<span class="meta-sep"> · </span><span>${escapeHtml(String(job.when))}</span>` : ''}
      </div>
      ${showProgress ? `
        <div class="job-bar" data-job-bar>
          <div class="job-bar-fill" data-bar-fill style="width:${Number(job.progress)||0}%; background:${ringStrokeForStatus(job.status)}"></div>
          <div class="job-bar-shimmer" data-bar-shimmer style="left:${Math.max(0, (Number(job.progress)||0) - 12)}%"></div>
        </div>
      ` : ''}
      ${isFailed && errorInfo ? `
        <details class="error-details" data-job-id="${escapeHtml(String(job.id))}" ${expandedErrorIds.has(String(job.id)) ? 'open' : ''}>
          <summary class="error-summary">
            <span class="error-icon">!</span>
            <span class="error-type">${escapeHtml(errorInfo.type)}</span>
            <span class="error-expand-icon">▶</span>
          </summary>
          <div class="error-content">
            <div class="error-message">${escapeHtml(errorInfo.message)}</div>
            <div class="error-solution">
              <div class="error-solution-title">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 2a6 6 0 0 1 6 6c0 7-3 9-3 9h-6s-3-2-3-9a6 6 0 0 1 6-6Z"/>
                  <path d="M9 18h6"/><path d="M10 22h4"/>
                </svg>
                ${escapeHtml(t('job.solution'))}
              </div>
              <div>${errorInfo.solution}</div>
            </div>
          </div>
        </details>
      ` : ''}
    </div>
    ${showProgress ? ringMarkup : ''}
    ${canCancel ? `
      <button class="cancel-btn" type="button" data-cancel data-job-id="${escapeHtml(String(job.id))}" title="${escapeHtml(t('job.cancel.title'))}">
        <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 4l8 8M12 4l-8 8"/>
        </svg>
      </button>
    ` : ''}
  `;
}

function ringStrokeForStatus(status) {
  if (status === 'downloading') return 'var(--accent)';
  if (status === 'processing' || status === 'merging') return 'var(--warn)';
  if (status === 'failed') return 'var(--err)';
  return 'var(--text-mid)';
}

function jobStatusLabel(job) {
  const pct = Number(job.progress) || 0;
  if (job.status === 'downloading') return `${getStatusLabel(job.status)} ${pct.toFixed(2)}%`;
  if (job.status === 'processing' || job.status === 'merging') return `${getStatusLabel(job.status)} ${pct.toFixed(2)}%`;
  if (job.status === 'completed') return getStatusLabel(job.status);
  if (job.status === 'failed') return job.error_message ? getStatusLabel(job.status) : getStatusLabel(job.status);
  return getStatusLabel(job.status);
}

function shouldFullRender(oldStatus, newStatus) {
  const hasProgress = s => isActiveStatus(s);
  const isFailed = s => s === 'failed';
  if (oldStatus !== newStatus) {
    if (hasProgress(oldStatus) !== hasProgress(newStatus)) return true;
    if (isFailed(oldStatus) !== isFailed(newStatus)) return true;
    if (['pending', 'completed', 'cancelled'].includes(newStatus)) return true;
  }
  return false;
}

function updateJobElement(el, job) {
  const meta = el.querySelector('.job-meta');
  if (meta) {
    meta.className = `job-meta status-${job.status}`;
    const statusEl = meta.querySelector('[data-status-text]');
    if (statusEl) statusEl.textContent = jobStatusLabel(job);
    const speedEl = meta.querySelector('[data-speed]');
    if (speedEl && job.speed != null) speedEl.textContent = `${job.speed} MB/s`;
  }
}

function startTween(el, jobId, target) {
  const state = jobTweens.get(jobId) || { shown: target, raf: 0 };
  if (state.raf) cancelAnimationFrame(state.raf);
  const from = state.shown;
  const to = target;
  if (from === to) {
    applyRing(el, jobId, to);
    state.shown = to;
    jobTweens.set(jobId, state);
    return;
  }
  const start = performance.now();
  const dur = 600;
  const tick = (now) => {
    const k = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    const cur = from + (to - from) * eased;
    applyRing(el, jobId, cur);
    state.shown = cur;
    if (k < 1) {
      state.raf = requestAnimationFrame(tick);
      jobTweens.set(jobId, state);
    } else {
      state.raf = 0;
      state.shown = to;
      jobTweens.set(jobId, state);
    }
  };
  state.raf = requestAnimationFrame(tick);
  jobTweens.set(jobId, state);
}

function stopTween(jobId) {
  const s = jobTweens.get(jobId);
  if (s && s.raf) cancelAnimationFrame(s.raf);
  jobTweens.delete(jobId);
}

function applyRing(el, jobId, value) {
  if (!el) return;
  const arc = el.querySelector('[data-ring-arc]');
  const pct = el.querySelector('[data-ring-pct]');
  const fill = el.querySelector('[data-bar-fill]');
  const shimmer = el.querySelector('[data-bar-shimmer]');
  const C = 81.68; // 2 * pi * 13
  const len = (Math.max(0, Math.min(100, value)) / 100) * C;
  if (arc) arc.setAttribute('stroke-dasharray', `${len} ${C}`);
  if (pct) pct.textContent = String(Math.round(value));
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
  if (shimmer) shimmer.style.left = `${Math.max(0, Math.min(100, value) - 12)}%`;
}

function bindJobEvents(el, jobId) {
  const cancelBtn = el.querySelector('[data-cancel]');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => cancelJob(jobId));
  }
  const details = el.querySelector('.error-details');
  if (details) {
    details.addEventListener('toggle', (e) => {
      if (e.target.open) expandedErrorIds.add(jobId);
      else expandedErrorIds.delete(jobId);
    });
  }
}

async function cancelJob(jobId) {
  if (!settings.nasEndpoint || !settings.apiKey) {
    showToast(t('toast.nasNotConfigured'));
    return;
  }

  try {
    const response = await fetch(`${settings.nasEndpoint}/api/jobs/${jobId}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${settings.apiKey}` }
    });

    if (response.ok) {
      showToast(t('toast.jobCancelled'));
      await loadRecentJobs();
    } else {
      const error = await response.json();
      showToast(`${error.detail || t('toast.failedToCancel')}`);
    }
  } catch (error) {
    console.error('Error cancelling job:', error);
    showToast(t('toast.failedToCancel'));
  }
}

// ---------- Toast ----------
function showToast(message) {
  const toast = document.createElement('div');
  toast.className = 'wv-toast';
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2000);
}

// ---------- Helpers (preserved for tests) ----------
function truncateUrl(url, maxLength = 60) {
  if (url.length <= maxLength) return url;
  return url.substring(0, maxLength) + '...';
}

function truncateMid(s, max) {
  const str = String(s || '');
  if (str.length <= max) return str;
  const half = Math.floor((max - 1) / 2);
  return str.slice(0, half) + '…' + str.slice(str.length - (max - half - 1));
}

function urlHost(url) {
  try { return new URL(url).host; } catch (_) { return ''; }
}

// Meta line: `{duration|LIVE} · {host}` — gracefully drops parts that are missing.
function buildMetaLine(urlInfo) {
  const parts = [];
  if (urlInfo.isLive) {
    parts.push('LIVE');
  } else if (typeof urlInfo.duration === 'number' && urlInfo.duration > 0) {
    parts.push(formatDuration(urlInfo.duration));
  }
  const host = urlHost(urlInfo.url);
  if (host) parts.push(host);
  if (!parts.length) return escapeHtml(truncateMid(urlInfo.url, 28));
  return parts.map(p => `<span>${escapeHtml(p)}</span>`)
    .join('<span class="meta-sep"> · </span>');
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

  return Array.from(found).sort((a, b) => b - a).map(n => `${n}p`);
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

function getStatusLabel(status) {
  switch (status) {
    case 'pending': return t('jobStatus.pending');
    case 'downloading': return t('jobStatus.downloading');
    case 'processing': return t('jobStatus.processing');
    case 'merging': return t('jobStatus.processing');
    case 'completed': return t('jobStatus.completed');
    case 'failed': return t('jobStatus.failed');
    case 'cancelled': return t('jobStatus.cancelled');
    default: return status;
  }
}

function getErrorInfo(errorMessage) {
  if (!errorMessage) {
    return {
      type: t('error.unknown.type'),
      message: t('error.unknown.message'),
      solution: t('error.unknown.solution')
    };
  }
  const msg = errorMessage.toLowerCase();
  // CDN signed-token expiry: short-lived ?auth=...&exp=... URLs that 401 mid-download.
  // Worker raises this either via the abort guard ("HTTP 401/403/474 errors") or the
  // sub-threshold success ratio guard ("Likely expired CDN auth token"). Match before
  // the generic 403 branch so 401-dominated aborts don't get classified as IP auth.
  if (
    msg.includes('401') ||
    msg.includes('expired cdn auth token') ||
    msg.includes('url/token expired') ||
    (msg.includes('download aborted') && msg.includes('only ') && msg.includes('segments succeeded'))
  ) {
    return { type: t('error.tokenExpired.type'), message: errorMessage, solution: t('error.tokenExpired.solution') };
  }
  if (msg.includes('403') || msg.includes('forbidden')) {
    return { type: t('error.403.type'), message: errorMessage, solution: t('error.403.solution') };
  }
  if (msg.includes('404') || msg.includes('not found')) {
    return { type: t('error.404.type'), message: errorMessage, solution: t('error.404.solution') };
  }
  if (msg.includes('timeout') || msg.includes('timed out')) {
    return { type: t('error.timeout.type'), message: errorMessage, solution: t('error.timeout.solution') };
  }
  if (msg.includes('ssl') || msg.includes('certificate') || msg.includes('tls')) {
    return { type: t('error.ssl.type'), message: errorMessage, solution: t('error.ssl.solution') };
  }
  if (msg.includes('connection') || msg.includes('network') || msg.includes('unreachable')) {
    return { type: t('error.connection.type'), message: errorMessage, solution: t('error.connection.solution') };
  }
  if (msg.includes('no segments') || msg.includes('empty playlist')) {
    return { type: t('error.invalidPlaylist.type'), message: errorMessage, solution: t('error.invalidPlaylist.solution') };
  }
  return { type: t('error.generic.type'), message: errorMessage, solution: t('error.generic.solution') };
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = s % 60;
  const mm = String(minutes).padStart(2, '0');
  const ss = String(seconds).padStart(2, '0');
  if (hours > 0) {
    const hh = String(hours).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  }
  return `${mm}:${ss}`;
}

function containsIpAddress(url) {
  const ipv4QueryPattern = /[?&]ip=(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/;
  return ipv4QueryPattern.test(url);
}

// Auto-refresh jobs every 5 seconds
setInterval(loadRecentJobs, 5000);
