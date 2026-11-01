package ventures.brock.gotchi.watch

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.SystemClock
import android.view.KeyEvent
import kotlin.math.sqrt

/**
 * Pixel Watch Hybrid Gesture & Hardware Key Trigger Detector.
 *
 * Supports three trigger modalities:
 * 1. Native Wear OS One-Handed Gestures:
 *    - Double Pinch (`ACTION_PRIMARY` / `GESTURE_DOUBLE_PINCH`): Triggers PTT / Barge-in
 *    - Wrist Turn (`ACTION_DISMISS` / `GESTURE_WRIST_TURN`): Cancels active PTT / dismisses
 * 2. Wear OS stem & physical hardware keys:
 *    - `KeyEvent.KEYCODE_STEM_PRIMARY` (Pixel Watch crown / side button)
 *    - `KeyEvent.KEYCODE_STEM_1`
 *    - `KeyEvent.KEYCODE_HEADSETHOOK`
 *    - `KeyEvent.KEYCODE_SPACE`
 *    - `KeyEvent.KEYCODE_ENTER`
 *    - `KeyEvent.KEYCODE_DPAD_CENTER`
 * 3. On-wrist IMU micro-impulse pinch detector:
 *    - Uses `Sensor.TYPE_LINEAR_ACCELERATION` (3.4 m/s^2 .. 11.5 m/s^2) for transient pinch impulse
 *    - Paired with `Sensor.TYPE_GYROSCOPE` guard (<= 1.2 rad/s) so arm swings & wrist rotations are rejected
 *    - Sampled at `SensorManager.SENSOR_DELAY_UI` to preserve battery
 *    - Deliberate double-pinch impulse required (120ms..550ms window)
 *    - 450 ms cooldown debounce
 *    - Suppression flag during transcribing/thinking/synthesizing
 *
 * Dispatches actions via:
 * - `onPinchTriggered(source: String)`:
 *   - When IDLE: startPttCapture(autoVadStop = true)
 *   - When RECORDING: stopPttAndCommit()
 *   - When SPEAKING: triggerBargeIn()
 * - `onDismissTriggered(source: String)`:
 *   - Aborts active PTT capture or skips pending answer without uploading.
 */
