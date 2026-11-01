package ventures.brock.gotchi.forwarder

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.zip.CRC32

/**
 * GOTCHI V1 BLE GATT / L2CAP CoC Binary Framing & 4-Bit IMA ADPCM Codec (F18 / T4.1 / T4.2).
 *
 * Matches the 16-byte little-endian PacketHeader, 4-byte IEEE 802.3 CRC32 trailer, and
 * 12-byte `GAD1` 4-bit IMA ADPCM audio container defined in firmware/include/ble_framing.h
 * and backend/ble_framing.py.
 */
object BleFramingCodec {
    const val MAGIC: Int = 0x4742 // 'GB' little-endian
    const val HEADER_BYTES: Int = 16
    const val TRAILER_BYTES: Int = 4
    const val OVERHEAD_BYTES: Int = HEADER_BYTES + TRAILER_BYTES

    const val DEFAULT_GATT_MTU: Int = 512
    const val DEFAULT_MAX_PAYLOAD: Int = 492
    const val L2CAP_MAX_PAYLOAD: Int = 2048
    const val MAX_FRAME_INDEX: Int = 4

    const val FLAG_NONE: Int = 0x00
    const val FLAG_FIRST_CHUNK: Int = 0x01
    const val FLAG_LAST_CHUNK: Int = 0x02
    const val FLAG_SINGLE_CHUNK: Int = 0x03 // FIRST_CHUNK | LAST_CHUNK
    const val FLAG_REQUIRES_ACK: Int = 0x04
    const val FLAG_ADPCM: Int = 0x08 // 4-bit IMA ADPCM compressed audio payload (GAD1)

    enum class FrameType(val id: Int) {
        AUDIO_CHUNK(0x01),
        IMAGE_FRAME(0x02),
        TURN_COMMIT(0x03),
        SSE_EVENT(0x04),
        ACK(0x05),
        CANCEL(0x06),
        CONTROL(0x07);

        companion object {
            fun fromId(id: Int): FrameType? = values().firstOrNull { it.id == id }
        }
    }

    data class PacketHeader(
        val magic: Int,
        val frameType: FrameType,
        val flags: Int,
        val frameIndex: Int,
        val reserved: Int,
        val seqNum: Int,
        val chunkIdx: Int,
        val totalChunks: Int,
        val payloadLen: Int,
        val headerCrc16: Int
    )

    data class DecodedPacket(
        val header: PacketHeader,
        val payload: ByteArray,
        val payloadCrc32: Long
    )

    /**
     * CRC-16/CCITT-FALSE (poly=0x1021, init=0xFFFF) over the first 14 bytes of PacketHeader.
     */
    fun crc16Ccitt(data: ByteArray, offset: Int = 0, length: Int = data.size - offset): Int {
        var crc = 0xFFFF
        for (i in offset until (offset + length)) {
            crc = crc xor ((data[i].toInt() and 0xFF) shl 8)
            for (bit in 0 until 8) {
                crc = if ((crc and 0x8000) != 0) {
                    ((crc shl 1) xor 0x1021) and 0xFFFF
                } else {
                    (crc shl 1) and 0xFFFF
                }
            }
        }
        return crc and 0xFFFF
    }

    /**
     * Standard IEEE 802.3 CRC-32 over payload bytes (returns 0 when payload is empty).
     */
    fun crc32Ieee(data: ByteArray): Long {
        if (data.isEmpty()) return 0L
        val crc = CRC32()
        crc.update(data)
        return crc.value and 0xFFFFFFFFL
    }

    fun encodePacket(
        frameType: FrameType,
        flags: Int,
        seqNum: Int,
        chunkIdx: Int,
        totalChunks: Int,
        payload: ByteArray,
        frameIndex: Int = 0
    ): ByteArray {
        require(totalChunks > 0 && chunkIdx in 0 until totalChunks) { "invalid_chunk_bounds" }
        require(frameIndex in 0..MAX_FRAME_INDEX) { "invalid_frame_index" }
        require(payload.size <= L2CAP_MAX_PAYLOAD) { "payload_too_large" }

        val prefix = ByteBuffer.allocate(14).order(ByteOrder.LITTLE_ENDIAN)
        prefix.putShort(MAGIC.toShort())
        prefix.put(frameType.id.toByte())
        prefix.put(flags.toByte())
        prefix.put(frameIndex.toByte())
        prefix.put(0.toByte())
        prefix.putShort((seqNum and 0xFFFF).toShort())
        prefix.putShort((chunkIdx and 0xFFFF).toShort())
        prefix.putShort((totalChunks and 0xFFFF).toShort())
        prefix.putShort(payload.size.toShort())
        val prefixBytes = prefix.array()
        val hCrc = crc16Ccitt(prefixBytes, 0, 14)

        val pCrc = crc32Ieee(payload)
        val out = ByteBuffer.allocate(HEADER_BYTES + payload.size + TRAILER_BYTES)
            .order(ByteOrder.LITTLE_ENDIAN)
        out.put(prefixBytes)
        out.putShort(hCrc.toShort())
        out.put(payload)
        out.putInt(pCrc.toInt())
        return out.array()
    }

