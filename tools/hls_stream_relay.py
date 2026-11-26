#!/usr/bin/env python3
"""Local HLS Sanitizer & Relay Server for Live Sports Streams.

Strips server-side ad-insertion (SSAI) non-video analytics/tracking segments
(such as Akamai/Anvato ``/targeting/analytics`` beacons) and problematic bare
``#EXT-X-DISCONTINUITY`` markers from live HLS playlists so VLC for Android TV
can play CBS/NFL and MLB streams without crashing back to the launcher.

Also provides an optional continuous MPEG-TS remux endpoint (``/stream/<id>/live.ts``)
backed by ffmpeg when ``mode='ts'`` is preferred.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
for _p in (str(REPO_ROOT), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
except ImportError:  # pragma: no cover - direct execution fallback
    import nas_config  # type: ignore[no-redef]


PID_FILE = Path("/workspace/data/hls_relay/relay.pid")
LOG_FILE = Path("/workspace/data/hls_relay/relay.log")
SECRETS_ENV_PATH = Path("/secrets/env.json")

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8844
DEFAULT_IDLE_TIMEOUT = 1800.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

VALID_MEDIA_EXTENSIONS = (".ts", ".m4s", ".mp4", ".aac", ".cmfv", ".cmfa")
NON_VIDEO_ASSET_EXTENSIONS = (
    ".gif",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
    ".js",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".txt",
    ".php",
    ".asp",
    ".aspx",
    ".cgi",
)
BOGUS_URI_KEYWORDS = (
    "/targeting/analytics",
    "/targeting/",
    "/analytics",
    "/beacon",
    "/pixel",
    "/tracking",
    "/impression",
    "ad_beacon",
    "ad-beacon",
    "clicktrack",
    "telemetry",
    "doubleclick",
    "scorecardresearch",
    "/vast",
    "/vmap",
)

_TAG_URI_RE = re.compile(r'URI="([^"]+)"')
_SEGMENT_METADATA_PREFIXES = (
    "#EXTINF:",
    "#EXT-X-BYTERANGE:",
    "#EXT-X-PROGRAM-DATE-TIME:",
)

START_TIME = time.time()


# ---------------------------------------------------------------------------
# Pure HLS playlist sanitization functions
# ---------------------------------------------------------------------------


def is_bogus_ssai_segment(uri: str) -> bool:
    """Return True if a media playlist URI is a non-video SSAI tracking/analytics beacon."""
    raw = (uri or "").strip()
    if not raw:
        return False

    parsed = urllib.parse.urlsplit(raw)
    path = parsed.path.lower()

    # Always preserve valid media segments, even when signed with query tokens (?hdntl=...)
    if path.endswith(VALID_MEDIA_EXTENSIONS):
        return False

    # Reject explicit non-video web asset extensions
    if path.endswith(NON_VIDEO_ASSET_EXTENSIONS):
        return True

    # Without a valid media segment extension, flag analytics/tracking paths or hostnames
    searchable = f"{parsed.netloc.lower()}{path}?{parsed.query.lower()}"
    if any(kw in searchable for kw in BOGUS_URI_KEYWORDS):
        return True

    # Also match standalone analytics/tracking keyword tokens in path/host
    path_and_host = f"{parsed.netloc.lower()}{path}"
    for kw in ("analytics", "beacon", "pixel", "tracking", "impression"):
        if re.search(rf"(?:^|[/._-]){kw}(?:$|[/._-])", path_and_host):
            return True

    return False


def _build_variant_proxy_url(raw_uri: str, upstream_base_url: str, stream_id: str, relay_base_url: str) -> str:
    abs_url = urllib.parse.urljoin(upstream_base_url, raw_uri.strip())
    encoded = urllib.parse.quote(abs_url, safe="")
    return f"{relay_base_url.rstrip('/')}/stream/{stream_id}/variant.m3u8?u={encoded}"


def rewrite_master_playlist(
    playlist_text: str,
    upstream_base_url: str,
    stream_id: str,
    relay_base_url: str,
) -> str:
    """Resolve variant URIs against upstream_base_url and route them through the local relay."""
    trailing_newline = playlist_text.endswith("\n")
    out_lines: list[str] = []

    for line in playlist_text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue

        if stripped.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")):
            rewritten_tag = _TAG_URI_RE.sub(
                lambda m: f'URI="{_build_variant_proxy_url(m.group(1), upstream_base_url, stream_id, relay_base_url)}"',
                line,
            )
            out_lines.append(rewritten_tag)
            continue

        if stripped.startswith("#"):
            out_lines.append(line)
            continue

        out_lines.append(_build_variant_proxy_url(stripped, upstream_base_url, stream_id, relay_base_url))

    result = "\n".join(out_lines)
    if trailing_newline and result:
        result += "\n"
    return result


def sanitize_media_playlist(
    playlist_text: str,
    upstream_base_url: str,
    strip_discontinuities: bool = True,
) -> tuple[str, dict[str, int]]:
    """Strip SSAI tracking segments, resolve relative media URLs, and optionally strip discontinuities."""
    trailing_newline = playlist_text.endswith("\n")
    output_lines: list[str] = []
    pending_segment_tags: list[str] = []
    stripped_segments = 0
    stripped_discontinuities = 0

    for line in playlist_text.splitlines():
        stripped = line.strip()

        if not stripped:
            if not pending_segment_tags:
                output_lines.append(line)
            continue

        if stripped == "#EXT-X-DISCONTINUITY":
            if pending_segment_tags:
                output_lines.extend(pending_segment_tags)
                pending_segment_tags.clear()
            if strip_discontinuities:
                stripped_discontinuities += 1
                continue
            output_lines.append(line)
            continue

        if stripped.startswith(_SEGMENT_METADATA_PREFIXES):
            pending_segment_tags.append(line)
            continue

        if stripped.startswith("#"):
            if pending_segment_tags:
                output_lines.extend(pending_segment_tags)
                pending_segment_tags.clear()
            if stripped.startswith(("#EXT-X-KEY:", "#EXT-X-MAP:")):
                line = _TAG_URI_RE.sub(
                    lambda m: f'URI="{urllib.parse.urljoin(upstream_base_url, m.group(1).strip())}"',
                    line,
                )
            output_lines.append(line)
            continue

        # Media segment URI line
        abs_uri = urllib.parse.urljoin(upstream_base_url, stripped)
        if is_bogus_ssai_segment(stripped) or is_bogus_ssai_segment(abs_uri):
            pending_segment_tags.clear()
            stripped_segments += 1
            continue

        if pending_segment_tags:
            output_lines.extend(pending_segment_tags)
            pending_segment_tags.clear()
        output_lines.append(abs_uri)

    if pending_segment_tags:
        output_lines.extend(pending_segment_tags)

    result = "\n".join(output_lines)
    if trailing_newline and result:
        result += "\n"

    stats = {
        "stripped_segments": stripped_segments,
        "stripped_discontinuities": stripped_discontinuities,
    }
    return result, stats


# ---------------------------------------------------------------------------
# Auth & Topology Helpers
# ---------------------------------------------------------------------------


def _load_secrets_env() -> dict[str, Any]:
    try:
        if SECRETS_ENV_PATH.exists():
            data = json.loads(SECRETS_ENV_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _get_config(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is not None and str(val).strip():
        return str(val).strip()
    secrets = _load_secrets_env()
    sval = secrets.get(key)
    if sval is not None and str(sval).strip():
        return str(sval).strip()
    return default


def _get_auth_tokens() -> list[str]:
    tokens: list[str] = []
    for key in ("HLS_RELAY_TOKEN", "ZERO_HEALTH_ADMIN_TOKEN"):
        val = _get_config(key, "")
        if val and val not in tokens:
            tokens.append(val)
    return tokens


def _is_authorized(auth_header: str, tokens: list[str] | None = None) -> bool:
    if not isinstance(auth_header, str):
        return False
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    provided = parts[1].strip()
    if not provided:
        return False
    expected = _get_auth_tokens() if tokens is None else tokens
    if not expected:
        return False
    return any(hmac.compare_digest(provided, tok) for tok in expected)


def _check_control_auth(request: Request) -> JSONResponse | None:
    """Allow loopback clients unconditionally, or remote clients presenting a valid Bearer token."""
    client_host = request.client.host if request.client else ""
    if client_host in LOOPBACK_HOSTS:
        return None
    auth_header = request.headers.get("authorization", "")
    if not _is_authorized(auth_header):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return None


def get_relay_host() -> str:
    """Resolve LAN IP for stream URLs dynamically via nas_config (fallback 127.0.0.1)."""
    host = getattr(nas_config, "HOST_2_IP", None)
    if isinstance(host, str) and host.strip():
        return host.strip()
    return "127.0.0.1"


def _is_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def _is_master_playlist(playlist_text: str) -> bool:
    if "#EXT-X-STREAM-INF" in playlist_text:
        return True
    return (
        "#EXT-X-MEDIA:" in playlist_text or "#EXT-X-I-FRAME-STREAM-INF:" in playlist_text
    ) and "#EXTINF:" not in playlist_text


def _extract_first_variant_url(playlist_text: str, upstream_base_url: str) -> str | None:
    lines = playlist_text.splitlines()
    best_url: str | None = None
    best_bw = -1
    pending_bw = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXT-X-STREAM-INF:"):
            m_bw = re.search(r"BANDWIDTH=(\d+)", stripped)
            pending_bw = int(m_bw.group(1)) if m_bw else 0
            continue
        if stripped.startswith("#"):
            continue
        abs_url = urllib.parse.urljoin(upstream_base_url, stripped)
        if best_url is None or pending_bw > best_bw:
            best_url = abs_url
            best_bw = pending_bw
        pending_bw = 0

    return best_url


# ---------------------------------------------------------------------------
# Stream Registry & Upstream Fetcher
# ---------------------------------------------------------------------------


@dataclass
class StreamEntry:
    stream_id: str
    upstream_url: str
    referer: str = ""
    origin: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    mode: str = "hls"
    strip_discontinuities: bool = True
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    active_ts_viewers: int = 0
    stripped_segments: int = 0
    stripped_discontinuities: int = 0
    playlist_requests: int = 0
    active_procs: set[asyncio.subprocess.Process] = field(default_factory=set)

    def upstream_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent or DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.referer:
            headers["Referer"] = self.referer
            if not self.origin:
                parsed = urllib.parse.urlsplit(self.referer)
                if parsed.scheme and parsed.netloc:
                    headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        if self.origin:
            headers["Origin"] = self.origin
        return headers


class StreamRegistry:
    """In-memory stream registry with automatic idle expiration."""

    def __init__(self, idle_timeout: float = DEFAULT_IDLE_TIMEOUT):
        self.idle_timeout = idle_timeout
        self.streams: dict[str, StreamEntry] = {}
        self.lock = asyncio.Lock()

    async def register(
        self,
        upstream_url: str,
        referer: str = "",
        mode: str = "hls",
        strip_discontinuities: bool = True,
        user_agent: str | None = None,
    ) -> StreamEntry:
        stream_id = uuid.uuid4().hex[:12]
        entry = StreamEntry(
            stream_id=stream_id,
            upstream_url=upstream_url.strip(),
            referer=referer.strip(),
            user_agent=(user_agent or DEFAULT_USER_AGENT).strip(),
            mode=mode if mode in ("hls", "ts") else "hls",
            strip_discontinuities=strip_discontinuities,
        )
        async with self.lock:
            self.streams[stream_id] = entry
        return entry

    async def get(self, stream_id: str) -> StreamEntry | None:
        async with self.lock:
            entry = self.streams.get(stream_id)
            if entry is not None:
                entry.last_accessed_at = time.time()
            return entry

    async def cleanup_idle(self) -> int:
        if self.idle_timeout <= 0:
            return 0
        now = time.time()
        removed = 0
        async with self.lock:
            stale_ids = [
                sid
                for sid, entry in self.streams.items()
                if entry.active_ts_viewers <= 0 and (now - entry.last_accessed_at) > self.idle_timeout
            ]
            for sid in stale_ids:
                self.streams.pop(sid, None)
                removed += 1
        return removed

    async def shutdown_all(self) -> None:
        async with self.lock:
            procs: list[asyncio.subprocess.Process] = []
            for entry in self.streams.values():
                procs.extend(list(entry.active_procs))
            self.streams.clear()
        for proc in procs:
            await _terminate_process(proc)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)


async def default_fetch_upstream(url: str, headers: dict[str, str]) -> tuple[str, str]:
    """Fetch upstream HLS playlist text, handling gzip decompression transparently."""
    timeout = aiohttp.ClientTimeout(total=15.0, connect=5.0, sock_read=10.0)
    async with (
        aiohttp.ClientSession(timeout=timeout, auto_decompress=True) as session,
        session.get(url, headers=headers, allow_redirects=True) as resp,
    ):
        if resp.status >= 400:
            body_preview = (await resp.text(errors="ignore"))[:200]
            raise HTTPException(
                status_code=502,
                detail=f"Upstream returned HTTP {resp.status}: {body_preview}",
            )
        raw_bytes = await resp.read()
        if raw_bytes[:2] == b"\x1f\x8b":
            raw_bytes = gzip.decompress(raw_bytes)
        text = raw_bytes.decode("utf-8", errors="replace")
        return text, str(resp.url)


# ---------------------------------------------------------------------------
# FastAPI App & Endpoints
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    upstream_url: str
    referer: str = ""
    mode: str = Field(default="hls", pattern="^(hls|ts)$")
    strip_discontinuities: bool = True
    user_agent: str | None = None


registry = StreamRegistry(idle_timeout=DEFAULT_IDLE_TIMEOUT)
FetchFn = Callable[[str, dict[str, str]], Awaitable[tuple[str, str]]]
_fetch_upstream: FetchFn = default_fetch_upstream
_active_port: int = DEFAULT_PORT


def set_fetch_upstream(fn: FetchFn | None) -> None:
    """Override upstream fetcher (primarily for unit tests)."""
    global _fetch_upstream
    _fetch_upstream = fn or default_fetch_upstream


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    stop_event = asyncio.Event()

    async def _reaper_loop() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                break
            except TimeoutError:
                await registry.cleanup_idle()

    reaper_task = asyncio.create_task(_reaper_loop())
    try:
        yield
    finally:
        stop_event.set()
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task
        await registry.shutdown_all()


app = FastAPI(title="Zero Local HLS Sanitizer Relay", version="1.0.0", lifespan=lifespan)


def _resolve_request_port(request: Request) -> int:
    if request.url.port:
        return int(request.url.port)
    host_hdr = request.headers.get("host", "")
    if ":" in host_hdr:
        try:
            return int(host_hdr.rsplit(":", 1)[1])
        except ValueError:
            pass
    return _active_port


def _resolve_relay_base_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if base:
        return base
    return f"http://{get_relay_host()}:{_active_port}"


@app.get("/health")
async def get_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "active_streams": len(registry.streams),
        "uptime_seconds": round(time.time() - START_TIME, 2),
    }


@app.get("/status")
async def get_status(request: Request):
    auth_err = _check_control_auth(request)
    if auth_err is not None:
        return auth_err
    now = time.time()
    streams_info = [
        {
            "stream_id": entry.stream_id,
            "mode": entry.mode,
            "upstream_url": entry.upstream_url,
            "active_ts_viewers": entry.active_ts_viewers,
            "playlist_requests": entry.playlist_requests,
            "stripped_segments": entry.stripped_segments,
            "stripped_discontinuities": entry.stripped_discontinuities,
            "idle_seconds": round(now - entry.last_accessed_at, 2),
        }
        for entry in registry.streams.values()
    ]
    return {
        "status": "ok",
        "host_ip": get_relay_host(),
        "port": _active_port,
        "uptime_seconds": round(now - START_TIME, 2),
        "active_streams": len(streams_info),
        "streams": streams_info,
    }


@app.post("/register")
async def register_stream(payload: RegisterRequest, request: Request):
    auth_err = _check_control_auth(request)
    if auth_err is not None:
        return auth_err

    if not _is_http_url(payload.upstream_url):
        raise HTTPException(status_code=400, detail="upstream_url must be a valid http(s) URL")

    entry = await registry.register(
        upstream_url=payload.upstream_url,
        referer=payload.referer,
        mode=payload.mode,
        strip_discontinuities=payload.strip_discontinuities,
        user_agent=payload.user_agent,
    )
    host_ip = get_relay_host()
    port = _active_port
    base = f"http://{host_ip}:{port}"
    return {
        "stream_id": entry.stream_id,
        "mode": entry.mode,
        "upstream_url": entry.upstream_url,
        "hls_url": f"{base}/stream/{entry.stream_id}/master.m3u8",
        "ts_url": f"{base}/stream/{entry.stream_id}/live.ts",
    }


@app.post("/stop")
async def stop_endpoint(request: Request):
    auth_err = _check_control_auth(request)
    if auth_err is not None:
        return auth_err

    await registry.shutdown_all()
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
    return {"stopping": True}


@app.get("/stream/{stream_id}/master.m3u8")
async def get_master_playlist(stream_id: str, request: Request) -> Response:
    entry = await registry.get(stream_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown stream_id")

    try:
        text, final_url = await _fetch_upstream(entry.upstream_url, entry.upstream_headers())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {exc}") from exc

    entry.playlist_requests += 1
    if _is_master_playlist(text):
        relay_base = _resolve_relay_base_url(request)
        body = rewrite_master_playlist(text, final_url, stream_id, relay_base)
    else:
        body, stats = sanitize_media_playlist(
            text, final_url, strip_discontinuities=entry.strip_discontinuities
        )
        entry.stripped_segments += stats["stripped_segments"]
        entry.stripped_discontinuities += stats["stripped_discontinuities"]

    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


async def _serve_sanitized_variant(stream_id: str, variant_url: str | None) -> Response:
    entry = await registry.get(stream_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown stream_id")

    target_url = variant_url
    if target_url is None:
        try:
            text, final_url = await _fetch_upstream(entry.upstream_url, entry.upstream_headers())
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {exc}") from exc

        if _is_master_playlist(text):
            chosen = _extract_first_variant_url(text, final_url)
            if not chosen:
                raise HTTPException(status_code=502, detail="No variant streams found in master playlist")
            target_url = chosen
            text, final_url = await _fetch_upstream(target_url, entry.upstream_headers())
    else:
        if not _is_http_url(target_url):
            raise HTTPException(status_code=400, detail="Invalid variant URL")
        try:
            text, final_url = await _fetch_upstream(target_url, entry.upstream_headers())
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {exc}") from exc

    sanitized, stats = sanitize_media_playlist(
        text, final_url, strip_discontinuities=entry.strip_discontinuities
    )
    entry.playlist_requests += 1
    entry.stripped_segments += stats["stripped_segments"]
    entry.stripped_discontinuities += stats["stripped_discontinuities"]

    return Response(
        content=sanitized,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/stream/{stream_id}/variant.m3u8")
async def get_variant_playlist(
    stream_id: str,
    u: str = Query(..., description="Upstream variant playlist URL"),
) -> Response:
    return await _serve_sanitized_variant(stream_id, u)


@app.get("/stream/{stream_id}/media.m3u8")
async def get_media_playlist(
    stream_id: str,
    u: str | None = Query(default=None, description="Optional upstream variant playlist URL"),
) -> Response:
    return await _serve_sanitized_variant(stream_id, u)


@app.get("/stream/{stream_id}/live.ts")
async def get_live_ts(stream_id: str, request: Request) -> StreamingResponse:
    entry = await registry.get(stream_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown stream_id")

    port = _resolve_request_port(request)
    local_sanitized_master_url = f"http://127.0.0.1:{port}/stream/{stream_id}/master.m3u8"
    cmd = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+discardcorrupt+genpts",
        "-err_detect",
        "ignore_err",
        "-i",
        local_sanitized_master_url,
        "-c",
        "copy",
        "-f",
        "mpegts",
        "pipe:1",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="ffmpeg binary not found") from exc

    entry.active_ts_viewers += 1
    entry.active_procs.add(proc)

    async def _ts_generator():
        try:
            assert proc.stdout is not None
            while True:
                if await request.is_disconnected():
                    break
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                entry.last_accessed_at = time.time()
                yield chunk
        finally:
            entry.active_procs.discard(proc)
            entry.active_ts_viewers = max(0, entry.active_ts_viewers - 1)
            entry.last_accessed_at = time.time()
            await _terminate_process(proc)

    return StreamingResponse(
        _ts_generator(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Daemon & CLI Lifecycle (mirrors tools/webcam_stream_proxy.py)
# ---------------------------------------------------------------------------


def is_running() -> tuple[bool, int | None]:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return True, pid
        except (ValueError, OSError):
            pass
    return False, None


def stop_relay() -> bool:
    running, pid = is_running()
    if not running or pid is None:
        print("HLS stream relay is not running.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return True

    print(f"Stopping HLS stream relay (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            if not is_running()[0]:
                print("HLS stream relay stopped cleanly.")
                if PID_FILE.exists():
                    PID_FILE.unlink()
                return True
        os.kill(pid, signal.SIGKILL)
        print("HLS stream relay killed.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return True
    except OSError as exc:
        print(f"Error stopping relay: {exc}", file=sys.stderr)
        return False


def start_daemon(host: str, port: int, idle_timeout: float) -> bool:
    running, pid = is_running()
    if running:
        print(f"HLS stream relay is already running (PID {pid}).")
        return True

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host",
        host,
        "--port",
        str(port),
        "--idle-timeout",
        str(idle_timeout),
    ]
    with open(LOG_FILE, "a", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    for _ in range(10):
        time.sleep(0.3)
        if is_running()[0]:
            print(f"Started HLS stream relay detached (PID {proc.pid}). Listening on {host}:{port}")
            return True

    print(f"Started HLS stream relay process (PID {proc.pid}). Logs: {LOG_FILE}")
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    default_host = os.environ.get("HLS_RELAY_HOST", "").strip() or DEFAULT_BIND_HOST
    default_port_raw = os.environ.get("HLS_RELAY_PORT", "").strip()
    default_port = int(default_port_raw) if default_port_raw.isdigit() else DEFAULT_PORT

    parser = argparse.ArgumentParser(description="Zero Local HLS Sanitizer & Relay Server")
    parser.add_argument(
        "--host",
        default=default_host,
        help=f"Bind address (default: {DEFAULT_BIND_HOST} or $HLS_RELAY_HOST; pass 0.0.0.0 for LAN)",
    )
    parser.add_argument("--port", type=int, default=default_port, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT,
        help=f"Stream idle cleanup timeout in seconds (default: {DEFAULT_IDLE_TIMEOUT})",
    )
    parser.add_argument("--daemon", action="store_true", help="Run relay in background detached")
    parser.add_argument("--stop", action="store_true", help="Stop running relay daemon")
    parser.add_argument("--status", action="store_true", help="Check relay daemon status")
    return parser


def main() -> None:
    global _active_port
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.stop:
        stop_relay()
        return

    if args.status:
        running, pid = is_running()
        print(f"HLS Relay Running: {running} (PID: {pid})")
        return

    if args.daemon:
        start_daemon(args.host, args.port, args.idle_timeout)
        return

    _active_port = args.port
    registry.idle_timeout = args.idle_timeout
    print(f"[HLS Relay] Starting on {args.host}:{args.port} (LAN host: {get_relay_host()}, idle: {args.idle_timeout}s)...")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
