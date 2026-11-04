package ventures.brock.gotchi.forwarder

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class GotchiBleBondingActivityTest {

    @Test
    fun `test SAVE and CLOSE button dispatches ACTION_SYNC_CONFIG with audio mode extras when relay enabled`() {
        val activityFile = File("src/main/java/ventures/brock/gotchi/forwarder/GotchiBleBondingActivity.kt")
        assertTrue("GotchiBleBondingActivity.kt should exist", activityFile.isFile)
        val src = activityFile.readText(Charsets.UTF_8)

        val dialogIdx = src.indexOf("fun showSettingsDialog()")
        assertTrue("showSettingsDialog() should exist", dialogIdx >= 0)
        val dialogSlice = src.substring(dialogIdx, minOf(src.length, dialogIdx + 1800))

        assertTrue(
            "SAVE & CLOSE handler should capture savePreferences() result",
            dialogSlice.contains("val audioMode = savePreferences()")
        )
        assertTrue(
            "SAVE & CLOSE handler should check KEY_RELAY_ENABLED",
            dialogSlice.contains("GotchiBleRelayService.KEY_RELAY_ENABLED")
        )
        assertTrue(
            "SAVE & CLOSE handler should set action = GotchiBleRelayService.ACTION_SYNC_CONFIG",
            dialogSlice.contains("action = GotchiBleRelayService.ACTION_SYNC_CONFIG")
        )
        assertTrue(
            "SAVE & CLOSE handler should include KEY_GATEWAY_URL extra",
            dialogSlice.contains("putExtra(GotchiBleRelayService.KEY_GATEWAY_URL, url)")
        )
        assertTrue(
            "SAVE & CLOSE handler should include KEY_BEARER_TOKEN extra",
            dialogSlice.contains("putExtra(GotchiBleRelayService.KEY_BEARER_TOKEN, token)")
        )
        assertTrue(
            "SAVE & CLOSE handler should include KEY_NODE_ID extra",
            dialogSlice.contains("putExtra(GotchiBleRelayService.KEY_NODE_ID, node)")
        )
        assertTrue(
            "SAVE & CLOSE handler should include KEY_AUDIO_OUT_MODE extra",
            dialogSlice.contains("putExtra(GotchiBleRelayService.KEY_AUDIO_OUT_MODE, audioMode)")
        )
    }
}
