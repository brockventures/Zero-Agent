package ventures.brock.gotchi.watch

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast

/**
 * Standalone Wear OS Pixel Watch Activity for GOTCHI (`ventures.brock.gotchi.watch`).
 *
 * Designed specifically for circular Wear OS displays (Google Pixel Watch 1/2/3):
 * - Renders circular `GotchiEpaperView` with 200x200 1-bit monochrome canvas and dual bezel status arcs
 *   (amber PTT status arc and cyan 'Hey Zero' pre-roll wake listener status arc).
 * - Full Push-to-Talk & Dismiss via `PinchGestureDetector`:
 *   - Physical stem hardware keys (`KEYCODE_STEM_PRIMARY` crown button, `KEYCODE_STEM_1`, `KEYCODE_HEADSETHOOK`).
 *   - Native Wear OS One-Handed Gestures (Double Pinch `ACTION_PRIMARY` / Wrist Turn `ACTION_DISMISS`).
 *   - Auto state-action mapping: START_PTT when idle, STOP_PTT_AND_COMMIT when recording, TRIGGER_BARGE_IN when speaking.
 * - Continuous 500 ms circular pre-roll listener via `HeyZeroPreRollListener`:
 *   - Spotting "Hey Zero" wake phrase offline.
 *   - Snapshots 500 ms 16 kHz PCM ring buffer, cleanly stops/releases AudioRecord, and delegates to
 *     `engine.startWakeWordTurn(preRollPcm)`.
 * - Remote Cloudflare Tunnel (`https://gotchi.brock.ventures`) + Bearer auth & `FallbackDns`, plus
 *   port `8771` LAN migration from legacy `:8765` / `:8769` endpoints.
 * - Rotary Crown hardware scroll handling (`MotionEvent.AXIS_SCROLL`) to cycle skins and adjust modes.
 * - Long-press & debounced double-tap gestures for quick wake-mode toggling and Go Live session activation.
 */
class GotchiWatchActivity : Activity() {

    companion object {
        const val PREFS_NAME = "gotchi_watch_prefs"
        const val KEY_GATEWAY_URL = "gateway_url"
        const val KEY_BEARER_TOKEN = "bearer_token"
        const val KEY_NODE_ID = "node_id"
        const val KEY_AUDIO_OUT_MODE = "audio_out_mode"
        const val KEY_PTT_SILENCE_MS = "ptt_silence_ms"
        const val KEY_SKIN = "skin"
        const val KEY_HEY_ZERO_ENABLED = "hey_zero_enabled"
        const val KEY_OPEN_SETTINGS = "open_settings"
        const val KEY_OLED_DARK_MODE = "oled_dark_mode"
        const val KEY_WRIST_ORIENTATION_GATE = "wrist_orientation_gate"
        const val KEY_KEEP_SCREEN_ON = "keep_screen_on"
        const val KEY_CHIMES_ENABLED = "chimes_enabled"

        private const val DOUBLE_TAP_WINDOW_MS = 350L
        private const val TRIPLE_TAP_WINDOW_MS = 500L
        private const val LONG_PRESS_MS = 600L
    }

    private lateinit var epaperView: GotchiEpaperView
    private lateinit var engine: GotchiWatchEngine
    private lateinit var pinchDetector: PinchGestureDetector
    private lateinit var preRollListener: HeyZeroPreRollListener
    private var wakeLock: PowerManager.WakeLock? = null

    private var lastTapDownMs: Long = 0L
    private var tapDownMs: Long = 0L
    private var isLongPressed = false
    private var tapCount: Int = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        setShowWhenLocked(true)
        setTurnScreenOn(true)

        val powerManager = getSystemService(Context.POWER_SERVICE) as? PowerManager
        wakeLock = powerManager?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gotchi:watch_session_wakelock")

        requestRuntimePermissionsIfNeeded()

        epaperView = GotchiEpaperView(this).apply {
            isFocusable = true
            isFocusableInTouchMode = true
        }

        engine = GotchiWatchEngine { state, transcript, reply, status, audioBytes, sampleRate ->
            runOnUiThread {
                epaperView.updateState(
                    newState = state,
                    transcript = transcript,
                    reply = reply,
                    statusText = status,
                    audioBytesDelta = audioBytes,
                    sampleRate = sampleRate
                )
                syncImuSuppressionState(state)
            }
        }

