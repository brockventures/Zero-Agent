#!/usr/bin/env python3
"""Path-gated Go verification and Modal serverless static analysis offload.

Prevents whole-program Go SSA static analyzers (`deadcode`, `golangci-lint`,
`oapi-codegen`) from consuming 2.0-2.5 GB RSS on Host 2 during frontend-only
FORGE chunks, and offloads heavy Go static analysis to Modal serverless
containers (with persistent Go build/module cache) or local memory-clamped
execution (`GOMEMLIMIT=1024MiB GOGC=50 GOMAXPROCS=2`).
"""

from __future__ import annotations

import fnmatch
import os
import re
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

SKIP_GO_ECHO_CMD = "echo '[modal_verify] Skipped Go verification (no .go/API files changed)'"

GO_CLAMP_DEFAULTS: dict[str, str] = {
    "GOMEMLIMIT": "1024MiB",
    "GOGC": "50",
    "GOMAXPROCS": "2",
    "OMP_NUM_THREADS": "2",
}

_GO_TRIGGER_EXACT_NAMES = {
    "go.mod",
    "go.sum",
    "go.work",
    "go.work.sum",
    "makefile",
    "dockerfile",
    ".golangci.yml",
    ".golangci.yaml",
    ".golangci.toml",
    ".golangci.json",
}

_GO_TRIGGER_GLOBS = (
    "*openapi*.yaml",
    "*openapi*.yml",
    "*openapi*.json",
    "*swagger*.yaml",
    "*swagger*.yml",
    "*swagger*.json",
    "*swagger*",
    "*oapi-codegen*",
)

_NON_GO_SAFE_EXTENSIONS = {
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".md",
    ".markdown",
    ".rst",
    ".txt",
    ".png",
    ".svg",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".py",
}

_GO_SUBCOMMAND_REGEX = re.compile(
    r"(?:^|[\s/\"'(\|&;])("
    r"deadcode\b"
    r"|golangci-lint\b"
    r"|oapi-codegen\b"
    r"|go\s+(?:vet|test|build|generate|run|tool)\b"
    r")"
)

_HEAVY_GO_ANALYSIS_REGEX = re.compile(
    r"(?:^|[\s/\"'(\|&;])("
    r"deadcode\b"
    r"|golangci-lint\b"
    r"|oapi-codegen\b"
    r"|go\s+test\s+[^\n;&|]*\./\.\.\."
    r")"
)


