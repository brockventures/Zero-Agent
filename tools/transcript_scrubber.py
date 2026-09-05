#!/usr/bin/env python3
"""
Transcript Scrubber for Zero (Option B - Tool Output Scrubbing).
Pre-turn hook that strips raw tool outputs and internal thinking chains
from older turns in transcript.jsonl while keeping full forensic logs in transcript_full.jsonl.
"""

import json
import os
import re
import sys
from pathlib import Path

BRAIN_ROOT = Path("/root/.gemini/antigravity-cli/brain")
CHUNK_SIZE = 102400  # 100 KB chunks standard in agy runtime


def sync_transcript_chunks(transcript_path: Path) -> None:
    """Synchronize chunks/transcript/ with the updated transcript.jsonl byte boundaries."""
    chunk_dir = transcript_path.parent / "chunks" / "transcript"
    if not chunk_dir.exists():
        return

    try:
        raw_bytes = transcript_path.read_bytes()
        for old_chunk in chunk_dir.glob("*.jsonl"):
            try:
                old_chunk.unlink()
            except OSError:
                pass

        for i in range(0, len(raw_bytes), CHUNK_SIZE):
            chunk_file = chunk_dir / f"{i // CHUNK_SIZE:08d}.jsonl"
            chunk_file.write_bytes(raw_bytes[i:i + CHUNK_SIZE])
    except Exception as e:
        print(f"[TranscriptScrubber] Warning synchronizing chunks for {transcript_path}: {e}")


def scrub_transcript_tool_outputs(
    conv_id: str | None,
    max_output_len: int = 200,
    grace_turns: int | None = None,
    brain_root: Path = BRAIN_ROOT,
) -> int:
    """Scrub bulky tool outputs (GENERIC) and thinking chains in transcript.jsonl.

    Preserves:
    - User input text and metadata
    - Planner response agent messages, interactive choices, and tool call definitions
    - Brief summary / exit code of all tool calls
    - The most recent `grace_turns` turns' tool outputs completely unscrubbed (1-turn grace window)
    - transcript_full.jsonl (remains 100% untruncated for forensic audits)

    Returns:
    - Number of bytes saved (0 if nothing changed or session not found).
    """
    if not conv_id:
        return 0

    if grace_turns is None:
        try:
            from tools.bridge_state import get_runtime_rules
            grace_turns = int(get_runtime_rules().get("scrubber_grace_turns", 1))
        except Exception:
            grace_turns = 1

    logs_dir = brain_root / conv_id / ".system_generated" / "logs"
    transcript_path = logs_dir / "transcript.jsonl"

    if not transcript_path.exists():
        return 0

    try:
        orig_size = transcript_path.stat().st_size
        if orig_size == 0:
            return 0

        steps = []
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    steps.append(json.loads(line_str))
                except Exception:
                    steps.append(line_str)

        user_turn_indices = [
            i for i, step in enumerate(steps)
            if isinstance(step, dict) and step.get("type") == "USER_INPUT"
        ]

        # Calculate cutoff index: steps at or after cutoff are within the grace window (preserved)
        if grace_turns > 0 and len(user_turn_indices) >= grace_turns:
            cutoff_step_idx = user_turn_indices[-grace_turns]
        elif grace_turns > 0 and len(user_turn_indices) > 0:
            cutoff_step_idx = 0
        else:
            cutoff_step_idx = len(steps) + 1

        modified = False
        scrubbed_steps = []

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                scrubbed_steps.append(step if isinstance(step, str) else json.dumps(step))
                continue

            # Within grace window -> keep 100% verbatim
            if idx >= cutoff_step_idx:
                scrubbed_steps.append(json.dumps(step))
                continue

            stype = step.get("type")

            # 1. Scrub bulky tool outputs
            if stype == "GENERIC":
                content = step.get("content", "")
                if len(content) > max_output_len:
                    if "The command exited with code" in content:
                        m = re.search(r"The command exited with code \d+\.", content)
                        summary = m.group(0) if m else content[:80].split("\n")[0]
                    elif "File Path:" in content:
                        summary = content[:120].split("\n")[0]
                    elif "Found " in content and " results" in content:
                        summary = content[:80].split("\n")[0]
                    else:
                        summary = content[:80].split("\n")[0].strip()

                    step["content"] = f"{summary}\n[Tool output scrubbed for context optimization: {len(content)} bytes]"
                    modified = True

            # 2. Scrub bloated chain-of-thought thinking
            thinking = step.get("thinking")
            if thinking and len(thinking) > 100:
                step["thinking"] = "[Thinking scrubbed]"
                modified = True

            scrubbed_steps.append(json.dumps(step))

        if not modified:
            return 0

        new_content = "\n".join(scrubbed_steps) + "\n"
        tmp_path = transcript_path.with_suffix(".tmp")
        tmp_path.write_text(new_content, encoding="utf-8")
        tmp_path.replace(transcript_path)

        sync_transcript_chunks(transcript_path)

        new_size = transcript_path.stat().st_size
        bytes_saved = max(0, orig_size - new_size)
        print(f"[TranscriptScrubber] 🧹 Scrubbed session {conv_id[:8]}: {orig_size}B -> {new_size}B (saved {bytes_saved}B, -{(bytes_saved/orig_size)*100:.1f}%, grace_turns={grace_turns})")
        return bytes_saved

    except Exception as e:
        print(f"[TranscriptScrubber] Error scrubbing transcript for {conv_id}: {e}")
        return 0


if __name__ == "__main__":
    cid = sys.argv[1] if len(sys.argv) > 1 else None
    if not cid:
        print("Usage: transcript_scrubber.py <conversation_id>")
        sys.exit(1)
    saved = scrub_transcript_tool_outputs(cid)
    print(f"Bytes saved: {saved}")
