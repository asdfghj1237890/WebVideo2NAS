"""
Segment Downloader
Multi-threaded downloader for m3u8 video segments
"""

import logging
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Callable
import time
from pathlib import Path
from urllib.parse import urlparse
import urllib3
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from ssl_adapter import create_legacy_session, create_impersonated_session, tls_verify_enabled

# Cross-process per-host concurrency throttle. Optional — no-op when
# HOST_CONCURRENCY_CAP env is unset. See host_throttle.py for rationale.
try:
    import host_throttle as _host_throttle
except ImportError:
    _host_throttle = None  # type: ignore[assignment]

# Network-layer errors that should NOT trigger Referer-strategy fallback.
# A RST or transfer timeout means the host is throttling/dropping us — trying
# 4 different Referer/Origin combos against the same throttled host just
# amplifies pressure. These get re-raised so the outer retry+backoff handles
# them instead. HTTP-layer rejections (4xx/5xx, anti-hotlink images) still
# fall back to other strategies as before.
_TRANSPORT_ERRORS: tuple = ()
try:
    from curl_cffi.requests.exceptions import (
        Timeout as _CurlTimeout,
        ConnectionError as _CurlConnectionError,
    )
    _TRANSPORT_ERRORS = _TRANSPORT_ERRORS + (_CurlTimeout, _CurlConnectionError)
except ImportError:
    pass
try:
    from requests.exceptions import (
        Timeout as _ReqTimeout,
        ConnectionError as _ReqConnectionError,
    )
    _TRANSPORT_ERRORS = _TRANSPORT_ERRORS + (_ReqTimeout, _ReqConnectionError)
except ImportError:
    pass

if not tls_verify_enabled():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# MPEG-TS sync byte - all valid .ts files start with this
TS_SYNC_BYTE = b'\x47'
TS_PACKET_SIZE = 188

# Common file magic bytes for detecting anti-hotlink responses
JPEG_MAGIC = b'\xff\xd8\xff'
PNG_MAGIC = b'\x89PNG'
GIF_MAGIC = b'GIF8'
MP4_FTYP_AT_4 = b'ftyp'
MP4_STYP_AT_4 = b'styp'


