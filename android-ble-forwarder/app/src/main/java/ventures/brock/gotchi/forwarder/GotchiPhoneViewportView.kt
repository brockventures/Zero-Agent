package ventures.brock.gotchi.forwarder

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.os.SystemClock
import android.util.AttributeSet
import android.view.View

/**
 * Full-Color HD Zero Avatar Viewport for Gotchi Phone App (`ventures.brock.gotchi.forwarder`).
 *
 * Renders the 256x256 full-color HD Zero avatar sprites (`zero_hd_<state>_<0..2>[_blink].png`)
 * inside a flush borderless display viewport with:
 * - 3-frame expression stepping (`(animFrame / 12) % 3`) across all states
 * - Periodic visor eye blinks (every ~3s, frames 0..6)
 * - Dynamic head posture shifts (`dx, dy` per state)
 * - 60 FPS animated overlays: pulsing soundwaves (listening), thought dots (thinking), 5-bar equalizer (speaking)
 * - Crisp monospace status header (top) and 2-line live caption overlay (bottom)
 * - Diagonal optical cover-glass sheen reflection
 * - Monochrome fallback for 1-bit e-paper substrate mode and Amos skin
 */
class GotchiPhoneViewportView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    companion object {
        const val EPD_WIDTH = 200
        const val EPD_HEIGHT = 200
        const val PORTRAIT_SCALE_RATIO = 0.68f
        const val ANIM_FRAME_INTERVAL_MS = 16L // 60 FPS

        val EINK_PAPER_COLOR = Color.rgb(234, 230, 220)
        val EINK_INK_COLOR = Color.rgb(20, 20, 24)

        val OLED_BG_COLOR = Color.rgb(12, 10, 22)
        val OLED_FG_COLOR = Color.rgb(236, 234, 248)

        val STATUS_ACCENT_COLOR = Color.rgb(94, 234, 212)
        val STATUS_AMBER_COLOR = Color.rgb(255, 170, 60)
        val STATUS_CORAL_COLOR = Color.rgb(255, 90, 45)

