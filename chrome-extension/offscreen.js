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
      await chrome.runtime.sendMessage(message);
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
    const { jobId } = msg.payload;
    if (jobs.has(jobId)) {
      sendResponse({ ok: false, error: 'job already running' });
      return false;
    }
    const controller = new AbortController();
    const state = { controller, userCancelled: false };
    jobs.set(jobId, state);

    // Codex review #16: heartbeat ticker. Sends a liveness signal
    // every HEARTBEAT_INTERVAL_MS to the SW so the watchdog at next
    // SW boot can distinguish "still actively downloading" from
    // "offscreen died and its job is stranded".
    const heartbeatTimer = setInterval(() => {
      chrome.runtime.sendMessage({
        type: 'BROWSER_JOB_HEARTBEAT',
        target: 'service-worker',
        payload: { jobId, ts: Date.now() },
      }).catch(() => {});
    }, HEARTBEAT_INTERVAL_MS);
    // Send one immediately so a recent persisted entry is seeded
    // before any potential SW restart.
    chrome.runtime.sendMessage({
      type: 'BROWSER_JOB_HEARTBEAT',
      target: 'service-worker',
      payload: { jobId, ts: Date.now() },
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
    const onProgress = ({ done, total }) => {
      const now = Date.now();
      const isFirst = done === 1;
      const isLast = total > 0 && done >= total;
      if (!isFirst && !isLast && now - lastProgressTs < PROGRESS_THROTTLE_MS) return;
      lastProgressTs = now;
      chrome.runtime.sendMessage({
        type: 'BROWSER_JOB_PROGRESS',
        target: 'service-worker',
        payload: { jobId, done, total, ts: now },
      }).catch(() => {});
    };

    runJob({ ...msg.payload, signal: controller.signal, onProgress })
      .then(async (summary) => {
        await deliverCompletionWhileAlive({
          type: 'BROWSER_JOB_DONE',
          target: 'service-worker',
          payload: { jobId, summary },
        });
      })
      .catch(async (err) => {
        // Codex review #4: forward finalizeAttempted flag so the SW can
        // decide whether the server-side abort is safe to call. Once
        // finalize has been POSTed, the server may have committed the
        // queue push regardless of the client-side outcome.
        await deliverCompletionWhileAlive({
          type: 'BROWSER_JOB_FAILED',
          target: 'service-worker',
          payload: {
            jobId,
            error: String(err && err.message || err),
            finalizeAttempted: !!(err && err.finalizeAttempted),
            userCancelled: !!state.userCancelled,
          },
        });
      })
      .finally(() => {
        clearInterval(heartbeatTimer);
        jobs.delete(jobId);
      });

    sendResponse({ ok: true });
    return false;
  }

  if (msg.type === 'CANCEL_BROWSER_JOB') {
    const state = jobs.get(msg.payload.jobId);
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
