"""
HTTP Session factory with browser impersonation
Uses curl_cffi to mimic Chrome's TLS fingerprint (JA3)
This bypasses TLS fingerprinting-based anti-bot detection
"""

import logging
import ssl
import os

logger = logging.getLogger(__name__)

# Try to import curl_cffi for browser impersonation
try:
    from curl_cffi.requests import Session as CurlSession
    CURL_CFFI_AVAILABLE = True
    logger.info("curl_cffi available - browser TLS impersonation enabled")
except ImportError:
    CURL_CFFI_AVAILABLE = False
    logger.warning("curl_cffi not available - falling back to requests (may be blocked by TLS fingerprinting)")

# Fallback imports
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


# Cipher suite that includes legacy options for compatibility
LEGACY_CIPHERS = (
    'DEFAULT:!aNULL:!eNULL:!MD5:@SECLEVEL=1'
)

def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def tls_verify_enabled() -> bool:
    """
    Control TLS verification defaults.

    Default: verify enabled (secure).
    To disable verification (insecure), set either:
      - INSECURE_SKIP_TLS_VERIFY=1
      - SSL_VERIFY=0
    """
    if _env_flag("INSECURE_SKIP_TLS_VERIFY", False):
        return False
    if os.getenv("SSL_VERIFY") is not None:
        return _env_flag("SSL_VERIFY", True)
    return True


def _force_http1_1() -> bool:
    """
    Whether to force HTTP/1.1 instead of letting curl negotiate.

    Default: False — let ALPN negotiate (modern CDNs and real Chrome speak
    HTTP/2). Forcing HTTP/1.1 was a v1.x workaround for a CDN that returned
    'keep-alive' headers (illegal in HTTP/2). That CDN isn't relevant any
    more, and the force was actively counterproductive against anti-bot
    systems like phncdn that fingerprint on TLS+HTTP-version: a request
    with chrome TLS impersonation but HTTP/1.1 transport doesn't match any
    real browser, which is itself a bot signal.

    Set FORCE_HTTP1_1=true only if you hit a CDN that genuinely breaks H2.
    """
    return _env_flag("FORCE_HTTP1_1", False)


class LegacySSLAdapter(HTTPAdapter):
    """
    HTTPAdapter with custom SSL context that supports legacy ciphers.
    Useful for servers with non-standard TLS configurations.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        verify = tls_verify_enabled()
        ctx.check_hostname = verify
        ctx.verify_mode = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
        if verify:
            # Best-effort: ensure default CAs are available with custom context.
            try:
                ctx.load_default_certs()
            except Exception:
                pass
        ctx.set_ciphers(LEGACY_CIPHERS)
        # Enable legacy renegotiation for older servers (Python 3.12+)
        if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


class BrowserSession:
    """
    Wrapper around curl_cffi Session that mimics requests.Session interface.
    Uses Chrome TLS fingerprint to bypass anti-bot detection.
    """

    def __init__(self, impersonate: str = "chrome"):
        """
        Initialize a browser-like session.

        Args:
            impersonate: Browser to impersonate. Options:
                - "chrome" (latest Chrome)
                - "chrome110", "chrome116", "chrome120", etc.
                - "safari", "safari_ios"
                - "edge"
        """
        self.impersonate = impersonate
        self._session = CurlSession(impersonate=impersonate)
        self.cookies = self._session.cookies
        logger.debug(f"Created BrowserSession with impersonate={impersonate}")

    def _prepare_kwargs(self, kwargs):
        """Prepare request kwargs with defaults"""
        # TLS verification is secure-by-default; opt-out via env (see tls_verify_enabled()).
        if 'verify' not in kwargs:
            kwargs['verify'] = tls_verify_enabled()
        # HTTP version: default to ALPN negotiation (lets curl pick H2 for
        # CDNs that support it, which matches real Chrome behavior). The
        # legacy "force H1.1" path is opt-in via FORCE_HTTP1_1 env — see
        # _force_http1_1() docstring for the why.
        if 'http_version' not in kwargs and _force_http1_1():
            from curl_cffi.const import CurlHttpVersion
            kwargs['http_version'] = CurlHttpVersion.V1_1
        return kwargs

    def get(self, url, **kwargs):
        """Send GET request"""
        kwargs = self._prepare_kwargs(kwargs)
        return self._session.get(url, **kwargs)

    def post(self, url, **kwargs):
        """Send POST request"""
        kwargs = self._prepare_kwargs(kwargs)
        return self._session.post(url, **kwargs)

    def head(self, url, **kwargs):
        """Send HEAD request"""
        kwargs = self._prepare_kwargs(kwargs)
        return self._session.head(url, **kwargs)

    def request(self, method, url, **kwargs):
        """Send request with given method"""
        kwargs = self._prepare_kwargs(kwargs)
        return self._session.request(method, url, **kwargs)

    def close(self):
        """Close the session"""
        self._session.close()


def create_legacy_session():
    """
    Create a standard HTTP Session with legacy SSL support.
    This uses requests library which is more compatible with various servers.

    Use this for m3u8 parsing and general requests.
    For segment downloads that need anti-bot bypass, use create_browser_session().

    Returns:
        requests.Session with legacy SSL support
    """
    session = requests.Session()
    adapter = LegacySSLAdapter()
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def create_impersonated_session():
    """
    Create an HTTP Session with browser TLS fingerprint impersonation.

    Uses curl_cffi with Chrome impersonation to bypass TLS fingerprinting.
    Falls back to requests if curl_cffi is not available.

    Use this specifically for segment downloads that are blocked by anti-bot detection.

    Returns:
        Session object (BrowserSession or requests.Session)
    """
    if CURL_CFFI_AVAILABLE:
        logger.info("Creating impersonated session with curl_cffi (Chrome TLS fingerprint)")
        return BrowserSession(impersonate="chrome")
    else:
        logger.warning("curl_cffi not available, falling back to requests (may be blocked by TLS fingerprinting)")
        return create_legacy_session()


def create_browser_session(impersonate: str = "chrome"):
    """
    Create a session that impersonates a specific browser.

    Args:
        impersonate: Browser to impersonate (chrome, safari, edge)

    Returns:
        BrowserSession if curl_cffi available, else requests.Session
    """
    if CURL_CFFI_AVAILABLE:
        return BrowserSession(impersonate=impersonate)
    else:
        logger.warning(f"Cannot impersonate {impersonate} - curl_cffi not available")
        return create_legacy_session()
