#!/usr/bin/env python3
"""
Deterministic Tool Output Shaper (output_shaper.py)
Implements Crab Cavern / Aerial & Arcane output-shaping patterns for Antigravity (agy):
1. Temp-Log Runner (--run -- <cmd>):
   Logs stdout/stderr to a temp .log/.status pair so passing test/linter suites collapse
   to a single summary line while preserving exact non-zero exit codes across && chains.
2. Pytest & Go Test Shaper:
   - 100% Pass: Collapses all per-test PASSED lines, platform headers, and warnings to 1 line.
   - Failures: Strips passing test noise and emits strictly failing test blocks, bounded
     tracebacks (head + assertion tail), and short test summary info.
3. Coverage Deficit, AST Top-5 Uncovered Functions & Merged Lstart-Lend Line Ranges:
   Suppresses 100% covered files, emitting strictly statement deficit, merged Lstart-Lend
   missing line spans, and the top 5 uncovered functions (via Python AST inspection),
   capped at 10 files.
4. Pre-Wake Failure Log Tail Shaper (--log <path>):
   Extracts tracebacks, error blocks, and final summary lines from detached job logs so
   agents never burn a tool roundtrip reading full log files on wakeup.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile

SHAPER_SCRIPT_PATH = (
    "/workspace/tools/output_shaper.py"
    if os.path.exists("/workspace/tools/output_shaper.py") or not os.path.exists("/opt/zero/workspace/tools/output_shaper.py")
    else "/opt/zero/workspace/tools/output_shaper.py"
)

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Coverage table row: Name  Stmts  Miss  [Branch BrPart]  Cover  [Missing]
_COV_ROW_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_./\\-]+?\.(?:py|go|ts|tsx|js|jsx))\s+"
    r"(?P<stmts>\d+)\s+"
    r"(?P<miss>\d+)\s+"
    r"(?:(?:\d+\s+\d+\s+)?)?"
    r"(?P<cover>\d+)%"
    r"(?:\s+(?P<missing>[0-9,\s\-–>exitpart]+))?\s*$"
)

_COV_TOTAL_RE = re.compile(
    r"^\s*TOTAL\s+(?P<stmts>\d+)\s+(?P<miss>\d+)\s+(?:(?:\d+\s+\d+\s+)?)?(?P<cover>\d+)%"
)

_PYTEST_SUMMARY_RE = re.compile(
    r"=+\s*(?P<summary>\d+\s+(?:passed|failed|skipped|deselected|xfailed|xpassed|warnings?|errors?)[^=]*?)\s*=+"
)


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def parse_missing_lines(missing_str: str) -> list[int]:
    """Parse coverage.py 'Missing' column (e.g. '12, 15-18, 22->exit') into sorted unique line ints."""
    if not missing_str:
        return []
    nums: set[int] = set()
    for token in re.split(r"[,\s]+", missing_str.strip()):
        tok = token.strip()
        if not tok:
            continue
        # Handle branch notation like 22->25 or 22->exit
        if "->" in tok:
            left = tok.split("->", 1)[0].strip()
            if left.isdigit():
                nums.add(int(left))
            continue
        m_range = re.match(r"^(\d+)[-–](\d+)$", tok)
        if m_range:
            start_n, end_n = int(m_range.group(1)), int(m_range.group(2))
            if start_n <= end_n and (end_n - start_n) <= 5000:
                nums.update(range(start_n, end_n + 1))
            continue
        if tok.isdigit():
            nums.add(int(tok))
    return sorted(nums)


def format_merged_line_ranges(lines: list[int] | str, max_ranges: int = 8) -> str:
    """Merge sorted line numbers into compact 'Lstart-Lend' ranges (e.g. 'L12, L15-L19, L25')."""
    parsed = parse_missing_lines(lines) if isinstance(lines, str) else sorted(set(lines))
    if not parsed:
        return ""
    ranges: list[tuple[int, int]] = []
    start = prev = parsed[0]
    for n in parsed[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append((start, prev))
            start = prev = n
    ranges.append((start, prev))

    formatted = [
        f"L{s}" if s == e else f"L{s}-L{e}"
        for s, e in ranges[:max_ranges]
    ]
    if len(ranges) > max_ranges:
        formatted.append(f"(+{len(ranges) - max_ranges} more)")
    return ", ".join(formatted)


def find_top_uncovered_functions(
    file_path: str,
    missing_lines: set[int] | list[int],
    cwd: str = "/workspace",
    top_n: int = 5,
) -> list[tuple[str, int]]:
    """
    Inspect Python AST of `file_path` to identify the top N functions/methods containing
    the most uncovered lines. Returns list of (qualname, missed_line_count).
    """
    missing_set = set(missing_lines)
    if not missing_set or not file_path.endswith(".py"):
        return []

    candidates = [
        os.path.join(cwd or "/workspace", file_path),
        os.path.join("/workspace", file_path),
        os.path.join("/baseball/big-projections", file_path),
        file_path,
    ]
    resolved = next((p for p in candidates if os.path.isfile(p)), None)
    if not resolved:
        return []

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=resolved)
    except Exception:
        return []

    func_spans: list[tuple[str, int, int]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _record_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qual = ".".join(self.scope + [node.name]) if self.scope else node.name
            start_l = getattr(node, "lineno", 0)
            end_l = getattr(node, "end_lineno", start_l) or start_l
            func_spans.append((qual, start_l, end_l))
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record_func(node)

    _Visitor().visit(tree)
    if not func_spans:
        return []

    # Assign each missing line to the innermost enclosing function
    func_spans.sort(key=lambda item: (item[2] - item[1], item[1]))
    counts: dict[str, int] = {}
    for line_no in sorted(missing_set):
        for qual, start_l, end_l in func_spans:
            if start_l <= line_no <= end_l:
                counts[qual] = counts.get(qual, 0) + 1
                break

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top_n]


def shape_coverage_output(
    text: str,
    threshold: float = 100.0,
    max_files: int = 10,
    cwd: str = "/workspace",
) -> str:
    """
    Filter coverage report tables to suppress 100% covered files (or files >= threshold)
    and emit strictly statement deficit, merged Lstart-Lend ranges, and top 5 uncovered functions
    capped at `max_files` files.
    """
    clean = strip_ansi(text)
    rows: list[dict] = []
    total_info: dict | None = None

    for line in clean.splitlines():
        m_tot = _COV_TOTAL_RE.match(line)
        if m_tot:
            total_info = {
                "stmts": int(m_tot.group("stmts")),
                "miss": int(m_tot.group("miss")),
                "cover": int(m_tot.group("cover")),
            }
            continue
        m_row = _COV_ROW_RE.match(line)
        if m_row:
            stmts = int(m_row.group("stmts"))
            miss = int(m_row.group("miss"))
            cover = int(m_row.group("cover"))
            missing_raw = (m_row.group("missing") or "").strip()
            rows.append(
                {
                    "name": m_row.group("name"),
                    "stmts": stmts,
                    "miss": miss,
                    "cover": cover,
                    "missing_raw": missing_raw,
                }
            )

    if not rows and not total_info:
        return text.strip()

    if total_info is None and rows:
        tot_stmts = sum(r["stmts"] for r in rows)
        tot_miss = sum(r["miss"] for r in rows)
        tot_cov = round(100.0 * (tot_stmts - tot_miss) / tot_stmts) if tot_stmts else 100
        total_info = {"stmts": tot_stmts, "miss": tot_miss, "cover": int(tot_cov)}

    below = [r for r in rows if r["cover"] < threshold and r["miss"] > 0]
    below.sort(key=lambda r: (-r["miss"], r["cover"], r["name"]))

    tot_s = total_info["stmts"] if total_info else 0
    tot_m = total_info["miss"] if total_info else 0
    tot_c = total_info["cover"] if total_info else 100

    if not below:
        return f"✅ coverage: TOTAL {tot_c}% ({tot_s} stmts, {tot_m} missed — all {len(rows)} file(s) >= {threshold:g}%)"

    out_lines = [
        f"📉 coverage: TOTAL {tot_c}% ({tot_s} stmts, {tot_m} missed) — {len(below)}/{len(rows)} file(s) below {threshold:g}%:"
    ]
    for r in below[:max_files]:
        missing_nums = parse_missing_lines(r["missing_raw"])
        ranges_str = format_merged_line_ranges(missing_nums)
        top_funcs = find_top_uncovered_functions(r["name"], set(missing_nums), cwd=cwd, top_n=5)
        parts = [f"  - {r['name']}: {r['cover']}% (deficit: {r['miss']} stmts)"]
        if ranges_str:
            parts.append(f"Ranges: {ranges_str}")
        if top_funcs:
            fn_str = ", ".join(f"{fn} ({cnt})" for fn, cnt in top_funcs)
            parts.append(f"Top uncovered: {fn_str}")
        out_lines.append(" | ".join(parts))

    if len(below) > max_files:
        out_lines.append(f"  ... +{len(below) - max_files} additional file(s) omitted (capped at {max_files})")
    return "\n".join(out_lines)


def _truncate_failure_block(block_lines: list[str], max_lines: int = 35) -> list[str]:
    """Keep the test header, entry frames, and final assertion tail of a failing test block."""
    if len(block_lines) <= max_lines:
        return block_lines
    head_count = max(8, max_lines // 3)
    tail_count = max(12, max_lines - head_count - 1)
    omitted = len(block_lines) - head_count - tail_count
    return (
        block_lines[:head_count]
        + [f"    ... [{omitted} intermediate traceback lines omitted by output_shaper] ..."]
        + block_lines[-tail_count:]
    )


def shape_pytest_output(
    text: str,
    returncode: int = 0,
    max_tb_lines: int = 35,
    cwd: str = "/workspace",
) -> str:
    """
    Shape pytest stdout/stderr:
    - 100% Pass (returncode == 0): Collapse all per-test noise & headers to a single summary line
      (plus shaped coverage summary if pytest-cov output is present).
    - Failure (returncode != 0): Strip passing test lines and session headers; emit only
      FAILURES / ERRORS blocks with bounded tracebacks and the final summary line.
    """
    clean = strip_ansi(text)
    if not clean.strip():
        return "✅ pytest: passed (0 output)" if returncode == 0 else f"❌ pytest failed (exit code {returncode})"

    # Split out embedded coverage table if present
    cov_section = ""
    main_text = clean
    cov_Split = re.split(r"^-{5,}\s*coverage:.*$", clean, maxsplit=1, flags=re.MULTILINE)
    if len(cov_Split) == 2:
        main_text = cov_Split[0]
        cov_section = shape_coverage_output(cov_Split[1], cwd=cwd)

    summaries = _PYTEST_SUMMARY_RE.findall(main_text)
    summary_str = summaries[-1].strip() if summaries else ""
    if not summary_str:
        # Check quiet mode summary e.g. "42 passed in 0.18s"
        for line in reversed(main_text.splitlines()):
            s_line = line.strip()
            if re.match(r"^\d+\s+(?:passed|failed|skipped|error)", s_line):
                summary_str = s_line
                break

    if returncode == 0:
        base_line = f"✅ pytest: {summary_str}" if summary_str else "✅ pytest: passed"
        if cov_section:
            return f"{base_line}\n{cov_section}"
        return base_line

    # Non-zero exit code: extract FAILURES / ERRORS / short test summary
    lines = main_text.splitlines()
    out: list[str] = []
    in_failure_section = False
    in_short_summary = False
    current_block: list[str] = []

    def _flush_block() -> None:
        nonlocal current_block
        if current_block:
            out.extend(_truncate_failure_block(current_block, max_lines=max_tb_lines))
            current_block = []

    for line in lines:
        stripped = line.strip()
        if re.match(r"^=+\s*(?:FAILURES|ERRORS)\s*=+$", stripped):
            _flush_block()
            in_failure_section = True
            in_short_summary = False
            out.append(stripped)
            continue
        if re.match(r"^=+\s*short test summary info\s*=+$", stripped):
            _flush_block()
            in_failure_section = False
            in_short_summary = True
            out.append(stripped)
            continue
        if re.match(r"^=+\s*warnings summary\s*=+$", stripped):
            _flush_block()
            in_failure_section = False
            in_short_summary = False
            continue
        if _PYTEST_SUMMARY_RE.match(stripped):
            _flush_block()
            in_failure_section = False
            in_short_summary = False
            out.append(f"❌ pytest: {stripped.strip('=').strip()}")
            continue

        if in_failure_section:
            if re.match(r"^_{5,}\s+.*\s+_{5,}$", stripped):
                _flush_block()
            current_block.append(line.rstrip())
        elif in_short_summary:
            if stripped.startswith(("FAILED ", "ERROR ")):
                out.append(line.rstrip())

    _flush_block()

    if not out:
        # Collection / syntax / CLI error before test execution
        tail_lines = [
            ln.rstrip()
            for ln in lines
            if ln.strip()
            and not ln.strip().startswith(("platform ", "rootdir:", "plugins:", "cachedir:"))
            and not re.match(r"^=+\s*test session starts\s*=+$", ln.strip())
        ]
        out = tail_lines[-max_tb_lines:]
        if summary_str and (not out or summary_str not in out[-1]):
            out.append(f"❌ pytest: {summary_str}")

    if cov_section:
        out.append(cov_section)
    return "\n".join(out).strip()


def shape_go_test_output(text: str, returncode: int = 0, max_fail_lines: int = 60) -> str:
    """Shape `go test` output: collapse passing package summaries; emit only FAIL blocks on error."""
    clean = strip_ansi(text)
    lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
    if returncode == 0:
        ok_pkgs = [ln for ln in lines if ln.startswith("ok ") or ln.startswith("ok\t")]
        count = len(ok_pkgs) if ok_pkgs else 1
        return f"✅ go test: {count} package(s) passed"

    fail_lines: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(("ok  \t", "ok \t", "=== RUN   ", "=== PAUSE ", "=== CONT  ", "--- PASS:")):
            continue
        fail_lines.append(ln)

    if len(fail_lines) > max_fail_lines:
        fail_lines = (
            fail_lines[:20]
            + [f"... [{len(fail_lines) - max_fail_lines} lines omitted by output_shaper] ..."]
            + fail_lines[-(max_fail_lines - 20):]
        )
    return "\n".join(fail_lines).strip() or f"❌ go test failed (exit code {returncode})"


def shape_ruff_output(text: str, returncode: int = 0, max_lines: int = 25) -> str:
    """Shape `ruff check` output: 1-line summary on clean pass, capped diagnostics on failure."""
    clean = strip_ansi(text).strip()
    if returncode == 0:
        return "✅ ruff: clean"
    lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} more ruff lines omitted)"]
    return "\n".join(lines)


def shape_unittest_output(text: str, returncode: int = 0, max_lines: int = 40) -> str:
    """Shape Python unittest output: collapse passing runs to 1 line, keep tracebacks on failure."""
    clean = strip_ansi(text).strip()
    if returncode == 0:
        m = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+s)", clean)
        if m:
            return f"✅ unittest: {m.group(1)} passed in {m.group(2)}"
        return "✅ unittest: passed"
    lines = [ln.rstrip() for ln in clean.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def shape_log_tail(text_or_path: str, max_lines: int = 35) -> str:
    """
    Pre-Wake Log Shaper: extract traceback blocks, ERROR/FAIL lines, and final summary
    metrics from detached job logs (`run.log`) without dumping Passing/Progress noise.
    """
    raw = text_or_path or ""
    if os.path.isfile(raw):
        try:
            with open(raw, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except Exception as e:
            return f"[output_shaper: unable to read log {raw}: {e}]"

    clean = strip_ansi(raw)
    filtered = [
        ln.rstrip()
        for ln in clean.splitlines()
        if ln.strip()
        and not ln.strip().startswith("--- [Detached Runner]")
        and not re.match(r"^[^\s]+::[^\s]+\s+PASSED\b", ln.strip())
    ]
    if not filtered:
        return "(empty log)"

    # Locate the last Python Traceback if present
    last_tb_idx = -1
    for i, ln in enumerate(filtered):
        if "Traceback (most recent call last):" in ln:
            last_tb_idx = i

    if last_tb_idx != -1:
        # Include up to 5 context lines before the traceback and the traceback itself
        start_idx = max(0, last_tb_idx - 5)
        slice_lines = filtered[start_idx:]
        if len(slice_lines) > max_lines:
            slice_lines = _truncate_failure_block(slice_lines, max_lines=max_lines)
        return "\n".join(slice_lines)

    if len(filtered) > max_lines:
        return "\n".join(filtered[-max_lines:])
    return "\n".join(filtered)


def detect_command_kind(argv: list[str]) -> str:
    """Classify command argv into 'pytest', 'go_test', 'coverage', 'ruff', 'unittest', or 'generic'."""
    if not argv:
        return "generic"
    joined = " ".join(argv)
    first = os.path.basename(argv[0])

    if first == "pytest" or (first.startswith("python") and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pytest"):
        return "pytest"
    if first == "go" and len(argv) >= 2 and argv[1] == "test":
        return "go_test"
    if (first == "coverage" and len(argv) >= 2 and argv[1] == "report") or (
        first.startswith("python") and len(argv) >= 4 and argv[1] == "-m" and argv[2] == "coverage" and argv[3] == "report"
    ):
        return "coverage"
    if first == "ruff" or (first.startswith("python") and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "ruff"):
        return "ruff"
    if first.startswith("python") and len(argv) >= 3 and argv[1] == "-m" and argv[2] == "unittest":
        return "unittest"
    if first.startswith("python") and any(os.path.basename(a).startswith("test_") for a in argv[1:]):
        return "pytest_or_unittest"
    if "--cov" in joined and "pytest" in joined:
        return "pytest"
    return "generic"


def shape_output_by_kind(kind: str, raw_output: str, returncode: int, cwd: str = "/workspace") -> str:
    if kind == "pytest":
        return shape_pytest_output(raw_output, returncode=returncode, cwd=cwd)
    if kind == "go_test":
        return shape_go_test_output(raw_output, returncode=returncode)
    if kind == "coverage":
        return shape_coverage_output(raw_output, cwd=cwd)
    if kind == "ruff":
        return shape_ruff_output(raw_output, returncode=returncode)
    if kind == "unittest":
        return shape_unittest_output(raw_output, returncode=returncode)
    if kind == "pytest_or_unittest":
        if "test session starts" in raw_output or _PYTEST_SUMMARY_RE.search(raw_output):
            return shape_pytest_output(raw_output, returncode=returncode, cwd=cwd)
        return shape_unittest_output(raw_output, returncode=returncode)
    return raw_output.strip()


def run_with_temp_log(
    argv: list[str],
    cwd: str | None = None,
    keep_log: bool = False,
) -> tuple[int, str, str]:
    """
    Aerial's Temp-Log Runner Pattern:
    Execute `argv`, logging raw stdout/stderr to a temp `.log` file and exit code to `.status`.
    Passing suites collapse to a 1-line summary; failures emit shaped tracebacks and preserve
    the exact non-zero exit code so compound `&&` chains short-circuit cleanly.
    """
    work_dir = cwd or os.getcwd()
    kind = detect_command_kind(argv)

    fd, log_path = tempfile.mkstemp(prefix=f"shaper_{kind}_{os.getpid()}_", suffix=".log", dir="/tmp")
    status_path = f"{log_path}.status"
    try:
        with os.fdopen(fd, "w+", encoding="utf-8", errors="replace") as lf:
            proc = subprocess.run(
                argv,
                cwd=work_dir,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            rc = proc.returncode
            lf.flush()
            lf.seek(0)
            raw_output = lf.read()

        with open(status_path, "w", encoding="utf-8") as sf:
            sf.write(f"{rc}\n")

        shaped = shape_output_by_kind(kind, raw_output, returncode=rc, cwd=work_dir)
        return rc, shaped, log_path
    finally:
        if not keep_log:
            for p in (log_path, status_path):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass


def _split_top_level_chain(cmd: str) -> list[tuple[str, str]] | None:
    """
    Split a single-line shell command on top-level unquoted `&&`, `||`, or `;` operators.
    Returns list of `(segment, trailing_operator)` tuples, or None if the command contains
    multiline scripts, heredocs, subshells, or unclosed quotes.
    """
    if not cmd or "\n" in cmd or "<<" in cmd or "$(" in cmd or "`" in cmd:
        return None

    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    paren_depth = 0
    i = 0
    n = len(cmd)

    while i < n:
        ch = cmd[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if not in_single and not in_double:
            if ch in ("(", "{"):
                paren_depth += 1
            elif ch in (")", "}"):
                paren_depth -= 1
                if paren_depth < 0:
                    return None

            if paren_depth == 0:
                if cmd[i : i + 2] in ("&&", "||"):
                    op = cmd[i : i + 2]
                    segments.append(("".join(buf), op))
                    buf = []
                    i += 2
                    continue
                if ch == ";":
                    segments.append(("".join(buf), ";"))
                    buf = []
                    i += 1
                    continue

        buf.append(ch)
        i += 1

    if in_single or in_double or paren_depth != 0:
        return None

    segments.append(("".join(buf), ""))
    return segments


_ELIGIBLE_WRAP_RE = re.compile(
    r"^(?:"
    r"(?:pytest|python[0-9.]*\s+-m\s+pytest)\b|"
    r"go\s+test\b|"
    r"(?:coverage|python[0-9.]*\s+-m\s+coverage)\s+report\b|"
    r"(?:ruff|python[0-9.]*\s+-m\s+ruff)\s+check\b|"
    r"python[0-9.]*\s+-m\s+unittest\b"
    r")"
)

_GIT_QUIET_ELIGIBLE_RE = re.compile(
    r"^git\s+(?P<subcmd>fetch|pull|checkout|switch|rebase|merge)\b(?P<rest>.*)$"
)


def _segment_has_redirection_or_pipe(seg: str) -> bool:
    """Return True if an unquoted pipe `|`, redirection `>`/`<`, or background `&` exists in `seg`."""
    in_single = False
    in_double = False
    escaped = False
    for ch in seg:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double and ch in ("|", ">", "<", "&"):
            return True
    return False


def rewrite_command_for_shaping(cmd: str) -> str:
    """
    Inspect a shell command string and safely wrap eligible test/coverage/linter segments
    with `python3 /workspace/tools/output_shaper.py --run -- <segment>` and inject `-q`
    into noisy git operations when safe.
    Returns the rewritten command string (or the original `cmd` if no changes apply).
    """
    if not cmd or "output_shaper.py --run" in cmd or "output_shaper.py --log" in cmd:
        return cmd

    parsed = _split_top_level_chain(cmd)
    if not parsed:
        return cmd

    changed = False
    rebuilt: list[str] = []

    for raw_seg, op in parsed:
        stripped = raw_seg.strip()
        new_seg = raw_seg

        if stripped and not _segment_has_redirection_or_pipe(stripped):
            if _ELIGIBLE_WRAP_RE.match(stripped):
                new_seg = f"python3 {SHAPER_SCRIPT_PATH} --run -- {stripped}"
                changed = True
            else:
                m_git = _GIT_QUIET_ELIGIBLE_RE.match(stripped)
                if m_git:
                    subcmd = m_git.group("subcmd")
                    rest = m_git.group("rest") or ""
                    # Inject -q if not already present and not a help/version check
                    if (
                        not re.search(r"\s(?:-q|--quiet|--help|-h)\b", rest)
                        and "--" not in rest
                    ):
                        new_seg = f"git {subcmd} -q{rest}"
                        changed = True

        if op == ";":
            rebuilt.append(f"{new_seg.strip()}; ")
        elif op:
            rebuilt.append(f"{new_seg.strip()} {op} ")
        else:
            rebuilt.append(new_seg.strip() if changed else new_seg)

    if not changed:
        return cmd
    return "".join(rebuilt).strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: output_shaper.py [--run -- <cmd...>] | [--log <path> [--lines N]]", file=sys.stderr)
        return 1

    if args[0] == "--log":
        if len(args) < 2:
            print("Error: --log requires a file path", file=sys.stderr)
            return 1
        log_path = args[1]
        max_lines = 35
        if "--lines" in args:
            idx = args.index("--lines")
            if idx + 1 < len(args) and args[idx + 1].isdigit():
                max_lines = int(args[idx + 1])
        print(shape_log_tail(log_path, max_lines=max_lines))
        return 0

    keep_log = False
    if "--keep-log" in args:
        keep_log = True
        args.remove("--keep-log")

    if args[0] == "--run":
        cmd_args = args[2:] if len(args) >= 2 and args[1] == "--" else args[1:]
    elif "--" in args:
        sep_idx = args.index("--")
        cmd_args = args[sep_idx + 1 :]
    else:
        cmd_args = args

    if not cmd_args:
        print("Error: no command specified for --run", file=sys.stderr)
        return 1

    rc, shaped, _ = run_with_temp_log(cmd_args, keep_log=keep_log)
    if shaped:
        print(shaped)
    return rc


if __name__ == "__main__":
    sys.exit(main())
