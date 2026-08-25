// v2.5 offscreen document — hosts the long-running browser-side segment
// downloader. MV3 service workers die after ~30s idle; offscreen documents
// live as long as the SW holds them open via chrome.offscreen.createDocument.
// SW just routes messages here; this file does the actual fetch + AES decrypt
// + streaming PUT to NAS.

import { runJob } from './segmentDownloader.js';

// Tracks active jobs so the SW can cancel mid-flight via signal.abort().
const jobs = new Map();

// Codex review #16: liveness heartbeats. MV3 SW can be evicted while
// offscreen continues running. The SW-boot watchdog used to reap any
// persisted job older than 1h regardless of whether it was still
// active — destroying legitimate slow downloads. Now offscreen sends
// a heartbeat every HEARTBEAT_INTERVAL_MS so the SW persists a fresh
// `lastHeartbeat` timestamp; the watchdog reaps based on heartbeat
// freshness rather than start age.
const HEARTBEAT_INTERVAL_MS = 10_000;
const COMPLETION_SEND_MAX_ATTEMPTS = 3;
const COMPLETION_SEND_RETRY_MS = 250;


function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}


async function sendCompletionMessageWithRetry(message) {
  let lastErr = null;
  for (let attempt = 0; attempt < COMPLETION_SEND_MAX_ATTEMPTS; attempt++) {
    try {
      const response = await chrome.runtime.sendMessage(message);
      if (response && response.ok === false) {
        throw new Error(response.error || 'service worker rejected completion');
      }
      return true;
    } catch (err) {
      lastErr = err;
      if (attempt < COMPLETION_SEND_MAX_ATTEMPTS - 1) {
        await sleep(COMPLETION_SEND_RETRY_MS);
      }
    }
  }
  console.warn(
    '[wv2nas-offscreen] completion message failed after retries:',
    lastErr,
  );
  return false;
}

async function deliverCompletionWhileAlive(message) {
  while (true) {
    if (await sendCompletionMessageWithRetry(message)) {
      return true;
    }
    await sleep(HEARTBEAT_INTERVAL_MS);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen') return false;

  if (msg.type === 'START_BROWSER_JOB') {
    const { jobId, nasScope } = msg.payload;
    // Job ids are unique per NAS, not globally. Two NAS restored from the same
    // backup issue the same ids, and both can have work in flight here at once
    // after a profile switch — so the uploader's table, and every message it
    // sends, is keyed by NAS + id. See nasIdentity.js.
    const jobKey = globalThis.WV2NNasIdentity.browserJobKey(nasScope, jobId);
    if (!jobKey) {
      sendResponse({ ok: false, error: 'job is missing its NAS scope' });
      return false;
    }
    if (jobs.has(jobKey)) {
      sendResponse({ ok: false, error: 'job already running' });
      return false;
    }
    const controller = new AbortController();
    const state = { controller, userCancelled: false };
    jobs.set(jobKey, state);

    // Codex review #16: heartbeat ticker. Sends a liveness signal
    // every HEARTBEAT_INTERVAL_MS to the SW so the watchdog at next
    // SW boot can distinguish "still actively downloading" from
    // "offscreen died and its job is stranded".
    const heartbeatTimer = setInterval(() => {
      chrome.runtime.sendMessage({
        type: 'BROWSER_JOB_HEARTBEAT',
        target: 'service-worker',
        payload: { jobId, nasScope, ts: Date.now() },
      }).catch(() => {});
    }, HEARTBEAT_INTERVAL_MS);
    // Send one immediately so a recent persisted entry is seeded
    // before any potential SW restart.
    chrome.runtime.sendMessage({
      type: 'BROWSER_JOB_HEARTBEAT',
      target: 'service-worker',
      payload: { jobId, nasScope, ts: Date.now() },
    }).catch(() => {});

    // Throttled progress emitter. segmentDownloader.runJob calls
    // onProgress per media segment; for a 65-segment HLS that's 65
    // events. Many quick events would still be cheap on the SW message
    // bus, but throttling avoids hammering chrome.runtime.sendMessage
    // when the UI doesn't need pixel-precise updates. Always emit the
    // first event (so the bar leaves 0% promptly) and the last event
    // (so it lands on 100%); throttle the middle.
    const PROGRESS_THROTTLE_MS = 200;
    let lastProgressTs = 0;
    const onProgress = ({ done, total, concurrency, transferTimings }) => {
      const now = Date.now();
      const isFirst = done === 1;
      const isLast = total > 0 && done >= total;
      if (!isFirst && !isLast && now - lastProgressTs < PROGRESS_THROTTLE_MS) return;
      lastProgressTs = now;
      chrome.runtime.sendMessage({
        type: 'BROWSER_JOB_PROGRESS',
        target: 'service-worker',
        payload: {
          jobId,
          nasScope,
          done,
          total,
          concurrency,
          transferTimings,
          ts: now,
        },
      }).catch(() => {});
    };

    runJob({ ...msg.payload, signal: controller.signal, onProgress })
      .then(async (summary) => {
        console.info('[wv2nas-transfer]', {
          jobId,
          concurrency: summary && summary.concurrency,
          transferTimings: summary && summary.transferTimings,
        });
        await deliverCompletionWhileAlive({
          type: 'BROWSER_JOB_DONE',
          target: 'service-worker',
          payload: { jobId, nasScope, summary },
        });
      })
      .catch(async (err) => {
        console.info('[wv2nas-transfer]', {
          jobId,
          failed: true,
          concurrency: err && err.concurrency,
          transferTimings: err && err.transferTimings,
        });
        // Codex review #4: forward finalizeAttempted flag so the SW can
        // decide whether the server-side abort is safe to call. Once
        // finalize has been POSTed, the server may have committed the
        // queue push regardless of the client-side outcome.
        await deliverCompletionWhileAlive({
          type: 'BROWSER_JOB_FAILED',
          target: 'service-worker',
          payload: {
            jobId,
            nasScope,
            error: String(err && err.message || err),
            finalizeAttempted: !!(err && err.finalizeAttempted),
            userCancelled: !!state.userCancelled,
            concurrency: err && err.concurrency,
            done: err && err.done,
            total: err && err.total,
            transferTimings: err && err.transferTimings,
          },
        });
      })
      .finally(() => {
        clearInterval(heartbeatTimer);
        jobs.delete(jobKey);
      });

    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'CANCEL_BROWSER_JOB') {
    // Match on NAS + id. A bare id could abort the other NAS's upload when
    // both were restored from the same database backup.
    const cancelKey = globalThis.WV2NNasIdentity.browserJobKey(
      msg.payload.nasScope, msg.payload.jobId);
    const state = cancelKey ? jobs.get(cancelKey) : null;
    if (state) {
      state.userCancelled = state.userCancelled || !!msg.payload.userCancelled;
      state.controller.abort();
    }
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

// Ack readiness so SW knows it can post START_BROWSER_JOB.
chrome.runtime.sendMessage({ type: 'OFFSCREEN_READY', target: 'service-worker' }).catch(() => {});
