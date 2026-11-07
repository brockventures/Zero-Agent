package ventures.brock.gotchi.forwarder

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.io.ByteArrayOutputStream
import kotlin.math.hypot

/**
 * Unified Gotchi Phone App Launcher Activity (`ventures.brock.gotchi.forwarder`).
 *
 * Renders the Translucent Atomic Purple Gotchi V2 Handheld UI:
 * - Translucent `#CC6B2CF5` / `#8A3FFC` resin shell over internal matte-black V2.5 PCB window
 *   with gold ENIG trace rails and CNC 6061 bead-blasted aluminum perimeter frame.
 * - Top-bezel glowing BLE Relay Status LED pill showing `BLE RELAY: ACTIVE` / `BONDED` vs `STANDALONE`.
 * - Central recessed OLED viewport embedding [GotchiPhoneViewportView] with full-color HD Zero avatar sprites.
 * - Lower-shell tactile `BTN_TOP (PTT / GO LIVE)` and `BTN_BOT (VISION / SNAP)` camera snapshot buttons
 *   next to the cyan-coated `OV5640` lens barrel and diffused bottom LED glow bar.
 * - Top-right Hamburger Menu (`☰`) opening the runtime Settings & Desk Gotchi BLE Relay drawer:
 *   - Gateway URL, Bearer Token, Node ID, and Audio Output Mode.
 *   - Scan & Bond button triggering OS-level BLE pairing and background relay service.
 *   - Disconnect / Disable BLE Relay button and Wi-Fi App Update controls.
 *   - "Hey Zero" Wake-Word continuous spotter toggle with smart arbitration (`isRelayTurnActive`).
 */
class GotchiBleBondingActivity : Activity() {

