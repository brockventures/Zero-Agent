package ventures.brock.gotchi.forwarder

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Base64
import android.util.Log
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
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt
import kotlin.math.tanh

/**
 * Unified Gotchi Phone Engine for Pixel Phone (`ventures.brock.gotchi.forwarder`).
 *
 * Implements full wire-protocol parity with `backend/voice_gateway.py`, `firmware/`, and `android-gotchi-watch`:
 * - `16 kHz` 16-bit mono PCM mic capture with `500 ms` circular pre-roll ring buffer (8,000 samples / 16,000 bytes).
 * - `POST /api/voice/interact` multipart WAV upload + optional camera JPEG attachment (`imageBytes: ByteArray? = null`)
 *   + streaming SSE response parser: `state`, `transcript`, `reply`, `sentence` / `audio_chunk` (`24 kHz` PCM16 or ADPCM/GAD1), `done`, `error`.
 * - Full-duplex `/api/live/ws` Go Live WebSocket mode with hardware `AcousticEchoCanceler` and 300 ms VAD barge-in (`{"type":"barge_in"}`).
 * - Smart wake-word arbitration checking `!GotchiBleRelayService.isRelayTurnActive`: suppresses/pauses local wake-word
 *   interactions when the desk Gotchi unit is actively streaming through the phone.
 * - 24 kHz tactile studio chimes via integrated [GotchiChimeSynthesizer].
 * - Integrated [HeyZeroPreRollListener] continuous wake-phrase spotter ("hey zero", "hay zero", "hi zero", "okay zero").
 */
