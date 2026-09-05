#!/usr/bin/env python3
"""
benchmark_ttft.py - TTFT & Token Pre-fill Latency Benchmark Harness

Measures Time To First Token (TTFT) and total turn latency across:
1. Turn 1: Post-compaction / fresh clean session baseline.
2. Turn 10 (Unscrubbed): Cumulated 9-turn session with heavy raw tool outputs.
3. Turn 10 (Scrubbed): Identical 9-turn session scrubbed via Option B (transcript_scrubber).
"""

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.transcript_scrubber import scrub_transcript_tool_outputs, sync_transcript_chunks, BRAIN_ROOT
from tools.bridge_state import get_active_model

PRINT_TIMEOUT = "3m"


class TurnTimer:
    def __init__(self):
        self.t_send: float = 0.0
        self.t_first_event: float = 0.0
        self.t_first_token: float = 0.0
        self.t_result: float = 0.0
        self.ttft: float = 0.0
        self.total_duration: float = 0.0
        self.response_text: str = ""
        self.raw_events_count: int = 0


async def run_single_turn(conv_id: str | None, prompt: str) -> tuple[str, TurnTimer]:
    timer = TurnTimer()
    active_model = get_active_model()

    cmd = [
        "agy",
        "--add-dir=/workspace",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--dangerously-skip-permissions",
        f"--print-timeout={PRINT_TIMEOUT}",
        "--print=",
    ]
    if conv_id:
        cmd.append(f"--conversation={conv_id}")
    if active_model:
        cmd.append(f"--model={active_model}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd="/workspace",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _drain_err():
        try:
            while proc and proc.stderr:
                line = await proc.stderr.readline()
                if not line:
                    break
        except Exception:
            pass

    stderr_task = asyncio.create_task(_drain_err())

    try:
        init_line = await asyncio.wait_for(proc.stdout.readline(), timeout=35.0)
        if not init_line:
            raise RuntimeError("agy exited before emitting init event")
        init_data = json.loads(init_line.decode("utf-8").strip())
        actual_cid = init_data.get("conversation_id", conv_id or "")

        payload = {"event": "user", "message": {"content": prompt}}
        timer.t_send = time.perf_counter()
        raw_payload = (json.dumps(payload) + "\n").encode("utf-8")
        proc.stdin.write(raw_payload)
        await proc.stdin.drain()

        while True:
            line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=60.0)
            if not line_bytes:
                break
            now = time.perf_counter()
            line_s = line_bytes.decode("utf-8", errors="replace").strip()
            if not line_s:
                continue

            if line_s.startswith("{") and line_s.endswith("}"):
                try:
                    ev = json.loads(line_s)
                    timer.raw_events_count += 1
                    ev_name = ev.get("event")

                    if ev_name == "step_update":
                        if timer.t_first_event == 0.0:
                            timer.t_first_event = now
                            timer.ttft = timer.t_first_event - timer.t_send

                        step = ev.get("step_update", {})
                        if step.get("step_type") == "agent_response" and step.get("text_delta"):
                            if timer.t_first_token == 0.0:
                                timer.t_first_token = now

                    elif ev_name == "result":
                        if timer.t_first_event == 0.0:
                            timer.t_first_event = now
                            timer.ttft = timer.t_first_event - timer.t_send
                        timer.t_result = now
                        timer.total_duration = timer.t_result - timer.t_send
                        timer.response_text = ev.get("result", {}).get("response", "")
                        break
                except Exception:
                    pass

        return actual_cid, timer

    finally:
        if proc:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()


def get_transcript_bytes(conv_id: str) -> int:
    p = BRAIN_ROOT / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if p.exists():
        return p.stat().st_size
    return 0


