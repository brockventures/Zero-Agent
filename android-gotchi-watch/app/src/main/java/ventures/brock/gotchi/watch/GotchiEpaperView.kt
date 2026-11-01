package ventures.brock.gotchi.watch

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
 * 200x200 1-Bit Monochrome E-Paper Viewport for Wear OS Pixel Watch (`GDEY0154D67-FT` V2.6).
 *
 * Tailored for the circular Pixel Watch display (minSdk = 30):
 * - Renders the 200x200 1-bit monochrome e-paper canvas centered inside the circular watch face.
 * - Circular outer bezel status arc for Pinch-to-Talk (amber/coral) and Hey Zero wake listener (cyan) indicators.
 * - 9 states: `idle`, `listening`, `transcribing`, `thinking`, `synthesizing`, `speaking`, `error`, `busy`, `offline`.
 * - Three skins: `zero` (1-bit pre-baked sprites in `assets/zero_skin/`), `amos` (1-bit pre-baked sprites
 *   in `assets/amos_skin/`), and `gotchi` (vector companion face).
 * - Realistic warm e-ink substrate (`#EAE6DC`) and carbon pigment (`#141418`) with optical cover-glass sheen.
 * - `RefreshTracker` for 5 partial refreshes before a 450 ms full-flash purge.
 */
class GotchiEpaperView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    companion object {
        const val EPD_WIDTH = 200
        const val EPD_HEIGHT = 200
        const val PARTIAL_REFRESH_MS = 300L
        const val FULL_REFRESH_MS = 450L // 450 ms full-flash purge
        const val PORTRAIT_SCALE_RATIO = 0.65f
        const val ANIM_FRAME_INTERVAL_MS = 16L // 60 FPS

        val EINK_PAPER_COLOR = Color.rgb(234, 230, 220)
        val EINK_INK_COLOR = Color.rgb(20, 20, 24)

        val OLED_BG_COLOR = Color.rgb(0, 0, 0)
        val OLED_FG_COLOR = Color.rgb(234, 230, 220)

        // Circular bezel status arc indicators
        val BEZEL_TRACK_COLOR = Color.argb(80, 45, 45, 52)
        val BEZEL_PTT_ACTIVE_COLOR = Color.rgb(255, 90, 45) // Coral/amber for active Push-to-Talk
        val BEZEL_PTT_IDLE_COLOR = Color.argb(120, 100, 100, 115)
        val BEZEL_WAKE_ACTIVE_COLOR = Color.rgb(50, 180, 255) // Cyan for active 'Hey Zero' pre-roll listener
        val BEZEL_WAKE_IDLE_COLOR = Color.argb(60, 50, 180, 255)

