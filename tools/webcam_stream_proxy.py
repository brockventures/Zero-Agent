#!/usr/bin/env python3
"""
On-Demand Webcam Stream Proxy for OctoPrint (Pixel 8 Pro / IP Webcam)
Intercepts stream and snapshot connections from OctoPrint and browser clients:
- Wakes Pixel 8 Pro over ADB on first viewer connection (~1.5s).
- Streams MJPEG video seamlessly over HTTP/HTTPS.
- Returns on-demand snapshots for OctoPrint timelapse / UI previews.
- Tracks active viewer socket count.
- Automatically powers down IP Webcam and sleeps phone when viewers leave.
"""

import sys
import os
import time
import signal
import asyncio
import argparse
import subprocess
from typing import Optional
from pathlib import Path

import aiohttp
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
import uvicorn

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import webcam_control

PID_FILE = Path("/workspace/data/3dprint/proxy.pid")
LOG_FILE = Path("/workspace/data/3dprint/proxy.log")

app = FastAPI(title="Zero On-Demand Webcam Proxy", version="1.0.0")


class StreamManager:
    def __init__(self, idle_timeout: float = 30.0):
        self.idle_timeout = idle_timeout
        self.active_viewers = 0
        self.lock = asyncio.Lock()
        self.idle_task: Optional[asyncio.Task] = None
        self.camera_starting = False
        self.last_wake_time: float = 0.0

    async def ensure_camera_running(self) -> bool:
        """Verify camera is open; wake over ADB if offline."""
        async with self.lock:
            if self.idle_task and not self.idle_task.done():
                self.idle_task.cancel()
                self.idle_task = None
                print("[Proxy] Cancelled pending idle shutdown timer. Viewer connected.")

            loop = asyncio.get_running_loop()
            is_open = await loop.run_in_executor(None, webcam_control.is_port_open)
            if is_open:
                return True

            if self.camera_starting:
                for _ in range(15):
                    await asyncio.sleep(0.5)
                    if await loop.run_in_executor(None, webcam_control.is_port_open):
                        return True
                return False

            self.camera_starting = True
            try:
                print("[Proxy] Waking Pixel 8 Pro via ADB for active viewer...")
                success = await loop.run_in_executor(None, webcam_control.start_camera)
                if success:
                    self.last_wake_time = time.time()
                return success
            finally:
                self.camera_starting = False

    async def viewer_connected(self):
        async with self.lock:
            self.active_viewers += 1
            if self.idle_task and not self.idle_task.done():
                self.idle_task.cancel()
                self.idle_task = None
            print(f"[Proxy] Viewer joined. Active viewers: {self.active_viewers}")

    async def viewer_disconnected(self):
        async with self.lock:
            self.active_viewers = max(0, self.active_viewers - 1)
            print(f"[Proxy] Viewer left. Active viewers: {self.active_viewers}")
            if self.active_viewers == 0:
                if self.idle_task and not self.idle_task.done():
                    self.idle_task.cancel()
                self.idle_task = asyncio.create_task(self._idle_shutdown_timer())

    async def _idle_shutdown_timer(self):
        print(f"[Proxy] Zero active viewers. Starting {self.idle_timeout}s idle shutdown countdown...")
        try:
            await asyncio.sleep(self.idle_timeout)
            async with self.lock:
                if self.active_viewers == 0:
                    print("[Proxy] Idle timeout expired. Stopping IP Webcam and sleeping Pixel 8 Pro...")
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, webcam_control.stop_camera)
                else:
                    print(f"[Proxy] Aborted idle shutdown; active viewers: {self.active_viewers}")
        except asyncio.CancelledError:
            print("[Proxy] Idle shutdown timer cancelled.")


manager = StreamManager(idle_timeout=30.0)


@app.get("/status")
async def get_status():
    loop = asyncio.get_running_loop()
    is_open = await loop.run_in_executor(None, webcam_control.is_port_open)
    return {
        "camera_online": is_open,
        "active_viewers": manager.active_viewers,
        "idle_timeout_sec": manager.idle_timeout,
        "idle_pending": (manager.idle_task is not None and not manager.idle_task.done()),
    }


@app.post("/wake")
async def manual_wake():
    ok = await manager.ensure_camera_running()
    return {"success": ok}


@app.post("/sleep")
async def manual_sleep():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, webcam_control.stop_camera)
    return {"success": True}


