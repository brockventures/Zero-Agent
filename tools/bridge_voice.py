#!/usr/bin/env python3
"""
Zero Discord Voice Live Engine (Gemini Multimodal Live API + Discord DAVE E2EE Voice)

Provides real-time, low-latency bidirectional voice conversation with Zero inside
Discord voice channels (specifically `#zero-voice`, ID: 1560924105279275042) powered by
Google's Gemini Live API (`gemini-3.8-live` / `gemini-2.5-flash-native-audio-latest`).

Features:
- Full Discord DAVE E2EE (MLS) decryption & encryption compatibility (`davey` + `PyNaCl`).
- Automatic 48kHz stereo Discord PCM <-> 16kHz/24kHz mono Gemini Live PCM resampling.
- Echo-safe barge-in (interruption) support: clears outgoing playback buffer only on sustained
  human speech energy (preventing accidental mid-sentence self-cutoff from mic bleed or coughs).
- Explicit English (`en-US`) language pinning on `SpeechConfig` so the Live model never drifts
  into Spanish or other languages on ambient noise.
- Full Agent Parity with the regular Discord agent:
  - Pre-loads `/workspace/GEMINI.md`, `/workspace/memory/private/user_ryan.md`, active tasks,
    and recent `#zero-chat` messages into the initial WebSocket `system_instruction` (sent once).
  - Enables `ContextWindowCompressionConfig(sliding_window=SlidingWindow())` for automatic
    sliding-window compaction on long calls.
  - Equips the Live model with full tool parity (`run_shell_command`, `read_local_file`,
    `write_local_file`, `search_zero_memory`, `check_tasks_and_repos`,
    `get_recent_discord_context`, `web_and_reddit_search`, `post_note_to_zero_chat`).
  - Persists live voice transcripts into `/workspace/data/voice_transcripts.json` and
    `#zero-chat` `channel_history.json` so text and voice share seamless conversational state.
"""

from __future__ import annotations

import asyncio
import audioop
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import voice_recv

try:
    import davey
    HAS_DAVEY = True
except ImportError:
    HAS_DAVEY = False

from google import genai
from google.genai import types

from tools.bridge_state import BROCK_GUILD_ID, OWNER_USER_ID, TARGET_CHANNEL_ID

log = logging.getLogger("bridge_voice")
PT_TZ = ZoneInfo("America/Los_Angeles")

ZERO_VOICE_CHANNEL_ID = 1560924105279275042
DEFAULT_LIVE_MODEL = "gemini-3.8-live"
FALLBACK_LIVE_MODEL = "gemini-2.5-flash-native-audio-latest"
DEFAULT_VOICE_NAME = "Schedar"  # Even, steady, composed lower-middle male voice in Gemini Live
VOICE_TRANSCRIPT_FILE = Path("/workspace/data/voice_transcripts.json")
CHANNEL_HISTORY_FILE = Path("/workspace/data/channel_history.json")


def _ensure_opus_loaded() -> bool:
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
        return discord.opus.is_loaded()
    except Exception as e:
        log.warning(f"[ZeroVoice] Failed to load opus: {e}")
        return False


def _append_voice_turn_to_history(speaker: str, text: str) -> None:
    """Persist voice turns to both `/workspace/data/voice_transcripts.json` and `#zero-chat`
    `channel_history.json` so text turns in `#zero-chat` have full context of what was said on voice!
    """
    clean = " ".join(text.strip().split())
    if not clean:
        return
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_pt = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT")

    try:
        VOICE_TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        items = []
        if VOICE_TRANSCRIPT_FILE.exists():
            try:
                items = json.loads(VOICE_TRANSCRIPT_FILE.read_text())
            except Exception:
                items = []
        items.append({"timestamp_utc": now_utc, "timestamp_pt": now_pt, "speaker": speaker, "text": clean})
        VOICE_TRANSCRIPT_FILE.write_text(json.dumps(items[-200:], indent=2))
    except Exception as e:
        log.debug(f"[ZeroVoice] Failed writing voice_transcripts.json: {e}")

    try:
        if CHANNEL_HISTORY_FILE.exists():
            ch_data = json.loads(CHANNEL_HISTORY_FILE.read_text())
            ch_key = str(TARGET_CHANNEL_ID)
            if ch_key in ch_data and isinstance(ch_data[ch_key], list):
                ch_data[ch_key].append(
                    {
                        "author": speaker,
                        "content": f"[🎙️ #zero-voice] {clean}",
                        "timestamp": now_utc,
                        "timestamp_pt": now_pt,
                    }
                )
                ch_data[ch_key] = ch_data[ch_key][-50:]
                CHANNEL_HISTORY_FILE.write_text(json.dumps(ch_data, indent=2))
    except Exception as e:
        log.debug(f"[ZeroVoice] Failed syncing voice turn to channel_history.json: {e}")


