// Sidepanel Script for WebVideo2NAS — Thumbnail Grid design.
// Adapts from few (2-up + per-tile send) to many (3-up + multi-select + bulk bar).
// UX animations: tile enter (staggered), pulse on new detection, hover lift,
// flight ghost on send, checkbox bounce, bulk bar slide, smooth ring tween.

let settings = {};
let detectedUrls = [];
let deepHits = [];
let jobs = [];
// Browser-side jobs: live segment-upload progress pushed from the
// offscreen document via the SW (BROWSER_JOB_PROGRESS → action:
// 'browserJobProgress'). The NAS API doesn't track upload-phase
// progress for browser-side jobs, so we keep our own map here and
// re-apply it after every loadRecentJobs() poll (which would
// otherwise overwrite job.progress with the API's stale 0).
const liveBrowserProgress = new Map();  // jobId(string) → { done, total, percent }
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

// browser-side states sit in the same priority class as their
// NAS-direct counterparts so they share rank with active in-flight
// work instead of falling through to the `?? 99` fallback (which put
// them at the bottom of the list mid-upload). browser_uploading
// mirrors downloading (actively transferring bytes); browser_finalizing
// mirrors merging (server is muxing); browser_pending mirrors pending.
const STATUS_RANK_ACTIVE = {
  downloading: 0, browser_uploading: 0,
  processing: 1, merging: 1, browser_finalizing: 1,
  pending: 2, browser_pending: 2,
  completed: 3,
  failed: 4, cancelled: 5,
};
const STATUS_RANK_FAILED = {
  failed: 0,
  downloading: 1, processing: 1, merging: 1,
  browser_uploading: 1, browser_finalizing: 1,
  pending: 2, browser_pending: 2,
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
  settings = await chrome.storage.sync.get([
    'nasEndpoint', 'apiKey', 'uiLanguage', 'uiTheme', 'jobSort',
    'hiddenMode', 'hiddenModeUrlTemplate',
    'trustedCdnSuffixes',
    // useBrowserSide drives the play-first lock, the bulk-skip
    // filter, and the mode-badge in the header. Was missing
    // from this list — references treated it as undefined, which
    // happens to coincide with the default-on semantics, so the
    // bug stayed dormant unless the user turned the option off
    // (in which case sidepanel kept hiding non-playing tiles
    // even though NAS-direct doesn't need play-first).
    'useBrowserSide',
  ]);
  if (settings.jobSort && JOB_SORT_CYCLE.includes(settings.jobSort)) {
    jobSort = settings.jobSort;
  } else {
    // Migrate stale value (e.g., legacy 'time' mode) → default 'failed' so failed jobs sort first.
    jobSort = 'failed';
  }
  applyHiddenModeVisibility();
  return settings;
}


// Parse the comma- or newline-separated trustedCdnSuffixes input
// into a clean string[]. The strict matcher in background.js trims,
// lowercases, and strips leading dots at match time, so this is
// mainly for storage hygiene + dedup, plus pulling the hostname out
// when the user pastes a full URL.
function parseTrustedCdnSuffixesInput(raw) {
  if (typeof raw !== 'string') return [];
  const seen = new Set();
  for (const part of raw.split(/[,\n]+/)) {
    let s = part.trim();
    if (!s) continue;
    if (s.includes('://')) {
      try { s = new URL(s).hostname; } catch (_) { /* keep raw on parse fail */ }
    }
    s = s.replace(/^\.+/, '').toLowerCase();
    if (s) seen.add(s);
  }
  return Array.from(seen);
}

function applyModeBadge() {
  const badge = document.getElementById('modeBadge');
  if (!badge) return;
  const on = !settings || settings.useBrowserSide !== false;
  badge.hidden = !on;
  // Title (tooltip) stays static — set in HTML — so no need to
  // localize per render. The visible text key is fixed and short
  // enough that running it through t() each time is a no-op
  // unless the user changes locale.
  const txt = document.getElementById('modeBadgeText');
  if (txt) txt.textContent = t('header.browserMode');
}

function populateTrustedCdnInput() {
  const input = document.getElementById('trustedCdnSuffixes');
  if (!input) return;
  const list = settings && settings.trustedCdnSuffixes;
  input.value = Array.isArray(list) ? list.join(', ') : '';
}

function updateTrustedCdnCount() {
  const el = document.getElementById('trustedCdnCount');
  if (!el) return;
  const list = settings && settings.trustedCdnSuffixes;
  const n = Array.isArray(list) ? list.length : 0;
  el.textContent = n > 0 ? `(${n})` : '';
}

