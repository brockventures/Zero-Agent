#!/usr/bin/env python3
"""
Unit tests for deterministic Tool Output Shaper (output_shaper.py) and batch_guard.py integration.
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, "/workspace/.agents/hooks")
sys.path.insert(0, "/workspace/tools")
sys.path.insert(0, os.path.join(ROOT_DIR, ".agents", "hooks"))
sys.path.insert(0, os.path.join(ROOT_DIR, "tools"))

import batch_guard
import output_shaper


def test_merge_line_ranges():
    merged = output_shaper.format_merged_line_ranges("10, 12-15, 16, 22->exit, 30-32")
    assert merged == "L10, L12-L16, L22, L30-L32", f"Unexpected merged ranges: {merged}"


def test_shape_pytest_pass_collapses_noise():
    raw_pytest = """============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-8.2.0, pluggy-1.5.0
rootdir: /workspace
plugins: anyio-4.4.0, asyncio-0.23.7
collected 42 items

tools/test_alpha.py ........................                             [ 57%]
tools/test_beta.py ..................                                    [100%]

============================== 42 passed in 0.38s ==============================
"""
    shaped = output_shaper.shape_pytest_output(raw_pytest, returncode=0)
    assert shaped == "✅ pytest: 42 passed in 0.38s", f"Unexpected shaped output: {shaped}"


def test_shape_pytest_failure_keeps_only_failures():
    raw_pytest = """============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-8.2.0
rootdir: /workspace
collected 3 items

tools/test_alpha.py::test_one PASSED                                     [ 33%]
tools/test_alpha.py::test_two FAILED                                     [ 66%]
tools/test_alpha.py::test_three PASSED                                   [100%]

=================================== FAILURES ===================================
___________________________________ test_two ___________________________________

    def test_two():
        x = 41
>       assert x == 42
E       assert 41 == 42

tools/test_alpha.py:14: AssertionError
=========================== short test summary info ============================
FAILED tools/test_alpha.py::test_two - assert 41 == 42
========================= 1 failed, 2 passed in 0.19s ==========================
"""
    shaped = output_shaper.shape_pytest_output(raw_pytest, returncode=1)
    assert "test_one PASSED" not in shaped
    assert "platform linux" not in shaped
    assert "assert 41 == 42" in shaped
    assert "FAILED tools/test_alpha.py::test_two" in shaped
    assert "❌ pytest: 1 failed, 2 passed in 0.19s" in shaped


def test_shape_coverage_output_and_ast_functions():
    with tempfile.TemporaryDirectory() as tmpdir:
        sample_py = os.path.join(tmpdir, "sample_mod.py")
        with open(sample_py, "w", encoding="utf-8") as f:
            f.write(
                "def covered_fn():\n"
                "    return 1\n\n"
                "def uncovered_alpha(x):\n"
                "    a = x + 1\n"
                "    b = a + 2\n"
                "    c = b + 3\n"
                "    return c\n\n"
                "class Worker:\n"
                "    def uncovered_beta(self):\n"
                "        y = 10\n"
                "        return y\n"
            )

        raw_cov = """Name            Stmts   Miss  Cover   Missing