class StreamingPCMAudioSource(discord.AudioSource):
    """Real-time 20ms frame (3840-byte 48kHz stereo 16-bit PCM) ring buffer for Discord playback.

    Never returns b'' (EOF) while active, instead returning 20ms of silence (zeros) when the buffer
    is empty so the Discord AudioPlayer thread stays alive continuously across multi-turn conversations.
    """

    FRAME_SIZE = 3840  # 20ms at 48000Hz * 2 channels * 2 bytes

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._resample_state = None
        self.is_speaking_out = False
        self.last_spoke_out_ts = 0.0
        self.output_audio_seconds = 0.0

    def is_opus(self) -> bool:
        return False

    def feed_gemini_pcm_24k_mono(self, pcm_24k_mono: bytes) -> None:
        """Convert 24kHz 16-bit mono PCM from Gemini Live into 48kHz 16-bit stereo PCM and enqueue."""
        if not pcm_24k_mono or self._closed:
            return
        try:
            pcm_48k_mono, self._resample_state = audioop.ratecv(
                pcm_24k_mono, 2, 1, 24000, 48000, self._resample_state
            )
            pcm_48k_stereo = audioop.tostereo(pcm_48k_mono, 2, 1.0, 1.0)
            with self._lock:
                self._buf.extend(pcm_48k_stereo)
                self.output_audio_seconds += len(pcm_24k_mono) / (24000 * 2)
        except Exception as e:
            log.warning(f"[ZeroVoice] Error resampling output PCM: {e}")

    def clear_buffer(self) -> None:
        """Immediately flush pending playback audio (used when Ryan interrupts/barges in)."""
        with self._lock:
            self._buf.clear()
            self._resample_state = None
            self.is_speaking_out = False

    def read(self) -> bytes:
        if self._closed:
            return b""
        with self._lock:
            if len(self._buf) >= self.FRAME_SIZE:
                frame = bytes(self._buf[: self.FRAME_SIZE])
                del self._buf[: self.FRAME_SIZE]
                self.is_speaking_out = True
                self.last_spoke_out_ts = time.monotonic()
                return frame
            elif len(self._buf) > 0:
                frame = bytes(self._buf) + b"\x00" * (self.FRAME_SIZE - len(self._buf))
                self._buf.clear()
                self.is_speaking_out = True
                self.last_spoke_out_ts = time.monotonic()
                return frame
            else:
                self.is_speaking_out = False
                return b"\x00" * self.FRAME_SIZE

    def cleanup(self) -> None:
        self._closed = True
        with self._lock:
            self._buf.clear()


