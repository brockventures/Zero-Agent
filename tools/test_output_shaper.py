#!/usr/bin/env python3
"""
Unit tests for deterministic Tool Output Shaper (output_shaper.py) and batch_guard.py integration.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, "/workspace/tools")
sys.path.insert(0, "/workspace/.agents/hooks")

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
    test_batch_guard_unscoped_test_and_overwrite()
    test_shape_log_tail_extracts_traceback()
    print("All output_shaper tests passed.")
