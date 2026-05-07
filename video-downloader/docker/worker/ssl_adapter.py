"""Backward-compat shim. Implementation moved to `shared.ssl` in v2.5.

Worker code (worker.py, downloader.py, m3u8_parser.py) imports via
`from ssl_adapter import ...`. The shim keeps those imports valid while
the api role can `from shared.ssl import ...` directly.
"""
from shared.ssl import (  # noqa: F401
    BrowserSession,
    LegacySSLAdapter,
    LEGACY_CIPHERS,
    create_legacy_session,
    create_impersonated_session,
    create_browser_session,
    tls_verify_enabled,
    CURL_CFFI_AVAILABLE,
    _force_http1_1,
    _env_flag,
)
