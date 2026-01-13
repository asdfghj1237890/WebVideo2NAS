// Sidepanel Script for WebVideo2NAS

let settings = {};
let detectedUrls = [];
let jobs = [];
let expandedErrorIds = new Set(); // Track which error details are expanded
let activeTabId = null;
let loadDetectedUrlsSeq = 0;

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
  settings = await chrome.storage.sync.get(['nasEndpoint', 'apiKey', 'uiLanguage']);
  return settings;
}

function applyUiLanguage() {
  // Priority:
  // 1) If user selected a language (uiLanguage), use it.
  // 2) Otherwise, use browser language if it maps to zh/en/ja/fr/es/pt.
  // 3) If not matched, default English. (Handled inside i18n.js)
  if (i18n) {
    i18n.setLanguage((settings.uiLanguage || '').trim());
  }
  localizeStaticText();
}

function localizeStaticText() {
  // Header button titles
  const refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) refreshBtn.setAttribute('title', t('btn.refresh.title'));
  const settingsBtn = document.getElementById('settingsBtn');
  if (settingsBtn) settingsBtn.setAttribute('title', t('btn.settings.title'));

  // Section titles
  const detectedTitle = document.getElementById('detectedVideosTitle');
  if (detectedTitle) detectedTitle.textContent = t('section.detectedVideos');
  const recentTitle = document.getElementById('recentDownloadsTitle');
  if (recentTitle) recentTitle.textContent = t('section.recentDownloads');

  // Empty states (initial HTML only; list renders will overwrite as needed)
  const detectedEmptyTitle = document.getElementById('detectedEmptyTitle');
  if (detectedEmptyTitle) detectedEmptyTitle.textContent = t('empty.noVideos.title');
  const detectedEmptyHint = document.getElementById('detectedEmptyHint');
  if (detectedEmptyHint) detectedEmptyHint.textContent = t('empty.noVideos.hint');
  const jobsEmptyTitle = document.getElementById('jobsEmptyTitle');
  if (jobsEmptyTitle) jobsEmptyTitle.textContent = t('empty.noJobs.title');

  // Status text default
  const statusText = document.getElementById('statusText');
  if (statusText && statusText.textContent === 'Checking...') {
    statusText.textContent = t('status.checking');
  }
}

// Initialize sidepanel
document.addEventListener('DOMContentLoaded', async () => {
  // Load settings
  await loadSettingsFromStorage();
  applyUiLanguage();

  // Check connection
  checkConnection();

  // Load detected URLs
  loadDetectedUrls();

  // Load recent jobs
  await loadRecentJobs();

  // Setup event listeners
  setupEventListeners();

  // Background will notify us when "activity" changes (hitCount/rangeHitCount) even
  // if no new URL is added. This makes the Now Playing badge appear without manual refresh.
  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.action === 'detectedUrlsUpdated') {
      // Only refresh when the update belongs to the tab we're currently showing.
      if (activeTabId != null && message.tabId === activeTabId) {
        loadDetectedUrls();
      }
    }
  });
});