        val VALID_STATES = setOf(
            "idle", "listening", "transcribing", "thinking",
            "synthesizing", "speaking", "error", "busy", "offline"
        )
    }

    data class RefreshTracker(
        val maxConsecutivePartials: Int = 5, // 5 partial refreshes before 450 ms full-flash purge
        val fullRefreshMinutes: Double = 15.0,
        val busyTimeoutSeconds: Double = 30.0,
        var hasDrawnInitial: Boolean = false,
        var consecutivePartials: Int = 0,
        var lastFullRefreshMs: Long = 0L,
        val history: MutableList<String> = mutableListOf()
    ) {
        fun selectRefreshMode(nowMs: Long = SystemClock.elapsedRealtime(), forceFull: Boolean = false): String {
            if (forceFull || !hasDrawnInitial) return "full"
            if (consecutivePartials >= maxConsecutivePartials) return "full"
            if ((nowMs - lastFullRefreshMs) >= (fullRefreshMinutes * 60_000.0).toLong()) return "full"
            return "partial"
        }

        fun recordWrite(mode: String, succeeded: Boolean = true, nowMs: Long = SystemClock.elapsedRealtime()): String {
            val resolved = if (!succeeded && mode == "partial") "full" else mode
            if (resolved == "full") {
                hasDrawnInitial = true
                consecutivePartials = 0
                lastFullRefreshMs = nowMs
            } else {
                consecutivePartials += 1
            }
            history.add(resolved)
            return resolved
        }
    }

    data class UIState(
        var state: String = "idle",
        var transcript: String = "",
        var reply: String = "",
        var statusText: String = "PINCH TO TALK",
        var thinkingSeconds: Int = 0,
        var audioBytesReceived: Int = 0,
        var lastSampleRate: Int = 24000,
        var batteryPct: Int = 96,
        var skin: String = "zero", // "zero" | "amos" | "gotchi"
        var lastRefreshMode: String = "full",
        var pttActive: Boolean = false,
        var wakeListenerActive: Boolean = true,
        var oledDarkMode: Boolean = true,
        var animFrame: Int = 0,
        var blink: Boolean = false,
        var epaperMode: Boolean = false
    )

    val refreshTracker = RefreshTracker()
    val uiState = UIState()
    private val zeroHdCache = mutableMapOf<String, Bitmap>()
    private val frameBitmap = Bitmap.createBitmap(EPD_WIDTH, EPD_HEIGHT, Bitmap.Config.ARGB_8888)
    private val frameCanvas = Canvas(frameBitmap)
    private val destRect = RectF()
    private val bezelRect = RectF()
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val bezelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }
    private val glassPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val overlayPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private var fullFlashPhase: Int = 0 // 0=normal, 1=invert-black, 2=invert-white

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

        val mode = refreshTracker.selectRefreshMode(forceFull = forceFullRefresh)
        uiState.lastRefreshMode = refreshTracker.recordWrite(mode, succeeded = true)
        render200x200Framebuffer()

        if (uiState.lastRefreshMode == "full" && !uiState.oledDarkMode) {
            fullFlashPhase = 1
            invalidate()
            postDelayed({ fullFlashPhase = 2; invalidate() }, 150L)
            postDelayed({ fullFlashPhase = 0; invalidate() }, 450L) // 450 ms full purge
        } else {
            fullFlashPhase = 0
            invalidate()
        }
    }

    fun setOledDarkMode(enabled: Boolean) {
        if (uiState.oledDarkMode != enabled) {
            uiState.oledDarkMode = enabled
            uiState.epaperMode = !enabled
            updateState(forceFullRefresh = true)
        }
    }

    fun setEpaperMode(enabled: Boolean) {
        if (uiState.epaperMode != enabled) {
            uiState.epaperMode = enabled
            uiState.oledDarkMode = !enabled
            updateState(forceFullRefresh = true)
        }
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
            updateState(forceFullRefresh = true)
        }
    }

    fun setState(newState: String) {
        updateState(newState = newState)
    }

    fun render200x200Framebuffer(): Bitmap {
        val c = frameCanvas
        val bgColor = if (uiState.oledDarkMode) OLED_BG_COLOR else EINK_PAPER_COLOR
        val fgColor = if (uiState.oledDarkMode) OLED_FG_COLOR else EINK_INK_COLOR

        c.drawColor(bgColor)

        val st = if (uiState.state in VALID_STATES) uiState.state else "idle"
        val badge = if (uiState.lastRefreshMode == "full") "FULL" else "PART"

        if (uiState.skin == "zero" || uiState.skin == "amos") {
            val assetPath = "${uiState.skin}_skin/${uiState.skin}_${st}_200x200_1bit.png"
            runCatching {
                context.assets.open(assetPath).use { stream ->
                    BitmapFactory.decodeStream(stream)?.let { rawBmp ->
                        val tinted = Bitmap.createBitmap(EPD_WIDTH, EPD_HEIGHT, Bitmap.Config.ARGB_8888)
                        val pixels = IntArray(EPD_WIDTH * EPD_HEIGHT)
                        rawBmp.getPixels(pixels, 0, EPD_WIDTH, 0, 0, EPD_WIDTH, EPD_HEIGHT)
                        for (y in 0 until EPD_HEIGHT) {
                            for (x in 0 until EPD_WIDTH) {
                                val idx = y * EPD_WIDTH + x
                                if (y < 22 || y >= 146 || x < 2 || x >= 198) {
                                    pixels[idx] = bgColor
                                } else {
                                    val r = Color.red(pixels[idx])
                                    pixels[idx] = if (uiState.skin == "zero") {
                                        if (r > 128) fgColor else bgColor
                                    } else {
                                        if (r > 128) bgColor else fgColor
                                    }
                                }
                            }
                        }
                        tinted.setPixels(pixels, 0, EPD_WIDTH, 0, 0, EPD_WIDTH, EPD_HEIGHT)
                        c.drawBitmap(tinted, 0f, 0f, null)
                    }
                }
            }
        } else {
            drawGotchiVectorFace(c, st, bgColor, fgColor)
        }

        // Top status (centered, borderless)
        paint.style = Paint.Style.FILL
        paint.color = fgColor
        paint.textAlign = Paint.Align.CENTER
        paint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
        paint.textSize = 10.0f
        val prefix = when (uiState.skin) { "zero" -> "ZERO"; "amos" -> "AMOS"; else -> "GOTCHI" }
        val headerText = if (st == "idle") prefix else "$prefix // ${st.uppercase()}"
        c.drawText(headerText, 100f, 19f, paint)

        // Bottom captions (centered, borderless)
        val primaryText = when {
            uiState.reply.isNotBlank() -> uiState.reply
            uiState.transcript.isNotBlank() -> uiState.transcript
            else -> uiState.statusText
        }
        val line1 = primaryText.take(22)
        val line2 = if (primaryText.length > 22) {
            primaryText.substring(22, minOf(44, primaryText.length))
        } else {
            "PIXEL WATCH // ${uiState.skin.uppercase()}".take(22)
        }

        paint.textAlign = Paint.Align.CENTER
        paint.color = fgColor
        paint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
        paint.textSize = 11.0f
        c.drawText(line1, 100f, 162f, paint)

        paint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.NORMAL)
        paint.textSize = 9.5f
        c.drawText(line2.take(22), 100f, 177f, paint)

        return frameBitmap
    }

    private fun drawGotchiVectorFace(
        c: Canvas,
        st: String,
        bgColor: Int = if (uiState.oledDarkMode) OLED_BG_COLOR else EINK_PAPER_COLOR,
        fgColor: Int = if (uiState.oledDarkMode) OLED_FG_COLOR else EINK_INK_COLOR
    ) {
        paint.color = fgColor
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = 3f
        c.drawRoundRect(RectF(42f, 30f, 158f, 134f), 18f, 18f, paint)

        paint.style = Paint.Style.FILL
        val leftEar = Path().apply { moveTo(56f, 30f); lineTo(66f, 18f); lineTo(76f, 30f); close() }
        val rightEar = Path().apply { moveTo(124f, 30f); lineTo(134f, 18f); lineTo(144f, 30f); close() }
        c.drawPath(leftEar, paint)
        c.drawPath(rightEar, paint)

        when (st) {
            "idle" -> {
                c.drawOval(RectF(66f, 62f, 82f, 78f), paint)
                c.drawOval(RectF(118f, 62f, 134f, 78f), paint)
                paint.color = bgColor
                c.drawOval(RectF(70f, 65f, 75f, 70f), paint)
                c.drawOval(RectF(122f, 65f, 127f, 70f), paint)
                paint.color = fgColor
                paint.style = Paint.Style.STROKE
                c.drawArc(RectF(82f, 88f, 118f, 110f), 15f, 150f, false, paint)
            }
            "listening" -> {
                paint.style = Paint.Style.STROKE
                c.drawOval(RectF(62f, 58f, 84f, 80f), paint)
                c.drawOval(RectF(116f, 58f, 138f, 80f), paint)
                c.drawOval(RectF(91f, 94f, 109f, 112f), paint)
                paint.style = Paint.Style.FILL
                c.drawOval(RectF(69f, 65f, 77f, 73f), paint)
                c.drawOval(RectF(123f, 65f, 131f, 73f), paint)
            }
            "transcribing" -> {
                c.drawRect(64f, 66f, 84f, 76f, paint)
                c.drawRect(116f, 66f, 136f, 76f, paint)
                val bars = intArrayOf(6, 14, 10, 18, 8, 14, 6)
                for (i in bars.indices) {
                    val bx = 72f + i * 9f
                    val h = bars[i] / 2f
                    c.drawRect(bx, 104f - h, bx + 5f, 104f + h, paint)
                }
            }
            "thinking", "synthesizing" -> {
                c.drawOval(RectF(66f, 56f, 82f, 72f), paint)
                c.drawOval(RectF(118f, 56f, 134f, 72f), paint)
                paint.style = Paint.Style.STROKE
                c.drawLine(84f, 102f, 116f, 102f, paint)
                c.drawOval(RectF(162f, 48f, 170f, 56f), paint)
                c.drawOval(RectF(172f, 34f, 186f, 48f), paint)
            }
            "speaking" -> {
                paint.style = Paint.Style.STROKE
                c.drawLine(64f, 74f, 74f, 62f, paint)
                c.drawLine(74f, 62f, 84f, 74f, paint)
                c.drawLine(116f, 74f, 126f, 62f, paint)
                c.drawLine(126f, 62f, 136f, 74f, paint)
                paint.style = Paint.Style.FILL
                c.drawArc(RectF(80f, 90f, 120f, 118f), 0f, 180f, true, paint)
            }
            else -> {
                paint.style = Paint.Style.STROKE
                c.drawLine(64f, 60f, 82f, 78f, paint)
                c.drawLine(82f, 60f, 64f, 78f, paint)
                c.drawLine(118f, 60f, 136f, 78f, paint)
                c.drawLine(136f, 60f, 118f, 78f, paint)
                c.drawArc(RectF(82f, 98f, 118f, 118f), 195f, 150f, false, paint)
            }
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

    /**
     * Renders character portrait at native watch resolution scaled to [PORTRAIT_SCALE_RATIO] (65% of circular inner diameter),
     * with head posture shifts (dx, dy per state), periodic eye blinks, 60 FPS animated overlays
     * (listening soundwaves, thinking dots, speaking mouth shapes + 5-bar equalizer), and crisp anti-aliased text.
     */
    fun drawNativeHighResViewport(canvas: Canvas, cx: Float, cy: Float, innerRadius: Float) {
        val st = if (uiState.state in VALID_STATES) uiState.state else "idle"
        val bgColor = if (uiState.oledDarkMode) OLED_BG_COLOR else EINK_PAPER_COLOR
        val fgColor = if (uiState.oledDarkMode) OLED_FG_COLOR else EINK_INK_COLOR

        // Fill viewport background
        bgPaint.style = Paint.Style.FILL
        bgPaint.color = bgColor
        canvas.drawCircle(cx, cy, innerRadius, bgPaint)

        // Advance animation frame counter (60 FPS tick)
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

        // Portrait bounds (65% of circular inner diameter)
        val portraitDiameter = innerRadius * 2f * PORTRAIT_SCALE_RATIO

        val hdPortrait = if (!uiState.epaperMode && uiState.skin == "zero") {
            getZeroHdBitmap(st, (uiState.animFrame / 16) % 3, uiState.blink)
        } else {
            null
        }

        if (hdPortrait != null) {
            val hdCx = cx
            val hdCy = cy - innerRadius * 0.04f
            val hdRect = RectF(
                hdCx - portraitDiameter / 2f,
                hdCy - portraitDiameter / 2f,
                hdCx + portraitDiameter / 2f,
                hdCy + portraitDiameter / 2f
            )
            paint.isFilterBitmap = true
            canvas.drawBitmap(hdPortrait, null, hdRect, paint)
        } else {
            val portraitWidth = portraitDiameter
            val portraitHeight = portraitDiameter * (124f / 200f)
            val portraitCx = cx + headDx
            val portraitCy = cy + headDy - innerRadius * 0.03f
            val portraitRect = RectF(
                portraitCx - portraitWidth / 2f,
                portraitCy - portraitHeight / 2f,
                portraitCx + portraitWidth / 2f,
                portraitCy + portraitHeight / 2f
            )

            // Draw character portrait sprite from frameBitmap cropped to character area (y: 22..146)
            val srcRect = Rect(0, 22, 200, 146)
            paint.isFilterBitmap = true
            canvas.drawBitmap(frameBitmap, srcRect, portraitRect, paint)

            // Periodic eye blinks
            if (uiState.blink) {
                val eyeY = portraitRect.top + portraitRect.height() * 0.38f
                val leftEyeX = portraitRect.left + portraitRect.width() * 0.36f
                val rightEyeX = portraitRect.left + portraitRect.width() * 0.64f
                val eyeHalfWidth = portraitRect.width() * 0.08f
                overlayPaint.style = Paint.Style.FILL
                overlayPaint.color = bgColor
                canvas.drawRect(leftEyeX - eyeHalfWidth, eyeY - innerRadius * 0.025f, leftEyeX + eyeHalfWidth, eyeY + innerRadius * 0.025f, overlayPaint)
                canvas.drawRect(rightEyeX - eyeHalfWidth, eyeY - innerRadius * 0.025f, rightEyeX + eyeHalfWidth, eyeY + innerRadius * 0.025f, overlayPaint)
                overlayPaint.color = fgColor
                overlayPaint.style = Paint.Style.STROKE
                overlayPaint.strokeWidth = maxOf(2.5f, innerRadius * 0.016f)
                overlayPaint.strokeCap = Paint.Cap.ROUND
                canvas.drawLine(leftEyeX - eyeHalfWidth, eyeY, leftEyeX + eyeHalfWidth, eyeY, overlayPaint)
                canvas.drawLine(rightEyeX - eyeHalfWidth, eyeY, rightEyeX + eyeHalfWidth, eyeY, overlayPaint)
            }

            // 60 FPS animated overlays
            when {
                st == "listening" -> {
                    // Pulsing soundwave arcs for listening
                    val wavePhase = (uiState.animFrame % 30) / 30f
                    val waveRadiusBase = portraitWidth * 0.44f
                    overlayPaint.style = Paint.Style.STROKE
                    overlayPaint.strokeCap = Paint.Cap.ROUND
                    overlayPaint.strokeWidth = maxOf(2.5f, innerRadius * 0.018f)
                    for (i in 0..2) {
                        val phaseOffset = (wavePhase + i / 3f) % 1f
                        val currentRadius = waveRadiusBase + phaseOffset * innerRadius * 0.22f
                        val alpha = ((1f - phaseOffset) * 220).toInt().coerceIn(0, 255)
                        overlayPaint.color = Color.argb(alpha, Color.red(fgColor), Color.green(fgColor), Color.blue(fgColor))
                        val waveRect = RectF(
                            portraitCx - currentRadius,
                            portraitCy - currentRadius,
                            portraitCx + currentRadius,
                            portraitCy + currentRadius
                        )
                        canvas.drawArc(waveRect, 150f, 60f, false, overlayPaint)
                        canvas.drawArc(waveRect, 330f, 60f, false, overlayPaint)
                    }
                }
                st in setOf("thinking", "synthesizing") -> {
                    // Cycling thought cloud dots
                    val dotCount = 3
                    val activeDot = (uiState.animFrame / 12) % dotCount
                    overlayPaint.style = Paint.Style.FILL
                    val baseDotX = portraitCx + portraitWidth * 0.36f
                    val baseDotY = portraitCy - portraitHeight * 0.30f
                    val dotOffsets = arrayOf(
                        Pair(0f, 0f),
                        Pair(innerRadius * 0.08f, -innerRadius * 0.06f),
                        Pair(innerRadius * 0.16f, -innerRadius * 0.13f)
                    )
                    val dotSizes = floatArrayOf(innerRadius * 0.024f, innerRadius * 0.036f, innerRadius * 0.052f)
                    for (i in 0 until dotCount) {
                        val (ox, oy) = dotOffsets[i]
                        val dRadius = dotSizes[i]
                        overlayPaint.color = if (i == activeDot) {
                            fgColor
                        } else {
                            Color.argb(80, Color.red(fgColor), Color.green(fgColor), Color.blue(fgColor))
                        }
                        canvas.drawCircle(baseDotX + ox, baseDotY + oy, dRadius, overlayPaint)
                    }
                }
                st == "speaking" -> {
                    // 3 mouth shapes cycling with speech cadence
                    val mouthCx = portraitRect.centerX()
                    val mouthCy = portraitRect.top + portraitRect.height() * 0.68f
                    overlayPaint.color = bgColor
                    overlayPaint.style = Paint.Style.FILL
                    canvas.drawRoundRect(
                        RectF(mouthCx - innerRadius * 0.12f, mouthCy - innerRadius * 0.08f, mouthCx + innerRadius * 0.12f, mouthCy + innerRadius * 0.08f),
                        6f, 6f, overlayPaint
                    )

                    overlayPaint.color = fgColor
                    val mouthShape = (uiState.animFrame / 8) % 3
                    when (mouthShape) {
                        0 -> {
                            overlayPaint.style = Paint.Style.FILL
                            canvas.drawOval(
                                RectF(mouthCx - innerRadius * 0.06f, mouthCy - innerRadius * 0.03f, mouthCx + innerRadius * 0.06f, mouthCy + innerRadius * 0.03f),
                                overlayPaint
                            )
                        }
                        1 -> {
                            overlayPaint.style = Paint.Style.FILL
                            canvas.drawArc(
                                RectF(mouthCx - innerRadius * 0.09f, mouthCy - innerRadius * 0.05f, mouthCx + innerRadius * 0.09f, mouthCy + innerRadius * 0.05f),
                                0f, 180f, true, overlayPaint
                            )
                        }
                        else -> {
                            overlayPaint.style = Paint.Style.STROKE
                            overlayPaint.strokeWidth = maxOf(2.5f, innerRadius * 0.018f)
                            canvas.drawCircle(mouthCx, mouthCy, innerRadius * 0.045f, overlayPaint)
                        }
                    }

                    // 5-bar equalizer below portrait
                    overlayPaint.style = Paint.Style.FILL
                    overlayPaint.color = fgColor
                    val numBars = 5
                    val barWidth = maxOf(3f, innerRadius * 0.024f)
                    val barGap = maxOf(2f, innerRadius * 0.016f)
                    val totalWidth = numBars * barWidth + (numBars - 1) * barGap
                    val startX = cx - totalWidth / 2f
                    val eqBaseY = portraitRect.bottom + innerRadius * 0.07f
                    for (i in 0 until numBars) {
                        val bx = startX + i * (barWidth + barGap)
                        val wave = kotlin.math.abs(kotlin.math.sin(uiState.animFrame * 0.35f + i * 1.2f))
                        val barHeight = innerRadius * (0.03f + 0.10f * wave)
                        canvas.drawRoundRect(
                            RectF(bx, eqBaseY - barHeight / 2f, bx + barWidth, eqBaseY + barHeight / 2f),
                            barWidth / 2f, barWidth / 2f, overlayPaint
                        )
                    }
                }
            }
        }

        // Crisp native-resolution anti-aliased status & caption text
        val prefix = when (uiState.skin) { "zero" -> "ZERO"; "amos" -> "AMOS"; else -> "GOTCHI" }
        val headerText = if (st == "idle") prefix else "$prefix // ${st.uppercase()}"

        textPaint.style = Paint.Style.FILL
        textPaint.color = fgColor
        textPaint.textAlign = Paint.Align.CENTER
        textPaint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
        textPaint.textSize = maxOf(12f, innerRadius * 0.11f)
        canvas.drawText(headerText, cx, cy - innerRadius * 0.72f, textPaint)

        val primaryText = when {
            uiState.reply.isNotBlank() -> uiState.reply
            uiState.transcript.isNotBlank() -> uiState.transcript
            else -> uiState.statusText
        }
        val line1 = primaryText.take(22)
        val line2 = if (primaryText.length > 22) {
            primaryText.substring(22, minOf(44, primaryText.length))
        } else {
            "PIXEL WATCH // ${uiState.skin.uppercase()}".take(22)
        }

        textPaint.textSize = maxOf(12f, innerRadius * 0.12f)
        canvas.drawText(line1, cx, cy + innerRadius * 0.68f, textPaint)

        textPaint.typeface = Typeface.create(Typeface.MONOSPACE, Typeface.NORMAL)
        textPaint.textSize = maxOf(10f, innerRadius * 0.095f)
        canvas.drawText(line2.take(22), cx, cy + innerRadius * 0.82f, textPaint)

        // Continuous 60 FPS animation loop
        postInvalidateOnAnimation()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val diameter = minOf(width, height).toFloat()
        if (diameter <= 0f) return

        val cx = width / 2f
        val cy = height / 2f
        val radius = diameter / 2f

        // 1. Draw watch face background (pure black OLED substrate)
        canvas.drawColor(Color.BLACK)

        // 2. Circular Bezel Status Arcs:
        // Stroke width scaled to watch diameter (typically 6-8 dp)
        val bezelStroke = maxOf(4f, diameter * 0.024f)
        val bezelRadius = radius - (bezelStroke / 2f) - 3f
        bezelRect.set(cx - bezelRadius, cy - bezelRadius, cx + bezelRadius, cy + bezelRadius)

        bezelPaint.strokeWidth = bezelStroke

        // Draw track background for full 360 circle
        bezelPaint.color = BEZEL_TRACK_COLOR
        canvas.drawCircle(cx, cy, bezelRadius, bezelPaint)

        // Push-to-Talk status arc on left side (135° to 225°, sweep 90°)
        bezelPaint.color = if (uiState.pttActive || uiState.state == "listening") {
            BEZEL_PTT_ACTIVE_COLOR
        } else {
            BEZEL_PTT_IDLE_COLOR
        }
        canvas.drawArc(bezelRect, 135f, 90f, false, bezelPaint)

        // 'Hey Zero' wake phrase pre-roll listener arc on right side (315° to 45°, sweep 90°)
        bezelPaint.color = if (uiState.wakeListenerActive) {
            BEZEL_WAKE_ACTIVE_COLOR
        } else {
            BEZEL_WAKE_IDLE_COLOR
        }
        canvas.drawArc(bezelRect, 315f, 90f, false, bezelPaint)

        // 3. Center the 200x200 1-bit monochrome canvas inside circular viewport
        val innerRadius = bezelRadius - (bezelStroke / 2f) - 6f
        destRect.set(cx - innerRadius, cy - innerRadius, cx + innerRadius, cy + innerRadius)

        val clipPath = Path().apply { addCircle(cx, cy, innerRadius, Path.Direction.CW) }
        canvas.save()
        canvas.clipPath(clipPath)

        when (fullFlashPhase) {
            1 -> {
                paint.style = Paint.Style.FILL
                paint.color = if (uiState.oledDarkMode) OLED_FG_COLOR else EINK_INK_COLOR
                canvas.drawRect(destRect, paint)
            }
            2 -> {
                paint.style = Paint.Style.FILL
                paint.color = if (uiState.oledDarkMode) OLED_BG_COLOR else EINK_PAPER_COLOR
                canvas.drawRect(destRect, paint)
            }
            else -> {
                drawNativeHighResViewport(canvas, cx, cy, innerRadius)

                if (!uiState.oledDarkMode) {
                    // Diagonal GDEY0154D67-FT optical cover-glass sheen reflection sweep
                    glassPaint.shader = LinearGradient(
                        destRect.left, destRect.top, destRect.right, destRect.bottom,
                        intArrayOf(
                            Color.argb(22, 255, 255, 255),
                            Color.argb(6, 255, 255, 255),
                            Color.argb(0, 255, 255, 255)
                        ),
                        floatArrayOf(0f, 0.40f, 1f),
                        Shader.TileMode.CLAMP
                    )
                    canvas.drawRect(destRect, glassPaint)
                }
            }
        }

        canvas.restore()
    }
}