    fun decodePacket(raw: ByteArray): DecodedPacket {
        require(raw.size >= OVERHEAD_BYTES) { "packet_too_short" }
        val buf = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN)
        val magic = buf.short.toInt() and 0xFFFF
        require(magic == MAGIC) { "invalid_magic" }

        val frameTypeRaw = buf.get().toInt() and 0xFF
        val frameType = FrameType.fromId(frameTypeRaw)
            ?: throw IllegalArgumentException("unknown_frame_type")
        val flags = buf.get().toInt() and 0xFF
        val frameIndex = buf.get().toInt() and 0xFF
        require(frameIndex <= MAX_FRAME_INDEX) { "invalid_frame_index" }
        val reserved = buf.get().toInt() and 0xFF
        val seqNum = buf.short.toInt() and 0xFFFF
        val chunkIdx = buf.short.toInt() and 0xFFFF
        val totalChunks = buf.short.toInt() and 0xFFFF
        val payloadLen = buf.short.toInt() and 0xFFFF
        val expectedHCrc = buf.short.toInt() and 0xFFFF

        val actualHCrc = crc16Ccitt(raw, 0, 14)
        require(actualHCrc == expectedHCrc) { "header_crc_mismatch" }
        require(totalChunks > 0 && chunkIdx < totalChunks) { "invalid_chunk_bounds" }
        require(raw.size == OVERHEAD_BYTES + payloadLen) { "length_mismatch" }

        val payload = raw.copyOfRange(HEADER_BYTES, HEADER_BYTES + payloadLen)
        val trailerBuf = ByteBuffer.wrap(raw, HEADER_BYTES + payloadLen, TRAILER_BYTES)
            .order(ByteOrder.LITTLE_ENDIAN)
        val expectedPCrc = trailerBuf.int.toLong() and 0xFFFFFFFFL
        val actualPCrc = crc32Ieee(payload)
        require(actualPCrc == expectedPCrc) { "payload_crc_mismatch" }

        return DecodedPacket(
            header = PacketHeader(
                magic = magic,
                frameType = frameType,
                flags = flags,
                frameIndex = frameIndex,
                reserved = reserved,
                seqNum = seqNum,
                chunkIdx = chunkIdx,
                totalChunks = totalChunks,
                payloadLen = payloadLen,
                headerCrc16 = expectedHCrc
            ),
            payload = payload,
            payloadCrc32 = expectedPCrc
        )
    }

    fun fragmentPayload(
        frameType: FrameType,
        flags: Int,
        startSeqNum: Int,
        payload: ByteArray,
        maxPayload: Int = DEFAULT_MAX_PAYLOAD,
        frameIndex: Int = 0
    ): Pair<List<ByteArray>, Int> {
        val clampedMax = maxPayload.coerceIn(1, L2CAP_MAX_PAYLOAD)
        if (payload.isEmpty()) {
            val pkt = encodePacket(
                frameType = frameType,
                flags = flags or FLAG_SINGLE_CHUNK,
                seqNum = startSeqNum and 0xFFFF,
                chunkIdx = 0,
                totalChunks = 1,
                payload = ByteArray(0),
                frameIndex = frameIndex
            )
            return Pair(listOf(pkt), (startSeqNum + 1) and 0xFFFF)
        }

        val totalChunks = (payload.size + clampedMax - 1) / clampedMax
        val packets = ArrayList<ByteArray>(totalChunks)
        var seq = startSeqNum and 0xFFFF
        for (idx in 0 until totalChunks) {
            val start = idx * clampedMax
            val end = minOf(start + clampedMax, payload.size)
            var pktFlags = flags
            if (idx == 0) pktFlags = pktFlags or FLAG_FIRST_CHUNK
            if (idx + 1 == totalChunks) pktFlags = pktFlags or FLAG_LAST_CHUNK
            packets.add(
                encodePacket(
                    frameType = frameType,
                    flags = pktFlags,
                    seqNum = seq,
                    chunkIdx = idx,
                    totalChunks = totalChunks,
                    payload = payload.copyOfRange(start, end),
                    frameIndex = frameIndex
                )
            )
            seq = (seq + 1) and 0xFFFF
        }
        return Pair(packets, seq)
    }
}

