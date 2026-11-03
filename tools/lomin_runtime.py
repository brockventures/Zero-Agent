#!/usr/bin/env python3
"""
lomin_runtime.py — Lomin Runtime Sync, Env Loader, Gateway Red-Team Patches & Adapter Shim
==========================================================================================
Provides:
1. `sync_lomin_modules_to_gotchi()`:
   Ensures `/tmp/lomin-parity-repo` modules are synced into `/workspace/gotchi/lomin/` so
   port 8771 and downstream imports have deterministic access to Lomin runtime modules.
2. `load_lomin_env_file(path='/workspace/config/lomin.env') -> dict`:
   Parses `KEY=VALUE` pairs (ignoring comments and blank lines, stripping optional quotes)
   from `/workspace/config/lomin.env`.
3. `apply_lomin_gateway_redteam_patches(gateway_mod)`:
   Applies runtime red-team patches to Lomin's `gateway.py` module:
   - Direct-Audio Whisper bypass: When `MVG_BRAIN_DIRECT_AUDIO=1` (or `MVG_DIRECT_AUDIO=1`
     or `gateway_mod.BRAIN_DIRECT_AUDIO` is truthy), local Whisper `transcribe()` failures or
     missing Whisper servers do not abort `/voice/turn` (or `/stt` / `/ambient`) with `502`
     or `error:stt`; instead a placeholder transcript (`"Voice prompt"`) is returned while
     `audio_b64` is forwarded directly to the brain.
   - Multimodal `**kwargs` forwarding: Updates `plugin_brain_call_stream`, `plugin_brain_call`,
     `STREAM_BRAIN_CALL`, and `_plugin_brain_call_stream` so `image_b64`, `burst_images_b64`,
     and `audio_b64` are forwarded through `STREAM_BRAIN_CALL` into `_plugin_brain_call_stream`
     (and into `GotchiZeroBrainAdapter.handle_turn` when the plugin is `gotchi_zero_adapter`).
   - Heartbeat pending notification status: Updates `/api/voice/heartbeat` (on both `GET` and
     `POST`) to return `200 application/json` containing `has_pending` (plus `pending` / `item`
     peeked from `get_pending_store()`) so Gotchi nodes discover queued async voice notifications
     immediately on heartbeat ticks.
4. `ensure_single_adapter_shim()`:
   Replaces `/workspace/gotchi/backend/gotchi_zero_adapter.py` with a thin re-export shim
   (`from tools.gotchi_zero_adapter import *`) so there is never duplicate adapter code across
   repositories.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("lomin_runtime")

DEFAULT_LOMIN_ENV_PATH = "/workspace/config/lomin.env"
DEFAULT_LOMIN_PARITY_REPO = "/tmp/lomin-parity-repo"
DEFAULT_GOTCHI_LOMIN_DIR = "/workspace/gotchi/lomin"
DEFAULT_GOTCHI_ADAPTER_SHIM_PATH = "/workspace/gotchi/backend/gotchi_zero_adapter.py"
DIRECT_AUDIO_PLACEHOLDER_TRANSCRIPT = "Voice prompt"

LOMIN_CORE_MODULES = (
    "adpcm_codec.py",
    "audio_utils.py",
    "bench_t3.py",
    "desktop_embed.py",
    "desktop_tts.py",
    "gateway.py",
    "ha_fastpath.py",
    "multipart.py",
    "pending_and_auth.py",
    "sentence_segmenter.py",
    "side_channel.py",
    "streaming_brain.py",
    "t3_fast.py",
    "text_utils.py",
    "tts_backends.py",
    "vision_adpcm.py",
)

ADAPTER_SHIM_CONTENT = '''#!/usr/bin/env python3
"""
gotchi_zero_adapter.py — Single-Source-of-Truth Re-Export Shim
==============================================================
Thin re-export shim delegating all symbols to `tools.gotchi_zero_adapter` so
`/workspace/tools/gotchi_zero_adapter.py` remains the sole canonical implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

for _root_candidate in (
    str(Path(__file__).resolve().parents[2]),
    "/workspace",
    "/workspace/gotchi",
    "/workspace/gotchi/lomin",
    "/tmp/lomin-parity-repo",
):
    if Path(_root_candidate).is_dir() and _root_candidate not in sys.path:
        sys.path.insert(0, _root_candidate)

from tools import gotchi_zero_adapter as _canonical_adapter  # noqa: E402
from tools.gotchi_zero_adapter import *  # noqa: F401,F403,E402

