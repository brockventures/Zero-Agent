#!/usr/bin/env python3
"""
test_forge_goldfish.py — Unit tests for FORGE Goldfish Plan Pre-Flight & Blind Diff Checker.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.forge import (
    detect_skill_and_rule_drift,
    format_origin_completion_envelope,
    get_db_connection,
    grade_chunk_diff_blind,
    init_forge_db,
    run_goldfish_plan_preflight,
    validate_forge_plan,
    verify_chunk_worktree,
)


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with default user and initial commit."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True, capture_output=True)
    init_file = path / "README.md"
    init_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True, capture_output=True)


def _make_sample_plan(tmp_path: Path) -> dict:
    """Return a baseline valid ForgePlan dictionary targeting tmp_path."""
    sample_py = tmp_path / "module.py"
    sample_py.write_text(
        "def existing_function():\n"
        "    return 42\n\n"
        "class ExistingClass:\n"
        "    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "module.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module.py"], cwd=str(tmp_path), check=True, capture_output=True)

    return {
        "title": "Sample Goldfish Test Plan",
        "origin_channel_id": "1562899941595160657",
        "repo": str(tmp_path),
        "context_fact_sheet": ["Invariant 1: deterministic testing"],
        "chunks": [
            {
                "title": "Chunk 0: Schema Setup",
                "target_files": ["module.py"],
                "target_symbols": ["module.py:existing_function"],
                "instructions": "Update existing_function to handle edge cases properly.",
            },
            {
                "title": "Chunk 1: Logic Implementation",
                "target_files": ["module.py"],
                "target_symbols": ["module.py:ExistingClass"],
                "depends_on": [0],
                "instructions": "Extend ExistingClass with new methods.",
            },
        ],
    }


# ==============================================================================
# 1. run_goldfish_plan_preflight Tests
# ==============================================================================

def test_goldfish_preflight_valid_plan(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is True
    assert findings == []


def test_goldfish_preflight_missing_symbol_rejected(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][0]["target_symbols"] = ["module.py:nonexistent_symbol_foo"]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("nonexistent_symbol_foo" in f for f in findings)


def test_goldfish_preflight_new_symbol_in_instructions_allowed(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    # Symbol doesn't exist yet, but chunk instructions state to implement/create it
    plan["chunks"][0]["target_symbols"] = ["module.py:brand_new_helper"]
    plan["chunks"][0]["instructions"] = "In module.py, implement brand_new_helper() to calculate ratios."
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is True
    assert findings == []


def test_goldfish_preflight_line_range_symbol_allowed(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][0]["target_symbols"] = ["module.py:1-4"]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is True
    assert findings == []


def test_goldfish_preflight_nonexistent_file_skipped(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    # File doesn't exist in repo yet, so symbol check is skipped per spec
    plan["chunks"][0]["target_symbols"] = ["new_file_not_yet_created.py:any_func"]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is True
    assert findings == []


def test_goldfish_preflight_depends_on_out_of_range(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][1]["depends_on"] = [99]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("out of range" in f for f in findings)


def test_goldfish_preflight_depends_on_non_integer(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][1]["depends_on"] = ["chunk_invalid"]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("invalid non-integer" in f for f in findings)


def test_goldfish_preflight_self_dependency_rejected(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][0]["depends_on"] = [0]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("depends on itself" in f for f in findings)


def test_goldfish_preflight_circular_dependency_rejected(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    # Chunk 0 depends on Chunk 1, and Chunk 1 depends on Chunk 0
    plan["chunks"][0]["depends_on"] = [1]
    plan["chunks"][1]["depends_on"] = [0]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("Circular chunk dependency" in f for f in findings)


def test_goldfish_preflight_multi_hop_circular_dependency(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"].append({
        "title": "Chunk 2: Integration",
        "target_files": ["module.py"],
        "instructions": "Final integration and tests.",
        "depends_on": [0],
    })
    # 0 -> 1 -> 2 -> 0
    plan["chunks"][0]["depends_on"] = [1]
    plan["chunks"][1]["depends_on"] = [2]
    plan["chunks"][2]["depends_on"] = [0]
    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("Circular chunk dependency" in f for f in findings)


# ==============================================================================
# 2. validate_forge_plan Pre-Flight Integration Tests
# ==============================================================================

def test_validate_forge_plan_passes_with_preflight(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    valid, msg = validate_forge_plan(plan)
    assert valid is True
    assert msg == "Valid ForgePlan."


def test_validate_forge_plan_rejects_missing_symbol(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][0]["target_symbols"] = ["module.py:ghost_symbol"]
    valid, msg = validate_forge_plan(plan)
    assert valid is False
    assert "Goldfish Plan Pre-Flight Gate failed" in msg
    assert "ghost_symbol" in msg


def test_validate_forge_plan_skips_preflight_when_flagged(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    plan = _make_sample_plan(tmp_path)
    plan["chunks"][0]["target_symbols"] = ["module.py:ghost_symbol"]
    plan["skip_goldfish_preflight"] = True
    valid, msg = validate_forge_plan(plan)
    assert valid is True
    assert msg == "Valid ForgePlan."


# ==============================================================================
# 3. grade_chunk_diff_blind Tests
# ==============================================================================

def test_grade_chunk_diff_blind_clean_diff(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("x = 10\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    # Make a clean substantive edit
    py_file.write_text("x = 20\ndef get_val():\n    return x\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is True
    assert "Diff grading passed clean" in msg


def test_grade_chunk_diff_blind_zero_changes_require_changes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is False
    assert "Worker produced zero file changes" in msg


def test_grade_chunk_diff_blind_zero_changes_not_required(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=False)
    assert passed is True
    assert "Diff grading passed clean" in msg


def test_grade_chunk_diff_blind_comment_only_rejected_when_required(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    # Edit app.py with only comments and blank lines
    py_file.write_text("x = 1\n\n# Just an added comment\n    # Another comment\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is False
    assert "only comments or blank lines" in msg


def test_grade_chunk_diff_blind_comment_only_allowed_when_changes_not_required(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    py_file.write_text("x = 1\n# Comment only\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=False)
    assert passed is True
    assert "Diff grading passed clean" in msg


def test_grade_chunk_diff_blind_rejects_conflict_markers(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("val = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    # Dynamically compose conflict marker strings so this test file's diff never contains them
    c_start = "<" * 7 + " HEAD"
    c_end = ">" * 7 + " feature"
    py_file.write_text(f"{c_start}\nval = 2\n=======\nval = 3\n{c_end}\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is False
    assert "merge conflict marker" in msg


def test_grade_chunk_diff_blind_rejects_breakpoint_trap(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    # Dynamically compose breakpoint trap string
    bp_call = "break" + "point()"
    py_file.write_text(f"x = 1\n{bp_call}\nx = 2\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is False
    assert "interactive debugger trap 'breakpoint()'" in msg


def test_grade_chunk_diff_blind_rejects_pdb_trap(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    py_file.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add app.py"], cwd=str(tmp_path), check=True, capture_output=True)

    # Dynamically compose pdb trap string
    pdb_call = "pdb.set_" + "trace()"
    py_file.write_text(f"import pdb; {pdb_call}\nx = 2\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is False
    assert "interactive debugger trap 'pdb.set_trace()'" in msg


def test_grade_chunk_diff_blind_removing_breakpoint_is_allowed(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    py_file = tmp_path / "app.py"
    bp_call = "break" + "point()"
    py_file.write_text(f"{bp_call}\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial with bp"], cwd=str(tmp_path), check=True, capture_output=True)

    # Remove the breakpoint and add real code
    py_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

    passed, msg = grade_chunk_diff_blind(tmp_path, require_changes=True)
    assert passed is True
    assert "Diff grading passed clean" in msg


def test_grade_chunk_diff_blind_with_target_files_scope(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    file1 = tmp_path / "file1.py"
    file2 = tmp_path / "file2.py"
    file1.write_text("a = 1\n", encoding="utf-8")
    file2.write_text("b = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "file1.py", "file2.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add files"], cwd=str(tmp_path), check=True, capture_output=True)

    # Make substantive change in file1, comment in file2
    file1.write_text("a = 2\n", encoding="utf-8")
    file2.write_text("b = 1\n# comment\n", encoding="utf-8")

    # Scoped to file1 -> passes
    passed1, msg1 = grade_chunk_diff_blind(tmp_path, target_files=["file1.py"], require_changes=True)
    assert passed1 is True

    # Scoped to file2 -> fails because file2 only has comments
    passed2, msg2 = grade_chunk_diff_blind(tmp_path, target_files=["file2.py"], require_changes=True)
    assert passed2 is False
    assert "only comments or blank lines" in msg2


# ==============================================================================
# 4. verify_chunk_worktree Integration Tests
# ==============================================================================

def test_verify_chunk_worktree_blind_diff_integration(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    test_py = tmp_path / "code.py"
    test_py.write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init code"], cwd=str(tmp_path), check=True, capture_output=True)

    # 1. Clean modification
    test_py.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    passed, diff_stat, err = verify_chunk_worktree(
        tmp_path,
        verification_cmd="python3 -m py_compile code.py",
        require_changes=True,
    )
    assert passed is True
    assert err == ""

    # 2. Modify with a debugger trap -> verify_chunk_worktree rejects before/during verification
    bp_call = "break" + "point()"
    test_py.write_text(f"def hello():\n    {bp_call}\n    return 'world'\n", encoding="utf-8")
    passed, diff_stat, err = verify_chunk_worktree(
        tmp_path,
        verification_cmd="python3 -m py_compile code.py",
        require_changes=True,
    )
    assert passed is False
    assert "Blind diff grading failed" in err
    assert "breakpoint()" in err


# ==============================================================================
# 5. detect_skill_and_rule_drift Tests
# ==============================================================================

def test_drift_audit_clean_match(tmp_path: Path) -> None:
    """A repo with a skill citing valid CLI flags and defined functions reports 0 drift warnings."""
    # Setup skill
    sk_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n# Demo Skill\n"
        "Run `tools/demo.py` via:\n"
        "```bash\n"
        "python3 /workspace/tools/demo.py --count 5\n"
        "```\n"
        "Call `calculate_score(data)` to retrieve results.\n",
        encoding="utf-8",
    )

    # Setup rule
    rule_dir = tmp_path / ".agents" / "rules"
    rule_dir.mkdir(parents=True)
    (rule_dir / "demo_rule.md").write_text(
        "# Demo Rule\n"
        "Enforces `tools/demo.py:calculate_score` usage.\n",
        encoding="utf-8",
    )

    # Setup target file
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "demo.py").write_text(
        "import sys\n\n"
        "def calculate_score(data):\n"
        "    return len(data)\n\n"
        "if '--count' in sys.argv:\n"
        "    pass\n",
        encoding="utf-8",
    )

    warnings = detect_skill_and_rule_drift(tmp_path, ["tools/demo.py"])
    assert warnings == []


def test_drift_audit_stale_cli_flag(tmp_path: Path) -> None:
    """A skill citing a removed/stale CLI flag triggers a drift warning."""
    sk_dir = tmp_path / ".agents" / "skills" / "demo-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\n---\n# Demo Skill\n"
        "Run `tools/demo.py`:\n"
        "```bash\n"
        "python3 /workspace/tools/demo.py --stale-flag\n"
        "```\n",
        encoding="utf-8",
    )

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "demo.py").write_text(
        "def main():\n    pass\n",
        encoding="utf-8",
    )

    warnings = detect_skill_and_rule_drift(tmp_path, ["tools/demo.py"])
    assert len(warnings) == 1
    assert "SKILL.md (demo-skill) references removed flag/symbol --stale-flag in tools/demo.py" in warnings[0]


def test_drift_audit_stale_python_function(tmp_path: Path) -> None:
    """A skill citing a removed Python function triggers a drift warning."""
    sk_dir = tmp_path / ".agents" / "skills" / "math-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: math-skill\n---\n# Math Skill\n"
        "In `tools/math_tool.py`, call `stale_helper_func(items)` to sum.\n",
        encoding="utf-8",
    )

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "math_tool.py").write_text(
        "def new_helper(items):\n    return sum(items)\n",
        encoding="utf-8",
    )

    warnings = detect_skill_and_rule_drift(tmp_path, ["tools/math_tool.py"])
    assert len(warnings) == 1
    assert "SKILL.md (math-skill) references removed flag/symbol stale_helper_func in tools/math_tool.py" in warnings[0]


def test_drift_audit_rule_drift(tmp_path: Path) -> None:
    """A rule document citing a stale symbol and removed CLI flag triggers drift warnings."""
    rule_dir = tmp_path / ".agents" / "rules"
    rule_dir.mkdir(parents=True)
    (rule_dir / "strict_rule.md").write_text(
        "# Strict Rule\n"
        "Reference `tools/ops.py:vanished_symbol` or run `tools/ops.py --removed-flag`.\n",
        encoding="utf-8",
    )

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "ops.py").write_text(
        "def remaining_symbol():\n    pass\n",
        encoding="utf-8",
    )

    warnings = detect_skill_and_rule_drift(tmp_path, ["tools/ops.py"])
    assert len(warnings) == 2
    assert any("strict_rule.md references removed flag/symbol vanished_symbol in tools/ops.py" in w for w in warnings)
    assert any("strict_rule.md references removed flag/symbol --removed-flag in tools/ops.py" in w for w in warnings)


def test_drift_audit_ignores_test_files(tmp_path: Path) -> None:
    """Target test files are ignored by drift audit so unit test edits don't trigger false alarms."""
    sk_dir = tmp_path / ".agents" / "skills" / "test-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "# Test Skill\n"
        "Inspects `tools/test_ops.py` for `test_old_case()`.\n",
        encoding="utf-8",
    )

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "test_ops.py").write_text("def test_new_case(): pass\n", encoding="utf-8")

    warnings = detect_skill_and_rule_drift(tmp_path, ["tools/test_ops.py"])
    assert warnings == []


