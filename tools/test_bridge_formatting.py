#!/usr/bin/env python3
"""
Comprehensive test suite for bridge_formatting.py
"""

import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.bridge_formatting import (
    format_command_preview,
    convert_markdown_tables,
    format_for_discord,
    extract_agent_response,
    AgyStreamParser,
    chunk_text,
    convert_markdown_to_mobile_html,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title,
    strip_reaction_gifs,
    is_internal_cli_leak,
    strip_internal_cli_chatter,
    dedup_repetitive_patterns,
    parse_agy_error,
    format_agy_error_message,
)


class TestBridgeFormatting(unittest.TestCase):

    def test_format_command_preview(self):
        cmd = 'ssh host1.local "docker restart bazarr"'
        res = format_command_preview(cmd)
        self.assertIn("Running", res)
        self.assertIn("docker restart bazarr", res)

        cmd2 = 'python3 /workspace/tools/sidecars.py heartbeat'
        res2 = format_command_preview(cmd2)
        self.assertEqual(res2, 'Running: python3 /workspace/tools/sidecars.py heartbeat...')

    def test_convert_markdown_tables(self):
        table = (
            "| Item | Cost | Status |\n"
            "|---|---|---|\n"
            "| Server 1 | $100 | Online |\n"
            "| Server 2 | $200 | Standby |\n"
        )
        converted = convert_markdown_tables(table)
        self.assertIn("- **Server 1** ($100): Online", converted)
        self.assertIn("- **Server 2** ($200): Standby", converted)

    def test_convert_multi_column_summary_tables(self):
        table = (
            "| Metric | Count | Status | Notes |\n"
            "|---|---|---|---|\n"
            "| **Total Entries** | 67 | — | Full canonical registry |\n"
            "| **HTTP 200 OK** | 67 / 67 | 🟢 100% | Zero dead links, DNS failures, or 404s |\n"
        )
        converted = convert_markdown_tables(table)
        self.assertIn("- **Total Entries** (67): Full canonical registry", converted)
        self.assertIn("- **HTTP 200 OK** (67 / 67): 🟢 100% · Zero dead links, DNS failures, or 404s", converted)

    def test_convert_comparison_markdown_tables(self):
        table = (
            "| Feature | Chest Freezer | Upright Freezer |\n"
            "|---|---|---|\n"
            "| Defrost Type | Manual (-10°F) | Auto (32°F) |\n"
            "| Power Outage | 48+ hours | 12-24 hours |\n"
        )
        converted = convert_markdown_tables(table)
        self.assertIn("- **Defrost Type**:\n  - *Chest Freezer*: Manual (-10°F)\n  - *Upright Freezer*: Auto (32°F)", converted)
        self.assertIn("- **Power Outage**:\n  - *Chest Freezer*: 48+ hours\n  - *Upright Freezer*: 12-24 hours", converted)

    def test_convert_capability_comparison_markdown_tables(self):
        table = (
            "| Capability | Local Home Assistant (`:8766`) | Google Home MCP (`:8769`) |\n"
            "| :--- | :--- | :--- |\n"
            "| **TV / Media Intents** | ⚡ Fast (VLC) | ❌ Restricted |\n"
            "| **Local Latency** | ⚡ Sub-50ms | ☁️ 300ms |\n"
        )
        converted = convert_markdown_tables(table)
        self.assertIn("- **TV / Media Intents**:\n  - *Local Home Assistant (`:8766`)*: ⚡ Fast (VLC)\n  - *Google Home MCP (`:8769`)*: ❌ Restricted", converted)
        self.assertIn("- **Local Latency**:\n  - *Local Home Assistant (`:8766`)*: ⚡ Sub-50ms\n  - *Google Home MCP (`:8769`)*: ☁️ 300ms", converted)

    def test_format_for_discord_bullet_normalization(self):
        text = "• Point 1\n  • Subpoint\n> • Quoted"
        formatted = format_for_discord(text)
        self.assertIn("- Point 1", formatted)
        self.assertIn("  - Subpoint", formatted)
        self.assertIn("> - Quoted", formatted)
        self.assertNotIn("•", formatted)

    def test_format_for_discord_tighten_loose_lists(self):
        text = "- **Header**:\n\n  - Sub 1\n  - Sub 2"
        formatted = format_for_discord(text)
        self.assertIn("- **Header**:\n  - Sub 1\n  - Sub 2", formatted)

    def test_format_for_discord_file_links(self):
        text = "Check [bridge.py](file:///workspace/tools/bridge.py) and [`test.py`](file:///workspace/tools/test.py) and bare file:///workspace/tools/run.py."
        formatted = format_for_discord(text)
        self.assertIn("`bridge.py`", formatted)
        self.assertIn("`test.py`", formatted)
        self.assertIn("`/workspace/tools/run.py`", formatted)
        self.assertNotIn("file://", formatted)

    def test_format_for_discord_hyperlinks(self):
        text = "👉 **[🛒 1-Click Cart](https://amazon.com/afx_item)** and *[Review](https://example.com)*"
        formatted = format_for_discord(text)
        self.assertIn("🛒 [1-Click Cart](<https://amazon.com/afx_item>)", formatted)
        self.assertIn("[Review](<https://example.com>)", formatted)
        self.assertNotIn("**[", formatted)
        self.assertNotIn(")**", formatted)

        # Redundant self-referencing links should collapse to clean bare <url>
        redundant = "Check [http://127.0.0.1:9090/planner](http://127.0.0.1:9090/planner) and [https://foo.com/bar](<https://foo.com/bar/>)"
        formatted_redundant = format_for_discord(redundant)
        self.assertIn("<http://127.0.0.1:9090/planner>", formatted_redundant)
        self.assertIn("<https://foo.com/bar/>", formatted_redundant)
        self.assertNotIn("[http://", formatted_redundant)

    def test_format_for_discord_subagent_boilerplate(self):
        text = "Subagent execution in progress...\nSubagents or tasks are still running. Pausing execution until next message.\nWait for notifications from:\n- task-123\nIf you call a tool now, you will not wait for the task to finish.\n\nActual substantive response."
        formatted = format_for_discord(text)
        self.assertNotIn("Subagent execution in progress", formatted)
        self.assertNotIn("task-123", formatted)
        self.assertEqual(formatted, "Actual substantive response.")

    def test_format_for_discord_github_alerts(self):
        text = "> [!NOTE]\n> This is a note.\n\n> [!WARNING]\n> High load."
        formatted = format_for_discord(text)
        self.assertIn("> ℹ️ **Note:**", formatted)
        self.assertIn("> ⚠️ **Warning:**", formatted)

    def test_format_for_discord_system_message_stripping(self):
        text = "<SYSTEM_MESSAGE>Some system note</SYSTEM_MESSAGE>\n\nUser facing answer."
        formatted = format_for_discord(text)
        self.assertNotIn("<SYSTEM_MESSAGE>", formatted)
        self.assertEqual(formatted, "User facing answer.")

    def test_format_for_discord_strips_no_content_generated_yet(self):
        # Leading sentinel
        text1 = "No content generated yet.\n### 1. Site Liveness\nBoth sites are UP."
        self.assertEqual(format_for_discord(text1), "### 1. Site Liveness\nBoth sites are UP.")

        # Standalone sentinel
        text2 = "No content generated yet."
        self.assertEqual(format_for_discord(text2), "")

        # Multiple repeated sentinels
        text3 = "No content generated yet.\nNo content generated yet.\nActual content."
        self.assertEqual(format_for_discord(text3), "Actual content.")

        # Embedded standalone line
        text4 = "First section.\nNo content generated yet.\nSecond section."
        self.assertEqual(format_for_discord(text4), "First section.\n\nSecond section.")

    def test_extract_agent_response(self):
        raw_stream = (
            '{"event":"init","conversation_id":"c-999"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Checking database..."}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/workspace/test"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Database check complete."}}\n'
            '{"event":"result","result":{"conversation_id":"c-999","status":"DONE"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertNotIn("Checking database", res)
        self.assertIn("Database check complete.", res)

    def test_extract_agent_response_with_result_concatenation(self):
        # agy concatenates all intermediate turn responses into result.response
        raw_stream = (
            '{"event":"init","conversation_id":"c-999"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Checking database...\\n"}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/workspace/test"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Database check complete.\\n"}}\n'
            '{"event":"result","result":{"conversation_id":"c-999","status":"DONE","response":"Checking database...\\nDatabase check complete.\\n"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertNotIn("Checking database", res)
        self.assertEqual(res, "Database check complete.")

    def test_extract_agent_response_with_concatenated_placeholder_sentinel(self):
        # agy concatenates internal sentinel into result.response
        raw_stream = (
            '{"event":"init","conversation_id":"c-999"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"No content generated yet.\\n"}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/workspace/test"}}}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Database check complete.\\n"}}\n'
            '{"event":"result","result":{"conversation_id":"c-999","status":"DONE","response":"No content generated yet.\\nDatabase check complete.\\n"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertNotIn("No content generated yet", res)
        self.assertEqual(res, "Database check complete.")

    def test_extract_agent_response_with_background_task(self):
        # Background task lifecycle: tool -> agent_response wait chatter -> system_message -> final response
        raw_stream = (
            '{"event":"init","conversation_id":"c-ha-scan"}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command"}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Scanning vacuum automations in the background...\\n"}}\n'
            '{"event":"step_update","step_update":{"step_type":"system_message"}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Here is the full automation audit.\\n"}}\n'
            '{"event":"result","result":{"conversation_id":"c-ha-scan","status":"DONE","response":"Scanning vacuum automations in the background...\\nHere is the full automation audit.\\n"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertNotIn("Scanning vacuum automations in the background", res)
        self.assertEqual(res, "Here is the full automation audit.")

    def test_extract_agent_response_preserves_substantive_response_when_followed_by_no_reply(self):
        raw_stream = (
            '{"event":"init","conversation_id":"c-bench"}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command"}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"gemini-3.8-flash-low is our fastest model. Sonnet is heavy."}}\n'
            '{"event":"step_update","step_update":{"step_type":"system_message"}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"[NO_REPLY]"}}\n'
            '{"event":"result","result":{"conversation_id":"c-bench","status":"DONE","response":"gemini-3.8-flash-low is our fastest model. Sonnet is heavy.\\n[NO_REPLY]"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertIn("gemini-3.8-flash-low is our fastest model", res)
        self.assertNotIn("[NO_REPLY]", res)

    def test_extract_agent_response_preserves_legitimate_background_text(self):
        # Verify legitimate prose mentioning "in the background" is preserved
        raw_stream = (
            '{"event":"init","conversation_id":"c-legit"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"We run Plex in the background on Host1 to stream media.\\n"}}\n'
            '{"event":"result","result":{"conversation_id":"c-legit","status":"DONE","response":"We run Plex in the background on Host1 to stream media.\\n"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertEqual(res, "We run Plex in the background on Host1 to stream media.")

    def test_extract_agent_response_non_streaming_fallback(self):
        # Non-streaming JSON mode where only result.response is present
        raw_stream = (
            '{"event":"result","result":{"conversation_id":"c-fallback","status":"DONE","response":"Standard non-streaming reply."}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertEqual(res, "Standard non-streaming reply.")

    def test_extract_agent_response_pure_silence_sentinel(self):
        # Explicit silence request without substantive content returns sentinel [NO_REPLY]
        raw_stream = (
            '{"event":"init","conversation_id":"c-silent"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"[NO_REPLY]"}}\n'
            '{"event":"result","result":{"conversation_id":"c-silent","status":"DONE","response":"[NO_REPLY]"}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertEqual(res, "[NO_REPLY]")

    def test_strip_task_wait_chatter(self):
        text = "Waiting for task-300 to complete...\nWaiting for task-357 to complete...\nHere is the real answer."
        res = format_for_discord(text)
        self.assertEqual(res, "Here is the real answer.")

    def test_extract_agent_response_ignores_task_wait_chatter(self):
        raw_stream = (
            '{"event":"init","conversation_id":"c-task-wait"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Waiting for task-300 to complete..."}}\n'
            '{"event":"step_update","step_update":{"step_type":"tool","tool_name":"manage_task"}}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Final substantive result."}}\n'
            '{"event":"result","result":{"conversation_id":"c-task-wait","status":"DONE","response":"Final substantive result."}}\n'
        )
        res = extract_agent_response(raw_stream)
    def test_is_internal_cli_leak(self):
        leaks = [
            "No tools called. Waiting for task to complete.",
            "No tools called.",
            "Waiting for task to complete.",
            "Waiting for the command to finish.",
            "Waiting for task-1154 to complete...",
            "I will wait for the task to finish.",
            "I have launched the command and will wait for it to finish.",
            'Task id "26040ca8-9071-484a-ba4a-bb822ec60c65/task-1154" was canceled with result:\nTool execution was canceled',
            "Tool is running as a background task with task id: 123",
            "No content generated yet.",
            "*(Response completed, but no text output was generated)*",
            "[NO_REPLY]",
            "   \n\n  ",
            "Process 295661fc-0967-4dec-862a-a0ecadbc2261/task-1536 completed with exit code 0. Output:\n........\nRan 8 tests in 10.373s\nOK\n[BridgeDaemon] Worker for #zero-chat exited cleanly.\n[BridgeDaemon] Worker for #the-banana-stand exited cleanly.",
            "Process 1234/task-99 completed with exit code 0. Output:\nSome raw task log\n[BridgeDaemon] Worker for #zero-chat exited cleanly.",
            "\n".join(["[BridgeDaemon] Worker for #zero-chat exited cleanly."] * 10),
            "\n".join(["[BridgeDaemon] Worker for #zero-chat exited cleanly.", "[BridgeDaemon] Worker for #the-banana-stand exited cleanly."] * 10),
        ]
        for leak in leaks:
            self.assertTrue(is_internal_cli_leak(leak), f"Failed to classify leak: {leak!r}")

        non_leaks = [
            "Here is the plan for the server migration.",
            "PR #31 is merged and PR #32 is ready for review.",
            "Waiting for task-300 to complete...\nHere is the real substantive answer.",
            "If the process exits 0 while stderr contains `terminating \\d+ background task(s) on exit` (or uncompleted tasks lack SYSTEM_MESSAGE payloads), catch the exit and resume.",
        ]
        for non_leak in non_leaks:
            self.assertFalse(is_internal_cli_leak(non_leak), f"False positive leak classification: {non_leak!r}")

    def test_strip_internal_cli_chatter(self):
        text = (
            "Here is part 1 of the report.\n"
            "No tools called. Waiting for task to complete.\n"
            "Here is part 2 with the conclusion."
        )
        cleaned = strip_internal_cli_chatter(text)
        self.assertEqual(cleaned, "Here is part 1 of the report.\nHere is part 2 with the conclusion.")

        # Test stripping RECEIVED_TASK_NOTIFICATION envelope
        notification_text = (
            "<RECEIVED_TASK_NOTIFICATION>\n"
            "Task `c16e568c/task-183` completed.\n"
            "Run results:\n"
            "Output:\n"
            "switch.irrigation_brains_hill_right -> State: on\n"
            "Exit code: 0\n"
            "</RECEIVED_TASK_NOTIFICATION>\n"
            "Handled. Updated **`Irrigation: Hill Right`** in Home Assistant."
        )
        self.assertEqual(
            strip_internal_cli_chatter(notification_text),
            "Handled. Updated **`Irrigation: Hill Right`** in Home Assistant."
        )

        # Test inline mentions of tags in code ticks are preserved and do NOT truncate to EOF
        inline_mention_text = (
            "The sanitizer scrubs CLI artifacts like `<SYSTEM_MESSAGE>` and `<RECEIVED_TASK_NOTIFICATION>` tags.\n"
            "Here is the rest of the text that must not be truncated."
        )
        self.assertEqual(
            strip_internal_cli_chatter(inline_mention_text),
            inline_mention_text
        )

    def test_dedup_repetitive_patterns(self):
        # Single-line repetition
        spam_single = "\n".join(["[BridgeDaemon] Worker for #zero-chat exited cleanly."] * 20)
        deduped = dedup_repetitive_patterns(spam_single, max_repeats=3)
        self.assertIn("... [repetitive output truncated] ...", deduped)
        self.assertLess(len(deduped), len(spam_single))

        # Multi-line pattern repetition
        pattern = ["[BridgeDaemon] Worker for #zero-chat exited cleanly.", "[BridgeDaemon] Worker for #the-banana-stand exited cleanly."]
        spam_multi = "\n".join(pattern * 15)
        deduped_multi = dedup_repetitive_patterns(spam_multi, max_repeats=3)
        self.assertIn("... [repetitive output truncated] ...", deduped_multi)
        self.assertLess(len(deduped_multi), len(spam_multi))

    def test_extract_agent_response_pure_task_wait_returns_no_reply(self):
        # When agent emits 'No tools called. Waiting for task to complete.', extract_agent_response must return [NO_REPLY]
        raw_stream = (
            '{"event":"init","conversation_id":"c-leak-test"}\n'
            '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"No tools called. Waiting for task to complete."}}\n'
            '{"event":"result","result":{"conversation_id":"c-leak-test","status":"DONE","response":"No tools called. Waiting for task to complete."}}\n'
        )
        res = extract_agent_response(raw_stream)
        self.assertEqual(res, "[NO_REPLY]")

        # Plain text mode fallback
        res_plain = extract_agent_response("No tools called. Waiting for task to complete.")
        self.assertEqual(res_plain, "[NO_REPLY]")

    def test_chunk_text_basic_and_boundary(self):
        short_text = "Hello world"
        self.assertEqual(chunk_text(short_text, 100), ["Hello world"])

        # Boundary squeeze
        padded_text = "Line 1\n\n\n\nLine 2   \n" + "x" * 1960
        chunks = chunk_text(padded_text, 1980)
        self.assertEqual(len(chunks), 1)

        # Large text splitting
        large_text = "\n".join(f"Line {i} content text" for i in range(200))
        chunks = chunk_text(large_text, 500)
        self.assertTrue(len(chunks) > 1)
        for c in chunks:
            self.assertTrue(len(c) <= 550)

    def test_chunk_text_preserves_code_block_when_it_fits(self):
        """Verify code blocks and handoff envelopes that fit in current chunk are NOT split into separate chunks."""
        # 1,749 characters of text + 163 character handoff block = 1,912 chars (Msg 36 regression test)
        body = "Engineering review of endpoints and referee state.\n" * 34
        env = (
            "```handoff\n"
            "{\n"
            '  "v": 0,\n'
            '  "kind": "proposal",\n'
            '  "to": "Amos"\n'
            "}\n"
            "```"
        )
        full_text = f"{body.strip()}\n\n{env}"
        self.assertLessEqual(len(full_text), 1980)

        chunks = chunk_text(full_text, 1980)
        self.assertEqual(len(chunks), 1)
        self.assertIn("```handoff", chunks[0])
        self.assertTrue(chunks[0].endswith("```"))

    def test_chunk_text_breaks_before_overflowing_code_block(self):
        """Verify that when a code block would overflow the current chunk, it breaks before the code block."""
        # 1,500 characters of text + 600 character code block = 2,100 chars
        body = "Line of text\n" * 115  # ~1495 chars
        code_body = "x = 1\n" * 100        # ~600 chars
        code_block = f"```python\n{code_body}```"
        full_text = f"{body.strip()}\n\n{code_block}"

        chunks = chunk_text(full_text, 1980)
        self.assertEqual(len(chunks), 2)
        # First chunk should have the text
        self.assertNotIn("```python", chunks[0])
        # Second chunk should cleanly start with the code block
        self.assertTrue(chunks[1].startswith("```python"))

    def test_clean_discord_latex(self):
        latex_text = "Let $x$ be the variable and $$\\alpha + \\beta = \\gamma$$ for calculation."
        cleaned = clean_discord_latex(latex_text)
        self.assertIn("*x*", cleaned)
        self.assertIn("α + β = γ", cleaned)

        # Single variable italics vs currency
        curr_text = "Price is $50.00 and param is $d$."
        curr_cleaned = clean_discord_latex(curr_text)
        self.assertIn("$50.00", curr_cleaned)
        self.assertIn("*d*", curr_cleaned)

    def test_scrub_credentials(self):
        text = "Connecting to " + "192.168.1." + "82 with token " + "ya29." + "a0AfH6SMD_1234567890."
        scrubbed = scrub_credentials(text)
        self.assertIn("[internal-ip]", scrubbed)
        self.assertIn("[REDACTED_OAUTH_TOKEN]", scrubbed)
        self.assertNotIn("192.168.1." + "82", scrubbed)
        self.assertNotIn("ya29.a0AfH6SMD", scrubbed)

        # Ensure task IDs do not trigger false-positive API key redaction (ta + sk-)
        task_text = "Tracking task-1788316504-voice-check-eval across the cluster."
        task_scrubbed = scrub_credentials(task_text)
        self.assertIn("task-1788316504-voice-check-eval", task_scrubbed)
        self.assertNotIn("[REDACTED_API_KEY]", task_scrubbed)

        # Real API key with sk- prefix should be redacted
        key_text = "Using secret " + "sk-" + "abcdef1234567890abcdef12345 for auth."
        key_scrubbed = scrub_credentials(key_text)
        self.assertIn("[REDACTED_API_KEY]", key_scrubbed)

    def test_generate_concise_thread_title(self):
        prompt = "please check the kia ev9 market listings and dealership alerts"
        title = generate_concise_thread_title(prompt)
        self.assertEqual(title, "Kia EV9 Dealership Listings Market Monitor")

        prompt2 = "can we investigate why tautulli plex transcode is failing on host1"
        title2 = generate_concise_thread_title(prompt2)
        self.assertEqual(title2, "Plex Media Server Alerts and Transcoding")

        prompt3 = "Analyze and refactor the database connector pool timeout"
        title3 = generate_concise_thread_title(prompt3)
        self.assertTrue(len(title3.split()) >= 3)
        self.assertNotIn("analyze", title3.lower())

    @patch("urllib.request.urlopen")
    def test_sanitize_reaction_gifs(self, mock_urlopen):
        # 1. 404 URL that previously broke in Crab Cavern gets replaced or stripped
        mock_urlopen.side_effect = Exception("404 Not Found")
        broken_url = "https://tenor.com/view/arrested-development-lucille-bluth-jessica-walter-lock-the-door-gif-26514757"
        text = f"Here is your reaction:\n{broken_url}\nEnjoy!"
        cleaned = format_for_discord(text)
        self.assertNotIn(broken_url, cleaned)

        # 2. Valid bare GIF URL gets wrapped into [GIF](url) and moved to end
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.side_effect = None
        mock_urlopen.return_value = mock_resp

        bare_url = "https://tenor.com/view/ive-made-a-huge-mistake-12345"
        text2 = f"Here is your reaction:\n{bare_url}\nEnjoy!"
        cleaned2 = format_for_discord(text2)
        self.assertIn(f"[GIF]({bare_url})", cleaned2)
        self.assertTrue(cleaned2.endswith(f"[GIF]({bare_url})"))

        # 3. Custom titled markdown link is normalized to [GIF](url) and moved to end
        wrapped_url = "[My Custom Title](https://tenor.com/view/ive-made-a-huge-mistake-12345)"
        text3 = f"Here is your reaction:\n{wrapped_url}\nEnjoy!"
        cleaned3 = format_for_discord(text3)
        self.assertIn(f"[GIF]({bare_url})", cleaned3)
        self.assertNotIn("[My Custom Title]", cleaned3)
        self.assertNotIn("[[", cleaned3)
        self.assertTrue(cleaned3.endswith(f"[GIF]({bare_url})"))

        # 4. GIF link is relocated before [CHOICES: ...] block
        text4 = f"[GIF]({bare_url})\n\nHandled. Both dispatchers are green.\n\n[CHOICES: Inspect Host 1 | Inspect Host 2]"
        cleaned4 = format_for_discord(text4)
        self.assertEqual(cleaned4, f"Handled. Both dispatchers are green.\n\n[GIF]({bare_url})\n\n[CHOICES: Inspect Host 1 | Inspect Host 2]")

        # 5. Hallucinated Tenor URL with mismatched redirected slug is rejected
        mock_hallucinated = MagicMock()
        mock_hallucinated.status = 200
        mock_hallucinated.geturl.return_value = "https://tenor.com/view/xdbacom-sfea-gif-20092285"
        mock_hallucinated.__enter__.return_value = mock_hallucinated
        mock_urlopen.return_value = mock_hallucinated

        hallucinated_url = "https://tenor.com/view/hot-dog-suit-costume-car-crash-i-think-you-should-leave-gif-20092285"
        text5 = f"Check this out:\n{hallucinated_url}\nDone."
        with patch("tools.gif_tool.get_contextual_gif", return_value=None):
            cleaned5 = format_for_discord(text5)
        self.assertNotIn(hallucinated_url, cleaned5)
        self.assertNotIn("xdbacom", cleaned5)

    def test_ensure_handoff_mentions_direct_bot(self):
        from tools.handoff import format_envelope, ensure_handoff_mentions
        text = (
            "Here is the proposal:\n"
            "```handoff\n"
            "{\n"
            '  "v": 1,\n'
            '  "kind": "proposal",\n'
            '  "reply": "required",\n'
            '  "to": "amos",\n'
            '  "subject": "mutex-review"\n'
            "}\n"
            "```\n"
            "Take a look."
        )
        formatted = format_for_discord(text)
        self.assertTrue(formatted.startswith("<@1468012353206354197>"))
        self.assertIn("mutex-review", formatted)

    def test_ensure_handoff_mentions_team_role(self):
        text = (
            "🍌 ```handoff\n"
            "{\n"
            '  "v": 1,\n'
            '  "kind": "proposal",\n'
            '  "reply": "optional",\n'
            '  "to": "team",\n'
            '  "subject": "harness-rfc"\n'
            "}\n"
            "```\n"
            "Discussion open."
        )
        formatted = format_for_discord(text)
        self.assertTrue(formatted.startswith("<@&1543462881624858624>"))

    def test_ensure_handoff_mentions_already_present(self):
        text = (
            "<@1468012353206354197> Amos heads up:\n"
            "```handoff\n"
            "{\n"
            '  "v": 1,\n'
            '  "kind": "proposal",\n'
            '  "reply": "required",\n'
            '  "to": "amos",\n'
            '  "subject": "mutex-review"\n'
            "}\n"
            "```"
        )
        formatted = format_for_discord(text)
        # Should not duplicate the tag
        self.assertEqual(formatted.count("1468012353206354197"), 1)

    def test_ensure_handoff_mentions_reply_none_silent(self):
        text = (
            "Status update:\n"
            "```handoff\n"
            "{\n"
            '  "v": 1,\n'
            '  "kind": "status",\n'
            '  "reply": "none",\n'
            '  "to": "amos",\n'
            '  "subject": "status-report"\n'
            "}\n"
            "```"
        )
        formatted = format_for_discord(text)
        self.assertNotIn("<@1468012353206354197>", formatted)

    def test_ensure_handoff_mentions_self_skip(self):
        text = (
            "```handoff\n"
            "{\n"
            '  "v": 1,\n'
            '  "kind": "answer",\n'
            '  "reply": "required",\n'
            '  "to": "zero",\n'
            '  "subject": "self-loop-prevention"\n'
            "}\n"
            "```"
        )
        formatted = format_for_discord(text)
        self.assertNotIn("<@1542285964213358633>", formatted)

    def test_format_envelope_with_mention(self):
        from tools.handoff import format_envelope
        res = format_envelope(kind="proposal", reply="required", subject="test", to="marvin")
        self.assertIn("<@1492043459618537492>", res)
        self.assertIn("```handoff", res)

        res_none = format_envelope(kind="status", reply="none", subject="test", to="marvin")
        self.assertNotIn("<@1492043459618537492>", res_none)


    def test_strip_background_wait_and_pause_chatter(self):
        """Verify format_for_discord strips intermediate background task wait and pause chatter."""
        text = (
            "I have launched the bridge test suite in the background to verify the concurrency and scheduler reload fixes, and will review the results the moment it completes.\n"
            "I am pausing tool calls to allow the bridge test suite to complete in the background. The system will resume execution automatically once the test results are in.\n\n"
            "### Direct Answer\n"
            "Here is the verified fix."
        )
        formatted = format_for_discord(text)
        self.assertNotIn("I have launched the bridge test suite", formatted)
        self.assertNotIn("I am pausing tool calls", formatted)
        self.assertNotIn("The system will resume execution automatically", formatted)
        self.assertIn("### Direct Answer", formatted)
        self.assertIn("Here is the verified fix.", formatted)

        # Standalone wait chatter should resolve to empty
        standalone = (
            "I have launched the bridge test suite in the background to verify the concurrency and scheduler reload fixes, and will review the results the moment it completes.\n"
            "I am pausing tool calls to allow the bridge test suite to complete in the background. The system will resume execution automatically once the test results are in."
        )
        self.assertEqual(format_for_discord(standalone), "")


    def test_strip_reaction_gifs(self):
        """Verify strip_reaction_gifs completely removes GIF markdown links and bare URLs."""
        text = (
            "Here is the technical update for the cluster.\n\n"
            "[GIF](<https://tenor.com/view/funny-cat-12345>)\n\n"
            "Next steps are listed below."
        )
        stripped = strip_reaction_gifs(text)
        self.assertNotIn("https://tenor.com/view/funny-cat-12345", stripped)
        self.assertNotIn("[GIF]", stripped)
        self.assertIn("Here is the technical update for the cluster.", stripped)
        self.assertIn("Next steps are listed below.", stripped)

        # Bare GIF URL and trailing placement
        bare = "All services green.\nhttps://media.tenor.com/abc-123/view.gif"
        self.assertEqual(strip_reaction_gifs(bare), "All services green.")

        # Text without GIFs remains untouched
        plain = "Pure text without any reaction links."
        self.assertEqual(strip_reaction_gifs(plain), plain)

    def test_agy_stream_parser_tool_clears_pre_tool_narration(self):
        parser = AgyStreamParser()
        parser.process_line('{"event":"init","conversation_id":"c-123"}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Let me view the config file..."}}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/workspace/config.json"}}}}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Config file parsed: port is 8080."}}')
        parser.process_line('{"event":"result","result":{"conversation_id":"c-123","status":"DONE","response":"Let me view the config file...\\nConfig file parsed: port is 8080."}}')
        
        final_resp = parser.get_final_response()
        self.assertNotIn("Let me view the config file", final_resp)
        self.assertEqual(final_resp, "Config file parsed: port is 8080.")
        self.assertEqual(parser.conv_id, "c-123")

    def test_agy_stream_parser_system_message_preserves_substantive_response(self):
        parser = AgyStreamParser()
        parser.process_line('{"event":"init","conversation_id":"c-456"}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command"}}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Benchmark complete: throughput is 12k req/s."}}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"system_message"}}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"[NO_REPLY]"}}')
        parser.process_line('{"event":"result","result":{"conversation_id":"c-456","status":"DONE"}}')

        final_resp = parser.get_final_response()
        self.assertEqual(final_resp, "Benchmark complete: throughput is 12k req/s.")

    def test_agy_stream_parser_pure_silence(self):
        parser = AgyStreamParser()
        parser.process_line('{"event":"init","conversation_id":"c-789"}')
        parser.process_line('{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"[NO_REPLY]"}}')
        parser.process_line('{"event":"result","result":{"conversation_id":"c-789","status":"DONE","response":"[NO_REPLY]"}}')

        final_resp = parser.get_final_response()
        self.assertEqual(final_resp, "[NO_REPLY]")

    def test_agy_stream_parser_error_handling(self):
        parser = AgyStreamParser()
        parser.process_line('{"event":"init","conversation_id":"c-err"}')
        parser.process_line('{"event":"result","result":{"conversation_id":"c-err","status":"ERROR","error":"Process killed by signal 9"}}')

        final_resp = parser.get_final_response()
        self.assertEqual(final_resp, "Error: Process killed by signal 9")

    def test_agy_stream_parser_multi_json_per_line(self):
        parser = AgyStreamParser()
        parser.process_line('{"event":"init","conversation_id":"c-multi"}{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Multi-event line test."}}')
        parser.process_line('{"event":"result","result":{"conversation_id":"c-multi","status":"DONE"}}')

        final_resp = parser.get_final_response()
        self.assertEqual(final_resp, "Multi-event line test.")
        self.assertEqual(parser.conv_id, "c-multi")

    def test_parse_agy_error(self):
        raw_stderr = (
            "Some preamble\n"
            'AGY_ERROR: {"canonical_status":"RESOURCE_EXHAUSTED","code":429,"retryable":true,"error_id":"err-xyz-123","short_error":"Quota exceeded for model gemini-3.8-flash"}\n'
            "Some trailer\n"
        )
        parsed = parse_agy_error(raw_stderr)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("canonical_status"), "RESOURCE_EXHAUSTED")
        self.assertEqual(parsed.get("code"), 429)
        self.assertTrue(parsed.get("retryable"))
        self.assertEqual(parsed.get("error_id"), "err-xyz-123")
        self.assertEqual(parsed.get("short_error"), "Quota exceeded for model gemini-3.8-flash")

        # Test empty or non-matching
        self.assertIsNone(parse_agy_error(""))
        self.assertIsNone(parse_agy_error("Random error without AGY_ERROR tag"))

    def test_format_agy_error_message(self):
        err = {
            "canonical_status": "UNAVAILABLE",
            "code": 503,
            "retryable": True,
            "error_id": "req-999-abc",
            "short_error": "Backend upstream unavailable",
        }
        msg = format_agy_error_message(err, elapsed_sec=12, pid_str="PID 12345")
        self.assertIn("⚠️ **Model API Failure (CLI Exit Code 3):**", msg)
        self.assertIn("Backend upstream unavailable", msg)
        self.assertIn("`UNAVAILABLE` (Code 503)", msg)
        self.assertIn("`req-999-abc`", msg)
        self.assertIn("Retryable:** Yes", msg)
        self.assertIn("Elapsed:** 12s", msg)
        self.assertIn("Process:** `PID 12345`", msg)


if __name__ == "__main__":
    unittest.main()
