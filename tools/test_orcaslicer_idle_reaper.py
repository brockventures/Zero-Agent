"""Unit tests for OrcaSlicer 30-minute idle auto-stop sidecar."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_TOOLS_DIR.parent))


def _load_local_module(name: str):
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reaper = _load_local_module("orcaslicer_idle_reaper")


def _idle_probe(**overrides):
    base = {
        "running": True,
        "uptime_sec": 3600.0,
        "compose_dir": "/opt/stacks/orcaslicer",
        "compose_service": "orcaslicer",
        "cpu_pct": 0.4,
        "active_web_conns": 0,
        "cli_slicing": False,
        "recent_print_write": False,
    }
    base.update(overrides)
    return base


def test_not_running_clears_idle_state_and_noops(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe(running=False)),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2000.0)

    assert ok is True
    assert msg == ""
    assert data["action"] == "not_running"
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] is None


def test_active_web_session_clears_idle_state(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe(active_web_conns=2)),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2000.0)

    assert ok is True
    assert msg == ""
    assert data["action"] == "active"
    assert "active_web_conns" in data["reasons"]
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] is None


def test_active_cpu_and_cli_slicing_block_reap(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(
            reaper,
            "probe_orcaslicer_activity",
            return_value=_idle_probe(cpu_pct=12.5, cli_slicing=True),
        ),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2000.0)

    assert ok is True
    assert msg == ""
    assert data["action"] == "active"
    assert set(data["reasons"]) == {"cpu_active", "cli_slicing"}
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] is None


def test_recent_print_write_blocks_reap(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe(recent_print_write=True)),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2000.0)

    assert ok is True
    assert msg == ""
    assert data["action"] == "active"
    assert "recent_print_write" in data["reasons"]
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] is None


def test_uptime_grace_period_blocks_reap(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe(uptime_sec=900.0)),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2000.0)

    assert ok is True
    assert msg == ""
    assert data["action"] == "active"
    assert "uptime_grace" in data["reasons"]
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] is None


def test_first_idle_observation_under_timeout_does_not_stop(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe()),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok1, msg1, data1 = reaper.evaluate_and_reap(state_path=state_path, now_ts=1000.0)
        ok2, msg2, data2 = reaper.evaluate_and_reap(state_path=state_path, now_ts=2500.0)

    assert ok1 is True and msg1 == "" and data1["action"] == "idle_waiting"
    assert data1["idle_sec"] == 0.0
    assert ok2 is True and msg2 == "" and data2["action"] == "idle_waiting"
    assert data2["idle_sec"] == 1500.0
    mock_stop.assert_not_called()
    assert json.loads(state_path.read_text(encoding="utf-8"))["idle_since_ts"] == 1000.0


def test_idle_at_or_above_timeout_triggers_compose_stop_and_clears_state(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe()),
        patch.object(reaper, "stop_orcaslicer_compose", return_value=(True, "")) as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(state_path=state_path, now_ts=2800.0)

    assert ok is True
    assert msg == "Stopped idle OrcaSlicer container after 30m inactivity."
    assert data["action"] == "stopped"
    mock_stop.assert_called_once_with("/opt/stacks/orcaslicer", "orcaslicer")
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["idle_since_ts"] is None
    assert saved["last_stopped_ts"] == 2800.0


def test_dry_run_does_not_stop_container(tmp_path):
    state_path = tmp_path / "orcaslicer_idle_state.json"
    state_path.write_text(json.dumps({"idle_since_ts": 1000.0}), encoding="utf-8")

    with (
        patch.object(reaper, "probe_orcaslicer_activity", return_value=_idle_probe()),
        patch.object(reaper, "stop_orcaslicer_compose") as mock_stop,
    ):
        ok, msg, data = reaper.evaluate_and_reap(dry_run=True, state_path=state_path, now_ts=2900.0)

    assert ok is True
    assert "DRY-RUN" in msg
    assert data["action"] == "dry_run_would_stop"
    mock_stop.assert_not_called()


def test_probe_and_compose_stop_use_nas_config_host2_ip():
    probe_json = json.dumps(_idle_probe())
    with (
        patch.object(reaper.nas_config, "HOST_2_IP", "198.51.100.84"),
        patch.object(reaper.nas_config, "run_ssh", return_value=(0, probe_json + "\n", "")) as mock_ssh,
    ):
        res = reaper.probe_orcaslicer_activity(idle_timeout_sec=1800)

    assert res["running"] is True
    assert res["compose_dir"] == "/opt/stacks/orcaslicer"
    assert mock_ssh.call_args[0][0] == "198.51.100.84"
    remote_cmd = mock_ssh.call_args[0][1]
    assert "base64.b64decode" in remote_cmd

    # Verify the encoded script compiles cleanly as valid Python
    b64_part = remote_cmd.split("b64decode('", 1)[1].split("'", 1)[0]
    decoded_py = base64.b64decode(b64_part).decode("utf-8")
    compile(decoded_py, "<remote_probe>", "exec")

    with (
        patch.object(reaper.nas_config, "HOST_2_IP", "198.51.100.84"),
        patch.object(reaper.nas_config, "run_ssh", return_value=(0, "", "")) as mock_stop_ssh,
    ):
        ok, _ = reaper.stop_orcaslicer_compose("/opt/stacks/orcaslicer", "orcaslicer")

    assert ok is True
    assert mock_stop_ssh.call_args[0][0] == "198.51.100.84"
    stop_cmd = mock_stop_ssh.call_args[0][1]
    assert "cd /opt/stacks/orcaslicer && sudo /usr/local/bin/docker compose stop orcaslicer" == stop_cmd


def test_sidecars_wrapper_passes_dry_run():
    sidecars = _load_local_module("sidecars")
    with patch.object(reaper, "evaluate_and_reap", return_value=(True, "", {"action": "active"})) as mock_eval:
        # Ensure tools.orcaslicer_idle_reaper resolves to our loaded module
        sys.modules["tools.orcaslicer_idle_reaper"] = reaper
        ok, msg, data = sidecars.run_orcaslicer_idle_reaper(test=True)

    assert ok is True
    assert msg == ""
    assert data == {"action": "active"}
    mock_eval.assert_called_once_with(dry_run=True)
