#!/usr/bin/env python3
"""
gotchi_zero_adapter.py — Unified Zero Brain & Transcript Adapter for GOTCHI Hardware
====================================================================================
Unifies the GOTCHI hardware companion (`brockventures/gotchi`) and `#zero-voice`
(`tools/bridge_voice.py`) into a single, seamless Zero experience:

1. Shared Persona, Memory & Tool Parity with `#zero-voice`:
   - Reuses `_load_startup_context_snapshot()`, `_build_live_tools()`, and tool execution
     from `tools/bridge_voice.py`, plus fast (<15ms) Turn-0 hybrid memory pre-fetching
     via `tools/transcript_index.py`.
   - Supports single-shot camera snapshots (`image_bytes` / `image_b64`) and 5-frame bursts
     (`burst_images` / `burst_images_b64`) natively via multimodal `types.Part.from_bytes`.
   - Optimizes spoken output for GOTCHI's `MAX98357A` speaker and 200x200 e-paper display:
     crisp 1-to-3 sentence spoken answers in Zero's `Schedar` voice, stripped of raw markdown
     or URL clutter (`clean_spoken_text_for_tts`), while automatically routing any links or
     code snippets to Discord.

2. Shared Transcript Continuity & Standalone `#gotchi-chat` Channel Logging:
   - Records every Gotchi interaction in `/workspace/data/voice_transcripts.json` and
     `/workspace/data/channel_history.json` (`#zero-chat` + `#gotchi-chat`, ID `1562246590943461429`)
     so text chat, `#zero-voice`, and Gotchi share one continuous conversation history.
   - Mirrors every completed turn (including camera snapshots via `deliver_image.py` or
     text turns via `outbox.py`) to `#gotchi-chat` (`1562246590943461429`).

3. Three Execution Modes:
   - Direct `turn_handler` & `turn_log_router` mounted inside `gotchi.backend.voice_gateway.create_app()`.
   - `POST /api/gotchi/brain` HTTP endpoint for `LLMConfig.subscription_url`.
   - `--stdin` JSON CLI runner for `LLMConfig.subscription_command`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    from fastapi import Request
    from fastapi.responses import JSONResponse
except ImportError:
    Request = Any  # type: ignore
    JSONResponse = Any  # type: ignore

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.insert(0, "/workspace/gotchi")

from tools.bridge_state import TARGET_CHANNEL_ID
from tools.bridge_voice import (
    DEFAULT_VOICE_NAME,
    GOTCHI_CHAT_CHANNEL_ID,
    _append_voice_turn_to_history,
    _build_live_tools,
    _load_startup_context_snapshot,
)
from tools import outbox

log = logging.getLogger("gotchi_zero_adapter")
PT_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_GOTCHI_MODEL = os.environ.get("GOTCHI_ZERO_MODEL", "gemini-2.5-flash")
ATTACHMENTS_DIR = Path("/workspace/data/attachments")


def clean_spoken_text_for_tts(text: str) -> str:
    """
    Cleans LLM markdown output for natural spoken TTS synthesis on Gotchi (`Schedar` voice)
    while preserving sentence punctuation for `split_into_sentences`.
    """
    if not text:
        return ""
    cleaned = text.strip()
    # Replace markdown links [label](<url>) or [label](url) with just the spoken label
    cleaned = re.sub(r"\[([^\]]+)\]\(<?https?://[^)\s>]+>?\)", r"\1", cleaned)
    # Remove bare URLs in angle brackets or plain text so TTS doesn't read 'https colon slash slash'
    cleaned = re.sub(r"<https?://[^>]+>", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Strip markdown headers, bold/italic markers, and inline code backticks
    cleaned = re.sub(r"(?m)^#{1,6}\s*", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    # Collapse bullets into natural sentence pauses
    cleaned = re.sub(r"(?m)^\s*[-*•]\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def prefetch_zero_memory(query: str, limit: int = 4) -> str:
    """Runs fast hybrid BM25 + vector memory recall for the spoken prompt."""
    clean_q = (query or "").strip()
    if not clean_q or len(clean_q) < 4:
        return ""
    try:
        res = subprocess.run(
            [
                "python3",
                "/workspace/tools/transcript_index.py",
                "search",
                clean_q,
                "--target",
                "memory",
                "--limit",
                str(limit),
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()[:2500]
    except Exception as exc:
        log.debug(f"[GotchiZero] Memory prefetch skipped: {exc}")
    return ""


def build_gotchi_zero_system_prompt(user_prompt: str = "", has_vision: bool = False) -> str:
    """
    Builds the unified Zero system prompt shared with `#zero-voice`, tuned for Gotchi's
    pocket Push-to-Talk, camera snapshot/burst, and 200x200 e-paper companion UX.
    """
    now_pt = datetime.now(PT_TZ).strftime("%A, %b %d, %Y %I:%M %p PT")
    snapshot = _load_startup_context_snapshot()
    memory_hits = prefetch_zero_memory(user_prompt) if user_prompt else ""
    mem_block = (
        f"\n\n=== PRE-FETCHED ZERO MEMORY FOR THIS TURN ===\n{memory_hits}"
        if memory_hits
        else ""
    )
    vision_note = (
        "\n- Ryan attached a live camera snapshot or 5-frame burst from Gotchi's onboard camera. "
        "Inspect the visual contents directly and answer with concrete, specific observations."
        if has_vision
        else ""
    )
    return (
        f"You are Zero, Ryan Brock's autonomous AI engineering partner running on Host 2 (.84 LAN) in the "
        f"Brock homelab (Redwood City, CA). Current time: {now_pt}.\n"
        f"Ryan is talking to you through his pocket GOTCHI hardware companion ( matching your `#zero-voice` "
        f"experience with your signature `{DEFAULT_VOICE_NAME}` voice and 200x200 e-paper display).\n\n"
        "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS:\n"
        "- Speak STRICTLY in English (`en-US`).\n"
        "- Razor-sharp, supremely capable staff engineer / collaborative partner (think Tony Stark in "
        "problem-solving mode or Ben Wyatt / Jerry Seinfeld wryly noting technical absurdity).\n"
        "- Calm mastery, dry wit, warm camaraderie, zero subservience. Never say 'I hope this helps!' or "
        "'Let me know if you need anything else!'\n"
        "- SPOKEN BREVITY FOR POCKET HARDWARE: Keep your spoken answer tight, punchy, and natural "
        "(1 to 3 sentences by default) so it plays cleanly through Gotchi's speaker without monologue fatigue.\n"
        "- NO RAW MARKDOWN OR URLS IN SPOKEN TEXT: Do not read URLs, file paths, or markdown tables out loud. "
        "If Ryan asks for links, code snippets, or a detailed list, drop them into `#gotchi-chat` or `#zero-chat` "
        "using `post_note_to_zero_chat` and tell him verbally that you dropped the details in Discord."
        f"{vision_note}\n\n"
        "FULL AGENT PARITY & TOOL ACCESS:\n"
        "- You have 100% of the same container access, filesystem access, memory search, GitHub CLI (`gh`), "
        "SSH, Home Assistant, and research tools as `#zero-voice` and `#zero-chat`.\n"
        "- Use `search_zero_memory` to search durable memory or past transcripts.\n"
        "- Use `check_tasks_and_repos` to inspect `/workspace/data/tasks.json` and open GitHub issues.\n"
        "- Use `run_shell_command` to run non-interactive bash commands in `/workspace`.\n"
        "- Use `read_local_file` and `write_local_file` to inspect or update files on disk.\n"
        "- Use `web_and_reddit_search` for live Reddit or Amazon SerpApi queries.\n"
        "- Use `get_recent_discord_context` to read recent messages across Discord channels.\n"
        "- Use `post_note_to_zero_chat` to post detailed links, notes, or code snippets to Discord.\n\n"
        f"{snapshot}{mem_block}"
    )


async def execute_gotchi_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Executes the shared `#zero-voice` tool suite for Gotchi turns."""
    log.info(f"[GotchiZero] Executing tool call: {name}({args})")

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
                [
                    "python3",
                    "/workspace/tools/transcript_index.py",
                    "search",
                    query,
                    "--target",
                    target,
                    "--limit",
                    "6",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return {"result": (res.stdout or "No matches found.")[:3500]}
        except Exception as e:
            return {"error": str(e)}

    if name == "check_tasks_and_repos":
        repo_filter = str(args.get("repo_or_filter", "all")).strip().lower()
        out_parts: List[str] = []
        try:
            t_file = Path("/workspace/data/tasks.json")
            if t_file.exists():
                tasks = json.loads(t_file.read_text())
                pending = [t for t in tasks if t.get("status") != "completed"]
                out_parts.append(
                    "Local Pending Tasks (/workspace/data/tasks.json):\n"
                    + "\n".join(
                        f"- #{t.get('id')} [{t.get('status')}]: {t.get('title')}"
                        for t in pending[:15]
                    )
                )
            repos = ["brockventures/gotchi", "brockventures/big-projections"]
            if "sharkfin" in repo_filter or repo_filter == "all":
                repos.append("brockventures/sharkfin")
            if "home" in repo_filter or "ha" in repo_filter or repo_filter == "all":
                repos.append("brockventures/homeassistant-config")

            for r in repos:
                gh_res = await asyncio.to_thread(
                    subprocess.run,
                    ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "10"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if gh_res.stdout.strip():
                    out_parts.append(f"Open GitHub Issues in {r}:\n{gh_res.stdout.strip()}")
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
            "gotchi-chat": str(GOTCHI_CHAT_CHANNEL_ID),
            "baseball": "1548196929308065893",
            "zero-ops": "1544953279664889888",
            "homelab": "1544955535722545253",
            "finances": "1544955532765560924",
            "shopping": "1544955538033348618",
            "projects": "1548196930788524094",
        }
        cid = ch_map.get(ch_name, str(TARGET_CHANNEL_ID))
        try:
            ch_file = Path("/workspace/data/channel_history.json")
            if ch_file.exists():
                msgs = json.loads(ch_file.read_text()).get(cid, [])[-12:]
                formatted = "\n".join(
                    f"[{m.get('timestamp_pt') or m.get('timestamp')}] {m.get('author')}: {str(m.get('content'))[:300]}"
                    for m in msgs
                )
                return {"result": formatted or "No recent messages."}
        except Exception as e:
            return {"error": str(e)}

    if name == "post_note_to_zero_chat":
        msg_text = str(args.get("message", "")).strip()
        if msg_text:
            outbox.queue_outbox_message("gotchi-chat", f"📟 **[Gotchi Session Note]:**\n{msg_text}")
            outbox.queue_outbox_message("zero-chat", f"📟 **[Gotchi Session Note]:**\n{msg_text}")
            return {"status": "posted to #gotchi-chat and #zero-chat"}
        return {"error": "Empty message"}

    return {"error": f"Unknown function {name}"}


def record_gotchi_turn_to_channels(
    node_id: str,
    session_id: str,
    transcript: str,
    reply: str,
    mode: str = "interact",
    voice_name: str = DEFAULT_VOICE_NAME,
    image_bytes: Optional[bytes] = None,
    burst_images: Optional[List[bytes]] = None,
    stt_ms: int = 0,
    first_audio_ms: Optional[int] = None,
    total_ms: Optional[int] = None,
    dispatch_discord: bool = True,
) -> Dict[str, Any]:
    """
    Persists a completed Gotchi turn into:
    1. `/workspace/data/voice_transcripts.json` (shared with `#zero-voice`).
    2. `/workspace/data/channel_history.json` for BOTH `#zero-chat` (`1542081375287640084`)
       and `#gotchi-chat` (`1562246590943461429`) so Zero has instant continuity everywhere.
    3. Standalone `#gotchi-chat` Discord channel (`1562246590943461429`) via `deliver_image.py`
       (when camera snapshots are present) or `outbox.enqueue_message`.
    """
    source_tag = f"📟 {node_id}"
    if transcript:
        _append_voice_turn_to_history(
            "Ryan",
            transcript,
            source_tag=source_tag,
            extra_channel_ids=[GOTCHI_CHAT_CHANNEL_ID],
        )
    if reply:
        _append_voice_turn_to_history(
            "Zero",
            reply,
            source_tag=source_tag,
            extra_channel_ids=[GOTCHI_CHAT_CHANNEL_ID],
        )

    burst_list = list(burst_images or [])
    primary_img = image_bytes or (burst_list[0] if burst_list else None)
    saved_image_path: Optional[str] = None

    if primary_img:
        try:
            ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            img_path = ATTACHMENTS_DIR / f"gotchi_{node_id}_{ts}.jpg"
            img_path.write_bytes(primary_img)
            saved_image_path = str(img_path)
        except Exception as e:
            log.warning(f"[GotchiZero] Failed saving snapshot attachment: {e}")

    img_badge = ""
    if len(burst_list) > 1:
        img_badge = f" 📸 *({len(burst_list)}-frame burst)*"
    elif primary_img:
        img_badge = " 📸 *(camera snapshot)*"

    timing_footer = ""
    if total_ms is not None and total_ms > 0:
        fa_str = f" · first_audio: {first_audio_ms}ms" if first_audio_ms is not None else ""
        timing_footer = f"\n-# session: {session_id} · stt: {stt_ms}ms{fa_str} · total: {total_ms}ms"

    if mode == "journal":
        discord_msg = f"📓 **[{node_id} · All-Day Journal]{img_badge}:**\n> {transcript}{timing_footer}"
    else:
        discord_msg = (
            f"🎙️ **Ryan ({node_id}){img_badge}:** {transcript}\n"
            f"🔊 **Zero ({voice_name}):** {reply}{timing_footer}"
        )

    dispatched = False
    if dispatch_discord:
        if saved_image_path:
            try:
                from tools.deliver_image import deliver_image

                deliver_image(
                    saved_image_path,
                    channel_input=str(GOTCHI_CHAT_CHANNEL_ID),
                    caption=discord_msg,
                )
                dispatched = True
            except Exception as e:
                log.warning(f"[GotchiZero] Image delivery fallback to outbox: {e}")
        if not dispatched:
            outbox.queue_outbox_message(
                str(GOTCHI_CHAT_CHANNEL_ID),
                discord_msg,
                dedupe_window=5,
            )
            dispatched = True

    return {
        "ok": True,
        "node_id": node_id,
        "session_id": session_id,
        "channel_id": GOTCHI_CHAT_CHANNEL_ID,
        "saved_image_path": saved_image_path,
        "discord_message": discord_msg,
        "dispatched": dispatched,
    }


class GotchiZeroBrainAdapter:
    """
    Zero Brain Adapter for Gotchi (`POST /api/voice/interact` and `/api/gotchi/brain`).
    Shares system prompt, memory pre-fetching, tool declarations, and conversation history
    with `#zero-voice` (`tools/bridge_voice.py`).
    """

    def __init__(
        self,
        model: str = DEFAULT_GOTCHI_MODEL,
        api_key: Optional[str] = None,
        stt_adapter: Optional[Any] = None,
        llm_caller: Optional[Callable[[Dict[str, Any]], Any]] = None,
        dispatch_discord: bool = True,
    ):
        self.model = model
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.stt_adapter = stt_adapter
        self.llm_caller = llm_caller
        self.dispatch_discord = dispatch_discord

    async def _run_gemini_with_tools(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        burst_images: List[bytes],
    ) -> str:
        from google import genai
        from google.genai import types

        if not self.api_key:
            from dotenv import load_dotenv

            load_dotenv("/workspace/.env")
            self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()

        if not self.api_key:
            return "Zero is online, but GEMINI_API_KEY is not configured in the environment."

        client = genai.Client(api_key=self.api_key)
        has_vision = bool(image_bytes or burst_images)
        sys_prompt = await asyncio.to_thread(
            build_gotchi_zero_system_prompt, prompt, has_vision
        )

        user_parts: List[types.Part] = []
        if image_bytes:
            user_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        for b_img in burst_images[:5]:
            user_parts.append(types.Part.from_bytes(data=b_img, mime_type="image/jpeg"))
        user_parts.append(types.Part.from_text(text=prompt or "Describe what you see."))

        contents: List[types.Content] = [types.Content(role="user", parts=user_parts)]
        config = types.GenerateContentConfig(
            system_instruction=sys_prompt,
            tools=_build_live_tools(),
            temperature=0.4,
        )

        for _step in range(4):
            resp = await client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            fcalls = getattr(resp, "function_calls", None)
            if not fcalls:
                return (getattr(resp, "text", "") or "").strip()

            model_content = resp.candidates[0].content if resp.candidates else None
            if model_content is not None:
                contents.append(model_content)

            fn_parts: List[types.Part] = []
            for fc in fcalls:
                res_dict = await execute_gotchi_tool_call(
                    getattr(fc, "name", ""),
                    dict(getattr(fc, "args", {}) or {}),
                )
                fn_parts.append(
                    types.Part.from_function_response(
                        name=getattr(fc, "name", ""),
                        response=res_dict,
                    )
                )
            contents.append(types.Content(role="user", parts=fn_parts))

        return "Handled. I posted the details to #gotchi-chat."

    async def handle_turn(self, parsed_turn: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compatible directly with `VoiceGatewayService(turn_handler=adapter.handle_turn)`
        and `SubscriptionBrainAdapter` JSON payloads.
        """
        t0 = time.monotonic()
        node_id = str(parsed_turn.get("node_id") or "gotchi-ryan-01")
        session_id = str(parsed_turn.get("session_id") or f"sess-{int(time.time()*1000)}")

        image_bytes: Optional[bytes] = parsed_turn.get("image_bytes")
        if image_bytes is None and parsed_turn.get("image_b64"):
            image_bytes = base64.b64decode(parsed_turn["image_b64"])

        burst_images: List[bytes] = list(parsed_turn.get("burst_images") or [])
        if not burst_images and parsed_turn.get("burst_images_b64"):
            burst_images = [
                base64.b64decode(b) for b in parsed_turn["burst_images_b64"] if b
            ]

        if parsed_turn.get("prompt"):
            transcript = str(parsed_turn["prompt"]).strip()
        elif parsed_turn.get("audio_bytes") and self.stt_adapter is not None:
            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                None, self.stt_adapter.transcribe, parsed_turn["audio_bytes"]
            )
        elif burst_images:
            transcript = "Analyze this 5-frame camera burst from Gotchi."
        elif image_bytes:
            transcript = "Analyze this camera snapshot from Gotchi."
        else:
            transcript = "Status check."

        stt_ms = int((time.monotonic() - t0) * 1000)

        if self.llm_caller is not None:
            raw_res = self.llm_caller(
                {
                    "node_id": node_id,
                    "session_id": session_id,
                    "prompt": transcript,
                    "image_bytes": image_bytes,
                    "burst_images": burst_images,
                }
            )
            if asyncio.iscoroutine(raw_res):
                raw_res = await raw_res
            raw_reply = str(raw_res.get("reply") if isinstance(raw_res, dict) else raw_res)
        else:
            raw_reply = await self._run_gemini_with_tools(
                prompt=transcript,
                image_bytes=image_bytes,
                burst_images=burst_images,
            )

        spoken_reply = clean_spoken_text_for_tts(raw_reply) or "Got it."
        total_ms = int((time.monotonic() - t0) * 1000)

        from backend.voice_gateway import split_into_sentences

        sentences = split_into_sentences(spoken_reply)

        record_gotchi_turn_to_channels(
            node_id=node_id,
            session_id=session_id,
            transcript=transcript,
            reply=raw_reply or spoken_reply,
            mode=str(parsed_turn.get("mode") or "interact"),
            voice_name=DEFAULT_VOICE_NAME,
            image_bytes=image_bytes,
            burst_images=burst_images,
            stt_ms=stt_ms,
            total_ms=total_ms,
            dispatch_discord=self.dispatch_discord,
        )

        return {
            "transcript": transcript,
            "reply": spoken_reply,
            "raw_reply": raw_reply,
            "sentences": sentences,
            "stt_ms": stt_ms,
        }


def create_zero_gotchi_app(
    adapter: Optional[GotchiZeroBrainAdapter] = None,
    live_mode_enabled: bool = True,
):
    """
    Creates the full FastAPI Gotchi Gateway (`POST /api/voice/interact`, `WebSocket /api/live/ws`,
    `POST /api/journal/ingest`, `GET /api/voice/logs`, plus `POST /api/gotchi/brain`) wired
    directly to Zero's brain and `#gotchi-chat` transcript recorder.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from backend.live_gateway import LiveGatewayService
    from backend.llm_config import LLMConfig
    from backend.voice_gateway import VoiceGatewayService, create_app

    brain = adapter or GotchiZeroBrainAdapter()
    cfg = LLMConfig.from_env_or_file()
    cfg.voice_name = DEFAULT_VOICE_NAME

    def _on_gateway_turn_logged(entry: Dict[str, Any]) -> None:
        # Ensure journal turns or non-custom-handler turns also mirror to #gotchi-chat
        if entry.get("mode") == "journal":
            record_gotchi_turn_to_channels(
                node_id=entry.get("node_id", "gotchi-ryan-01"),
                session_id=entry.get("session_id", "sess-0"),
                transcript=entry.get("transcript", ""),
                reply="",
                mode="journal",
                voice_name=entry.get("voice_name", DEFAULT_VOICE_NAME),
                dispatch_discord=brain.dispatch_discord,
            )

    voice_srv = VoiceGatewayService(
        llm_config=cfg,
        turn_handler=brain.handle_turn,
        turn_log_router=_on_gateway_turn_logged,
    )
    brain.stt_adapter = voice_srv.stt_adapter
    live_srv = LiveGatewayService(
        verifier=voice_srv.verifier,
        auth_required=voice_srv.auth_required,
        live_mode_enabled=live_mode_enabled,
        llm_config=cfg,
    )
    app = create_app(service=voice_srv, live_service=live_srv)

    @app.post("/api/gotchi/brain")
    async def gotchi_brain_endpoint(request: Request):
        body = await request.json()
        res = await brain.handle_turn(body)
        return JSONResponse(status_code=200, content=res)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Gotchi <-> Zero Unified Brain & Transcript Adapter")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read SubscriptionBrainAdapter JSON payload from stdin and write JSON reply to stdout",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the unified Gotchi + Zero FastAPI gateway server",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for --serve (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port for --serve (default: 8765)")
    args = parser.parse_args()

    if args.stdin:
        raw_in = sys.stdin.read().strip()
        payload = json.loads(raw_in) if raw_in else {}
        adapter = GotchiZeroBrainAdapter()
        result = asyncio.run(adapter.handle_turn(payload))
        sys.stdout.write(json.dumps(result) + "\n")
        return

    if args.serve:
        import uvicorn

        app = create_zero_gotchi_app()
        uvicorn.run(app, host=args.host, port=args.port)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
