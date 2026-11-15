import importlib.util
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_HOOK_PATH = os.path.join(_REPO_ROOT, ".agents", "hooks", "batch_guard.py")

_spec = importlib.util.spec_from_file_location("hooks_batch_guard", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
batch_guard = importlib.util.module_from_spec(_spec)
sys.modules["hooks_batch_guard"] = batch_guard
_spec.loader.exec_module(batch_guard)


@pytest.fixture(autouse=True)
def isolate_batch_guard_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_WORKER_ACTIVE", raising=False)
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.setattr(batch_guard, "IN_FLIGHT_TURN_FILE", str(tmp_path / "nonexistent_in_flight_turn.json"))


def test_big_model_suite_inline_multi_year_oos_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        'python3 -c "from big_projections.models import BIGModelSuite; '
        '[BIGModelSuite().fit_and_predict(y) for y in [2021, 2022, 2023]]"'
    )
    ok_helper, reason_helper = batch_guard.check_baseball_heavy_sweep_and_migration_guard(cmd)
    assert ok_helper is False
    assert "/baseball/big-projections/modal_harness.py" in reason_helper
    assert "Modal" in reason_helper

    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/modal_harness.py" in reason


def test_big_model_suite_heredoc_range_holdout_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        "python3 - <<'EOF'\n"
        "from models import BIGModelSuite\n"
        "for season in range(2019, 2025):\n"
        "    suite = BIGModelSuite(holdout=True)\n"
        "    suite.fit(season)\n"
        "EOF"
    )
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/modal_harness.py" in reason


def test_big_model_suite_local_script_oos_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    sweep_script = tmp_path / "oos_sweep.py"
    sweep_script.write_text(
        "from models import BIGModelSuite\n"
        "for year in [2021, 2022, 2023, 2024]:\n"
        "    BIGModelSuite().fit_and_predict(year)\n",
        encoding="utf-8",
    )
    cmd = f"python3 {sweep_script}"
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd=str(tmp_path),
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/modal_harness.py" in reason


def test_big_model_suite_job_start_without_modal_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        'python3 /workspace/tools/forge.py job start --name "OOS Holdout" '
        '--command "python3 -c \'from models import BIGModelSuite; '
        '[BIGModelSuite().fit(y) for y in [2022, 2023]]\'"'
    )
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/modal_harness.py" in reason


def test_big_model_suite_modal_harness_allowed(tmp_path):
    state_file = str(tmp_path / "state.json")
    direct_cmd = "python3 /baseball/big-projections/modal_harness.py --mode oos --years 2021,2022,2023"
    ok_direct, reason_direct = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": direct_cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok_direct is True, reason_direct

    job_cmd = (
        'python3 /workspace/tools/forge.py job start --name "Modal OOS" '
        '--command "python3 /baseball/big-projections/modal_harness.py --model BIGModelSuite --years 2021,2022,2023"'
    )
    ok_job, reason_job = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": job_cmd},
        state_file=str(tmp_path / "state2.json"),
        cwd="/workspace",
        now=1000.0,
    )
    assert ok_job is True, reason_job


def test_big_model_suite_single_season_allowed(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = 'python3 -c "from models import BIGModelSuite; print(BIGModelSuite())"'
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is True, reason


def test_migrate_all_pt_chained_predict_big_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        "python3 /baseball/big-projections/predict_big.py --migrate-all-pt-modes && "
        "python3 /baseball/big-projections/predict_big.py --migrate-db"
    )
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/refresh_preseason.py" in reason


def test_migrate_all_pt_shell_loop_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        "for mode in blended oopsy; do "
        "python3 /baseball/big-projections/predict_big.py --migrate-all-pt-modes --pt-mode $mode; "
        "done"
    )
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/refresh_preseason.py" in reason


def test_migrate_all_pt_job_start_chained_blocked(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        'python3 /workspace/tools/forge.py job start --name "Migrate PT" '
        '--command "python3 /baseball/big-projections/predict_big.py --migrate-all-pt-modes && '
        'python3 /baseball/big-projections/predict_big.py --pt-mode oopsy"'
    )
    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "/baseball/big-projections/refresh_preseason.py" in reason


def test_migrate_all_pt_bare_inline_blocked_by_heavy_gate(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = "python3 /baseball/big-projections/predict_big.py --migrate-all-pt-modes"
    assert batch_guard.is_heavy_inline_benchmark_or_migration(cmd, cwd="/workspace") is True

    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is False
    assert "forge.py job start" in reason


def test_migrate_all_pt_refresh_preseason_via_job_start_allowed(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = (
        'python3 /workspace/tools/forge.py job start --name "Preseason Refresh" '
        '--command "python3 /baseball/big-projections/refresh_preseason.py"'
    )
    assert batch_guard.is_heavy_inline_benchmark_or_migration(cmd, cwd="/workspace") is False

    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is True, reason


def test_migrate_all_pt_refresh_preseason_inline_not_heavy_gated(tmp_path):
    state_file = str(tmp_path / "state.json")
    cmd = "python3 /baseball/big-projections/refresh_preseason.py --migrate-all-pt-modes"
    assert batch_guard.is_heavy_inline_benchmark_or_migration(cmd, cwd="/workspace") is False

    ok, reason = batch_guard.check_tool_use(
        "run_command",
        {"CommandLine": cmd},
        state_file=state_file,
        cwd="/workspace",
        now=1000.0,
    )
    assert ok is True, reason
