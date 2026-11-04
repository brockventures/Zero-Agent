package voice

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/prometheus/client_golang/prometheus"
)

type funcBrain func(context.Context, AskRequest, func(string)) (string, error)

func (f funcBrain) Ask(ctx context.Context, r AskRequest, s func(string)) (string, error) {
	return f(ctx, r, s)
}

type funcStreamingBrain func(context.Context, AskRequest, func(string), func(BrainAudioChunk)) (string, error)

func (f funcStreamingBrain) Ask(ctx context.Context, r AskRequest, s func(string)) (string, error) {
	return f(ctx, r, s, nil)
}

func (f funcStreamingBrain) AskStreaming(ctx context.Context, r AskRequest, s func(string), a func(BrainAudioChunk)) (string, error) {
	return f(ctx, r, s, a)
}

var pcm4800 = TTSAudioChunk{Data: make([]byte, 4800), Format: "pcm", SampleRate: 24000, Channels: 1}

func waitCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func wrapBrain(streaming bool, run funcStreamingBrain) BrainClient {
	if streaming {
		return run
	}
	return funcBrain(func(ctx context.Context, r AskRequest, s func(string)) (string, error) { return run(ctx, r, s, nil) })
}

func simBrain(streaming bool, steps int, delay time.Duration, status, reply string, stall bool) BrainClient {
	return wrapBrain(streaming, func(ctx context.Context, _ AskRequest, onStatus func(string), onAudio func(BrainAudioChunk)) (string, error) {
		if status != "" {
			if !waitCtx(ctx, delay) {
				return "", ctx.Err()
			}
			if onStatus != nil {
				onStatus(status)
			}
		}
		for i := range steps {
			if delay > 0 && !waitCtx(ctx, delay) {
				return "", ctx.Err()
			}
			if onAudio != nil {
				onAudio(BrainAudioChunk{Text: fmt.Sprintf("part-%d", i)})
			}
		}
		if stall {
			<-ctx.Done()
			return "", ctx.Err()
		}
		return reply, nil
	})
}

func heartbeatBrain(streaming bool, ch chan struct{}) BrainClient {
	return wrapBrain(streaming, func(ctx context.Context, _ AskRequest, _ func(string), onAudio func(BrainAudioChunk)) (string, error) {
		select {
		case <-ch:
			if onAudio != nil {
				onAudio(BrainAudioChunk{Text: "chunk"})
			}
			return "done", nil
		case <-ctx.Done():
			return "", ctx.Err()
		}
	})
}

func simTTS(chunks int, delay time.Duration, stall bool, finalErr error) TTSClient {
	return mkStreamTTS(func(ctx context.Context, _ string, onChunk func(TTSAudioChunk) error) error {
		_ = onChunk(TTSAudioChunk{})
		for range chunks {
			if delay > 0 && !waitCtx(ctx, delay) {
				return ctx.Err()
			}
			if err := onChunk(pcm4800); err != nil {
				return err
			}
		}
		if stall {
			<-ctx.Done()
			return ctx.Err()
		}
		return finalErr
	})
}

func TestHub_ThinkingHeartbeat(t *testing.T) {
	t.Parallel()
	tb, tt := 45, 12
	cfg := &config.VoiceHubConfig{Enabled: true, BrainTimeoutSeconds: &tb, TTSTimeoutSeconds: &tt}
	if h0 := NewHub(cfg, nil); h0.brainTimeout != 45*time.Second || h0.ttsTimeout != 12*time.Second || h0.heartbeatInterval != 5*time.Second {
		t.Fatalf("unexpected config defaults: %+v", h0)
	}
	tests := []struct {
		name                                                        string
		streaming, failThinkingSink, wantAudioChunk, wantPosElapsed bool
	}{
		{"unary", false, false, false, true},
		{"streaming", true, false, true, false},
		{"sink_error", false, true, false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			heartbeatSeen := make(chan struct{}, 1)
			sink, getEvents := collectEvents()
			var once sync.Once
			h := NewHub(&config.VoiceHubConfig{Enabled: true}, nil, WithSTTClient(&mockSTT{text: "hi"}), WithBrainClient(heartbeatBrain(tc.streaming, heartbeatSeen)), WithTTSClient(&mockTTS{audio: makeValidWAV(1600), format: "pcm"}), WithThinkingHeartbeatInterval(10*time.Millisecond), WithBrainTimeout(2*time.Second), WithTTSTimeout(5*time.Second))
			err := h.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "node-1", "s-1", func(event string, data any) error {
				_ = sink(event, data)
				if event == "thinking" {
					once.Do(func() { heartbeatSeen <- struct{}{} })
					if tc.failThinkingSink {
						return errors.New("client socket closed")
					}
				}
				return nil
			}, EdgeTimings{})
			if err != nil {
				t.Fatalf("expected nil error, got %v", err)
			}
			var hadThinking, hadAudioChunk, thinkingAfterAudio bool
			for _, ev := range getEvents() {
				switch ev.Event {
				case "thinking":
					hadThinking, thinkingAfterAudio = true, thinkingAfterAudio || hadAudioChunk
					if m, ok := ev.Data.(map[string]int); ok && tc.wantPosElapsed && m["elapsed_seconds"] <= 0 {
						t.Errorf("expected positive elapsed_seconds, got %v", m["elapsed_seconds"])
					}
				case "audio_chunk":
					hadAudioChunk = true
				}
			}
			if !hadThinking || (tc.wantAudioChunk && !hadAudioChunk) || thinkingAfterAudio {
				t.Errorf("heartbeat check failed: hadThinking=%v hadAudio=%v afterAudio=%v", hadThinking, hadAudioChunk, thinkingAfterAudio)
			}
		})
	}
}