        // Initialize Pinch Gesture & Stem Key Detector
        pinchDetector = PinchGestureDetector(this) { source ->
            runOnUiThread {
                handlePinchOrStemTrigger(source)
            }
        }
        pinchDetector.onDismissTriggered = { source ->
            runOnUiThread {
                handleDismissTrigger(source)
            }
        }

        // Initialize 'Hey Zero' Pre-Roll Listener & link to engine for clean AudioRecord handoff
        preRollListener = HeyZeroPreRollListener(this) { preRollPcm ->
            runOnUiThread {
                handleWakeWordDetected(preRollPcm)
            }
        }
        engine.preRollListener = preRollListener

        loadPreferencesAndIntent(intent)

        // Build circular watch container
        val rootLayout = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            isFocusable = true
            isFocusableInTouchMode = true
            addView(
                epaperView,
                FrameLayout.LayoutParams(
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    FrameLayout.LayoutParams.MATCH_PARENT,
                    Gravity.CENTER
                )
            )
        }

        setupTouchInteractions(rootLayout)
        setContentView(rootLayout)

        epaperView.updateState(
            newState = "idle",
            statusText = "PINCH TO TALK",
            forceFullRefresh = true,
            wakeListenerActive = engine.config.heyZeroWakeEnabled
        )

        if (intent?.getBooleanExtra(KEY_OPEN_SETTINGS, false) == true) {
            window.decorView.postDelayed({ showWatchSettingsDialog() }, 350L)
        }
    }

    fun applyKeepScreenOnFlag(enabled: Boolean) {
        if (enabled) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    override fun onResume() {
        super.onResume()
        if (wakeLock?.isHeld == false) {
            wakeLock?.acquire()
        }
        pinchDetector.start()
        engine.startNotificationPolling { statusText, _ ->
            runOnUiThread {
                Toast.makeText(this, statusText, Toast.LENGTH_SHORT).show()
            }
        }
        if (engine.config.heyZeroWakeEnabled) {
            preRollListener.start()
            epaperView.setWakeListenerActive(true)
        } else {
            epaperView.setWakeListenerActive(false)
        }
        GotchiWatchUpdater.syncRemoteConfig(
            gatewayUrl = engine.config.gatewayUrl,
            nodeId = engine.config.nodeId,
            pinchDetector = pinchDetector,
            engine = engine,
            epaperView = epaperView,
            context = this,
            onUpdateAvailable = { ver -> showUpdateAvailablePrompt(ver) }
        )
    }

    override fun onPause() {
        super.onPause()
        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
        pinchDetector.stop()
        preRollListener.stop()
        engine.stopNotificationPolling()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
        wakeLock = null
        pinchDetector.stop()
        preRollListener.stop()
        engine.shutdown()
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        loadPreferencesAndIntent(intent)
        epaperView.updateState(forceFullRefresh = true)
        if (intent?.getBooleanExtra(KEY_OPEN_SETTINGS, false) == true) {
            window.decorView.postDelayed({ showWatchSettingsDialog() }, 250L)
        }
    }

    private fun requestRuntimePermissionsIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val needed = mutableListOf<String>()
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.RECORD_AUDIO)
            }
            if (needed.isNotEmpty()) {
                requestPermissions(needed.toTypedArray(), 2001)
            }
        }
    }

    private fun loadPreferencesAndIntent(incoming: Intent?) {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val edit = prefs.edit()

        incoming?.let {
            it.getStringExtra(KEY_GATEWAY_URL)?.let { v ->
                val targetUrl = if (v.contains(":8765") || v.contains(":8769")) {
                    v.replace(":8765", ":8771").replace(":8769", ":8771")
                } else {
                    GotchiWatchEngine.normalizeGatewayUrl(v)
                }
                edit.putString(KEY_GATEWAY_URL, targetUrl)
            }
            it.getStringExtra(KEY_BEARER_TOKEN)?.let { v -> edit.putString(KEY_BEARER_TOKEN, v) }
            it.getStringExtra(KEY_NODE_ID)?.let { v -> edit.putString(KEY_NODE_ID, v) }
            it.getStringExtra(KEY_AUDIO_OUT_MODE)?.let { v -> edit.putString(KEY_AUDIO_OUT_MODE, v) }
            it.getStringExtra(KEY_SKIN)?.let { v -> edit.putString(KEY_SKIN, v) }
            if (it.hasExtra(KEY_PTT_SILENCE_MS)) {
                edit.putLong(KEY_PTT_SILENCE_MS, it.getLongExtra(KEY_PTT_SILENCE_MS, 1500L))
            }
            if (it.hasExtra(KEY_HEY_ZERO_ENABLED)) {
                edit.putBoolean(KEY_HEY_ZERO_ENABLED, it.getBooleanExtra(KEY_HEY_ZERO_ENABLED, false))
            }
            if (it.hasExtra(KEY_OLED_DARK_MODE)) {
                edit.putBoolean(KEY_OLED_DARK_MODE, it.getBooleanExtra(KEY_OLED_DARK_MODE, true))
            }
            if (it.hasExtra(KEY_WRIST_ORIENTATION_GATE)) {
                edit.putBoolean(KEY_WRIST_ORIENTATION_GATE, it.getBooleanExtra(KEY_WRIST_ORIENTATION_GATE, true))
            }
            if (it.hasExtra(KEY_KEEP_SCREEN_ON)) {
                edit.putBoolean(KEY_KEEP_SCREEN_ON, it.getBooleanExtra(KEY_KEEP_SCREEN_ON, true))
            }
            if (it.hasExtra(KEY_CHIMES_ENABLED)) {
                edit.putBoolean(KEY_CHIMES_ENABLED, it.getBooleanExtra(KEY_CHIMES_ENABLED, true))
            }
            edit.apply()
        }

        var gwUrl = prefs.getString(KEY_GATEWAY_URL, GotchiWatchEngine.DEFAULT_GATEWAY_URL)
            ?: GotchiWatchEngine.DEFAULT_GATEWAY_URL
        if (gwUrl.contains(":8765") || gwUrl.contains(":8769")) {
            gwUrl = gwUrl.replace(":8765", ":8771").replace(":8769", ":8771")
            prefs.edit().putString(KEY_GATEWAY_URL, gwUrl).apply()
        } else if (gwUrl.isBlank()) {
            gwUrl = GotchiWatchEngine.DEFAULT_GATEWAY_URL
        }
        engine.config.gatewayUrl = gwUrl
        engine.config.bearerToken = prefs.getString(KEY_BEARER_TOKEN, "") ?: ""
        engine.config.nodeId = prefs.getString(KEY_NODE_ID, GotchiWatchEngine.DEFAULT_NODE_ID)
            ?: GotchiWatchEngine.DEFAULT_NODE_ID
        engine.config.audioOutMode = prefs.getString(KEY_AUDIO_OUT_MODE, "speaker") ?: "speaker"
        engine.config.pttSilenceMs = prefs.getLong(KEY_PTT_SILENCE_MS, GotchiWatchEngine.DEFAULT_PTT_SILENCE_MS)
        engine.config.heyZeroWakeEnabled = prefs.getBoolean(KEY_HEY_ZERO_ENABLED, false)
        epaperView.uiState.skin = prefs.getString(KEY_SKIN, "zero") ?: "zero"
        val oledDark = prefs.getBoolean(KEY_OLED_DARK_MODE, true)
        epaperView.setOledDarkMode(oledDark)
        preRollListener.requireWristOrientation = prefs.getBoolean(KEY_WRIST_ORIENTATION_GATE, true)
        val keepAwake = prefs.getBoolean(KEY_KEEP_SCREEN_ON, true)
        applyKeepScreenOnFlag(keepAwake)
        engine.config.chimesEnabled = prefs.getBoolean(KEY_CHIMES_ENABLED, true)
        epaperView.setWakeListenerActive(engine.config.heyZeroWakeEnabled)
    }

    private fun syncImuSuppressionState(state: String?) {
        // Suppress IMU during server-side processing to prevent false triggers;
        // pinching is allowed during idle (start PTT), listening (stop PTT / commit), and speaking (barge-in)
        pinchDetector.isSuppressed = (state in setOf("transcribing", "thinking", "synthesizing"))
        if (state != "idle") {
            preRollListener.pauseForActiveTurn()
        } else if (state == "idle" && engine.config.heyZeroWakeEnabled) {
            preRollListener.resumeAfterTurn()
        }
        epaperView.setPttActive(state == "listening" || engine.isRecording())
    }

    /**
     * Dispatches action when a physical finger-pinch, double-pinch, or stem button event occurs.
     */
    internal fun handlePinchOrStemTrigger(source: String) {
        if (engine.isRecording()) {
            engine.stopPttAndCommit()
            return
        }
        if (engine.hasPendingAnswer && !engine.isRecording()) {
            engine.fetchAndPlayPendingAnswer()
            return
        }
        val currentState = epaperView.uiState.state
        val action = pinchDetector.resolveActionForState(currentState)

        when (action) {
            PinchGestureDetector.PinchAction.START_PTT -> {
                // Snapshot pre-roll and cleanly stop/release HeyZero AudioRecord before PTT starts
                if (preRollListener.isListening()) {
                    val preRoll = preRollListener.releaseAudioRecordForHandoff()
                    engine.startWakeWordTurn(preRoll)
                } else {
                    preRollListener.pauseForActiveTurn()
                    engine.startPttCapture(autoVadStop = true)
                }
            }
            PinchGestureDetector.PinchAction.STOP_PTT_AND_COMMIT -> {
                engine.stopPttAndCommit()
            }
            PinchGestureDetector.PinchAction.TRIGGER_BARGE_IN -> {
                engine.triggerBargeIn()
            }
            PinchGestureDetector.PinchAction.NONE -> {
                // Toggle fallback if in transcribing or error
                if (currentState in setOf("error", "offline")) {
                    preRollListener.pauseForActiveTurn()
                    engine.startPttCapture(autoVadStop = true)
                }
            }
        }
    }

    /**
     * Dispatches action when a native wrist-turn gesture (ACTION_DISMISS / GESTURE_WRIST_TURN) occurs:
     * - Skips a pending answer if one is held
     * - Cancels an active PTT recording
     * - Or stops playback (barge-in)
     */
    internal fun handleDismissTrigger(source: String) {
        if (engine.hasPendingAnswer) {
            engine.skipPendingAnswer()
            Toast.makeText(this, "SKIPPED ANSWER", Toast.LENGTH_SHORT).show()
        } else if (engine.isRecording()) {
            engine.cancelPttCapture()
        } else {
            engine.triggerBargeIn()
        }
    }

    /**
     * Dispatch native Wear OS One-Handed Gesture events to PinchGestureDetector.
     */
    fun onGestureEvent(event: PinchGestureDetector.GestureEvent): Boolean {
        return pinchDetector.onGestureEvent(event)
    }

    /**
     * Dispatches turn when 'Hey Zero' wake word is detected by the continuous pre-roll listener.
     */
    internal fun handleWakeWordDetected(preRollPcm: ByteArray) {
        preRollListener.pauseForActiveTurn()
        engine.startWakeWordTurn(preRollPcm)
    }

    /**
     * Rotary Crown hardware scrolling (`MotionEvent.AXIS_SCROLL`):
     * Wear OS Pixel Watch rotary side crown cycles companion skins or adjusts settings.
     */
    override fun onGenericMotionEvent(event: MotionEvent?): Boolean {
        if (event?.action == MotionEvent.ACTION_SCROLL) {
            val delta = -event.getAxisValue(MotionEvent.AXIS_SCROLL)
            if (delta > 0.2f) {
                cycleSkinForward()
                return true
            } else if (delta < -0.2f) {
                cycleSkinBackward()
                return true
            }
        }
        return super.onGenericMotionEvent(event)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (event != null && pinchDetector.onKeyEvent(event)) {
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onKeyUp(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode in PinchGestureDetector.SUPPORTED_KEY_CODES) {
            return true
        }
        return super.onKeyUp(keyCode, event)
    }

    private fun setupTouchInteractions(view: View) {
        var pendingTapAction: Runnable? = null

        view.setOnTouchListener { _, event ->
            val now = SystemClock.elapsedRealtime()
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    pendingTapAction?.let { view.removeCallbacks(it) }
                    tapDownMs = now
                    isLongPressed = false
                    view.postDelayed({
                        if (tapDownMs != 0L && (SystemClock.elapsedRealtime() - tapDownMs) >= LONG_PRESS_MS) {
                            isLongPressed = true
                            if (engine.hasPendingAnswer) {
                                engine.skipPendingAnswer()
                                Toast.makeText(this, "SKIPPED ANSWER", Toast.LENGTH_SHORT).show()
                            } else {
                                toggleHeyZeroWakeMode()
                            }
                        }
                    }, LONG_PRESS_MS)
                    true
                }
                MotionEvent.ACTION_UP -> {
                    val heldMs = now - tapDownMs
                    tapDownMs = 0L
                    if (!isLongPressed && heldMs < LONG_PRESS_MS) {
                        if ((now - lastTapDownMs) <= TRIPLE_TAP_WINDOW_MS) {
                            tapCount++
                        } else {
                            tapCount = 1
                        }
                        lastTapDownMs = now

                        if (tapCount >= 3) {
                            tapCount = 0
                            lastTapDownMs = 0L
                            pendingTapAction?.let { view.removeCallbacks(it) }
                            pendingTapAction = null
                            showWatchSettingsDialog()
                        } else {
                            pendingTapAction = Runnable {
                                val count = tapCount
                                tapCount = 0
                                lastTapDownMs = 0L
                                if (count == 2) {
                                    toggleLiveSession()
                                } else if (count == 1) {
                                    handlePinchOrStemTrigger("touch_tap")
                                }
                            }
                            view.postDelayed(pendingTapAction!!, DOUBLE_TAP_WINDOW_MS)
                        }
                    }
                    true
                }
                MotionEvent.ACTION_CANCEL -> {
                    pendingTapAction?.let { view.removeCallbacks(it) }
                    pendingTapAction = null
                    tapDownMs = 0L
                    isLongPressed = false
                    tapCount = 0
                    lastTapDownMs = 0L
                    true
                }
                else -> false
            }
        }
    }

    internal fun showUpdateAvailablePrompt(remoteVersion: Int) {
        AlertDialog.Builder(this)
            .setTitle("Update Available")
            .setMessage("Version $remoteVersion is available. Install now?")
            .setPositiveButton("Install") { _, _ ->
                GotchiWatchUpdater.downloadAndInstallApk(this, engine.config.gatewayUrl, engine.config.bearerToken)
            }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun toggleLiveSession() {
        if (engine.isLiveActive()) {
            engine.stopLiveSession()
        } else {
            preRollListener.pauseForActiveTurn()
            engine.startLiveSession()
        }
    }

    private fun toggleHeyZeroWakeMode() {
        val next = !engine.config.heyZeroWakeEnabled
        engine.config.heyZeroWakeEnabled = next
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_HEY_ZERO_ENABLED, next)
            .apply()

        if (next) {
            preRollListener.start()
            epaperView.setWakeListenerActive(true)
            Toast.makeText(this, "HEY ZERO: ON", Toast.LENGTH_SHORT).show()
        } else {
            preRollListener.stop()
            epaperView.setWakeListenerActive(false)
            Toast.makeText(this, "HEY ZERO: OFF", Toast.LENGTH_SHORT).show()
        }
        epaperView.updateState(statusText = if (next) "HEY ZERO ON" else "HEY ZERO OFF")
    }

    private fun cycleSkinForward() {
        val next = when (epaperView.uiState.skin) {
            "zero" -> "amos"
            "amos" -> "gotchi"
            else -> "zero"
        }
        epaperView.setSkin(next)
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_SKIN, next)
            .apply()
    }

    private fun cycleSkinBackward() {
        val prev = when (epaperView.uiState.skin) {
            "zero" -> "gotchi"
            "gotchi" -> "amos"
            else -> "zero"
        }
        epaperView.setSkin(prev)
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_SKIN, prev)
            .apply()
    }

    private fun showWatchSettingsDialog() {
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(16, 16, 16, 16)
            setBackgroundColor(Color.rgb(20, 20, 28))
        }

        val skinBtn = Button(this).apply {
            text = "SKIN: ${epaperView.uiState.skin.uppercase()}"
            setOnClickListener {
                cycleSkinForward()
                text = "SKIN: ${epaperView.uiState.skin.uppercase()}"
            }
        }

        val wakeBtn = Button(this).apply {
            text = "HEY ZERO: ${if (engine.config.heyZeroWakeEnabled) "ON" else "OFF"}"
            setOnClickListener {
                toggleHeyZeroWakeMode()
                text = "HEY ZERO: ${if (engine.config.heyZeroWakeEnabled) "ON" else "OFF"}"
            }
        }

        var keepAwake = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).getBoolean(KEY_KEEP_SCREEN_ON, true)
        val keepAwakeBtn = Button(this).apply {
            text = "KEEP AWAKE: ${if (keepAwake) "ON" else "OFF"}"
            setOnClickListener {
                keepAwake = !keepAwake
                applyKeepScreenOnFlag(keepAwake)
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putBoolean(KEY_KEEP_SCREEN_ON, keepAwake)
                    .apply()
                text = "KEEP AWAKE: ${if (keepAwake) "ON" else "OFF"}"
            }
        }

        val chimesBtn = Button(this).apply {
            text = "CHIMES: ${if (engine.config.chimesEnabled) "ON" else "OFF"}"
            setOnClickListener {
                val next = !engine.config.chimesEnabled
                engine.config.chimesEnabled = next
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putBoolean(KEY_CHIMES_ENABLED, next)
                    .apply()
                text = "CHIMES: ${if (next) "ON" else "OFF"}"
            }
        }

        val oledBtn = Button(this).apply {
            text = "OLED DARK: ${if (epaperView.uiState.oledDarkMode) "ON" else "OFF"}"
            setOnClickListener {
                val next = !epaperView.uiState.oledDarkMode
                epaperView.setOledDarkMode(next)
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putBoolean(KEY_OLED_DARK_MODE, next)
                    .apply()
                text = "OLED DARK: ${if (next) "ON" else "OFF"}"
            }
        }

        val wristBtn = Button(this).apply {
            text = "WRIST GATE: ${if (preRollListener.requireWristOrientation) "ON" else "OFF"}"
            setOnClickListener {
                val next = !preRollListener.requireWristOrientation
                preRollListener.requireWristOrientation = next
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putBoolean(KEY_WRIST_ORIENTATION_GATE, next)
                    .apply()
                text = "WRIST GATE: ${if (next) "ON" else "OFF"}"
            }
        }

        val vadBtn = Button(this).apply {
            text = "VAD: ${engine.config.pttSilenceMs}ms"
            setOnClickListener {
                engine.config.pttSilenceMs = when (engine.config.pttSilenceMs) {
                    1500L -> 800L
                    800L -> 1000L
                    else -> 1500L
                }
                text = "VAD: ${engine.config.pttSilenceMs}ms"
            }
        }

        val updateAppBtn = Button(this).apply {
            text = "UPDATE APP (WI-FI)"
            setOnClickListener {
                Toast.makeText(this@GotchiWatchActivity, "STARTING UPDATE...", Toast.LENGTH_SHORT).show()
                GotchiWatchUpdater.downloadAndInstallApk(
                    this@GotchiWatchActivity,
                    engine.config.gatewayUrl,
                    engine.config.bearerToken
                ) { status ->
                    runOnUiThread {
                        Toast.makeText(this@GotchiWatchActivity, status, Toast.LENGTH_SHORT).show()
                    }
                }
            }
        }

        val gatewayInput = EditText(this).apply {
            hint = "Gateway URL (https://gotchi.brock.ventures)"
            setText(engine.config.gatewayUrl)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        val bearerInput = EditText(this).apply {
            hint = "Bearer Token (Tunnel Auth)"
            setText(engine.config.bearerToken)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        val nodeInput = EditText(this).apply {
            hint = "Node ID"
            setText(engine.config.nodeId)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
        }

        panel.addView(skinBtn)
        panel.addView(wakeBtn)
        panel.addView(keepAwakeBtn)
        panel.addView(chimesBtn)
        panel.addView(oledBtn)
        panel.addView(wristBtn)
        panel.addView(vadBtn)
        panel.addView(updateAppBtn)
        panel.addView(gatewayInput)
        panel.addView(bearerInput)
        panel.addView(nodeInput)

        val scrollView = ScrollView(this).apply {
            addView(panel)
        }

        AlertDialog.Builder(this)
            .setTitle("Gotchi Watch")
            .setView(scrollView)
            .setPositiveButton("Save") { _, _ ->
                engine.config.gatewayUrl = GotchiWatchEngine.normalizeGatewayUrl(gatewayInput.text.toString())
                engine.config.bearerToken = bearerInput.text.toString().trim()
                engine.config.nodeId = nodeInput.text.toString().trim().ifEmpty { GotchiWatchEngine.DEFAULT_NODE_ID }
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putString(KEY_GATEWAY_URL, engine.config.gatewayUrl)
                    .putString(KEY_BEARER_TOKEN, engine.config.bearerToken)
                    .putString(KEY_NODE_ID, engine.config.nodeId)
                    .putLong(KEY_PTT_SILENCE_MS, engine.config.pttSilenceMs)
                    .putString(KEY_SKIN, epaperView.uiState.skin)
                    .putBoolean(KEY_HEY_ZERO_ENABLED, engine.config.heyZeroWakeEnabled)
                    .putBoolean(KEY_OLED_DARK_MODE, epaperView.uiState.oledDarkMode)
                    .putBoolean(KEY_WRIST_ORIENTATION_GATE, preRollListener.requireWristOrientation)
                    .putBoolean(KEY_KEEP_SCREEN_ON, keepAwake)
                    .putBoolean(KEY_CHIMES_ENABLED, engine.config.chimesEnabled)
                    .apply()
                epaperView.updateState(statusText = "SAVED")
            }
            .setNegativeButton("Cancel", null)
            .show()
    }
}