# Explicitly re-export underscore helpers referenced by unit tests or external callers
for _attr_name in dir(_canonical_adapter):
    if not _attr_name.startswith("__"):
        globals()[_attr_name] = getattr(_canonical_adapter, _attr_name)

__all__ = [name for name in globals() if not name.startswith("__")]


if __name__ == "__main__":
    main()  # noqa: F405
'''


def sync_lomin_modules_to_gotchi(
    src_dir: str | Path = DEFAULT_LOMIN_PARITY_REPO,
    dest_dir: str | Path = DEFAULT_GOTCHI_LOMIN_DIR,
) -> List[str]:
    """
    Syncs Python modules from `/tmp/lomin-parity-repo` into `/workspace/gotchi/lomin/`
    and ensures `/workspace/gotchi/lomin` is on `sys.path`.
    Returns the list of synced filenames.
    """
    src_path = Path(src_dir)
    dest_path = Path(dest_dir)
    synced: List[str] = []

    try:
        dest_path.mkdir(parents=True, exist_ok=True)
        init_file = dest_path / "__init__.py"
        if not init_file.exists():
            init_file.write_text('"""Vendored Lomin voice gateway runtime modules."""\n', encoding="utf-8")
    except Exception as exc:
        log.warning(f"[LominRuntime] Could not create {dest_path}: {exc}")
        return synced

    if src_path.is_dir():
        candidates = list(LOMIN_CORE_MODULES)
        for py_file in sorted(src_path.glob("*.py")):
            if py_file.name not in candidates:
                candidates.append(py_file.name)

        for filename in candidates:
            s_file = src_path / filename
            if not s_file.is_file():
                continue
            d_file = dest_path / filename
            try:
                if (
                    not d_file.exists()
                    or s_file.stat().st_size != d_file.stat().st_size
                    or s_file.read_bytes() != d_file.read_bytes()
                ):
                    shutil.copy2(s_file, d_file)
                synced.append(filename)
            except Exception as exc:
                log.warning(f"[LominRuntime] Failed to sync {s_file} -> {d_file}: {exc}")

    for path_str in (str(dest_path), str(src_path)):
        if Path(path_str).is_dir() and path_str not in sys.path:
            sys.path.insert(0, path_str)

    return synced


def load_lomin_env_file(
    path: str | Path = DEFAULT_LOMIN_ENV_PATH,
    apply_to_environ: bool = False,
    override: bool = True,
) -> Dict[str, str]:
    """
    Parses `KEY=VALUE` pairs from `/workspace/config/lomin.env`.
    Ignores blank lines and `#` comments, strips optional `export ` prefix and
    surrounding single/double quotes.
    """
    env_path = Path(path)
    parsed: Dict[str, str] = {}
    if not env_path.is_file():
        return parsed

    try:
        raw_text = env_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning(f"[LominRuntime] Failed to read {env_path}: {exc}")
        return parsed

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if " #" in val and not ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val.split(" #", 1)[0].strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        parsed[key] = val
        if apply_to_environ:
            if override or key not in os.environ:
                os.environ[key] = val

    return parsed


def is_direct_audio_active(gateway_mod: Any = None) -> bool:
    """Returns True when Lomin direct-audio mode (`MVG_BRAIN_DIRECT_AUDIO=1`) is enabled."""
    if gateway_mod is not None and bool(getattr(gateway_mod, "BRAIN_DIRECT_AUDIO", False)):
        return True
    for env_key in ("MVG_BRAIN_DIRECT_AUDIO", "MVG_DIRECT_AUDIO"):
        val = os.environ.get(env_key, "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
    return False


def _invoke_callable_with_supported_kwargs(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Calls `fn(*args, **filtered_kwargs)` if `fn` accepts `**kwargs` or the named keyword
    parameters; if `fn` is `tools.gotchi_zero_adapter.brain_call_stream` (or `brain_call`)
    which has a fixed 5-arg signature in `tools/gotchi_zero_adapter.py`, bridges multimodal
    kwargs (`image_b64`, `burst_images_b64`, `audio_b64`) directly into `GotchiZeroBrainAdapter`.
    """
    clean_kwargs = {k: v for k, v in kwargs.items() if v is not None and v != [] and v != ""}
    if not clean_kwargs:
        return fn(*args)

    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        accepted = {k: v for k, v in clean_kwargs.items() if has_var_kw or k in params}
    except (ValueError, TypeError):
        accepted = {}

    if accepted:
        return fn(*args, **accepted)

    # If the underlying plugin function is gotchi_zero_adapter.brain_call_stream / brain_call
    # (which cannot be modified in Chunk 2 and only declares positional/basic args), execute
    # through GotchiZeroBrainAdapter.handle_turn with the multimodal kwargs attached.
    fn_mod = getattr(fn, "__module__", "") or ""
    fn_name = getattr(fn, "__name__", "") or ""
    if "gotchi_zero_adapter" in fn_mod and fn_name in ("brain_call_stream", "brain_call"):
        try:
            from tools.gotchi_zero_adapter import GotchiZeroBrainAdapter

            target = args[0] if len(args) > 0 else "gotchi-ryan-01"
            prompt = args[1] if len(args) > 1 else ""
            on_partial = args[3] if len(args) > 3 else None

            adapter = GotchiZeroBrainAdapter()
            running_sentences: List[str] = []

            def _on_s(s_txt: str) -> None:
                running_sentences.append(s_txt)
                if on_partial is not None:
                    on_partial(" ".join(running_sentences))

            payload: Dict[str, Any] = {
                "prompt": prompt,
                "node_id": target or "gotchi-ryan-01",
            }
            if fn_name == "brain_call_stream":
                payload["_on_sentence"] = _on_s
            if clean_kwargs.get("image_b64"):
                payload["image_b64"] = clean_kwargs["image_b64"]
            if clean_kwargs.get("burst_images_b64"):
                payload["burst_images_b64"] = clean_kwargs["burst_images_b64"]
            if clean_kwargs.get("audio_b64"):
                raw_aud = clean_kwargs["audio_b64"]
                payload["audio_b64"] = raw_aud
                if isinstance(raw_aud, str):
                    try:
                        payload["audio_bytes"] = base64.b64decode(raw_aud)
                    except Exception:
                        pass

            res = asyncio.run(adapter.handle_turn(payload))
            reply = str(res.get("reply") or "")
            if fn_name == "brain_call_stream" and on_partial is not None and not running_sentences and reply:
                on_partial(reply)
            return reply
        except Exception as exc:
            log.warning(f"[LominRuntime] Multimodal adapter bridge fallback triggered: {exc}")

    return fn(*args)


def apply_lomin_gateway_redteam_patches(gateway_mod: Any = None) -> Any:
    """
    Applies the three Lomin `gateway.py` red-team fixes at runtime:
    1. Direct-Audio Whisper bypass: When `MVG_BRAIN_DIRECT_AUDIO=1` is active, local Whisper
       `transcribe()` failures or missing Whisper servers do not abort `/voice/turn` with `502`
       or `error:stt`; instead `transcribe()` returns a placeholder transcript (`"Voice prompt"`)
       while `audio_b64` is forwarded directly to the brain.
    2. Multimodal `**kwargs` forwarding: `plugin_brain_call_stream` and `STREAM_BRAIN_CALL`
       accept and forward `**kwargs` (`image_b64`, `burst_images_b64`, `audio_b64`) into
       `_plugin_brain_call_stream` (and `plugin_brain_call` / `_plugin_brain_call`).
    3. Heartbeat pending notification status: `/api/voice/heartbeat` returns `200 JSON` with
       `has_pending` (and `pending` / `item`) from `get_pending_store()`.
    """
    if gateway_mod is None:
        sync_lomin_modules_to_gotchi()
        import gateway as gateway_mod  # type: ignore

    if getattr(gateway_mod, "_zero_redteam_patched", False):
        return gateway_mod

    orig_transcribe = getattr(gateway_mod, "transcribe", None)
    orig_plugin_brain_call = getattr(gateway_mod, "plugin_brain_call", None)
    orig_plugin_brain_call_stream = getattr(gateway_mod, "plugin_brain_call_stream", None)
    orig_stream_brain_call = getattr(gateway_mod, "STREAM_BRAIN_CALL", None)

    # --- 1. Direct-Audio Whisper Bypass (`transcribe`) ---
    def patched_transcribe(wav_bytes: bytes) -> str:
        raw_fn = getattr(gateway_mod, "_orig_transcribe", orig_transcribe)
        try:
            tx = raw_fn(wav_bytes) if callable(raw_fn) else ""
            if tx and str(tx).strip():
                return str(tx).strip()
            if is_direct_audio_active(gateway_mod):
                return DIRECT_AUDIO_PLACEHOLDER_TRANSCRIPT
            return str(tx or "").strip()
        except Exception as exc:
            if is_direct_audio_active(gateway_mod):
                log.info(
                    f"[LominRuntime] Whisper transcribe failed under MVG_BRAIN_DIRECT_AUDIO=1 "
                    f"({exc}); bypassing local STT with placeholder transcript."
                )
                return DIRECT_AUDIO_PLACEHOLDER_TRANSCRIPT
            raise

    gateway_mod._orig_transcribe = orig_transcribe
    gateway_mod.transcribe = patched_transcribe

    # --- 2. Multimodal `**kwargs` Forwarding (`STREAM_BRAIN_CALL` -> `_plugin_brain_call_stream`) ---
    def _extract_multimodal_kwargs(explicit_kwargs: Dict[str, Any]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        frame_ctx_holder = getattr(gateway_mod, "_FRAME_CTX", None)
        ctx = getattr(frame_ctx_holder, "ctx", None) or {}
        enable_vision_fn = getattr(gateway_mod, "is_vision_enabled", lambda: True)
        if enable_vision_fn():
            img = explicit_kwargs.get("image_b64") or ctx.get("image_b64")
            burst = explicit_kwargs.get("burst_images_b64") or ctx.get("burst_images_b64")
            if img:
                merged["image_b64"] = img
            if burst:
                merged["burst_images_b64"] = burst
        if is_direct_audio_active(gateway_mod) or "audio_b64" in explicit_kwargs:
            aud = explicit_kwargs.get("audio_b64") or ctx.get("audio_b64")
            if aud:
                merged["audio_b64"] = aud
        for k, v in explicit_kwargs.items():
            if k not in merged and v is not None:
                merged[k] = v
        return merged

    def patched_plugin_brain_call(
        prompt: str,
        session_id: str,
        node_id: str,
        speaker: str,
        speaker_score: Any,
        timeout: int,
        **kwargs: Any,
    ) -> str:
        plugin_fn = getattr(gateway_mod, "_plugin_brain_call", None)
        if plugin_fn is None:
            raise RuntimeError("no plugin brain")
        wrapped = gateway_mod._frame_for_call(prompt, node_id, speaker, speaker_score)
        target = gateway_mod._call_target(node_id)
        mm_kwargs = _extract_multimodal_kwargs(kwargs)
        return _invoke_callable_with_supported_kwargs(
            plugin_fn,
            target,
            wrapped,
            timeout,
            **mm_kwargs,
        )

    def patched_plugin_brain_call_stream(
        prompt: str,
        session_id: str,
        node_id: str,
        speaker: str,
        speaker_score: Any,
        timeout: int,
        on_partial: Any,
        **kwargs: Any,
    ) -> str:
        mm_kwargs = _extract_multimodal_kwargs(kwargs)
        plugin_stream_fn = getattr(gateway_mod, "_plugin_brain_call_stream", None)
        if plugin_stream_fn is None:
            reply = gateway_mod.plugin_brain_call(
                prompt,
                session_id,
                node_id,
                speaker,
                speaker_score,
                timeout,
                **mm_kwargs,
            )
            if callable(on_partial):
                on_partial(reply)
            return reply
        wrapped = gateway_mod._frame_for_call(prompt, node_id, speaker, speaker_score)
        target = gateway_mod._call_target(node_id)
        stage_sink = getattr(gateway_mod, "_STAGE_SINK", None)
        stages = getattr(stage_sink, "stages", None) if stage_sink is not None else None
        return _invoke_callable_with_supported_kwargs(
            plugin_stream_fn,
            target,
            wrapped,
            timeout,
            on_partial,
            stages,
            **mm_kwargs,
        )

    gateway_mod._orig_plugin_brain_call = orig_plugin_brain_call
    gateway_mod._orig_plugin_brain_call_stream = orig_plugin_brain_call_stream
    gateway_mod.plugin_brain_call = patched_plugin_brain_call
    gateway_mod.plugin_brain_call_stream = patched_plugin_brain_call_stream

    if getattr(gateway_mod, "MVG_BRAIN", "http") != "http" or orig_stream_brain_call is orig_plugin_brain_call_stream:
        gateway_mod.STREAM_BRAIN_CALL = patched_plugin_brain_call_stream
    if getattr(gateway_mod, "MVG_BRAIN", "http") != "http":
        gateway_mod.BRAIN_CALL = patched_plugin_brain_call

    # --- 3. `/api/voice/heartbeat` -> 200 JSON with `has_pending` from `get_pending_store()` ---
    handler_cls = getattr(gateway_mod, "GatewayHandler", None)
    if handler_cls is not None:
        orig_do_get = getattr(handler_cls, "do_GET", None)
        orig_dispatch_post = getattr(handler_cls, "_dispatch_post", None)

        def _handle_heartbeat(self: Any) -> None:
            t0 = getattr(gateway_mod, "time", __import__("time")).time()
            parsed = urlsplit(getattr(self, "path", "/api/voice/heartbeat"))
            qs = parse_qs(parsed.query)
            body_req: Dict[str, Any] = {}
            if getattr(self, "command", "POST") == "POST" and hasattr(self, "_read_body"):
                try:
                    raw = self._read_body()
                    if raw:
                        parsed_body = json.loads(raw.decode("utf-8"))
                        if isinstance(parsed_body, dict):
                            body_req = parsed_body
                except Exception:
                    body_req = {}

            headers = getattr(self, "headers", {}) or {}
            node_id = (
                str(body_req.get("node_id") or body_req.get("node") or "")
                or (qs.get("node") or qs.get("node_id") or [""])[0]
                or headers.get("X-Node-Id")
                or ""
            ).strip()

            get_store_fn = getattr(gateway_mod, "get_pending_store", None)
            store = get_store_fn() if callable(get_store_fn) else None
            pending_item: Optional[Dict[str, Any]] = None
            pending_count = 0

            if store is not None:
                if node_id:
                    pending_item = store.peek_for_node(node_id)
                    if hasattr(store, "count_for_node"):
                        pending_count = int(store.count_for_node(node_id))
                    else:
                        pending_count = 1 if pending_item is not None else 0
                else:
                    # If no node_id was specified on the heartbeat, check default or any node in store
                    default_node = os.environ.get("MVG_DEFAULT_NODE", "gotchi-ryan-01")
                    pending_item = store.peek_for_node(default_node)
                    if pending_item is None and hasattr(store, "_store"):
                        for cand_node in list(getattr(store, "_store", {}).keys()):
                            pending_item = store.peek_for_node(cand_node)
                            if pending_item is not None:
                                node_id = cand_node
                                break
                    if pending_item is not None:
                        pending_count = (
                            int(store.count_for_node(node_id or default_node))
                            if hasattr(store, "count_for_node")
                            else 1
                        )

            has_pending = pending_item is not None
            payload = {
                "status": "ok",
                "node": node_id,
                "node_id": node_id,
                "has_pending": has_pending,
                "pending_count": pending_count,
                "pending": pending_item,
                "item": pending_item,
            }
            self._send_json(200, payload)
            log_req_fn = getattr(gateway_mod, "log_request", None)
            if callable(log_req_fn):
                now_s = getattr(gateway_mod, "time", __import__("time")).time()
                log_req_fn("/api/voice/heartbeat", (now_s - t0) * 1000, "ok", node=node_id, has_pending=has_pending)

        def patched_do_get(self: Any) -> Any:
            path = urlsplit(getattr(self, "path", "")).path
            if path == "/api/voice/heartbeat":
                return self._handle_heartbeat()
            if callable(orig_do_get):
                return orig_do_get(self)

        def patched_dispatch_post(self: Any) -> Any:
            path = urlsplit(getattr(self, "path", "")).path
            if path == "/api/voice/heartbeat":
                return self._handle_heartbeat()
            if callable(orig_dispatch_post):
                return orig_dispatch_post(self)

        handler_cls._handle_heartbeat = _handle_heartbeat
        handler_cls._orig_do_GET = orig_do_get
        handler_cls._orig_dispatch_post = orig_dispatch_post
        handler_cls.do_GET = patched_do_get
        handler_cls._dispatch_post = patched_dispatch_post

    gateway_mod._zero_redteam_patched = True
    return gateway_mod


def ensure_single_adapter_shim(
    shim_path: str | Path = DEFAULT_GOTCHI_ADAPTER_SHIM_PATH,
    sync_lomin: bool = True,
) -> Path:
    """
    Replaces `/workspace/gotchi/backend/gotchi_zero_adapter.py` with a clean re-export
    shim (`from tools.gotchi_zero_adapter import *`) and optionally syncs `/tmp/lomin-parity-repo`
    into `/workspace/gotchi/lomin/`.
    """
    if sync_lomin:
        sync_lomin_modules_to_gotchi()

    target = Path(shim_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if existing != ADAPTER_SHIM_CONTENT:
            target.write_text(ADAPTER_SHIM_CONTENT, encoding="utf-8")
    except Exception as exc:
        log.warning(f"[LominRuntime] Could not write adapter shim to {target}: {exc}")

    return target