func TestHub_BrainTimeouts(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                                       string
		brain                                      BrainClient
		timeout                                    time.Duration
		cancelOnStatus                             bool
		wantErr                                    error
		wantCoordErr, wantMetric, wantTimeoutEvent bool
		wantReply                                  string
	}{
		{"unary_timeout", simBrain(false, 0, 0, "", "", true), 20 * time.Millisecond, false, ErrBrainTimeout, true, true, true, ""},
		{"streaming_timeout", simBrain(true, 0, 0, "", "", true), 20 * time.Millisecond, false, ErrBrainTimeout, false, false, true, ""},
		{"client_cancellation", &mockBrain{statuses: []string{"thinking..."}}, 5 * time.Second, true, nil, false, false, false, ""},
		{"streaming_long_reply", simBrain(true, 30, 4*time.Millisecond, "", "full reply text", false), 80 * time.Millisecond, false, nil, false, false, false, "full reply text"},
		{"streaming_stall", simBrain(true, 1, 0, "", "", true), 20 * time.Millisecond, false, ErrBrainTimeout, false, false, true, ""},
		{"status_reset", simBrain(true, 20, 4*time.Millisecond, "tool executing: web search", "done answer", false), 60 * time.Millisecond, false, nil, false, false, false, "done answer"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			reg := prometheus.NewRegistry()
			coord := NewCoordinator(nil)
			sink, getEvents := collectEvents()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var cancelOnce sync.Once
			h := NewHub(&config.VoiceHubConfig{Enabled: true}, coord, WithSTTClient(&mockSTT{text: "hello"}), WithBrainClient(tc.brain), WithMetrics(NewMetrics(reg)), WithBrainTimeout(tc.timeout))
			err := h.Interact(ctx, bytes.NewReader(makeValidWAV(1600)), "node-brain", "s-brain", func(event string, data any) error {
				if tc.cancelOnStatus && event == "status" {
					cancelOnce.Do(cancel)
				}
				return sink(event, data)
			}, EdgeTimings{})
			if tc.wantErr != nil && (!errors.Is(err, tc.wantErr) || !errors.Is(err, ErrBrainFailed)) {
				t.Fatalf("expected error wrapping %v + ErrBrainFailed, got %v", tc.wantErr, err)
			} else if tc.wantErr == nil && !tc.cancelOnStatus && err != nil {
				t.Fatalf("expected nil error, got %v", err)
			}
			evs := getEvents()
			if hasErrorCode(evs, "brain_timeout") != tc.wantTimeoutEvent || (tc.wantCoordErr && coord.GetState().State != StateError) {
				t.Errorf("timeoutEvent=%v (want %v) state=%q", hasErrorCode(evs, "brain_timeout"), tc.wantTimeoutEvent, coord.GetState().State)
			}
			if tc.wantMetric {
				if val, ok := metricVal(t, reg, "mirrormere_voice_errors_total", lbl("stage", "brain", "error_type", "brain_timeout")); !ok || val != 1 {
					t.Errorf("expected brain_timeout metric 1, got %v (ok=%v)", val, ok)
				}
			}
			var gotReply string
			for _, ev := range evs {
				if m, ok := ev.Data.(map[string]string); ok && ev.Event == "reply" {
					gotReply = m["reply"]
				}
			}
			if tc.wantReply != "" && gotReply != tc.wantReply {
				t.Errorf("expected reply %q, got %q", tc.wantReply, gotReply)
			}
		})
	}
}

