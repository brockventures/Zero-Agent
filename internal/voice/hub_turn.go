package voice

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/chatlog"
)

func (h *Hub) recordEdgeTimings(nodeID string, timings EdgeTimings) {
	if timings.WakeEvalSec > 0 {
		h.metrics.RecordStageDuration(nodeID, "wake_eval", "success", timings.WakeEvalSec)
	}
	if timings.UtteranceSpeechSec > 0 {
		h.metrics.RecordStageDuration(nodeID, "utterance_speech", "success", timings.UtteranceSpeechSec)
	}
	if timings.UtteranceSilenceSec > 0 {
		h.metrics.RecordStageDuration(nodeID, "utterance_silence", "success", timings.UtteranceSilenceSec)
	}
}

func (h *Hub) transcribeWithSpeaker(
	ctx context.Context,
	wavData []byte,
	spk SpeakerIdentifier,
	nodeID string,
	safeSink SSEEventSink,
) (string, SpeakerMatch, error) {
	if h.stt == nil {
		h.metrics.RecordStageDuration(nodeID, "stt", "error", 0)
		h.metrics.RecordError(nodeID, "stt", "stt_error")
		return "", SpeakerMatch{}, ErrSTTFailed
	}
	speakerCh := make(chan SpeakerMatch, 1)
	idCtx, idCancel := context.WithCancel(ctx)
	defer idCancel()
	if spk != nil {
		go func() {
			m, idErr := spk.Identify(idCtx, wavData)
			if idErr != nil {
				slog.Warn("speaker identification failed", "error", idErr)
				m = SpeakerMatch{}
			}
			speakerCh <- m
		}()
	} else {
		speakerCh <- SpeakerMatch{}
	}
	sttStart := time.Now()
	transcript, err := h.stt.Transcribe(ctx, wavData)
	sttDuration := time.Since(sttStart).Seconds()
	var match SpeakerMatch
	if err == nil {
		select {
		case match = <-speakerCh:
		case <-time.After(speakerGrace):
			idCancel()
			slog.Warn("speaker identification exceeded grace period after STT; continuing without speaker", "grace", speakerGrace)
		}
	}
	if err != nil {
		h.metrics.RecordStageDuration(nodeID, "stt", "error", sttDuration)
		h.metrics.RecordError(nodeID, "stt", "stt_error")
		if h.coord != nil {
			h.coord.Transition(StateError, nil, nil, nil, nil)
		}
		if sinkErr := safeSink("error", map[string]string{"error": "stt_error", "message": err.Error()}); sinkErr != nil {
			return "", SpeakerMatch{}, sinkErr
		}
		return "", SpeakerMatch{}, fmt.Errorf("%w: %w", ErrSTTFailed, err)
	}
	h.metrics.RecordStageDuration(nodeID, "stt", "success", sttDuration)
	pcmBytes := max(len(wavData)-44, 0)
	audioSec := float64(pcmBytes) / (16000.0 * 2.0)
	if audioSec > 0.001 {
		rtf := sttDuration / audioSec
		h.metrics.RecordSTTRTF(nodeID, "whisper", rtf)
	}
	return transcript, match, nil
}

func startThinkingHeartbeat(doneCtx context.Context, interval time.Duration, safeSink SSEEventSink) func() {
	stopCh := make(chan struct{})
	var stopOnce sync.Once
	stop := func() {
		stopOnce.Do(func() {
			close(stopCh)
		})
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		tickerStart := time.Now()
		for {
			select {
			case <-stopCh:
				return
			case <-doneCtx.Done():
				return
			case tick := <-ticker.C:
				select {
				case <-stopCh:
					return
				default:
				}
				elapsedSec := int(math.Round(tick.Sub(tickerStart).Seconds()))
				if elapsedSec <= 0 {
					elapsedSec = 1
				}
				if sinkErr := safeSink("thinking", map[string]int{"elapsed_seconds": elapsedSec}); sinkErr != nil {
					slog.Debug("thinking heartbeat sink error", "error", sinkErr)
					return
				}
			}
		}
	}()
	return stop
}

