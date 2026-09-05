#!/usr/bin/env python3
"""
Comprehensive test suite for bridge_formatting.py
"""

import sys
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.bridge_formatting import (
    format_command_preview,
    convert_markdown_tables,
    format_for_discord,
    extract_agent_response,
    chunk_text,
    convert_markdown_to_mobile_html,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title
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
        self.assertIn("• **Server 1** ($100): Online", converted)
        self.assertIn("• **Server 2** ($200): Standby", converted)

    def test_convert_comparison_markdown_tables(self):
        table = (
            "| Feature | Chest Freezer | Upright Freezer |\n"
            "|---|---|---|\n"
            "| Defrost Type | Manual (-10°F) | Auto (32°F) |\n"
            "| Power Outage | 48+ hours | 12-24 hours |\n"
        )
        converted = convert_markdown_tables(table)
        self.assertIn("• **Defrost Type**:\n  - *Chest Freezer*: Manual (-10°F)\n  - *Upright Freezer*: Auto (32°F)", converted)
        self.assertIn("• **Power Outage**:\n  - *Chest Freezer*: 48+ hours\n  - *Upright Freezer*: 12-24 hours", converted)

    def test_format_for_discord_file_links(self):
        text = "Check [bridge.py](file:///workspace/tools/bridge.py) and [`test.py`](file:///workspace/tools/test.py)."
        formatted = format_for_discord(text)
        self.assertIn("`bridge.py`", formatted)
        self.assertIn("`test.py`", formatted)
        self.assertNotIn("file://", formatted)

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

    @unittest.mock.patch("urllib.request.urlopen")
    def test_sanitize_reaction_gifs(self, mock_urlopen):
        # 1. 404 URL that previously broke in Crab Cavern gets replaced or stripped
        mock_urlopen.side_effect = Exception("404 Not Found")
        broken_url = "https://tenor.com/view/arrested-development-lucille-bluth-jessica-walter-lock-the-door-gif-26514757"
        text = f"Here is your reaction:\n{broken_url}\nEnjoy!"
        cleaned = format_for_discord(text)
        self.assertNotIn(broken_url, cleaned)

        # 2. Valid bare GIF URL gets wrapped into titled markdown link
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.side_effect = None
        mock_urlopen.return_value = mock_resp

        bare_url = "https://tenor.com/view/ive-made-a-huge-mistake-12345"
        text2 = f"Here is your reaction:\n{bare_url}\nEnjoy!"
        cleaned2 = format_for_discord(text2)
        self.assertIn("[I've Made A Huge Mistake](https://tenor.com/view/ive-made-a-huge-mistake-12345)", cleaned2)

        # 3. Already wrapped markdown link is NOT double-wrapped
        wrapped_url = "[My Custom Title](https://tenor.com/view/ive-made-a-huge-mistake-12345)"
        text3 = f"Here is your reaction:\n{wrapped_url}\nEnjoy!"
        cleaned3 = format_for_discord(text3)
        self.assertIn("[My Custom Title](https://tenor.com/view/ive-made-a-huge-mistake-12345)", cleaned3)
        self.assertNotIn("[[", cleaned3)


if __name__ == "__main__":
    unittest.main()
