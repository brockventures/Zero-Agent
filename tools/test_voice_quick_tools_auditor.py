#!/usr/bin/env python3
"""
test_voice_quick_tools_auditor.py — Unit tests for Weekly Voice Quick-Tools Promotion Auditor
=============================================================================================
Covers:
1. Skill and tool discovery across `.agents/skills/*/SKILL.md`,
   `/root/.gemini/antigravity-cli/skills/*/SKILL.md`, and `/workspace/tools/*.py`.
2. Candidate filtering against existing active inline voice tools in
   `tools.gotchi_zero_adapter._build_gotchi_tools()` and voice log triggers
   (`load_skill_playbook` and `handoff_to_zero_runtime`).
3. Atomic FORGE Decision Card staging (`Approve | Edit | Dismiss` with explicit
   `**Approve Action:**` summary) when candidates exist, and silent (`""`) exit
   when 0 candidates exist.
4. Idempotent schedule registration (`ensure_weekly_schedule_registered`) for
   `voice_quick_tools_weekly_audit` on Sundays at 18:40 PT, `channel_id` 1542081375287640084.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, REPO_ROOT)

if "tools" in sys.modules:
    tools_mod = sys.modules["tools"]
    tools_path = getattr(tools_mod, "__file__", "") or (
        tools_mod.__path__[0] if hasattr(tools_mod, "__path__") and tools_mod.__path__ else ""
    )
    if not tools_path.startswith(REPO_ROOT):
        for _m in list(sys.modules.keys()):
            if _m == "tools" or _m.startswith("tools."):
                del sys.modules[_m]

for _m in list(sys.modules.keys()):
    if _m.startswith("tools."):
        _mf = getattr(sys.modules[_m], "__file__", "") or ""
        if _mf and not os.path.abspath(_mf).startswith(REPO_ROOT):
            del sys.modules[_m]

import tools
tools.__path__ = [os.path.join(REPO_ROOT, "tools")]

# Pin local worktree bridge_voice before gotchi_zero_adapter imports backend.voice_gateway
import tools.bridge_voice  # noqa: F401, E402

from tools.voice_quick_tools_auditor import (
    JOB_DAY_OF_WEEK,
    JOB_HOUR_PT,
    JOB_ID,
    JOB_MINUTE_PT,
    TARGET_CHANNEL_ID,
    discover_skills_and_tools,
    ensure_weekly_schedule_registered,
    get_active_inline_voice_tools,
    identify_promotion_candidates,
    main,
    run_voice_quick_tools_audit,
    scan_voice_gateway_logs,
    stage_quick_tools_promotion_card,
)


def test_discover_skills_and_tools(tmp_path: Path) -> None:
    """Verifies discovery of SKILL.md playbooks and Python tool scripts."""
    skills_dir_1 = tmp_path / "workspace_skills"
    skills_dir_2 = tmp_path / "cli_skills"
    tools_dir = tmp_path / "tools"

    sk1 = skills_dir_1 / "garage-door-quick"
    sk1.mkdir(parents=True)
    (sk1 / "SKILL.md").write_text(
        "---\nname: garage-door-quick\ndescription: Fast <1.5s inline voice garage door status check and toggle.\n---\n# Garage\n",
        encoding="utf-8",
    )

    sk2 = skills_dir_2 / "outbox"
    sk2.mkdir(parents=True)
    (sk2 / "SKILL.md").write_text(
        "---\nname: outbox\ndescription: Cross-channel outbox spooling.\n---\n# Outbox\n",
        encoding="utf-8",
    )

    tools_dir.mkdir(parents=True)
    (tools_dir / "sprinkler_quick_tool.py").write_text(
        '"""Fast <2s inline voice sprinkler zone status lookup."""\ndef run():\n    return True\n',
        encoding="utf-8",
    )
    (tools_dir / "test_ignore_me.py").write_text(
        '"""Unit test file that should be ignored by tool discovery."""\n',
        encoding="utf-8",
    )

    discovered = discover_skills_and_tools(
        skill_dirs=[skills_dir_1, skills_dir_2],
        tools_dir=tools_dir,
    )

    skill_names = {s["name"] for s in discovered["skills"]}
    tool_names = {t["name"] for t in discovered["tools"]}

    assert "garage-door-quick" in skill_names
    assert "outbox" in skill_names
    assert "sprinkler_quick_tool" in tool_names
    assert "test_ignore_me" not in tool_names


def test_get_active_inline_voice_tools_includes_5_household_quick_tools() -> None:
    """Verifies active inline voice tools from _build_gotchi_tools() include the 5 household Quick Tools."""
    active = get_active_inline_voice_tools()
    expected = {
        "home_assistant_control",
        "check_calendar",
        "tv_and_plex_control",
        "check_3d_printer",
        "grocery_list_action",
        "load_skill_playbook",
        "handoff_to_zero_runtime",
    }
    assert expected.issubset(active)


def test_scan_voice_gateway_logs_counts_playbook_and_handoff_triggers(tmp_path: Path) -> None:
    """Verifies log parser extracts load_skill_playbook and handoff_to_zero_runtime call counts."""
    log_file = tmp_path / "gotchi_gateway.log"
    log_file.write_text(
        "\n".join([
            "INFO: [GotchiZero] Executing tool call: load_skill_playbook({'skill_name': 'ev-charger-status'})",
            "INFO: [GotchiZero] Executing tool call: load_skill_playbook({'skill_name': 'ev-charger-status'})",
            "INFO: [GotchiZero] Executing tool call: load_skill_playbook({'skill_name': 'plex-playback'})",
            "INFO: [GotchiZero] Executing tool call: handoff_to_zero_runtime({'task_prompt': 'Check EV charger status', 'spoken_ack': 'Checking now.'})",
        ]),
        encoding="utf-8",
    )

    stats = scan_voice_gateway_logs(log_path=log_file)
    assert stats["total_playbook_calls"] == 3
    assert stats["total_handoff_calls"] == 1
    assert stats["playbook_counts"]["ev-charger-status"] == 2
    assert stats["playbook_counts"]["plex-playback"] == 1


def test_candidate_filtering_excludes_covered_and_heavy_skills_and_promotes_fast_or_repeated() -> None:
    """Verifies candidate filtering excludes existing inline tools/heavy skills and promotes <3s or repeated log candidates."""
    active_inline = get_active_inline_voice_tools()
    discovered = {
        "skills": [
            {
                "name": "whole-foods-cart",
                "normalized_name": "whole-foods-cart",
                "description": "Orchestrates Whole Foods cart staging (already covered by grocery_list_action).",
                "path": "/workspace/.agents/skills/whole-foods-cart/SKILL.md",
                "kind": "skill",
            },
            {
                "name": "modal-compute",
                "normalized_name": "modal-compute",
                "description": "Offloads heavy ML sweeps to Modal cloud compute.",
                "path": "/workspace/.agents/skills/modal-compute/SKILL.md",
                "kind": "skill",
            },
            {
                "name": "ev-battery-quick",
                "normalized_name": "ev-battery-quick",
                "description": "Fast <1.5s inline voice EV state-of-charge and range lookup.",
                "path": "/workspace/.agents/skills/ev-battery-quick/SKILL.md",
                "kind": "skill",
            },
        ],
        "tools": [
            {
                "name": "gotchi_zero_adapter",
                "normalized_name": "gotchi_zero_adapter",
                "description": "Main voice adapter.",
                "path": "/workspace/tools/gotchi_zero_adapter.py",
                "kind": "tool",
            },
            {
                "name": "pool_temp_sensor",
                "normalized_name": "pool_temp_sensor",
                "description": "Fast <1s inline voice pool temperature sensor query.",
                "path": "/workspace/tools/pool_temp_sensor.py",
                "kind": "tool",
            },
        ],
    }
    log_stats = {
        "playbook_counts": {"nursery-sound-machine": 3, "whole-foods-cart": 1},
        "handoff_counts": {},
        "total_playbook_calls": 4,
        "total_handoff_calls": 0,
    }

    candidates = identify_promotion_candidates(
        discovered=discovered,
        active_inline_tools=active_inline,
        log_stats=log_stats,
    )
    cand_names = {c["name"] for c in candidates}

    assert "whole-foods-cart" not in cand_names
    assert "modal-compute" not in cand_names
    assert "gotchi_zero_adapter" not in cand_names
    assert "ev-battery-quick" in cand_names
    assert "pool_temp_sensor" in cand_names
    assert "nursery-sound-machine" in cand_names


def test_clean_baseline_yields_zero_candidates_and_silent_no_reply(tmp_path: Path) -> None:
    """When 0 new candidates exist on baseline skills/tools, audit returns empty string for silent [NO_REPLY]."""
    empty_log = tmp_path / "gotchi_gateway.log"
    empty_log.write_text("INFO: Uvicorn running on http://127.0.0.1:8771\n", encoding="utf-8")

    with patch("tools.voice_quick_tools_auditor.stage_decision") as mock_stage:
        ok, report, meta = run_voice_quick_tools_audit(
            log_path=empty_log,
            post_to_discord=False,
        )
        assert ok is True
        assert report == ""
        assert meta["candidates_count"] == 0
        assert meta["decision_staged"] is False
        mock_stage.assert_not_called()


def test_stage_quick_tools_promotion_card_stages_atomic_decision_card() -> None:
    """When candidates exist, stages an atomic FORGE Decision Card with explicit Approve Action summary."""
    candidates = [
        {
            "name": "ev-battery-quick",
            "kind": "skill",
            "path": "/workspace/.agents/skills/ev-battery-quick/SKILL.md",
            "description": "Fast <1.5s inline voice EV battery lookup.",
            "proposed_tool_name": "voice_ev_battery_quick",
            "log_triggers": 3,
            "reason": "Repeated voice log trigger (3x in gotchi_gateway.log)",
        }
    ]
    with patch("tools.voice_quick_tools_auditor.stage_decision") as mock_stage:
        mock_stage.return_value = {
            "ok": True,
            "decision_id": "dec-12345678-voicequi",
            "channel_id": TARGET_CHANNEL_ID,
        }
        res = stage_quick_tools_promotion_card(
            candidates=candidates,
            channel_id=TARGET_CHANNEL_ID,
            post_to_discord=False,
        )
        assert res is not None
        assert res["ok"] is True
        assert res["decision_id"] == "dec-12345678-voicequi"

        mock_stage.assert_called_once()
        kws = mock_stage.call_args.kwargs
        assert kws["category"] == "voice_quick_tools"
        assert kws["channel_id"] == TARGET_CHANNEL_ID
        assert kws["handler_type"] == "shell"
        assert "**Approve Action:**" in kws["summary"]
        assert "ev-battery-quick" in kws["summary"]
        assert "--promote ev-battery-quick" in kws["payload"]["command"]


def test_ensure_weekly_schedule_registered_idempotent(tmp_path: Path) -> None:
    """Verifies idempotent registration of voice_quick_tools_weekly_audit on Sundays at 18:40 PT."""
    sched_file = tmp_path / "schedule.json"
    sched_file.write_text(
        json.dumps(
            [
                {
                    "id": "existing_job",
                    "name": "Existing Job",
                    "enabled": True,
                    "schedule_type": "daily",
                    "hour_pt": 8,
                    "minute_pt": 0,
                    "prompt": "noop",
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    # First call: creates the schedule entry
    res1 = ensure_weekly_schedule_registered(schedule_path=sched_file)
    assert res1["ok"] is True
    assert res1["created"] is True
    assert res1["id"] == JOB_ID

    jobs_after_1 = json.loads(sched_file.read_text(encoding="utf-8"))
    matching = [j for j in jobs_after_1 if j.get("id") == JOB_ID]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["schedule_type"] == "weekly"
    assert entry["day_of_week"] == JOB_DAY_OF_WEEK == 6
    assert entry["hour_pt"] == JOB_HOUR_PT == 18
    assert entry["minute_pt"] == JOB_MINUTE_PT == 40
    assert entry["channel_id"] == TARGET_CHANNEL_ID == 1542081375287640084
    assert "[NO_REPLY]" in entry["prompt"]

    # Second call: idempotent (no duplicate created, no modification needed)
    res2 = ensure_weekly_schedule_registered(schedule_path=sched_file)
    assert res2["ok"] is True
    assert res2["created"] is False
    assert res2["updated"] is False

    jobs_after_2 = json.loads(sched_file.read_text(encoding="utf-8"))
    assert len([j for j in jobs_after_2 if j.get("id") == JOB_ID]) == 1

    # CLI --register-schedule invocation
    rc = main(["--register-schedule", "--schedule-path", str(sched_file)])
    assert rc == 0


def test_gemini_live_audio_sink_fails_closed_on_none_bot_and_non_owner() -> None:
    """GeminiLiveAudioSink.write() must immediately drop packets from None, bots, or non-owners."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tools.bridge_voice import GeminiLiveAudioSink

    loop = asyncio.new_event_loop()
    try:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
        with patch("tools.bridge_voice.discord.opus.Decoder") as mock_decoder_cls:
            decoder_inst = MagicMock()
            mock_decoder_cls.return_value = decoder_inst
            sink = GeminiLiveAudioSink(loop=loop, audio_in_queue=queue, target_user_id=999001)

            fake_data = SimpleNamespace(opus=b"\x01\x02\x03\x04")

            # 1. user=None MUST fail closed (never fall back to target_user_id)
            sink.write(None, fake_data)
            decoder_inst.decode.assert_not_called()

            # 2. Bot user with matching ID MUST be dropped
            sink.write(SimpleNamespace(id=999001, bot=True), fake_data)
            decoder_inst.decode.assert_not_called()

            # 3. Non-owner human user MUST be dropped
            sink.write(SimpleNamespace(id=888002, bot=False), fake_data)
            decoder_inst.decode.assert_not_called()

            # 4. User object missing id attribute MUST be dropped
            sink.write(SimpleNamespace(bot=False), fake_data)
            decoder_inst.decode.assert_not_called()

            assert sink.input_audio_seconds == 0.0
            assert queue.empty()
    finally:
        loop.close()


