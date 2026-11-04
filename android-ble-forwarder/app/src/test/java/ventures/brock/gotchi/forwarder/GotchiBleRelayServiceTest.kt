package ventures.brock.gotchi.forwarder

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class GotchiBleRelayServiceTest {

    @Test
    fun `test BleFramingCodec fragment and reassemble roundtrip`() {
        val payload = "Hello Gotchi v11 SMP Bonding & mDeviceBusy Fix".toByteArray(Charsets.UTF_8)
        val (packets, nextSeq) = BleFramingCodec.fragmentPayload(
            frameType = BleFramingCodec.FrameType.SSE_EVENT,
            flags = BleFramingCodec.FLAG_NONE,
            startSeqNum = 10,
            payload = payload,
            maxPayload = 16
        )
        assertTrue(packets.size >= 3)
        assertEquals(10 + packets.size, nextSeq)

        val reassembler = BleReassembler()
        var completed: BleReassembler.CompletedMessage? = null
        for (pkt in packets) {
            completed = reassembler.pushPacket(pkt)
        }
        assertNotNull(completed)
        assertEquals(BleFramingCodec.FrameType.SSE_EVENT, completed!!.frameType)
        assertArrayEquals(payload, completed.payload)
    }

    @Test
    fun `test SMP bonding wait and mDeviceBusy constants and reflection`() {
        assertEquals(1200L, GotchiBleRelayService.SMP_INITIAL_SETTLE_DELAY_MS)
        assertEquals(20000L, GotchiBleRelayService.SMP_BONDING_MAX_WAIT_MS)
        assertEquals(1200L, GotchiBleRelayService.SMP_POST_BOND_DISC_SETTLE_DELAY_MS)
        assertEquals(3000L, GotchiBleRelayService.HANDSHAKE_RETRY_INTERVAL_MS)
        assertEquals(6, GotchiBleRelayService.HANDSHAKE_MAX_ATTEMPTS)

        val service = GotchiBleRelayService()
        assertFalse(service.clearDeviceBusyFlag(null))
    }

    @Test
    fun `test ACTION_SYNC_CONFIG constant and pushAudioOutModeToGotchi returns false when unsubscribed`() {
        assertEquals(
            "ventures.brock.gotchi.forwarder.ACTION_SYNC_CONFIG",
            GotchiBleRelayService.ACTION_SYNC_CONFIG
        )
        GotchiBleRelayService.isTxSubscribed = false
        val service = GotchiBleRelayService()
        assertFalse(service.pushAudioOutModeToGotchi("ble_phone"))
        assertFalse(service.pushAudioOutModeToGotchi("speaker"))
    }

    @Test
    fun `test BLE CONTROL set_config frame encoding and source contract`() {
        val jsonPayload = """{"cmd":"set_config","audio_out_mode":"ble_phone"}""".toByteArray(Charsets.UTF_8)
        val (packets, _) = BleFramingCodec.fragmentPayload(
            frameType = BleFramingCodec.FrameType.CONTROL,
            flags = BleFramingCodec.FLAG_NONE,
            startSeqNum = 1,
            payload = jsonPayload,
            maxPayload = BleFramingCodec.DEFAULT_MAX_PAYLOAD
        )
        assertEquals(1, packets.size)
        val reassembler = BleReassembler()
        val msg = reassembler.pushPacket(packets[0])
        assertNotNull(msg)
        assertEquals(BleFramingCodec.FrameType.CONTROL, msg!!.frameType)
        val decoded = String(msg.payload, Charsets.UTF_8)
        assertTrue(decoded.contains("\"cmd\":\"set_config\""))
        assertTrue(decoded.contains("\"audio_out_mode\":\"ble_phone\""))

        val serviceFile = File("src/main/java/ventures/brock/gotchi/forwarder/GotchiBleRelayService.kt")
        assertTrue("GotchiBleRelayService.kt should exist", serviceFile.isFile)
        val src = serviceFile.readText(Charsets.UTF_8)

        assertTrue(src.contains("const val ACTION_SYNC_CONFIG = \"ventures.brock.gotchi.forwarder.ACTION_SYNC_CONFIG\""))
        assertTrue(src.contains("internal fun pushAudioOutModeToGotchi(modeOverride: String? = null): Boolean"))
        assertTrue(src.contains(".put(\"cmd\", \"set_config\")"))
        assertTrue(src.contains(".put(\"audio_out_mode\", mode)"))
        assertTrue(src.contains("sendBleFrame(BleFramingCodec.FrameType.CONTROL, jsonBytes)"))
        assertTrue(src.contains("delay(350)"))
        assertTrue(src.contains("pushAudioOutModeToGotchi()"))
        assertTrue(src.contains("if (prefAudioOutMode == \"ble_phone\")"))
    }

    @Test
    fun `test source contract for v11 mDeviceBusy and SMP bonding handshake`() {
        val serviceFile = File("src/main/java/ventures/brock/gotchi/forwarder/GotchiBleRelayService.kt")
        assertTrue("GotchiBleRelayService.kt should exist", serviceFile.isFile)
        val src = serviceFile.readText(Charsets.UTF_8)

        // (a) No setPreferredPhy or requestMtu in onConnectionStateChange(STATE_CONNECTED)
        assertFalse("Should not call setPreferredPhy in onConnectionStateChange", src.contains("gatt.setPreferredPhy("))

        // (b) Wait 1200ms after STATE_CONNECTED, wait up to 20s for BOND_BONDING -> BOND_BONDED, then 1200ms for bta_dm_disc_gatt
        assertTrue(src.contains("SMP_INITIAL_SETTLE_DELAY_MS = 1200L"))
        assertTrue(src.contains("SMP_BONDING_MAX_WAIT_MS = 20000L"))
        assertTrue(src.contains("SMP_POST_BOND_DISC_SETTLE_DELAY_MS = 1200L"))
        assertTrue(src.contains("BluetoothDevice.BOND_BONDING"))

        // (c) Clear wedged mDeviceBusy via reflection
        assertTrue(src.contains("getDeclaredField(\"mDeviceBusy\")"))
        assertTrue(src.contains("setBoolean("))
        assertTrue(src.contains("clearDeviceBusyFlag(gatt)"))

        // (d) discoverServices FIRST -> onServicesDiscovered writes CCCD -> onDescriptorWrite sets isTxSubscribed = true & calls requestMtu(512)
        val onDescIdx = src.indexOf("override fun onDescriptorWrite")
        assertTrue(onDescIdx > 0)
        val onDescSlice = src.substring(onDescIdx, minOf(src.length, onDescIdx + 900))
        assertTrue(onDescSlice.contains("isTxSubscribed = true"))
        assertTrue(onDescSlice.contains("gatt.requestMtu(BleFramingCodec.DEFAULT_GATT_MTU)"))

        // (e) AndroidManifest exported="true" on GotchiBleRelayService & versionCode 11
        val manifestFile = File("src/main/AndroidManifest.xml")
        assertTrue(manifestFile.isFile)
        val manifestXml = manifestFile.readText(Charsets.UTF_8)
        assertTrue(
            "GotchiBleRelayService must have android:exported=\"true\"",
            Regex("""<service\s+android:name="\.GotchiBleRelayService"[\s\S]*?android:exported="true"""").containsMatchIn(manifestXml)
        )

        val gradleFile = File("build.gradle.kts")
        assertTrue(gradleFile.isFile)
        val gradleSrc = gradleFile.readText(Charsets.UTF_8)
        assertTrue(gradleSrc.contains("versionCode = 11"))
    }
}
