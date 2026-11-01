package ventures.brock.gotchi.watch

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
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
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.sqrt

/**
 * Continuous 16 kHz Pre-Roll Listener with 'Hey Zero' Wake-Phrase Spotter for Wear OS Pixel Watch.
 *
 * Invariants:
 * - Continuous 16 kHz 16-bit mono PCM capture into a 500 ms circular ring buffer (16,000 bytes / 8,000 samples).
 * - Wake phrase spotter matching normalized transcripts for:
 *   "hey zero", "hay zero", "hi zero", "okay zero".
 * - Utilizes Android SpeechRecognizer with RecognizerIntent.EXTRA_PREFER_OFFLINE and EXTRA_PARTIAL_RESULTS
 *   paired with acoustic energy envelope tracking (RMS).
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
        private val buffer = ByteArray(capacity)
        private var writeIndex = 0
        private var totalWritten = 0L
        private val lock = Any()

        fun write(data: ByteArray, offset: Int = 0, length: Int = data.size) {
            if (length <= 0) return
            synchronized(lock) {
                var remaining = length
                var srcOffset = offset
                while (remaining > 0) {
                    val toCopy = minOf(remaining, capacity - writeIndex)
                    System.arraycopy(data, srcOffset, buffer, writeIndex, toCopy)
                    writeIndex = (writeIndex + toCopy) % capacity
                    srcOffset += toCopy
                    remaining -= toCopy
                    totalWritten += toCopy
                }
            }
        }

        /**
         * Returns an ordered snapshot of the last captured PCM bytes (up to capacity),
         * aligned chronologically from oldest sample to newest sample.
         */
        fun snapshot(): ByteArray {
            synchronized(lock) {
                val available = minOf(totalWritten, capacity.toLong()).toInt()
                val result = ByteArray(available)
                if (totalWritten < capacity) {
                    // Buffer has not yet wrapped
                    System.arraycopy(buffer, 0, result, 0, available)
                } else {
                    // Buffer has wrapped: writeIndex points to the oldest available byte
                    val tailLen = capacity - writeIndex
                    System.arraycopy(buffer, writeIndex, result, 0, tailLen)
                    if (writeIndex > 0) {
                        System.arraycopy(buffer, 0, result, tailLen, writeIndex)
                    }
                }
                return result
            }
        }

        fun reset() {
            synchronized(lock) {
                writeIndex = 0
                totalWritten = 0L
                buffer.fill(0)
            }
        }

        fun size(): Int = synchronized(lock) {
            minOf(totalWritten, capacity.toLong()).toInt()
        }
    }

    val ringBuffer = CircularPreRollBuffer(PRE_ROLL_BYTES)
    val isListening = AtomicBoolean(false)

    var currentRms: Double = 0.0
        private set

    var requireWristOrientation: Boolean = true
    var isWristOriented: Boolean = false

    fun isWatchInSpeakingOrientation(gx: Float, gy: Float, gz: Float): Boolean =
        Companion.isWatchInSpeakingOrientation(gx, gy, gz)

    private val sensorManager = context.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
    private val gravitySensor = sensorManager?.getDefaultSensor(Sensor.TYPE_GRAVITY)
        ?: sensorManager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val wristTiltSensor = sensorManager?.getDefaultSensor(TYPE_WRIST_TILT_GESTURE)
    private var isSensorRegistered = false
    private var isSpeechRecognizerActive = false

    private fun registerSensors() {
        if (isSensorRegistered || sensorManager == null) return
        gravitySensor?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_UI)
        }
        wristTiltSensor?.let {
            sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_UI)
        }
        isSensorRegistered = true
    }

    private fun unregisterSensors() {
        if (!isSensorRegistered) return
        sensorManager?.unregisterListener(this)
        isSensorRegistered = false
    }

    override fun onSensorChanged(event: SensorEvent?) {
        if (event == null) return
        when (event.sensor.type) {
            Sensor.TYPE_GRAVITY, Sensor.TYPE_ACCELEROMETER -> {
                val gx = event.values[0]
                val gy = event.values[1]
                val gz = event.values[2]
                isWristOriented = isWatchInSpeakingOrientation(gx, gy, gz)
            }
            TYPE_WRIST_TILT_GESTURE -> {
                if (event.values.isNotEmpty() && event.values[0] > 0f) {
                    isWristOriented = true
                }
            }
        }
        if (isListening.get() && !isSpeechRecognizerActive && currentRms >= WAKE_RMS_GATE_THRESHOLD && (!requireWristOrientation || isWristOriented)) {
            mainHandler.post { restartRecognizer() }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    fun pauseForActiveTurn() {
        stopSpeechRecognizerLoop()
        unregisterSensors()
    }

    fun resumeAfterTurn() {
        if (isListening.get()) {
            registerSensors()
            startSpeechRecognizerLoop()
        }
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private var speechRecognizer: SpeechRecognizer? = null
    private var recognizerIntent: Intent? = null
    private var audioRecordThread: Thread? = null
    private var audioRecord: AudioRecord? = null

    /**
     * Start the continuous 16 kHz pre-roll audio capture and wake phrase recognizer.
     */
    fun start() {
        if (isListening.compareAndSet(false, true)) {
            registerSensors()
            startAudioRecordCapture()
            startSpeechRecognizerLoop()
        }
    }

    fun startListening() = start()

    /**
     * Stop pre-roll audio capture and wake-word listening.
     */
    fun stop() {
        if (isListening.compareAndSet(true, false)) {
            unregisterSensors()
            stopAudioRecordCapture()
            stopSpeechRecognizerLoop()
        }
    }

    fun stopListening() = stop()

    fun isListening(): Boolean = isListening.get()

    /**
     * Snapshots the current 500 ms circular pre-roll PCM bytes for handoff to active turn capture.
     */
    fun getPreRollSnapshot(): ByteArray = ringBuffer.snapshot()

    /**
     * Allows feeding external PCM audio directly into the ring buffer (e.g. from shared AudioRecord
     * or unit test fixtures).
     */
    fun feedPcm(data: ByteArray, offset: Int = 0, length: Int = data.size) {
        ringBuffer.write(data, offset, length)
        currentRms = calculateRms(data, length)
        onRmsChangedListener?.invoke(currentRms.toFloat())
        if (isListening.get() && !isSpeechRecognizerActive && currentRms >= WAKE_RMS_GATE_THRESHOLD && (!requireWristOrientation || isWristOriented)) {
            mainHandler.post { restartRecognizer() }
        }
    }

    /**
     * Check candidates for wake phrase match and trigger notification + haptic.
     */
    fun processTranscriptCandidates(candidates: List<String>): Boolean {
        for (raw in candidates) {
            if (isWakePhrase(raw)) {
                Log.i(TAG, "Wake word matched: '$raw'")
                triggerHapticConfirmation()
                val preRoll = ringBuffer.snapshot()
                mainHandler.post {
                    onWakeWordDetected?.invoke(preRoll)
                    wakeWordListener?.onWakeWordDetected(preRoll)
                }
                return true
            }
        }
        return false
    }

    @Suppress("DEPRECATION")
    private fun triggerHapticConfirmation() {
        runCatching {
            val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
                vm?.defaultVibrator
            } else {
                context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                vibrator?.vibrate(
                    VibrationEffect.createOneShot(75L, VibrationEffect.DEFAULT_AMPLITUDE)
                )
            } else {
                vibrator?.vibrate(75L)
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun startAudioRecordCapture() {
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
        val bufSize = maxOf(minBuf, 2048)

        audioRecordThread = Thread({
            runCatching {
                val rec = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    CHANNEL_CONFIG,
                    AUDIO_FORMAT,
                    bufSize
                )
                audioRecord = rec
                rec.startRecording()

                val temp = ByteArray(1024)
                while (isListening.get()) {
                    val read = rec.read(temp, 0, temp.size)
                    if (read > 0) {
                        feedPcm(temp, 0, read)
                    }
                }

                rec.stop()
                rec.release()
            }.onFailure { err ->
                Log.w(TAG, "AudioRecord pre-roll capture unavailable or permission missing: ${err.message}")
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