func (h *Hub) synthesizeStreamedSentence(
	ctx context.Context,
	text string,
	streamer *pcmChunkStreamer,
	cancelStream func(),
) (float64, error) {
	if h.tts == nil {
		slog.Warn("no TTS client configured; dropping streamed sentence", "text", text)
		return 0, nil
	}
	var streamedTTSAny bool
	ttsTimeout := h.ttsTimeout
	if ttsTimeout <= 0 {
		ttsTimeout = 10 * time.Second
	}
	ttsCtx, stopTTS := newInactivityContext(ctx, ttsTimeout)
	defer stopTTS()
	t0 := time.Now()
	var pushErr error
	ttsErr := h.tts.Synthesize(ttsCtx, text, func(tc TTSAudioChunk) error {
		if len(tc.Data) == 0 {
			return nil
		}
		ttsCtx.Reset()
		streamedTTSAny = true
		if err := streamer.push(tc, text); err != nil {
			pushErr = err
			slog.Error("TTS stream chunk rejected", "error", pushErr)
			cancelStream()
			return pushErr
		}
		return nil
	})
	dur := time.Since(t0).Seconds()
	if pushErr != nil {
		return dur, pushErr
	}
	if errors.Is(ttsErr, ErrUnsupportedAudioFormat) {
		cancelStream()
		return dur, fmt.Errorf("%w (sentence %q)", ttsErr, text)
	}
	if ttsErr != nil || !streamedTTSAny {
		slog.Warn("TTS synthesis for streamed sentence failed; dropping sentence", "text", text, "error", ttsErr)
		return dur, nil
	}
	slog.Debug("synthesized streamed sentence via TTS", "text", text)
	return dur, nil
}

type streamAudioCollector struct {
	hub              *Hub
	cancelStream     func()
	streamer         *pcmChunkStreamer
	transcript       string
	engine           string
	caption          strings.Builder
	streamChars      int
	totalTTSDuration float64
	streamedAny      bool
	streamErr        error
}

func (c *streamAudioCollector) onAudio(ctx context.Context, chunk BrainAudioChunk, stopThinking func(), streamCtx *inactivityContext) {
	stopThinking()
	if chunk.Text != "" {
		streamCtx.Reset()
	}
	if c.streamErr != nil || strings.TrimSpace(chunk.Text) == "" {
		return
	}
	c.streamedAny = true
	c.streamChars += len(chunk.Text)

	if c.caption.Len() > 0 && chunk.Text != "" {
		c.caption.WriteString(" ")
	}
	c.caption.WriteString(chunk.Text)
	if c.hub.coord != nil {
		captionSoFar := c.caption.String()
		c.hub.coord.Transition(StateSpeaking, &c.transcript, &captionSoFar, &c.engine, nil)
	}

	dur, err := c.hub.synthesizeStreamedSentence(ctx, chunk.Text, c.streamer, c.cancelStream)
	c.totalTTSDuration += dur
	if err != nil {
		c.streamErr = err
	}
}

func (h *Hub) finalizeStreamerPlayback(streamer *pcmChunkStreamer, nodeID, engine string, totalTTSDuration float64, streamChars int) error {
	if finishErr := streamer.finish(); finishErr != nil {
		return finishErr
	}
	h.metrics.RecordStageDuration(nodeID, "tts", "success", totalTTSDuration)
	if totalTTSDuration > 0.001 && streamChars > 0 {
		cps := float64(streamChars) / totalTTSDuration
		h.metrics.RecordTTSCPS(nodeID, engine, cps)
	}
	return nil
}

func (h *Hub) emitStreamedReply(
	ctx context.Context,
	reply string,
	transcript string,
	nodeID string,
	engine string,
	streamer *pcmChunkStreamer,
	totalTTSDuration float64,
	streamChars int,
	safeSink SSEEventSink,
) (string, error) {
	if h.coord != nil {
		h.coord.Transition(StateSpeaking, &transcript, &reply, &engine, nil)
	}
	finalStatus := "success"
	if streamer.anySent || len(streamer.pcmBuf) > 0 {
		if err := h.finalizeStreamerPlayback(streamer, nodeID, engine, totalTTSDuration, streamChars); err != nil {
			return "error", err
		}
	} else if h.tts != nil {
		status, ttsErr := h.synthesizeAndEmitReply(ctx, reply, nodeID, engine, &transcript, safeSink)
		if ttsErr != nil {
			return "error", ttsErr
		}
		if status == "error" {
			finalStatus = "error"
		}
	}
	if err := safeSink("reply", map[string]string{"reply": reply, "tts_engine": engine}); err != nil {
		return "error", err
	}
	return finalStatus, nil
}

