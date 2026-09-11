#!/usr/bin/env python3
"""
Unit test suite for Zero bridge safety, reload intents, and formatting scrubbing.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE))

from tools.bridge import is_reload_intent, is_container_restart_intent, format_for_discord, extract_agent_response
from tools.session_summarizer import extract_recent_dialogue


class TestBridgeSafety(unittest.TestCase):

    def test_reload_intent_recognition(self):
        """Test exact and natural language in-place reload/restart intents."""
        positive_cases = [
            "!reload",
            "/reload",
            "!restart",
            "/restart",
            "!reboot",
            "/reboot",
            "reload",
            "restart",
            "reboot",
            "restart yourself",
            "please restart",
            "can you restart?",
            "do a restart",
            "hey zero, please reload the bridge",
            "restart bridge now",
            "reload now",
            "reboot yourself",
            "yes restart",
            "yes reload",
            "Reload Bridge In-Place",
            "reload bridge in place",
            "restart bridge in-place",
        ]
        for text in positive_cases:
            with self.subTest(text=text):
                self.assertTrue(
                    is_reload_intent(text),
                    f"Expected '{text}' to match reload intent",
                )

        negative_cases = [
            "how do you restart a docker container?",
            "what happened during the restart?",
            "can you fix the restart bug in python?",
            "test",
            "tell me about reboot procedures in linux",
            "we had a restart yesterday",
            "Restart Container Now",
            "Restart Docker Container",
            "restart container",
        ]
        for text in negative_cases:
            with self.subTest(text=text):
                self.assertFalse(
                    is_reload_intent(text),
                    f"Expected '{text}' NOT to match reload intent",
                )

    def test_container_restart_intent_recognition(self):
        """Test exact and natural language Docker container restart intents."""
        positive_cases = [
            "Restart Docker Container",
            "restart docker container",
            "Restart Container Now",
            "restart container now",
            "restart container",
            "reboot container",
            "docker restart",
            "restart the container",
            "restart the docker container",
            "reboot the container",
            "hey zero, please restart the container",
        ]
        for text in positive_cases:
            with self.subTest(text=text):
                self.assertTrue(
                    is_container_restart_intent(text),
                    f"Expected '{text}' to match container restart intent",
                )

        negative_cases = [
            "!reload",
            "/reload",
            "Reload Bridge In-Place",
            "reload bridge",
            "restart bridge",
            "what is a container?",
            "how do containers restart?",
        ]
        for text in negative_cases:
            with self.subTest(text=text):
                self.assertFalse(
                    is_container_restart_intent(text),
                    f"Expected '{text}' NOT to match container restart intent",
                )

    def test_task_envelope_scrubbing(self):
        """Ensure internal agent task completion envelopes and wait chatter are scrubbed."""
        raw_output = (
            "An asynchronous task has completed: task-123 (State: DONE) "
            "Result payload: 42 Task output: [Success]\n\n"
            "Here is the real answer Ryan."
        )
        cleaned = format_for_discord(raw_output)
        self.assertNotIn("An asynchronous task has completed", cleaned)
        self.assertIn("Here is the real answer Ryan.", cleaned)

    def test_wait_narration_scrubbing(self):
        """Ensure intermediate wait/launch chatter is stripped."""
        raw_output = (
            "I have launched the background task and will wait for it to finish.\n\n"
            "### Summary\nAll operations complete."
        )
        cleaned = format_for_discord(raw_output)
        self.assertNotIn("I have launched the background task", cleaned)
        self.assertIn("### Summary", cleaned)

    def test_bridge_ast_no_module_shadowing(self):
        """Verify that tools/bridge.py has no exception handlers or assignments shadowing critical stdlib modules."""
        bridge_file = WORKSPACE / "tools" / "bridge.py"
        self.assertTrue(bridge_file.exists())
        with open(bridge_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(bridge_file))

        dangerous_names = {
            "re",
            "json",
            "os",
            "sys",
            "time",
            "asyncio",
            "discord",
            "uuid",
            "signal",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.ExceptHandler) and child.name:
                        self.assertNotIn(
                            child.name,
                            dangerous_names,
                            f"Function '{node.name}' shadows module '{child.name}' in ExceptHandler at line {child.lineno}",
                        )

    def test_extract_agent_response_clears_intermediate_tool_narration(self):
        """Verify that intermediate thoughts/speech before tool calls are discarded in stream-json parsing."""
        raw_stream = (
            '{"event":"init","conversation_id":"c123"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Testing connectivity for all dashboard services and widget endpoints..."}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"curl -s http://127.0.0.1:8008"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Testing remaining endpoints..."}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"curl -s http://127.0.0.1:32400"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Locating Plex configuration..."}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/workspace/config/plex.json"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Here is the forensic breakdown of your Homelab Homepage dashboard."}}\n'
            '{"event":"result","result":{"conversation_id":"c123","status":"DONE"}}\n'
        )
        extracted = extract_agent_response(raw_stream)
        self.assertNotIn("Testing connectivity", extracted)
        self.assertNotIn("Testing remaining endpoints", extracted)
        self.assertNotIn("Locating Plex configuration", extracted)
        self.assertIn("Here is the forensic breakdown of your Homelab Homepage dashboard.", extracted)

    def test_extract_agent_response_without_tools(self):
        """Verify that a single direct response without tool calls is fully accumulated."""
        raw_stream = (
            '{"event":"init","conversation_id":"c123"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Hello Ryan, "}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"all systems are green."}}\n'
            '{"event":"result","result":{"conversation_id":"c123","status":"DONE"}}\n'
        )
        extracted = extract_agent_response(raw_stream)
        self.assertEqual(extracted, "Hello Ryan, all systems are green.")


    def test_no_module_shadowing_in_exception_handlers(self):
        """Ensure no standard module names (re, os, sys, json, etc.) are shadowed in except handlers."""
        shadowed_modules = {"re", "os", "sys", "json", "time", "asyncio", "discord"}
        target_files = [
            WORKSPACE / "tools" / "bridge_runner.py",
            WORKSPACE / "tools" / "bridge_daemons.py",
            WORKSPACE / "tools" / "bridge_handlers.py",
        ]
        for fpath in target_files:
            tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.name in shadowed_modules:
                    self.fail(f"Found shadowed module name '{node.name}' in exception handler in {fpath.name}:{node.lineno}")


if __name__ == "__main__":
    unittest.main()
