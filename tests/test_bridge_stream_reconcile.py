import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.bridge_stream import AgyStreamParser, reconcile_step_segments
from tools.bridge_pipeline import deliver_turn_output


class TestBridgeStreamReconcile(unittest.TestCase):
    def test_full_redraft_after_step_update_error_message(self):
        """Reproduces the 02:41:13 PM PT double-message bug (Step 90 cut off -> Step 91 error_message -> Step 92 full redraft)."""
        parser = AgyStreamParser(conv_id="conv-123")
        step_90_partial = (
            "🟢 **Ivy is online and ready.**\n\n"
            "### 🔄 Restart Summary\n"
            "- **1. Bridge & Daemon Reload:** Activated steering guard.\n"
            "- **3. Hold for Zero's `TASK-23.17B"
        )
        step_92_full = (
            "🟢 **Ivy is online and ready.**\n\n"
            "### 🔄 Restart Summary\n"
            "- **1. Bridge & Daemon Reload:** Activated steering guard.\n"
            "- **3. Stand By for Zero's `TASK-23.17B` Summary:** Let Zero complete the requested review first."
        )

        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_index": 90,
                "step_type": "agent_response",
                "text_delta": step_90_partial,
            },
        })
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_index": 91,
                "step_type": "error_message",
                "content": "Error: The stream was interrupted. Please continue the task you were working on.",
            },
        })
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_index": 92,
                "step_type": "agent_response",
                "text_delta": step_92_full,
                "state": "DONE",
            },
        })

        final = parser.get_final_response()
        self.assertEqual(final, step_92_full)
        self.assertNotIn("Hold for Zero's `TASK-23.17B\n🟢", final)

    def test_tail_continuation_preserves_pre_interruption_draft(self):
        """When model obeys 'continue the task' by emitting only the remaining tail, preserve and stitch prefix + tail."""
        parser = AgyStreamParser(conv_id="conv-123")
        step_90_partial = (
            "🟢 **Ivy is online and ready.**\n\n"
            "- **1. Bridge & Daemon Reload:** Activated steering guard.\n"
            "- **2. Next Step:** Hold for Zero's"
        )
        step_92_tail = " `TASK-23.17B` review before starting `TASK-23.18`."

        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_index": 90,
                "step_type": "agent_response",
                "text_delta": step_90_partial,
            },
        })
        parser.process_event({
            "event": "error_message",
            "content": "Error: The stream was interrupted. Please continue the task you were working on.",
        })
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_index": 92,
                "step_type": "agent_response",
                "text_delta": step_92_tail,
                "state": "DONE",
            },
        })

        final = parser.get_final_response()
        self.assertEqual(
            final,
            "🟢 **Ivy is online and ready.**\n\n"
            "- **1. Bridge & Daemon Reload:** Activated steering guard.\n"
            "- **2. Next Step:** Hold for Zero's `TASK-23.17B` review before starting `TASK-23.18`.",
        )

    def test_bullet_restart_replaces_only_truncated_last_line(self):
        """When model restarts from the truncated bullet at the end of Step 90, replace only that broken line."""
        earlier = (
            "### ⚾ Next Operations on Deck\n"
            "- **1. `TASK-23.18`:** Couple strikeout totals.\n"
            "- **2. 2027 Preseason Audit:** Generate leaderboard.\n"
            "- **3. Hold for Zero's `TASK-23.17B"
        )
        latest = "- **3. Stand By for Zero's `TASK-23.17B` Summary:** Let Zero complete the review first."
        merged = reconcile_step_segments(earlier, latest, is_interrupted=True)
        self.assertEqual(
            merged,
            "### ⚾ Next Operations on Deck\n"
            "- **1. `TASK-23.18`:** Couple strikeout totals.\n"
            "- **2. 2027 Preseason Audit:** Generate leaderboard.\n"
            "- **3. Stand By for Zero's `TASK-23.17B` Summary:** Let Zero complete the review first.",
        )

    def test_suffix_prefix_overlap_deduplication(self):
        """When continuation backs up a few words before the cutoff point, deduplicate the overlap cleanly."""
        earlier = "The 3-Stage pitcher workload engine is locked, and the aces are back to"
        latest = "the aces are back to throwing 190 innings."
        merged = reconcile_step_segments(earlier, latest, is_interrupted=True)
        self.assertEqual(
            merged,
            "The 3-Stage pitcher workload engine is locked, and the aces are back to throwing 190 innings.",
        )


class TestDeliverTurnOutputInPlaceEdit(unittest.IsolatedAsyncioTestCase):
    @patch("tools.bridge_pipeline.sync_credentials")
    @patch("tools.bridge_pipeline.increment_bot_messages")
    @patch("tools.bridge_pipeline.increment_gif_turn", return_value=1)
    @patch("tools.bridge_pipeline.find_new_artifacts", return_value=[])
    async def test_existing_messages_edited_in_place_and_surplus_deleted(
        self, _mock_art, _mock_gif, _mock_inc, _mock_sync
    ):
        msg0 = MagicMock()
        msg0.edit = AsyncMock(return_value=msg0)
        msg0.delete = AsyncMock()

        msg1 = MagicMock()
        msg1.edit = AsyncMock(return_value=msg1)
        msg1.delete = AsyncMock()

        reply_target = MagicMock()
        reply_target.reply = AsyncMock()

        sent = await deliver_turn_output(
            output_text="Updated single-message consolidated response.",
            status_msg=None,
            reply_target=reply_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-123",
            turn_start_time=1000.0,
            existing_messages=[msg0, msg1],
        )

        self.assertEqual(len(sent), 1)
        msg0.edit.assert_awaited_once()
        msg1.delete.assert_awaited_once()
        reply_target.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