/**
 * 4-Bit IMA ADPCM Smart Pipe Codec (`GAD1` container <-> 16-bit mono RIFF WAV / PCM).
 * Matches `encode_ima_adpcm` / `decode_ima_adpcm` in `backend/ble_framing.py` and
 * `encodeImaAdpcm` / `decodeImaAdpcm` in `firmware/src/ble_framing.cpp`.
 */
object ImaAdpcmCodec {
    const val GAD1_HEADER_BYTES: Int = 16
    private const val LEGACY_GAD1_HEADER_BYTES: Int = 12
    private val GAD1_MAGIC = byteArrayOf('G'.code.toByte(), 'A'.code.toByte(), 'D'.code.toByte(), '1'.code.toByte())

    data class AdpcmEncodeResult(
        val adpcmBytes: ByteArray,
        val initialPredictor: Int,
        val initialStepIndex: Int,
        val finalPredictor: Int,
        val finalStepIndex: Int
    )

    data class Gad1EncodeResult(
        val gad1Bytes: ByteArray,
        val finalPredictor: Int,
        val finalStepIndex: Int
    )

    private val INDEX_TABLE = intArrayOf(
        -1, -1, -1, -1, 2, 4, 6, 8,
        -1, -1, -1, -1, 2, 4, 6, 8
    )

