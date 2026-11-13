package ventures.brock.gotchi.watch

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.util.Base64
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.Dns
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.ByteArrayOutputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL
import java.net.UnknownHostException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit
import kotlin.math.sqrt

/**
 * Tailscale/Cloudflare Fallback DNS Resolver for Wear OS OkHttpClient (parity with GotchiPhoneEngine).
 *
 * Ensures hostnames like `gotchi.brock.ventures` resolve reliably even when Wear OS Bluetooth-tethered
 * DNS, VPN DNS, or LTE resolver is unreachable or wedged.
 * Resolution chain:
 * 1. Standard Android system DNS (`Dns.SYSTEM`)
 * 2. Cloudflare (1.1.1.1) and Google (8.8.8.8) DNS-over-HTTPS (DoH) via raw IP
 * 3. Static bootstrap IP fallback (Cloudflare Anycast IPs for `gotchi.brock.ventures`)
 */
class FallbackDns(
    private val staticFallbacks: Map<String, List<String>> = mapOf(
        "gotchi.brock.ventures" to listOf("104.21.32.66", "172.67.184.64")
    )
) : Dns {
    override fun lookup(hostname: String): List<InetAddress> {
        // 1. Try system standard DNS first
        try {
            val addresses = Dns.SYSTEM.lookup(hostname)
            if (addresses.isNotEmpty()) {
                return addresses
            }
        } catch (ignored: Exception) {
            // System DNS failed on watch Wi-Fi / Bluetooth proxy / LTE
        }

        // 2. Query Cloudflare (1.1.1.1) or Google (8.8.8.8) DoH over raw IP without recursive DNS
        val dohAddresses = queryDoh(hostname)
        if (dohAddresses.isNotEmpty()) {
            return dohAddresses
        }

        // 3. Fall back to static bootstrap IPs
        val fallbackIps = staticFallbacks[hostname.lowercase()]
        if (!fallbackIps.isNullOrEmpty()) {
            val parsed = fallbackIps.mapNotNull { ipStr ->
                runCatching {
                    val parts = ipStr.split('.').map { it.toInt().toByte() }.toByteArray()
                    InetAddress.getByAddress(hostname, parts)
                }.getOrNull()
            }
            if (parsed.isNotEmpty()) {
                return parsed
            }
        }

        throw UnknownHostException("Unable to resolve $hostname via system DNS, DoH, or static fallback")
    }

    private fun queryDoh(hostname: String): List<InetAddress> {
        val dohUrls = listOf(
            "https://1.1.1.1/dns-query?name=$hostname&type=A",
            "https://8.8.8.8/resolve?name=$hostname&type=A"
        )
        for (urlStr in dohUrls) {
            try {
                val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
                    connectTimeout = 3000
                    readTimeout = 3000
                    setRequestProperty("Accept", "application/dns-json, application/json")
                    instanceFollowRedirects = false
                }
                if (conn.responseCode == 200) {
                    val body = conn.inputStream.bufferedReader().use { it.readText() }
                    val json = JSONObject(body)
                    val answers = json.optJSONArray("Answer")
                    val list = mutableListOf<InetAddress>()
                    if (answers != null) {
                        for (i in 0 until answers.length()) {
                            val item = answers.optJSONObject(i) ?: continue
                            if (item.optInt("type") == 1) { // A record
                                val ip = item.optString("data")
                                if (ip.isNotBlank()) {
                                    val parts = ip.split('.').map { it.toInt().toByte() }.toByteArray()
                                    list.add(InetAddress.getByAddress(hostname, parts))
                                }
                            }
                        }
                    }
                    if (list.isNotEmpty()) {
                        return list
                    }
                }
            } catch (ignored: Exception) {
                // Try next DoH provider
            }
        }
        return emptyList()
    }
}

/**
 * Wear OS Standalone Gotchi Engine for Google Pixel Watch (`ventures.brock.gotchi.watch`).
 *
 * Implements full wire-protocol parity with `backend/voice_gateway.py`, `firmware/`, and `GotchiPhoneEngine`:
 * - `16 kHz` 16-bit mono PCM mic capture with `500 ms` circular pre-roll ring buffer (8,000 samples / 16,000 bytes).
 * - `POST /api/voice/interact` multipart WAV upload + streaming SSE response parser:
 *   `state`, `transcript`, `reply`, `sentence` / `audio_chunk` (`24 kHz` PCM16), `done`, `error`.
 * - `/api/live/ws` RFC 6455 full-duplex Go Live streaming with hardware `AcousticEchoCanceler`
 *   and 300 ms sustained VAD barge-in (`{"type":"barge_in"}`).
 * - Remote Cloudflare Tunnel (`https://gotchi.brock.ventures`) + Bearer auth & `FallbackDns`, plus
 *   port `8771` LAN support (`http://gotchi-gateway.local:8771`) with automatic `:8765`/`:8769` -> `:8771` migration.
 * - Integration with `HeyZeroPreRollListener`: guarantees pre-roll `AudioRecord` is cleanly stopped/released
 *   before starting PTT, wake-word, or Go Live capture.
 */