    companion object {
        private const val PERMISSION_REQUEST_CODE = 4743
        private const val DOUBLE_TAP_WINDOW_MS = 350L
        private const val BURST_HOLD_MS = 500L
        const val KEY_WAKE_WORD_ENABLED = "key_wake_word_enabled"
        const val DEFAULT_FALLBACK_URL = "https://gotchi.brock.ventures"
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    // Engine & UI Viewport
    private lateinit var engine: GotchiPhoneEngine
    private lateinit var viewportView: GotchiPhoneViewportView
    private lateinit var chassisView: AtomicPurpleChassisView
    private lateinit var bleStatusPill: TextView

    // Settings drawer components (kept accessible for tests and background updates)
    internal lateinit var statusTextView: TextView
    internal lateinit var deviceTextView: TextView
    internal lateinit var urlEditText: EditText
    internal lateinit var tokenEditText: EditText
    internal lateinit var nodeEditText: EditText
    internal lateinit var audioRadioGroup: RadioGroup
    internal lateinit var radioSpeaker: RadioButton
    internal lateinit var radioPhone: RadioButton
    internal lateinit var wakeWordCheckBox: CheckBox
    private var settingsDialog: AlertDialog? = null
    private lateinit var settingsLayout: View

    // Button gesture tracking
    private var lastBtnTopDownMs: Long = 0L
    private var btnTopHoldStartMs: Long = 0L
    private var btnBotDownMs: Long = 0L
    private var isLiveSession: Boolean = false

    private val statusPollRunnable = object : Runnable {
        override fun run() {
            updateStatusUi()
            mainHandler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableFullScreenImmersiveMode()

        initEngineAndViewport()
        initSettingsComponents()
        setContentView(buildContentView())
        loadPreferences()
        checkAndRequestPermissions()
    }

    override fun onResume() {
        super.onResume()
        enableFullScreenImmersiveMode()
        updateStatusUi()
        mainHandler.post(statusPollRunnable)

        val onUpdate: (Int) -> Unit = { remoteVersion ->
            showUpdateAvailablePrompt(remoteVersion)
        }
        GotchiForwarderUpdater.checkRemoteUpdate(this, onUpdate)

        // Resume wake-word listener if enabled
        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
        val wakeEnabled = prefs.getBoolean(KEY_WAKE_WORD_ENABLED, true)
        if (wakeEnabled && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            if (engine.preRollListener == null) {
                engine.initWakeWordListener(this).start()
            }
        }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            enableFullScreenImmersiveMode()
        }
    }

    private fun enableFullScreenImmersiveMode() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let { controller ->
                controller.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                controller.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                    or View.SYSTEM_UI_FLAG_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            )
        }
    }

    override fun onPause() {
        super.onPause()
        mainHandler.removeCallbacks(statusPollRunnable)
        engine.preRollListener?.stop()
        engine.preRollListener = null
    }

    private fun dp(dpVal: Int): Int {
        return (dpVal * resources.displayMetrics.density).toInt()
    }

    private fun createCardDrawable(bgColor: Int, strokeColor: Int = Color.parseColor("#2A2A2A")): GradientDrawable {
        return GradientDrawable().apply {
            setColor(bgColor)
            setStroke(dp(1), strokeColor)
            cornerRadius = dp(12).toFloat()
        }
    }

    private fun initEngineAndViewport() {
        viewportView = GotchiPhoneViewportView(this)
        engine = GotchiPhoneEngine { state, transcript, reply, status, audioBytes, sampleRate ->
            mainHandler.post {
                viewportView.updateState(
                    newState = state,
                    transcript = transcript,
                    reply = reply,
                    statusText = status,
                    audioBytesDelta = audioBytes,
                    sampleRate = sampleRate
                )
                if (::chassisView.isInitialized) {
                    chassisView.invalidate()
                }
            }
        }
    }

    private fun buildContentView(): View {
        val rootLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#0C0A16"))
            setPadding(dp(2), dp(2), dp(2), dp(4))
        }

        // Top-Bezel Header Bar
        val headerRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(6), dp(2), dp(6), dp(8))
        }

        val titleView = TextView(this).apply {
            text = "GOTCHI V2"
            textSize = 15f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#D4C4FC"))
        }
        val titleParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        headerRow.addView(titleView, titleParams)

        // Top-Bezel Glowing BLE Relay Status LED Pill
        bleStatusPill = TextView(this).apply {
            text = "BLE RELAY: STANDALONE"
            textSize = 10.5f
            typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
            setTextColor(Color.parseColor("#B0BEC5"))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#181428"))
                setStroke(dp(1), Color.parseColor("#382858"))
                cornerRadius = dp(12).toFloat()
            }
            setPadding(dp(8), dp(4), dp(8), dp(4))
            gravity = Gravity.CENTER
        }
        val pillParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            rightMargin = dp(8)
        }
        headerRow.addView(bleStatusPill, pillParams)

        // Top-Right Hamburger Menu (☰) Settings Drawer Button
        val hamburgerBtn = TextView(this).apply {
            text = "☰"
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#261E3E"))
                setStroke(dp(1), Color.parseColor("#4C3A7A"))
                cornerRadius = dp(8).toFloat()
            }
            setPadding(dp(8), dp(4), dp(8), dp(4))
            setOnClickListener {
                showHamburgerSettingsDialog()
            }
        }
        headerRow.addView(hamburgerBtn, LinearLayout.LayoutParams(dp(36), dp(34)))
        rootLayout.addView(headerRow)

        // Main Hardware Enclosure Viewport: Atomic Purple V2 Handheld Shell
        chassisView = AtomicPurpleChassisView(this, viewportView)
        val chassisParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            0,
            1f
        )
        rootLayout.addView(chassisView, chassisParams)

        return rootLayout
    }

    private fun initSettingsComponents() {
        val scrollView = ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#121212"))
            isFillViewport = true
        }

        val rootLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(24))
        }

        // Header Title
        val titleView = TextView(this).apply {
            text = "GOTCHI BLE RELAY"
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#00E676"))
            setPadding(0, 0, 0, dp(4))
        }
        rootLayout.addView(titleView)

        val subtitleView = TextView(this).apply {
            text = "Smart Pipe Companion Forwarder & Desk Relay"
            textSize = 12f
            setTextColor(Color.parseColor("#9E9E9E"))
            setPadding(0, 0, 0, dp(16))
        }
        rootLayout.addView(subtitleView)

        // Status Card
        val statusCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = createCardDrawable(Color.parseColor("#1E1E1E"))
            setPadding(dp(14), dp(14), dp(14), dp(14))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dp(16)
            }
            layoutParams = lp
        }

        val statusCardHeader = TextView(this).apply {
            text = "CONNECTION STATUS"
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#B0BEC5"))
            setPadding(0, 0, 0, dp(6))
        }
        statusCard.addView(statusCardHeader)

        statusTextView = TextView(this).apply {
            text = "Status: IDLE"
            textSize = 15f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#B0BEC5"))
            setPadding(0, 0, 0, dp(4))
        }
        statusCard.addView(statusTextView)

        deviceTextView = TextView(this).apply {
            text = "Device: None"
            textSize = 13f
            setTextColor(Color.parseColor("#E0E0E0"))
        }
        statusCard.addView(deviceTextView)
        rootLayout.addView(statusCard)

        // Configuration Card
        val configCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = createCardDrawable(Color.parseColor("#1E1E1E"))
            setPadding(dp(14), dp(14), dp(14), dp(14))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dp(16)
            }
            layoutParams = lp
        }

        val configHeader = TextView(this).apply {
            text = "GATEWAY CONFIGURATION"
            textSize = 11f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#81C784"))
            setPadding(0, 0, 0, dp(10))
        }
        configCard.addView(configHeader)

        // Gateway URL Field
        configCard.addView(createLabel("Gateway URL (LAN or Cloudflare Tunnel: https://gotchi.brock.ventures)"))
        urlEditText = EditText(this).apply {
            hint = DEFAULT_FALLBACK_URL
            setHintTextColor(Color.parseColor("#616161"))
            setTextColor(Color.WHITE)
            textSize = 13.5f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            background = createCardDrawable(Color.parseColor("#262626"), Color.parseColor("#424242"))
            setPadding(dp(10), dp(8), dp(10), dp(8))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dp(10)
            }
            layoutParams = lp
        }
        configCard.addView(urlEditText)

        // Bearer Token Field
        configCard.addView(createLabel("Bearer Token (Optional)"))
        tokenEditText = EditText(this).apply {
            hint = "Leave blank if no auth required"
            setHintTextColor(Color.parseColor("#616161"))
            setTextColor(Color.WHITE)
            textSize = 13.5f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            background = createCardDrawable(Color.parseColor("#262626"), Color.parseColor("#424242"))
            setPadding(dp(10), dp(8), dp(10), dp(8))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dp(10)
            }
            layoutParams = lp
        }
        configCard.addView(tokenEditText)

        // Node ID Field
        configCard.addView(createLabel("Node ID"))
        nodeEditText = EditText(this).apply {
            hint = GotchiBleRelayService.DEFAULT_NODE_ID
            setHintTextColor(Color.parseColor("#616161"))
            setTextColor(Color.WHITE)
            textSize = 13.5f
            inputType = InputType.TYPE_CLASS_TEXT
            background = createCardDrawable(Color.parseColor("#262626"), Color.parseColor("#424242"))
            setPadding(dp(10), dp(8), dp(10), dp(8))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dp(10)
            }
            layoutParams = lp
        }
        configCard.addView(nodeEditText)

        // Audio Output Mode Field
        configCard.addView(createLabel("Audio Output Mode"))
        audioRadioGroup = RadioGroup(this).apply {
            orientation = RadioGroup.VERTICAL
            setPadding(0, dp(2), 0, dp(6))
        }

        radioSpeaker = RadioButton(this).apply {
            text = "Gotchi Speaker (4-bit IMA ADPCM over BLE)"
            setTextColor(Color.parseColor("#E0E0E0"))
            textSize = 12.5f
            id = View.generateViewId()
        }
        audioRadioGroup.addView(radioSpeaker)

        radioPhone = RadioButton(this).apply {
            text = "Phone Audio (Earbuds / Phone Speaker)"
            setTextColor(Color.parseColor("#E0E0E0"))
            textSize = 12.5f
            id = View.generateViewId()
        }
        audioRadioGroup.addView(radioPhone)
        configCard.addView(audioRadioGroup)

        // Wake-Word Arbitration Field
        configCard.addView(createLabel("Wake-Word Continuous Spotter"))
        wakeWordCheckBox = CheckBox(this).apply {
            text = "Enable 'Hey Zero' continuous wake phrase\n(Auto-suppressed when Desk Gotchi BLE turns active)"
            setTextColor(Color.parseColor("#E0E0E0"))
            textSize = 12f
            isChecked = true
            setPadding(dp(6), dp(4), 0, dp(8))
        }
        configCard.addView(wakeWordCheckBox)

        rootLayout.addView(configCard)

        // Scan & Bond Button
        val scanBondButton = Button(this).apply {
            text = "SCAN & BOND TO GOTCHI"
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.BLACK)
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#00E676"))
                cornerRadius = dp(10).toFloat()
            }
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(12), dp(14), dp(12))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
            layoutParams = lp
            setOnClickListener {
                onScanAndBondClicked()
            }
        }
        rootLayout.addView(scanBondButton)

        // Manual Wi-Fi App Update Button
        val updateAppButton = Button(this).apply {
            text = "UPDATE APP (WI-FI)"
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#2979FF"))
                cornerRadius = dp(10).toFloat()
            }
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(12), dp(14), dp(12))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(10)
            }
            layoutParams = lp
            setOnClickListener {
                Toast.makeText(this@GotchiBleBondingActivity, "STARTING UPDATE...", Toast.LENGTH_SHORT).show()
                val url = if (::urlEditText.isInitialized) urlEditText.text.toString().trim() else ""
                GotchiForwarderUpdater.downloadAndInstallApk(this@GotchiBleBondingActivity, url) { status ->
                    Toast.makeText(this@GotchiBleBondingActivity, status, Toast.LENGTH_SHORT).show()
                }
            }
        }
        rootLayout.addView(updateAppButton)

        // Disconnect / Disable BLE Relay Button
        val disconnectButton = Button(this).apply {
            text = "DISCONNECT / DISABLE BLE RELAY"
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#FF5252"))
                cornerRadius = dp(10).toFloat()
            }
            gravity = Gravity.CENTER
            setPadding(dp(14), dp(12), dp(14), dp(12))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(10)
            }
            layoutParams = lp
            setOnClickListener {
                onDisconnectClicked()
            }
        }
        rootLayout.addView(disconnectButton)

        scrollView.addView(rootLayout)
        settingsLayout = scrollView
    }

    private fun createLabel(text: String): TextView {
        return TextView(this).apply {
            this.text = text
            textSize = 11.5f
            setTextColor(Color.parseColor("#B0BEC5"))
            setPadding(0, 0, 0, dp(4))
        }
    }

    fun showHamburgerSettingsDialog() {
        showSettingsDialog()
    }

    fun showSettingsDialog() {
        if (isFinishing || isDestroyed) return
        updateStatusUi()

        if (settingsDialog == null) {
            settingsDialog = AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
                .setTitle("GOTCHI SETTINGS & BLE RELAY")
                .setView(settingsLayout)
                .setPositiveButton("SAVE & CLOSE") { dialog, _ ->
                    val audioMode = savePreferences()
                    val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
                    if (prefs.getBoolean(GotchiBleRelayService.KEY_RELAY_ENABLED, true)) {
                        val url = urlEditText.text.toString().trim().ifBlank { DEFAULT_FALLBACK_URL }
                        val token = tokenEditText.text.toString().trim()
                        val node = nodeEditText.text.toString().trim().ifBlank { GotchiBleRelayService.DEFAULT_NODE_ID }
                        val syncIntent = Intent(this, GotchiBleRelayService::class.java).apply {
                            action = GotchiBleRelayService.ACTION_SYNC_CONFIG
                            putExtra(GotchiBleRelayService.KEY_GATEWAY_URL, url)
                            putExtra(GotchiBleRelayService.KEY_BEARER_TOKEN, token)
                            putExtra(GotchiBleRelayService.KEY_NODE_ID, node)
                            putExtra(GotchiBleRelayService.KEY_AUDIO_OUT_MODE, audioMode)
                        }
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(syncIntent)
                        } else {
                            startService(syncIntent)
                        }
                    }
                    dialog.dismiss()
                }
                .setNegativeButton("CANCEL", null)
                .create()
        }
        settingsDialog?.show()
    }

    private fun loadPreferences() {
        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
        val url = prefs.getString(GotchiBleRelayService.KEY_GATEWAY_URL, DEFAULT_FALLBACK_URL) ?: DEFAULT_FALLBACK_URL
        val token = prefs.getString(GotchiBleRelayService.KEY_BEARER_TOKEN, "") ?: ""
        val node = prefs.getString(GotchiBleRelayService.KEY_NODE_ID, GotchiBleRelayService.DEFAULT_NODE_ID) ?: GotchiBleRelayService.DEFAULT_NODE_ID
        val audioMode = prefs.getString(GotchiBleRelayService.KEY_AUDIO_OUT_MODE, GotchiBleRelayService.DEFAULT_AUDIO_OUT_MODE) ?: GotchiBleRelayService.DEFAULT_AUDIO_OUT_MODE
        val wakeEnabled = prefs.getBoolean(KEY_WAKE_WORD_ENABLED, true)

        urlEditText.setText(url)
        tokenEditText.setText(token)
        nodeEditText.setText(node)
        wakeWordCheckBox.isChecked = wakeEnabled

        if (audioMode == "ble_phone") {
            radioPhone.isChecked = true
        } else {
            radioSpeaker.isChecked = true
        }

        // Apply to local GotchiPhoneEngine
        engine.config.gatewayUrl = url.ifBlank { DEFAULT_FALLBACK_URL }
        engine.config.bearerToken = token
        engine.config.nodeId = node.ifBlank { GotchiBleRelayService.DEFAULT_NODE_ID }
        engine.config.audioOutMode = audioMode
        engine.config.heyZeroWakeEnabled = wakeEnabled
    }

    private fun savePreferences(): String {
        val url = urlEditText.text.toString().trim().ifBlank { DEFAULT_FALLBACK_URL }
        val token = tokenEditText.text.toString().trim()
        val node = nodeEditText.text.toString().trim().ifBlank { GotchiBleRelayService.DEFAULT_NODE_ID }
        val audioMode = if (radioPhone.isChecked) "ble_phone" else "speaker"
        val wakeEnabled = wakeWordCheckBox.isChecked

        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE).edit()
        prefs.putString(GotchiBleRelayService.KEY_GATEWAY_URL, url)
        prefs.putString(GotchiBleRelayService.KEY_BEARER_TOKEN, token)
        prefs.putString(GotchiBleRelayService.KEY_NODE_ID, node)
        prefs.putString(GotchiBleRelayService.KEY_AUDIO_OUT_MODE, audioMode)
        prefs.putBoolean(KEY_WAKE_WORD_ENABLED, wakeEnabled)
        prefs.apply()

        engine.config.gatewayUrl = url
        engine.config.bearerToken = token
        engine.config.nodeId = node
        engine.config.audioOutMode = audioMode
        engine.config.heyZeroWakeEnabled = wakeEnabled

        if (wakeEnabled && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            if (engine.preRollListener == null) {
                engine.initWakeWordListener(this).start()
            }
        } else {
            engine.preRollListener?.stop()
            engine.preRollListener = null
        }

        return audioMode
    }

    private fun checkAndRequestPermissions() {
        val permissions = mutableListOf<String>()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) != PackageManager.PERMISSION_GRANTED) {
                permissions.add(Manifest.permission.BLUETOOTH_SCAN)
            }
            if (checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) {
                permissions.add(Manifest.permission.BLUETOOTH_CONNECT)
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                permissions.add(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        if (checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.ACCESS_COARSE_LOCATION)
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.RECORD_AUDIO)
        }
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            permissions.add(Manifest.permission.CAMERA)
        }

        if (permissions.isNotEmpty()) {
            requestPermissions(permissions.toTypedArray(), PERMISSION_REQUEST_CODE)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            val allGranted = grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }
            if (!allGranted) {
                Toast.makeText(
                    this,
                    "Microphone & BLE permissions recommended for full voice engine and relay",
                    Toast.LENGTH_LONG
                ).show()
            } else {
                if (wakeWordCheckBox.isChecked && engine.preRollListener == null) {
                    engine.initWakeWordListener(this).start()
                }
            }
        }
    }

    private var pendingBondReceiver: BroadcastReceiver? = null

    private fun unregisterPendingBondReceiver() {
        val receiver = pendingBondReceiver ?: return
        pendingBondReceiver = null
        runCatching { unregisterReceiver(receiver) }
    }

    override fun onDestroy() {
        unregisterPendingBondReceiver()
        super.onDestroy()
    }

    @SuppressLint("MissingPermission")
    internal fun bondAndConnectDevice(
        device: BluetoothDevice,
        onReadyToConnect: (BluetoothDevice) -> Unit
    ) {
        unregisterPendingBondReceiver()
        when (device.bondState) {
            BluetoothDevice.BOND_BONDED -> {
                onReadyToConnect(device)
            }
            BluetoothDevice.BOND_BONDING -> {
                GotchiBleRelayService.connectionStatus = "BONDING"
                updateStatusUi()
                waitForDeviceBondedThenConnect(device, onReadyToConnect)
            }
            else -> {
                GotchiBleRelayService.connectionStatus = "BONDING"
                updateStatusUi()
                waitForDeviceBondedThenConnect(device, onReadyToConnect)
                runCatching { device.createBond() }
            }
        }
    }

    private fun waitForDeviceBondedThenConnect(
        targetDevice: BluetoothDevice,
        onReadyToConnect: (BluetoothDevice) -> Unit
    ) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                if (intent?.action != BluetoothDevice.ACTION_BOND_STATE_CHANGED) return
                @Suppress("DEPRECATION")
                val dev = intent.getParcelableExtra<BluetoothDevice>(BluetoothDevice.EXTRA_DEVICE) ?: return
                if (dev.address != targetDevice.address) return
                when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)) {
                    BluetoothDevice.BOND_BONDED -> {
                        unregisterPendingBondReceiver()
                        onReadyToConnect(dev)
                    }
                    BluetoothDevice.BOND_NONE -> {
                        unregisterPendingBondReceiver()
                    }
                }
            }
        }
        pendingBondReceiver = receiver
        val filter = IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(receiver, filter)
        }
    }

    fun onScanAndBondClicked() {
        checkAndRequestPermissions()
        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE).edit()
        prefs.putBoolean(GotchiBleRelayService.KEY_RELAY_ENABLED, true)
        prefs.apply()

        val audioMode = savePreferences()

        val url = urlEditText.text.toString().trim().ifBlank { DEFAULT_FALLBACK_URL }
        val token = tokenEditText.text.toString().trim()
        val node = nodeEditText.text.toString().trim().ifBlank { GotchiBleRelayService.DEFAULT_NODE_ID }

        val serviceIntent = Intent(this, GotchiBleRelayService::class.java).apply {
            action = GotchiBleRelayService.ACTION_RESCAN
            putExtra(GotchiBleRelayService.KEY_GATEWAY_URL, url)
            putExtra(GotchiBleRelayService.KEY_BEARER_TOKEN, token)
            putExtra(GotchiBleRelayService.KEY_NODE_ID, node)
            putExtra(GotchiBleRelayService.KEY_AUDIO_OUT_MODE, audioMode)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }

        Toast.makeText(this, "Scanning for Gotchi over BLE...", Toast.LENGTH_SHORT).show()
        updateStatusUi()
    }

    fun disconnectAndDisableRelay() {
        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE).edit()
        prefs.putBoolean(GotchiBleRelayService.KEY_RELAY_ENABLED, false)
        prefs.apply()

        val serviceIntent = Intent(this, GotchiBleRelayService::class.java).apply {
            action = GotchiBleRelayService.ACTION_DISCONNECT
        }
        startService(serviceIntent)

        GotchiBleRelayService.connectionStatus = "DISABLED"
        GotchiBleRelayService.connectedDeviceName = ""

        Toast.makeText(this, "BLE Relay Disabled", Toast.LENGTH_SHORT).show()
        updateStatusUi()
    }

    fun onDisconnectClicked() {
        disconnectAndDisableRelay()
    }

    private fun updateStatusUi() {
        val prefs = getSharedPreferences(GotchiBleRelayService.PREFS_NAME, Context.MODE_PRIVATE)
        val relayEnabled = prefs.getBoolean(GotchiBleRelayService.KEY_RELAY_ENABLED, true)
        val status = if (!relayEnabled || GotchiBleRelayService.connectionStatus == "DISABLED") {
            "DISABLED"
        } else {
            GotchiBleRelayService.connectionStatus
        }
        val device = GotchiBleRelayService.connectedDeviceName

        if (::statusTextView.isInitialized && ::deviceTextView.isInitialized) {
            if (status == "DISABLED") {
                statusTextView.text = "Status: DISABLED (Wi-Fi Only)"
                statusTextView.setTextColor(Color.parseColor("#FF5252"))
                deviceTextView.text = "Device: None"
            } else {
                statusTextView.text = "Status: $status"
                when (status) {
                    "CONNECTED" -> statusTextView.setTextColor(Color.parseColor("#00E676"))
                    "SCANNING" -> statusTextView.setTextColor(Color.parseColor("#FFD54F"))
                    "CONNECTING", "BONDING" -> statusTextView.setTextColor(Color.parseColor("#448AFF"))
                    "DISCONNECTED" -> statusTextView.setTextColor(Color.parseColor("#FF5252"))
                    else -> statusTextView.setTextColor(Color.parseColor("#B0BEC5"))
                }
                deviceTextView.text = if (device.isNotBlank()) "Device: $device" else "Device: None"
            }
        }

        if (::bleStatusPill.isInitialized) {
            when (status) {
                "CONNECTED" -> {
                    bleStatusPill.text = "● BLE RELAY: ACTIVE"
                    bleStatusPill.setTextColor(Color.parseColor("#00E676"))
                }
                "SCANNING" -> {
                    bleStatusPill.text = "● BLE RELAY: SCANNING"
                    bleStatusPill.setTextColor(Color.parseColor("#FFD54F"))
                }
                "CONNECTING", "BONDING" -> {
                    bleStatusPill.text = "● BLE RELAY: BONDED"
                    bleStatusPill.setTextColor(Color.parseColor("#448AFF"))
                }
                "DISABLED" -> {
                    bleStatusPill.text = "○ BLE RELAY: DISABLED"
                    bleStatusPill.setTextColor(Color.parseColor("#FF5252"))
                }
                else -> {
                    bleStatusPill.text = "○ BLE RELAY: STANDALONE"
                    bleStatusPill.setTextColor(Color.parseColor("#B0BEC5"))
                }
            }
        }
    }

    internal fun showUpdateAvailablePrompt(remoteVersion: Int) {
        if (isFinishing || isDestroyed) return
        AlertDialog.Builder(this)
            .setTitle("Update Available")
            .setMessage("Version $remoteVersion is available. Install now?")
            .setPositiveButton("Install") { _, _ ->
                val url = if (::urlEditText.isInitialized) urlEditText.text.toString().trim() else ""
                GotchiForwarderUpdater.downloadAndInstallApk(this, url) { status ->
                    Toast.makeText(this@GotchiBleBondingActivity, status, Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Later", null)
            .show()
    }

    // Interactive Button Handlers
    private var pendingPttRunnable: Runnable? = null

    private fun handleBtnTopTouch(action: Int) {
        when (action) {
            MotionEvent.ACTION_DOWN -> {
                val now = SystemClock.uptimeMillis()
                if (now - lastBtnTopDownMs <= DOUBLE_TAP_WINDOW_MS) {
                    pendingPttRunnable?.let { mainHandler.removeCallbacks(it) }
                    pendingPttRunnable = null
                    lastBtnTopDownMs = 0L
                    toggleLiveSession()
                    return
                }
                lastBtnTopDownMs = now
                if (isLiveSession) {
                    return
                }
                if (engine.isRecording()) {
                    stopPtt()
                    return
                }
                btnTopHoldStartMs = now
                // Avoid firing duplicate start chimes during rapid double-tap window
                val pttRunnable = Runnable {
                    pendingPttRunnable = null
                    if (!isLiveSession && !engine.isRecording()) {
                        startPtt()
                    }
                }
                pendingPttRunnable = pttRunnable
                mainHandler.postDelayed(pttRunnable, DOUBLE_TAP_WINDOW_MS)
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                pendingPttRunnable?.let {
                    mainHandler.removeCallbacks(it)
                    pendingPttRunnable = null
                }
                if (!isLiveSession && engine.isRecording() && SystemClock.uptimeMillis() - btnTopHoldStartMs >= 500L) {
                    stopPtt()
                }
            }
        }
    }

    private fun startPtt() {
        viewportView.setPttActive(true)
        viewportView.updateState(newState = "listening", statusText = "LISTENING (RELEASE TO SEND)")
        engine.startPttCapture()
    }

    private fun stopPtt() {
        viewportView.setPttActive(false)
        viewportView.updateState(newState = "transcribing", statusText = "TRANSCRIBING...")
        engine.stopPttAndCommit()
    }

    private fun toggleLiveSession() {
        if (!isLiveSession) {
            if (engine.isRecording()) {
                engine.cancelPttCapture()
                viewportView.setPttActive(false)
            }
            isLiveSession = true
            engine.playChime("ready")
            viewportView.updateState(newState = "listening", statusText = "LIVE SESSION ACTIVE (DOUBLE-TAP TO EXIT)")
            engine.startLiveSession()
        } else {
            isLiveSession = false
            engine.playChime("ptt_stop")
            engine.stopLiveSession()
            viewportView.updateState(newState = "idle", statusText = "READY (HOLD PTT)")
        }
    }

    private fun handleBtnBotTouch(action: Int) {
        when (action) {
            MotionEvent.ACTION_DOWN -> {
                btnBotDownMs = SystemClock.uptimeMillis()
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                val duration = SystemClock.uptimeMillis() - btnBotDownMs
                if (duration >= BURST_HOLD_MS) {
                    triggerBurstCapture()
                } else {
                    triggerSnapCapture()
                }
            }
        }
    }

    private fun triggerSnapCapture() {
        engine.playChime("snap")
        val jpeg = captureSyntheticFrame(320, 240, 1)
        viewportView.updateState(newState = "thinking", statusText = "VISION SNAPSHOT...")
        if (engine.isRecording()) {
            viewportView.setPttActive(false)
            engine.stopPttAndCommit(imageBytes = jpeg)
        } else {
            engine.dispatchStandaloneSnapshotTurn(imageBytes = jpeg)
        }
    }

    private fun triggerBurstCapture() {
        engine.playChime("snap")
        val jpeg = captureSyntheticFrame(320, 240, 5)
        viewportView.updateState(newState = "thinking", statusText = "5-FRAME BURST VISION...")
        if (engine.isRecording()) {
            viewportView.setPttActive(false)
            engine.stopPttAndCommit(imageBytes = jpeg)
        } else {
            engine.dispatchStandaloneSnapshotTurn(imageBytes = jpeg)
        }
    }

    private fun captureSyntheticFrame(width: Int, height: Int, frameIndex: Int): ByteArray {
        val bmp = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)
        canvas.drawColor(Color.rgb(18, 22, 32))
        val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.rgb(94, 234, 212)
            textSize = 14f
            typeface = Typeface.MONOSPACE
        }
        canvas.drawText("GOTCHI V2 CAM FRAME #$frameIndex", 16f, height / 2f, paint)
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.JPEG, 85, out)
        return out.toByteArray()
    }

    /**
     * Custom ViewGroup rendering the Gotchi V2 Translucent Atomic Purple Handheld Enclosure
     * around the live [GotchiPhoneViewportView] with interactive front-mounted machined aluminum buttons,
     * top-left protruding OV5640 sapphire camera bump, front/top dual-mic pinholes, left-rail controls,
     * and 5-slot diagonal corner speaker grate.
     */
    inner class AtomicPurpleChassisView(
        context: Context,
        private val viewportChild: GotchiPhoneViewportView
    ) : FrameLayout(context) {

        private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG)
        private val outerRailRect = RectF()
        private val chamferRect = RectF()
        private val purplePlateRect = RectF()
        private val screenCutoutRect = RectF()
        private val chinWindowRect = RectF()
        private val powerSliderRect = RectF()
        private val usbCRect = RectF()

        private var camCx = 0f
        private var camCy = 0f
        private var camBumpCollarR = 0f

        private var mic1Cx = 0f
        private var mic1Cy = 0f
        private var mic2Cx = 0f
        private var mic2Cy = 0f

        private var btnTopCx = 0f
        private var btnTopCy = 0f
        private var btnBotCx = 0f
        private var btnBotCy = 0f
        private var btnRadius = 0f

        var btnTopPressed = false
        var btnBotPressed = false
        private var activeTouchBtn = 0 // 0=none, 1=top/B, 2=bot/A

        init {
            setWillNotDraw(false)
            addView(viewportChild)
        }

        override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
            val w = (right - left).toFloat()
            val h = (bottom - top).toFloat()
            if (w <= 0f || h <= 0f) return

            // Aspect ratio: V2 Canonical 44.0 mm width x 73.0 mm height (1 : 1.659)
            val targetAspect = 44.0f / 73.0f
            var caseW = w * 0.96f
            var caseH = caseW / targetAspect
            if (caseH > h * 0.98f) {
                caseH = h * 0.98f
                caseW = caseH * targetAspect
            }

            val cx = w / 2f
            val cy = h / 2f
            outerRailRect.set(cx - caseW / 2f, cy - caseH / 2f, cx + caseW / 2f, cy + caseH / 2f)

            val railWall = caseW * 0.052f
            chamferRect.set(
                outerRailRect.left + railWall * 0.32f,
                outerRailRect.top + railWall * 0.32f,
                outerRailRect.right - railWall * 0.32f,
                outerRailRect.bottom - railWall * 0.32f
            )
            purplePlateRect.set(
                outerRailRect.left + railWall,
                outerRailRect.top + railWall,
                outerRailRect.right - railWall,
                outerRailRect.bottom - railWall
            )

            // Left-rail Chrome GBC power slider and lower USB-C recess
            val sliderW = railWall * 0.65f
            val sliderH = caseW * 0.16f
            val sliderTop = outerRailRect.top + caseH * 0.18f
            powerSliderRect.set(
                outerRailRect.left - sliderW * 0.45f,
                sliderTop,
                outerRailRect.left + sliderW * 0.55f,
                sliderTop + sliderH
            )

            val usbW = railWall * 0.70f
            val usbH = caseW * 0.13f
            val usbTop = outerRailRect.top + caseH * 0.72f
            usbCRect.set(
                outerRailRect.left - usbW * 0.25f,
                usbTop,
                outerRailRect.left + usbW * 0.75f,
                usbTop + usbH
            )

            // Top-left forehead zone: protruding OV5640 CNC 6061 aluminum camera bump collar
            camBumpCollarR = caseW * 0.095f
            camCx = purplePlateRect.left + purplePlateRect.width() * 0.24f
            camCy = purplePlateRect.top + caseW * 0.125f

            // Top rail MIC2 ambient reference pinhole (orthogonal 90° split)
            mic2Cx = outerRailRect.centerX() + caseW * 0.24f
            mic2Cy = (outerRailRect.top + purplePlateRect.top) / 2f

            // Square screenCutoutRect (GotchiPhoneViewportView) cleanly below top-left camera bump
            val screenMargin = purplePlateRect.width() * 0.065f
            val screenSize = purplePlateRect.width() - 2f * screenMargin
            val screenLeft = purplePlateRect.left + screenMargin
            val screenTop = camCy + camBumpCollarR + caseW * 0.045f
            screenCutoutRect.set(screenLeft, screenTop, screenLeft + screenSize, screenTop + screenSize)

            viewportChild.layout(
                screenCutoutRect.left.toInt(),
                screenCutoutRect.top.toInt(),
                screenCutoutRect.right.toInt(),
                screenCutoutRect.bottom.toInt()
            )

            // Front-firing MIC1 bezel pinhole just below bottom-left screen corner
            mic1Cx = screenCutoutRect.left + caseW * 0.040f
            mic1Cy = screenCutoutRect.bottom + caseW * 0.040f

            // Lower deck: internal matte-black PCB cavity under screen
            val chinTop = screenCutoutRect.bottom + caseW * 0.048f
            val chinBottom = purplePlateRect.bottom - caseW * 0.045f
            val chinMargin = purplePlateRect.width() * 0.060f
            chinWindowRect.set(
                purplePlateRect.left + chinMargin,
                chinTop,
                purplePlateRect.right - chinMargin,
                chinBottom
            )

            // Staggered Game Boy Color Chrome Silver Buttons:
            // B button: lower-left (PTT / 2x LIVE)
            // A button: upper-right (SNAP / VISION)
            // Guaranteed staggered layout where A is higher than B: btnBotCy < btnTopCy
            btnRadius = caseW * 0.076f
            btnTopCx = chinWindowRect.left + chinWindowRect.width() * 0.22f
            btnTopCy = chinWindowRect.top + chinWindowRect.height() * 0.58f

            btnBotCx = chinWindowRect.left + chinWindowRect.width() * 0.48f
            btnBotCy = chinWindowRect.top + chinWindowRect.height() * 0.36f
            if (btnBotCy >= btnTopCy) {
                btnBotCy = btnTopCy - btnRadius * 0.6f
            }
        }

        override fun onDraw(canvas: Canvas) {
            if (outerRailRect.isEmpty) return
            val caseW = outerRailRect.width()
            val cornerR = caseW * 0.105f
            val plateR = caseW * 0.075f

            // 1. Soft studio drop shadow behind CNC aluminum case
            paint.style = Paint.Style.FILL
            paint.shader = null
            paint.color = Color.argb(110, 0, 0, 0)
            canvas.drawRoundRect(
                RectF(outerRailRect.left + 8f, outerRailRect.top + 16f, outerRailRect.right + 8f, outerRailRect.bottom + 16f),
                cornerR, cornerR, paint
            )

            // 2. Left-Rail Chrome GBC Power Slider
            paint.style = Paint.Style.FILL
            paint.shader = LinearGradient(
                powerSliderRect.left, powerSliderRect.top, powerSliderRect.right, powerSliderRect.bottom,
                intArrayOf(
                    Color.rgb(240, 244, 250),
                    Color.rgb(180, 190, 204),
                    Color.rgb(130, 140, 155)
                ),
                null,
                Shader.TileMode.CLAMP
            )
            val sliderR = powerSliderRect.width() * 0.35f
            canvas.drawRoundRect(powerSliderRect, sliderR, sliderR, paint)
            paint.shader = null

            // Tactile serration ridges on power slider
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = 1.2f
            paint.color = Color.rgb(90, 100, 115)
            val ridgeCount = 4
            val ridgeSpacing = powerSliderRect.height() / (ridgeCount + 1)
            for (r in 1..ridgeCount) {
                val ry = powerSliderRect.top + r * ridgeSpacing
                canvas.drawLine(powerSliderRect.left + 2f, ry, powerSliderRect.right - 2f, ry, paint)
            }

            // 3. Lower Left-Rail USB-C Port Recess
            paint.style = Paint.Style.FILL
            paint.color = Color.rgb(20, 22, 30)
            val usbR = usbCRect.width() * 0.35f
            canvas.drawRoundRect(usbCRect, usbR, usbR, paint)
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = 1f
            paint.color = Color.argb(140, 160, 175, 195)
            canvas.drawRoundRect(usbCRect, usbR, usbR, paint)
            paint.style = Paint.Style.FILL

            // 4. CNC 6061 Bead-Blasted Aluminum Perimeter Frame Side-Rail
            paint.shader = LinearGradient(
                outerRailRect.left, outerRailRect.top, outerRailRect.right, outerRailRect.bottom,
                intArrayOf(
                    Color.rgb(214, 222, 232),
                    Color.rgb(164, 174, 188),
                    Color.rgb(126, 136, 150),
                    Color.rgb(178, 186, 198)
                ),
                floatArrayOf(0f, 0.35f, 0.75f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawRoundRect(outerRailRect, cornerR, cornerR, paint)

            // 45° Diamond-Cut Specular Chamfer Highlight on Aluminum Rail
            paint.shader = null
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = caseW * 0.008f
            paint.color = Color.argb(185, 242, 247, 255)
            canvas.drawRoundRect(chamferRect, cornerR * 0.90f, cornerR * 0.90f, paint)

            // Top PDM Microphone MIC2 Pinhole on CNC Aluminum Top Rail
            paint.style = Paint.Style.FILL
            val micR = caseW * 0.013f
            paint.color = Color.rgb(32, 36, 44)
            canvas.drawCircle(mic2Cx, mic2Cy, micR, paint)

            // 5. Translucent Atomic Purple (#CC6B2CF5 / #8A3FFC) SLA Resin Front Plate
            paint.style = Paint.Style.FILL
            paint.shader = LinearGradient(
                purplePlateRect.left, purplePlateRect.top, purplePlateRect.right, purplePlateRect.bottom,
                intArrayOf(
                    Color.parseColor("#CC6B2CF5"),
                    Color.parseColor("#8A3FFC"),
                    Color.rgb(84, 52, 168),
                    Color.rgb(74, 44, 154)
                ),
                floatArrayOf(0f, 0.42f, 0.82f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawRoundRect(purplePlateRect, plateR, plateR, paint)

            // Diagonal Frosted Translucent Resin Specular Sheen across Atomic Purple Plate
            paint.shader = LinearGradient(
                purplePlateRect.left, purplePlateRect.top, purplePlateRect.right, purplePlateRect.bottom,
                intArrayOf(
                    Color.argb(48, 224, 204, 255),
                    Color.argb(14, 180, 150, 255),
                    Color.argb(0, 120, 90, 220),
                    Color.argb(32, 195, 170, 255)
                ),
                floatArrayOf(0f, 0.35f, 0.70f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawRoundRect(purplePlateRect, plateR, plateR, paint)
            paint.shader = null

            // Inner rabbet border around Atomic Purple plate
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = caseW * 0.007f
            paint.color = Color.argb(175, 170, 142, 248)
            canvas.drawRoundRect(purplePlateRect, plateR, plateR, paint)

            // 6. Top-Left Forehead: Protruding CNC-Turned 6061 Aluminum Camera Bump Collar + Sapphire Coverglass (OV5640 5MP)
            // Soft drop shadow for protruding collar
            paint.style = Paint.Style.FILL
            paint.color = Color.argb(110, 10, 6, 22)
            canvas.drawCircle(camCx + 2f, camCy + 3.5f, camBumpCollarR * 1.04f, paint)

            // CNC-Turned 6061 Aluminum Collar Lens Ring
            paint.shader = LinearGradient(
                camCx - camBumpCollarR, camCy - camBumpCollarR, camCx + camBumpCollarR, camCy + camBumpCollarR,
                intArrayOf(
                    Color.rgb(228, 234, 244),
                    Color.rgb(172, 182, 198),
                    Color.rgb(122, 132, 148),
                    Color.rgb(196, 204, 216)
                ),
                null,
                Shader.TileMode.CLAMP
            )
            canvas.drawCircle(camCx, camCy, camBumpCollarR, paint)
            paint.shader = null

            // Specular diamond-turned bevel rim on camera collar
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = caseW * 0.007f
            paint.color = Color.argb(220, 245, 250, 255)
            canvas.drawCircle(camCx, camCy, camBumpCollarR * 0.92f, paint)

            // Dark anodized internal stepped bezel seat
            paint.style = Paint.Style.FILL
            paint.color = Color.rgb(20, 22, 34)
            canvas.drawCircle(camCx, camCy, camBumpCollarR * 0.78f, paint)

            // OV5640 Cyan-Teal AR-Coated Optical Sapphire Glass Lens
            val lensR = camBumpCollarR * 0.62f
            paint.shader = RadialGradient(
                camCx - lensR * 0.25f,
                camCy - lensR * 0.25f,
                lensR * 1.15f,
                intArrayOf(
                    Color.rgb(42, 184, 216),
                    Color.rgb(18, 118, 152),
                    Color.rgb(10, 56, 78)
                ),
                floatArrayOf(0f, 0.58f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawCircle(camCx, camCy, lensR, paint)
            paint.shader = null

            // Inner aperture pupil & specular glint
            paint.color = Color.argb(150, 8, 22, 34)
            canvas.drawCircle(camCx, camCy, lensR * 0.40f, paint)
            paint.color = Color.argb(210, 195, 248, 255)
            canvas.drawCircle(camCx - lensR * 0.30f, camCy - lensR * 0.30f, lensR * 0.16f, paint)

            // 7. Central Recessed OLED Viewport Bezel Cutout Frame Shadow
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = caseW * 0.012f
            paint.color = Color.rgb(20, 14, 38)
            canvas.drawRect(
                screenCutoutRect.left - 2f,
                screenCutoutRect.top - 2f,
                screenCutoutRect.right + 2f,
                screenCutoutRect.bottom + 2f,
                paint
            )

            // 8. Front-Firing MIC1 Primary Voice Pinhole (just below bottom-left screen corner)
            val mic1R = caseW * 0.012f
            paint.style = Paint.Style.FILL
            paint.color = Color.rgb(24, 20, 36)
            canvas.drawCircle(mic1Cx, mic1Cy, mic1R, paint)
            paint.color = Color.argb(120, 200, 180, 240)
            canvas.drawCircle(mic1Cx, mic1Cy + 0.8f, mic1R * 0.45f, paint)

            // 9. Internal Matte-Black V2.5 PCB Window in Chin
            paint.style = Paint.Style.FILL
            paint.color = Color.argb(140, 18, 12, 38)
            canvas.drawRoundRect(chinWindowRect, caseW * 0.03f, caseW * 0.03f, paint)

            // Left & Right Gold ENIG Sensor Bus / Trace Rails visible through resin
            val busW = caseW * 0.026f
            paint.color = Color.argb(165, 218, 165, 32)
            canvas.drawRoundRect(
                RectF(chinWindowRect.left + busW * 0.6f, chinWindowRect.top + busW, chinWindowRect.left + busW * 1.7f, chinWindowRect.bottom - busW * 1.4f),
                4f, 4f, paint
            )
            canvas.drawRoundRect(
                RectF(chinWindowRect.right - busW * 1.7f, chinWindowRect.top + busW * 2.5f, chinWindowRect.right - busW * 0.6f, chinWindowRect.bottom - busW),
                4f, 4f, paint
            )

            // 10. Staggered Game Boy Color Chrome Silver Buttons:
            // Lower-Left B (PTT / 2x LIVE) & Upper-Right A (SNAP / VISION)
            drawMachinedAluminumButton(
                canvas = canvas,
                cx = btnTopCx,
                cy = btnTopCy,
                radius = btnRadius,
                pressed = btnTopPressed,
                topLabel = "PTT",
                subLabel = "2x LIVE"
            )
            drawMachinedAluminumButton(
                canvas = canvas,
                cx = btnBotCx,
                cy = btnBotCy,
                radius = btnRadius,
                pressed = btnBotPressed,
                topLabel = "SNAP",
                subLabel = "VISION"
            )

            // 11. 5-Slot OG Game Boy Diagonal Speaker Grate in Bottom-Right Corner
            drawDiagonalSpeakerGrate(
                canvas = canvas,
                originX = chinWindowRect.right - caseW * 0.13f,
                originY = chinWindowRect.bottom - caseW * 0.09f,
                caseW = caseW
            )
            // No under-screen LED glow bar (clean locked V2 hardware specification)
        }

        private fun drawDiagonalSpeakerGrate(
            canvas: Canvas,
            originX: Float,
            originY: Float,
            caseW: Float
        ) {
            val speakerSlotCount = 5
            val slotW = caseW * 0.016f
            val slotSpacing = caseW * 0.032f
            val slotAngle = -28f // Classic Game Boy diagonal speaker slot angle

            paint.style = Paint.Style.FILL
            paint.shader = null

            for (i in 0 until speakerSlotCount) {
                val slotLen = caseW * (0.085f + i * 0.016f)
                val slotX = originX + (i - (speakerSlotCount - 1) / 2f) * slotSpacing
                val slotY = originY - (i - (speakerSlotCount - 1) / 2f) * (slotSpacing * 0.35f)

                canvas.save()
                canvas.rotate(slotAngle, slotX, slotY)

                // Dark acoustic opening
                paint.color = Color.rgb(18, 14, 28)
                val slotRect = RectF(slotX - slotW / 2f, slotY - slotLen / 2f, slotX + slotW / 2f, slotY + slotLen / 2f)
                canvas.drawRoundRect(slotRect, slotW / 2f, slotW / 2f, paint)

                // Chamfer rim highlight
                paint.style = Paint.Style.STROKE
                paint.strokeWidth = 1f
                paint.color = Color.argb(85, 170, 145, 230)
                canvas.drawRoundRect(slotRect, slotW / 2f, slotW / 2f, paint)
                paint.style = Paint.Style.FILL

                canvas.restore()
            }
        }

        private fun drawMachinedAluminumButton(
            canvas: Canvas,
            cx: Float,
            cy: Float,
            radius: Float,
            pressed: Boolean,
            topLabel: String,
            subLabel: String
        ) {
            val offset = if (pressed) 2.5f else 0f

            // Machined bore collar in resin
            paint.style = Paint.Style.FILL
            paint.shader = null
            paint.color = Color.rgb(26, 16, 48)
            canvas.drawCircle(cx, cy, radius * 1.16f, paint)

            // Drop shadow
            if (!pressed) {
                paint.color = Color.argb(120, 8, 4, 20)
                canvas.drawCircle(cx + 2f, cy + 4f, radius * 1.04f, paint)
            }

            // Bead-blasted 6061 Aluminum / Chrome Silver Cap
            val capR = radius * (if (pressed) 0.96f else 1.0f)
            paint.shader = LinearGradient(
                cx - capR, cy - capR + offset, cx + capR, cy + capR + offset,
                if (pressed) {
                    intArrayOf(Color.rgb(148, 158, 174), Color.rgb(184, 194, 208))
                } else {
                    intArrayOf(Color.rgb(236, 242, 250), Color.rgb(182, 192, 206), Color.rgb(134, 144, 160))
                },
                null,
                Shader.TileMode.CLAMP
            )
            canvas.drawCircle(cx, cy + offset, capR, paint)
            paint.shader = null

            // Specular diamond-turned rim on button cap
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = radius * 0.08f
            paint.color = Color.argb(210, 248, 250, 255)
            canvas.drawCircle(cx, cy + offset, capR * 0.92f, paint)

            // Laser-etched button label on cap + sub-caption below bore
            textPaint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
            textPaint.textAlign = Paint.Align.CENTER
            textPaint.textSize = radius * 0.42f
            textPaint.color = Color.rgb(28, 32, 44)
            canvas.drawText(topLabel, cx, cy + offset + radius * 0.15f, textPaint)

            textPaint.textSize = radius * 0.30f
            textPaint.color = Color.rgb(214, 198, 255)
            canvas.drawText(subLabel, cx, cy + radius * 1.48f, textPaint)
        }

        override fun onTouchEvent(event: MotionEvent): Boolean {
            val x = event.x
            val y = event.y
            val hitRadius = btnRadius * 1.45f

            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (hypot((x - btnTopCx).toDouble(), (y - btnTopCy).toDouble()) <= hitRadius) {
                        activeTouchBtn = 1
                        btnTopPressed = true
                        invalidate()
                        handleBtnTopTouch(MotionEvent.ACTION_DOWN)
                        return true
                    }
                    if (hypot((x - btnBotCx).toDouble(), (y - btnBotCy).toDouble()) <= hitRadius) {
                        activeTouchBtn = 2
                        btnBotPressed = true
                        invalidate()
                        handleBtnBotTouch(MotionEvent.ACTION_DOWN)
                        return true
                    }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    if (activeTouchBtn == 1) {
                        btnTopPressed = false
                        activeTouchBtn = 0
                        invalidate()
                        handleBtnTopTouch(MotionEvent.ACTION_UP)
                        return true
                    }
                    if (activeTouchBtn == 2) {
                        btnBotPressed = false
                        activeTouchBtn = 0
                        invalidate()
                        handleBtnBotTouch(MotionEvent.ACTION_UP)
                        return true
                    }
                }
            }
            return super.onTouchEvent(event)
        }
    }
}
