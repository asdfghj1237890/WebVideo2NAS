"""
Smoke tests for the worker requirements upgrade. Each test pokes the surface
that worker.py / downloader.py / m3u8_parser.py / ssl_adapter.py actually
relies on, so a breaking change in redis 5→7, curl_cffi 0.7→0.15, m3u8 3→6,
or pycryptodome 3.19→3.23 would surface here instead of in production.
"""
import importlib


def _ge(actual: str, minimum: tuple[int, ...]) -> bool:
    parts = []
    for piece in actual.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) >= len(minimum):
            break
    return tuple(parts[: len(minimum)]) >= minimum


def test_dependency_versions_meet_minimums():
    """Some packages (e.g. m3u8) don't expose __version__, so use the installed
    distribution metadata — that's what the lockfile actually pins anyway."""
    from importlib.metadata import version as dist_version

    assert _ge(dist_version("redis"), (7, 4))
    assert _ge(dist_version("requests"), (2, 33))
    assert _ge(dist_version("m3u8"), (6, 0))
    assert _ge(dist_version("curl_cffi"), (0, 15))
    assert _ge(dist_version("pycryptodome"), (3, 23))
    assert _ge(dist_version("sqlalchemy"), (2, 0, 49))
    assert _ge(dist_version("psycopg2-binary"), (2, 9, 12))
    assert _ge(dist_version("python-dotenv"), (1, 2))
    assert _ge(dist_version("brotli"), (1, 2))


def test_redis_client_constructs_and_exposes_used_methods():
    """worker.py uses from_url, blpop, rpush, ping, exceptions.ConnectionError."""
    import redis

    client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    for name in ("blpop", "rpush", "ping"):
        assert callable(getattr(client, name)), name
    assert issubclass(redis.exceptions.ConnectionError, Exception)


def test_curl_cffi_browser_session_constructs():
    """ssl_adapter.BrowserSession wraps curl_cffi.requests.Session(impersonate=...)."""
    from curl_cffi.requests import Session as CurlSession

    sess = CurlSession(impersonate="chrome")
    try:
        for name in ("get", "post", "head", "request", "close"):
            assert callable(getattr(sess, name)), name
        assert sess.cookies is not None
    finally:
        sess.close()


def test_curl_cffi_http_version_constant_still_exposed():
    """ssl_adapter.BrowserSession._prepare_kwargs imports CurlHttpVersion.V1_1 at runtime."""
    from curl_cffi.const import CurlHttpVersion

    assert hasattr(CurlHttpVersion, "V1_1")


def test_m3u8_loads_and_segment_interface():
    """m3u8_parser._parse_media_playlist relies on this exact attribute set."""
    import m3u8

    content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:5
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="key.key",IV=0x000102030405060708090a0b0c0d0e0f
#EXTINF:6,
seg0.ts
#EXTINF:6,
seg1.ts
#EXT-X-ENDLIST
"""
    pl = m3u8.loads(content, uri="https://cdn.example.com/v/p.m3u8")
    assert pl.is_variant is False
    assert pl.media_sequence == 5
    assert pl.base_uri  # used as the join base for segment URIs

    seg = pl.segments[0]
    assert seg.uri == "seg0.ts"
    assert seg.duration == 6
    assert seg.key.method == "AES-128"
    assert seg.key.uri == "key.key"
    assert seg.key.iv.lower().startswith("0x")


def test_m3u8_master_playlist_variant_interface():
    """_parse_master_playlist sorts on stream_info.bandwidth and reads .resolution + .uri."""
    import m3u8

    content = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080
high.m3u8
"""
    pl = m3u8.loads(content, uri="https://cdn.example.com/v/master.m3u8")
    assert pl.is_variant is True
    assert len(pl.playlists) == 2
    best = sorted(pl.playlists, key=lambda p: p.stream_info.bandwidth, reverse=True)[0]
    assert best.uri == "high.m3u8"
    assert best.stream_info.resolution == (1920, 1080)


def test_pycryptodome_aes_cbc_roundtrip():
    """downloader._decrypt_segment_with_key uses AES.new(MODE_CBC) + AES.block_size + unpad."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad

    assert AES.block_size == 16
    key = b"0" * 16
    iv = b"1" * 16
    plain = b"\x47" + b"hello-world-test"
    padded = pad(plain, AES.block_size)

    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(padded)
    dec = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(enc), AES.block_size)
    assert dec == plain


def test_requests_session_with_legacy_adapter():
    """ssl_adapter.create_legacy_session mounts a custom HTTPAdapter on a Session."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.ssl_ import create_urllib3_context
    import urllib3

    s = requests.Session()
    s.mount("https://", HTTPAdapter())
    assert "https://" in s.adapters
    assert create_urllib3_context() is not None
    # urllib3 surfaces used by m3u8_parser at import time
    assert hasattr(urllib3, "disable_warnings")
    assert hasattr(urllib3.exceptions, "InsecureRequestWarning")


def test_brotli_module_decompress_roundtrip():
    """m3u8_parser conditionally imports brotli; verify it's actually functional."""
    import brotli

    assert brotli.decompress(brotli.compress(b"hello")) == b"hello"


def test_dotenv_load_dotenv_signature():
    import dotenv

    assert callable(dotenv.load_dotenv)


def test_worker_module_imports_against_real_deps(monkeypatch):
    """Final integration check: worker.py reads env at import time and constructs
    redis/sqlalchemy clients eagerly. Verify it loads against the upgraded versions."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SSRF_GUARD", "false")

    if "worker" in importlib.sys.modules:
        del importlib.sys.modules["worker"]
    worker_mod = importlib.import_module("worker")
    assert hasattr(worker_mod, "DownloadWorker")
    assert hasattr(worker_mod, "redis_client")


def test_ssl_adapter_factories_construct_without_network():
    import ssl_adapter

    sess = ssl_adapter.create_legacy_session()
    try:
        assert hasattr(sess, "get")
    finally:
        sess.close()

    impersonated = ssl_adapter.create_impersonated_session()
    try:
        assert hasattr(impersonated, "get")
    finally:
        impersonated.close()
