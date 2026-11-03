#!/usr/bin/env python3
"""Unit tests for tools/modal_verify.py and FORGE Maker-Checker verification integration."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.forge import verify_chunk_worktree  # noqa: E402
from tools.modal_verify import (  # noqa: E402
    SKIP_GO_ECHO_CMD,
    detect_changed_files_from_git,
    filter_verify_cmd_for_changed_files,
    get_clamped_verify_env,
    has_heavy_go_static_analysis,
    is_go_or_api_trigger_file,
    run_or_offload_verify_cmd,
    should_run_go_verification,
)


class TestModalVerify(unittest.TestCase):
    """Verify path-gated Go static analysis filtering, memory clamping, and Modal offload."""

    def test_01_should_run_go_verification_path_gating(self) -> None:
        # Frontend / asset / doc-only file lists -> False
        self.assertFalse(
            should_run_go_verification(
                ["web/index.html", "web/styles/app.css", "web/js/board.js"],
                "deadcode ./... && golangci-lint run",
            )
        )
        self.assertFalse(
            should_run_go_verification(
                ["src/components/View.tsx", "README.md", "assets/logo.svg", "assets/icon.png"],
                "go test ./...",
            )
        )

        # Go / go.mod / go.sum / OpenAPI / Makefile / Dockerfile present -> True
        self.assertTrue(
            should_run_go_verification(
                ["web/index.html", "internal/game/engine.go"],
                "deadcode ./...",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["go.mod", "web/app.css"],
                "golangci-lint run",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["go.sum"],
                "go test ./...",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["api/openapi.yaml", "web/index.html"],
                "oapi-codegen -config cfg.yaml api/openapi.yaml",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["docs/swagger.json"],
                "oapi-codegen -config cfg.yaml docs/swagger.json",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["Makefile"],
                "deadcode ./...",
            )
        )
        self.assertTrue(
            should_run_go_verification(
                ["Dockerfile"],
                "go build ./...",
            )
        )

        # Empty changed_files -> conservative True fallback
        self.assertTrue(should_run_go_verification([], "deadcode ./..."))

    def test_02_filter_verify_cmd_for_changed_files(self) -> None:
        frontend_only = ["web/static/index.html", "web/static/styles.css", "web/static/app.js"]
        compound_cmd = (
            "npx eslint web/static/app.js && "
            "deadcode ./... && "
            "golangci-lint run ./... && "
            "oapi-codegen -generate types api/openapi.yaml && "
            "go vet ./... && "
            "go test ./... && "
            "pytest tests/test_ui.py"
        )

        filtered = filter_verify_cmd_for_changed_files(compound_cmd, frontend_only)
        self.assertIn("npx eslint web/static/app.js", filtered)
        self.assertIn("pytest tests/test_ui.py", filtered)
        self.assertIn(SKIP_GO_ECHO_CMD, filtered)
        self.assertNotIn("deadcode", filtered)
        self.assertNotIn("golangci-lint", filtered)
        self.assertNotIn("oapi-codegen", filtered)
        self.assertNotIn("go vet", filtered)
        self.assertNotIn("go test", filtered)

        # Semicolon-delimited compound commands are also handled cleanly
        semi_cmd = "deadcode ./...; golangci-lint run; echo 'frontend-check-ok'"
        filtered_semi = filter_verify_cmd_for_changed_files(semi_cmd, frontend_only)
        self.assertIn(SKIP_GO_ECHO_CMD, filtered_semi)
        self.assertIn("echo 'frontend-check-ok'", filtered_semi)
        self.assertNotIn("deadcode", filtered_semi)

        # When .go or openapi.yaml is modified, command is preserved verbatim
        backend_files = ["web/static/app.js", "cmd/server/main.go"]
        preserved = filter_verify_cmd_for_changed_files(compound_cmd, backend_files)
        self.assertEqual(preserved, compound_cmd)

    def test_03_get_clamped_verify_env(self) -> None:
        env = get_clamped_verify_env({})
        self.assertEqual(env["GOMEMLIMIT"], "1024MiB")
        self.assertEqual(env["GOGC"], "50")
        self.assertEqual(env["GOMAXPROCS"], "2")
        self.assertEqual(env["OMP_NUM_THREADS"], "2")

        # Explicit overrides in base_env are respected via setdefault
        custom_env = get_clamped_verify_env({"GOMEMLIMIT": "2048MiB"})
        self.assertEqual(custom_env["GOMEMLIMIT"], "2048MiB")
        self.assertEqual(custom_env["GOGC"], "50")

    def test_04_run_or_offload_verify_cmd_modal_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            # 1) When Modal offload is enabled and heavy Go analysis is active, invokes _run_modal_offload
            heavy_cmd = "deadcode ./... && golangci-lint run && go test ./..."
            with patch(
                "tools.modal_verify._run_modal_offload",
                return_value=(0, "modal-offload-ok\n", ""),
            ) as mock_modal:
                ret, out, err = run_or_offload_verify_cmd(
                    heavy_cmd,
                    cwd=wt,
                    changed_files=["internal/server/handler.go"],
                    prefer_modal=True,
                )
                self.assertEqual(ret, 0)
                self.assertIn("modal-offload-ok", out)
                self.assertEqual(err, "")
                mock_modal.assert_called_once()

            # 2) When Modal raises an exception (e.g. missing credentials/network), falls back cleanly to clamped local execution
            with patch(
                "tools.modal_verify._run_modal_offload",
                side_effect=RuntimeError("Modal auth unavailable"),
            ):
                ret, out, err = run_or_offload_verify_cmd(
                    "echo 'fallback-local' && printenv GOMEMLIMIT GOGC GOMAXPROCS",
                    cwd=wt,
                    changed_files=["internal/server/handler.go"],
                    prefer_modal=True,
                )
                self.assertEqual(ret, 0)
                self.assertIn("fallback-local", out)
                self.assertIn("1024MiB", out)
                self.assertIn("50", out)
                self.assertIn("2", out)

            # 3) Frontend-only changed_files bypass Go commands before execution
            ret_fe, out_fe, _ = run_or_offload_verify_cmd(
                "deadcode ./... && echo 'kept-segment-ok'",
                cwd=wt,
                changed_files=["ui/index.html", "ui/style.css"],
                prefer_modal=False,
            )
            self.assertEqual(ret_fe, 0)
            self.assertIn("[modal_verify] Skipped Go verification", out_fe)
            self.assertIn("kept-segment-ok", out_fe)

    def test_05_git_auto_detection_and_forge_verify_chunk_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "repo"
            wt.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-b", "main"], cwd=str(wt), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt), check=True, capture_output=True)

            (wt / "index.html").write_text("<html><body>v1</body></html>\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(wt), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt), check=True, capture_output=True)

            # Modify only index.html
            (wt / "index.html").write_text("<html><body>v2</body></html>\n", encoding="utf-8")
            detected = detect_changed_files_from_git(wt)
            self.assertIn("index.html", detected)
            self.assertFalse(should_run_go_verification(None, "deadcode ./...", cwd=wt))

            # verify_chunk_worktree skips non-existent `deadcode` binary on HTML-only change and passes
            passed, diff_stat, err = verify_chunk_worktree(
                wt,
                "deadcode ./... && golangci-lint run && echo 'html-verified'",
                target_files=["index.html"],
                require_changes=True,
            )
            self.assertTrue(passed, err)
            self.assertIn("1 file changed", diff_stat)

    def test_06_helpers_trigger_and_heavy_detection(self) -> None:
        self.assertTrue(is_go_or_api_trigger_file("pkg/api/openapi.yaml"))
        self.assertTrue(is_go_or_api_trigger_file("cmd/main.go"))
        self.assertFalse(is_go_or_api_trigger_file("static/app.js"))

        self.assertTrue(has_heavy_go_static_analysis("deadcode ./..."))
        self.assertTrue(has_heavy_go_static_analysis("golangci-lint run"))
        self.assertTrue(has_heavy_go_static_analysis("go test -race ./..."))
        self.assertFalse(has_heavy_go_static_analysis("pytest tools/test_modal_verify.py"))


if __name__ == "__main__":
    unittest.main()