async function saveTrustedCdnInput() {
  const input = document.getElementById('trustedCdnSuffixes');
  if (!input) return;
  const trustedCdnSuffixes = parseTrustedCdnSuffixesInput(input.value || '');
  settings.trustedCdnSuffixes = trustedCdnSuffixes;
  await chrome.storage.sync.set({ trustedCdnSuffixes });
  updateTrustedCdnCount();
  // Re-render detected tiles so per-tile trust badges update.
  if (typeof renderDetectedUrls === 'function') {
    renderDetectedUrls({ keepPulse: false });
  }
}

// Derive the exact hostname from a URL for the per-tile "+" trust
// button. We intentionally store the full host rather than guessing
// an eTLD+1 from the last two labels: without a Public Suffix List,
// hosts like `cdn.example.co.uk` would collapse to the dangerously
// broad `co.uk`. The matcher still treats entries as suffixes, so a
// user can manually widen the trust boundary in the textbox when they
// really mean to trust a whole CDN suffix.
//
// Returns null when "+" trust would be meaningless: malformed URL,
// IP literal (gate rejects regardless of trust list), single-label
// host (e.g. localhost — also rejected by gate).
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
  } catch (_) {
    return null;
  }
  if (!host) return null;
  host = host.toLowerCase().replace(/^\.+|\.+$/g, '');
  // IP literal (v4 or bracketed v6) — gate rejects at IP-classification
  // step, no trust list change can unblock these.
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return null;
  if (host.startsWith('[')) return null;
  const parts = host.split('.').filter(Boolean);
  if (parts.length < 2) return null;
  // Conservative DNS-ish shape check. URL.hostname already punycodes
  // IDNs, so xn-- labels are fine here.
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host)) return null;
  return host;
}

// Pure JS twin of background.js's `_wv2nasMatchesTrustedCdnSuffix`.
// Sidepanel can't call into the SW directly, so we duplicate the
// strict-suffix matcher here for "is this host already covered?"
// rendering decisions. Both helpers MUST stay in lockstep — same
// rules (exact-or-dotted-suffix, lowercase, leading-dot tolerant).
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

async function trustCdnFromUrl(url) {
  const suffix = deriveTrustedCdnSuffix(url);
  if (!suffix) return false;
  let host;
  try { host = new URL(url).hostname.toLowerCase(); } catch (_) { return false; }
  const current = Array.isArray(settings.trustedCdnSuffixes)
    ? settings.trustedCdnSuffixes.slice()
    : [];
  if (hostMatchesAnyTrustedSuffix(host, current)) return false; // already covered
  current.push(suffix);
  settings.trustedCdnSuffixes = current;
  await chrome.storage.sync.set({ trustedCdnSuffixes: current });
  populateTrustedCdnInput();
  updateTrustedCdnCount();
  return true;
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
  populateTrustedCdnInput();
  updateTrustedCdnCount();
  applyModeBadge();

  checkConnection();
  loadDetectedUrls();
  await loadRecentJobs();

  setupEventListeners();

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.action === 'detectedUrlsUpdated') {
      if (activeTabId != null && message.tabId === activeTabId) {
        loadDetectedUrls();
      }
    } else if (message && message.action === 'browserJobProgress') {
      handleBrowserJobProgress(message);
    }
  });
});

function handleBrowserJobProgress(message) {
  const idStr = String(message.jobId || '');
  if (!idStr) return;
  const total = Number(message.total) || 0;
  const done = Number(message.done) || 0;
  const percent = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  liveBrowserProgress.set(idStr, { done, total, percent });

  const job = jobs.find((j) => String(j.id) === idStr);
  if (!job) return;
  // Promote browser_pending → browser_uploading once the first
  // segment lands. The API status update may lag; the user-visible
  // progress should kick in on the first event.
  if (job.status === 'browser_pending') job.status = 'browser_uploading';
  job.progress = percent;

  const itemEl = document.getElementById(`job-${idStr}`);
  if (!itemEl) return;
  const oldStatus = itemEl.dataset.status;
  itemEl.dataset.status = job.status;
  itemEl.dataset.progress = String(job.progress);
  if (shouldFullRender(oldStatus, job.status)) {
    itemEl.innerHTML = getJobInnerHtml(job);
    bindJobEvents(itemEl, idStr);
  } else {
    updateJobElement(itemEl, job);
  }
  startTween(itemEl, idStr, percent);
}