---------------------------------------------
clean_mod.py       50      0   100%
sample_mod.py      12      6    50%   5-8, 12-13
---------------------------------------------
TOTAL              62      6    90%
"""
        shaped = output_shaper.shape_coverage_output(raw_cov, threshold=100.0, cwd=tmpdir)
        assert "clean_mod.py" not in shaped
        assert "sample_mod.py: 50% (deficit: 6 stmts)" in shaped
        assert "Ranges: L5-L8, L12-L13" in shaped
        assert "uncovered_alpha (4)" in shaped
        assert "Worker.uncovered_beta (2)" in shaped


def test_run_with_temp_log_preserves_exit_code():
    rc_ok, out_ok, log_ok = output_shaper.run_with_temp_log(
        [sys.executable, "-c", "print('=== 5 passed in 0.05s ===')"]
    )
    assert rc_ok == 0
    assert not os.path.exists(log_ok)

    rc_fail, out_fail, log_fail = output_shaper.run_with_temp_log(
        [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]
    )
    assert rc_fail == 3
    assert "boom" in out_fail
    assert not os.path.exists(log_fail)


def test_rewrite_command_for_shaping_and_git_quiet():
    cmd = "ruff check tools/output_shaper.py && pytest tools/test_output_shaper.py && git checkout main && git status -s"
    rewritten = output_shaper.rewrite_command_for_shaping(cmd)
    assert "output_shaper.py --run -- ruff check tools/output_shaper.py" in rewritten
    assert "output_shaper.py --run -- pytest tools/test_output_shaper.py" in rewritten
    assert "git checkout -q main" in rewritten
    assert rewritten.endswith("&& git status -s")

    # Commands with pipes or redirections must remain untouched in that segment
    piped = "pytest tools/test_output_shaper.py | tee /tmp/out.log"
    assert output_shaper.rewrite_command_for_shaping(piped) == piped


def test_rewrite_command_sqlite3_compact():
    # 1. Injects -list when no explicit mode flag is present
    cmd = 'sqlite3 /workspace/data/test.db "SELECT * FROM users"'
    rewritten = output_shaper.rewrite_command_for_shaping(cmd)
    assert rewritten == 'sqlite3 -list /workspace/data/test.db "SELECT * FROM users"'
    # Idempotent
    assert output_shaper.rewrite_command_for_shaping(rewritten) == rewritten

    # Bare sqlite3
    assert output_shaper.rewrite_command_for_shaping("sqlite3") == "sqlite3 -list"
    assert output_shaper.rewrite_command_for_shaping("sqlite3 -list") == "sqlite3 -list"

    # sqlite3 with other options like -header or -readonly still gets -list
    header_cmd = 'sqlite3 -header -readonly /tmp/test.db "SELECT 1"'
    assert output_shaper.rewrite_command_for_shaping(header_cmd) == 'sqlite3 -list -header -readonly /tmp/test.db "SELECT 1"'

    # 2. Preserves explicit formatting flags
    explicit_flags = [
        "-list",
        "-csv",
        "--csv",
        "-json",
        "--json",
        "-line",
        "-column",
        "-box",
        "-table",
        "-markdown",
        "-html",
        "--help",
        "-help",
        "-h",
        "-version",
        "--version",
    ]
    for flag in explicit_flags:
        explicit_cmd = f'sqlite3 {flag} /tmp/test.db "SELECT 1"'
        assert output_shaper.rewrite_command_for_shaping(explicit_cmd) == explicit_cmd, f"Failed for {flag}"


def test_rewrite_command_psql_compact():
    # 1. Injects -A -P pager=off when no explicit mode flag is present
    cmd = 'psql -c "SELECT 1"'
    rewritten = output_shaper.rewrite_command_for_shaping(cmd)
    assert rewritten == 'psql -A -P pager=off -c "SELECT 1"'
    # Idempotent
    assert output_shaper.rewrite_command_for_shaping(rewritten) == rewritten

    # Bare psql
    assert output_shaper.rewrite_command_for_shaping("psql") == "psql -A -P pager=off"
    assert output_shaper.rewrite_command_for_shaping("psql -A -P pager=off") == "psql -A -P pager=off"

    # psql with db/user flags
    app_cmd = 'psql -U postgres -d app -c "SELECT * FROM orders"'
    rewritten_app = output_shaper.rewrite_command_for_shaping(app_cmd)
    assert rewritten_app == 'psql -A -P pager=off -U postgres -d app -c "SELECT * FROM orders"'
    assert output_shaper.rewrite_command_for_shaping(rewritten_app) == rewritten_app

    # If pager=off is already present, injects -A without duplicating -P pager=off
    pager_cmd = 'psql -P pager=off -c "SELECT 1"'
    rewritten_pager = output_shaper.rewrite_command_for_shaping(pager_cmd)
    assert rewritten_pager == 'psql -A -P pager=off -c "SELECT 1"'

    # 2. Preserves explicit flags
    explicit_psql_flags = [
        "-A",
        "--no-align",
        "--csv",
        "-H",
        "--html",
        "-x",
        "--expanded",
        "-t",
        "--tuples-only",
        "--help",
        "-h",
        "-?",
        "-V",
        "--version",
    ]
    for flag in explicit_psql_flags:
        explicit_cmd = f'psql {flag} -c "SELECT 1"'
        assert output_shaper.rewrite_command_for_shaping(explicit_cmd) == explicit_cmd, f"Failed for {flag}"


def test_rewrite_command_compound_db_chains():
    cmd = 'sqlite3 /workspace/data/test.db "SELECT 1" && psql -U postgres -d app -c "SELECT 2"'
    rewritten = output_shaper.rewrite_command_for_shaping(cmd)
    expected = (
        'sqlite3 -list /workspace/data/test.db "SELECT 1" && '
        'psql -A -P pager=off -U postgres -d app -c "SELECT 2"'
    )
    assert rewritten == expected
    # Idempotent on compound chain
    assert output_shaper.rewrite_command_for_shaping(rewritten) == expected

    # Mixed chain with git and pytest
    multi_cmd = (
        'git pull origin main && '
        'sqlite3 db.sqlite "SELECT 1" && '
        'psql -c "SELECT 2" && '
        'pytest tools/test_output_shaper.py'
    )
    rewritten_multi = output_shaper.rewrite_command_for_shaping(multi_cmd)
    assert "git pull -q origin main" in rewritten_multi
    assert 'sqlite3 -list db.sqlite "SELECT 1"' in rewritten_multi
    assert 'psql -A -P pager=off -c "SELECT 2"' in rewritten_multi
    assert "output_shaper.py --run -- pytest tools/test_output_shaper.py" in rewritten_multi
    assert output_shaper.rewrite_command_for_shaping(rewritten_multi) == rewritten_multi


def test_batch_guard_unscoped_test_and_overwrite():
    assert batch_guard.is_unscoped_pytest("pytest") is True
    assert batch_guard.is_unscoped_pytest("cd /workspace && pytest tests/") is True
    assert batch_guard.is_unscoped_pytest("go test ./...") is True
    assert batch_guard.is_unscoped_pytest("go test ./pkg/shaper/...") is False
    assert batch_guard.is_unscoped_pytest("pytest tools/test_output_shaper.py") is False

    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {
                "CommandLine": "ruff check tools/output_shaper.py && pytest tools/test_output_shaper.py",
                "WaitMsBeforeAsync": 5000,
            },
        },
        "cwd": "/workspace",
    }
    res = subprocess.run(
        [sys.executable, "/workspace/.agents/hooks/batch_guard.py"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    resp = json.loads(res.stdout)
    assert resp["decision"] == "allow"
    assert resp["overwrite"]["WaitMsBeforeAsync"] == 10000
    assert "output_shaper.py --run -- ruff check" in resp["overwrite"]["CommandLine"]
    assert "output_shaper.py --run -- pytest" in resp["overwrite"]["CommandLine"]


def test_shape_log_tail_extracts_traceback():
    raw_log = """--- [Detached Runner] Started at 2026-10-24T22:48:49Z ---