class GotchiWatchEngine(
    private val onStateUpdate: (state: String?, transcript: String?, reply: String?, status: String?, audioBytes: Int, sampleRate: Int?) -> Unit
) {

    companion object {
        const val MIC_SAMPLE_RATE = 16000
        const val TTS_DEFAULT_SAMPLE_RATE = 24000
        const val PRE_ROLL_MS = 500
        const val PRE_ROLL_BYTES = (MIC_SAMPLE_RATE * 2 * PRE_ROLL_MS) / 1000 // 16,000 bytes
        const val DEFAULT_PTT_SILENCE_MS = 1500L
        const val DEFAULT_LIVE_SILENCE_MS = 15000L
        const val LIVE_VAD_HANGOVER_MS = 600L
        const val BARGE_IN_SPEECH_MS = 300L
        const val DEFAULT_RMS_THRESHOLD = 180.0
        const val DEFAULT_GATEWAY_PORT = 8771
        const val DEFAULT_GATEWAY_URL = "https://gotchi.brock.ventures"
        const val DEFAULT_LAN_GATEWAY_URL = "http://gotchi-gateway.local:8771"
        const val DEFAULT_NODE_ID = "gotchi-pixel-watch-01"
        const val DEFAULT_NOTIFY_POLL_INTERVAL_MS = 180000L // 3 minutes, matching desk unit

        const val ADPCM_MAGIC = "GAD1"
        const val ADPCM_HEADER_SIZE = 16

        /**
         * Normalizes gateway URLs, migrating legacy `:8765` and `:8769` ports to `:8771`
         * and defaulting blank strings to `DEFAULT_GATEWAY_URL` (`https://gotchi.brock.ventures`).
         */
        fun normalizeGatewayUrl(url: String): String {
            val trimmed = url.trim()
            if (trimmed.isEmpty()) {
                return DEFAULT_GATEWAY_URL
            }
            return trimmed
                .replace(":8765", ":8771")
                .replace(":8769", ":8771")
        }

        val IMA_INDEX_TABLE = intArrayOf(
            -1, -1, -1, -1, 2, 4, 6, 8,
            -1, -1, -1, -1, 2, 4, 6, 8
        )

        val IMA_STEP_TABLE = intArrayOf(
            7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
            19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
            50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
            130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
            337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
            876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
            2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
            5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
            15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
        )

        /**
         * Decompresses 4-bit IMA-ADPCM nibbles into 16-bit signed little-endian mono PCM.
         */
        fun decodeImaAdpcm(
            adpcmBytes: ByteArray,
            offset: Int = 0,
            numSamples: Int = (adpcmBytes.size - offset) * 2,
            initialPredictor: Int = 0,
            initialStepIndex: Int = 0
        ): ByteArray {
            if (adpcmBytes.size <= offset || numSamples <= 0) return ByteArray(0)
            val maxSamples = minOf(numSamples, (adpcmBytes.size - offset) * 2)
            val out = ByteBuffer.allocate(maxSamples * 2).order(ByteOrder.LITTLE_ENDIAN)
            var predictor = initialPredictor.coerceIn(-32768, 32767)
            var stepIndex = initialStepIndex.coerceIn(0, 88)
            var decodedCount = 0

            for (i in offset until adpcmBytes.size) {
                val b = adpcmBytes[i].toInt() and 0xFF
                for (shift in intArrayOf(0, 4)) {
                    if (decodedCount >= maxSamples) break
                    val nibble = (b ushr shift) and 0x0F
                    val step = IMA_STEP_TABLE[stepIndex]
                    var vpdiff = step shr 3
                    if ((nibble and 4) != 0) vpdiff += step
                    if ((nibble and 2) != 0) vpdiff += (step shr 1)
                    if ((nibble and 1) != 0) vpdiff += (step shr 2)
                    if ((nibble and 8) != 0) {
                        predictor -= vpdiff
                    } else {
                        predictor += vpdiff
                    }
                    predictor = predictor.coerceIn(-32768, 32767)
                    stepIndex = (stepIndex + IMA_INDEX_TABLE[nibble]).coerceIn(0, 88)
                    out.putShort(predictor.toShort())
                    decodedCount++
                }
                if (decodedCount >= maxSamples) break
            }
            return out.array()
        }

        /**
         * Unpacks a `GAD1` 16-byte IMA-ADPCM container (`magic="GAD1"`, `sample_rate: u32`,
         * `num_samples: u32`, `initial_predictor: i16`, `initial_step_index: u8`, `reserved: u8`),
         * raw ADPCM stream (`format == "adpcm"` or `"adpcm_ima"`), or standard RIFF WAV / raw PCM16.
         */
        fun decodeAudioPayload(
            raw: ByteArray,
            format: String = "pcm",
            defaultSampleRate: Int = TTS_DEFAULT_SAMPLE_RATE
        ): Pair<ByteArray, Int> {
            val fmt = format.trim().lowercase()
            if (raw.size >= 12 && String(raw, 0, 4, Charsets.US_ASCII) == ADPCM_MAGIC) {
                val hdr = ByteBuffer.wrap(raw, 4, minOf(12, raw.size - 4)).order(ByteOrder.LITTLE_ENDIAN)
                val parsedSr = hdr.int
                val numSamples = hdr.int
                val sr = if (parsedSr in 4000..96000) parsedSr else defaultSampleRate
                val adpcmBytesNeeded = (numSamples + 1) / 2
                return if (raw.size >= ADPCM_HEADER_SIZE && (raw.size - ADPCM_HEADER_SIZE) >= adpcmBytesNeeded) {
                    val initPred = hdr.short.toInt()
                    val initIdx = hdr.get().toInt() and 0xFF
                    val pcm = decodeImaAdpcm(raw, ADPCM_HEADER_SIZE, numSamples, initPred, initIdx)
                    Pair(pcm, sr)
                } else {
                    val pcm = decodeImaAdpcm(raw, 12, numSamples, 0, 0)
                    Pair(pcm, sr)
                }
            }
            if (fmt == "adpcm" || fmt == "adpcm_ima") {
                return Pair(decodeImaAdpcm(raw), defaultSampleRate)
            }
            return extractPcmFromWavOrRaw(raw, defaultSampleRate)
        }

        val NODE_ID_REGEX = Regex("^[A-Za-z0-9_-]{1,64}$")
        val VALID_AUDIO_OUT_MODES = setOf("speaker", "discord_voice", "ble_phone")

        fun validateNodeId(nodeId: String): Boolean = NODE_ID_REGEX.matches(nodeId)

        /**
         * Wrap raw 16-bit signed little-endian PCM bytes in a standard 44-byte RIFF WAV header.
         */
        fun wrapPcm16ToWav(pcmBytes: ByteArray, sampleRate: Int = MIC_SAMPLE_RATE, channels: Int = 1): ByteArray {
            val byteRate = sampleRate * channels * 2
            val blockAlign = channels * 2
            val dataSize = pcmBytes.size
            val buf = ByteBuffer.allocate(44 + dataSize).order(ByteOrder.LITTLE_ENDIAN)
            buf.put("RIFF".toByteArray(Charsets.US_ASCII))
            buf.putInt(36 + dataSize)
            buf.put("WAVE".toByteArray(Charsets.US_ASCII))
            buf.put("fmt ".toByteArray(Charsets.US_ASCII))
            buf.putInt(16)
            buf.putShort(1) // PCM format
            buf.putShort(channels.toShort())
            buf.putInt(sampleRate)
            buf.putInt(byteRate)
            buf.putShort(blockAlign.toShort())
            buf.putShort(16) // 16-bit
            buf.put("data".toByteArray(Charsets.US_ASCII))
            buf.putInt(dataSize)
            buf.put(pcmBytes)
            return buf.array()
        }

        /**
         * Walk RIFF/WAV chunks to the `data` subchunk (matching `parseWAVHeader` in firmware
         * and `extract_pcm_from_wav_or_raw` in `backend/voice_gateway.py`), or return raw PCM.
         */
        fun extractPcmFromWavOrRaw(raw: ByteArray, defaultSampleRate: Int = TTS_DEFAULT_SAMPLE_RATE): Pair<ByteArray, Int> {
            if (raw.size < 44) return Pair(raw, defaultSampleRate)
            val headerTag = String(raw, 0, 4, Charsets.US_ASCII)
            val waveTag = String(raw, 8, 4, Charsets.US_ASCII)
            if (headerTag != "RIFF" || waveTag != "WAVE") {
                return Pair(raw, defaultSampleRate)
            }
            var offset = 12
            var sampleRate = defaultSampleRate
            while (offset + 8 <= raw.size) {
                val chunkId = String(raw, offset, 4, Charsets.US_ASCII)
                val chunkSize = ByteBuffer.wrap(raw, offset + 4, 4).order(ByteOrder.LITTLE_ENDIAN).int
                val dataStart = offset + 8
                if (chunkSize < 0 || dataStart > raw.size) break
                if (chunkId == "fmt " && chunkSize >= 16 && dataStart + 8 <= raw.size) {
                    sampleRate = ByteBuffer.wrap(raw, dataStart + 4, 4).order(ByteOrder.LITTLE_ENDIAN).int
                } else if (chunkId == "data") {
                    val end = minOf(raw.size, dataStart + chunkSize)
                    return Pair(raw.copyOfRange(dataStart, end), sampleRate)
                }
                offset = dataStart + chunkSize + (chunkSize and 1)
            }
            return Pair(raw.copyOfRange(44, raw.size), sampleRate)
        }

        fun computePcm16Rms(pcmBytes: ByteArray, length: Int = pcmBytes.size): Double {
            val sampleCount = length / 2
            if (sampleCount <= 0) return 0.0
            var sumSq = 0.0
            val buf = ByteBuffer.wrap(pcmBytes, 0, sampleCount * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (i in 0 until sampleCount) {
                val s = buf.short.toDouble()
                sumSq += s * s
            }
            return sqrt(sumSq / sampleCount)
        }
    }

    data class Config(
        var gatewayUrl: String = DEFAULT_GATEWAY_URL,
        var bearerToken: String = "",
        var nodeId: String = DEFAULT_NODE_ID,
        var sessionId: String = "sess-watch-01",
        var audioOutMode: String = "speaker", // "speaker" | "discord_voice" | "ble_phone"
        var pttSilenceMs: Long = DEFAULT_PTT_SILENCE_MS,
        var liveSilenceMs: Long = DEFAULT_LIVE_SILENCE_MS,
        var liveBargeIn: Boolean = true,
        var heyZeroWakeEnabled: Boolean = false,
        var chimesEnabled: Boolean = true
    )

    val config = Config()
    var preRollListener: HeyZeroPreRollListener? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val httpClient = OkHttpClient.Builder()
        .dns(FallbackDns())
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    fun playChime(kind: String) {
        if (!config.chimesEnabled) return
        val pcm = GotchiChimeSynthesizer.synthesizeChimePcm16(kind)
        if (pcm.isEmpty()) return
        playPcmOnSpeaker(pcm, GotchiChimeSynthesizer.CHIME_SR)
    }

    private val preRollCapacityBytes = PRE_ROLL_BYTES
    private val preRollRing = ByteArray(preRollCapacityBytes)
    private var preRollWritePos = 0
    private var preRollFilled = false

    private val pendingBurstImages = mutableMapOf<Int, ByteArray>()
    private var recordingJob: Job? = null
    @Volatile
    private var isPttRecording = false
    @Volatile
    private var isPttCancelled = false
    private var activeLiveWs: WebSocket? = null
    @Volatile
    private var liveSessionActive = false
    private var activeAudioTrack: AudioTrack? = null
    private val audioPlaybackQueue = Channel<Pair<ByteArray, Int>>(Channel.UNLIMITED)
    private val playbackMutex = Mutex()
    private val playbackWorkerJob = scope.launch {
        for ((pcmBytes, sampleRate) in audioPlaybackQueue) {
            playPcmChunkSequential(pcmBytes, sampleRate)
        }
    }

    fun writePreRollBytes(chunk: ByteArray, len: Int = chunk.size) {
        for (i in 0 until len) {
            preRollRing[preRollWritePos] = chunk[i]
            preRollWritePos = (preRollWritePos + 1) % preRollCapacityBytes
            if (preRollWritePos == 0) preRollFilled = true
        }
    }

    fun snapshotPreRollBytes(): ByteArray {
        if (!preRollFilled) {
            return preRollRing.copyOfRange(0, preRollWritePos)
        }
        val out = ByteArray(preRollCapacityBytes)
        val tailLen = preRollCapacityBytes - preRollWritePos
        System.arraycopy(preRollRing, preRollWritePos, out, 0, tailLen)
        System.arraycopy(preRollRing, 0, out, tailLen, preRollWritePos)
        return out
    }

    fun attachSnapshot(jpegBytes: ByteArray, frameIndex: Int = 0) {
        playChime("snap")
        pendingBurstImages[frameIndex.coerceIn(0, 4)] = jpegBytes
        onStateUpdate(null, null, null, "SNAP #${frameIndex + 1} (${jpegBytes.size / 1024}KB)", 0, null)
    }

    /**
     * Releases any active HeyZeroPreRollListener AudioRecord and acquires a fresh, initialized
     * Wear OS AudioRecord instance with retry fallback across VOICE_COMMUNICATION / MIC sources.
     */
    @SuppressLint("MissingPermission")
    private suspend fun acquireWatchAudioRecord(minBuf: Int): AudioRecord? {
        preRollListener?.pauseForActiveTurn()
        val sources = intArrayOf(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.MIC
        )
        for (attempt in 0..1) {
            for (source in sources) {
                val rec = runCatching {
                    AudioRecord(
                        source,
                        MIC_SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT,
                        minBuf
                    )
                }.getOrNull()
                if (rec != null) {
                    if (rec.state == AudioRecord.STATE_INITIALIZED) {
                        return rec
                    }
                    runCatching { rec.release() }
                }
            }
            if (attempt == 0) {
                delay(50L)
            }
        }
        return null
    }

    /**
     * Start Push-to-Talk audio capture from watch microphone.
     */
    @SuppressLint("MissingPermission")
    fun startPttCapture(autoVadStop: Boolean = true) {
        if (isPttRecording) return
        if (liveSessionActive) {
            stopLiveSession()
            return
        }
        val handoffPreRoll = if (preRollListener?.isListening() == true) {
            preRollListener?.releaseAudioRecordForHandoff() ?: ByteArray(0)
        } else {
            preRollListener?.pauseForActiveTurn()
            ByteArray(0)
        }
        playChime("ptt_start")
        isPttRecording = true
        isPttCancelled = false
        onStateUpdate("listening", "", "", "PINCH REC 16kHz...", 0, null)

        recordingJob = scope.launch {
            val pcmStream = ByteArrayOutputStream()
            val preRoll = if (handoffPreRoll.isNotEmpty()) handoffPreRoll else snapshotPreRollBytes()
            if (preRoll.isNotEmpty()) pcmStream.write(preRoll)

            val minBuf = AudioRecord.getMinBufferSize(
                MIC_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(3200)

            val recorder = acquireWatchAudioRecord(minBuf)
            if (recorder == null) {
                isPttRecording = false
                playChime("error")
                onStateUpdate("error", null, null, "MIC UNAVAILABLE", 0, null)
                return@launch
            }

            val readBuf = ByteArray(1600) // 50ms @ 16kHz PCM16
            var lastVoiceMs = System.currentTimeMillis()
            var heardSpeech = false

            try {
                recorder.startRecording()
                while (isActive && isPttRecording && !isPttCancelled) {
                    val read = recorder.read(readBuf, 0, readBuf.size)
                    if (read > 0) {
                        pcmStream.write(readBuf, 0, read)
                        val rms = computePcm16Rms(readBuf, read)
                        if (rms >= DEFAULT_RMS_THRESHOLD) {
                            heardSpeech = true
                            lastVoiceMs = System.currentTimeMillis()
                        } else if (autoVadStop && heardSpeech &&
                            (System.currentTimeMillis() - lastVoiceMs) >= config.pttSilenceMs
                        ) {
                            isPttRecording = false
                            break
                        }
                    } else if (read < 0) {
                        isPttRecording = false
                        break
                    } else {
                        delay(25)
                    }
                }
            } finally {
                isPttRecording = false
                runCatching { recorder.stop(); recorder.release() }
            }

            if (isPttCancelled || !isActive) {
                return@launch
            }

            playChime("ptt_stop")
            val rawPcm = pcmStream.toByteArray()
            val wavBytes = wrapPcm16ToWav(rawPcm, MIC_SAMPLE_RATE, 1)
            val images = pendingBurstImages.toSortedMap()
            pendingBurstImages.clear()
            dispatchVoiceTurn(wavBytes = wavBytes, images = images)
        }
    }

    /**
     * Start turn capture triggered by 'Hey Zero' wake phrase, seamlessly prepending the
     * 500 ms circular pre-roll ring buffer into the active 16 kHz turn stream.
     */
    @SuppressLint("MissingPermission")
    fun startWakeWordTurn(preRollPcm: ByteArray) {
        if (isPttRecording) return
        if (liveSessionActive) {
            stopLiveSession()
        }
        preRollListener?.pauseForActiveTurn()
        playChime("ptt_start")
        isPttRecording = true
        isPttCancelled = false
        onStateUpdate("listening", "", "", "HEY ZERO // REC...", 0, null)

        recordingJob = scope.launch {
            val pcmStream = ByteArrayOutputStream()
            // Seamlessly prepend 500 ms pre-roll snapshot
            if (preRollPcm.isNotEmpty()) {
                pcmStream.write(preRollPcm)
            }

            val minBuf = AudioRecord.getMinBufferSize(
                MIC_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(3200)

            val recorder = acquireWatchAudioRecord(minBuf)
            if (recorder == null) {
                isPttRecording = false
                playChime("error")
                onStateUpdate("error", null, null, "MIC UNAVAILABLE", 0, null)
                return@launch
            }

            val readBuf = ByteArray(1600) // 50ms @ 16kHz PCM16
            var lastVoiceMs = System.currentTimeMillis()
            var heardSpeech = true // Wake phrase already spoken

            try {
                recorder.startRecording()
                while (isActive && isPttRecording && !isPttCancelled) {
                    val read = recorder.read(readBuf, 0, readBuf.size)
                    if (read > 0) {
                        pcmStream.write(readBuf, 0, read)
                        val rms = computePcm16Rms(readBuf, read)
                        if (rms >= DEFAULT_RMS_THRESHOLD) {
                            lastVoiceMs = System.currentTimeMillis()
                        } else if (heardSpeech &&
                            (System.currentTimeMillis() - lastVoiceMs) >= config.pttSilenceMs
                        ) {
                            isPttRecording = false
                            break
                        }
                    } else if (read < 0) {
                        isPttRecording = false
                        break
                    } else {
                        delay(25)
                    }
                }
            } finally {
                isPttRecording = false
                runCatching { recorder.stop(); recorder.release() }
            }

            if (isPttCancelled || !isActive) {
                return@launch
            }

            playChime("ptt_stop")
            val rawPcm = pcmStream.toByteArray()
            val wavBytes = wrapPcm16ToWav(rawPcm, MIC_SAMPLE_RATE, 1)
            val images = pendingBurstImages.toSortedMap()
            pendingBurstImages.clear()
            dispatchVoiceTurn(wavBytes = wavBytes, images = images)
        }
    }

    /**
     * Stop active Push-to-Talk recording and commit the captured audio turn.
     */
    fun stopPttAndCommit() {
        if (!isPttRecording) return
        isPttRecording = false
    }

    /**
     * Abort an active Push-to-Talk recording without uploading when the user flicks their wrist (ACTION_DISMISS).
     */
    fun cancelPttCapture() {
        if (!isPttRecording && recordingJob == null) return
        isPttRecording = false
        isPttCancelled = true
        recordingJob?.cancel()
        recordingJob = null
        pendingBurstImages.clear()
        playChime("error")
        onStateUpdate("idle", null, null, "DISMISSED", 0, null)
    }

    fun isRecording(): Boolean = isPttRecording

    /**
     * Trigger speech barge-in: immediately stops playback, flushes audio buffers,
     * dispatches `{"type":"barge_in"}` over Go Live WebSocket if active, and transitions state.
     */
    fun triggerBargeIn() {
        while (audioPlaybackQueue.tryReceive().isSuccess) {}
        runCatching {
            activeAudioTrack?.pause()
            activeAudioTrack?.flush()
            activeAudioTrack?.stop()
        }
        if (liveSessionActive && activeLiveWs != null) {
            val bargeMsg = JSONObject().put("type", "barge_in").toString()
            activeLiveWs?.send(bargeMsg)
        }
        onStateUpdate("listening", null, null, "BARGE-IN", 0, null)
    }

    fun dispatchStandaloneSnapshotTurn(images: Map<Int, ByteArray>, prompt: String? = null) {
        scope.launch {
            dispatchVoiceTurn(wavBytes = ByteArray(0), images = images, prompt = prompt)
        }
    }

    suspend fun dispatchVoiceTurn(
        wavBytes: ByteArray,
        images: Map<Int, ByteArray> = emptyMap(),
        prompt: String? = null,
        mode: String? = null
    ) {
        val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
        val safeAudioOut = if (config.audioOutMode in VALID_AUDIO_OUT_MODES) config.audioOutMode else "speaker"

        onStateUpdate("transcribing", null, null, "UPLOADING TURN...", 0, null)

        val multipart = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("node_id", safeNodeId)
            .addFormDataPart("session_id", config.sessionId)
            .addFormDataPart("audio_out_mode", safeAudioOut)

        if (!prompt.isNullOrBlank()) {
            multipart.addFormDataPart("prompt", prompt)
        }
        if (!mode.isNullOrBlank()) {
            multipart.addFormDataPart("mode", mode)
        }
        if (wavBytes.isNotEmpty()) {
            multipart.addFormDataPart(
                "audio",
                "ptt.wav",
                wavBytes.toRequestBody("audio/wav".toMediaType())
            )
        }
        if (images.size == 1 && images.containsKey(0)) {
            multipart.addFormDataPart(
                "image",
                "snap.jpg",
                images.getValue(0).toRequestBody("image/jpeg".toMediaType())
            )
        } else {
            for ((idx, jpeg) in images) {
                multipart.addFormDataPart(
                    "image_$idx",
                    "snap_$idx.jpg",
                    jpeg.toRequestBody("image/jpeg".toMediaType())
                )
            }
        }

        val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
        val reqBuilder = Request.Builder()
            .url("$baseUrl/api/voice/interact")
            .post(multipart.build())
        if (config.bearerToken.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
        }

        try {
            httpClient.newCall(reqBuilder.build()).execute().use { response ->
                if (!response.isSuccessful) {
                    playChime("error")
                    onStateUpdate("error", null, null, "HTTP ${response.code}", 0, null)
                    return
                }
                val body = response.body ?: run {
                    playChime("error")
                    return
                }
                val reader = BufferedReader(InputStreamReader(body.byteStream(), Charsets.UTF_8))
                var curEvent = "message"
                val dataLines = mutableListOf<String>()

                while (true) {
                    val line = reader.readLine() ?: break
                    if (line.isEmpty()) {
                        if (dataLines.isNotEmpty()) {
                            handleSseEvent(curEvent, dataLines.joinToString("\n"), safeAudioOut)
                            dataLines.clear()
                        }
                        curEvent = "message"
                    } else if (line.startsWith("event:")) {
                        curEvent = line.removePrefix("event:").trim()
                    } else if (line.startsWith("data:")) {
                        dataLines.add(line.removePrefix("data:").trim())
                    }
                }
            }
        } catch (e: Exception) {
            playChime("error")
            onStateUpdate("error", null, null, (e.message ?: "offline").take(24), 0, null)
        }
    }

    internal fun handleSseEvent(eventType: String, rawData: String, audioOutMode: String) {
        val obj = runCatching { JSONObject(rawData) }.getOrNull() ?: JSONObject()
        when (eventType) {
            "state" -> {
                val st = obj.optString("state", "idle")
                onStateUpdate(st, null, null, st.uppercase(), 0, null)
            }
            "transcript" -> {
                val tr = obj.optString("transcript", "")
                onStateUpdate(null, tr, null, "HEARD", 0, null)
            }
            "thinking" -> {
                val sec = obj.optInt("elapsed_seconds", 0)
                if (sec <= 2) {
                    playChime("thinking")
                }
                onStateUpdate("thinking", null, null, "THINKING (${sec}s)", 0, null)
            }
            "reply" -> {
                val rep = obj.optString("reply", "")
                onStateUpdate(null, null, rep, "SPEAKING", 0, null)
            }
            "sentence", "audio_chunk" -> {
                val b64 = when {
                    obj.has("audio_b64") -> obj.optString("audio_b64", "")
                    obj.has("pcm_b64") -> obj.optString("pcm_b64", "")
                    else -> obj.optString("data", "")
                }
                val fmt = obj.optString("format", "pcm")
                val defaultSr = obj.optInt("sample_rate", TTS_DEFAULT_SAMPLE_RATE)
                val text = obj.optString("text", "")
                if (text.isNotEmpty()) {
                    onStateUpdate(null, null, text, null, 0, null)
                }
                if (b64.isNotEmpty()) {
                    val decoded = Base64.decode(b64, Base64.DEFAULT)
                    val (pcmBytes, sr) = decodeAudioPayload(decoded, fmt, defaultSr)
                    onStateUpdate("speaking", null, null, "PLAYING ${sr / 1000}kHz", pcmBytes.size, sr)
                    if (audioOutMode != "discord_voice") {
                        playPcmOnSpeaker(pcmBytes, sr)
                    }
                }
            }
            "error" -> {
                playChime("error")
                val msg = obj.optString("message", obj.optString("error", "error"))
                onStateUpdate("error", null, null, msg.take(24), 0, null)
            }
            "done" -> {
                val dur = obj.optInt("duration_ms", 0)
                onStateUpdate("idle", null, null, "DONE (${dur}ms)", 0, null)
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun startLiveSession() {
        if (liveSessionActive) return
        if (isPttRecording) {
            cancelPttCapture()
        }
        preRollListener?.pauseForActiveTurn()
        liveSessionActive = true
        onStateUpdate("listening", "", "", "GO LIVE CONNECTING...", 0, null)

        val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
        val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
            .replace("http://", "ws://")
            .replace("https://", "wss://")
        val wsUrl = "$baseUrl/api/live/ws?node_id=$safeNodeId&session_id=${config.sessionId}"
        val reqBuilder = Request.Builder().url(wsUrl)
        if (config.bearerToken.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
        }

        activeLiveWs = httpClient.newWebSocket(reqBuilder.build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                onStateUpdate("listening", null, null, "GO LIVE CONNECTED", 0, null)
                startLiveMicLoop(webSocket)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = runCatching { JSONObject(text) }.getOrNull() ?: return
                val type = msg.optString("type", "")
                when (type) {
                    "transcript" -> onStateUpdate(null, msg.optString("text"), null, "LIVE HEARD", 0, null)
                    "reply" -> onStateUpdate(null, null, msg.optString("text"), "LIVE SPEAKING", 0, null)
                    "state" -> onStateUpdate(msg.optString("state", "idle"), null, null, null, 0, null)
                    "audio", "audio_chunk" -> {
                        val b64 = msg.optString("data", "")
                        val fmt = msg.optString("format", "pcm")
                        val defaultSr = msg.optInt("sample_rate", TTS_DEFAULT_SAMPLE_RATE)
                        if (b64.isNotEmpty()) {
                            val raw = Base64.decode(b64, Base64.DEFAULT)
                            val (pcm, sr) = decodeAudioPayload(raw, fmt, defaultSr)
                            playPcmOnSpeaker(pcm, sr)
                            onStateUpdate("speaking", null, null, "LIVE AUDIO ${sr / 1000}kHz", pcm.size, sr)
                        }
                    }
                    "error" -> onStateUpdate("error", null, null, msg.optString("message").take(24), 0, null)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onStateUpdate("error", null, null, "LIVE FAIL: ${t.message}".take(24), 0, null)
                liveSessionActive = false
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onStateUpdate("idle", null, null, "LIVE CLOSED", 0, null)
                liveSessionActive = false
            }
        })
    }

    @SuppressLint("MissingPermission")
    private fun startLiveMicLoop(ws: WebSocket) {
        scope.launch {
            val minBuf = AudioRecord.getMinBufferSize(
                MIC_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(3200)

            val recorder = acquireWatchAudioRecord(minBuf)
            if (recorder == null) {
                liveSessionActive = false
                playChime("error")
                onStateUpdate("error", null, null, "LIVE MIC UNAVAIL", 0, null)
                return@launch
            }

            // Attach hardware AcousticEchoCanceler if available on watch device
            if (AcousticEchoCanceler.isAvailable()) {
                runCatching {
                    AcousticEchoCanceler.create(recorder.audioSessionId)?.apply {
                        enabled = true
                    }
                }
            }

            val chunk = ByteArray(1600) // 50ms chunks @ 16kHz PCM16
            var speechStartTime = 0L
            var lastLiveVoiceMs = 0L

            try {
                recorder.startRecording()
                while (isActive && liveSessionActive) {
                    val read = recorder.read(chunk, 0, chunk.size)
                    if (read > 0) {
                        val rms = computePcm16Rms(chunk, read)
                        if (rms >= DEFAULT_RMS_THRESHOLD) {
                            lastLiveVoiceMs = System.currentTimeMillis()
                        }

                        // VAD radio gating: only transmit audio frames during speech or within 600 ms hangover window
                        if (rms >= DEFAULT_RMS_THRESHOLD || (System.currentTimeMillis() - lastLiveVoiceMs) <= LIVE_VAD_HANGOVER_MS) {
                            val b64Audio = Base64.encodeToString(chunk, 0, read, Base64.NO_WRAP)
                            val frame = JSONObject().apply {
                                put("type", "audio_input")
                                put("data", b64Audio)
                                put("sample_rate", MIC_SAMPLE_RATE)
                            }.toString()
                            ws.send(frame)
                        }

                        // 300 ms sustained VAD barge-in detection
                        if (config.liveBargeIn && rms >= DEFAULT_RMS_THRESHOLD) {
                            if (speechStartTime == 0L) {
                                speechStartTime = System.currentTimeMillis()
                            } else if ((System.currentTimeMillis() - speechStartTime) >= BARGE_IN_SPEECH_MS) {
                                val bargeInMsg = JSONObject().put("type", "barge_in").toString()
                                ws.send(bargeInMsg)
                                speechStartTime = 0L
                            }
                        } else {
                            speechStartTime = 0L
                        }
                    } else if (read < 0) {
                        break
                    } else {
                        delay(25)
                    }
                }
            } finally {
                runCatching { recorder.stop(); recorder.release() }
            }
        }
    }

    fun stopLiveSession() {
        if (!liveSessionActive) return
        liveSessionActive = false
        activeLiveWs?.close(1000, "user_stopped")
        activeLiveWs = null
        onStateUpdate("idle", null, null, "LIVE DISCONNECTED", 0, null)
    }

    fun isLiveActive(): Boolean = liveSessionActive

    private fun playPcmOnSpeaker(pcmBytes: ByteArray, sampleRate: Int = TTS_DEFAULT_SAMPLE_RATE) {
        audioPlaybackQueue.trySend(Pair(pcmBytes, sampleRate))
    }

    private suspend fun playPcmChunkSequential(pcmBytes: ByteArray, sampleRate: Int = TTS_DEFAULT_SAMPLE_RATE) {
        if (pcmBytes.isEmpty()) return
        playbackMutex.withLock {
            val minBuf = AudioTrack.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            )
            val track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build()
                )
                .setBufferSizeInBytes(maxOf(minBuf, pcmBytes.size))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()

            activeAudioTrack = track
            try {
                track.play()
                track.write(pcmBytes, 0, pcmBytes.size)
                val durationMs = (pcmBytes.size * 1000L) / (sampleRate * 2)
                delay(durationMs + 40L)
            } finally {
                runCatching {
                    track.stop()
                    track.release()
                }
                if (activeAudioTrack == track) {
                    activeAudioTrack = null
                }
            }
        }
    }

    @Volatile
    var hasPendingAnswer: Boolean = false
        private set

    @Volatile
    var pendingAnswerId: String = ""
        private set

    private var notifyPollJob: Job? = null

    /**
     * Start background polling of `/api/gotchi/notify` for async push-back notifications
     * (checks immediately once on resume, then every 3 minutes while foregrounded).
     */
    fun startNotificationPolling(
        intervalMs: Long = DEFAULT_NOTIFY_POLL_INTERVAL_MS,
        onPendingReady: ((statusText: String, autoSpeak: Boolean) -> Unit)? = null
    ) {
        notifyPollJob?.cancel()
        notifyPollJob = scope.launch {
            while (isActive) {
                if (!isPttRecording && !liveSessionActive) {
                    runCatching { pollPendingNotificationsOnce(onPendingReady) }
                }
                delay(intervalMs)
            }
        }
    }

    /**
     * Stop notification polling when the watch activity pauses or enters ambient/background state.
     */
    fun stopNotificationPolling() {
        notifyPollJob?.cancel()
        notifyPollJob = null
    }

    fun pollPendingNotificationsOnce(onPendingReady: ((statusText: String, autoSpeak: Boolean) -> Unit)? = null) {
        val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
        val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
        val reqBuilder = Request.Builder()
            .url("$baseUrl/api/gotchi/notify?node_id=$safeNodeId")
            .get()
        if (config.bearerToken.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
        }
        httpClient.newCall(reqBuilder.build()).execute().use { resp ->
            if (!resp.isSuccessful) return
            val bodyStr = resp.body?.string() ?: return
            val root = runCatching { JSONObject(bodyStr) }.getOrNull() ?: return
            val arr: JSONArray = root.optJSONArray("notifications") ?: return
            for (i in 0 until arr.length()) {
                val n = arr.optJSONObject(i) ?: continue
                val notifyId = n.optString("notify_id", "")
                val statusText = n.optString("status_text", "ANSWER READY")
                val hasAudio = n.optBoolean("has_pending_audio", false)
                val ansId = n.optString("answer_id", "")
                val autoSpeak = n.optBoolean("auto_speak", false)

                if (notifyId.isNotBlank()) {
                    ackNotification(safeNodeId, notifyId)
                }
                if (hasAudio) {
                    hasPendingAnswer = true
                    pendingAnswerId = ansId
                    playChime("ready")
                    onStateUpdate("idle", null, null, statusText, 0, null)
                    onPendingReady?.invoke(statusText, autoSpeak)
                    if (autoSpeak) {
                        fetchAndPlayPendingAnswer()
                    }
                    break
                }
            }
        }
    }

    private fun ackNotification(nodeId: String, notifyId: String) {
        val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
        val payload = JSONObject()
            .put("node_id", nodeId)
            .put("notify_id", notifyId)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val reqBuilder = Request.Builder()
            .url("$baseUrl/api/gotchi/notify/ack")
            .post(payload)
        if (config.bearerToken.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
        }
        runCatching { httpClient.newCall(reqBuilder.build()).execute().close() }
    }

    /**
     * Fetch and stream the cached pending answer from `GET /api/gotchi/pending_answer`
     * (retained for 2 hours in SQLite on the gateway), then acknowledge via
     * `POST /api/gotchi/pending_answer/ack`.
     */
    fun fetchAndPlayPendingAnswer() {
        scope.launch {
            val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
            val ansId = pendingAnswerId
            val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
            val url = if (ansId.isNotBlank()) {
                "$baseUrl/api/gotchi/pending_answer?node_id=$safeNodeId&answer_id=$ansId"
            } else {
                "$baseUrl/api/gotchi/pending_answer?node_id=$safeNodeId"
            }
            val reqBuilder = Request.Builder().url(url).get()
            if (config.bearerToken.isNotBlank()) {
                reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
            }

            try {
                httpClient.newCall(reqBuilder.build()).execute().use { response ->
                    if (!response.isSuccessful) {
                        hasPendingAnswer = false
                        pendingAnswerId = ""
                        playChime("error")
                        onStateUpdate("idle", null, null, "NO PENDING ANS", 0, null)
                        return@launch
                    }
                    val body = response.body ?: run {
                        playChime("error")
                        return@launch
                    }
                    val reader = BufferedReader(InputStreamReader(body.byteStream(), Charsets.UTF_8))
                    var curEvent = "message"
                    val dataLines = mutableListOf<String>()

                    while (true) {
                        val line = reader.readLine() ?: break
                        if (line.isEmpty()) {
                            if (dataLines.isNotEmpty()) {
                                handleSseEvent(curEvent, dataLines.joinToString("\n"), config.audioOutMode)
                                dataLines.clear()
                            }
                            curEvent = "message"
                        } else if (line.startsWith("event:")) {
                            curEvent = line.removePrefix("event:").trim()
                        } else if (line.startsWith("data:")) {
                            dataLines.add(line.removePrefix("data:").trim())
                        }
                    }
                }
                ackPendingAnswer(safeNodeId, ansId)
            } catch (e: Exception) {
                playChime("error")
                onStateUpdate("error", null, null, (e.message ?: "pending err").take(24), 0, null)
            } finally {
                hasPendingAnswer = false
                pendingAnswerId = ""
            }
        }
    }

    /**
     * Dismiss/skip a pending async answer on hold without playing it (`POST /api/gotchi/pending_answer/ack`).
     */
    fun skipPendingAnswer() {
        val ansId = pendingAnswerId
        hasPendingAnswer = false
        pendingAnswerId = ""
        onStateUpdate("idle", null, null, "SKIPPED ANSWER", 0, null)
        scope.launch {
            val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
            ackPendingAnswer(safeNodeId, ansId)
        }
    }

    private fun ackPendingAnswer(nodeId: String, answerId: String) {
        val baseUrl = normalizeGatewayUrl(config.gatewayUrl).trimEnd('/')
        val obj = JSONObject().put("node_id", nodeId)
        if (answerId.isNotBlank()) obj.put("answer_id", answerId)
        val body = obj.toString().toRequestBody("application/json".toMediaType())
        val reqBuilder = Request.Builder()
            .url("$baseUrl/api/gotchi/pending_answer/ack")
            .post(body)
        if (config.bearerToken.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer ${config.bearerToken}")
        }
        runCatching { httpClient.newCall(reqBuilder.build()).execute().close() }
    }

    fun shutdown() {
        notifyPollJob?.cancel()
        cancelPttCapture()
        stopLiveSession()
        while (audioPlaybackQueue.tryReceive().isSuccess) {}
        playbackWorkerJob.cancel()
        runCatching {
            activeAudioTrack?.pause()
            activeAudioTrack?.flush()
            activeAudioTrack?.stop()
            activeAudioTrack?.release()
        }
        scope.cancel()
    }
}