// Setup event listeners
function setupEventListeners() {
  document.getElementById('settingsBtn').addEventListener('click', openSettings);
  document.getElementById('refreshBtn').addEventListener('click', loadDetectedUrls);

  // Listen for tab updates (navigation, reload, etc.)
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    // When page finishes loading, refresh detected URLs
    if (changeInfo.status === 'complete') {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0] && tabs[0].id === tabId) {
          // Only refresh if it's the current active tab
          loadDetectedUrls();
        }
      });
    }
  });

  // Refresh immediately when the user switches tabs
  chrome.tabs.onActivated.addListener(() => {
    loadDetectedUrls();
  });

  // Listen for storage changes to auto-update when new URLs are detected
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === 'local' && changes.detectedUrls) {
      loadDetectedUrls();
    }
    if (areaName === 'sync') {
      const needsUiUpdate = !!changes.uiLanguage;
      const needsConnUpdate = !!changes.nasEndpoint || !!changes.apiKey;

      if (needsUiUpdate) {
        settings.uiLanguage = changes.uiLanguage.newValue || '';
        applyUiLanguage();
        renderDetectedUrls();
        // Force full re-render so translated labels update
        const listElement = document.getElementById('recentJobsList');
        if (listElement) listElement.innerHTML = '';
        renderJobs();
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

// Open settings page
function openSettings() {
  chrome.runtime.openOptionsPage();
}

function connectionReasonFromResponse(response) {
  if (!response) return '';
  if (response.status === 401) return t('options.status.invalidApiKey');
  if (response.status === 404) return t('options.status.apiNotFound');
  return `HTTP ${response.status}${response.statusText ? `: ${response.statusText}` : ''}`;
}

function connectionReasonFromError(error) {
  if (!error) return '';
  // AbortController timeout
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

// Check NAS connection
async function checkConnection() {
  const statusElement = document.getElementById('connectionStatus');
  const statusText = document.getElementById('statusText');
  const statusIcon = document.getElementById('statusIcon');

  // Avoid stale state if options were updated while sidepanel is open
  await loadSettingsFromStorage();

  if (!settings.nasEndpoint || !settings.apiKey) {
    statusElement.className = 'connection-status disconnected';
    statusText.textContent = t('status.notConfigured');
    statusIcon.textContent = '⚠️';
    statusElement.title = '';
    return;
  }

  statusElement.className = 'connection-status';
  statusText.textContent = t('status.checking');
  statusIcon.textContent = '⏳';
  statusElement.title = settings.nasEndpoint;

  try {
    const url = `${settings.nasEndpoint}/api/health`;
    const response = await fetchWithTimeout(url, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${settings.apiKey}`
      }
    }, 5000);

    if (response.ok) {
      statusElement.className = 'connection-status connected';
      statusText.textContent = t('status.connected');
      statusIcon.textContent = '✅';
      statusElement.title = `${settings.nasEndpoint}\n/api/health: OK`;
    } else {
      const reason = connectionReasonFromResponse(response);
      statusElement.className = 'connection-status disconnected';
      statusText.textContent = reason ? `${t('status.disconnected')} - ${reason}` : t('status.disconnected');
      statusIcon.textContent = '❌';
      statusElement.title = `${settings.nasEndpoint}\n/api/health: ${reason}`;
    }
  } catch (error) {
    const reason = connectionReasonFromError(error);
    statusElement.className = 'connection-status disconnected';
    statusText.textContent = reason ? `${t('status.disconnected')} - ${reason}` : t('status.disconnected');
    statusIcon.textContent = '❌';
    statusElement.title = `${settings.nasEndpoint}\n/api/health: ${reason || t('status.disconnected')}`;
  }
}

// Load detected URLs
function loadDetectedUrls() {
  const seq = ++loadDetectedUrlsSeq;

  // Avoid showing stale URLs while switching tabs.
  detectedUrls = [];
  renderDetectedUrls();

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (seq !== loadDetectedUrlsSeq) return;

    activeTabId = (tabs && tabs[0]) ? tabs[0].id : null;
    chrome.runtime.sendMessage({ action: 'getDetectedUrls', tabId: activeTabId }, (response) => {
      if (seq !== loadDetectedUrlsSeq) return;

      // If the sidepanel lost connection to the background (e.g., reloaded),
      // keep it empty rather than showing stale data.
      if (chrome.runtime.lastError) {
        detectedUrls = [];
        renderDetectedUrls();
        return;
      }

      detectedUrls = (response && response.urls) ? response.urls : [];
      renderDetectedUrls();
    });
  });
}

// Render detected URLs
function renderDetectedUrls() {
  const listElement = document.getElementById('detectedUrlsList');

  if (detectedUrls.length === 0) {
    listElement.innerHTML = `
      <div class="empty-state">
        <p>${escapeHtml(t('empty.noVideos.title'))}</p>
        <p class="hint">${escapeHtml(t('empty.noVideos.hint'))}</p>
      </div>
    `;
    return;
  }

  // Keep stable order: prefer higher resolution, then by score, then by detection time.
  // "Now Playing" badge is just a label, doesn't affect sort order.
  const sortedUrls = detectedUrls.slice().sort((a, b) => {
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

  listElement.innerHTML = sortedUrls.map((urlInfo, index) => {
    const hasIp = containsIpAddress(urlInfo.url);
    const urlLower = urlInfo.url.toLowerCase();
    // Some sites use paths like "...something.mp4/index.m3u8". Prefer the actual manifest type.
    const videoType = urlLower.includes('.m3u8') ? 'M3U8' : (urlLower.includes('.mp4') ? 'MP4' : 'VIDEO');
    const nowPlayingBadge = urlInfo.isNowPlaying
      ? ` <span class="badge-now-playing">${escapeHtml(t('url.nowPlaying'))}</span>`
      : '';
    const qualityBadges = buildQualityBadgesHtml(urlInfo.url);
    return `
    <div class="url-item">
      <div class="url-title">${videoType} #${index + 1}${nowPlayingBadge}${qualityBadges}</div>
      <div class="url-link" title="${escapeHtml(urlInfo.url)}">${escapeHtml(truncateUrl(urlInfo.url))}</div>
      ${hasIp ? `
        <div class="ip-warning">
          <strong>${escapeHtml(t('url.ipWarning.title'))}</strong><br>
          ${tHtml('url.ipWarning.body')}
        </div>
      ` : ''}
      <div class="url-actions">
        <button class="btn-send" data-url="${escapeHtml(urlInfo.url)}" data-page="${escapeHtml(urlInfo.pageUrl || '')}">
          ${escapeHtml(t('url.sendToNas'))}
        </button>
        <button class="btn-copy" data-url="${escapeHtml(urlInfo.url)}">
          ${escapeHtml(t('url.copy'))}
        </button>
      </div>
    </div>
  `;
  }).join('');

  // Add event listeners to buttons
  listElement.querySelectorAll('.btn-send').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const url = e.target.dataset.url;
      const pageUrl = e.target.dataset.page;
      sendToNAS(url, pageUrl);
    });
  });

  listElement.querySelectorAll('.btn-copy').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const url = e.target.dataset.url;
      copyToClipboard(url);
    });
  });
}

// Load recent jobs
async function loadRecentJobs() {
  if (!settings.nasEndpoint || !settings.apiKey) {
    return;
  }

  try {
    const response = await fetch(`${settings.nasEndpoint}/api/jobs?limit=10`, {
      headers: {
        'Authorization': `Bearer ${settings.apiKey}`
      }
    });

    if (response.ok) {
      jobs = await response.json();
      renderJobs();
    }
  } catch (error) {
    console.error('Failed to load jobs:', error);
  }
}

// Render jobs with smart update to avoid flickering
function renderJobs() {
  const listElement = document.getElementById('recentJobsList');
  const currentJobIds = new Set(jobs.map(j => j.id));

  // 1. Handle Empty State
  if (jobs.length === 0) {
    if (!listElement.querySelector('.empty-state')) {
      listElement.innerHTML = `
        <div class="empty-state">
          <p>${escapeHtml(t('empty.noJobs.short'))}</p>
        </div>
      `;
    }
    return;
  }

  // Remove empty state if present
  const emptyState = listElement.querySelector('.empty-state');
  if (emptyState) emptyState.remove();

  // 2. Update/Create items
  jobs.forEach((job, index) => {
    let itemEl = document.getElementById(`job-${job.id}`);

    // Create if not exists
    if (!itemEl) {
      itemEl = document.createElement('div');
      itemEl.className = 'job-item';
      itemEl.id = `job-${job.id}`;
      
      // Insert at correct position
      const nextSibling = listElement.children[index];
      if (nextSibling) {
        listElement.insertBefore(itemEl, nextSibling);
      } else {
        listElement.appendChild(itemEl);
      }
      
      // Initial Render
      itemEl.innerHTML = getJobInnerHtml(job);
      bindJobEvents(itemEl, job.id);
    } else {
      // Update existing
      const oldStatus = itemEl.dataset.status;
      const oldProgress = itemEl.dataset.progress;

      // Update data attributes
      itemEl.dataset.status = job.status;
      itemEl.dataset.progress = job.progress;
      
      // Check if full re-render is needed (structure change)
      if (shouldFullRender(oldStatus, job.status)) {
         itemEl.innerHTML = getJobInnerHtml(job);
         bindJobEvents(itemEl, job.id);
      } else {
         // Minimal update (progress bar, text only)
         updateJobElement(itemEl, job);
      }
      
      // Ensure element is in correct position (if list order changed)
      const currentIdx = Array.from(listElement.children).indexOf(itemEl);
      if (currentIdx !== index) {
         const nextSibling = listElement.children[index];
         if (nextSibling) {
            listElement.insertBefore(itemEl, nextSibling);
         } else {
            listElement.appendChild(itemEl);
         }
      }
    }
  });

  // 3. Remove old items
  Array.from(listElement.children).forEach(child => {
    if (child.id.startsWith('job-') && !currentJobIds.has(child.id.replace('job-', ''))) {
      child.remove();
    }
  });
}

// Generate inner HTML for a job item
function getJobInnerHtml(job) {
  const canCancel = ['pending', 'downloading', 'processing'].includes(job.status);
  const showProgress = job.status === 'downloading' || job.status === 'processing';
  const isFailed = job.status === 'failed';
  const errorInfo = isFailed ? getErrorInfo(job.error_message) : null;
  const statusTooltip = (job.status === 'completed' && typeof job.duration === 'number')
    ? t('job.duration', { duration: formatDuration(job.duration) })
    : '';

  return `
    <div class="job-header">
      <div class="job-title" title="${escapeHtml(job.title)}">${escapeHtml(job.title)}</div>
      <div class="job-status ${job.status}" ${statusTooltip ? `title="${escapeHtml(statusTooltip)}"` : ''}>${getStatusLabel(job.status)}</div>
    </div>
    ${showProgress || canCancel ? `
      <div class="job-progress">
        ${showProgress ? `
          <div class="progress-container">
            <div class="progress-fill" style="width: ${job.progress}%"></div>
          </div>
          <div class="progress-text">${job.progress}%</div>
        ` : ''}
        ${canCancel ? `
          <button class="btn-cancel" data-job-id="${job.id}" title="${escapeHtml(t('job.cancel.title'))}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <path d="m15 9-6 6M9 9l6 6"/>
            </svg>
            <span>${escapeHtml(t('job.cancel'))}</span>
          </button>
        ` : ''}
      </div>
    ` : ''}
    ${isFailed && errorInfo ? `
      <details class="error-details" data-job-id="${job.id}" ${expandedErrorIds.has(job.id) ? 'open' : ''}>
        <summary class="error-summary">
          <div class="error-icon">!</div>
          <span class="error-type">${escapeHtml(errorInfo.type)}</span>
          <span class="error-expand-icon">▶</span>
        </summary>
        <div class="error-content">
          <div class="error-message">${escapeHtml(errorInfo.message)}</div>
          <div class="error-solution">
            <div class="error-solution-title">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 2a6 6 0 0 1 6 6c0 7-3 9-3 9h-6s-3-2-3-9a6 6 0 0 1 6-6Z"/>
                <path d="M9 18h6"/>
                <path d="M10 22h4"/>
              </svg>
              ${escapeHtml(t('job.solution'))}
            </div>
            <div class="error-solution-text">${errorInfo.solution}</div>
          </div>
        </div>
      </details>
    ` : ''}
  `;
}

// Check if full re-render is needed
function shouldFullRender(oldStatus, newStatus) {
  // If status changed to/from 'downloading'/'processing' (progress bar visibility changes)
  const hasProgress = s => ['downloading', 'processing'].includes(s);
  const isFailed = s => s === 'failed';
  
  if (oldStatus !== newStatus) {
    if (hasProgress(oldStatus) !== hasProgress(newStatus)) return true;
    if (isFailed(oldStatus) !== isFailed(newStatus)) return true;
    if (['pending', 'completed', 'cancelled'].includes(newStatus)) return true;
  }
  
  return false;
}

// Update existing element without re-rendering HTML
function updateJobElement(el, job) {
  // Update status label
  const statusEl = el.querySelector('.job-status');
  if (statusEl) {
    statusEl.className = `job-status ${job.status}`;
    statusEl.textContent = getStatusLabel(job.status);
    const statusTooltip = (job.status === 'completed' && typeof job.duration === 'number')
      ? t('job.duration', { duration: formatDuration(job.duration) })
      : '';
    if (statusTooltip) {
      statusEl.setAttribute('title', statusTooltip);
    } else {
      statusEl.removeAttribute('title');
    }
  }
  
  // Update progress bar
  const fillEl = el.querySelector('.progress-fill');
  const textEl = el.querySelector('.progress-text');
  if (fillEl) fillEl.style.width = `${job.progress}%`;
  if (textEl) textEl.textContent = `${job.progress}%`;
}

// Bind events to job item elements
function bindJobEvents(el, jobId) {
  const cancelBtn = el.querySelector('.btn-cancel');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => cancelJob(jobId));
  }
  
  const details = el.querySelector('.error-details');
  if (details) {
    details.addEventListener('toggle', (e) => {
      if (e.target.open) {
        expandedErrorIds.add(jobId);
      } else {
        expandedErrorIds.delete(jobId);
      }
    });
  }
}


// Send to NAS
async function sendToNAS(url, pageUrl) {
  // Ensure we have the latest settings before sending
  await loadSettingsFromStorage();

  if (!settings.nasEndpoint || !settings.apiKey) {
    alert(t('alert.configureFirst'));
    chrome.runtime.openOptionsPage();
    return;
  }

  try {
    // Get current tab title
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const title = tab.title || t('video.untitled');

    chrome.runtime.sendMessage({
      action: 'sendToNAS',
      url: url,
      title: title,
      pageUrl: pageUrl || tab.url
    });

    // Show feedback
    showToast(t('toast.sending'));

    // Refresh jobs after 2 seconds
    setTimeout(loadRecentJobs, 2000);

  } catch (error) {
    console.error('Error:', error);
    showToast(t('toast.failedToSend'));
  }
}

// Cancel job on NAS
async function cancelJob(jobId) {
  if (!settings.nasEndpoint || !settings.apiKey) {
    showToast(t('toast.nasNotConfigured'));
    return;
  }

  try {
    const response = await fetch(`${settings.nasEndpoint}/api/jobs/${jobId}`, {
      method: 'DELETE',
      headers: {
        'Authorization': `Bearer ${settings.apiKey}`
      }
    });

    if (response.ok) {
      showToast(t('toast.jobCancelled'));
      // Refresh jobs list
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

// Copy to clipboard
function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    showToast(t('toast.copied'));
  });
}

// Show toast notification
function showToast(message) {
  // Simple toast implementation
  const toast = document.createElement('div');
  toast.textContent = message;
  toast.style.cssText = `
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: #333;
    color: white;
    padding: 8px 16px;
    border-radius: 4px;
    font-size: 12px;
    z-index: 1000;
  `;
  document.body.appendChild(toast);

  setTimeout(() => {
    toast.remove();
  }, 2000);
}

// Utility functions
function truncateUrl(url, maxLength = 60) {
  if (url.length <= maxLength) return url;
  return url.substring(0, maxLength) + '...';
}

function extractQualitiesFromUrl(url) {
  const raw = String(url || '');
  const lower = raw.toLowerCase();
  if (!lower) return [];

  const allowed = new Set([2160, 1440, 1080, 720, 540, 480, 360, 240]);
  const found = new Set();

  // Common patterns: "...1080p...", "..._720p...", "/480p/", etc.
  // Use lookarounds so multiple adjacent matches (e.g. "...720p_1080p...") are not skipped.
  const pMatches = lower.matchAll(/(?<![0-9])([0-9]{3,4})p(?![a-z0-9])/g);
  for (const m of pMatches) {
    const n = Number(m[1]);
    if (allowed.has(n)) found.add(n);
  }

  // Query string hints: ?quality=720, &res=1080, &height=480, etc.
  const qMatches = lower.matchAll(/[?&](?:res|resolution|quality|q|height|h)=([0-9]{3,4})\b/g);
  for (const m of qMatches) {
    const n = Number(m[1]);
    if (allowed.has(n)) found.add(n);
  }

  return Array.from(found)
    .sort((a, b) => b - a)
    .map(n => `${n}p`);
}

function buildQualityBadgesHtml(url) {
  const qualities = extractQualitiesFromUrl(url);
  if (!qualities.length) return '';
  return ` <span class="badges-qualities">${qualities
    .map(q => `<span class="badge-quality" title="${escapeHtml(q)}">${escapeHtml(q)}</span>`)
    .join('')}</span>`;
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
    case 'pending':
      return t('jobStatus.pending');
    case 'downloading':
      return t('jobStatus.downloading');
    case 'processing':
      return t('jobStatus.processing');
    case 'completed':
      return t('jobStatus.completed');
    case 'failed':
      return t('jobStatus.failed');
    case 'cancelled':
      return t('jobStatus.cancelled');
    default:
      return status;
  }
}

// Parse error message and return type, message, and solution
function getErrorInfo(errorMessage) {
  if (!errorMessage) {
    return {
      type: t('error.unknown.type'),
      message: t('error.unknown.message'),
      solution: t('error.unknown.solution')
    };
  }

  const msg = errorMessage.toLowerCase();

  // 403 Forbidden - IP restriction or authentication issue
  if (msg.includes('403') || msg.includes('forbidden')) {
    return {
      type: t('error.403.type'),
      message: errorMessage,
      solution: t('error.403.solution')
    };
  }

  // 404 Not Found
  if (msg.includes('404') || msg.includes('not found')) {
    return {
      type: t('error.404.type'),
      message: errorMessage,
      solution: t('error.404.solution')
    };
  }

  // Timeout errors
  if (msg.includes('timeout') || msg.includes('timed out')) {
    return {
      type: t('error.timeout.type'),
      message: errorMessage,
      solution: t('error.timeout.solution')
    };
  }

  // SSL/TLS errors
  if (msg.includes('ssl') || msg.includes('certificate') || msg.includes('tls')) {
    return {
      type: t('error.ssl.type'),
      message: errorMessage,
      solution: t('error.ssl.solution')
    };
  }

  // Connection errors
  if (msg.includes('connection') || msg.includes('network') || msg.includes('unreachable')) {
    return {
      type: t('error.connection.type'),
      message: errorMessage,
      solution: t('error.connection.solution')
    };
  }

  // No segments found
  if (msg.includes('no segments') || msg.includes('empty playlist')) {
    return {
      type: t('error.invalidPlaylist.type'),
      message: errorMessage,
      solution: t('error.invalidPlaylist.solution')
    };
  }

  // Generic error
  return {
    type: t('error.generic.type'),
    message: errorMessage,
    solution: t('error.generic.solution')
  };
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
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

// Check if URL contains an IP address
function containsIpAddress(url) {
  // IPv4 pattern in query parameters: matches ?ip=114.24.18.78 or &ip=114.24.18.78
  const ipv4QueryPattern = /[?&]ip=(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/;
  
  return ipv4QueryPattern.test(url);
}

// Auto-refresh jobs every 5 seconds
setInterval(loadRecentJobs, 5000);