--- Command: python3 -u /workspace/scratch/calibrate.py ---
Step 1: loading...
tools/test_foo.py::test_a PASSED
Traceback (most recent call last):
  File "/workspace/scratch/calibrate.py", line 28, in <module>
    suite = pickle.load(f)
AttributeError: Can't get attribute 'BIGModelSuite'
"""
    shaped = output_shaper.shape_log_tail(raw_log, max_lines=20)
    assert "--- [Detached Runner]" not in shaped
    assert "test_a PASSED" not in shaped
    assert "Traceback (most recent call last):" in shaped
    assert "AttributeError: Can't get attribute 'BIGModelSuite'" in shaped


if __name__ == "__main__":
    test_merge_line_ranges()
    test_shape_pytest_pass_collapses_noise()
    test_shape_pytest_failure_keeps_only_failures()
    test_shape_coverage_output_and_ast_functions()
    test_run_with_temp_log_preserves_exit_code()
    test_rewrite_command_for_shaping_and_git_quiet()
    test_rewrite_command_sqlite3_compact()
    test_rewrite_command_psql_compact()
    test_rewrite_command_compound_db_chains()
    test_batch_guard_unscoped_test_and_overwrite()
    test_shape_log_tail_extracts_traceback()
    print("All output_shaper tests passed.")