    private val STEP_TABLE = intArrayOf(
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

    fun isGad1Container(data: ByteArray): Boolean {
        return data.size >= LEGACY_GAD1_HEADER_BYTES &&
            data[0] == GAD1_MAGIC[0] &&
            data[1] == GAD1_MAGIC[1] &&
            data[2] == GAD1_MAGIC[2] &&
            data[3] == GAD1_MAGIC[3]
    }

    fun encodePcm16WithState(
        pcmBytes: ByteArray,
        initialPredictor: Int? = null,
        initialStepIndex: Int = 0
    ): AdpcmEncodeResult {
        val numSamples = pcmBytes.size / 2
        if (numSamples <= 0) {
            val initP = (initialPredictor ?: 0).coerceIn(-32768, 32767)
            val initIdx = initialStepIndex.coerceIn(0, 88)
            return AdpcmEncodeResult(ByteArray(0), initP, initIdx, initP, initIdx)
        }
        val inBuf = ByteBuffer.wrap(pcmBytes, 0, numSamples * 2).order(ByteOrder.LITTLE_ENDIAN)
        val firstSample = inBuf.getShort(0).toInt()
        val initP = (initialPredictor ?: firstSample).coerceIn(-32768, 32767)
        val initIdx = initialStepIndex.coerceIn(0, 88)

        val out = ByteArray((numSamples + 1) / 2)
        var predictor = initP
        var stepIndex = initIdx

        fun encodeSample(sample: Int): Int {
            val step = STEP_TABLE[stepIndex]
            var diff = sample - predictor
            var nibble = 0
            if (diff < 0) {
                nibble = 8
                diff = -diff
            }
            if (diff >= step) {
                nibble = nibble or 4
                diff -= step
            }
            if (diff >= (step shr 1)) {
                nibble = nibble or 2
                diff -= (step shr 1)
            }
            if (diff >= (step shr 2)) {
                nibble = nibble or 1
            }

            var vpdiff = step shr 3
            if ((nibble and 4) != 0) vpdiff += step
            if ((nibble and 2) != 0) vpdiff += (step shr 1)
            if ((nibble and 1) != 0) vpdiff += (step shr 2)

            predictor = if ((nibble and 8) != 0) predictor - vpdiff else predictor + vpdiff
            predictor = predictor.coerceIn(-32768, 32767)
            stepIndex = (stepIndex + INDEX_TABLE[nibble and 0x0F]).coerceIn(0, 88)
            return nibble and 0x0F
        }

        var i = 0
        while (i < numSamples) {
            val lo = encodeSample(inBuf.short.toInt())
            val hi = if (i + 1 < numSamples) encodeSample(inBuf.short.toInt()) else 0
            out[i / 2] = ((lo and 0x0F) or ((hi and 0x0F) shl 4)).toByte()
            i += 2
        }
        return AdpcmEncodeResult(out, initP, initIdx, predictor, stepIndex)
    }

    fun encodePcm16(
        pcmBytes: ByteArray,
        initialPredictor: Int = 0,
        initialStepIndex: Int = 0
    ): ByteArray {
        return encodePcm16WithState(pcmBytes, initialPredictor, initialStepIndex).adpcmBytes
    }

    fun decodeToPcm16(
        adpcmBytes: ByteArray,
        numSamples: Int = adpcmBytes.size * 2,
        initialPredictor: Int = 0,
        initialStepIndex: Int = 0
    ): ByteArray {
        if (adpcmBytes.isEmpty() || numSamples <= 0) return ByteArray(0)
        val target = minOf(numSamples, adpcmBytes.size * 2)
        val outBuf = ByteBuffer.allocate(target * 2).order(ByteOrder.LITTLE_ENDIAN)
        var predictor = initialPredictor.coerceIn(-32768, 32767)
        var stepIndex = initialStepIndex.coerceIn(0, 88)
        var written = 0

        fun decodeNibble(nibble: Int): Short {
            val step = STEP_TABLE[stepIndex]
            var vpdiff = step shr 3
            if ((nibble and 4) != 0) vpdiff += step
            if ((nibble and 2) != 0) vpdiff += (step shr 1)
            if ((nibble and 1) != 0) vpdiff += (step shr 2)

            predictor = if ((nibble and 8) != 0) predictor - vpdiff else predictor + vpdiff
            predictor = predictor.coerceIn(-32768, 32767)
            stepIndex = (stepIndex + INDEX_TABLE[nibble and 0x0F]).coerceIn(0, 88)
            return predictor.toShort()
        }

        for (b in adpcmBytes) {
            if (written >= target) break
            val unsigned = b.toInt() and 0xFF
            outBuf.putShort(decodeNibble(unsigned and 0x0F))
            written++
            if (written >= target) break
            outBuf.putShort(decodeNibble((unsigned shr 4) and 0x0F))
            written++
        }
        return outBuf.array()
    }

    fun encodePcmToGad1WithState(
        pcmBytes: ByteArray,
        sampleRate: Int = 24000,
        initialPredictor: Int? = null,
        initialStepIndex: Int = 0
    ): Gad1EncodeResult {
        val numSamples = pcmBytes.size / 2
        val res = encodePcm16WithState(pcmBytes, initialPredictor, initialStepIndex)
        val out = ByteBuffer.allocate(GAD1_HEADER_BYTES + res.adpcmBytes.size).order(ByteOrder.LITTLE_ENDIAN)
        out.put(GAD1_MAGIC)
        out.putInt(sampleRate)
        out.putInt(numSamples)
        out.putShort(res.initialPredictor.toShort())
        out.put(res.initialStepIndex.toByte())
        out.put(0.toByte())
        out.put(res.adpcmBytes)
        return Gad1EncodeResult(out.array(), res.finalPredictor, res.finalStepIndex)
    }

    fun encodePcmToGad1(
        pcmBytes: ByteArray,
        sampleRate: Int = 24000,
        initialPredictor: Int? = null,
        initialStepIndex: Int = 0
    ): ByteArray {
        return encodePcmToGad1WithState(pcmBytes, sampleRate, initialPredictor, initialStepIndex).gad1Bytes
    }

    fun decodeGad1OrAdpcmToWav(payload: ByteArray, defaultSampleRate: Int = 16000): ByteArray {
        val sampleRate: Int
        val pcmBytes: ByteArray
        if (isGad1Container(payload)) {
            val hdr = ByteBuffer.wrap(payload, 4, 8).order(ByteOrder.LITTLE_ENDIAN)
            val parsedSr = hdr.int
            val numSamples = hdr.int
            sampleRate = if (parsedSr in 4000..96000) parsedSr else defaultSampleRate
            val neededAdpcmBytes = (numSamples + 1) / 2
            val (initPred, initIdx, rawAdpcm) = if (
                payload.size >= GAD1_HEADER_BYTES &&
                (payload.size - GAD1_HEADER_BYTES) >= neededAdpcmBytes
            ) {
                val ext = ByteBuffer.wrap(payload, 12, 4).order(ByteOrder.LITTLE_ENDIAN)
                val p = ext.short.toInt()
                val idx = ext.get().toInt() and 0xFF
                Triple(p, idx, payload.copyOfRange(GAD1_HEADER_BYTES, payload.size))
            } else {
                Triple(0, 0, payload.copyOfRange(LEGACY_GAD1_HEADER_BYTES, payload.size))
            }
            pcmBytes = decodeToPcm16(rawAdpcm, numSamples, initPred, initIdx)
        } else {
            sampleRate = defaultSampleRate
            pcmBytes = decodeToPcm16(payload)
        }
        return wrapPcmInWav(pcmBytes, sampleRate)
    }

    fun wrapPcmInWav(pcmBytes: ByteArray, sampleRate: Int = 16000): ByteArray {
        val out = ByteBuffer.allocate(44 + pcmBytes.size).order(ByteOrder.LITTLE_ENDIAN)
        out.put("RIFF".toByteArray(Charsets.US_ASCII))
        out.putInt(36 + pcmBytes.size)
        out.put("WAVE".toByteArray(Charsets.US_ASCII))
        out.put("fmt ".toByteArray(Charsets.US_ASCII))
        out.putInt(16)
        out.putShort(1) // PCM
        out.putShort(1) // Mono
        out.putInt(sampleRate)
        out.putInt(sampleRate * 2)
        out.putShort(2) // BlockAlign
        out.putShort(16) // BitsPerSample
        out.put("data".toByteArray(Charsets.US_ASCII))
        out.putInt(pcmBytes.size)
        out.put(pcmBytes)
        return out.array()
    }
}

/**
 * Stateful BLE packet reassembler enforcing strict chunk_idx and seq_num continuity.
 */
class BleReassembler(private val maxPayloadBytes: Int = 128 * 1024) {
    private var activeFrameType: BleFramingCodec.FrameType? = null
    private var activeFlags: Int = 0
    private var activeFrameIndex: Int = 0
    private var expectedTotalChunks: Int = 0
    private var expectedChunkIdx: Int = 0
    private var lastSeqNum: Int? = null
    private var inProgress: Boolean = false
    private val buffer = java.io.ByteArrayOutputStream()

