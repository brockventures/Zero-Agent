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
        self.assertEqual(res, "Final substantive result.")

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


if __name__ == "__main__":
    unittest.main()