class GotchiPhoneEngine(
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
        const val DEFAULT_RMS_THRESHOLD = 50.0
        const val DEFAULT_GATEWAY_URL = "http://gotchi.local:8765"
        const val DEFAULT_NODE_ID = "gotchi-pixel-phone-01"
        const val DEFAULT_NOTIFY_POLL_INTERVAL_MS = 180000L // 3 minutes

        const val ADPCM_MAGIC = "GAD1"
        const val ADPCM_HEADER_SIZE = 16

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
                val nibbles = intArrayOf(b and 0x0F, (b ushr 4) and 0x0F)
                for (nibble in nibbles) {
                    val step = IMA_STEP_TABLE[stepIndex]
                    var vpdiff = step shr 3
                    if ((nibble and 1) != 0) vpdiff += step shr 2
                    if ((nibble and 2) != 0) vpdiff += step shr 1
                    if ((nibble and 4) != 0) vpdiff += step
                    if ((nibble and 8) != 0) {
                        predictor -= vpdiff
                    } else {
                        predictor += vpdiff
                    }
                    predictor = predictor.coerceIn(-32768, 32767)
                    stepIndex = (stepIndex + IMA_INDEX_TABLE[nibble]).coerceIn(0, 88)
                    out.putShort(predictor.toShort())
                    decodedCount++
                    if (decodedCount >= maxSamples) break
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
        var sessionId: String = "sess-phone-01",
        var audioOutMode: String = "speaker", // "speaker" | "discord_voice" | "ble_phone"
        var pttSilenceMs: Long = DEFAULT_PTT_SILENCE_MS,
        var liveSilenceMs: Long = DEFAULT_LIVE_SILENCE_MS,
        var liveBargeIn: Boolean = true,
        var heyZeroWakeEnabled: Boolean = false,
        var chimesEnabled: Boolean = true
    )

    val config = Config()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    var preRollListener: HeyZeroPreRollListener? = null

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
    private var isPttRecording = false
    private var isPttCancelled = false
    private var activeLiveWs: WebSocket? = null
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
     * Start Push-to-Talk audio capture from phone microphone.
     */
    @SuppressLint("MissingPermission")
    fun startPttCapture(autoVadStop: Boolean = true) {
        if (isPttRecording) return
        if (liveSessionActive) {
            stopLiveSession()
            return
        }
        playChime("ptt_start")
        isPttRecording = true
        isPttCancelled = false
        onStateUpdate("listening", "", "", "PTT REC 16kHz...", 0, null)

        recordingJob = scope.launch {
            val pcmStream = ByteArrayOutputStream()
            val preRoll = snapshotPreRollBytes()
            if (preRoll.isNotEmpty()) pcmStream.write(preRoll)

            val minBuf = AudioRecord.getMinBufferSize(
                MIC_SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT
            ).coerceAtLeast(3200)

            val recorder = runCatching {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    MIC_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    minBuf
                )
            }.getOrNull()

            val readBuf = ByteArray(1600) // 50ms @ 16kHz PCM16
            var lastVoiceMs = System.currentTimeMillis()
            var heardSpeech = false

            try {
                recorder?.startRecording()
                while (isActive && isPttRecording && !isPttCancelled) {
                    val read = recorder?.read(readBuf, 0, readBuf.size) ?: -1
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
                    } else {
                        delay(25)
                    }
                }
            } finally {
                runCatching { recorder?.stop(); recorder?.release() }
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
     *
     * Smart wake-word arbitration: checks `!GotchiBleRelayService.isRelayTurnActive` to pause
     * when the physical desk Gotchi is actively streaming through the phone.
     */
    @SuppressLint("MissingPermission")
    fun startWakeWordTurn(preRollPcm: ByteArray): Boolean {
        if (GotchiBleRelayService.isRelayTurnActive) {
            Log.d("GotchiPhoneEngine", "Wake-word ignored: GotchiBleRelayService.isRelayTurnActive is true")
            return false
        }
        if (isPttRecording) return false
        if (liveSessionActive) {
            stopLiveSession()
        }
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

            val recorder = runCatching {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    MIC_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    minBuf
                )
            }.getOrNull()

            val readBuf = ByteArray(1600) // 50ms @ 16kHz PCM16
            var lastVoiceMs = System.currentTimeMillis()
            val heardSpeech = true // Wake phrase already spoken

            try {
                recorder?.startRecording()
                while (isActive && isPttRecording && !isPttCancelled) {
                    val read = recorder?.read(readBuf, 0, readBuf.size) ?: -1
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
                    } else {
                        delay(25)
                    }
                }
            } finally {
                runCatching { recorder?.stop(); recorder?.release() }
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
        return true
    }

    /**
     * Stop active Push-to-Talk recording and commit the captured audio turn,
     * optionally attaching a camera snapshot JPEG.
     */
    fun stopPttAndCommit(imageBytes: ByteArray? = null) {
        if (imageBytes != null) {
            pendingBurstImages[0] = imageBytes
        }
        if (!isPttRecording) return
        isPttRecording = false
    }

    /**
     * Abort an active Push-to-Talk recording without uploading.
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

    fun dispatchStandaloneSnapshotTurn(
        images: Map<Int, ByteArray> = emptyMap(),
        imageBytes: ByteArray? = null,
        prompt: String? = null
    ) {
        scope.launch {
            val allImages = images.toMutableMap()
            if (imageBytes != null && !allImages.containsKey(0)) {
                allImages[0] = imageBytes
            }
            dispatchVoiceTurn(wavBytes = ByteArray(0), images = allImages, prompt = prompt)
        }
    }

    suspend fun dispatchVoiceTurn(
        wavBytes: ByteArray,
        images: Map<Int, ByteArray> = emptyMap(),
        imageBytes: ByteArray? = null,
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

        val allImages = images.toMutableMap()
        if (imageBytes != null && !allImages.containsKey(0)) {
            allImages[0] = imageBytes
        }

        if (allImages.size == 1 && allImages.containsKey(0)) {
            multipart.addFormDataPart(
                "image",
                "snap.jpg",
                allImages.getValue(0).toRequestBody("image/jpeg".toMediaType())
            )
        } else {
            for ((idx, jpeg) in allImages) {
                multipart.addFormDataPart(
                    "image_$idx",
                    "snap_$idx.jpg",
                    jpeg.toRequestBody("image/jpeg".toMediaType())
                )
            }
        }

        val baseUrl = config.gatewayUrl.trimEnd('/')
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
        liveSessionActive = true
        onStateUpdate("listening", "", "", "GO LIVE CONNECTING...", 0, null)

        val baseUrl = config.gatewayUrl.trimEnd('/').replace("http://", "ws://").replace("https://", "wss://")
        val wsUrl = "$baseUrl/api/live/ws?node_id=${config.nodeId}&session_id=${config.sessionId}"
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

            val recorder = runCatching {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                    MIC_SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    minBuf
                )
            }.getOrNull()

            // Attach hardware AcousticEchoCanceler if available on phone
            if (recorder != null && AcousticEchoCanceler.isAvailable()) {
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
                recorder?.startRecording()
                while (isActive && liveSessionActive) {
                    val read = recorder?.read(chunk, 0, chunk.size) ?: -1
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
                    } else {
                        delay(25)
                    }
                }
            } finally {
                runCatching { recorder?.stop(); recorder?.release() }
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
     * Stop notification polling.
     */
    fun stopNotificationPolling() {
        notifyPollJob?.cancel()
        notifyPollJob = null
    }

    fun pollPendingNotificationsOnce(onPendingReady: ((statusText: String, autoSpeak: Boolean) -> Unit)? = null) {
        val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
        val baseUrl = config.gatewayUrl.trimEnd('/')
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
        val baseUrl = config.gatewayUrl.trimEnd('/')
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
     * Fetch and stream the cached pending answer from `GET /api/gotchi/pending_answer`.
     */
    fun fetchAndPlayPendingAnswer() {
        scope.launch {
            val safeNodeId = if (validateNodeId(config.nodeId)) config.nodeId else DEFAULT_NODE_ID
            val ansId = pendingAnswerId
            val baseUrl = config.gatewayUrl.trimEnd('/')
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
     * Dismiss/skip a pending async answer on hold without playing it.
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
        val baseUrl = config.gatewayUrl.trimEnd('/')
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

    fun initWakeWordListener(context: Context): HeyZeroPreRollListener {
        val listener = HeyZeroPreRollListener(context) { preRollPcm ->
            startWakeWordTurn(preRollPcm)
        }
        preRollListener = listener
        return listener
    }

    fun shutdown() {
        notifyPollJob?.cancel()
        preRollListener?.destroy()
        preRollListener = null
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

/**
 * 1C-2A-2 Reed & Shutter Audio Chime Synthesizer for Gotchi.
 *
 * Direct port of 24 kHz DSP tactile studio sound generator from `firmware/src/audio_hal.cpp`:
 * - 6 tactile studio chimes: "ptt_start", "ptt_stop", "snap", "thinking", "ready", "error".
 * - Casio/Wurlitzer reed oscillator waveform (`casioReedWave`).
 * - Mechanical switch latch click (`addSwitchClick`).
 * - Smooth ADSR reed note envelope with vibrato (`addReedNoteSmooth`).
 * - 35mm SLR focal plane curtain shutter impacts (`addShutterImpact`, `addCurtainZip`, `addSpringRingTail`).
 * - Two-pole 300 Hz high-pass filter (`alpha = 0.924f`) with 24,000 LSB peak ceiling to avoid rail sag.
 */
object GotchiChimeSynthesizer {

    const val CHIME_SR = 24000
    const val MAX_CHIME_SAMPLES = 20400

    val VALID_CHIME_KINDS = setOf(
        "ready",
        "ptt_start",
        "ptt_stop",
        "snap",
        "thinking",
        "error"
    )

    class SimpleRng(seed: Long) {
        var state: Int = if (seed != 0L) seed.toInt() else 1

        fun uniformSigned(): Float {
            // Xorshift32 deterministic PRNG -> [-1.0f, 1.0f]
            var x = state
            x = x xor (x shl 13)
            x = x xor (x ushr 17)
            x = x xor (x shl 5)
            state = x
            val masked = x and 0x00FFFFFF
            return (masked.toFloat() / 8388607.5f) - 1.0f
        }
    }

    class BiquadBp {
        var b0: Float = 0.0f
        var b1: Float = 0.0f
        var b2: Float = 0.0f
        var a1: Float = 0.0f
        var a2: Float = 0.0f
        var x1: Float = 0.0f
        var x2: Float = 0.0f
        var y1: Float = 0.0f
        var y2: Float = 0.0f

        fun init(centerHz: Float, q: Float) {
            val w0 = (2.0 * Math.PI * centerHz.toDouble() / CHIME_SR.toDouble()).toFloat()
            val alpha = (sin(w0.toDouble()) / (2.0 * q.toDouble())).toFloat()
            val a0 = 1.0f + alpha
            b0 = alpha / a0
            b1 = 0.0f
            b2 = -alpha / a0
            a1 = (-2.0 * cos(w0.toDouble()) / a0.toDouble()).toFloat()
            a2 = (1.0f - alpha) / a0
            x1 = 0.0f
            x2 = 0.0f
            y1 = 0.0f
            y2 = 0.0f
        }

        fun step(x0: Float): Float {
            val y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2 = x1
            x1 = x0
            y2 = y1
            y1 = y0
            return y0
        }
    }

    fun casioReedWave(phase: Float, brightness: Float = 1.0f): Float {
        val p = phase.toDouble()
        val b = brightness.toDouble()
        val wave = 1.00 * sin(p) +
            0.38 * b * sin(2.0 * p + 0.3) +
            0.46 * b * sin(3.0 * p) +
            0.14 * b * sin(4.0 * p) +
            0.20 * b * sin(5.0 * p)
        return (wave * 0.62).toFloat()
    }

    fun addSwitchClick(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float = 7.0f,
        freq: Float = 2300.0f,
        bodyFreq: Float = 850.0f,
        amp: Float = 0.92f,
        metallic: Float = 0.45f
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val env = exp(-frac * 6.0f)
            val noise = sin((i.toFloat() * 2.31f + cos(i.toFloat() * 1.13f) * 4.0f).toDouble()).toFloat()
            val t = i.toFloat() / CHIME_SR.toFloat()
            val snap = sin((2.0 * Math.PI * freq.toDouble() * t.toDouble())).toFloat() +
                0.55f * sin((2.0 * Math.PI * (freq * 1.58f).toDouble() * t.toDouble())).toFloat()
            val body = sin((2.0 * Math.PI * bodyFreq.toDouble() * t.toDouble())).toFloat() * exp(-frac * 3.5f)
            val s = (0.45f * body + (1.0f - metallic) * 0.55f * snap + metallic * 0.45f * noise) * env * amp * 28500.0f
            mixBuf[idx0 + i] += s
        }
    }

    fun addReedNoteSmooth(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        freq: Float,
        ms: Float,
        amp: Float = 0.68f,
        clickMs: Float = 6.5f,
        clickFreq: Float = 2400.0f,
        clickLeadMs: Float = 8.0f,
        attackMs: Float = 6.0f,
        decayTau: Float = 0.36f,
        releaseMs: Float = 45.0f,
        vibratoHz: Float = 5.2f,
        vibratoDepth: Float = 0.006f
    ) {
        if (clickMs > 0.0f) {
            addSwitchClick(mixBuf, maxLen, startSec, clickMs, clickFreq, 850.0f, 0.90f, 0.45f)
        }
        val leadN = (CHIME_SR * (clickLeadMs / 1000.0f)).toInt()
        val idx0 = (startSec * CHIME_SR).toInt() + leadN
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val attN = (CHIME_SR * (attackMs / 1000.0f)).toInt().coerceAtLeast(1)
        val relN = (CHIME_SR * (releaseMs / 1000.0f)).toInt().coerceAtLeast(1)
        val sustN = if (n > relN) n - relN else 0

        var phase = 0.0
        val baseW = 2.0 * Math.PI * freq.toDouble() / CHIME_SR.toDouble()

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            var env: Double
            if (i < attN) {
                val r = i.toDouble() / attN.toDouble()
                env = 0.5 * (1.0 - cos(Math.PI * r))
            } else if (i < sustN) {
                val tSust = (i - attN).toDouble() / CHIME_SR.toDouble()
                env = 1.0 - (1.0 - exp(-tSust / decayTau.toDouble())) * 0.18
            } else {
                val r = (i - sustN).toDouble() / relN.toDouble()
                val tEnd = (sustN - attN).coerceAtLeast(0).toDouble() / CHIME_SR.toDouble()
                val sustEndEnv = 1.0 - (1.0 - exp(-tEnd / decayTau.toDouble())) * 0.18
                env = sustEndEnv * 0.5 * (1.0 + cos(Math.PI * r.coerceIn(0.0, 1.0)))
            }
            val tSec = i.toDouble() / CHIME_SR.toDouble()
            val vib = 1.0 + vibratoDepth.toDouble() * sin(2.0 * Math.PI * vibratoHz.toDouble() * tSec)
            phase += baseW * vib
            if (phase > 2.0 * Math.PI) phase -= 2.0 * Math.PI
            val sample = casioReedWave(phase.toFloat(), 1.0f) * env.toFloat() * amp * 30000.0f
            mixBuf[idx0 + i] += sample
        }
    }

    fun addShutterImpact(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        startHz: Float,
        endHz: Float,
        snapFreq: Float,
        bodyFreq: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val rng = SimpleRng(seed)
        val bp = BiquadBp().apply { init(bodyFreq, 2.8f) }
        val attN = (CHIME_SR * 0.0012f).toInt().coerceAtLeast(1)

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val env = if (i < attN) {
                i.toFloat() / attN.toFloat()
            } else {
                exp(-((i - attN).toFloat() / CHIME_SR.toFloat()) * 38.0f)
            }
            val sweepHz = startHz + (endHz - startHz) * (frac * frac)
            val t = i.toFloat() / CHIME_SR.toFloat()
            val chirp = sin((2.0 * Math.PI * sweepHz.toDouble() * t.toDouble())).toFloat()
            val snap = sin((2.0 * Math.PI * snapFreq.toDouble() * t.toDouble())).toFloat() * 0.55f +
                sin((2.0 * Math.PI * (snapFreq * 1.62f).toDouble() * t.toDouble())).toFloat() * 0.28f
            val white = rng.uniformSigned()
            val tunedNoise = bp.step(white)
            val s = (0.50f * chirp + 0.30f * snap + 0.20f * tunedNoise) * env * amp * 29000.0f
            mixBuf[idx0 + i] += s
        }
    }

    fun addCurtainZip(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        startCenterHz: Float,
        endCenterHz: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val rng = SimpleRng(seed)
        val bp = BiquadBp().apply { init(startCenterHz, 3.2f) }

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val env = sin((Math.PI * frac.toDouble())).toFloat().pow(1.4f)
            if ((i and 31) == 0) {
                val center = startCenterHz + (endCenterHz - startCenterHz) * frac
                bp.init(center, 3.2f)
            }
            val white = rng.uniformSigned()
            val filtered = bp.step(white)
            val grit = sin((2.0 * Math.PI * 480.0 * (i.toDouble() / CHIME_SR.toDouble()))).toFloat() * 0.22f
            val s = (filtered * 0.85f + grit) * env * amp * 23000.0f
            mixBuf[idx0 + i] += s
        }
    }

    fun addSpringRingTail(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        ringHz: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val bp = BiquadBp().apply { init(ringHz, 8.5f) }
        val rng = SimpleRng(seed)

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val env = exp(-frac * 5.2f)
            val t = i.toFloat() / CHIME_SR.toFloat()
            val pure = sin((2.0 * Math.PI * ringHz.toDouble() * t.toDouble())).toFloat()
            val partial2 = sin((2.0 * Math.PI * (ringHz * 1.48f).toDouble() * t.toDouble())).toFloat() * 0.35f
            val impulse = if (i == 0) 1.0f else 0.0f
            val ping = bp.step(impulse + rng.uniformSigned() * 0.08f)
            val s = (0.55f * pure + 0.20f * partial2 + 0.25f * ping) * env * amp * 21000.0f
            mixBuf[idx0 + i] += s
        }
    }

    /**
     * Synthesizes 24 kHz 16-bit mono signed little-endian PCM audio for any of the 6 tactile studio chimes.
     */
    fun synthesizeChimePcm16(kind: String): ByteArray {
        val mixBuf = FloatArray(MAX_CHIME_SAMPLES)
        var totalSamples = 0

        when (kind) {
            "ptt_start" -> {
                // 1C-2A-2 (Warm Studio Fade) PTT Start (~248ms):
                // Latch click -> F5 (698.5 Hz) -> G5 (784.0 Hz) -> C6 (1046.5 Hz) -> latch click
                addSwitchClick(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 6.5f, 2200.0f, 850.0f, 0.92f, 0.45f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.008f, 698.5f, 78.0f, 0.65f, 6.0f, 2200.0f, 8.0f, 6.0f, 0.36f, 22.0f, 5.2f, 0.0045f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.076f, 784.0f, 80.0f, 0.68f, 6.0f, 2400.0f, 8.0f, 6.0f, 0.36f, 24.0f, 5.2f, 0.005f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.145f, 1046.5f, 96.0f, 0.68f, 6.5f, 2800.0f, 8.0f, 6.0f, 0.36f, 26.0f, 5.2f, 0.0055f)
                addSwitchClick(mixBuf, MAX_CHIME_SAMPLES, 0.235f, 6.5f, 2600.0f, 950.0f, 0.85f, 0.45f)
                totalSamples = (0.248f * CHIME_SR).toInt()
            }
            "ptt_stop" -> {
                // 1C-2A-2 PTT Stop (~272ms): Descending C6 -> F5 -> release click
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 1046.5f, 108.0f, 0.68f, 7.5f, 2600.0f, 10.0f, 6.0f, 0.36f, 26.0f, 5.2f, 0.005f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.092f, 698.5f, 165.0f, 0.68f, 6.5f, 1800.0f, 8.0f, 6.0f, 0.36f, 48.0f, 5.2f, 0.006f)
                addSwitchClick(mixBuf, MAX_CHIME_SAMPLES, 0.258f, 6.0f, 1550.0f, 700.0f, 0.80f, 0.45f)
                totalSamples = (0.272f * CHIME_SR).toInt()
            }
            "snap" -> {
                // 1C-2A Classic 35mm SLR Focal-Plane Curtain ("ka-tshhh-CHACK" + spring ring)
                addShutterImpact(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 7.0f, 850.0f, 300.0f, 3900.0f, 3200.0f, 0.82f, 11L)
                addShutterImpact(mixBuf, MAX_CHIME_SAMPLES, 0.014f, 19.0f, 580.0f, 155.0f, 3100.0f, 2300.0f, 0.98f, 12L)
                addCurtainZip(mixBuf, MAX_CHIME_SAMPLES, 0.022f, 38.0f, 1900.0f, 4600.0f, 0.68f, 13L)
                addShutterImpact(mixBuf, MAX_CHIME_SAMPLES, 0.068f, 22.0f, 640.0f, 135.0f, 3650.0f, 2850.0f, 1.00f, 14L)
                addShutterImpact(mixBuf, MAX_CHIME_SAMPLES, 0.094f, 11.0f, 420.0f, 160.0f, 2900.0f, 2200.0f, 0.68f, 15L)
                addSpringRingTail(mixBuf, MAX_CHIME_SAMPLES, 0.098f, 55.0f, 3850.0f, 0.26f, 16L)
                totalSamples = (0.160f * CHIME_SR).toInt()
            }
            "thinking" -> {
                // 1C-2A Thinking / Tool Pulse (~385ms): Two relay latches into warm G5 -> B5 interval
                addSwitchClick(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 6.5f, 2200.0f, 820.0f, 0.88f, 0.45f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.035f, 784.0f, 140.0f, 0.54f, 5.0f, 2400.0f, 6.0f, 6.0f, 0.36f, 35.0f, 5.2f, 0.0048f)
                addSwitchClick(mixBuf, MAX_CHIME_SAMPLES, 0.165f, 6.5f, 2600.0f, 920.0f, 0.88f, 0.45f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.200f, 987.8f, 180.0f, 0.56f, 5.0f, 2750.0f, 6.0f, 6.0f, 0.36f, 55.0f, 5.2f, 0.006f)
                totalSamples = (0.390f * CHIME_SR).toInt()
            }
            "ready" -> {
                // 1C-2A-2 (Warm Studio Fade) Build Complete (~795ms):
                // Fast 298ms 4-step climb + legato overlap + 240ms Wurlitzer-style natural cosine fade-out tail
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 698.5f, 130.0f, 0.68f, 6.5f, 2250.0f, 7.0f, 6.0f, 0.40f, 42.0f, 5.2f, 0.006f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.096f, 880.0f, 130.0f, 0.68f, 6.0f, 2500.0f, 7.0f, 6.0f, 0.40f, 42.0f, 5.2f, 0.006f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.192f, 1046.5f, 135.0f, 0.68f, 6.0f, 2750.0f, 7.0f, 6.0f, 0.40f, 45.0f, 5.2f, 0.006f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.298f, 698.5f, 490.0f, 0.40f, 7.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.298f, 1046.5f, 490.0f, 0.44f, 0.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.298f, 1396.9f, 490.0f, 0.46f, 0.0f, 2950.0f, 7.0f, 6.0f, 0.35f, 240.0f, 5.2f, 0.007f)
                totalSamples = (0.798f * CHIME_SR).toInt()
            }
            "error" -> {
                // 1C-2A Error / Cancel (~450ms): 3-step descending reed (E5 -> C5 -> A4)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 659.3f, 122.0f, 0.68f, 7.0f, 1950.0f, 10.0f, 6.0f, 0.35f, 28.0f, 5.2f, 0.0042f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.105f, 523.3f, 122.0f, 0.68f, 6.0f, 1650.0f, 8.0f, 6.0f, 0.35f, 28.0f, 5.2f, 0.0042f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.210f, 440.0f, 230.0f, 0.68f, 6.5f, 1350.0f, 8.0f, 6.0f, 0.36f, 75.0f, 5.2f, 0.006f)
                totalSamples = (0.452f * CHIME_SR).toInt()
            }
        }

        if (totalSamples > MAX_CHIME_SAMPLES) totalSamples = MAX_CHIME_SAMPLES

        // Two-pole 300 Hz high-pass filter at 24 kHz (alpha = 0.924) to strip sub-bass coil current spikes,
        // paired with 24,000 LSB peak ceiling so chimes are crisp and clearly audible without rail sag.
        val alpha = 0.924f
        var x1 = 0.0f
        var y1 = 0.0f
        var x2 = 0.0f
        var y2 = 0.0f
        val pcmBytes = ByteArray(totalSamples * 2)

        for (i in 0 until totalSamples) {
            val x = tanh((mixBuf[i] / 28000.0f).toDouble()).toFloat() * 24000.0f
            val s1 = alpha * (y1 + x - x1)
            x1 = x
            y1 = s1
            val s2 = alpha * (y2 + s1 - x2)
            x2 = s1
            y2 = s2
            val clamped = s2.coerceIn(-32768.0f, 32767.0f)
            val s16 = clamped.toInt().toShort()
            val idx = i * 2
            pcmBytes[idx] = (s16.toInt() and 0xFF).toByte()
            pcmBytes[idx + 1] = ((s16.toInt() ushr 8) and 0xFF).toByte()
        }
        return pcmBytes
    }
}