    data class CompletedMessage(
        val frameType: BleFramingCodec.FrameType,
        val flags: Int,
        val frameIndex: Int,
        val seqNum: Int,
        val payload: ByteArray
    )

    fun reset() {
        activeFrameType = null
        activeFlags = 0
        activeFrameIndex = 0
        expectedTotalChunks = 0
        expectedChunkIdx = 0
        inProgress = false
        buffer.reset()
    }

    fun resetSequenceTracking() {
        reset()
        lastSeqNum = null
    }

    fun pushPacket(raw: ByteArray): CompletedMessage? {
        val pkt = BleFramingCodec.decodePacket(raw)
        val h = pkt.header

        lastSeqNum?.let { prevSeq ->
            val expectedSeq = (prevSeq + 1) and 0xFFFF
            if (h.seqNum != expectedSeq) {
                resetSequenceTracking()
                throw IllegalStateException("seq_num_discontinuity")
            }
        }

        val isFirst = ((h.flags and BleFramingCodec.FLAG_FIRST_CHUNK) != 0) || (h.chunkIdx == 0)
        val isLast = ((h.flags and BleFramingCodec.FLAG_LAST_CHUNK) != 0) || (h.chunkIdx + 1 == h.totalChunks)

        if (isFirst) {
            if (inProgress) {
                reset()
                throw IllegalStateException("interrupted_multi_chunk_frame")
            }
            reset()
            activeFrameType = h.frameType
            activeFlags = h.flags
            activeFrameIndex = h.frameIndex
            expectedTotalChunks = h.totalChunks
            expectedChunkIdx = 0
            inProgress = true
        } else if (!inProgress) {
            throw IllegalStateException("missing_first_chunk")
        }

        if (h.chunkIdx != expectedChunkIdx ||
            h.frameType != activeFrameType ||
            h.frameIndex != activeFrameIndex ||
            h.totalChunks != expectedTotalChunks
        ) {
            reset()
            throw IllegalStateException("chunk_sequence_error")
        }

        if (buffer.size() + pkt.payload.size > maxPayloadBytes) {
            reset()
            throw IllegalStateException("payload_exceeds_max_buffer")
        }

        if (pkt.payload.isNotEmpty()) {
            buffer.write(pkt.payload)
        }
        activeFlags = activeFlags or h.flags
        lastSeqNum = h.seqNum
        expectedChunkIdx += 1

        if (isLast || expectedChunkIdx >= expectedTotalChunks) {
            val completed = CompletedMessage(
                frameType = activeFrameType!!,
                flags = activeFlags,
                frameIndex = activeFrameIndex,
                seqNum = h.seqNum,
                payload = buffer.toByteArray()
            )
            reset()
            return completed
        }
        return null
    }
}