class SegmentDownloader:
    """Download video segments with multi-threading and retry logic"""
    
    def __init__(
        self,
        segments: List[Dict],
        output_dir: str,
        headers: Optional[Dict] = None,
        max_workers: int = 10,
        max_retries: int = 3,
        timeout: int = 30,
        encryption_key: Optional[bytes] = None,
        encryption_iv: Optional[bytes] = None,
        m3u8_url: Optional[str] = None,
        session=None
    ):
        self.segments = segments
        self.output_dir = Path(output_dir)
        self.headers = headers or {}
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.timeout = timeout
        self.encryption_key = encryption_key
        self.encryption_iv = encryption_iv
        self.m3u8_url = m3u8_url

        # Cache for rotating AES-128 keys (key URI -> bytes)
        self._key_cache = {}
        self._key_cache_lock = threading.Lock()
        
        self.downloaded_count = 0
        self.total_segments = len(segments)
        self.failed_segments = []
        
        # Stop event for cooperative cancellation
        self._stop_event = threading.Event()
        
        # Track which Referer strategy worked (for logging)
        self.working_referer_strategy = None
        
        # Use provided session or create impersonated session for anti-bot bypass
        # curl_cffi with Chrome TLS fingerprint helps bypass CDN anti-hotlinking
        self.session = session if session else create_impersonated_session()
        logger.info(f"Segment downloader using session type: {type(self.session).__name__}")
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def request_stop(self):
        """Request all download threads to stop"""
        logger.info("Stop requested for segment downloader")
        self._stop_event.set()
    
    def is_stop_requested(self) -> bool:
        """Check if stop has been requested"""
        return self._stop_event.is_set()
    
    def _is_valid_ts_content(self, data: bytes) -> tuple[bool, str]:
        """
        Validate if the content is a valid MPEG-TS file.
        Returns (is_valid, error_reason) tuple.
        """
        if not data or len(data) < TS_PACKET_SIZE:
            return False, "Content too small"
        
        # Check for image files (anti-hotlinking protection)
        if data[:3] == JPEG_MAGIC:
            return False, "Server returned JPEG image (anti-hotlinking protection)"
        if data[:4] == PNG_MAGIC:
            return False, "Server returned PNG image (anti-hotlinking protection)"
        if data[:4] == GIF_MAGIC:
            return False, "Server returned GIF image (anti-hotlinking protection)"
        
        # Check if it starts with HTML (error page)
        if data[:5].lower() in (b'<!doc', b'<html', b'<?xml'):
            return False, "Server returned HTML error page"
        
        # Check for common error text patterns
        lower_start = data[:500].lower()
        if b'error' in lower_start or b'forbidden' in lower_start or b'denied' in lower_start:
            return False, "Server returned error response"
        
        # Check for TS sync byte at expected positions
        # TS packets are 188 bytes, sync byte should appear at 0, 188, 376, etc.
        sync_count = 0
        for i in range(0, min(len(data), TS_PACKET_SIZE * 5), TS_PACKET_SIZE):
            if data[i:i+1] == TS_SYNC_BYTE:
                sync_count += 1
        
        # If we found sync bytes at expected positions, it's likely valid
        if sync_count >= 2:
            return True, ""
        
        return False, "Invalid TS format (no sync bytes found)"

    def _is_obviously_blocked_response(self, data: bytes, content_type: str = "") -> tuple[bool, str]:
        """
        Detect common non-media responses (HTML/JSON/images) before any decryption.
        This prevents turning block pages into random bytes via AES decrypt and then
        mistakenly accepting them.
        """
        if not data:
            return True, "Empty response"

        ct = (content_type or "").lower()
        if "text/html" in ct:
            return True, "Server returned text/html (likely blocked)"
        if "application/json" in ct:
            return True, "Server returned application/json (likely error)"

        # Images (anti-hotlinking placeholders)
        if data[:3] == JPEG_MAGIC:
            return True, "Server returned JPEG image (anti-hotlinking protection)"
        if data[:4] == PNG_MAGIC:
            return True, "Server returned PNG image (anti-hotlinking protection)"
        if data[:4] == GIF_MAGIC:
            return True, "Server returned GIF image (anti-hotlinking protection)"

        # HTML/XML
        if data[:5].lower() in (b'<!doc', b'<html', b'<?xml'):
            return True, "Server returned HTML/XML error page"

        lower_start = data[:1000].lower()
        if b'forbidden' in lower_start or b'access denied' in lower_start or b'denied' in lower_start:
            return True, "Server returned access denied response"

        return False, ""
    
    def _decrypt_segment(self, data: bytes, segment_index: int) -> bytes:
        """Decrypt AES-128 encrypted segment"""
        if not self.encryption_key:
            return data
        
        # Log key info on first segment
        if segment_index == 0:
            logger.info(f"Encryption key (first 4 bytes): {self.encryption_key[:4].hex()}")
            if self.encryption_iv is not None:
                logger.info(f"Using provided IV: {self.encryption_iv.hex()}")
            else:
                logger.info("No IV provided, will use segment index")
        
        # Check if data is already valid TS content (not encrypted despite m3u8 claim)
        # Some CDNs or caching layers decrypt content server-side
        if data[:1] == TS_SYNC_BYTE:
            if segment_index == 0:
                logger.info("Segment 0: Data already appears to be valid TS (starts with sync byte), skipping decryption")
            return data
        
        # AES-128-CBC requires input to be a multiple of 16 bytes
        # If data isn't aligned, it's likely not encrypted or is corrupted
        if len(data) % 16 != 0:
            if segment_index == 0:
                logger.warning(f"Segment 0: Data length ({len(data)}) is not 16-byte aligned - content may not be encrypted")
            # Pad the data to attempt decryption anyway
            padding_needed = 16 - (len(data) % 16)
            padded_data = data + bytes(padding_needed)
        else:
            padded_data = data
        
        try:
            # Try multiple IV strategies
            iv_strategies = []
            
            # Strategy 1: Use provided IV if specified (HLS spec compliant)
            if self.encryption_iv is not None:
                iv_strategies.append(("provided IV", self.encryption_iv))
            
            # Strategy 2: Use segment index as IV (common non-compliant streams)
            iv_strategies.append(("segment index IV", segment_index.to_bytes(16, byteorder='big')))
            
            # Strategy 3: Use zeros IV if not already tried
            if self.encryption_iv is None or self.encryption_iv != bytes(16):
                iv_strategies.append(("zeros IV", bytes(16)))
            
            decrypted = None
            for strategy_name, iv in iv_strategies:
                cipher = AES.new(self.encryption_key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(padded_data)
                
                # Remove PKCS7 padding
                try:
                    decrypted = unpad(decrypted, AES.block_size)
                except ValueError:
                    # Some streams don't use proper padding
                    pass
                
                # Check if decryption produced valid TS data
                if decrypted[:1] == TS_SYNC_BYTE:
                    if segment_index < 3:  # Log first few segments
                        logger.info(f"Segment {segment_index}: Decryption successful with {strategy_name}")
                    return decrypted
            
            # None of the strategies worked
            logger.warning(f"Segment {segment_index}: All decryption strategies failed (first byte after zeros IV: {hex(decrypted[0]) if decrypted else 'empty'})")
            
            # Return the last decrypted result (with zeros IV) - let ffmpeg try to handle it
            return decrypted
            
        except Exception as e:
            logger.warning(f"Decryption failed for segment {segment_index}: {e}")
            return data  # Return original data if decryption fails

    def _get_key_bytes(self, key_url: str) -> bytes:
        """Fetch AES-128 key bytes with caching (thread-safe)."""
        with self._key_cache_lock:
            cached = self._key_cache.get(key_url)
        if cached is not None:
            return cached

        response = self.session.get(
            key_url,
            headers=self.headers,
            timeout=self.timeout,
            stream=False,
        )
        response.raise_for_status()
        key = response.content or b""
        if len(key) != 16:
            raise ValueError(f"Unexpected AES-128 key length: {len(key)} bytes (expected 16)")

        # Diagnostic: a real AES-128 key is 16 random binary bytes. If the
        # endpoint returned 16 PRINTABLE ASCII chars instead (e.g. a hex
        # string truncated to 16 chars), every segment will decrypt to
        # garbage even though length passes the check above. Log the full
        # hex + Content-Type so we can tell at a glance whether the key
        # is real or text. Loud WARNING when it looks like ASCII.
        content_type = ''
        try:
            content_type = response.headers.get('Content-Type', '') or ''
        except Exception:
            content_type = ''
        is_printable_ascii = all(0x20 <= b <= 0x7E for b in key)
        logger.info(
            f"Key fetched from {key_url.split('?', 1)[0]}: "
            f"Content-Type={content_type!r}, len={len(key)}, hex={key.hex()}"
        )
        if is_printable_ascii:
            try:
                as_text = key.decode('ascii', errors='replace')
            except Exception:
                as_text = repr(key)
            logger.warning(
                f"AES-128 key looks like printable ASCII text "
                f"({as_text!r}) — endpoint may be returning a hex string "
                f"or other text instead of binary bytes. If decryption "
                f"output looks wrong, check the key endpoint response."
            )

        with self._key_cache_lock:
            self._key_cache[key_url] = key
        return key

    def _decrypt_segment_with_key(
        self,
        data: bytes,
        segment_index: int,
        key_bytes: bytes,
        iv_bytes: Optional[bytes],
        sequence_number: Optional[int],
    ) -> bytes:
        """Decrypt AES-128 encrypted segment with per-segment key/iv metadata."""
        if not key_bytes:
            return data

        if segment_index == 0:
            logger.info(f"Encryption key (first 4 bytes): {key_bytes[:4].hex()}")
            if iv_bytes is not None:
                logger.info(f"Using provided IV: {iv_bytes.hex()}")
            else:
                logger.info("No IV provided, will use segment sequence/index")

        # If it's already valid TS, skip decryption entirely.
        is_ts, _ = self._is_valid_ts_content(data)
        if is_ts:
            if segment_index == 0:
                logger.info("Segment 0: Data already appears to be valid TS, skipping decryption")
            return data

        # AES-128-CBC requires input to be a multiple of 16 bytes
        if len(data) % 16 != 0:
            padding_needed = 16 - (len(data) % 16)
            padded_data = data + bytes(padding_needed)
        else:
            padded_data = data

        try:
            iv_strategies = []

            if iv_bytes is not None:
                iv_strategies.append(("provided IV", iv_bytes))

            # HLS default IV is the media sequence number (big-endian 128-bit)
            if sequence_number is not None:
                iv_strategies.append(("sequence IV", int(sequence_number).to_bytes(16, byteorder="big")))

            # Fallback: segment index
            iv_strategies.append(("segment index IV", int(segment_index).to_bytes(16, byteorder="big")))

            # Fallback: zeros
            iv_strategies.append(("zeros IV", bytes(16)))

            last = None
            for strategy_name, iv in iv_strategies:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(padded_data)
                last = decrypted
                try:
                    decrypted = unpad(decrypted, AES.block_size)
                except ValueError:
                    pass

                if decrypted[:1] == TS_SYNC_BYTE:
                    if segment_index < 3:
                        logger.info(f"Segment {segment_index}: Decryption successful with {strategy_name}")
                    return decrypted

            logger.warning(
                f"Segment {segment_index}: All decryption strategies failed "
                f"(first byte after zeros IV: {hex(last[0]) if last else 'empty'})"
            )
            return last or data
        except Exception as e:
            logger.warning(f"Decryption failed for segment {segment_index}: {e}")
            return data
    
    def _get_referer_strategies(self, segment_url: str) -> List[Dict[str, str]]:
        """
        Generate different Referer/Origin header combinations to try.
        Some CDNs have specific requirements for these headers.
        """
        strategies = []
        
        # Parse URLs for building strategies
        segment_parsed = urlparse(segment_url)
        segment_origin = f"{segment_parsed.scheme}://{segment_parsed.netloc}"
        
        original_referer = self.headers.get('Referer', '')
        original_origin = self.headers.get('Origin', '')
        
        # Strategy 1: Original headers (source page as Referer) - already tried
        strategies.append({
            'name': 'source_page',
            'Referer': original_referer,
            'Origin': original_origin
        })
        
        # Strategy 2: Use segment's own domain as Referer (same-origin request simulation)
        strategies.append({
            'name': 'segment_domain',
            'Referer': segment_origin + '/',
            'Origin': segment_origin
        })
        
        # Strategy 3: Use m3u8 URL as Referer
        if self.m3u8_url:
            m3u8_parsed = urlparse(self.m3u8_url)
            m3u8_origin = f"{m3u8_parsed.scheme}://{m3u8_parsed.netloc}"
            strategies.append({
                'name': 'm3u8_url',
                'Referer': self.m3u8_url,
                'Origin': m3u8_origin
            })
        
        # Strategy 4: No Referer/Origin (some servers allow this)
        strategies.append({
            'name': 'no_referer',
            'Referer': None,
            'Origin': None
        })
        
        return strategies
    
    def _try_download_with_headers(self, url: str, headers: Dict, index: int) -> Optional[bytes]:
        """
        Try downloading a segment with specific headers.

        Returns:
            bytes on success.
            None when an HTTP response was received but indicates an
            application-level rejection (4xx/5xx, 474, anti-hotlink image,
            HTML/JSON block page, body too small). Caller should try a
            different Referer/Origin strategy.

        Raises:
            Transport errors (Timeout, ConnectionError, etc. — see
            _TRANSPORT_ERRORS at module level). Caller must NOT switch
            strategies in this case: the host is throttling or unreachable
            and trying alternate Referers against the same host just adds
            pressure. Let the outer retry+backoff in download_segment
            handle recovery.
        """
        # Acquire a per-host slot if cross-process throttle is enabled. Held
        # for the entire request (including response.content read) — we don't
        # release mid-transfer because a partial download still occupies a
        # connection slot at the CDN. Released in the outer finally below.
        throttle = _host_throttle.get() if _host_throttle is not None else None
        slot_acquired = throttle.acquire(url) if throttle is not None else False
        try:
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    stream=False
                )

                # Log response cookies for debugging
                if response.cookies and index == 0:
                    logger.info(f"Response set cookies: {dict(response.cookies)}")

                if response.status_code == 474:
                    logger.debug(f"Segment {index} got 474 error with current headers")
                    return None

                response.raise_for_status()
                content = response.content

                # Early content-type based blocking detection
                content_type = ""
                try:
                    content_type = response.headers.get("Content-Type", "")
                except Exception:
                    content_type = ""
                blocked, _reason = self._is_obviously_blocked_response(content, content_type=content_type)
                if blocked:
                    return None

                if len(content) < 188:
                    return None

                # Check if response is an anti-hotlink image
                if content[:3] == JPEG_MAGIC or content[:4] == PNG_MAGIC or content[:4] == GIF_MAGIC:
                    return None

                return content

            except _TRANSPORT_ERRORS:
                # Network-layer failure (connect timeout, RST, partial-body
                # timeout). Re-raise so caller skips remaining Referer
                # strategies and falls into outer retry+backoff. Switching
                # strategies against a throttled host just adds pressure.
                raise
            except Exception as e:
                # HTTP-level failure (raise_for_status on 4xx/5xx, etc.) —
                # an alternate Referer might succeed.
                logger.debug(f"Segment {index} download attempt failed (HTTP/app level): {e}")
                return None
        finally:
            if slot_acquired and throttle is not None:
                throttle.release(url)
    
    def download_segment(
        self, 
        segment: Dict, 
        retry_count: int = 0
    ) -> Optional[str]:
        """
        Download a single segment with multiple Referer strategies
        
        Args:
            segment: Segment info dict with 'url', 'index'
            retry_count: Current retry attempt
        
        Returns:
            Path to downloaded file or None if failed
        """
        # Check if stop was requested before starting
        if self._stop_event.is_set():
            logger.debug(f"Segment {segment['index']} skipped - stop requested")
            return None
        
        url = segment['url']
        index = segment['index']
        output_path = self.output_dir / f"segment_{index:05d}.ts"
        
        try:
            logger.debug(f"Downloading segment {index}: {url}")
            
            # Log headers for first segment
            if index == 0 and retry_count == 0:
                logger.info(f"Segment download headers: {self.headers}")
                logger.info(f"First segment URL: {url}")
            
            content = None
            used_strategy = None
            
            # If we already found a working strategy, use it directly
            if self.working_referer_strategy and retry_count == 0:
                strategy = self.working_referer_strategy
                headers = self.headers.copy()
                if strategy.get('Referer'):
                    headers['Referer'] = strategy['Referer']
                elif 'Referer' in headers and strategy.get('Referer') is None:
                    del headers['Referer']
                if strategy.get('Origin'):
                    headers['Origin'] = strategy['Origin']
                elif 'Origin' in headers and strategy.get('Origin') is None:
                    del headers['Origin']

                # _try_download_with_headers re-raises on transport errors
                # (RST/timeout) so this branch only fires on application-level
                # rejections (4xx/5xx/474/anti-hotlink). On transport errors
                # the cached strategy is still correct — the host is throttled,
                # not the Referer wrong — and the exception propagates to the
                # outer retry+backoff without invalidating the cache.
                content = self._try_download_with_headers(url, headers, index)
                if content:
                    used_strategy = strategy['name']
                else:
                    # Application-level rejection (token expired / Referer
                    # newly required). Drop the cache so the strategy loop
                    # below can re-probe.
                    logger.warning(
                        f"Cached Referer strategy '{strategy['name']}' got an "
                        f"application-level rejection (segment {index}); "
                        f"invalidating. Likely a signed-URL/token expiry."
                    )
                    self.working_referer_strategy = None
            
            # If no working strategy yet, or it failed, try all strategies
            if content is None:
                strategies = self._get_referer_strategies(url)
                
                for strategy in strategies:
                    # Check if stop was requested between strategy attempts
                    if self._stop_event.is_set():
                        logger.debug(f"Segment {index} aborted during strategy attempts - stop requested")
                        return None
                    
                    headers = self.headers.copy()
                    
                    # Apply strategy headers
                    if strategy.get('Referer'):
                        headers['Referer'] = strategy['Referer']
                    elif 'Referer' in headers and strategy.get('Referer') is None:
                        del headers['Referer']
                    
                    if strategy.get('Origin'):
                        headers['Origin'] = strategy['Origin']
                    elif 'Origin' in headers and strategy.get('Origin') is None:
                        del headers['Origin']
                    
                    if index == 0 and retry_count == 0:
                        logger.info(f"Trying Referer strategy: {strategy['name']}")
                    
                    content = self._try_download_with_headers(url, headers, index)
                    
                    if content:
                        used_strategy = strategy['name']
                        # Remember this strategy for future segments
                        if self.working_referer_strategy is None:
                            logger.info(f"Found working Referer strategy: {strategy['name']}")
                            self.working_referer_strategy = strategy
                        break
            
            # If all strategies failed, use original headers and let the error handling below deal with it
            if content is None:
                response = self.session.get(
                    url,
                    headers=self.headers,
                    timeout=self.timeout,
                    stream=False
                )
                
                if response.status_code == 474:
                    logger.error(f"Segment {index} got 474 error")
                    logger.error(f"Response headers: {dict(response.headers)}")
                    error_content = response.text[:500] if hasattr(response, 'text') else "No content"
                    logger.error(f"Error content: {error_content}")
                
                response.raise_for_status()
                content = response.content

                content_type = ""
                try:
                    content_type = response.headers.get("Content-Type", "")
                except Exception:
                    content_type = ""
                blocked, reason = self._is_obviously_blocked_response(content, content_type=content_type)
                if blocked:
                    raise ValueError(reason)
                
                if len(content) < 188:
                    raise ValueError(f"Segment too small: {len(content)} bytes")

            # Always check for obvious block/HTML responses BEFORE decryption.
            # If we decrypt first, block pages become random bytes and may slip through.
            blocked, reason = self._is_obviously_blocked_response(content)
            if blocked:
                raise ValueError(reason)
            
            # Decrypt (supports per-segment rotating keys via segment['key'])
            segment_key = segment.get("key") if isinstance(segment, dict) else None
            if segment_key and isinstance(segment_key, dict) and segment_key.get("method") == "AES-128":
                key_url = segment_key.get("uri")
                if not key_url:
                    raise ValueError("Encrypted segment missing key URI")
                key_bytes = self._get_key_bytes(key_url)
                content = self._decrypt_segment_with_key(
                    content,
                    index,
                    key_bytes=key_bytes,
                    iv_bytes=segment_key.get("iv"),
                    sequence_number=segment.get("sequence"),
                )
            elif self.encryption_key:
                content = self._decrypt_segment(content, index)
            
            # Validate content is actually a TS file (not an error page)
            is_valid, error_reason = self._is_valid_ts_content(content)
            if not is_valid:
                skip_validation = os.environ.get('SKIP_TS_VALIDATION', 'false').lower() == 'true'
                
                # For encrypted streams, do NOT blindly save invalid decrypted bytes.
                # This usually indicates the key/iv is wrong or the server served a block page.
                if (self.encryption_key or (segment_key and isinstance(segment_key, dict) and segment_key.get("method") == "AES-128")) and not skip_validation:
                    preview = content[:200]
                    logger.error(f"Segment {index}: {error_reason}")
                    logger.error(f"Content preview (first 200 bytes): {preview}")
                    raise ValueError(error_reason)
                elif skip_validation:
                    logger.warning(f"Segment {index}: {error_reason} - validation skipped")
                else:
                    preview = content[:200]
                    logger.error(f"Segment {index}: {error_reason}")
                    logger.error(f"Content preview (first 200 bytes): {preview}")
                    raise ValueError(error_reason)
            
            # Write validated content to file
            with open(output_path, 'wb') as f:
                f.write(content)
            
            if index == 0 and used_strategy:
                logger.info(f"Segment {index} downloaded successfully with strategy: {used_strategy}")
            else:
                logger.debug(f"Segment {index} downloaded and validated successfully ({len(content)} bytes)")
            
            return str(output_path)
        
        except Exception as e:
            err_str = str(e)
            logger.warning(f"Failed to download segment {index} (attempt {retry_count + 1}): {err_str}")

            # Check if stop was requested before retrying
            if self._stop_event.is_set():
                logger.debug(f"Segment {index} retry cancelled - stop requested")
                return None

            # Skip retries when the CDN returned an anti-hotlink placeholder
            # (PNG/JPEG/GIF) on every Referer strategy. Same session + same URL +
            # same auth on retry → same PNG. Retrying just wastes ~16 requests
            # and delays the abort threshold by ~4 seconds. Let it fail now so
            # the worker's hotlink-count guard trips quickly and the user gets
            # the Re-fetch prompt.
            if 'anti-hotlinking' in err_str.lower():
                logger.error(f"Segment {index} hit anti-hotlink response; not retrying (retries cannot recover an expired CDN token)")
                self.failed_segments.append({'segment': segment, 'error': err_str})
                return None

            # Retry logic — exponential backoff with full jitter so N segments
            # that failed simultaneously (typical CDN-throttle pattern) don't
            # all wake up at the same moment and burst-retry against the still-
            # throttled host. Sleep range: [base, 2*base) where base = 2^retry.
            if retry_count < self.max_retries:
                base = 2 ** retry_count
                time.sleep(base + random.uniform(0, base))
                return self.download_segment(segment, retry_count + 1)
            else:
                logger.error(f"Segment {index} failed after {self.max_retries} attempts")
                self.failed_segments.append({'segment': segment, 'error': err_str})
                return None
    
    def download_all(
        self, 
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> List[str]:
        """
        Download all segments with multi-threading
        
        Args:
            progress_callback: Optional callback function(completed, total)
        
        Returns:
            List of downloaded file paths
        """
        logger.info(f"Starting download of {self.total_segments} segments with {self.max_workers} workers")
        
        downloaded_files = [None] * self.total_segments
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all download tasks
            future_to_segment = {
                executor.submit(self.download_segment, segment): segment
                for segment in self.segments
            }
            
            # Process completed downloads
            try:
                for future in as_completed(future_to_segment):
                    # Check if stop was requested before processing more results
                    if self._stop_event.is_set():
                        logger.info("Stop event detected in download_all, aborting...")
                        for f in future_to_segment:
                            f.cancel()
                        break
                    
                    segment = future_to_segment[future]
                    index = segment['index']
                    
                    try:
                        file_path = future.result()
                        if file_path:
                            downloaded_files[index] = file_path
                            self.downloaded_count += 1
                    
                    except Exception as e:
                        logger.error(f"Unexpected error downloading segment {index}: {e}")
                        self.failed_segments.append({'segment': segment, 'error': str(e)})
                    
                    # Call progress callback (outside try-except so callback exceptions propagate)
                    if progress_callback:
                        progress_callback(self.downloaded_count, self.total_segments)
            
            except Exception as e:
                # Callback raised an exception (e.g., job cancelled or too many errors)
                # Signal all threads to stop and cancel pending futures
                logger.warning("Download aborted, signaling stop and cancelling remaining tasks...")
                self._stop_event.set()
                for future in future_to_segment:
                    future.cancel()
                # Re-raise the exception
                raise
        
        # Filter out None values (failed downloads)
        successful_files = [f for f in downloaded_files if f is not None]
        
        logger.info(f"Download complete: {len(successful_files)}/{self.total_segments} segments successful")
        
        if self.failed_segments:
            logger.warning(f"Failed segments: {len(self.failed_segments)}")
        
        return successful_files
    
    def get_progress(self) -> Dict:
        """Get download progress information"""
        return {
            'downloaded': self.downloaded_count,
            'total': self.total_segments,
            'percentage': int((self.downloaded_count / self.total_segments) * 100),
            'failed': len(self.failed_segments)
        }
    
    def cleanup(self):
        """Remove downloaded segment files"""
        try:
            logger.info("Cleaning up segment files")
            for file in self.output_dir.glob("segment_*.ts"):
                file.unlink()
            
            # Try to remove directory if empty
            try:
                self.output_dir.rmdir()
            except OSError:
                pass  # Directory not empty or doesn't exist
        
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")


def download_segments(
    segments: List[Dict],
    output_dir: str,
    headers: Optional[Dict] = None,
    max_workers: int = 10,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    encryption_key: Optional[bytes] = None,
    encryption_iv: Optional[bytes] = None,
    m3u8_url: Optional[str] = None,
    session=None
) -> List[str]:
    """
    Convenience function to download segments
    
    Args:
        segments: List of segment dicts
        output_dir: Directory to save segments
        headers: Optional HTTP headers
        max_workers: Number of concurrent download threads
        progress_callback: Optional callback(completed, total)
        encryption_key: Optional AES-128 encryption key
        encryption_iv: Optional AES-128 IV
        m3u8_url: Optional m3u8 URL (for Referer strategy)
        session: Optional requests session (for cookie persistence)
    
    Returns:
        List of downloaded file paths
    """
    downloader = SegmentDownloader(
        segments=segments,
        output_dir=output_dir,
        headers=headers,
        max_workers=max_workers,
        encryption_key=encryption_key,
        encryption_iv=encryption_iv,
        m3u8_url=m3u8_url,
        session=session
    )
    
    return downloader.download_all(progress_callback)

