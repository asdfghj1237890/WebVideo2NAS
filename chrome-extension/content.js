// Content script to detect user interaction with video elements

(function() {
  'use strict';

  // Get the index of a video element among all videos on the page
  function getVideoIndex(video) {
    const allVideos = Array.from(document.querySelectorAll('video'));
    return allVideos.indexOf(video);
  }

  // Get total number of videos on the page
  function getVideoCount() {
    return document.querySelectorAll('video').length;
  }

  // Find the video element from a click target (could be the video itself or a parent/overlay)
  function findVideoElement(element) {
    if (!element) return null;

    // Direct video element
    if (element.tagName === 'VIDEO') return element;

    // Check if element contains a video
    const video = element.querySelector('video');
    if (video) return video;

    // Walk up the DOM to find a video in a parent container
    let parent = element.parentElement;
    let depth = 0;
    const maxDepth = 10;

    while (parent && depth < maxDepth) {
      if (parent.tagName === 'VIDEO') return parent;

      const video = parent.querySelector('video');
      if (video) return video;

      parent = parent.parentElement;
      depth++;
    }

    return null;
  }

  // Get the video source URL
  function getVideoSrc(video) {
    if (!video) return null;

    // Prefer currentSrc (the actual playing source after source selection)
    if (video.currentSrc) return video.currentSrc;

    // Fallback to src attribute
    if (video.src) return video.src;

    // Check source elements
    const source = video.querySelector('source');
    if (source && source.src) return source.src;

    return null;
  }

  // Send video interaction info to background
  function sendVideoInteraction(action, video) {
    const src = getVideoSrc(video);
    const videoIndex = getVideoIndex(video);
    const videoCount = getVideoCount();

    try {
      chrome.runtime.sendMessage({
        action: action,
        videoSrc: src || null,
        videoIndex: videoIndex,
        videoCount: videoCount,
        pageUrl: window.location.href,
        timestamp: Date.now()
      });
    } catch (e) {
      // Extension context may be invalid (e.g., extension reloaded)
    }
  }

  // Handle click events
  function handleClick(event) {
    const video = findVideoElement(event.target);
    if (!video) return;

    sendVideoInteraction('userClickedVideo', video);
  }

  // Handle play events (when video starts playing, regardless of how it was triggered)
  function handlePlay(event) {
    const video = event.target;
    if (!video || video.tagName !== 'VIDEO') return;

    sendVideoInteraction('videoStartedPlaying', video);
  }

  // Use capture phase to catch clicks before they're stopped by video player overlays
  document.addEventListener('click', handleClick, true);
  
  // Also listen for play events on video elements
  document.addEventListener('play', handlePlay, true);

  // For dynamically added videos, observe DOM mutations
  const observer = new MutationObserver((mutations) => {
    let sawNewVideo = false;
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;

        // Check if it's a video element or contains one
        if (node.tagName === 'VIDEO') {
          node.addEventListener('play', handlePlay);
          sawNewVideo = true;
        } else if (node.querySelectorAll) {
          const videos = node.querySelectorAll('video');
          if (videos.length > 0) {
            videos.forEach(v => v.addEventListener('play', handlePlay));
            sawNewVideo = true;
          }
        }
      }
    }
    if (sawNewVideo) schedulePageThumbnails();
  });

  observer.observe(document.body || document.documentElement, {
    childList: true,
    subtree: true
  });

  // Attach play listener to existing videos
  document.querySelectorAll('video').forEach(v => {
    v.addEventListener('play', handlePlay);
  });

  // ---- Page thumbnail scraping (og:image + <video poster>) ----
  function absoluteUrl(maybeRelative) {
    if (!maybeRelative) return null;
    try { return new URL(maybeRelative, location.href).href; }
    catch (_) { return maybeRelative; }
  }

  function getPageThumbnail() {
    const selectors = [
      'meta[property="og:image:secure_url"]',
      'meta[property="og:image:url"]',
      'meta[property="og:image"]',
      'meta[name="og:image"]',
      'meta[name="twitter:image"]',
      'meta[name="twitter:image:src"]',
      'meta[itemprop="thumbnailUrl"]',
      'link[rel="image_src"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const v = el.getAttribute('content') || el.getAttribute('href');
      if (v) return absoluteUrl(v);
    }
    return null;
  }

  function getVideoPosters() {
    const out = [];
    document.querySelectorAll('video').forEach((v, idx) => {
      const poster = v.getAttribute('poster');
      if (!poster) return;
      out.push({
        poster: absoluteUrl(poster),
        src: v.currentSrc || v.src || null,
        index: idx,
      });
    });
    return out;
  }

  function sendPageThumbnails() {
    const pageThumbnail = getPageThumbnail();
    const videoPosters = getVideoPosters();
    if (!pageThumbnail && videoPosters.length === 0) return;
    try {
      chrome.runtime.sendMessage({
        action: 'pageThumbnails',
        pageUrl: window.location.href,
        pageThumbnail: pageThumbnail,
        videoPosters: videoPosters,
      });
    } catch (e) {
      // Extension context may be invalid
    }
  }

  // Initial scrape — wait for head to settle
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', sendPageThumbnails, { once: true });
  } else {
    sendPageThumbnails();
  }

  // Re-scrape when DOM changes (debounced) — handles SPA route changes + late-loaded posters
  let pageThumbsTimer = null;
  function schedulePageThumbnails() {
    clearTimeout(pageThumbsTimer);
    pageThumbsTimer = setTimeout(sendPageThumbnails, 600);
  }
  const headObserver = new MutationObserver(schedulePageThumbnails);
  if (document.head) {
    headObserver.observe(document.head, { childList: true, subtree: true, attributes: true });
  }

  // Forward manifest detection to background
  function forwardManifest(data) {
    if (!data || !data.url || !data.format) return;
    try {
      chrome.runtime.sendMessage({
        action: 'manifestDetected',
        url: data.url,
        format: data.format,
        pageUrl: window.location.href,
        timestamp: Date.now()
      });
    } catch (e) {
      // Extension context may be invalid
    }
  }

  // Listen for manifest detections from inject.js (MAIN world)
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    if (!event.data || event.data.type !== 'WV2NAS_MANIFEST_DETECTED') return;
    forwardManifest(event.data);
  });

  // Ask inject.js to re-send any manifests detected before we were ready
  window.postMessage({ type: 'WV2NAS_CONTENT_READY' }, '*');


})();
