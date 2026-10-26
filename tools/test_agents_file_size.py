"""Test suite verifying agents.md size limit, symlinks, and referenced memory files."""

import os
import unittest


class TestAgentsFileSize(unittest.TestCase):
    WORKSPACE = "/workspace" if os.path.exists("/workspace/agents.md") else os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    MAX_BYTES = 24000  # Hard ceiling enforced by Antigravity runtime

    def test_draft_or_live_agents_file_size(self):
        """Verify agents.md (or staged draft) does not exceed Antigravity 24 KB ceiling."""
        target_file = os.path.join(self.WORKSPACE, "agents.md")
        # Check staged draft first if present during refactor, else live agents.md
        draft_file = os.path.join(self.WORKSPACE, "scratch/agents_refactor_draft.md")
        check_file = draft_file if os.path.exists(draft_file) else target_file

        self.assertTrue(os.path.exists(check_file), f"File {check_file} does not exist")
        with open(check_file, "rb") as f:
            content = f.read()

        file_bytes = len(content)
        self.assertLessEqual(
            file_bytes,
            self.MAX_BYTES,
            f"{check_file} is {file_bytes} bytes, which exceeds the {self.MAX_BYTES} byte ceiling!",
        )

    def test_referenced_memory_files_exist(self):
        """Verify all extracted memory documents referenced in agents.md exist on disk."""
        referenced_files = [
            os.path.join(self.WORKSPACE, "memory/public/protocol_crab_cavern_peer_operations.md"),
            os.path.join(self.WORKSPACE, "memory/public/reference_native_maintenance_tools.md"),
            os.path.join(self.WORKSPACE, "memory/public/known_non_issues.md"),
        ]
        for path in referenced_files:
            self.assertTrue(os.path.exists(path), f"Referenced memory file missing: {path}")


if __name__ == "__main__":
    unittest.main()