def test_bridge_voice_read_local_file_blocks_secrets_and_private_paths() -> None:
    """Voice read_local_file must reject /secrets/, .env, memory/private, memory/vault, and '..'."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tools.bridge_voice import ZeroVoiceSessionManager

    mgr = ZeroVoiceSessionManager(bot=MagicMock())
    blocked_paths = [
        "/secrets/env.json",
        "/workspace/.env",
        "/workspace/.env.local",
        "/workspace/memory/private/user_ryan.md",
        "/workspace/memory/vault/keys.txt",
        "/workspace/../secrets/env.json",
        "/etc/passwd",
    ]
    for path in blocked_paths:
        res = asyncio.run(
            mgr._execute_tool_call(
                SimpleNamespace(name="read_local_file", args={"file_path": path}),
                notify_channel=None,
            )
        )
        assert "error" in res, f"Expected read_local_file to reject {path}, got: {res}"
        assert "Access denied" in res["error"], f"Unexpected error for {path}: {res}"


def test_bridge_voice_run_shell_command_always_stages_decision_card() -> None:
    """Voice run_shell_command must NEVER run subprocess.run(shell=True); always stages Decision Card."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tools.bridge_voice import TARGET_CHANNEL_ID, ZeroVoiceSessionManager

    mgr = ZeroVoiceSessionManager(bot=MagicMock())
    staged_payload = {
        "confirmation_required": True,
        "status": "staged_in_zero_chat",
        "channel_id": TARGET_CHANNEL_ID,
        "tool": "run_shell_command",
        "message": "Staged in #zero-chat",
    }

    with (
        patch("tools.bridge_voice.subprocess.run") as mock_subproc,
        patch(
            "tools.gotchi_zero_adapter.stage_voice_mutation_confirmation",
            return_value=staged_payload,
        ) as mock_stage,
    ):
        res = asyncio.run(
            mgr._execute_tool_call(
                SimpleNamespace(name="run_shell_command", args={"command": "id && whoami"}),
                notify_channel=None,
            )
        )
        mock_subproc.assert_not_called()
        mock_stage.assert_called_once_with("run_shell_command", {"command": "id && whoami"})
        assert res["confirmation_required"] is True
        assert res["status"] == "staged_in_zero_chat"