function setupEventListeners() {
  document.getElementById('settingsBtn').addEventListener('click', openSettings);
  document.getElementById('refreshBtn').addEventListener('click', () => {
    checkConnection();
    loadDetectedUrls();
    loadRecentJobs();
  });

  // Trusted CDN suffixes — save on blur (avoid spamming chrome.storage.sync
  // writes per keystroke; sync has per-item byte quota and write rate limits).
  const trustedCdnInput = document.getElementById('trustedCdnSuffixes');
  if (trustedCdnInput) {
    trustedCdnInput.addEventListener('blur', () => {
      saveTrustedCdnInput().catch((e) =>
        console.warn('[wv2nas] failed to persist trustedCdnSuffixes:', e)
      );
    });
  }

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

  const avSubmitBtn = document.getElementById('avSubmitBtn');
  const avInput = document.getElementById('avCodeInput');
  if (avSubmitBtn && avInput) {
    avSubmitBtn.addEventListener('click', () => submitAvTask(avInput.value));
    avInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitAvTask(avInput.value);
      }
    });
  }

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
        applyModeBadge();   // re-localize "BROWSER" label
        renderDetectedUrls({ keepPulse: false });
        const listElement = document.getElementById('recentJobsList');
        if (listElement) listElement.innerHTML = '';
        renderJobs();
      }

      if (changes.useBrowserSide) {
        // Toggling browser-side flips the play-first hide AND the
        // mode badge. Update settings cache, badge, and re-render
        // detected tiles so previously-hidden ones (or vice versa)
        // pop in/out without waiting for the next detection event.
        settings.useBrowserSide = changes.useBrowserSide.newValue;
        applyModeBadge();
        renderDetectedUrls({ keepPulse: false });
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

      if (changes.hiddenMode || changes.hiddenModeUrlTemplate) {
        if (changes.hiddenMode) settings.hiddenMode = changes.hiddenMode.newValue;
        if (changes.hiddenModeUrlTemplate) settings.hiddenModeUrlTemplate = changes.hiddenModeUrlTemplate.newValue;
        applyHiddenModeVisibility();
      }

      if (changes.trustedCdnSuffixes) {
        settings.trustedCdnSuffixes = changes.trustedCdnSuffixes.newValue;
        updateTrustedCdnCount();
        // Don't clobber a field the user is currently editing.
        const input = document.getElementById('trustedCdnSuffixes');
        if (input && document.activeElement !== input) {
          populateTrustedCdnInput();
        }
        // Refresh tile trust badges (a "+" elsewhere may have just
        // covered some of the visible tiles).
        if (typeof renderDetectedUrls === 'function') {
          renderDetectedUrls({ keepPulse: false });
        }
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

  // Don't clear-and-render before the async tabs.query/sendMessage
  // round-trip — that path made the grid flash to the empty state
  // EVERY time a new URL was detected (background notifies us at
  // most once a second, but the user still saw a brief blank gap).
  // Only clear when the active tab actually changed; for same-tab
  // updates keep the existing list visible and let renderDetectedUrls
  // do an in-place swap when the response lands.
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (seq !== loadDetectedUrlsSeq) return;

    const newTabId = (tabs && tabs[0]) ? tabs[0].id : null;
    const tabChanged = newTabId !== activeTabId;
    activeTabId = newTabId;

    if (tabChanged) {
      detectedUrls = [];
      deepHits = [];
      renderDetectedUrls({ keepPulse: false });
    }

    chrome.runtime.sendMessage({ action: 'getDetectedUrls', tabId: activeTabId }, (response) => {
      if (seq !== loadDetectedUrlsSeq) return;

      if (chrome.runtime.lastError) {
        detectedUrls = [];
        renderDetectedUrls({ keepPulse: false });
        return;
      }

      detectedUrls = (response && response.urls) ? response.urls : [];
      deepHits = (response && Array.isArray(response.deepHits)) ? response.deepHits : [];
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
  if (lower.includes('.mov')) return 'MOV';
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
  MOV:  15,    // warm orange — direct QuickTime file (close to MP4 since both are progressive single-file)
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

function normalizeQualityFilter() {
  if (qualityFilter === 'all') return;
  const qualities = uniqueQualityLabels(detectedUrls);
  if (!qualities.includes(qualityFilter)) {
    qualityFilter = 'all';
  }
}

// Browser-side play-first gate. HLS / DASH downloads need the
// browser session's just-issued (often IP-bound, expiring-in-
// minutes) token; the page's player typically only requests it
// at the moment it calls play(). Sending before that gives the
// extension a stale URL and the segments 403. Doesn't apply to
// MP4 (NAS-direct, the URL is the auth material) or when the
// user has explicitly disabled useBrowserSide.
function urlInfoRequiresPlayFirst(urlInfo) {
  if (!urlInfo || !urlInfo.url) return false;
  const browserSideOn = !settings || settings.useBrowserSide !== false;
  if (!browserSideOn) return false;
  const fmt = (urlInfo.detectedFormat || classifyVideoType(urlInfo.url) || '').toUpperCase();
  if (fmt !== 'M3U8' && fmt !== 'MPD') return false;
  if (urlInfo.playbackObserved) return false;
  const isNowPlaying = !!urlInfo.isNowPlaying || !!urlInfo.isLive;
  return !isNowPlaying;
}

function visibleDetectedUrls() {
  normalizeQualityFilter();
  let items = sortedDetectedUrls();
  if (qualityFilter !== 'all') {
    items = items.filter(it => topQualityLabel(it.url) === qualityFilter);
  }
  if (searchQuery) {
    items = items.filter(it => String(it.url || '').toLowerCase().includes(searchQuery));
  }
  // Keep play-first-gated HLS/DASH candidates visible. The send action
  // stays locked until playback starts, but hiding the tiles made the UI
  // look contradictory ("4 on this page" while the section showed 0).
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
    const hasDeepHits = Array.isArray(deepHits) && deepHits.length > 0;
    listElement.innerHTML = `
      <div class="empty-state">
        <p>${escapeHtml(t(hasDeepHits ? 'empty.deepDetected.title' : 'empty.noVideos.title'))}</p>
        <p class="hint">${escapeHtml(t(hasDeepHits ? 'empty.deepDetected.hint' : 'empty.noVideos.hint'))}</p>
      </div>
    `;
    selected.clear();
    updateBulkBar();
    prevDetectedIds = new Set();
    return;
  }

  // total > 0 but `visible` is empty — search/quality filter ate
  // everything. Leave the grid blank; the toolbar chip + search box
  // show why.
  if (visible.length === 0) {
    listElement.innerHTML = '';
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

    // Browser-side play-first gate: keep the candidate visible, but
    // lock sending until the page's player has started and issued the
    // fresh per-session token.
    const requiresPlayFirst = urlInfoRequiresPlayFirst(urlInfo);
    const playFirstClass = requiresPlayFirst ? ' play-first-blocked' : '';

    const thumbnail = urlInfo.thumbnail || null;
    // Trust-CDN button state. Hide entirely when the URL host is an
    // IP literal / single-label / unparseable (gate rejects regardless,
    // so a "trust" action would do nothing useful).
    const trustSuffix = deriveTrustedCdnSuffix(url);
    let trustHostLower = null;
    if (trustSuffix) {
      try { trustHostLower = new URL(url).hostname.toLowerCase(); } catch (_) {}
    }
    const trustAlreadyCovered = trustHostLower && hostMatchesAnyTrustedSuffix(
      trustHostLower, settings && settings.trustedCdnSuffixes,
    );
    return `
      <div class="tile${newClass}${selClass}${sentClass}${interactClass}${playFirstClass}"
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
            ${isMany && requiresPlayFirst ? `
              <span class="thumb-play-first"
                    title="${escapeHtml(t('url.playFirst.tooltip'))}"
                    aria-label="${escapeHtml(t('url.playFirst.tooltip'))}">
                <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">
                  <path d="M5 3l9 5-9 5V3z" fill="currentColor"/>
                </svg>
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
            <button class="send-btn${requiresPlayFirst ? ' play-first-blocked' : ''}" type="button"
                    title="${escapeHtml(requiresPlayFirst
                      ? t('url.playFirst.tooltip')
                      : (isSent ? t('url.sentResend') : t('url.sendToNas')))}"
                    ${requiresPlayFirst ? 'disabled aria-disabled="true"' : 'data-action="send"'}
                    data-url="${escapeHtml(url)}"
                    data-page="${escapeHtml(urlInfo.pageUrl || '')}">
              <span class="send-icon icon-arrow" aria-hidden="true">
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                     stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  ${requiresPlayFirst
                    /* Play-triangle icon when blocked, so the disabled state
                       reads as "press play first" instead of a generic ban. */
                    ? '<path d="M5 3l9 5-9 5V3z" fill="currentColor" stroke="none"/>'
                    : '<path d="M3 8h10M9 4l4 4-4 4"/>'}
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
          ${trustSuffix ? `
            <button class="trust-cdn-btn${trustAlreadyCovered ? ' is-trusted' : ''}"
                    type="button"
                    ${trustAlreadyCovered ? 'disabled aria-pressed="true"' : 'data-action="trust-cdn"'}
                    data-url="${escapeHtml(url)}"
                    data-suffix="${escapeHtml(trustSuffix)}"
                    aria-label="${escapeHtml(trustAlreadyCovered
                      ? `${trustSuffix} is already in trusted CDNs`
                      : `Add ${trustSuffix} to trusted CDNs`)}"
                    title="${escapeHtml(trustAlreadyCovered
                      ? `trusted: ${trustSuffix}`
                      : `+ trust ${trustSuffix} (browser-side cross-site)`)}">
              ${trustAlreadyCovered ? '✓' : '+'}
            </button>
          ` : ''}
        </div>
        ${hasIp ? `
          <details class="ip-warn">
            <summary class="ip-warn-summary">
              <span class="ip-warn-icon" aria-hidden="true">!</span>
              <span class="ip-warn-title">${escapeHtml(t('url.ipWarning.title'))}</span>
              <span class="ip-warn-expand" aria-hidden="true">▶</span>
            </summary>
            <div class="ip-warn-body">${tHtml('url.ipWarning.body')}</div>
          </details>
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

  listElement.querySelectorAll('[data-action="trust-cdn"]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const url = btn.dataset.url;
      if (!url) return;
      const added = await trustCdnFromUrl(url);
      if (added) {
        // Re-render so the clicked tile (and any siblings on the same
        // suffix) flip to the "already trusted" state.
        renderDetectedUrls({ keepPulse: false });
      }
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
  // Fire the actual NAS submission FIRST, before any visuals. Previously
  // sendToNAS lived inside a 700 ms setTimeout (the fly-ghost animation),
  // which created a window where the request could be lost: if the user
  // closed the sidepanel during the animation, Chrome killed the JS context
  // and the queued setTimeout never fired → silent drop. Same for the
  // bulkSend path where the LAST tile's real send was ~1.4 s after the
  // click. Fire-then-animate guarantees the request is in flight by the
  // time the animation even starts. Bookkeeping (sentUrls / selected) is
  // also moved up so a `loadDetectedUrls()` triggered by a new
  // background-detected URL during the animation re-renders the tile in
  // the correct .sent state instead of as a fresh untouched tile.
  sentUrls.add(url);
  selected.delete(url);
  updateBulkBar();
  sendToNAS(url, pageUrl);

  if (!tileEl) return;

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
  }, 700);
}

function bulkSend(items) {
  if (!items || items.length === 0) return;
  // Filter out HLS / DASH items that haven't been played yet — same
  // gate as the per-tile send button. Many-mode users can still
  // SELECT them (so the count badge reflects everything), but the
  // bulk send skips the not-ready ones and toasts how many were
  // skipped so the user knows to press play and retry.
  const ready = [];
  let skipped = 0;
  for (const it of items) {
    if (urlInfoRequiresPlayFirst(it)) skipped += 1;
    else ready.push(it);
  }
  if (skipped > 0) {
    showToast(t('url.playFirst.bulkSkipped', { n: skipped }));
  }
  if (ready.length === 0) {
    selected.clear();
    setTimeout(updateBulkBar, 50);
    return;
  }
  // Stagger the visual sends so each tile gets its own flight; cap concurrency.
  const list = ready.slice();
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

// Send a message to the background SW with retry on transient MV3 errors.
// "Receiving end does not exist" → SW listener not yet registered (cold start
// race after Chrome unloads the SW). "The message port closed before a
// response was received" → SW died mid-handler. Both are recoverable by
// resending after a brief delay; without retry, bulk sends from multiple
// tabs silently dropped 1–2 of N requests because the first message arrived
// during SW boot.
async function sendMessageWithRetry(payload, maxAttempts = 4) {
  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await chrome.runtime.sendMessage(payload);
    } catch (err) {
      lastErr = err;
      const msg = (err && err.message) || String(err);
      const transient =
        msg.includes('Receiving end does not exist') ||
        msg.includes('message port closed') ||
        msg.includes('Extension context invalidated');
      if (!transient || attempt === maxAttempts) throw err;
      // Exponential backoff: 50, 150, 350 ms — total ≤ 550 ms before giving up.
      await new Promise(r => setTimeout(r, 50 * (2 ** (attempt - 1)) + Math.floor(Math.random() * 50)));
    }
  }
  throw lastErr;
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
    //
    // tabId anchors the captured-header substitution to this exact tab.
    // Without it, sending from tab B/C in a same-site multi-tab session
    // could rewrite the URL to tab A's video (origin-prefix scoring leak).
    await sendMessageWithRetry({
      action: 'sendToNAS',
      url: url,
      title: t('video.untitled'),
      pageUrl: pageUrl || '',
      tabId: activeTabId
    });

    showToast(t('toast.sending'));
    setTimeout(loadRecentJobs, 2000);
  } catch (error) {
    console.error('sendToNAS message failed after retries:', error);
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
      // The API returns progress=0 for browser-side uploads (it doesn't
      // track that phase). Re-apply the live counter we kept from
      // BROWSER_JOB_PROGRESS pushes so the just-polled `jobs` doesn't
      // visually snap back to 0% mid-upload. Drop entries for jobs no
      // longer in the uploading state to bound the map.
      const livePresentIds = new Set();
      for (const job of jobs) {
        const idStr = String(job.id);
        if (job.status === 'browser_uploading') {
          const live = liveBrowserProgress.get(idStr);
          if (live) {
            job.progress = live.percent;
            livePresentIds.add(idStr);
          }
        }
      }
      for (const id of Array.from(liveBrowserProgress.keys())) {
        if (!livePresentIds.has(id)) liveBrowserProgress.delete(id);
      }
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

// Codex review (P3): the v2.5 browser-side states (browser_pending,
// browser_uploading, browser_finalizing) are ACTIVE for display
// purposes — they hold staging disk and progress towards completion,
// so the sidepanel must render them as in-flight rather than as an
// "unknown inactive status".
//
// Codex adversarial-review (medium): all three browser_* states are
// ALSO cancellable. browser_pending: DELETE handles CAS + staging
// cleanup atomically (extension never started uploading). browser_
// uploading: cancelJob() fires CANCEL_BROWSER_JOB to offscreen first
// so the AbortController halts in-flight PUTs, then DELETE flips the
// row and rmtrees staging. browser_finalizing: brief window before the
// API flips to 'pending'; treated the same as pending by DELETE. This
// closes the gap where a long HLS/DASH browser-side job had no user-
// visible stop path even while consuming bandwidth + staging quota.
function isActiveStatus(s) {
  return (
    s === 'downloading'
    || s === 'processing'
    || s === 'merging'
    || s === 'browser_pending'
    || s === 'browser_uploading'
    || s === 'browser_finalizing'
  );
}

function getJobInnerHtml(job) {
  const canCancel = [
    'pending', 'downloading', 'processing',
    'browser_pending', 'browser_uploading', 'browser_finalizing',
  ].includes(job.status);
  const showProgress = isActiveStatus(job.status);
  const isFailed = job.status === 'failed';
  const errorInfo = isFailed ? getErrorInfo(job.error_message) : null;
  // Suspect = worker (or backfill) flagged the merged file as probably-wrong
  // (actual duration << declared, or implausible bitrate). Only relevant
  // for completed jobs, and only useful when we have a source_page to
  // re-open in the browser so the user can capture a fresh m3u8 token.
  const isSuspect = job.status === 'completed' && !!job.suspect_reason;
  // Hotlink-fail = worker aborted mid-download because the CDN started
  // serving anti-hotlink PNGs (token expired / session invalidated). Same
  // recovery as suspect: re-open source_page so the extension grabs fresh
  // m3u8/segment tokens, then user resends. The error_message above
  // already shows the failure reason, so the suspect-block here just
  // surfaces the action button.
  const isHotlinkFail = isFailed && !!job.error_message && (() => {
    const m = job.error_message.toLowerCase();
    return m.includes('anti-hotlinking') || m.includes('download aborted');
  })();
  const canRefetch = !!job.source_page && (isSuspect || isHotlinkFail);
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
      <div class="job-title" title="${escapeHtml(job.title)}"${statusTooltip ? ` data-tip="${escapeHtml(statusTooltip)}"` : ''}>${
        job.mode === 'browser' ? '<span class="mode-badge mode-browser" title="Downloaded in browser session (v2.5)">browser</span>' : ''
      }${escapeHtml(job.title)}</div>
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
      ${(isSuspect || isHotlinkFail) ? `
        <div class="suspect-block">
          <div class="suspect-summary">
            <span class="suspect-icon" aria-hidden="true">!</span>
            <span class="suspect-label">${escapeHtml(t(isHotlinkFail ? 'suspect.label.refetch' : 'suspect.label'))}</span>
          </div>
          ${isSuspect ? `<div class="suspect-reason">${escapeHtml(job.suspect_reason)}</div>` : ''}
          ${canRefetch ? `
            <button class="suspect-refetch-btn" type="button"
                    data-refetch
                    data-job-id="${escapeHtml(String(job.id))}"
                    data-source-page="${escapeHtml(job.source_page)}"
                    title="${escapeHtml(t('suspect.refetch.title'))}">
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 8a5 5 0 0 1 9-3l1.5 1.5"/>
                <path d="M13 3v3.5h-3.5"/>
                <path d="M13 8a5 5 0 0 1-9 3L2.5 9.5"/>
                <path d="M3 13v-3.5h3.5"/>
              </svg>
              ${escapeHtml(t('suspect.refetch'))}
            </button>
          ` : ''}
        </div>
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
  if (status === 'downloading' || status === 'browser_uploading') return 'var(--accent)';
  if (
    status === 'processing'
    || status === 'merging'
    || status === 'browser_pending'
    || status === 'browser_finalizing'
  ) return 'var(--warn)';
  if (status === 'failed') return 'var(--err)';
  return 'var(--text-mid)';
}

function jobStatusLabel(job) {
  const pct = Number(job.progress) || 0;
  if (job.status === 'downloading') return `${getStatusLabel(job.status)} ${pct.toFixed(2)}%`;
  if (job.status === 'processing' || job.status === 'merging') return `${getStatusLabel(job.status)} ${pct.toFixed(2)}%`;
  // Browser-side upload: the NAS API doesn't track upload-phase progress;
  // the extension pushes per-segment progress via BROWSER_JOB_PROGRESS,
  // which `handleBrowserJobProgress` writes onto job.progress. Show the
  // percentage when we have a non-zero value; otherwise fall back to the
  // bare state label (covers the brief gap before the first segment lands).
  if (job.status === 'browser_uploading') {
    if (pct > 0) return `${getStatusLabel(job.status)} ${pct.toFixed(2)}%`;
    return getStatusLabel(job.status);
  }
  if (
    job.status === 'browser_pending'
    || job.status === 'browser_finalizing'
  ) {
    return getStatusLabel(job.status);
  }
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
  // Ring stroke color tracks status, so a transition between two
  // ACTIVE statuses (e.g. browser_pending → browser_uploading,
  // browser_uploading → browser_finalizing) needs an explicit
  // attribute write here. shouldFullRender() returns false for
  // active→active to preserve the smooth dasharray tween — without
  // this, the SVG stroke attribute keeps whatever value the FIRST
  // render baked in, leaving (e.g.) a job that started as
  // browser_pending stuck on the warn (orange) ring color even after
  // it's been uploading for a while.
  const arc = el.querySelector('[data-ring-arc]');
  if (arc) arc.setAttribute('stroke', ringStrokeForStatus(job.status));
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
  const refetchBtn = el.querySelector('[data-refetch]');
  if (refetchBtn) {
    refetchBtn.addEventListener('click', () => {
      const sourcePage = refetchBtn.dataset.sourcePage;
      if (!sourcePage) {
        showToast(t('suspect.toast.noSource'));
        return;
      }
      // Open the original video page in a new active tab so the site's JS
      // re-runs, the player triggers a fresh m3u8 request, and the
      // background SW captures a usable token + cookies. The user then
      // clicks Send normally on that tab — no auto-Send here on purpose
      // (avoids racing the player's load).
      try {
        chrome.tabs.create({ url: sourcePage, active: true });
        showToast(t('suspect.toast.opened'));
      } catch (_) {
        showToast(t('suspect.toast.noSource'));
      }
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
      // DELETE is the authoritative user-cancel transition. Only after
      // the NAS accepts it do we stop any browser-mode offscreen upload,
      // and we mark the resulting FAILED message as user-cancelled so
      // background cleanup does not race DELETE with /abort.
      try {
        await chrome.runtime.sendMessage({
          target: 'offscreen',
          type: 'CANCEL_BROWSER_JOB',
          payload: { jobId, userCancelled: true },
        });
      } catch (_) {
        // No offscreen receiver (most jobs aren't browser-mode) — ignore.
      }
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

// ---------- Hidden mode (AV-task quick-input) ----------
// Shared persistence: chrome.storage.local.avTaskHistory is the single
// source of truth for both this side panel and the options page's
// hidden_mode.toml table. Background.js writes; we just render the
// most-recent slice and listen for changes.
const AV_TASKS_VISIBLE = 8;

function applyHiddenModeVisibility() {
  const block = document.getElementById('avTaskBlock');
  if (!block) return;
  const enabled = !!(settings && settings.hiddenMode);
  if (enabled) {
    block.removeAttribute('hidden');
    const titleEl = document.getElementById('avTaskTitle');
    const labelEl = document.getElementById('avSubmitLabel');
    if (titleEl) titleEl.textContent = t('av.title');
    if (labelEl) labelEl.textContent = t('av.submit');
    const input = document.getElementById('avCodeInput');
    if (input) input.placeholder = t('av.placeholder');
    refreshAvTasksFromStorage();
  } else {
    block.setAttribute('hidden', '');
  }
}

async function refreshAvTasksFromStorage() {
  try {
    const stored = await chrome.storage.local.get(['avTaskHistory']);
    const list = Array.isArray(stored.avTaskHistory) ? stored.avTaskHistory : [];
    renderAvTasks(list.slice(0, AV_TASKS_VISIBLE));
  } catch (_) {
    renderAvTasks([]);
  }
}

function submitAvTask(rawCode) {
  const code = (rawCode || '').trim();
  if (!code) return;

  const template = (settings && settings.hiddenModeUrlTemplate) || 'https://missav.ws/dm18/{code}';
  if (!template.includes('{code}')) {
    showToast(t('av.toast.invalidTemplate'));
    return;
  }
  // Sanitize the code: only printable URL-safe chars. Strips spaces and
  // any path-injection attempts like "foo/bar"; the template is supposed
  // to substitute a single segment.
  const safeCode = code.replace(/[^A-Za-z0-9._-]/g, '');
  if (!safeCode) {
    showToast(t('av.toast.invalidCode'));
    return;
  }
  const url = template.replace('{code}', encodeURIComponent(safeCode));

  // Clear input immediately for the next code.
  const input = document.getElementById('avCodeInput');
  if (input) input.value = '';

  // Background.js writes the history row (status='pending') before
  // chrome.tabs.create returns; the storage.onChanged listener below
  // re-renders us. We don't need an optimistic local insert.
  chrome.runtime.sendMessage(
    { action: 'avTaskFetch', code: safeCode, url },
    () => {
      // Acks are best-effort; lastError fires when sidepanel was the only
      // listener and the SW restarted. The history row in storage is the
      // authoritative state, so we ignore the ack content here.
      void chrome.runtime.lastError;
    }
  );
}

function renderAvTasks(rows) {
  const list = document.getElementById('avTasksList');
  if (!list) return;
  if (!rows || rows.length === 0) {
    list.innerHTML = '';
    return;
  }
  list.innerHTML = rows.map(task => `
    <div class="av-task-row is-${escapeHtml(task.status || 'unknown')}">
      <span class="av-task-code">${escapeHtml(task.code || '?')}</span>
      <span class="av-task-status">${escapeHtml(avStatusLabel(task))}</span>
    </div>
  `).join('');
}

function avStatusLabel(task) {
  if (task.status === 'pending') return t('av.status.pending');
  if (task.status === 'sent')    return t('av.status.sent');
  if (task.status === 'failed')  return task.message || t('av.status.failed');
  return task.status || '';
}

// Live updates: the options-page table + this list both refresh whenever
// background.js writes to avTaskHistory. No need for the old code-keyed
// `avTaskUpdate` runtime broadcast (still fired for backwards compat
// inside background, but not consumed here anymore).
chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== 'local' || !changes.avTaskHistory) return;
  const list = changes.avTaskHistory.newValue;
  if (Array.isArray(list)) {
    renderAvTasks(list.slice(0, AV_TASKS_VISIBLE));
  }
});

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
    // v2.5 browser-side states (Codex P3 review).
    case 'browser_pending': return t('jobStatus.browser_pending');
    case 'browser_uploading': return t('jobStatus.browser_uploading');
    case 'browser_finalizing': return t('jobStatus.browser_finalizing');
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
    msg.includes('anti-hotlinking') ||
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

// Auto-refresh jobs every 2 seconds
setInterval(loadRecentJobs, 2000);