func (h *Hub) deliberateStreaming(
	ctx context.Context,
	streamingBrain AudioStreamingBrainClient,
	turnBrain BrainClient,
	askReq AskRequest,
	transcript string,
	nodeID string,
	engine string,
	brainTimeout time.Duration,
	heartbeatInterval time.Duration,
	safeSink SSEEventSink,
	onStatus func(status string),
	emitNonStreamingReply func(reply string) error,
) (string, error) {
	streamCtx, stopStream := newInactivityContext(ctx, brainTimeout)
	defer stopStream()

	stopThinking := startThinkingHeartbeat(streamCtx, heartbeatInterval, safeSink)
	defer stopThinking()

	streamOnStatus := func(status string) {
		streamCtx.Reset()
		onStatus(status)
	}

	col := &streamAudioCollector{
		hub:          h,
		cancelStream: stopStream,
		streamer: &pcmChunkStreamer{
			sink:          safeSink,
			ignoreSinkErr: true,
		},
		transcript: transcript,
		engine:     engine,
	}

	brainStart := time.Now()
	reply, err := streamingBrain.AskStreaming(streamCtx, askReq, streamOnStatus, func(chunk BrainAudioChunk) {
		col.onAudio(ctx, chunk, stopThinking, streamCtx)
	})
	brainDuration, recordBrain := brainStageSeconds(turnBrain, time.Since(brainStart))
	stopThinking()
	if col.streamErr != nil {
		err = col.streamErr
	}
	if err != nil {
		return "error", h.recordBrainStageError(ctx, streamCtx, nodeID, brainDuration, err, safeSink)
	}
	if recordBrain {
		h.metrics.RecordStageDuration(nodeID, "brain", "success", brainDuration)
	}
	h.recordChat(nodeID, chatlog.RoleAgent, "", reply)

	if col.streamedAny {
		return h.emitStreamedReply(ctx, reply, transcript, nodeID, engine, col.streamer, col.totalTTSDuration, col.streamChars, safeSink)
	}
	if err := emitNonStreamingReply(reply); err != nil {
		return "error", err
	}
	return "success", nil
}

func (h *Hub) deliberateUnary(
	ctx context.Context,
	turnBrain BrainClient,
	askReq AskRequest,
	nodeID string,
	brainTimeout time.Duration,
	heartbeatInterval time.Duration,
	safeSink SSEEventSink,
	onStatus func(status string),
	emitNonStreamingReply func(reply string) error,
) error {
	brainCtx, stopBrain := newInactivityContext(ctx, brainTimeout)
	defer stopBrain()

	stopThinking := startThinkingHeartbeat(brainCtx, heartbeatInterval, safeSink)
	defer stopThinking()

	unaryOnStatus := func(status string) {
		brainCtx.Reset()
		onStatus(status)
	}

	brainStart := time.Now()
	reply, err := turnBrain.Ask(brainCtx, askReq, unaryOnStatus)
	brainDuration, recordBrain := brainStageSeconds(turnBrain, time.Since(brainStart))
	stopThinking()
	if err != nil {
		return h.recordBrainStageError(ctx, brainCtx, nodeID, brainDuration, err, safeSink)
	}
	if recordBrain {
		h.metrics.RecordStageDuration(nodeID, "brain", "success", brainDuration)
	}
	h.recordChat(nodeID, chatlog.RoleAgent, "", reply)

	return emitNonStreamingReply(reply)
}

func (h *Hub) recordTTSSynthesisError(
	ctx, ttsCtx context.Context,
	ttsErr error,
	nodeID string,
	ttsDuration float64,
	midStream bool,
	safeSink SSEEventSink,
) error {
	if midStream {
		slog.Warn("TTS synthesis failed mid-stream", "node_id", nodeID, "error", ttsErr)
	} else {
		slog.Warn("TTS synthesis failed", "node_id", nodeID, "error", ttsErr)
	}
	h.metrics.RecordStageDuration(nodeID, "tts", "error", ttsDuration)
	errType := "tts_error"
	if isTimeoutError(ttsCtx, ctx, ttsErr) {
		errType = "tts_timeout"
	}
	h.metrics.RecordError(nodeID, "tts", errType)
	if midStream && h.coord != nil {
		h.coord.Transition(StateError, nil, nil, nil, nil)
	}
	if sinkErr := safeSink("error", map[string]string{"error": errType, "message": ttsErr.Error()}); sinkErr != nil {
		return sinkErr
	}
	if midStream {
		return ttsErr
	}
	return nil
}

func parseWAVFmtChunk(r io.Reader, size uint32, info *wavInfo) (isPCM bool, bits int, ok bool) {
	if size < 16 || size > 4096 {
		return false, 0, false
	}
	body := make([]byte, size)
	if _, err := io.ReadFull(r, body); err != nil {
		return false, 0, false
	}
	if size%2 == 1 {
		if _, err := io.CopyN(io.Discard, r, 1); err != nil {
			return false, 0, false
		}
	}
	tag := binary.LittleEndian.Uint16(body[0:2])
	if tag == 0xFFFE && size >= 26 {
		tag = binary.LittleEndian.Uint16(body[24:26])
	}
	isPCM = tag == 1
	info.channels = int(binary.LittleEndian.Uint16(body[2:4]))
	info.sampleRate = int(binary.LittleEndian.Uint32(body[4:8]))
	bits = int(binary.LittleEndian.Uint16(body[14:16]))
	if info.channels <= 0 {
		info.channels = 1
	}
	if info.sampleRate <= 0 {
		info.sampleRate = 24000
	}
	info.haveFmt = true
	return isPCM, bits, true
}