/**
 * Continuous 16 kHz Pre-Roll Listener with 'Hey Zero' Wake-Phrase Spotter for Gotchi Phone.
 *
 * Invariants:
 * - Continuous 16 kHz 16-bit mono PCM capture into a 500 ms circular ring buffer (16,000 bytes / 8,000 samples).
 * - Wake phrase spotter matching normalized transcripts for:
 *   "hey zero", "hay zero", "hi zero", "okay zero".
 * - Utilizes Android SpeechRecognizer with RecognizerIntent.EXTRA_PREFER_OFFLINE and EXTRA_PARTIAL_RESULTS
 *   paired with acoustic energy envelope tracking (RMS).
 * - Smart wake-word arbitration checking `!GotchiBleRelayService.isRelayTurnActive` to avoid colliding with
 *   desk Gotchi relay interactions.
 * - On wake-word match:
 *   1. Snapshots the 500 ms circular pre-roll PCM ring buffer.
 *   2. Emits haptic confirmation via `VibrationEffect.createOneShot(75L, VibrationEffect.DEFAULT_AMPLITUDE)`.
 *   3. Dispatches `onWakeWordDetected(preRollPcm: ByteArray)` so the 500 ms pre-roll is prepended seamlessly
 *      into the active turn upload.
 */
class HeyZeroPreRollListener(
    private val context: Context,
    var onWakeWordDetected: ((preRollPcm: ByteArray) -> Unit)? = null
) : SensorEventListener {

    fun interface WakeWordListener {
        fun onWakeWordDetected(preRollPcm: ByteArray)
    }

    var wakeWordListener: WakeWordListener? = null
    var onRmsChangedListener: ((rms: Float) -> Unit)? = null

    companion object {
        private const val TAG = "HeyZeroPreRollListener"

        const val SAMPLE_RATE = 16000
        const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        const val BYTES_PER_SAMPLE = 2

        const val PRE_ROLL_MS = 500
        // 500 ms @ 16 kHz 16-bit mono: 16000 samples/sec * 2 bytes/sample * 0.5s = 16000 bytes (8000 samples)
        const val PRE_ROLL_BYTES = (SAMPLE_RATE * BYTES_PER_SAMPLE * PRE_ROLL_MS) / 1000

        val WAKE_PHRASES = setOf(
            "hey zero",
            "hay zero",
            "hi zero",
            "okay zero"
        )

        const val TYPE_WRIST_TILT_GESTURE = 26
        const val MIN_WRIST_Z_GRAVITY = 5.5f
        const val MAX_WRIST_XY_TILT = 7.5f
        const val WAKE_RMS_GATE_THRESHOLD = 45.0

        fun isWatchInSpeakingOrientation(gx: Float, gy: Float, gz: Float): Boolean =
            gz >= MIN_WRIST_Z_GRAVITY && kotlin.math.abs(gx) <= MAX_WRIST_XY_TILT && kotlin.math.abs(gy) <= MAX_WRIST_XY_TILT

        /**
         * Normalizes transcript strings for robust wake-word matching (lowercased, stripped punctuation,
         * collapsed whitespace).
         */
        fun normalizeTranscript(text: String): String {
            return text.lowercase()
                .replace(Regex("[^a-z0-9\\s]"), " ")
                .replace(Regex("\\s+"), " ")
                .trim()
        }

        /**
         * Tests whether a transcript candidate contains or begins with a supported wake phrase.
         */
        fun isWakePhrase(text: String): Boolean {
            val normalized = normalizeTranscript(text)
            if (normalized in WAKE_PHRASES) return true
            return WAKE_PHRASES.any { phrase ->
                normalized.startsWith(phrase) || normalized.contains(phrase)
            }
        }

        /**
         * Computes Root-Mean-Square (RMS) amplitude from 16-bit signed PCM byte stream.
         */
        fun calculateRms(pcmData: ByteArray, length: Int = pcmData.size): Double {
            var sum = 0.0
            val sampleCount = length / 2
            if (sampleCount == 0) return 0.0
            for (i in 0 until sampleCount) {
                val idx = i * 2
                val lo = pcmData[idx].toInt() and 0xFF
                val hi = pcmData[idx + 1].toInt()
                val sample = (hi shl 8) or lo
                val sampleDouble = sample.toDouble()
                sum += sampleDouble * sampleDouble
            }
            return sqrt(sum / sampleCount)
        }
    }

    /**
     * Thread-safe circular ring buffer for pre-roll PCM audio.
     */
    class CircularPreRollBuffer(val capacity: Int = PRE_ROLL_BYTES) {
        val buffer = ByteArray(capacity)
        var writePos = 0
        var isFilled = false
        val lock = Any()

        fun write(data: ByteArray, length: Int = data.size) {
            synchronized(lock) {
                for (i in 0 until length) {
                    buffer[writePos] = data[i]
                    writePos = (writePos + 1) % capacity
                    if (writePos == 0) isFilled = true
                }
            }
        }

        fun readSnapshot(): ByteArray {
            synchronized(lock) {
                val out = ByteArray(if (isFilled) capacity else writePos)
                if (!isFilled) {
                    System.arraycopy(buffer, 0, out, 0, writePos)
                } else {
                    val tail = capacity - writePos
                    System.arraycopy(buffer, writePos, out, 0, tail)
                    System.arraycopy(buffer, 0, out, tail, writePos)
                }
                return out
            }
        }

        fun snapshot(): ByteArray = readSnapshot()

        fun reset() {
            synchronized(lock) {
                writePos = 0
                isFilled = false
            }
        }
    }

    val ringBuffer = CircularPreRollBuffer()
    private val isListening = AtomicBoolean(false)
    private var isSpeechRecognizerActive = false
    private var audioRecordThread: Thread? = null
    private var audioRecord: AudioRecord? = null
    private var speechRecognizer: SpeechRecognizer? = null
    private var recognizerIntent: Intent? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    private var sensorManager: SensorManager? = null
    private var gravitySensor: Sensor? = null
    private var isWristOriented = true
    var requireWristOrientation: Boolean = false

    private var lastWakeTriggerMs = 0L
    private val WAKE_COOLDOWN_MS = 2000L
    private var currentRms: Double = 0.0

    fun start() {
        if (isListening.getAndSet(true)) return
        startAudioRecordCapture()
        startSpeechRecognizerLoop()
        if (requireWristOrientation) {
            registerGravitySensor()
        }
    }

    fun stop() {
        if (!isListening.getAndSet(false)) return
        stopAudioRecordCapture()
        stopSpeechRecognizerLoop()
        unregisterGravitySensor()
    }

    fun pauseForActiveTurn() {
        stopSpeechRecognizerLoop()
    }

    fun resumeAfterTurn() {
        if (isListening.get()) {
            startSpeechRecognizerLoop()
        }
    }

    private fun registerGravitySensor() {
        runCatching {
            sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
            gravitySensor = sensorManager?.getDefaultSensor(Sensor.TYPE_GRAVITY)
            gravitySensor?.let { sensorManager?.registerListener(this, it, SensorManager.SENSOR_DELAY_UI) }
        }
    }

    private fun unregisterGravitySensor() {
        runCatching {
            sensorManager?.unregisterListener(this)
            sensorManager = null
            gravitySensor = null
        }
        isWristOriented = true
    }

    override fun onSensorChanged(event: SensorEvent?) {
        if (event == null || !requireWristOrientation) return
        if (event.sensor.type == Sensor.TYPE_GRAVITY) {
            val gx = event.values[0]
            val gy = event.values[1]
            val gz = event.values[2]
            val wasOriented = isWristOriented
            isWristOriented = isWatchInSpeakingOrientation(gx, gy, gz)
            if (!wasOriented && isWristOriented && isListening.get() && !isSpeechRecognizerActive) {
                restartRecognizer()
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    private fun processTranscriptCandidates(candidates: List<String>) {
        if (!isListening.get()) return
        // Smart wake-word arbitration: pause/ignore if desk Gotchi relay turn is active
        if (GotchiBleRelayService.isRelayTurnActive) {
            Log.d(TAG, "Ignoring wake word candidate: GotchiBleRelayService.isRelayTurnActive is true")
            return
        }
        if (requireWristOrientation && !isWristOriented) return

        for (candidate in candidates) {
            if (isWakePhrase(candidate)) {
                val now = System.currentTimeMillis()
                if (now - lastWakeTriggerMs < WAKE_COOLDOWN_MS) return
                lastWakeTriggerMs = now
                triggerHapticFeedback()
                val preRollSnapshot = ringBuffer.readSnapshot()
                mainHandler.post {
                    if (!GotchiBleRelayService.isRelayTurnActive) {
                        onWakeWordDetected?.invoke(preRollSnapshot)
                        wakeWordListener?.onWakeWordDetected(preRollSnapshot)
                    }
                }
                break
            }
        }
    }

    private fun triggerHapticFeedback() {
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
                vm?.defaultVibrator?.vibrate(VibrationEffect.createOneShot(75L, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                val v = context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
                v?.vibrate(VibrationEffect.createOneShot(75L, VibrationEffect.DEFAULT_AMPLITUDE))
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun startAudioRecordCapture() {
        val minBufferSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            CHANNEL_CONFIG,
            AUDIO_FORMAT
        ).coerceAtLeast(3200)

        audioRecordThread = Thread({
            runCatching {
                val record = AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    CHANNEL_CONFIG,
                    AUDIO_FORMAT,
                    minBufferSize
                )
                if (record.state != AudioRecord.STATE_INITIALIZED) {
                    record.release()
                    return@Thread
                }
                audioRecord = record
                record.startRecording()

                val buffer = ByteArray(640) // 20 ms frames @ 16 kHz 16-bit mono
                while (isListening.get() && !Thread.currentThread().isInterrupted) {
                    val read = record.read(buffer, 0, buffer.size)
                    if (read > 0) {
                        ringBuffer.write(buffer, read)
                        currentRms = calculateRms(buffer, read)
                    }
                }
                record.stop()
                record.release()
            }.onFailure { err ->
                Log.w(TAG, "AudioRecord pre-roll capture unavailable: ${err.message}")
            }
        }, "HeyZeroPreRollAudioThread").apply {
            priority = Thread.NORM_PRIORITY + 1
            start()
        }
    }

    private fun stopAudioRecordCapture() {
        runCatching {
            audioRecord?.stop()
            audioRecord?.release()
        }
        audioRecord = null
        audioRecordThread?.interrupt()
        audioRecordThread = null
    }

    private fun startSpeechRecognizerLoop() {
        mainHandler.post {
            if (!isListening.get()) return@post
            runCatching {
                if (speechRecognizer == null) {
                    if (!SpeechRecognizer.isRecognitionAvailable(context)) {
                        Log.w(TAG, "SpeechRecognizer is not available on this device")
                        return@runCatching
                    }
                    speechRecognizer = SpeechRecognizer.createSpeechRecognizer(context).apply {
                        setRecognitionListener(createRecognitionListener())
                    }
                }

                if (recognizerIntent == null) {
                    recognizerIntent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                        putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                        putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                        putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
                        putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 5)
                    }
                }

                speechRecognizer?.startListening(recognizerIntent)
                isSpeechRecognizerActive = true
            }.onFailure { err ->
                Log.w(TAG, "Failed to start speech recognizer: ${err.message}")
            }
        }
    }

    private fun stopSpeechRecognizerLoop() {
        mainHandler.post {
            runCatching {
                speechRecognizer?.stopListening()
                speechRecognizer?.cancel()
                speechRecognizer?.destroy()
            }
            speechRecognizer = null
            recognizerIntent = null
            isSpeechRecognizerActive = false
        }
    }

    private fun createRecognitionListener(): RecognitionListener {
        return object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                isSpeechRecognizerActive = true
            }
            override fun onBeginningOfSpeech() {
                isSpeechRecognizerActive = true
            }
            override fun onRmsChanged(rmsdB: Float) {
                onRmsChangedListener?.invoke(rmsdB)
            }
            override fun onBufferReceived(buffer: ByteArray?) {
                buffer?.let { ringBuffer.write(it) }
            }
            override fun onEndOfSpeech() {
                isSpeechRecognizerActive = false
                if (isListening.get()) {
                    mainHandler.postDelayed({ restartRecognizer() }, 80L)
                }
            }
            override fun onError(error: Int) {
                isSpeechRecognizerActive = false
                if (isListening.get()) {
                    mainHandler.postDelayed({ restartRecognizer() }, 200L)
                }
            }
            override fun onResults(results: Bundle?) {
                isSpeechRecognizerActive = false
                val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: emptyList<String>()
                processTranscriptCandidates(matches)
                if (isListening.get()) {
                    mainHandler.postDelayed({ restartRecognizer() }, 80L)
                }
            }
            override fun onPartialResults(partialResults: Bundle?) {
                val matches = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION) ?: emptyList<String>()
                processTranscriptCandidates(matches)
            }
            override fun onEvent(eventType: Int, params: Bundle?) {}
        }
    }

    private fun restartRecognizer() {
        if (!isListening.get()) return
        if (currentRms < WAKE_RMS_GATE_THRESHOLD || (requireWristOrientation && !isWristOriented)) {
            return
        }
        runCatching {
            speechRecognizer?.cancel()
            recognizerIntent?.let {
                speechRecognizer?.startListening(it)
                isSpeechRecognizerActive = true
            }
        }.onFailure {
            startSpeechRecognizerLoop()
        }
    }

    /**
     * Release all native resources.
     */
    fun destroy() {
        stop()
        ringBuffer.reset()
    }
}