async def stream_generator(request: Request):
    """Bridge incoming HTTP client to IP Webcam MJPEG stream."""
    await manager.viewer_connected()
    try:
        ok = await manager.ensure_camera_running()
        if not ok:
            print("[Proxy] Failed to wake camera for stream.")
            yield b"--frame\r\nContent-Type: text/plain\r\n\r\nCamera unavailable\r\n"
            return

        _, web_url, _ = webcam_control.get_adb_context(ensure_forward=True)
        upstream_url = f"{web_url}/video"

        timeout = aiohttp.ClientTimeout(total=None, connect=5.0, sock_read=10.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(upstream_url) as resp:
                    if resp.status != 200:
                        print(f"[Proxy] Upstream returned status {resp.status}")
                        return

                    async for chunk in resp.content.iter_any():
                        if await request.is_disconnected():
                            break
                        yield chunk
            except Exception as e:
                print(f"[Proxy] Stream transmission error: {e}")
    finally:
        await manager.viewer_disconnected()


@app.api_route("/video", methods=["GET", "HEAD"])
@app.api_route("/webcam/", methods=["GET", "HEAD"])
@app.api_route("/webcam/?action=stream", methods=["GET", "HEAD"])
async def get_stream(request: Request):
    action = request.query_params.get("action", "stream")
    if action == "snapshot":
        return await handle_snapshot(request)

    response = StreamingResponse(
        stream_generator(request),
        media_type="multipart/x-mixed-replace; boundary=--video boundary--",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, pre-check=0, post-check=0, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.api_route("/photo.jpg", methods=["GET", "POST", "HEAD"])
@app.api_route("/snapshot", methods=["GET", "POST", "HEAD"])
@app.api_route("/webcam/?action=snapshot", methods=["GET", "POST", "HEAD"])
async def handle_snapshot(request: Request):
    """Return single JPEG snapshot, waking camera if needed."""
    ok = await manager.ensure_camera_running()
    if not ok:
        return Response(status_code=503, content=b"Camera unavailable")

    _, web_url, _ = webcam_control.get_adb_context(ensure_forward=True)
    upstream_url = f"{web_url}/shot.jpg"

    timeout = aiohttp.ClientTimeout(total=5.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(upstream_url) as resp:
                data = await resp.read()
                return Response(
                    content=data,
                    media_type="image/jpeg",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
        except Exception as e:
            return Response(status_code=502, content=str(e).encode())


def is_running():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return True, pid
        except (ValueError, OSError):
            pass
    return False, None


def stop_proxy():
    running, pid = is_running()
    if not running:
        print("Webcam stream proxy is not running.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return True
    print(f"Stopping webcam stream proxy (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.3)
            if not is_running()[0]:
                print("Webcam stream proxy stopped cleanly.")
                if PID_FILE.exists():
                    PID_FILE.unlink()
                return True
        os.kill(pid, signal.SIGKILL)
        print("Webcam stream proxy killed.")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return True
    except Exception as e:
        print(f"Error stopping proxy: {e}", file=sys.stderr)
        return False


def start_daemon(host: str, port: int, idle_timeout: float):
    running, pid = is_running()
    if running:
        print(f"Webcam stream proxy is already running (PID {pid}).")
        return True

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--host", host,
        "--port", str(port),
        "--idle-timeout", str(idle_timeout),
    ]
    log_f = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    for _ in range(10):
        time.sleep(0.3)
        if is_running()[0]:
            print(f"Started Zero Webcam Stream Proxy detached (PID {proc.pid}). Listening on {host}:{port}")
            return True

    print(f"Started proxy process (PID {proc.pid}). Logs: {LOG_FILE}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Zero On-Demand Webcam Proxy")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=3334, help="Listen port (default: 3334)")
    parser.add_argument("--idle-timeout", type=float, default=30.0, help="Idle timeout in seconds (default: 30.0)")
    parser.add_argument("--daemon", action="store_true", help="Run proxy in background detached")
    parser.add_argument("--stop", action="store_true", help="Stop running proxy daemon")
    parser.add_argument("--status", action="store_true", help="Check proxy status")
    args = parser.parse_args()

    if args.stop:
        stop_proxy()
        return

    if args.status:
        running, pid = is_running()
        print(f"Proxy Running: {running} (PID: {pid})")
        return

    if args.daemon:
        start_daemon(args.host, args.port, args.idle_timeout)
        return

    manager.idle_timeout = args.idle_timeout
    print(f"[Proxy] Starting Zero On-Demand Webcam Proxy on {args.host}:{args.port} (Idle Timeout: {args.idle_timeout}s)...")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
