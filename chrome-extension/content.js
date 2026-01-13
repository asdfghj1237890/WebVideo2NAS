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
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        
        // Check if it's a video element or contains one
        if (node.tagName === 'VIDEO') {
          node.addEventListener('play', handlePlay);
        } else if (node.querySelectorAll) {
          const videos = node.querySelectorAll('video');
          videos.forEach(v => v.addEventListener('play', handlePlay));
        }
      }
    }
  });

  observer.observe(document.body || document.documentElement, {
    childList: true,
    subtree: true
  });

  // Attach play listener to existing videos
  document.querySelectorAll('video').forEach(v => {
    v.addEventListener('play', handlePlay);
  });

})();
