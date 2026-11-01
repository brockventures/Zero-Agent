package ventures.brock.gotchi.watch

import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.tanh

/**
 * 1C-2A-2 Reed & Shutter Audio Chime Synthesizer for Gotchi Wear OS Watch.
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
        val noteN = (CHIME_SR * (ms / 1000.0f)).toInt()
        val idx0 = (startSec * CHIME_SR).toInt() + leadN
        var attackN = (CHIME_SR * (attackMs / 1000.0f)).toInt()
        if (attackN < 1) attackN = 1
        var releaseN = (CHIME_SR * (releaseMs / 1000.0f)).toInt()
        if (releaseN < 1) releaseN = 1
        if (releaseN > noteN / 2) releaseN = noteN / 2

        var phase = 0.0f
        for (i in 0 until noteN) {
            if (idx0 + i >= maxLen) break
            val t = i.toFloat() / CHIME_SR.toFloat()
            val frac = i.toFloat() / (if (noteN > 0) noteN.toFloat() else 1.0f)
            val vib = 1.0f + if (t > 0.045f) {
                (vibratoDepth * sin((2.0 * Math.PI * vibratoHz.toDouble() * t.toDouble()))).toFloat()
            } else {
                0.0f
            }
            val curF = freq * vib
            phase += (2.0 * Math.PI * curF.toDouble() / CHIME_SR.toDouble()).toFloat()

            var env = if (i < attackN) {
                i.toFloat() / attackN.toFloat()
            } else {
                val effectiveTau = if (decayTau > 0.06f) decayTau else 0.06f
                exp(-((frac - (attackN.toFloat() / noteN.toFloat())) / effectiveTau))
            }

            val rem = noteN - 1 - i
            if (rem < releaseN) {
                val relFrac = rem.toFloat() / releaseN.toFloat()
                val taper = 0.5f * (1.0f - cos((Math.PI * relFrac.toDouble())).toFloat())
                env *= taper
            }

            val reed = casioReedWave(phase, 1.0f)
            val sat = tanh((reed * amp * 1.35f).toDouble()).toFloat()
            mixBuf[idx0 + i] += sat * env * 25500.0f
        }
    }

    fun addShutterImpact(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        thumpStartHz: Float,
        thumpEndHz: Float,
        metalHz: Float,
        noiseHz: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val rng = SimpleRng(seed)
        val bpNoise = BiquadBp().apply { init(noiseHz, 1.4f) }
        val hiNoise = BiquadBp().apply { init(metalHz * 1.25f, 2.2f) }

        var phaseThump = 0.0f
        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val raw = rng.uniformSigned()
            val bpn = bpNoise.step(raw)
            val hin = hiNoise.step(raw)

            val curThumpF = thumpStartHz * (thumpEndHz / thumpStartHz).toDouble().pow(frac.toDouble().pow(0.55)).toFloat()
            phaseThump += (2.0 * Math.PI * curThumpF.toDouble() / CHIME_SR.toDouble()).toFloat()
            val envThump = exp(-frac * 5.2f)
            val envSnap  = exp(-frac * 7.5f)
            val envHi    = exp(-frac * 12.0f)

            val t = i.toFloat() / CHIME_SR.toFloat()
            val thump = sin(phaseThump.toDouble()).toFloat() * envThump
            val metalRing = (sin((2.0 * Math.PI * metalHz.toDouble() * t.toDouble())).toFloat() +
                0.5f * sin((2.0 * Math.PI * (metalHz * 1.47f).toDouble() * t.toDouble())).toFloat()) * envHi
            val v = 0.48f * thump + 0.65f * bpn * envSnap + 0.45f * hin * envHi + 0.28f * metalRing
            mixBuf[idx0 + i] += tanh((v * amp * 1.7f).toDouble()).toFloat() * 29500.0f
        }
    }

    fun addCurtainZip(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        startHz: Float,
        endHz: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val rng = SimpleRng(seed)
        val bp1 = BiquadBp().apply { init(startHz, 1.6f) }
        val bp2 = BiquadBp().apply { init(endHz, 1.8f) }

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val raw = rng.uniformSigned()
            val y1 = bp1.step(raw)
            val y2 = bp2.step(raw)
            val t = i.toFloat() / CHIME_SR.toFloat()
            val env = sin((Math.PI * frac.toDouble().pow(0.65))).toFloat() * (0.4f + 0.6f * frac)
            val ripple = 0.75f + 0.25f * sin((2.0 * Math.PI * 680.0 * t.toDouble())).toFloat()
            val mix = (1.0f - frac) * y1 + frac * y2
            mixBuf[idx0 + i] += tanh((mix * env * ripple * amp * 2.2f).toDouble()).toFloat() * 26000.0f
        }
    }

    fun addSpringRingTail(
        mixBuf: FloatArray,
        maxLen: Int,
        startSec: Float,
        ms: Float,
        freq: Float,
        amp: Float,
        seed: Long
    ) {
        val idx0 = (startSec * CHIME_SR).toInt()
        var n = (CHIME_SR * (ms / 1000.0f)).toInt()
        if (n < 1) n = 1
        val rng = SimpleRng(seed)
        val bp = BiquadBp().apply { init(freq, 4.5f) }

        for (i in 0 until n) {
            if (idx0 + i >= maxLen) break
            val frac = i.toFloat() / n.toFloat()
            val t = i.toFloat() / CHIME_SR.toFloat()
            val env = exp(-frac * 5.8f)
            val ring = sin((2.0 * Math.PI * freq.toDouble() * t.toDouble())).toFloat() +
                0.45f * sin((2.0 * Math.PI * (freq * 1.31f).toDouble() * t.toDouble())).toFloat() +
                0.35f * bp.step(rng.uniformSigned())
            mixBuf[idx0 + i] += ring * env * amp * 22000.0f
        }
    }

    fun synthesizeChimePcm16(kind: String): ByteArray {
        val k = kind.trim().lowercase()
        if (k !in VALID_CHIME_KINDS) return ByteArray(0)

        val mixBuf = FloatArray(MAX_CHIME_SAMPLES)
        var totalSamples = 0

        when (k) {
            "ptt_start" -> {
                // 1C-2A-2 PTT Start (~295ms): Switch latch + F5 (118ms) -> C6 (185ms)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.000f, 698.5f, 118.0f, 0.68f, 7.5f, 2300.0f, 10.0f, 6.0f, 0.38f, 28.0f, 5.2f, 0.006f)
                addReedNoteSmooth(mixBuf, MAX_CHIME_SAMPLES, 0.102f, 1046.5f, 185.0f, 0.68f, 6.5f, 2750.0f, 8.0f, 6.0f, 0.36f, 55.0f, 5.2f, 0.007f)
                totalSamples = (0.300f * CHIME_SR).toInt()
            }
            "ptt_stop" -> {
                // 1C-2A-2 PTT Stop (~270ms): Switch release + C6 (108ms) -> F5 (165ms) + mechanical lock click
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