class PinchGestureDetector(
    private val context: Context? = null,
    var onPinchTriggered: ((source: String) -> Unit)? = null
) : SensorEventListener {

    fun interface PinchListener {
        fun onPinchTriggered(source: String)
    }

    fun interface DismissListener {
        fun onDismissTriggered(source: String)
    }

    var pinchListener: PinchListener? = null
    var dismissListener: DismissListener? = null

    var onDismissTriggered: ((source: String) -> Unit)? = null

    companion object {
        const val ACTION_PRIMARY = 1
        const val ACTION_DISMISS = 2
        const val GESTURE_DOUBLE_PINCH = 1
        const val GESTURE_WRIST_TURN = 2

        const val COOLDOWN_MS = 450L
        const val DOUBLE_PINCH_MIN_GAP_MS = 120L
        const val DOUBLE_PINCH_WINDOW_MS = 550L
        const val DEFAULT_LINEAR_ACCEL_THRESHOLD = 3.4f
        const val DEFAULT_MIN_LINEAR_ACCEL_THRESHOLD = 3.4f
        const val DEFAULT_MAX_LINEAR_ACCEL_THRESHOLD = 11.5f
        const val DEFAULT_MAX_GYRO_THRESHOLD = 1.2f
        const val IMU_MAX_REPORT_LATENCY_US = 100_000

        val SUPPORTED_KEY_CODES = setOf(
            KeyEvent.KEYCODE_STEM_PRIMARY,
            KeyEvent.KEYCODE_STEM_1,
            KeyEvent.KEYCODE_HEADSETHOOK,
            KeyEvent.KEYCODE_SPACE,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_DPAD_CENTER
        )
    }

    data class GestureEvent(val eventTimeMillis: Long, val action: Int, val gesture: Int)

    enum class PinchAction {
        START_PTT,
        STOP_PTT_AND_COMMIT,
        TRIGGER_BARGE_IN,
        NONE
    }

    var minLinearAccelThreshold: Float = DEFAULT_MIN_LINEAR_ACCEL_THRESHOLD
    var maxLinearAccelThreshold: Float = DEFAULT_MAX_LINEAR_ACCEL_THRESHOLD
    var linearAccelThreshold: Float
        get() = minLinearAccelThreshold
        set(value) { minLinearAccelThreshold = value }
    var maxGyroThreshold: Float = DEFAULT_MAX_GYRO_THRESHOLD
    var cooldownMs: Long = COOLDOWN_MS
    var requireDoubleImpulse: Boolean = true

    /**
     * Suppression flag: when true (e.g. while Gotchi is processing/synthesizing),
     * IMU sensors are unregistered to save watch battery.
     */
    var isSuppressed: Boolean = false
        set(value) {
            field = value
            if (value) {
                sensorManager?.unregisterListener(this)
                firstImpulseMs = 0L
            } else if (isListening) {
                linearAccelSensor?.let {
                    sensorManager?.registerListener(this, it, SensorManager.SENSOR_DELAY_UI, IMU_MAX_REPORT_LATENCY_US)
                }
                gyroSensor?.let {
                    sensorManager?.registerListener(this, it, SensorManager.SENSOR_DELAY_UI, IMU_MAX_REPORT_LATENCY_US)
                }
            }
        }

    private val sensorManager = context?.getSystemService(Context.SENSOR_SERVICE) as? SensorManager
    private val linearAccelSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION)
    private val gyroSensor = sensorManager?.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

    private var lastTriggerMs: Long = 0L
    private var lastDismissTriggerMs: Long = 0L
    private var lastGyroMagnitude: Float = 0f
    private var firstImpulseMs: Long = 0L
    private var isListening: Boolean = false

    /**
     * Maps the current Gotchi UI state to the appropriate PTT lifecycle action:
     * - IDLE / READY / OFFLINE / ERROR -> START_PTT (startPttCapture(autoVadStop = true))
     * - LISTENING / RECORDING -> STOP_PTT_AND_COMMIT (stopPttAndCommit())
     * - SPEAKING / SYNTHESIZING -> TRIGGER_BARGE_IN (triggerBargeIn())
     */
    fun resolveActionForState(currentState: String): PinchAction {
        return when (currentState.lowercase().trim()) {
            "idle", "ready", "offline", "error" -> PinchAction.START_PTT
            "listening", "recording" -> PinchAction.STOP_PTT_AND_COMMIT
            "speaking", "synthesizing" -> PinchAction.TRIGGER_BARGE_IN
            else -> PinchAction.NONE
        }
    }

    /**
     * Handle native Wear OS One-Handed Gesture events.
     */
    fun onGestureEvent(event: GestureEvent): Boolean {
        return when {
            event.action == ACTION_PRIMARY || event.gesture == GESTURE_DOUBLE_PINCH -> onDoublePinchGesture()
            event.action == ACTION_DISMISS || event.gesture == GESTURE_WRIST_TURN -> onWristTurnGesture()
            else -> false
        }
    }

    /**
     * Handle KeyEvent dispatched from Activity or View.
     */
    fun onKeyEvent(event: KeyEvent): Boolean {
        if (event.action == KeyEvent.ACTION_DOWN && event.keyCode in SUPPORTED_KEY_CODES) {
            val source = when (event.keyCode) {
                KeyEvent.KEYCODE_STEM_PRIMARY -> "stem_primary"
                KeyEvent.KEYCODE_STEM_1 -> "stem_1"
                KeyEvent.KEYCODE_HEADSETHOOK -> "headset_hook"
                KeyEvent.KEYCODE_SPACE -> "key_space"
                KeyEvent.KEYCODE_ENTER -> "key_enter"
                KeyEvent.KEYCODE_DPAD_CENTER -> "dpad_center"
                else -> "key_${event.keyCode}"
            }
            return tryTriggerPinch(source)
        }
        return false
    }

    fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode in SUPPORTED_KEY_CODES) {
            return tryTriggerPinch("key_$keyCode")
        }
        return false
    }

    fun onKeyUp(keyCode: Int, event: KeyEvent?): Boolean {
        return keyCode in SUPPORTED_KEY_CODES
    }

    /**
     * Trigger from Wear OS One-Handed double-pinch gesture or accessibility switch event.
     * Dispatches ACTION_PRIMARY / "wear_os_double_pinch".
     */
    fun onDoublePinchGesture(): Boolean {
        return tryTriggerPinch("wear_os_double_pinch")
    }

    /**
     * Trigger from Wear OS One-Handed wrist-turn gesture.
     * Dispatches ACTION_DISMISS / "wear_os_wrist_turn" to onDismissTriggered.
     */
    fun onWristTurnGesture(): Boolean {
        return tryTriggerDismiss("wear_os_wrist_turn")
    }

    /**
     * Trigger from system accessibility action.
     */
    fun onAccessibilityAction(actionId: Int = 0): Boolean {
        return tryTriggerPinch("accessibility_action_$actionId")
    }

    private fun tryTriggerPinch(source: String): Boolean {
        val now = SystemClock.elapsedRealtime()
        if (now - lastTriggerMs >= cooldownMs) {
            lastTriggerMs = now
            dispatchPinch(source)
            return true
        }
        // Return true to consume the key/gesture even if within cooldown window
        return true
    }

    private fun dispatchPinch(source: String) {
        onPinchTriggered?.invoke(source)
        pinchListener?.onPinchTriggered(source)
    }

    private fun tryTriggerDismiss(source: String): Boolean {
        val now = SystemClock.elapsedRealtime()
        if (now - lastDismissTriggerMs >= cooldownMs) {
            lastDismissTriggerMs = now
            dispatchDismiss(source)
            return true
        }
        return true
    }

    private fun dispatchDismiss(source: String) {
        onDismissTriggered?.invoke(source)
        dismissListener?.onDismissTriggered(source)
    }

    /**
     * Register IMU accelerometer and gyroscope listeners with SensorManager using SENSOR_DELAY_UI.
     */
    fun start(): Boolean {
        if (isListening) return false
        isListening = true
        if (sensorManager == null) return true
        var registered = false
        if (!isSuppressed) {
            linearAccelSensor?.let {
                val ok = sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_UI, IMU_MAX_REPORT_LATENCY_US)
                registered = registered || ok
            }
            gyroSensor?.let {
                val ok = sensorManager.registerListener(this, it, SensorManager.SENSOR_DELAY_UI, IMU_MAX_REPORT_LATENCY_US)
                registered = registered || ok
            }
        } else {
            registered = true
        }
        return registered
    }

    /**
     * Unregister IMU sensors to save watch battery.
     */
    fun stop() {
        if (!isListening) return
        sensorManager?.unregisterListener(this)
        isListening = false
        firstImpulseMs = 0L
    }

    fun isRunning(): Boolean = isListening

    override fun onSensorChanged(event: SensorEvent?) {
        if (event == null || isSuppressed) return

        when (event.sensor.type) {
            Sensor.TYPE_GYROSCOPE -> {
                val gx = event.values[0]
                val gy = event.values[1]
                val gz = event.values[2]
                lastGyroMagnitude = sqrt(gx * gx + gy * gy + gz * gz)
            }
            Sensor.TYPE_LINEAR_ACCELERATION -> {
                val ax = event.values[0]
                val ay = event.values[1]
                val az = event.values[2]
                val accelMag = sqrt(ax * ax + ay * ay + az * az)

                // High-pass micro-impulse transient spike check (3.4f..11.5f)
                if (accelMag in minLinearAccelThreshold..maxLinearAccelThreshold) {
                    // Arm-swing rejection guard: reject if wrist angular velocity is high (<= 1.2f)
                    if (lastGyroMagnitude <= maxGyroThreshold) {
                        if (requireDoubleImpulse) {
                            val now = SystemClock.elapsedRealtime()
                            val elapsedSinceFirst = now - firstImpulseMs
                            if (firstImpulseMs == 0L || elapsedSinceFirst > DOUBLE_PINCH_WINDOW_MS) {
                                firstImpulseMs = now
                            } else if (elapsedSinceFirst >= DOUBLE_PINCH_MIN_GAP_MS) {
                                firstImpulseMs = 0L
                                tryTriggerPinch("imu_pinch")
                            }
                        } else {
                            tryTriggerPinch("imu_pinch")
                        }
                    }
                }
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
}

typealias GestureEvent = PinchGestureDetector.GestureEvent