        val VALID_STATES = setOf(
            "idle", "listening", "transcribing", "thinking",
            "synthesizing", "speaking", "error", "busy", "offline"
        )
    }

    data class UIState(
        var state: String = "idle",
        var transcript: String = "",
        var reply: String = "",
        var statusText: String = "READY (HOLD PTT)",
        var thinkingSeconds: Int = 0,
        var audioBytesReceived: Int = 0,
        var lastSampleRate: Int = 24000,
        var batteryPct: Int = 96,
        var skin: String = "zero", // "zero" | "amos" | "gotchi"
        var pttActive: Boolean = false,
        var wakeListenerActive: Boolean = true,
        var oledDarkMode: Boolean = true,
        var animFrame: Int = 0,
        var blink: Boolean = false,
        var epaperMode: Boolean = false
    )

    val uiState = UIState()
    private val zeroHdCache = mutableMapOf<String, Bitmap>()
    private val amosCache = mutableMapOf<String, Bitmap>()
    private val destRect = RectF()
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glassPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val overlayPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    fun updateState(
        newState: String? = null,
        transcript: String? = null,
        reply: String? = null,
        statusText: String? = null,
        audioBytesDelta: Int = 0,
        sampleRate: Int? = null,
        forceFullRefresh: Boolean = false,
        pttActive: Boolean? = null,
        wakeListenerActive: Boolean? = null,
        oledDarkMode: Boolean? = null
    ) {
        newState?.let { uiState.state = if (it in VALID_STATES) it else "idle" }
        transcript?.let { uiState.transcript = it }
        reply?.let { uiState.reply = it }
        statusText?.let { uiState.statusText = it }
        if (audioBytesDelta > 0) uiState.audioBytesReceived += audioBytesDelta
        sampleRate?.let { uiState.lastSampleRate = it }
        pttActive?.let { uiState.pttActive = it }
        wakeListenerActive?.let { uiState.wakeListenerActive = it }
        oledDarkMode?.let {
            uiState.oledDarkMode = it
            uiState.epaperMode = !it
        }
        invalidate()
    }

    fun setState(newState: String) {
        updateState(newState = newState)
    }

    fun setPttActive(active: Boolean) {
        if (uiState.pttActive != active) {
            uiState.pttActive = active
            invalidate()
        }
    }

    fun setWakeListenerActive(active: Boolean) {
        if (uiState.wakeListenerActive != active) {
            uiState.wakeListenerActive = active
            invalidate()
        }
    }

    fun setSkin(skinName: String) {
        if (skinName in setOf("zero", "amos", "gotchi") && uiState.skin != skinName) {
            uiState.skin = skinName
            invalidate()
        }
    }

    fun setOledDarkMode(enabled: Boolean) {
        if (uiState.oledDarkMode != enabled) {
            uiState.oledDarkMode = enabled
            uiState.epaperMode = !enabled
            invalidate()
        }
    }

    fun setEpaperMode(enabled: Boolean) {
        if (uiState.epaperMode != enabled) {
            uiState.epaperMode = enabled
            uiState.oledDarkMode = !enabled
            invalidate()
        }
    }

    private fun getZeroHdBitmap(state: String, frameIdx: Int, blink: Boolean): Bitmap? {
        val s = if (state in VALID_STATES) state else "idle"
        val f = (frameIdx % 3 + 3) % 3
        val cacheKey = "${s}_${f}_$blink"
        zeroHdCache[cacheKey]?.let { return it }

        val candidates = if (blink) {
            listOf(
                "zero_skin/zero_hd_${s}_${f}_blink.png",
                "zero_skin/zero_hd_${s}_blink.png",
                "zero_skin/zero_hd_${s}_${f}.png",
                "zero_skin/zero_hd_${s}.png",
                "zero_skin/zero_hd_idle_0_blink.png",
                "zero_skin/zero_hd_idle_blink.png",
                "zero_skin/zero_hd_idle_0.png",
                "zero_skin/zero_hd_idle.png"
            )
        } else {
            listOf(
                "zero_skin/zero_hd_${s}_${f}.png",
                "zero_skin/zero_hd_${s}.png",
                "zero_skin/zero_hd_idle_0.png",
                "zero_skin/zero_hd_idle.png"
            )
        }

        for (path in candidates) {
            try {
                context.assets.open(path).use { stream ->
                    BitmapFactory.decodeStream(stream)?.let { bmp ->
                        zeroHdCache[cacheKey] = bmp
                        return bmp
                    }
                }
            } catch (_: Exception) {
                // Try next candidate
            }
        }
        return null
    }

    private fun getAmosBitmap(state: String): Bitmap? {
        val s = if (state in VALID_STATES) state else "idle"
        amosCache[s]?.let { return it }
        val path = "amos_skin/amos_${s}_200x200_1bit.png"
        return try {
            context.assets.open(path).use { stream ->
                BitmapFactory.decodeStream(stream)?.also { amosCache[s] = it }
            }
        } catch (_: Exception) {
            null
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        val cx = w / 2f
        val cy = h / 2f
        val viewportSize = minOf(w, h)
        val innerRadius = viewportSize / 2f

        val st = if (uiState.state in VALID_STATES) uiState.state else "idle"
        val bgColor = if (uiState.epaperMode) EINK_PAPER_COLOR else OLED_BG_COLOR
        val fgColor = if (uiState.epaperMode) EINK_INK_COLOR else OLED_FG_COLOR

        // 1. Viewport background substrate
        bgPaint.style = Paint.Style.FILL
        bgPaint.color = bgColor
        canvas.drawRect(0f, 0f, w, h, bgPaint)

        // Advance 60 FPS animation frame counter
        uiState.animFrame = (uiState.animFrame + 1) % 3600
        val blinkCycle = uiState.animFrame % 180
        uiState.blink = blinkCycle in 0..6 && st in setOf("idle", "listening", "speaking", "transcribing")

        // Head posture shifts (dx, dy per state)
        val (headDx, headDy) = when (st) {
            "idle" -> 0f to 0f
            "listening" -> (innerRadius * 0.02f) to (-innerRadius * 0.03f)
            "transcribing" -> 0f to (innerRadius * 0.02f)
            "thinking" -> (innerRadius * 0.04f) to (-innerRadius * 0.03f)
            "synthesizing" -> (-innerRadius * 0.03f) to (-innerRadius * 0.03f)
            "speaking" -> 0f to (kotlin.math.sin(uiState.animFrame * 0.25f) * innerRadius * 0.025f)
            "error" -> (if ((uiState.animFrame / 4) % 2 == 0) -innerRadius * 0.02f else innerRadius * 0.02f) to (innerRadius * 0.02f)
            "busy" -> 0f to (innerRadius * 0.015f)
            "offline" -> 0f to (innerRadius * 0.05f)
            else -> 0f to 0f
        }

        // Portrait dimensions (68% of viewport)
        val portraitDiameter = viewportSize * PORTRAIT_SCALE_RATIO
        val hdCx = cx + headDx
        val hdCy = cy - viewportSize * 0.04f + headDy
        destRect.set(
            hdCx - portraitDiameter / 2f,
            hdCy - portraitDiameter / 2f,
            hdCx + portraitDiameter / 2f,
            hdCy + portraitDiameter / 2f
        )

        // 2. Render Character Avatar
        if (uiState.skin == "zero") {
            val frameIndex = (uiState.animFrame / 12) % 3
            val hdPortrait = getZeroHdBitmap(st, frameIndex, uiState.blink)
            if (hdPortrait != null) {
                paint.isFilterBitmap = true
                canvas.drawBitmap(hdPortrait, null, destRect, paint)
            }
        } else if (uiState.skin == "amos") {
            val amosBmp = getAmosBitmap(st)
            if (amosBmp != null) {
                paint.isFilterBitmap = false
                canvas.drawBitmap(amosBmp, null, destRect, paint)
            }
        }

        // 3. 60 FPS Animated Overlays
        when (st) {
            "listening" -> {
                // Pulsing soundwave arcs for listening mode
                val wavePhase = (uiState.animFrame % 30) / 30f
                val waveRadiusBase = portraitDiameter * 0.44f
                overlayPaint.style = Paint.Style.STROKE
                overlayPaint.strokeCap = Paint.Cap.ROUND
                overlayPaint.strokeWidth = maxOf(2.5f, innerRadius * 0.018f)
                val waveColor = if (uiState.epaperMode) fgColor else STATUS_ACCENT_COLOR
                for (i in 0..2) {
                    val phaseOffset = (wavePhase + i / 3f) % 1f
                    val currentRadius = waveRadiusBase + phaseOffset * innerRadius * 0.24f
                    val alpha = ((1f - phaseOffset) * 220).toInt().coerceIn(0, 255)
                    overlayPaint.color = Color.argb(alpha, Color.red(waveColor), Color.green(waveColor), Color.blue(waveColor))
                    val waveRect = RectF(
                        hdCx - currentRadius,
                        hdCy - currentRadius,
                        hdCx + currentRadius,
                        hdCy + currentRadius
                    )
                    canvas.drawArc(waveRect, 150f, 60f, false, overlayPaint)
                    canvas.drawArc(waveRect, 330f, 60f, false, overlayPaint)
                }
            }
            "thinking", "synthesizing" -> {
                // Cycling thought cloud dots
                val dotCount = 3
                val activeDot = (uiState.animFrame / 12) % dotCount
                overlayPaint.style = Paint.Style.FILL
                val baseDotX = hdCx + portraitDiameter * 0.36f
                val baseDotY = hdCy - portraitDiameter * 0.30f
                val dotOffsets = arrayOf(
                    Pair(0f, 0f),
                    Pair(innerRadius * 0.08f, -innerRadius * 0.06f),
                    Pair(innerRadius * 0.16f, -innerRadius * 0.13f)
                )
                val dotSizes = floatArrayOf(innerRadius * 0.024f, innerRadius * 0.036f, innerRadius * 0.052f)
                val dotColor = if (uiState.epaperMode) fgColor else STATUS_AMBER_COLOR
                for (i in 0 until dotCount) {
                    val (ox, oy) = dotOffsets[i]
                    val dRadius = dotSizes[i]
                    overlayPaint.color = if (i == activeDot) {
                        dotColor
                    } else {
                        Color.argb(80, Color.red(dotColor), Color.green(dotColor), Color.blue(dotColor))
                    }
                    canvas.drawCircle(baseDotX + ox, baseDotY + oy, dRadius, overlayPaint)
                }
            }
            "speaking" -> {
                // 5-bar equalizer below portrait
                overlayPaint.style = Paint.Style.FILL
                val eqColor = if (uiState.epaperMode) fgColor else STATUS_CORAL_COLOR
                overlayPaint.color = eqColor
                val numBars = 5
                val barWidth = maxOf(3.5f, innerRadius * 0.028f)
                val barGap = maxOf(2.5f, innerRadius * 0.018f)
                val totalWidth = numBars * barWidth + (numBars - 1) * barGap
                val startX = cx - totalWidth / 2f
                val eqBaseY = destRect.bottom + innerRadius * 0.06f
                for (i in 0 until numBars) {
                    val bx = startX + i * (barWidth + barGap)
                    val wave = kotlin.math.abs(kotlin.math.sin(uiState.animFrame * 0.35f + i * 1.2f))
                    val barHeight = innerRadius * (0.04f + 0.12f * wave)
                    canvas.drawRoundRect(
                        RectF(bx, eqBaseY - barHeight / 2f, bx + barWidth, eqBaseY + barHeight / 2f),
                        barWidth / 2f, barWidth / 2f, overlayPaint
                    )
                }
            }
        }

        // 4. Status Header (Top)
        val prefix = when (uiState.skin) { "zero" -> "ZERO"; "amos" -> "AMOS"; else -> "GOTCHI" }
        val headerText = if (st == "idle") prefix else "$prefix // ${st.uppercase()}"

        textPaint.style = Paint.Style.FILL
        textPaint.color = fgColor
        textPaint.textAlign = Paint.Align.CENTER
        textPaint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
        textPaint.textSize = maxOf(12f, viewportSize * 0.052f)
        canvas.drawText(headerText, cx, maxOf(24f, viewportSize * 0.09f), textPaint)

        // 5. Live Caption Overlay (Bottom)
        val primaryText = when {
            uiState.reply.isNotBlank() -> uiState.reply
            uiState.transcript.isNotBlank() -> uiState.transcript
            else -> uiState.statusText
        }
        val line1 = primaryText.take(28)
        val line2 = if (primaryText.length > 28) {
            primaryText.substring(28, minOf(56, primaryText.length))
        } else {
            "GOTCHI PHONE // ${uiState.skin.uppercase()}".take(28)
        }

        textPaint.textSize = maxOf(12f, viewportSize * 0.054f)
        canvas.drawText(line1, cx, h - maxOf(28f, viewportSize * 0.12f), textPaint)

        textPaint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.NORMAL)
        textPaint.textSize = maxOf(10f, viewportSize * 0.042f)
        canvas.drawText(line2.take(28), cx, h - maxOf(12f, viewportSize * 0.05f), textPaint)

        // 6. Diagonal optical cover-glass sheen reflection sweep
        if (!uiState.epaperMode) {
            glassPaint.shader = LinearGradient(
                0f, 0f, w, h,
                intArrayOf(
                    Color.argb(26, 255, 255, 255),
                    Color.argb(8, 255, 255, 255),
                    Color.argb(0, 255, 255, 255)
                ),
                floatArrayOf(0f, 0.42f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawRect(0f, 0f, w, h, glassPaint)
        }

        // Continuous 60 FPS animation loop
        postInvalidateOnAnimation()
    }
}