def inject_synthetic_turns(source_cid: str, dest_cid: str, num_turns: int = 8, tool_output_size: int = 15000):
    src_dir = BRAIN_ROOT / source_cid
    dst_dir = BRAIN_ROOT / dest_cid
    if dst_dir.exists():
        shutil.rmtree(dst_dir)

    shutil.copytree(src_dir, dst_dir)

    logs_dir = dst_dir / ".system_generated" / "logs"
    transcript_file = logs_dir / "transcript.jsonl"
    transcript_full_file = logs_dir / "transcript_full.jsonl"

    existing_steps = []
    if transcript_file.exists():
        with open(transcript_file, "r", encoding="utf-8") as f:
            for l in f:
                if l.strip():
                    try:
                        existing_steps.append(json.loads(l))
                    except Exception:
                        pass

    start_step = len(existing_steps)
    sample_git_diff = (
        "diff --git a/tools/bridge_runner.py b/tools/bridge_runner.py\n"
        "--- a/tools/bridge_runner.py\n"
        "+++ b/tools/bridge_runner.py\n"
        "@@ -100,10 +100,20 @@ def execute_agy_turn():\n"
        "+    # Process tree termination and stream buffer draining\n"
        "+    kill_process_tree(proc, signal.SIGTERM)\n"
    )
    multiplier = max(1, tool_output_size // len(sample_git_diff))
    large_payload = (sample_git_diff * multiplier)[:tool_output_size]

    new_steps = []
    for i in range(num_turns):
        idx = start_step + (i * 4)
        new_steps.append({
            "step_index": idx,
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "content": f"<USER_REQUEST>\nTurn {i + 2}: Audit git repository diff and run unit tests.\n</USER_REQUEST>"
        })
        new_steps.append({
            "step_index": idx + 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "thinking": f"Analyzing repository state for turn {i + 2}...",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": f"git diff --stat HEAD~{i + 1}"}}]
        })
        new_steps.append({
            "step_index": idx + 2,
            "type": "GENERIC",
            "source": "MODEL",
            "content": f"The command exited with code 0.\nOutput:\n{large_payload}"
        })
        new_steps.append({
            "step_index": idx + 3,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "content": f"Turn {i + 2} audit complete. All 12 files verified clean."
        })

    all_steps = existing_steps + new_steps
    raw_content = "\n".join(json.dumps(s) for s in all_steps) + "\n"
    transcript_file.write_text(raw_content, encoding="utf-8")
    transcript_full_file.write_text(raw_content, encoding="utf-8")

    sync_transcript_chunks(transcript_file)


async def run_benchmark(keep_sessions: bool = False):
    tag = uuid.uuid4().hex[:6]
    fresh_cid = f"bench-fresh-{tag}"
    unscrubbed_cid = f"bench-unscrubbed-{tag}"
    scrubbed_cid = f"bench-scrubbed-{tag}"

    created_sessions = []
    test_prompt = "Reply with the single word 'pong' and nothing else."

    print("==================================================================")
    print("🚀 ANTIGRAVITY TTFT & TOKEN PRE-FILL BENCHMARK HARNESS")
    print("==================================================================")
    print(f"Model: {get_active_model() or 'default'}")
    print(f"Test Prompt: \"{test_prompt}\"")
    print("Target: Turn 1 (Fresh) vs Turn 10 (Raw Unscrubbed) vs Turn 10 (Option B Scrubbed)\n")

    try:
        print("▶️ [Phase 1/3] Benchmarking Turn 1 (Baseline Clean Post-Compaction)...")
        actual_fresh_cid, t1 = await run_single_turn(fresh_cid, test_prompt)
        created_sessions.append(actual_fresh_cid)
        t1_bytes = get_transcript_bytes(actual_fresh_cid)
        print(f"   ✓ Turn 1 complete: TTFT = {t1.ttft:.3f}s | Total = {t1.total_duration:.3f}s | Transcript = {t1_bytes:,} bytes\n")

        print("▶️ [Phase 2/3] Generating 8 intermediate heavy tool turns (git diffs & command dumps)...")
        inject_synthetic_turns(actual_fresh_cid, unscrubbed_cid, num_turns=8, tool_output_size=12000)
        inject_synthetic_turns(actual_fresh_cid, scrubbed_cid, num_turns=8, tool_output_size=12000)
        created_sessions.extend([unscrubbed_cid, scrubbed_cid])

        unscrubbed_pre_bytes = get_transcript_bytes(unscrubbed_cid)
        print(f"   ✓ Unscrubbed 9-turn session prepared: {unscrubbed_pre_bytes:,} bytes (~{unscrubbed_pre_bytes // 4:,} tokens)")

        bytes_saved = scrub_transcript_tool_outputs(scrubbed_cid, max_output_len=200)
        scrubbed_pre_bytes = get_transcript_bytes(scrubbed_cid)
        reduction_pct = ((unscrubbed_pre_bytes - scrubbed_pre_bytes) / unscrubbed_pre_bytes) * 100
        print(f"   ✓ Option B applied to scrubbed session: {scrubbed_pre_bytes:,} bytes (saved {bytes_saved:,} bytes, -{reduction_pct:.1f}%)\n")

        print("▶️ [Phase 3a/3] Benchmarking Turn 10 (Raw Unscrubbed)...")
        _, t10_raw = await run_single_turn(unscrubbed_cid, test_prompt)
        print(f"   ✓ Turn 10 Unscrubbed: TTFT = {t10_raw.ttft:.3f}s | Total = {t10_raw.total_duration:.3f}s")

        print("▶️ [Phase 3b/3] Benchmarking Turn 10 (Option B Scrubbed)...")
        _, t10_scrub = await run_single_turn(scrubbed_cid, test_prompt)
        print(f"   ✓ Turn 10 Scrubbed:   TTFT = {t10_scrub.ttft:.3f}s | Total = {t10_scrub.total_duration:.3f}s\n")

        slowdown_sec = t10_raw.ttft - t1.ttft
        slowdown_pct = ((t10_raw.ttft - t1.ttft) / t1.ttft) * 100 if t1.ttft > 0 else 0
        recovery_sec = t10_raw.ttft - t10_scrub.ttft
        recovery_pct = ((t10_raw.ttft - t10_scrub.ttft) / t10_raw.ttft) * 100 if t10_raw.ttft > 0 else 0

        print("==================================================================")
        print("📊 BENCHMARK RESULTS SUMMARY")
        print("==================================================================")
        print("• Turn 1 (Baseline Clean):")
        print(f"  - Context Size:    {t1_bytes:,} bytes (~{t1_bytes // 4:,} tokens)")
        print(f"  - TTFT:            {t1.ttft:.3f}s")
        print(f"  - Total Latency:   {t1.total_duration:.3f}s")
        print()
        print("• Turn 10 (Raw Unscrubbed):")
        print(f"  - Context Size:    {unscrubbed_pre_bytes:,} bytes (~{unscrubbed_pre_bytes // 4:,} tokens)")
        print(f"  - TTFT:            {t10_raw.ttft:.3f}s (+{slowdown_sec:.3f}s / +{slowdown_pct:.1f}% pre-fill penalty)")
        print(f"  - Total Latency:   {t10_raw.total_duration:.3f}s")
        print()
        print("• Turn 10 (Option B Scrubbed):")
        print(f"  - Context Size:    {scrubbed_pre_bytes:,} bytes (~{scrubbed_pre_bytes // 4:,} tokens) [-{reduction_pct:.1f}%]")
        print(f"  - TTFT:            {t10_scrub.ttft:.3f}s (saved {recovery_sec:.3f}s / {recovery_pct:.1f}% faster TTFT)")
        print(f"  - Total Latency:   {t10_scrub.total_duration:.3f}s")
        print("==================================================================")

    finally:
        if not keep_sessions:
            for cid in created_sessions:
                sdir = BRAIN_ROOT / cid
                if sdir.exists():
                    try:
                        shutil.rmtree(sdir)
                    except Exception:
                        pass


if __name__ == "__main__":
    keep = "--keep" in sys.argv
    asyncio.run(run_benchmark(keep_sessions=keep))