class GeminiLiveAudioSink(voice_recv.AudioSink):
    """Receives incoming RTP audio packets from Discord, handles DAVE E2EE decryption if active,
    decodes Opus -> 48kHz stereo PCM -> downsamples to 16kHz mono PCM, and pushes 40ms frames
    to an async queue for low-latency streaming to Gemini Live.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        audio_in_queue: asyncio.Queue,
        audio_source: Optional[StreamingPCMAudioSource] = None,
        target_user_id: int = OWNER_USER_ID,
    ):
        super().__init__()
        self.loop = loop
        self.audio_in_queue = audio_in_queue
        self.audio_source = audio_source
        self.target_user_id = target_user_id
        self._opus_decoder = discord.opus.Decoder()
        self._resample_state = None
        self._pcm_16k_accum = bytearray()
        # Send 40ms chunks (640 samples = 1280 bytes at 16kHz 16-bit mono) for minimal framing latency
        self._chunk_bytes = 1280
        self.input_audio_seconds = 0.0
        self.last_voice_packet_ts = 0.0
        self._consecutive_barge_packets = 0

    def wants_opus(self) -> bool:
        return True

    def write(self, user: Optional[discord.abc.User], data: voice_recv.VoiceData) -> None:
        uid = getattr(user, "id", None) or self.target_user_id
        if user is not None and getattr(user, "bot", False):
            return
        if uid != self.target_user_id:
            return

        opus_bytes = data.opus
        if not opus_bytes or opus_bytes == b"\xf8\xff\xfe":
            return

        # DAVE E2EE Decryption Layer
        vc = self.voice_client
        if HAS_DAVEY and vc and getattr(vc, "_connection", None):
            conn = vc._connection
            dave_sess = getattr(conn, "dave_session", None)
            if dave_sess is not None and getattr(conn, "dave_protocol_version", 0) > 0:
                try:
                    if not dave_sess.can_passthrough(uid):
                        opus_bytes = dave_sess.decrypt(uid, davey.MediaType.audio, opus_bytes)
                    else:
                        try:
                            opus_bytes = dave_sess.decrypt(uid, davey.MediaType.audio, opus_bytes)
                        except Exception:
                            pass
                except Exception as de:
                    log.debug(f"[ZeroVoice] DAVE decrypt skipped/failed for packet (len={len(opus_bytes)}): {de}")
                    return

        # Decode Opus -> 48kHz 16-bit stereo PCM
        try:
            pcm_48k_stereo = self._opus_decoder.decode(opus_bytes, fec=False)
        except Exception as oe:
            log.debug(f"[ZeroVoice] Opus decode error: {oe}")
            return

        # Downmix stereo -> mono and downsample 48000Hz -> 16000Hz (3:1 ratio)
        try:
            pcm_48k_mono = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
            pcm_16k_mono, self._resample_state = audioop.ratecv(
                pcm_48k_mono, 2, 1, 48000, 16000, self._resample_state
            )

            # Echo & Self-Cutoff Prevention:
            # Only forward incoming audio while Zero is speaking if it has real sustained speech energy
            # (>= 8 consecutive 20ms packets = 160ms of RMS > 1600, i.e., genuine human barge-in).
            is_zero_speaking = bool(
                self.audio_source
                and (self.audio_source.is_speaking_out or (time.monotonic() - self.audio_source.last_spoke_out_ts < 0.25))
            )
            if is_zero_speaking:
                rms = audioop.rms(pcm_16k_mono, 2)
                if rms > 1600:
                    self._consecutive_barge_packets += 1
                    if self._consecutive_barge_packets >= 8 and self.audio_source:
                        self.audio_source.clear_buffer()
                else:
                    self._consecutive_barge_packets = 0
                    return
            else:
                self._consecutive_barge_packets = 0

            self._pcm_16k_accum.extend(pcm_16k_mono)
            self.input_audio_seconds += len(pcm_16k_mono) / (16000 * 2)
            self.last_voice_packet_ts = time.monotonic()

            while len(self._pcm_16k_accum) >= self._chunk_bytes:
                chunk = bytes(self._pcm_16k_accum[: self._chunk_bytes])
                del self._pcm_16k_accum[: self._chunk_bytes]
                self.loop.call_soon_threadsafe(self._enqueue_safe, chunk)
        except Exception as re_err:
            log.warning(f"[ZeroVoice] Resample input error: {re_err}")

    def flush_partial(self) -> Optional[bytes]:
        if len(self._pcm_16k_accum) > 0:
            chunk = bytes(self._pcm_16k_accum)
            self._pcm_16k_accum.clear()
            return chunk
        return None

    def _enqueue_safe(self, chunk: bytes) -> None:
        if self.audio_in_queue.qsize() > 80:
            try:
                self.audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.audio_in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    def cleanup(self) -> None:
        self._pcm_16k_accum.clear()


def _load_startup_context_snapshot() -> str:
    """Pre-load Zero's operational reference (`GEMINI.md`), Ryan's core profile (`user_ryan.md`),
    active tasks (`tasks.json`), and recent `#zero-chat` messages right into the initial
    WebSocket `system_instruction` (transmitted once per session).
    """
    sections = []

    # 1. Core Operational Reference (/workspace/GEMINI.md)
    try:
        gemini_md = Path("/workspace/GEMINI.md")
        if gemini_md.exists():
            sections.append("=== ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md) ===\n" + gemini_md.read_text()[:10000])
    except Exception:
        pass

    # 2. Ryan Brock Core Profile (/workspace/memory/private/user_ryan.md)
    try:
        ryan_md = Path("/workspace/memory/private/user_ryan.md")
        if ryan_md.exists():
            sections.append("=== RYAN BROCK PROFILE (/workspace/memory/private/user_ryan.md) ===\n" + ryan_md.read_text()[:8500])
    except Exception:
        pass

    # 3. Pending tasks from /workspace/data/tasks.json
    try:
        t_file = Path("/workspace/data/tasks.json")
        if t_file.exists():
            tasks = json.loads(t_file.read_text())
            pending = [t for t in tasks if t.get("status") in ("pending", "in_progress", "parking_lot")]
            if pending:
                lines = [f"  - [#{t.get('id')}] ({t.get('status')}) {t.get('title')}" for t in pending[-15:]]
                sections.append("=== ACTIVE ZERO TASKS (/workspace/data/tasks.json) ===\n" + "\n".join(lines))
    except Exception:
        pass

    # 4. Recent #zero-chat messages
    try:
        if CHANNEL_HISTORY_FILE.exists():
            ch = json.loads(CHANNEL_HISTORY_FILE.read_text()).get(str(TARGET_CHANNEL_ID), [])
            if ch:
                lines = []
                for m in ch[-10:]:
                    author = m.get("author", "User")
                    content = " ".join(str(m.get("content", "")).split())[:220]
                    lines.append(f"  - {author}: {content}")
                sections.append("=== RECENT #zero-chat CONTEXT ===\n" + "\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(sections)


def _build_zero_live_system_prompt() -> str:
    now_pt = datetime.now(PT_TZ).strftime("%A, %b %d, %Y %I:%M %p PT")
    snapshot = _load_startup_context_snapshot()
    return (
        f"You are Zero, Ryan Brock's autonomous AI engineering partner running on Host 2 (.84 LAN) in the "
        f"Brock homelab (Redwood City, CA). Current time: {now_pt}.\n\n"
        "LANGUAGE & VOICE INVARIANT:\n"
        "- Speak STRICTLY in English (`en-US`) at all times. NEVER switch to Spanish or any other language, "
        "even if background audio or noise sounds ambiguous.\n"
        "- Razor-sharp, supremely capable staff engineer / collaborative partner (think Tony Stark in "
        "problem-solving mode or Ben Wyatt / Jerry Seinfeld wryly noting technical absurdity).\n"
        "- Calm mastery, dry wit, warm camaraderie, zero subservience. Never say 'I hope this helps!' or "
        "'Let me know if you need anything else!'\n"
        "- Keep spoken answers tight, natural, and conversational (1 to 3 sentences by default unless Ryan asks for detail).\n"
        "- NEVER repeat status announcements like 'I am online' once the call has started.\n\n"
        "FULL AGENT PARITY & TOOL ACCESS:\n"
        "- You have 100% of the same container access, filesystem access, memory search, GitHub CLI (`gh`), "
        "SSH, Home Assistant, and research tools as the regular Discord text agent!\n"
        "- NEVER claim you don't have access to local files, repositories, task boards, or homelab systems.\n"
        "- Use `search_zero_memory` to search durable memory or past transcripts (`memory` or `transcripts`).\n"
        "- Use `check_tasks_and_repos` to inspect `/workspace/data/tasks.json`, Google Tasks, and GitHub issues "
        "across `brockventures/big-projections`, `brockventures/sharkfin`, and `brockventures/homeassistant-config`.\n"
        "- Use `run_shell_command` to run ANY bash command in `/workspace` (e.g. `gh`, `git`, `python3`, `curl`, "
        "checking `/baseball/big-projections/`, querying Home Assistant, checking Docker containers, etc.).\n"
        "- Use `read_local_file` and `write_local_file` to inspect or edit files directly on disk.\n"
        "- Use `web_and_reddit_search` to run live Reddit (`reddit_extractor.py`) or Amazon (`amazon_serpapi.py`) queries.\n"
        "- Use `get_recent_discord_context` to read recent messages in `#zero-chat`, `#baseball`, `#zero-ops`, `#homelab`, or `#finances`.\n"
        "- Use `post_note_to_zero_chat` whenever Ryan asks you to drop a link, snippet, or note into `#zero-chat`.\n\n"
        f"{snapshot}"
    )


def _build_live_tools() -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="search_zero_memory",
                    description="Hybrid BM25 + dense vector search across all of Zero's durable memory files (/workspace/memory/) and past conversation transcripts.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "query": types.Schema(
                                type="STRING",
                                description="Keywords or semantic question to search for in Zero's memory index.",
                            ),
                            "target": types.Schema(
                                type="STRING",
                                description="Search target: 'memory' (default, durable memory files) or 'transcripts' (past session logs).",
                            ),
                        },
                        required=["query"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="check_tasks_and_repos",
                    description="Check Ryan's task board (/workspace/data/tasks.json) AND open GitHub issues/epics across repositories ('big-projections' for the BIG baseball projection system, 'sharkfin', 'homeassistant-config').",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "repo_or_filter": types.Schema(
                                type="STRING",
                                description="Optional repository name ('big-projections', 'sharkfin', 'homeassistant-config', or 'all') or keyword filter.",
                            )
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="run_shell_command",
                    description="Execute a non-interactive bash command inside the Zero container (/workspace) with full access to gh, git, python3, tools, /baseball/big-projections, Home Assistant APIs, etc. Never restart services without explicit confirmation.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "command": types.Schema(
                                type="STRING",
                                description="The bash command line to run (bound outputs with head/tail; avoid server-wide grep).",
                            )
                        },
                        required=["command"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="read_local_file",
                    description="Read the contents of any local file on disk (e.g. /workspace/memory/..., /baseball/big-projections/..., /workspace/tools/...).",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "file_path": types.Schema(
                                type="STRING",
                                description="Absolute path to the file.",
                            ),
                            "max_chars": types.Schema(
                                type="INTEGER",
                                description="Maximum characters to return (default 4000).",
                            ),
                        },
                        required=["file_path"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="write_local_file",
                    description="Write or update a local file on disk when Ryan asks to save a note, update a config, or edit code during a voice call.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "file_path": types.Schema(
                                type="STRING",
                                description="Absolute path to the file.",
                            ),
                            "content": types.Schema(
                                type="STRING",
                                description="Full text content to write.",
                            ),
                        },
                        required=["file_path", "content"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="web_and_reddit_search",
                    description="Search Reddit (via /workspace/tools/reddit_extractor.py) or Amazon (via /workspace/tools/amazon_serpapi.py) for live community threads, active subreddits, or products.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "query": types.Schema(
                                type="STRING",
                                description="Search query string.",
                            ),
                            "subreddit": types.Schema(
                                type="STRING",
                                description="Optional specific subreddit name (without r/) to scope the search.",
                            ),
                            "mode": types.Schema(
                                type="STRING",
                                description="'reddit' (default) or 'amazon'.",
                            ),
                        },
                        required=["query"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="get_recent_discord_context",
                    description="Fetch the most recent messages from Discord channels (#zero-chat, #baseball, #zero-ops, #homelab, #finances, #shopping, #projects).",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "channel_name": types.Schema(
                                type="STRING",
                                description="Channel name to inspect: 'zero-chat', 'baseball', 'zero-ops', 'homelab', 'finances', 'shopping', or 'projects'.",
                            )
                        },
                    ),
                ),
                types.FunctionDeclaration(
                    name="post_note_to_zero_chat",
                    description="Send a text summary, code snippet, link, or action item into #zero-chat on Discord during a voice call.",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "message": types.Schema(
                                type="STRING",
                                description="Markdown text to post to #zero-chat.",
                            )
                        },
                        required=["message"],
                    ),
                ),
            ]
        )
    ]


class ZeroVoiceSessionManager:
    """Manages the lifecycle of a Discord Voice <-> Gemini Live API session."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.voice_client: Optional[voice_recv.VoiceRecvClient] = None
        self.audio_source: Optional[StreamingPCMAudioSource] = None
        self.audio_sink: Optional[GeminiLiveAudioSink] = None
        self.live_task: Optional[asyncio.Task] = None
        self.active_model: str = DEFAULT_LIVE_MODEL
        self.voice_name: str = DEFAULT_VOICE_NAME
        self.started_at: Optional[float] = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_connected() and self.live_task and not self.live_task.done())

    def get_status_dict(self) -> dict[str, Any]:
        uptime = int(time.time() - self.started_at) if self.started_at and self.is_active else 0
        in_sec = round(self.audio_sink.input_audio_seconds, 1) if self.audio_sink else 0.0
        out_sec = round(self.audio_source.output_audio_seconds, 1) if self.audio_source else 0.0
        est_in_tokens = int(in_sec * 25)
        est_out_tokens = int(out_sec * 25)
        return {
            "active": self.is_active,
            "channel": getattr(getattr(self.voice_client, "channel", None), "name", None),
            "channel_id": getattr(getattr(self.voice_client, "channel", None), "id", None),
            "model": self.active_model,
            "voice": self.voice_name,
            "uptime_seconds": uptime,
            "input_audio_seconds": in_sec,
            "output_audio_seconds": out_sec,
            "est_audio_tokens": est_in_tokens + est_out_tokens,
        }

    async def start_session(
        self,
        voice_channel: discord.VoiceChannel,
        notify_channel: Optional[discord.abc.Messageable] = None,
        model_override: Optional[str] = None,
    ) -> tuple[bool, str]:
        async with self._lock:
            _ensure_opus_loaded()
            if self.is_active:
                if self.voice_client.channel.id == voice_channel.id:
                    return True, f"🎙️ Already connected to **#{voice_channel.name}** on `{self.active_model}`."
                await self._stop_unlocked()

            if model_override:
                self.active_model = model_override

            self._stop_event.clear()
            last_err: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    existing_vc = getattr(voice_channel.guild, "voice_client", None)
                    if existing_vc is not None:
                        try:
                            await existing_vc.disconnect(force=True)
                        except Exception:
                            pass
                    self.voice_client = await voice_channel.connect(
                        cls=voice_recv.VoiceRecvClient,
                        timeout=20.0,
                        reconnect=True,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    err_desc = str(e) or type(e).__name__
                    log.warning(
                        f"[ZeroVoice] Voice connect attempt {attempt}/3 to {voice_channel.id} failed: {err_desc}"
                    )
                    existing_vc = getattr(voice_channel.guild, "voice_client", None)
                    if existing_vc is not None:
                        try:
                            await existing_vc.disconnect(force=True)
                        except Exception:
                            pass
                    if attempt < 3:
                        await asyncio.sleep(1.5)

            if last_err is not None or self.voice_client is None:
                err_desc = str(last_err) or type(last_err).__name__
                log.error(f"[ZeroVoice] Failed to connect to voice channel {voice_channel.id}: {err_desc}")
                return False, f"⚠️ Failed to join voice channel **#{voice_channel.name}**: `{err_desc}`"

            audio_in_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=120)
            self.audio_source = StreamingPCMAudioSource()
            self.audio_sink = GeminiLiveAudioSink(
                asyncio.get_running_loop(),
                audio_in_queue,
                audio_source=self.audio_source,
                target_user_id=OWNER_USER_ID,
            )

            try:
                self.voice_client.play(self.audio_source)
                self.voice_client.listen(self.audio_sink)
            except Exception as pe:
                log.error(f"[ZeroVoice] Error starting player/listener: {pe}")
                await self._stop_unlocked()
                return False, f"⚠️ Connected to voice but failed to initialize audio pipeline: `{pe}`"

            self.started_at = time.time()
            self.live_task = asyncio.create_task(
                self._run_gemini_live_loop(audio_in_queue, notify_channel),
                name="zero-gemini-live-loop",
            )
            return True, (
                f"🎙️ **Zero Live Voice Online** in <#{voice_channel.id}>\n"
                f"- **Model:** `{self.active_model}` (Native Bidirectional Audio + Full Agent Parity)\n"
                f"- **Voice:** `{self.voice_name}` (`en-US`) | **Encryption:** DAVE E2EE Ready\n"
                f"- Hop into <#{voice_channel.id}> to talk hands-free, or use `!voice leave` when done."
            )

    async def stop_session(self) -> str:
        async with self._lock:
            stats = self.get_status_dict()
            await self._stop_unlocked()
            if stats["active"] or stats["uptime_seconds"] > 0:
                return (
                    f"🛑 **Disconnected from Voice** (`{stats['model']}`).\n"
                    f"- **Session Duration:** {stats['uptime_seconds']}s | "
                    f"**Audio Processed:** {stats['input_audio_seconds']}s in / {stats['output_audio_seconds']}s out "
                    f"(~{stats['est_audio_tokens']:,} audio tokens)."
                )
            return "ℹ️ Zero is not currently in a voice channel."

    async def _stop_unlocked(self) -> None:
        self._stop_event.set()
        if self.live_task and not self.live_task.done():
            self.live_task.cancel()
            try:
                await asyncio.wait_for(self.live_task, timeout=3.0)
            except Exception:
                pass
        self.live_task = None

        if self.voice_client:
            try:
                self.voice_client.stop()
            except Exception:
                pass
            try:
                await self.voice_client.disconnect(force=True)
            except Exception:
                pass
        self.voice_client = None

        if self.audio_source:
            self.audio_source.cleanup()
            self.audio_source = None
        if self.audio_sink:
            self.audio_sink.cleanup()
            self.audio_sink = None
        self.started_at = None

    async def _execute_tool_call(self, fn_call: Any, notify_channel: Optional[discord.abc.Messageable]) -> dict[str, Any]:
        name = getattr(fn_call, "name", "")
        args = getattr(fn_call, "args", {}) or {}
        log.info(f"[ZeroVoice] Executing Live tool call: {name}({args})")

        if name == "search_zero_memory":
            query = str(args.get("query", "")).strip()
            target = str(args.get("target", "memory")).strip().lower()
            if target not in ("memory", "transcripts", "all"):
                target = "memory"
            if not query:
                return {"result": "Empty query."}
            try:
                res = await asyncio.to_thread(
                    subprocess.run,
                    ["python3", "/workspace/tools/transcript_index.py", "search", query, "--target", target, "--limit", "6"],
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                return {"result": (res.stdout or "No matches found.")[:3500]}
            except Exception as e:
                return {"error": str(e)}

        if name == "check_tasks_and_repos":
            repo_filter = str(args.get("repo_or_filter", "all")).strip().lower()
            out_parts = []
            try:
                t_file = Path("/workspace/data/tasks.json")
                if t_file.exists():
                    tasks = json.loads(t_file.read_text())
                    pending = [t for t in tasks if t.get("status") != "completed"]
                    out_parts.append(
                        "Local Pending Tasks (/workspace/data/tasks.json):\n"
                        + "\n".join(f"- #{t.get('id')} [{t.get('status')}]: {t.get('title')}" for t in pending)
                    )
                repos = ["brockventures/big-projections"]
                if "sharkfin" in repo_filter or repo_filter == "all":
                    repos.append("brockventures/sharkfin")
                if "home" in repo_filter or "ha" in repo_filter or repo_filter == "all":
                    repos.append("brockventures/homeassistant-config")

                for r in repos:
                    gh_res = await asyncio.to_thread(
                        subprocess.run,
                        ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "15"],
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                    if gh_res.stdout.strip():
                        out_parts.append(f"Open GitHub Issues in {r}:\n{gh_res.stdout.strip()}")

                for bp_doc in ["/baseball/big-projections/ROADMAP.md", "/baseball/big-projections/TODO.md", "/baseball/big-projections/README.md"]:
                    p = Path(bp_doc)
                    if p.exists():
                        out_parts.append(f"{bp_doc} snippet:\n{p.read_text()[:1500]}")
                        break
            except Exception as e:
                out_parts.append(f"Error checking tasks/repos: {e}")
            return {"result": "\n\n".join(out_parts)[:4000]}

        if name == "run_shell_command":
            cmd_str = str(args.get("command", "")).strip()
            if not cmd_str:
                return {"error": "Empty command"}
            try:
                res = await asyncio.to_thread(
                    subprocess.run,
                    cmd_str,
                    shell=True,
                    cwd="/workspace",
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                combined = (res.stdout or "") + ("\nSTDERR:\n" + res.stderr if res.stderr else "")
                return {"exit_code": res.returncode, "output": combined[:4000] or "(no output)"}
            except Exception as e:
                return {"error": str(e)}

        if name == "read_local_file":
            fpath = str(args.get("file_path", "")).strip()
            max_chars = int(args.get("max_chars", 4000) or 4000)
            try:
                p = Path(fpath)
                if not p.exists():
                    return {"error": f"File not found: {fpath}"}
                return {"content": p.read_text(errors="replace")[:max_chars]}
            except Exception as e:
                return {"error": str(e)}

        if name == "write_local_file":
            fpath = str(args.get("file_path", "")).strip()
            content = str(args.get("content", ""))
            try:
                p = Path(fpath)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                return {"status": f"Successfully wrote {len(content)} chars to {fpath}"}
            except Exception as e:
                return {"error": str(e)}

        if name == "web_and_reddit_search":
            query = str(args.get("query", "")).strip()
            sub = str(args.get("subreddit", "")).strip()
            mode = str(args.get("mode", "reddit")).strip().lower()
            if not query:
                return {"error": "Empty query"}
            try:
                if mode == "amazon":
                    cmd = ["python3", "/workspace/tools/amazon_serpapi.py", query]
                else:
                    cmd = ["python3", "/workspace/tools/reddit_extractor.py", "search", query, "-n", "5"]
                    if sub:
                        cmd.extend(["--sub", sub])
                res = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=18,
                )
                return {"result": (res.stdout or res.stderr or "No results")[:3500]}
            except Exception as e:
                return {"error": str(e)}

        if name == "get_recent_discord_context":
            ch_name = str(args.get("channel_name", "zero-chat")).strip().lower()
            ch_map = {
                "zero-chat": str(TARGET_CHANNEL_ID),
                "baseball": "1548196929308065893",
                "zero-ops": "1544953279664889888",
                "homelab": "1548196930788524094",
                "finances": "1544955532765560924",
                "shopping": "1544955538033348618",
                "projects": "1544955535722545253",
            }
            cid = ch_map.get(ch_name, str(TARGET_CHANNEL_ID))
            try:
                if CHANNEL_HISTORY_FILE.exists():
                    msgs = json.loads(CHANNEL_HISTORY_FILE.read_text()).get(cid, [])[-12:]
                    formatted = "\n".join(
                        f"[{m.get('timestamp_pt') or m.get('timestamp')}] {m.get('author')}: {str(m.get('content'))[:300]}"
                        for m in msgs
                    )
                    return {"result": formatted or "No recent messages."}
            except Exception as e:
                return {"error": str(e)}

        if name == "post_note_to_zero_chat":
            msg_text = str(args.get("message", "")).strip()
            target_ch = notify_channel or self.bot.get_channel(TARGET_CHANNEL_ID)
            if target_ch and msg_text:
                try:
                    await target_ch.send(f"🎙️ **[Voice Session Note]:**\n{msg_text}")
                    return {"status": "posted to #zero-chat"}
                except Exception as e:
                    return {"error": str(e)}
            return {"error": "No target channel or empty message"}

        return {"error": f"Unknown function {name}"}

    async def _run_gemini_live_loop(
        self,
        audio_in_queue: asyncio.Queue[bytes],
        notify_channel: Optional[discord.abc.Messageable],
    ) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            log.error("[ZeroVoice] GEMINI_API_KEY missing!")
            return

        client = genai.Client(api_key=api_key)
        models_to_try = [self.active_model]
        if FALLBACK_LIVE_MODEL not in models_to_try:
            models_to_try.append(FALLBACK_LIVE_MODEL)

        greeted_once = False

        while not self._stop_event.is_set():
            connected_ok = False
            for model_id in models_to_try:
                if self._stop_event.is_set():
                    break
                try:
                    config = types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            language_code="en-US",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                            ),
                        ),
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        context_window_compression=types.ContextWindowCompressionConfig(
                            sliding_window=types.SlidingWindow()
                        ),
                        input_audio_transcription=types.AudioTranscriptionConfig(),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                        realtime_input_config=types.RealtimeInputConfig(
                            automatic_activity_detection=types.AutomaticActivityDetection(
                                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                                prefix_padding_ms=150,
                                silence_duration_ms=400,
                            ),
                            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                        ),
                        system_instruction=types.Content(
                            parts=[types.Part.from_text(text=await asyncio.to_thread(_build_zero_live_system_prompt))]
                        ),
                        tools=_build_live_tools(),
                    )
                    log.info(f"[ZeroVoice] Connecting to Gemini Live WebSocket ({model_id})...")
                    async with client.aio.live.connect(model=model_id, config=config) as session:
                        self.active_model = model_id
                        connected_ok = True
                        log.info(f"[ZeroVoice] Gemini Live session established on {model_id}!")

                        if not greeted_once:
                            greeted_once = True
                            await session.send_client_content(
                                turns=types.Content(
                                    role="user",
                                    parts=[
                                        types.Part.from_text(
                                            text="You just connected to the #zero-voice Discord channel with Ryan. Give a quick, natural 4-to-6 word greeting in English and wait silently for him to speak."
                                        )
                                    ],
                                ),
                                turn_complete=True,
                            )

                        send_task = asyncio.create_task(self._pump_input_audio(session, audio_in_queue))
                        recv_task = asyncio.create_task(self._pump_output_events(session, notify_channel))

                        done, pending = await asyncio.wait(
                            [send_task, recv_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for p in pending:
                            p.cancel()
                        break
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    log.warning(f"[ZeroVoice] Gemini Live connection error on {model_id}: {e}")
                    await asyncio.sleep(2.0)

            if not connected_ok and not self._stop_event.is_set():
                await asyncio.sleep(5.0)

    async def _pump_input_audio(self, session: Any, audio_in_queue: asyncio.Queue[bytes]) -> None:
        silence_40ms = b"\x00" * 1280  # 40ms at 16kHz 16-bit mono
        trailing_silence_sent_ms = 9999

        while not self._stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(audio_in_queue.get(), timeout=0.04)
                if self._stop_event.is_set():
                    break
                trailing_silence_sent_ms = 0
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
            except asyncio.TimeoutError:
                if self._stop_event.is_set():
                    break
                if self.audio_sink and trailing_silence_sent_ms == 0:
                    rem = self.audio_sink.flush_partial()
                    if rem:
                        await session.send_realtime_input(
                            audio=types.Blob(data=rem, mime_type="audio/pcm;rate=16000")
                        )
                if trailing_silence_sent_ms < 1200:
                    trailing_silence_sent_ms += 40
                    await session.send_realtime_input(
                        audio=types.Blob(data=silence_40ms, mime_type="audio/pcm;rate=16000")
                    )

    async def _pump_output_events(
        self,
        session: Any,
        notify_channel: Optional[discord.abc.Messageable],
    ) -> None:
        user_transcript_buf: list[str] = []
        zero_transcript_buf: list[str] = []

        while not self._stop_event.is_set():
            async for response in session.receive():
                if self._stop_event.is_set():
                    break

                sc = getattr(response, "server_content", None)
                if sc:
                    in_tr = getattr(sc, "input_transcription", None)
                    if in_tr and getattr(in_tr, "text", None):
                        user_transcript_buf.append(in_tr.text)

                    out_tr = getattr(sc, "output_transcription", None)
                    if out_tr and getattr(out_tr, "text", None):
                        zero_transcript_buf.append(out_tr.text)

                    if getattr(sc, "interrupted", False):
                        if self.audio_source:
                            self.audio_source.clear_buffer()

                    model_turn = getattr(sc, "model_turn", None)
                    if model_turn and getattr(model_turn, "parts", None):
                        for part in model_turn.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and getattr(inline, "data", None) and self.audio_source:
                                self.audio_source.feed_gemini_pcm_24k_mono(inline.data)

                    if getattr(sc, "turn_complete", False) or getattr(sc, "interrupted", False):
                        if user_transcript_buf:
                            _append_voice_turn_to_history("Ryan", "".join(user_transcript_buf))
                            user_transcript_buf.clear()
                        if zero_transcript_buf:
                            _append_voice_turn_to_history("Zero", "".join(zero_transcript_buf))
                            zero_transcript_buf.clear()

                tool_call = getattr(response, "tool_call", None)
                if tool_call and getattr(tool_call, "function_calls", None):
                    if user_transcript_buf:
                        _append_voice_turn_to_history("Ryan", "".join(user_transcript_buf))
                        user_transcript_buf.clear()
                    func_responses = []
                    for fc in tool_call.function_calls:
                        res_dict = await self._execute_tool_call(fc, notify_channel)
                        func_responses.append(
                            types.FunctionResponse(
                                id=getattr(fc, "id", None),
                                name=fc.name,
                                response=res_dict,
                            )
                        )
                    if func_responses:
                        await session.send_tool_response(function_responses=func_responses)


_voice_manager: Optional[ZeroVoiceSessionManager] = None


def get_voice_manager(bot: discord.Client) -> ZeroVoiceSessionManager:
    global _voice_manager
    if _voice_manager is None:
        _voice_manager = ZeroVoiceSessionManager(bot)
    return _voice_manager


async def handle_voice_state_update(
    bot: discord.Client,
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    """Auto-join #zero-voice when Ryan hops in; auto-leave when Ryan leaves."""
    if member.id != OWNER_USER_ID:
        return

    mgr = get_voice_manager(bot)

    if after.channel and after.channel.id == ZERO_VOICE_CHANNEL_ID:
        if not mgr.is_active:
            chat_ch = bot.get_channel(TARGET_CHANNEL_ID)
            ok, msg = await mgr.start_session(after.channel, notify_channel=chat_ch)
            if chat_ch:
                try:
                    await chat_ch.send(msg)
                except Exception:
                    pass

    elif before.channel and mgr.is_active and mgr.voice_client and mgr.voice_client.channel.id == before.channel.id:
        human_members = [m for m in before.channel.members if not m.bot]
        if not human_members:
            summary = await mgr.stop_session()
            chat_ch = bot.get_channel(TARGET_CHANNEL_ID)
            if chat_ch:
                try:
                    await chat_ch.send(summary)
                except Exception:
                    pass
