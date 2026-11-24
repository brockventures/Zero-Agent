"""Configuration, path resolution, kill switch, heartbeat, and memory scaffold for BRIEF."""

from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Any

DEFAULT_CHANNEL_ID = 1574655816236269569
DEFAULT_STATE_DIR = Path("/workspace/data/brief")
DEFAULT_MEMORY_DIR = Path("/workspace/memory/private/brief")
DEFAULT_SECRETS_PATH = Path("/secrets/env.json")
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "brief"


def _normalize_email(value: str) -> str:
    _, addr = parseaddr(str(value or "").strip())
    candidate = addr.strip().lower()
    if "@" in candidate:
        return candidate
    raw = str(value or "").strip().lower()
    return raw if "@" in raw else ""


def _parse_email_list(raw: Any) -> list[str]:
    if not raw:
        return []
    items: list[str]
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = _normalize_email(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _merge_unique_emails(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for email_addr in group:
            normalized = _normalize_email(email_addr)
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
    return merged


def _load_secrets_owner_emails(secrets_path: Path | None) -> list[str]:
    if secrets_path is None or not secrets_path.exists():
        return []
    try:
        payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []

    collected: list[str] = []
    for key in ("BRIEF_OWNER_EMAILS", "OWNER_EMAIL", "GMAIL_USER"):
        if key in payload:
            collected.extend(_parse_email_list(payload[key]))
    return collected


@dataclass
class BriefConfig:
    """Runtime configuration for BRIEF."""

    # [channel]
    channel_id: int = DEFAULT_CHANNEL_ID

    # [owner]
    allowlisted_emails: list[str] = field(default_factory=list)

    # [polling]
    gmail_interval_seconds: int = 120
    calendar_interval_seconds: int = 300
    router_interval_seconds: int = 15

    # [limits]
    max_events_per_hour: int = 30
    max_proactive_messages_per_day: int = 12
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    timezone: str = "America/Los_Angeles"

    # [sessions]
    email_grain: str = "thread"
    dm_max_turns: int = 40
    dm_max_days: int = 7
    intake_model: str = "gemini-4-hydrogen-high"
    task_model: str = "gemini-4-neon-medium"

    # [safety]
    dry_run: bool = True
    max_retries: int = 3
    stuck_delivery_seconds: int = 300

    # Runtime filesystem directories
    state_dir: Path = DEFAULT_STATE_DIR
    memory_dir: Path = DEFAULT_MEMORY_DIR
    prompts_dir: Path = DEFAULT_PROMPTS_DIR
    config_path: Path | None = None

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.memory_dir = Path(self.memory_dir)
        self.prompts_dir = Path(self.prompts_dir)
        if self.config_path is not None:
            self.config_path = Path(self.config_path)
        self.allowlisted_emails = _merge_unique_emails(self.allowlisted_emails)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "inbox.db"

    @property
    def cursors_path(self) -> Path:
        return self.state_dir / "cursors.json"

    @property
    def heartbeats_dir(self) -> Path:
        return self.state_dir / "heartbeats"

    @property
    def kill_switch_path(self) -> Path:
        return self.state_dir / "kill_switch.flag"

    @property
    def proposals_pending_dir(self) -> Path:
        return self.state_dir / "proposals" / "pending"

    @property
    def proposals_archive_dir(self) -> Path:
        return self.state_dir / "proposals" / "archive"

    @property
    def preferences_path(self) -> Path:
        return self.memory_dir / "preferences.md"

    @property
    def people_path(self) -> Path:
        return self.memory_dir / "people.md"

    @property
    def intake_dir(self) -> Path:
        return self.memory_dir / "intake"

    @property
    def task_dir(self) -> Path:
        return self.memory_dir / "task"

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
        *,
        state_dir: Path | str | None = None,
        memory_dir: Path | str | None = None,
        prompts_dir: Path | str | None = None,
        secrets_path: Path | str | None = DEFAULT_SECRETS_PATH,
    ) -> BriefConfig:
        """Load BriefConfig from explicit path, memory config, state config, or defaults.

        Owner allowlist emails are merged from TOML, BRIEF_OWNER_EMAILS env var,
        and /secrets/env.json (if present).
        """
        resolved_state = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR
        resolved_memory = Path(memory_dir) if memory_dir is not None else DEFAULT_MEMORY_DIR
        resolved_prompts = Path(prompts_dir) if prompts_dir is not None else DEFAULT_PROMPTS_DIR

        chosen_path: Path | None = None
        data: dict[str, Any] = {}

        if config_path is not None:
            explicit = Path(config_path)
            if not explicit.is_file():
                raise FileNotFoundError(f"BRIEF config file not found: {explicit}")
            chosen_path = explicit
        else:
            candidates = [
                resolved_memory / "config.toml",
                resolved_state / "config.toml",
            ]
            if resolved_memory != DEFAULT_MEMORY_DIR:
                candidates.append(DEFAULT_MEMORY_DIR / "config.toml")
            if resolved_state != DEFAULT_STATE_DIR:
                candidates.append(DEFAULT_STATE_DIR / "config.toml")
            # Deduplicate while preserving lookup priority:
            # explicit -> memory_dir/config.toml -> state_dir/config.toml
            # Wait: when custom state_dir/memory_dir are passed in tests without
            # config_path, avoid leaking host /workspace/memory/private/brief/config.toml.
            if memory_dir is not None or state_dir is not None:
                candidates = [
                    resolved_memory / "config.toml",
                    resolved_state / "config.toml",
                ]
            for candidate in candidates:
                if candidate.is_file():
                    chosen_path = candidate
                    break

        if chosen_path is not None:
            with chosen_path.open("rb") as f:
                loaded = tomllib.load(f)
            if isinstance(loaded, dict):
                data = loaded

        channel = data.get("channel", {})
        owner = data.get("owner", {})
        polling = data.get("polling", {})
        limits = data.get("limits", {})
        sessions = data.get("sessions", {})
        safety = data.get("safety", {})

        toml_emails = _parse_email_list(owner.get("allowlisted_emails", []))
        env_emails = _parse_email_list(os.environ.get("BRIEF_OWNER_EMAILS", ""))
        resolved_secrets = Path(secrets_path) if secrets_path is not None else None
        secret_emails = _load_secrets_owner_emails(resolved_secrets)

        return cls(
            channel_id=int(channel.get("channel_id", DEFAULT_CHANNEL_ID)),
            allowlisted_emails=_merge_unique_emails(toml_emails, env_emails, secret_emails),
            gmail_interval_seconds=int(polling.get("gmail_interval_seconds", 120)),
            calendar_interval_seconds=int(polling.get("calendar_interval_seconds", 300)),
            router_interval_seconds=int(polling.get("router_interval_seconds", 15)),
            max_events_per_hour=int(limits.get("max_events_per_hour", 30)),
            max_proactive_messages_per_day=int(limits.get("max_proactive_messages_per_day", 12)),
            quiet_hours_start=str(limits.get("quiet_hours_start", "22:00")),
            quiet_hours_end=str(limits.get("quiet_hours_end", "07:00")),
            timezone=str(limits.get("timezone", "America/Los_Angeles")),
            email_grain=str(sessions.get("email_grain", "thread")),
            dm_max_turns=int(sessions.get("dm_max_turns", 40)),
            dm_max_days=int(sessions.get("dm_max_days", 7)),
            intake_model=str(sessions.get("intake_model", "gemini-4-hydrogen-high")),
            task_model=str(sessions.get("task_model", "gemini-4-neon-medium")),
            dry_run=bool(safety.get("dry_run", True)),
            max_retries=int(safety.get("max_retries", 3)),
            stuck_delivery_seconds=int(safety.get("stuck_delivery_seconds", 300)),
            state_dir=resolved_state,
            memory_dir=resolved_memory,
            prompts_dir=resolved_prompts,
            config_path=chosen_path,
        )


def _resolve_kill_switch_path(target: BriefConfig | Path | str) -> Path:
    if isinstance(target, BriefConfig):
        return target.kill_switch_path
    path = Path(target)
    if path.name == "kill_switch.flag" or path.suffix == ".flag":
        return path
    return path / "kill_switch.flag"


def is_kill_switch_active(state_dir_or_config: BriefConfig | Path | str) -> bool:
    """Return True if the BRIEF kill_switch.flag file exists."""
    return _resolve_kill_switch_path(state_dir_or_config).exists()


def set_kill_switch(
    state_dir_or_config: BriefConfig | Path | str,
    active: bool,
    reason: str = "",
) -> Path:
    """Enable or disable the BRIEF kill_switch.flag atomically."""
    flag_path = _resolve_kill_switch_path(state_dir_or_config)
    if active:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        payload = reason.strip() or f"activated_at={int(time.time())}"
        tmp_path = flag_path.with_name(f".{flag_path.name}.tmp.{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(payload + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, flag_path)
    else:
        flag_path.unlink(missing_ok=True)
    return flag_path


def touch_heartbeat(
    heartbeats_dir: Path | str,
    name: str,
    now: float | None = None,
) -> Path:
    """Atomically write a component heartbeat timestamp (Unix epoch seconds)."""
    hb_dir = Path(heartbeats_dir)
    hb_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(name).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError(f"Invalid heartbeat name: {name!r}")
    target = hb_dir / f"{safe_name}.hb"
    tmp_path = hb_dir / f".{safe_name}.hb.tmp.{os.getpid()}"
    ts = time.time() if now is None else float(now)
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(f"{ts:.6f}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, target)
    return target


_DEFAULT_PREFERENCES_MD = """# BRIEF Preferences

- Timezone: America/Los_Angeles (PT)
- Quiet hours: 22:00-07:00 PT (urgent owner messages bypass; routine digests wait until morning)
- Default style: concise bullet points with explicit PT timestamps
"""

_DEFAULT_PEOPLE_MD = """# BRIEF People & Contacts

Record trusted senders, roles, and communication context here.
"""


def ensure_memory_scaffold(memory_dir: Path | str) -> Path:
    """Idempotently initialize BRIEF private memory directories and starter files."""
    root = Path(memory_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "intake").mkdir(parents=True, exist_ok=True)
    (root / "task").mkdir(parents=True, exist_ok=True)

    prefs = root / "preferences.md"
    if not prefs.exists():
        prefs.write_text(_DEFAULT_PREFERENCES_MD, encoding="utf-8")

    people = root / "people.md"
    if not people.exists():
        people.write_text(_DEFAULT_PEOPLE_MD, encoding="utf-8")

    return root