def _normalize_rel_path(path_str: str, cwd: Optional[str | Path] = None) -> str:
    """Normalize a file path string into a clean relative POSIX path."""
    raw = str(path_str or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[-1].strip()
    if cwd is not None:
        try:
            cwd_str = str(Path(cwd).resolve())
            if raw.startswith(cwd_str):
                raw = os.path.relpath(raw, cwd_str)
        except Exception:
            pass
    raw = raw.replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.strip("/")


def detect_changed_files_from_git(cwd: str | Path) -> list[str]:
    """Query `git status --porcelain` and `git diff --name-only HEAD` in `cwd`."""
    wt = Path(cwd)
    if not wt.exists():
        return []

    found: list[str] = []
    seen: set[str] = set()

    try:
        st_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if st_proc.returncode == 0:
            for line in st_proc.stdout.splitlines():
                if len(line) > 3:
                    entry = _normalize_rel_path(line[3:], cwd=wt)
                    if entry and entry not in seen:
                        seen.add(entry)
                        found.append(entry)
    except Exception:
        pass

    try:
        df_proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if df_proc.returncode == 0:
            for line in df_proc.stdout.splitlines():
                entry = _normalize_rel_path(line, cwd=wt)
                if entry and entry not in seen:
                    seen.add(entry)
                    found.append(entry)
    except Exception:
        pass

    return found


def is_go_or_api_trigger_file(file_path: str) -> bool:
    """Return True if `file_path` is a backend Go, build, or OpenAPI spec trigger file."""
    norm = _normalize_rel_path(file_path)
    if not norm:
        return False
    p = Path(norm)
    name_low = p.name.lower()
    norm_low = norm.lower()

    if name_low.endswith(".go") or name_low.endswith(".proto") or name_low.endswith(".sql"):
        return True
    if name_low in _GO_TRIGGER_EXACT_NAMES or name_low.startswith("dockerfile."):
        return True
    for pat in _GO_TRIGGER_GLOBS:
        if fnmatch.fnmatch(name_low, pat) or fnmatch.fnmatch(norm_low, pat):
            return True
    return False


def is_go_subcommand(segment: str) -> bool:
    """Return True if a command segment invokes a Go toolchain or Go static analyzer."""
    s = (segment or "").strip()
    if not s or s.startswith("echo "):
        return False
    return bool(_GO_SUBCOMMAND_REGEX.search(s))


def has_heavy_go_static_analysis(verify_cmd: str) -> bool:
    """Return True if `verify_cmd` contains active heavy Go whole-program static analysis."""
    cmd = (verify_cmd or "").strip()
    if not cmd:
        return False
    for seg, _ in _split_compound_command(cmd):
        s = seg.strip()
        if not s or s.startswith("echo "):
            continue
        if _HEAVY_GO_ANALYSIS_REGEX.search(s):
            return True
    return False


def should_run_go_verification(
    changed_files: Optional[list[str]],
    verify_cmd: str = "",
    cwd: Optional[str | Path] = None,
) -> bool:
    """Determine whether Go static analysis / verification should execute.

    Inspects `changed_files` (normalizing relative paths; if `changed_files` is None,
    queries `git status --porcelain` and `git diff --name-only HEAD` in `cwd`).
    Returns True when:
    - `changed_files` is empty/unknown (conservative fail-safe), OR
    - Any file in `changed_files` is a Go/API trigger file (`.go`, `go.mod`, `go.sum`,
      `Makefile`, `Dockerfile`, or `*openapi*` / `*swagger*`), OR
    - Any file is outside the known non-Go frontend/doc/asset extensions.
    Returns False when `changed_files` is non-empty and contains ONLY non-Go
    frontend/doc/asset files.
    """
    files = list(changed_files) if changed_files is not None else (
        detect_changed_files_from_git(cwd) if cwd is not None else []
    )
    normalized = [_normalize_rel_path(f, cwd=cwd) for f in files]
    normalized = [f for f in normalized if f]

    if not normalized:
        return True

    for f in normalized:
        if is_go_or_api_trigger_file(f):
            return True

    for f in normalized:
        ext = Path(f).suffix.lower()
        if ext not in _NON_GO_SAFE_EXTENSIONS:
            return True

    return False


def _split_compound_command(cmd: str) -> list[tuple[str, str]]:
    """Split a shell command on top-level `&&` and `;` operators while respecting quotes and parens.

    Returns a list of `(segment, trailing_operator)` tuples where `trailing_operator`
    is `'&&'`, `';'`, or `''` for the final segment.
    """
    results: list[tuple[str, str]] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escape = False
    paren_depth = 0
    i = 0
    n = len(cmd)

    while i < n:
        ch = cmd[i]
        if escape:
            buf.append(ch)
            escape = False
            i += 1
            continue

        if ch == "\\" and not in_single:
            buf.append(ch)
            escape = True
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
                buf.append(ch)
                i += 1
                continue
            if ch in (")", "}") and paren_depth > 0:
                paren_depth -= 1
                buf.append(ch)
                i += 1
                continue

            if paren_depth == 0:
                if cmd[i : i + 2] == "&&":
                    results.append(("".join(buf).strip(), "&&"))
                    buf = []
                    i += 2
                    continue
                if ch == ";":
                    results.append(("".join(buf).strip(), ";"))
                    buf = []
                    i += 1
                    continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail or results:
        results.append((tail, ""))
    return results


def filter_verify_cmd_for_changed_files(
    verify_cmd: str,
    changed_files: Optional[list[str]],
    cwd: Optional[str | Path] = None,
) -> str:
    """Filter `verify_cmd` based on `changed_files`.

    When `should_run_go_verification(changed_files, verify_cmd, cwd=cwd)` is False,
    replaces heavy Go subcommands (`deadcode`, `golangci-lint`, `oapi-codegen`,
    `go vet`, `go test`, `go build`) inside compound `&&` / `;` verification commands
    with `SKIP_GO_ECHO_CMD` while preserving any non-Go verification segments.
    Consecutive duplicate skip echoes are collapsed into a single echo segment.
    """
    raw_cmd = (verify_cmd or "").strip()
    if not raw_cmd:
        return raw_cmd

    if should_run_go_verification(changed_files, raw_cmd, cwd=cwd):
        return raw_cmd

    parts = _split_compound_command(raw_cmd)
    if not parts:
        return raw_cmd

    rebuilt: list[tuple[str, str]] = []
    replaced_any = False

    for seg, op in parts:
        if is_go_subcommand(seg):
            replaced_any = True
            if rebuilt and rebuilt[-1][0] == SKIP_GO_ECHO_CMD:
                # Update previous operator to current segment's operator so chain remains valid
                rebuilt[-1] = (SKIP_GO_ECHO_CMD, op)
            else:
                rebuilt.append((SKIP_GO_ECHO_CMD, op))
        else:
            rebuilt.append((seg, op))

    if not replaced_any:
        return raw_cmd

    out_pieces: list[str] = []
    for idx, (seg, op) in enumerate(rebuilt):
        out_pieces.append(seg)
        if idx < len(rebuilt) - 1:
            effective_op = op or "&&"
            if effective_op == ";":
                out_pieces.append("; ")
            else:
                out_pieces.append(f" {effective_op} ")

    return "".join(out_pieces).strip()


def get_clamped_verify_env(
    base_env: Optional[dict[str, str]] = None,
    verify_cmd: Optional[str] = None,
) -> dict[str, str]:
    """Return environment dictionary with Host 2 Go memory/CPU clamping variables injected.

    Sets `GOMEMLIMIT=1024MiB`, `GOGC=50`, `GOMAXPROCS=2`, and `OMP_NUM_THREADS=2`
    to prevent Go SSA analyzers (`deadcode`, `golangci-lint`, `go test`) from triggering
    cgroup OOM kills or NAS swap thrashing on Host 2.
    """
    env = dict(base_env) if base_env is not None else os.environ.copy()
    for k, v in GO_CLAMP_DEFAULTS.items():
        env.setdefault(k, v)
    return env


def _build_workspace_archive(cwd: Path) -> bytes:
    """Create a lightweight gzipped tar archive of `cwd` (excluding `.git`, `node_modules`, `.venv`, etc.)."""
    excluded_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build"}
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with tarfile.open(tmp_path, mode="w:gz") as tar:
            for root, dirs, files in os.walk(cwd):
                dirs[:] = [d for d in dirs if d not in excluded_dirs]
                rel_root = os.path.relpath(root, cwd)
                for fname in files:
                    full_p = Path(root) / fname
                    if not full_p.is_file() or full_p.is_symlink():
                        continue
                    try:
                        if full_p.stat().st_size > 25 * 1024 * 1024:
                            continue
                        arcname = fname if rel_root == "." else os.path.join(rel_root, fname)
                        tar.add(str(full_p), arcname=arcname, recursive=False)
                    except Exception:
                        continue
        return tmp_path.read_bytes()
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_modal_offload(
    verify_cmd: str,
    cwd: Path,
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Execute `verify_cmd` inside a Modal serverless container with persistent Go cache volume.

    Uses `modal.App('zero-verify-offload')` and `modal.Volume.from_name('zero-go-verify-cache', create_if_missing=True)`
    mounted at `/cache` with `GOMODCACHE=/cache/gomod` and `GOCACHE=/cache/gobuild`,
    `cpu=4.0`, and `memory=8192`.
    """
    import modal  # type: ignore

    app = modal.App("zero-verify-offload")
    cache_vol = modal.Volume.from_name("zero-go-verify-cache", create_if_missing=True)
    image = modal.Image.debian_slim().apt_install("git", "ca-certificates", "curl", "build-essential")

    archive_bytes = _build_workspace_archive(cwd)

    @app.function(
        image=image,
        volumes={"/cache": cache_vol},
        cpu=4.0,
        memory=8192,
        timeout=max(60, int(timeout)),
    )
    def _remote_verify_runner(payload: bytes, cmd_str: str, timeout_sec: int) -> tuple[int, str, str]:
        import io
        import os as _os
        import subprocess as _sp
        import tarfile as _tf
        import tempfile as _tmp

        _os.makedirs("/cache/gomod", exist_ok=True)
        _os.makedirs("/cache/gobuild", exist_ok=True)

        work_dir = _tmp.mkdtemp(prefix="modal-verify-")
        with _tf.open(fileobj=io.BytesIO(payload), mode="r:gz") as t:
            t.extractall(path=work_dir)

        env = _os.environ.copy()
        env["GOMODCACHE"] = "/cache/gomod"
        env["GOCACHE"] = "/cache/gobuild"
        env.setdefault("GOMEMLIMIT", "6144MiB")
        env.setdefault("GOGC", "100")
        env.setdefault("GOMAXPROCS", "4")

        proc = _sp.run(
            cmd_str,
            shell=True,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        try:
            cache_vol.commit()
        except Exception:
            pass
        return int(proc.returncode), proc.stdout or "", proc.stderr or ""

    with app.run():
        ret, out, err = _remote_verify_runner.remote(archive_bytes, verify_cmd, int(timeout))
        return int(ret), str(out), str(err)


def _run_local_clamped_subprocess(
    verify_cmd: str,
    cwd: Path,
    timeout: int = 300,
    base_env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str]:
    """Execute `verify_cmd` locally with Go memory/CPU clamping (`GOMEMLIMIT=1024MiB GOGC=50 GOMAXPROCS=2`)."""
    env = get_clamped_verify_env(base_env=base_env, verify_cmd=verify_cmd)
    env["PYTHONPATH"] = f"{cwd}:{env.get('PYTHONPATH', '')}".strip(":")

    def _low_priority() -> None:
        try:
            os.nice(10)
        except Exception:
            pass

    proc = subprocess.Popen(
        verify_cmd,
        shell=True,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=_low_priority if hasattr(os, "nice") else None,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return int(proc.returncode or 0), stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        timeout_msg = f"Verification command timed out after {int(timeout)}s"
        stderr_combined = f"{stderr}\n{timeout_msg}".strip() if stderr else timeout_msg
        return 124, stdout or "", stderr_combined


def run_or_offload_verify_cmd(
    verify_cmd: str,
    cwd: str | Path,
    changed_files: list[str] | None = None,
    timeout: int = 300,
    prefer_modal: bool = True,
    base_env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str]:
    """Filter and execute `verify_cmd`, offloading heavy Go static analysis to Modal when enabled
    and falling back cleanly to memory-clamped local execution (`GOMEMLIMIT=1024MiB`, `GOGC=50`, `GOMAXPROCS=2`).

    Returns `(returncode, stdout, stderr)`.
    """
    wt = Path(cwd)
    resolved_files = (
        list(changed_files)
        if changed_files is not None
        else detect_changed_files_from_git(wt)
    )

    filtered_cmd = filter_verify_cmd_for_changed_files(verify_cmd, resolved_files, cwd=wt)
    if not filtered_cmd:
        return 0, "", ""

    modal_disabled = os.environ.get("ZERO_MODAL_VERIFY_DISABLE", "").strip().lower() in ("1", "true", "yes")
    if (
        prefer_modal
        and not modal_disabled
        and has_heavy_go_static_analysis(filtered_cmd)
    ):
        try:
            return _run_modal_offload(filtered_cmd, wt, timeout=timeout)
        except Exception:
            # Fall back cleanly to memory-clamped local execution
            pass

    return _run_local_clamped_subprocess(
        filtered_cmd,
        wt,
        timeout=timeout,
        base_env=base_env,
    )