func TestHub_TTSTimeouts(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                     string
		tts                      TTSClient
		timeout                  time.Duration
		wantStatus               string
		wantErr                  bool
		wantEventCode            string
		wantCoordErr, wantMetric bool
	}{
		{"initial_timeout_graceful", simTTS(0, 0, true, nil), 20 * time.Millisecond, "error", false, "tts_timeout", false, true},
		{"mid_stream_timeout", simTTS(1, 0, false, context.DeadlineExceeded), 50 * time.Millisecond, "error", true, "tts_timeout", true, false},
		{"mid_stream_generic_error", simTTS(1, 0, false, errors.New("i/o error")), -1 * time.Second, "error", true, "tts_error", false, false},
		{"long_reply_inactivity", simTTS(25, 4*time.Millisecond, false, nil), 80 * time.Millisecond, "success", false, "", false, false},
		{"mid_stream_stall", simTTS(1, 0, true, nil), 20 * time.Millisecond, "error", true, "tts_timeout", false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			reg := prometheus.NewRegistry()
			coord := NewCoordinator(nil)
			sink, getEvents := collectEvents()
			h := NewHub(&config.VoiceHubConfig{Enabled: true}, coord, WithTTSClient(tc.tts), WithMetrics(NewMetrics(reg)), WithTTSTimeout(tc.timeout))
			tr := "transcript"
			status, err := h.synthesizeAndEmitReply(context.Background(), "reply text", "node-tts", "kokoro", &tr, sink)
			if status != tc.wantStatus || (err != nil) != tc.wantErr {
				t.Fatalf("status=%q err=%v, want status=%q wantErr=%v", status, err, tc.wantStatus, tc.wantErr)
			}
			evs := getEvents()
			if (tc.wantEventCode != "" && !hasErrorCode(evs, tc.wantEventCode)) || (tc.wantEventCode == "" && hasErrorCode(evs, "tts_timeout")) || (tc.wantCoordErr && coord.GetState().State != StateError) {
				t.Errorf("events=%v state=%q", eventNames(evs), coord.GetState().State)
			}
			if tc.wantMetric {
				if val, ok := metricVal(t, reg, "mirrormere_voice_errors_total", lbl("stage", "tts", "error_type", "tts_timeout")); !ok || val != 1 {
					t.Errorf("expected tts_timeout metric 1, got %v (ok=%v)", val, ok)
				}
			}
		})
	}
}

func TestInactivityContext(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                      string
		timeout                   time.Duration
		cancelParent, triggerStop bool
		resetDelay                time.Duration
		wantDoneErr               error
	}{
		{"expires_on_timeout", 50 * time.Millisecond, false, false, 0, context.DeadlineExceeded},
		{"reset_extends_deadline", 100 * time.Millisecond, false, false, 55 * time.Millisecond, context.DeadlineExceeded},
		{"parent_cancellation", 200 * time.Millisecond, true, false, 0, context.Canceled},
		{"stop_cancels_context", 500 * time.Millisecond, false, true, 0, context.Canceled},
		{"zero_duration", 0, false, true, 15 * time.Millisecond, context.Canceled},
		{"negative_duration", -1 * time.Second, false, true, 15 * time.Millisecond, context.Canceled},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			parent, cancelParent := context.WithCancel(context.Background())
			defer cancelParent()
			ctx, stop := newInactivityContext(parent, tc.timeout)
			defer stop()
			if tc.resetDelay > 0 {
				if !waitCtx(ctx, tc.resetDelay) {
					t.Fatalf("premature cancel: %v", ctx.Err())
				}
				ctx.Reset()
				if !waitCtx(ctx, tc.resetDelay) {
					t.Fatalf("canceled prematurely after reset: %v", ctx.Err())
				}
			}
			if tc.cancelParent {
				cancelParent()
			}
			if tc.triggerStop {
				stop()
			}
			if waitCtx(ctx, 500*time.Millisecond) || !errors.Is(ctx.Err(), tc.wantDoneErr) {
				t.Fatalf("expected %v, got %v", tc.wantDoneErr, ctx.Err())
			}
			ctx.Reset()
		})
	}
}

func TestIsTimeoutError(t *testing.T) {
	t.Parallel()
	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	deadlineCtx, cancelDeadline := context.WithTimeout(context.Background(), time.Nanosecond)
	t.Cleanup(cancelDeadline)
	<-deadlineCtx.Done()
	validCtx := context.Background()
	tests := []struct {
		name                  string
		err                   error
		timeoutCtx, parentCtx context.Context
		expected              bool
	}{
		{"parent_canceled_returns_false", context.DeadlineExceeded, deadlineCtx, canceledCtx, false},
		{"err_is_deadline_exceeded", context.DeadlineExceeded, nil, validCtx, true},
		{"timeout_ctx_is_deadline_exceeded", errors.New("net down"), deadlineCtx, validCtx, true},
		{"general_error_returns_false", errors.New("err"), validCtx, validCtx, false},
		{"nil_err_returns_false", nil, validCtx, validCtx, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := isTimeoutError(tc.timeoutCtx, tc.parentCtx, tc.err); got != tc.expected {
				t.Errorf("isTimeoutError() = %v, expected %v", got, tc.expected)
			}
		})
	}
}