def test_bridge_voice_taint_blocks_write_after_gmail_and_web_search_and_restricts_paths(
    tmp_path: Path,
) -> None:
    """write_local_file enforces workspace path restrictions and stages Decision Card after Gmail/web taint."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from tools.bridge_voice import TARGET_CHANNEL_ID, ZeroVoiceSessionManager

    # 1. Path restriction check (blocks /secrets/, .env, memory/private, memory/vault, .., /tmp)
    mgr = ZeroVoiceSessionManager(bot=MagicMock())
    for bad_path in (
        "/secrets/pwn.json",
        "/workspace/.env",
        "/workspace/memory/private/pwn.md",
        "/workspace/memory/vault/pwn.md",
        "/workspace/scratch/../../etc/cron.d/pwn",
        str(tmp_path / "outside_workspace.txt"),
    ):
        res = asyncio.run(
            mgr._execute_tool_call(
                SimpleNamespace(
                    name="write_local_file",
                    args={"file_path": bad_path, "content": "evil"},
                ),
                notify_channel=None,
            )
        )
        assert "error" in res and "Access denied" in res["error"], f"Failed to block {bad_path}: {res}"

    # 2. Taint via search_gmail blocks direct write_local_file and stages a Decision Card
    mgr_gmail = ZeroVoiceSessionManager(bot=MagicMock())
    assert mgr_gmail._session_tainted_by_external_data is False

    with (
        patch("tools.zero_mail_listener.get_access_token", side_effect=RuntimeError("mock-stop")),
    ):
        asyncio.run(
            mgr_gmail._execute_tool_call(
                SimpleNamespace(name="search_gmail", args={"query": "invoice"}),
                notify_channel=None,
            )
        )
    assert mgr_gmail._session_tainted_by_external_data is True

    staged_write = {
        "confirmation_required": True,
        "status": "staged_in_zero_chat",
        "channel_id": TARGET_CHANNEL_ID,
        "tool": "write_local_file",
        "message": "Staged in #zero-chat",
    }
    target_ws_file = "/workspace/scratch/voice_taint_test_never_written.txt"
    with (
        patch(
            "tools.gotchi_zero_adapter.stage_voice_mutation_confirmation",
            return_value=staged_write,
        ) as mock_stage,
        patch("pathlib.Path.write_text") as mock_write_text,
    ):
        res_write = asyncio.run(
            mgr_gmail._execute_tool_call(
                SimpleNamespace(
                    name="write_local_file",
                    args={"file_path": target_ws_file, "content": "prompt-injected write"},
                ),
                notify_channel=None,
            )
        )
        mock_write_text.assert_not_called()
        mock_stage.assert_called_once()
        assert res_write["confirmation_required"] is True
        assert res_write["tainted_session"] is True

    # 3. Taint via web_and_reddit_search also sets taint and blocks direct write_local_file
    mgr_web = ZeroVoiceSessionManager(bot=MagicMock())
    assert mgr_web._session_tainted_by_external_data is False
    fake_proc = SimpleNamespace(stdout="reddit thread", stderr="", returncode=0)
    with patch("tools.bridge_voice.subprocess.run", return_value=fake_proc):
        asyncio.run(
            mgr_web._execute_tool_call(
                SimpleNamespace(name="web_and_reddit_search", args={"query": "homelab"}),
                notify_channel=None,
            )
        )
    assert mgr_web._session_tainted_by_external_data is True

    with (
        patch(
            "tools.gotchi_zero_adapter.stage_voice_mutation_confirmation",
            return_value=staged_write,
        ) as mock_stage2,
        patch("pathlib.Path.write_text") as mock_write_text2,
    ):
        res_write2 = asyncio.run(
            mgr_web._execute_tool_call(
                SimpleNamespace(
                    name="write_local_file",
                    args={"file_path": target_ws_file, "content": "web-injected write"},
                ),
                notify_channel=None,
            )
        )
        mock_write_text2.assert_not_called()
        mock_stage2.assert_called_once()
        assert res_write2["confirmation_required"] is True
        assert res_write2["tainted_session"] is True


def test_bridge_voice_post_note_hardcoded_to_zero_chat() -> None:
    """post_note_to_zero_chat must strictly send to TARGET_CHANNEL_ID, ignoring arbitrary notify_channel."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from tools.bridge_voice import TARGET_CHANNEL_ID, ZeroVoiceSessionManager

    zero_chat_ch = MagicMock()
    zero_chat_ch.send = AsyncMock()
    foreign_ch = MagicMock()
    foreign_ch.send = AsyncMock()

    bot = MagicMock()
    bot.get_channel.side_effect = lambda cid: zero_chat_ch if cid == TARGET_CHANNEL_ID else None

    mgr = ZeroVoiceSessionManager(bot=bot)
    res = asyncio.run(
        mgr._execute_tool_call(
            SimpleNamespace(name="post_note_to_zero_chat", args={"message": "Hello Zero Chat"}),
            notify_channel=foreign_ch,
        )
    )
    assert res == {"status": "posted to #zero-chat"}
    bot.get_channel.assert_called_with(TARGET_CHANNEL_ID)
    zero_chat_ch.send.assert_awaited_once()
    foreign_ch.send.assert_not_awaited()
