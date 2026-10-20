#!/usr/bin/env python3
"""Live model registry, alias resolution, and fallback selection for the Discord bridge.

The `agy` CLI validates `--model` against a model list it fetches at runtime, and that list
changes over time (models are added and retired). This module keeps the bridge honest:

1. `list_available_models()` asks the CLI (`agy models`) what actually exists, with a short
   on-disk cache and a stale-cache fallback when the fetch fails.
2. `resolve_model()` maps user shortcuts (`sonnet`, `3.8-low`, `opus`, ...) onto real model IDs.
3. `preflight_model()` validates the configured model before a spawn and, if it is missing,
   swaps in the fallback (default: Claude Sonnet, medium effort) and returns a user-facing notice.
4. `is_model_error()` recognizes the CLI's model-rejection text so callers can retry reactively.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

DEFAULT_FALLBACK_MODEL = "claude-sonnet-5-5-medium"
CACHE_TTL_SECONDS = 600
FORCED_REFRESH_MIN_AGE_SECONDS = 60

MODEL_ERROR_RE = re.compile(r"invalid model selection|not recognized as a known model", re.IGNORECASE)

_EFFORT_ALIASES = {"low": "low", "lite": "low", "med": "medium", "medium": "medium", "high": "high"}
_EFFORT_PREFERENCE = ("high", "medium", "low")


def _data_dir() -> Path:
    from tools.bridge_state import DATA_DIR
    return Path(DATA_DIR)


def _cache_file() -> Path:
    return _data_dir() / "available_models.json"


def _config_file() -> Path:
    return _data_dir() / "runtime_config.json"


def is_model_error(text: str | None) -> bool:
    """True if CLI output is the 'model not recognized' rejection."""
    return bool(text and MODEL_ERROR_RE.search(text))


def parse_models_output(stdout: str) -> dict[str, str]:
    """Parse `agy models` output (`<id>\\t<label>` per line) into {id: label}."""
    models: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.rstrip().split("\t", 1)
        mid = parts[0].strip()
        if not mid or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", mid):
            continue
        models[mid] = parts[1].strip() if len(parts) > 1 and parts[1].strip() else mid
    return models


def _read_cache() -> dict | None:
    try:
        with open(_cache_file()) as f:
            data = json.load(f)
        if isinstance(data.get("models"), dict) and data["models"]:
            return data
    except Exception:
        pass
    return None


def _fetch_models(timeout: int = 45) -> dict[str, str]:
    """Query the CLI for its current model list. Returns {} on any failure."""
    try:
        res = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            return parse_models_output(res.stdout)
    except Exception:
        pass
    return {}


def _write_cache(models: dict[str, str]) -> None:
    try:
        tmp = _cache_file().with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f, indent=2)
        tmp.replace(_cache_file())
    except Exception:
        pass


def list_available_models(
    force: bool = False,
    max_age: float = CACHE_TTL_SECONDS,
    timeout: int = 45,
    cache_only: bool = False,
) -> dict[str, str]:
    """Return {model_id: label} for models the CLI currently accepts.

    Uses a fresh cache when possible (`force` still honors a short anti-hammering window).
    If `cache_only` is True, immediately returns whatever is cached on disk (or {}) without
    spawning a blocking `agy models` subprocess (essential for <3s Discord autocomplete).
    If the live fetch fails, returns the stale cache (or {} when nothing has ever been fetched).
    """
    cache = _read_cache()
    if cache_only:
        return cache["models"] if cache else {}
    age = (time.time() - cache.get("fetched_at", 0)) if cache else None
    if cache and not force and age < max_age:
        return cache["models"]
    if cache and force and age < FORCED_REFRESH_MIN_AGE_SECONDS:
        return cache["models"]

    models = _fetch_models(timeout)
    if models:
        _write_cache(models)
        return models
    return cache["models"] if cache else {}


def _norm_id(model_id: str) -> str:
    # claude-sonnet-5-5-medium -> claude-sonnet-5.5-medium (so "5.5" matches as one token)
    return re.sub(r"(?<=\d)-(?=\d)", ".", model_id.lower())


def _parse_id(model_id: str) -> tuple[list[str], str | None, tuple[int, ...]]:
    toks = _norm_id(model_id).split("-")
    effort = toks[-1] if toks[-1] in ("low", "medium", "high") else None
    base = toks[:-1] if effort else toks
    ver_tok = next((t for t in base if re.fullmatch(r"\d+(\.\d+)*", t)), "0")
    return base, effort, tuple(int(x) for x in ver_tok.split("."))


def resolve_model(requested: str | None, available: dict[str, str]) -> str | None:
    """Map a model ID or shortcut onto a real model ID, or None if nothing matches.

    Shortcuts are matched against the live list: `sonnet`, `claude` (= sonnet), `opus`, `gpt`,
    `flash`, `3.8`, `3.8-low`, `flash-med`, `sonnet-medium`. Without an effort suffix the
    highest available effort is used; with one, only that effort matches.
    """
    if not requested or not available:
        return None
    req = requested.strip()
    if req in available:
        return req

    req_n = _norm_id(req)
    m = re.search(r"-(low|lite|med|medium|high)$", req_n)
    effort = _EFFORT_ALIASES[m.group(1)] if m else None
    base_s = req_n[: m.start()] if m else req_n
    want = [t for t in re.split(r"[-\s]+", base_s) if t]
    if want == ["claude"]:
        want = ["sonnet"]
    if not want:
        return None

    cands = []
    for mid in available:
        base, eff, ver = _parse_id(mid)
        if all(w in base for w in want):
            cands.append((mid, eff, ver))
    if effort:
        cands = [c for c in cands if c[1] == effort]
    if not cands:
        return None

    def rank(c):
        pref = _EFFORT_PREFERENCE.index(c[1]) if c[1] in _EFFORT_PREFERENCE else len(_EFFORT_PREFERENCE)
        return (c[2], -pref)

    return max(cands, key=rank)[0]


def configured_fallback() -> str:
    """Fallback model from runtime_config.json (`fallback_model`), else the Sonnet-medium default."""
    try:
        with open(_config_file()) as f:
            return json.load(f).get("fallback_model") or DEFAULT_FALLBACK_MODEL
    except Exception:
        return DEFAULT_FALLBACK_MODEL


def get_fallback_model(available: dict[str, str], exclude: str | None = None) -> str | None:
    """Pick the fallback: configured model if real, else best Sonnet-medium, else any medium, else any."""
    pref = configured_fallback()
    if pref in available and pref != exclude:
        return pref
    sonnet = resolve_model("sonnet-medium", available)
    if sonnet and sonnet != exclude:
        return sonnet
    others = [m for m in available if m != exclude]
    for mid in others:
        if _parse_id(mid)[1] == "medium":
            return mid
    return others[0] if others else None


def format_fallback_notice(requested: str, fallback: str, reason: str = "is not available in the CLI model list") -> str:
    return (
        f"⚠️ **Model fallback:** `{requested}` {reason}. "
        f"Running on `{fallback}` instead. Pick a valid model with `!model`."
    )


def preflight_model(requested: str) -> tuple[str, str | None]:
    """Validate `requested` against the live model list before spawning the CLI.

    Returns (model_to_use, notice). `notice` is non-None exactly when a fallback was substituted.
    If the model list cannot be fetched at all, fail open and return the requested model.
    Because the CLI's list can vary between calls, a missing model triggers one fresh live
    re-fetch (bypassing the cache) before the fallback is used.
    """
    available = list_available_models()
    if not available or requested in available:
        return requested, None
    fresh = _fetch_models()
    if fresh:
        _write_cache(fresh)
        available = fresh
    if requested in available:
        return requested, None
    fallback = get_fallback_model(available, exclude=requested)
    if not fallback:
        return requested, None
    return fallback, format_fallback_notice(requested, fallback)


class ModelSelectionError(RuntimeError):
    """Raised when the CLI rejects the configured model at spawn time."""

    def __init__(self, model: str, detail: str = ""):
        super().__init__(f"CLI rejected model '{model}': {detail}".strip())
        self.model = model
        self.detail = detail


def set_fallback_model(model_id: str) -> None:
    """Persist the fallback model into runtime_config.json without touching other keys."""
    cfg: dict = {}
    try:
        with open(_config_file()) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        cfg = {}
    cfg["fallback_model"] = model_id
    with open(_config_file(), "w") as f:
        json.dump(cfg, f, indent=2)

CHANNEL_MODEL_OVERRIDES: dict[int, str] = {
    # Tier 1: Gemini 4 Argon (High) — Spatial CAD & 3D Geometry
    1555973316752449576: "gemini-4-argon-high",     # #3d-print
    # Tier 2: Gemini 4 Argon (Medium) — Infrastructure, Ops, Quant, Finance & Engineering
    1548196929308065893: "gemini-4-argon-medium",   # #baseball
    1544955532765560924: "gemini-4-argon-medium",   # #finances
    1550577910757458015: "gemini-4-argon-medium",   # #vault
    1544953279664889888: "gemini-4-argon-medium",   # #zero-ops
    1544953275877556334: "gemini-4-argon-medium",   # #home-assistant
    1544955535722545253: "gemini-4-argon-medium",   # #homelab
    1548196930788524094: "gemini-4-argon-medium",   # #projects
    1558202642663211169: "gemini-4-argon-medium",   # #agora
    1551465050072416286: "gemini-4-argon-medium",   # #side-project
    1561509863018856468: "gemini-4-argon-medium",   # #gotchi
    # Tier 3: Gemini 4 Argon (Low) — Fast Conversational Speed + Argon Quality
    1542081375287640084: "gemini-4-argon-low",      # #zero-chat
    1562246590943461429: "gemini-4-argon-low",      # #gotchi-chat
    1550577908811178095: "gemini-4-argon-low",      # #brock-house
    1544955538033348618: "gemini-4-argon-low",      # #shopping
    1544953277592899615: "gemini-4-argon-low",      # #steam-deck
    1534436119888793750: "gemini-4-argon-low",      # #the-banana-stand
    1534452820995080192: "gemini-4-argon-low",      # #lounge
    # Tier 4: Gemini 3.8 Flash (Low) — Pure Notification & Webhook Channels
    1330447543477338202: "gemini-3.8-flash-low",    # #server-updates
    1210466877835313155: "gemini-3.8-flash-low",    # #seerr-notifications
    1453427860793463000: "gemini-3.8-flash-low",    # #seerr-requests-and-chat
}

SYNTHESIS_TURN_MODEL = "gemini-3.8-flash-low"


def get_channel_model_override(
    channel_id: int | str | None = None,
    channel_name: str | None = None,
) -> str | None:
    """Return a pinned model ID for specific channels across the 4-tier architecture, or None."""
    if channel_id is not None and str(channel_id).isdigit():
        cid = int(channel_id)
        if cid in CHANNEL_MODEL_OVERRIDES:
            return CHANNEL_MODEL_OVERRIDES[cid]
    if channel_name:
        cname = str(channel_name).strip().lstrip("#").lower()
        if cname == "3d-print":
            return "gemini-4-argon-high"
        if cname in (
            "baseball",
            "finances",
            "vault",
            "zero-ops",
            "harness-management",
            "home-assistant",
            "homelab",
            "projects",
            "agora",
            "side-project",
            "gotchi",
        ):
            return "gemini-4-argon-medium"
        if cname in ("zero-chat", "gotchi-chat", "gotchi-log", "brock-house", "shopping", "steam-deck", "the-banana-stand", "lounge"):
            return "gemini-4-argon-low"
        if cname == "server-updates" or cname.startswith("seer"):
            return "gemini-3.8-flash-low"
    return None