def test_drift_audit_nonexistent_repo(tmp_path: Path) -> None:
    """Non-existent repo directory gracefully returns empty warning list."""
    warnings = detect_skill_and_rule_drift(tmp_path / "nonexistent", ["tools/demo.py"])
    assert warnings == []


# ==============================================================================
# 6. format_origin_completion_envelope Drift Section Tests
# ==============================================================================

def test_format_origin_completion_envelope_with_drift_warnings(tmp_path: Path) -> None:
    """Completion envelope appends a bulleted drift warnings section when drift is detected."""
    db_path = tmp_path / "forge.db"
    init_forge_db(db_path)

    # Setup repo with skill containing stale flag
    sk_dir = tmp_path / ".agents" / "skills" / "drift-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: drift-skill\n---\n# Drift Skill\n"
        "Run `tools/api.py`:\n"
        "```bash\n"
        "python3 /workspace/tools/api.py --deleted-flag\n"
        "```\n",
        encoding="utf-8",
    )
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "api.py").write_text("def run(): pass\n", encoding="utf-8")

    build_id = "fg-drift-test-01"
    now = time.time()
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (id, title, repo, origin_channel_id, fact_sheet_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (build_id, "Drift Test Build", str(tmp_path), 12345, json.dumps(["Context Fact 1"]), now, now),
        )
        conn.execute(
            """
            INSERT INTO forge_chunks (id, build_id, chunk_index, title, repo, status, target_files_json, instructions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"{build_id}-c0", build_id, 0, "Chunk 0", str(tmp_path), "merged", json.dumps(["tools/api.py"]), "Run tasks"),
        )

    envelope = format_origin_completion_envelope(build_id, db_path=db_path)
    assert "### Skill & Rule Drift Warnings:" in envelope
    assert "SKILL.md (drift-skill) references removed flag/symbol --deleted-flag in tools/api.py" in envelope
    assert "Original Context Fact Sheet:" in envelope


def test_format_origin_completion_envelope_clean_no_warnings(tmp_path: Path) -> None:
    """Completion envelope omits drift warnings section when no drift is detected."""
    db_path = tmp_path / "forge.db"
    init_forge_db(db_path)

    # Setup clean repo
    sk_dir = tmp_path / ".agents" / "skills" / "clean-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: clean-skill\n---\n# Clean Skill\n"
        "Run `tools/api.py` with `execute_api()`.\n",
        encoding="utf-8",
    )
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "api.py").write_text("def execute_api(): pass\n", encoding="utf-8")

    build_id = "fg-clean-test-02"
    now = time.time()
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (id, title, repo, origin_channel_id, fact_sheet_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (build_id, "Clean Test Build", str(tmp_path), 12345, json.dumps(["Clean Fact"]), now, now),
        )
        conn.execute(
            """
            INSERT INTO forge_chunks (id, build_id, chunk_index, title, repo, status, target_files_json, instructions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"{build_id}-c0", build_id, 0, "Chunk 0", str(tmp_path), "merged", json.dumps(["tools/api.py"]), "Run tasks"),
        )

    envelope = format_origin_completion_envelope(build_id, db_path=db_path)
    assert "### Skill & Rule Drift Warnings:" not in envelope
    assert "Original Context Fact Sheet:" in envelope

