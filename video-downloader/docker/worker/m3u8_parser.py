"""
M3U8 Parser
Parse m3u8 playlists and extract segment information
"""

import logging
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional
import m3u8
import urllib3
from ssl_adapter import create_legacy_session, tls_verify_enabled

if not tls_verify_enabled():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Check if brotli is available for requests
try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    logger.warning("brotli package not installed - removing 'br' from Accept-Encoding headers")


class M3U8Parser:
    """Parse m3u8 playlists and extract segment URLs"""
    
    def __init__(self, url: str, headers: Optional[Dict] = None, session=None):
        self.url = url
        self.headers = self._sanitize_headers(headers or {})
        self.base_url = self._get_base_url(url)
        # Use provided session to preserve cookies / TLS fingerprint across playlist+key+segments.
        self.session = session if session is not None else create_legacy_session()
    
    def _sanitize_headers(self, headers: Dict) -> Dict:
        """
        Sanitize request headers to prevent decompression issues.
        Removes 'br' (brotli) from Accept-Encoding if brotli is not available.
        """
        if BROTLI_AVAILABLE:
            return headers
        
        sanitized = headers.copy()
        accept_encoding = str(sanitized.get('Accept-Encoding', '') or '')
        
        if 'br' in accept_encoding.lower():
            # Remove 'br' from Accept-Encoding
            parts = [p.strip() for p in accept_encoding.split(',')]
            parts = [p for p in parts if p.lower() != 'br']
            if parts:
                sanitized['Accept-Encoding'] = ', '.join(parts)
            else:
                sanitized.pop('Accept-Encoding', None)
            logger.info(f"Sanitized Accept-Encoding: {accept_encoding} -> {sanitized.get('Accept-Encoding', '(removed)')}")
        
        return sanitized
        
    def _get_base_url(self, url: str) -> str:
        """Extract base URL from m3u8 URL"""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = parsed.path.rsplit('/', 1)
        if len(path_parts) > 1:
            base += path_parts[0] + '/'
        return base
    
    def fetch_playlist(self) -> str:
        """Fetch m3u8 playlist content with early validation"""
        try:
            logger.info(f"Fetching playlist: {self.url}")
            
            # NOTE: Use non-streaming reads for compatibility across session backends
            # (requests vs curl_cffi BrowserSession). m3u8 playlists should be small.
            response = self.session.get(
                self.url,
                headers=self.headers,
                timeout=30,
                allow_redirects=True,
                stream=False,
            )
            response.raise_for_status()
            
            # Check content-type header for early detection
            content_type = response.headers.get('Content-Type', '').lower()
            logger.info(f"Response Content-Type: {content_type}")
            
            # Warn if content-type suggests non-m3u8 content
            if content_type and not any(t in content_type for t in ['mpegurl', 'text', 'application/vnd.apple']):
                if 'video' in content_type or 'octet-stream' in content_type:
                    logger.warning(f"Content-Type suggests this is not an m3u8 playlist: {content_type}")
            
            # Check content-length to detect large files (likely not m3u8)
            content_length = response.headers.get('Content-Length')
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > 1:  # m3u8 playlists are typically < 1MB
                    logger.warning(f"Response is {size_mb:.1f}MB - likely not an m3u8 playlist")
                    raise ValueError(f"Response too large ({size_mb:.1f}MB) - this appears to be a video file, not an m3u8 playlist")
            
            raw = response.content or b""
            if not raw:
                raise ValueError("Empty response - not a valid m3u8 playlist")

            # Read first chunk to validate content
            first_chunk = raw[:8192]
            
            # Check if content is binary (not text)
            try:
                first_text = first_chunk.decode('utf-8')
            except UnicodeDecodeError:
                logger.error("Response is binary data, not an m3u8 playlist")
                raise ValueError("Response is binary data - this appears to be a video file, not an m3u8 playlist")
            
            # Check if it starts with #EXTM3U (required for m3u8)
            if not first_text.strip().startswith('#EXTM3U'):
                # Check for common binary signatures
                if first_chunk[:4] in (b'\x00\x00\x00\x1c', b'\x00\x00\x00\x18', b'\x00\x00\x00\x20'):  # MP4 ftyp
                    raise ValueError("Response is an MP4 file, not an m3u8 playlist")
                if first_chunk[:3] == b'\xff\xd8\xff':  # JPEG
                    raise ValueError("Response is a JPEG image, not an m3u8 playlist")
                if first_chunk[:4] == b'\x89PNG':  # PNG
                    raise ValueError("Response is a PNG image, not an m3u8 playlist")
                
                # Log first 200 chars for debugging
                preview = first_text[:200] if len(first_text) > 200 else first_text
                logger.warning(f"Content doesn't start with #EXTM3U: {preview}")
            
            # Decode full content (with reasonable limit)
            max_size = 10 * 1024 * 1024  # 10MB max for m3u8
            if len(raw) > max_size:
                raise ValueError(f"Response exceeds {max_size // 1024 // 1024}MB limit - not a valid m3u8 playlist")

            return raw.decode("utf-8")
            
        except Exception as e:
            logger.error(f"Failed to fetch playlist: {e}")
            raise
    
    def parse(self) -> Dict:
        """
        Parse m3u8 playlist and return segment information
        
        Returns:
            Dict with keys:
                - segments: List of segment URLs
                - duration: Total duration in seconds
                - is_variant: Whether this is a master playlist
                - resolution: Video resolution if available
        """
        try:
            # Fetch playlist content
            content = self.fetch_playlist()
            
            # Log first 500 chars of content to diagnose parsing issues
            content_preview = content[:500] if len(content) > 500 else content
            logger.info(f"Playlist content preview ({len(content)} bytes):\n{content_preview}")
            
            # Parse with m3u8 library
            playlist = m3u8.loads(content, uri=self.url)
            
            # Check if this is a master playlist (with variants)
            if playlist.is_variant:
                logger.info("Master playlist detected, selecting best quality")
                return self._parse_master_playlist(playlist, content)
            else:
                logger.info("Media playlist detected")
                # Debug: log segment count before parsing
                logger.debug(f"Raw playlist has {len(playlist.segments)} segments, {len(playlist.playlists)} playlists")
                return self._parse_media_playlist(playlist, content)
        
        except Exception as e:
            logger.error(f"Failed to parse m3u8: {e}")
            raise
    
    def _parse_master_playlist(self, playlist: m3u8.M3U8, content: str = None) -> Dict:
        """Parse master playlist and select best quality variant"""
        if not playlist.playlists:
            raise ValueError("No variants found in master playlist")
        
        # Sort by bandwidth (quality) and select highest
        variants = sorted(
            playlist.playlists, 
            key=lambda p: p.stream_info.bandwidth,
            reverse=True
        )
        
        best_variant = variants[0]
        logger.info(f"Selected variant: {best_variant.stream_info.bandwidth} bps")
        
        # Get resolution if available
        resolution = None
        if best_variant.stream_info.resolution:
            width, height = best_variant.stream_info.resolution
            resolution = f"{width}x{height}"
        
        # Get absolute URL for variant playlist
        variant_url = urljoin(self.url, best_variant.uri)
        
        # Parse the selected variant (media playlist)
        variant_parser = M3U8Parser(variant_url, self.headers, session=self.session)
        variant_content = variant_parser.fetch_playlist()
        variant_playlist = m3u8.loads(variant_content, uri=variant_url)
        
        result = self._parse_media_playlist(variant_playlist, variant_content)
        result['resolution'] = resolution
        result['selected_variant_url'] = variant_url
        
        return result
    
    def _parse_media_playlist(self, playlist: m3u8.M3U8, content: str = None) -> Dict:
        """Parse media playlist and extract segment URLs"""
        segments = []
        total_duration = 0.0

        media_sequence = getattr(playlist, "media_sequence", 0) or 0

        # HLS-fMP4 / CMAF: an #EXT-X-MAP box declares an init segment that
        # carries the moov/ftyp boxes the media segments need to decode.
        # The m3u8 lib exposes it via segment.init_section (same object
        # forwards across segments until a new #EXT-X-MAP appears). We only
        # capture the FIRST init segment URL — multi-init streams are rare
        # and would need per-segment init handling that we don't do today.
        init_segment_url: Optional[str] = None
        first_init = playlist.segments[0].init_section if playlist.segments else None
        if first_init is not None and first_init.uri:
            init_segment_url = urljoin(playlist.base_uri or self.url, first_init.uri)
            logger.info(f"Detected fMP4 init segment: {init_segment_url.split('?', 1)[0]}")
        
        for segment in playlist.segments:
            # Get absolute URL for segment
            segment_url = urljoin(playlist.base_uri or self.url, segment.uri)

            # Capture per-segment encryption metadata (keys can rotate within a playlist)
            key_info = None
            if segment.key and segment.key.method == "AES-128" and segment.key.uri:
                key_url = urljoin(playlist.base_uri or self.url, segment.key.uri)

                iv = None
                if segment.key.iv:
                    iv_str = segment.key.iv
                    if isinstance(iv_str, str):
                        try:
                            if iv_str.startswith("0x") or iv_str.startswith("0X"):
                                iv = bytes.fromhex(iv_str[2:])
                            else:
                                iv = bytes.fromhex(iv_str)
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Invalid IV value in m3u8, ignoring IV: {iv_str!r} ({e})")
                            iv = None

                key_info = {
                    "method": "AES-128",
                    "uri": key_url,
                    "iv": iv,
                }
            
            segments.append({
                'url': segment_url,
                'duration': segment.duration,
                'index': len(segments),
                # HLS sequence number is used for default IV when EXT-X-KEY has no IV
                'sequence': media_sequence + len(segments),
                'key': key_info,
            })
            
            total_duration += segment.duration
        
        if not segments:
            # Log the actual content for debugging
            if content:
                content_preview = content[:1000] if len(content) > 1000 else content
                logger.error(f"Playlist content (no segments found):\n{content_preview}")
            raise ValueError("No segments found in playlist")
        
        logger.info(f"Found {len(segments)} segments, total duration: {total_duration:.1f}s")
        
        # Check if encrypted and get encryption info
        encryption_info = self._get_encryption_info(playlist)
        has_encryption = encryption_info is not None
        
        if has_encryption:
            logger.info(f"Playlist is encrypted with {encryption_info['method']}")
        
        # Heuristic: if either an init segment is present OR the first segment
        # URL has an fMP4 extension, we treat the whole playlist as fMP4.
        # Downloader uses this to skip the TS sync-byte validation, and
        # ffmpeg_wrapper uses it to switch the stdin format flag.
        is_fmp4 = init_segment_url is not None or any(
            seg['url'].split('?', 1)[0].lower().endswith(('.m4s', '.mp4', '.cmfv', '.cmfa'))
            for seg in segments[:1]
        )

        return {
            'segments': segments,
            'duration': int(total_duration),
            'segment_count': len(segments),
            'is_variant': False,
            'has_encryption': has_encryption,
            'encryption_key_uri': encryption_info.get('key_uri') if encryption_info else None,
            'encryption_iv': encryption_info.get('iv') if encryption_info else None,
            'base_url': playlist.base_uri or self.url,
            'is_fmp4': is_fmp4,
            'init_segment_url': init_segment_url,
        }
    
    def _get_encryption_info(self, playlist: m3u8.M3U8) -> Optional[Dict]:
        """Get encryption key URI and IV if playlist is encrypted (per-segment keys may rotate)"""
        for segment in playlist.segments:
            if segment.key and segment.key.method == 'AES-128':
                try:
                    if not segment.key.uri:
                        return None
                    key_url = urljoin(playlist.base_uri or self.url, segment.key.uri)
                    logger.info(f"Found encryption key URI: {key_url}")
                    
                    # Get IV from key info or use default
                    iv = None
                    if segment.key.iv:
                        # IV is usually specified as hex string like 0x...
                        iv_str = segment.key.iv
                        logger.info(f"IV from m3u8: {iv_str}")
                        if iv_str.startswith('0x') or iv_str.startswith('0X'):
                            iv = bytes.fromhex(iv_str[2:])
                        else:
                            iv = bytes.fromhex(iv_str)
                        logger.info(f"Parsed IV length: {len(iv)} bytes, value: {iv.hex()}")
                    else:
                        logger.info("No IV specified in m3u8, will use segment sequence number")
                    
                    return {
                        'method': 'AES-128',
                        'key_uri': key_url,
                        'iv': iv
                    }
                except Exception as e:
                    logger.error(f"Failed to read encryption info: {e}")
                    return None
        return None
    
    def get_encryption_key(self, playlist: m3u8.M3U8) -> Optional[bytes]:
        """Get encryption key if playlist is encrypted (deprecated, use _get_encryption_info)"""
        info = self._get_encryption_info(playlist)
        return info.get('key') if info else None


def parse_m3u8(url: str, headers: Optional[Dict] = None, session=None) -> Dict:
    """
    Convenience function to parse m3u8 URL
    
    Args:
        url: M3U8 playlist URL
        headers: Optional HTTP headers
    
    Returns:
        Dict with segment information
    """
    parser = M3U8Parser(url, headers, session=session)
    return parser.parse()

