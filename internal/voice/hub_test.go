package voice

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/prometheus/client_golang/prometheus"
)

// WithSTTClient overrides the STT client implementation.
func WithSTTClient(stt STTClient) HubOption {
	return func(h *Hub) { h.stt = stt }
}

// WithBrainClient overrides the Brain client implementation.
func WithBrainClient(brain BrainClient) HubOption {
	return func(h *Hub) { h.brain = brain }
}

// WithTTSClient overrides the TTS client implementation.
func WithTTSClient(tts TTSClient) HubOption {
	return func(h *Hub) { h.tts = tts }
}

// WithSpeakerIdentifier overrides the speaker identification implementation.
func WithSpeakerIdentifier(id SpeakerIdentifier) HubOption {
	return func(h *Hub) { h.speaker = id }
}

// WithMetrics overrides the voice metrics collector.
func WithMetrics(m *Metrics) HubOption {
	return func(h *Hub) { h.metrics = m }
}

// WithFastPathClient overrides the deterministic fast-path client (nil disables it).
func WithFastPathClient(fp FastPathClient) HubOption {
	return func(h *Hub) { h.fastPath = fp }
}

// WithBrainTimeout overrides the brain deliberation timeout.
func WithBrainTimeout(d time.Duration) HubOption {
	return func(h *Hub) { h.brainTimeout = d }
}

// WithTTSTimeout overrides the TTS synthesis timeout.
func WithTTSTimeout(d time.Duration) HubOption {
	return func(h *Hub) { h.ttsTimeout = d }
}

// WithThinkingHeartbeatInterval overrides the thinking heartbeat interval.
func WithThinkingHeartbeatInterval(d time.Duration) HubOption {
	return func(h *Hub) { h.heartbeatInterval = d }
}

type mockSTT struct {
	mu         sync.Mutex
	delay      time.Duration
	text       string
	err        error
	calledWith []byte
}

func (m *mockSTT) Transcribe(_ context.Context, wavData []byte) (string, error) {
	m.mu.Lock()
	delay, text, err := m.delay, m.text, m.err
	m.calledWith = wavData
	m.mu.Unlock()
	if delay > 0 {
		<-time.After(delay)
	}
	return text, err
}

type mockBrain struct {
	mu                sync.Mutex
	reply, calledWith string
	statuses          []string
	err               error
	lastReq           AskRequest
}

func (m *mockBrain) Ask(_ context.Context, req AskRequest, onStatus func(status string)) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.calledWith, m.lastReq = req.Prompt, req
	for _, s := range m.statuses {
		if onStatus != nil {
			onStatus(s)
		}
	}
	return m.reply, m.err
}

type mockTTS struct {
	mu                 sync.Mutex
	delay              time.Duration
	audio              []byte
	format, calledWith string
	err                error
	calledTexts        []string
	calls              int
}

func (m *mockTTS) Synthesize(_ context.Context, text string, onChunk func(chunk TTSAudioChunk) error) error {
	m.mu.Lock()
	delay, audio, format, err := m.delay, m.audio, m.format, m.err
	m.calledWith, m.calledTexts, m.calls = text, append(m.calledTexts, text), m.calls+1
	m.mu.Unlock()
	if delay > 0 {
		<-time.After(delay)
	}
	if err != nil || len(audio) == 0 || onChunk == nil {
		return err
	}
	if format == "" {
		format = "pcm"
	}
	return onChunk(TTSAudioChunk{Data: audio, Format: format, SampleRate: 24000, Channels: 1})
}

type mockBlockingSTT struct{ onTranscribe func() }

func (m *mockBlockingSTT) Transcribe(context.Context, []byte) (string, error) {
	m.onTranscribe()
	return "test", nil
}

func makeValidWAV(sampleCount int) []byte {
	buf := make([]byte, 44+sampleCount*2)
	copy(buf[0:4], "RIFF")
	copy(buf[8:16], "WAVEfmt ")
	buf[16], buf[20], buf[22], buf[24], buf[25], buf[34] = 16, 1, 1, 0x80, 0x3E, 16
	copy(buf[36:40], "data")
	return buf
}

func drainMockWyomingRequest(reader *bufio.Reader) {
	for {
		line, rErr := reader.ReadBytes('\n')
		if rErr != nil {
			break
		}
		var hdr struct {
			Type          string `json:"type"`
			DataLength    int    `json:"data_length"`
			PayloadLength int    `json:"payload_length"`
		}
		_ = json.Unmarshal(line, &hdr)
		_, _ = io.CopyN(io.Discard, reader, int64(hdr.DataLength+hdr.PayloadLength))
		if hdr.Type == "audio-stop" {
			break
		}
	}
}

func mkSTT(text string, err error) *mockSTT { return &mockSTT{text: text, err: err} }
func wavRdr(n int) io.Reader                { return bytes.NewReader(makeValidWAV(n)) }

func wavWithZeroRateAndChannels(samples int) []byte {
	b := makeValidWAV(samples)
	copy(b[22:28], make([]byte, 6))
	return b
}

func startWyomingServer(t *testing.T, handler func(conn net.Conn, r *bufio.Reader)) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen failed: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	go func() {
		if conn, err := l.Accept(); err == nil {
			defer conn.Close()
			handler(conn, bufio.NewReader(conn))
		}
	}()
	return "tcp://" + l.Addr().String()
}

func TestHub_Interact_Scenarios(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                             string
		disabled, noCoord                bool
		audio                            io.Reader
		stt                              STTClient
		brain                            BrainClient
		tts                              TTSClient
		sinkErrEvent                     string
		wantErr                          error
		wantErrStr, wantState, wantReply string
		wantEvents                       []string
	}{
		{"success", false, false, wavRdr(1600), mkSTT("Turn off office lights", nil), &mockBrain{reply: "Turned off office lights.", statuses: []string{"⚡ Checking devices...", "⚡ Turning off lights..."}}, mkTTS([]byte("fake-pcm-bytes"), "pcm", nil), "", nil, "", StateSpeaking, "Turned off office lights.", []string{"state", "transcript", "status", "reply", "audio_chunk", "done"}},
		{"disabled", true, false, wavRdr(100), nil, nil, nil, "", ErrHubDisabled, "", "", "", nil},
		{"invalid_audio_nil", false, false, nil, nil, nil, nil, "", ErrInvalidAudio, "", "", "", nil},
		{"invalid_audio_short", false, false, strings.NewReader("RIFFshort"), nil, nil, nil, "", ErrInvalidAudio, "", "", "", nil},
		{"invalid_audio_non_riff", false, false, strings.NewReader(strings.Repeat("NOT_WAV_", 7)), nil, nil, nil, "", ErrInvalidAudio, "", "", "", nil},
		{"stt_failure", false, false, wavRdr(100), mkSTT("", errors.New("whisper offline")), nil, nil, "", ErrSTTFailed, "", StateError, "", []string{"error"}},
		{"brain_failure", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("", errors.New("aerial unreachable")), nil, "", ErrBrainFailed, "", StateError, "", nil},
		{"tts_degradation", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi there", nil), mkTTS(nil, "", errors.New("kokoro timeout")), "", nil, "", StateSpeaking, "hi there", []string{"reply", "error", "done"}},
		{"nil_tts_speaking", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("quiet hours response", nil), nil, "", nil, "", StateSpeaking, "quiet hours response", nil},
		{"nil_stt_coord_error", false, false, wavRdr(100), nil, mkBrain("", nil), mkTTS(nil, "", nil), "", ErrSTTFailed, "", StateError, "", nil},
		{"nil_brain_coord_error", false, false, wavRdr(100), mkSTT("hello", nil), nil, mkTTS(nil, "", nil), "", ErrBrainFailed, "", StateError, "", nil},
		{"sink_state_coord_error", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "state", nil, "sink error on state", StateError, "", nil},
		{"sink_transcript_coord_error", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "transcript", nil, "sink error on transcript", StateError, "", nil},
		{"sink_reply_coord_error", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "reply", nil, "sink error on reply", StateError, "", nil},
		{"sink_done_coord_error", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "done", nil, "sink error on done", StateError, "", nil},
		{"nil_stt_no_coord", false, true, wavRdr(100), nil, mkBrain("", nil), mkTTS(nil, "", nil), "", ErrSTTFailed, "", "", "", nil},
		{"nil_brain_no_coord", false, true, wavRdr(100), mkSTT("hello", nil), nil, mkTTS(nil, "", nil), "", ErrBrainFailed, "", "", "", nil},
		{"sink_state_no_coord", false, true, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "state", nil, "sink error on state", "", "", nil},
		{"sink_transcript_no_coord", false, true, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "transcript", nil, "sink error on transcript", "", "", nil},
		{"sink_reply_no_coord", false, true, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", nil), "reply", nil, "sink error on reply", "", "", nil},
		{"sink_status_non_fatal", false, false, wavRdr(100), mkSTT("hello", nil), &mockBrain{reply: "hi", statuses: []string{"thinking..."}}, mkTTS([]byte("wav"), "pcm", nil), "status", nil, "", "", "", nil},
		{"sink_audio_chunk_aborts", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS([]byte("wav"), "pcm", nil), "audio_chunk", nil, "audio sink error", "", "", nil},
		{"sink_stt_error_propagates", false, false, wavRdr(100), mkSTT("", errors.New("stt failure")), nil, nil, "error", nil, "sink error on stt failure", "", "", nil},
		{"sink_brain_error_propagates", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("", errors.New("brain failure")), nil, "error", nil, "sink error on brain failure", "", "", nil},
		{"sink_tts_error_propagates", false, false, wavRdr(100), mkSTT("hello", nil), mkBrain("hi", nil), mkTTS(nil, "", errors.New("tts failure")), "error", nil, "sink error on tts failure", "", "", nil},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			hubEvents := events.NewHub(events.HubConfig{}, nil, nil)
			defer hubEvents.Close()
			var coord *Coordinator
			if !tc.noCoord {
				coord = NewCoordinator(hubEvents)
			}
			h := NewHub(&config.VoiceHubConfig{Enabled: !tc.disabled}, coord, WithSTTClient(tc.stt), WithBrainClient(tc.brain), WithTTSClient(tc.tts))
			if h.IsEnabled() == tc.disabled {
				t.Fatalf("IsEnabled() = %v, disabled = %v", h.IsEnabled(), tc.disabled)
			}
			sink, getEvents := collectEvents()
			err := h.Interact(context.Background(), tc.audio, "kiosk-kitchen", "", func(event string, data any) error {
				_ = sink(event, data)
				if tc.sinkErrEvent != "" && event == tc.sinkErrEvent {
					if tc.wantErrStr != "" {
						return errors.New(tc.wantErrStr)
					}
					return errors.New("status sink error")
				}
				return nil
			}, EdgeTimings{})
			if (tc.wantErr != nil && !errors.Is(err, tc.wantErr)) || (tc.wantErrStr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErrStr))) || (tc.wantErr == nil && tc.wantErrStr == "" && err != nil) {
				t.Fatalf("err = %v; wantErr=%v wantErrStr=%q", err, tc.wantErr, tc.wantErrStr)
			}
			if coord != nil && tc.wantState != "" && coord.GetState().State != tc.wantState {
				t.Errorf("expected state %s, got %s", tc.wantState, coord.GetState().State)
			}
			if coord != nil && tc.wantReply != "" {
				if coord.GetState().Reply == nil || *coord.GetState().Reply != tc.wantReply {
					t.Errorf("expected reply %q, got %v", tc.wantReply, coord.GetState().Reply)
				}
				if reset := coord.Reset(); reset.State != StateIdle {
					t.Errorf("expected state idle after reset, got %s", reset.State)
				}
			}
			emitted := strings.Join(eventNames(getEvents()), ",")
			for _, exp := range tc.wantEvents {
				if !strings.Contains(emitted, exp) {
					t.Errorf("expected event %q in %q", exp, emitted)
				}
			}
		})
	}
}

func TestHub_Interact_ConcurrencyAndPanic(t *testing.T) {
	t.Parallel()
	t.Run("concurrent_busy", func(t *testing.T) {
		t.Parallel()
		sttStarted, sttBlock, doneCh := make(chan struct{}), make(chan struct{}), make(chan error)
		h := NewHub(&config.VoiceHubConfig{Enabled: true}, nil, WithSTTClient(&mockBlockingSTT{
			onTranscribe: func() {
				close(sttStarted)
				<-sttBlock
			},
		}))
		nopSink := func(string, any) error { return nil }
		go func() { doneCh <- h.Interact(context.Background(), wavRdr(100), "", "", nopSink, EdgeTimings{}) }()
		<-sttStarted
		if err := h.Interact(context.Background(), wavRdr(100), "", "", nopSink, EdgeTimings{}); !errors.Is(err, ErrInteractionBusy) {
			t.Errorf("expected ErrInteractionBusy, got: %v", err)
		}
		close(sttBlock)
		if firstErr := <-doneCh; errors.Is(firstErr, ErrInteractionBusy) {
			t.Errorf("first call should not be busy: %v", firstErr)
		}
	})

	t.Run("panic_recovers_and_transitions_coordinator", func(t *testing.T) {
		t.Parallel()
		coord := NewCoordinator(nil)
		h := NewHub(&config.VoiceHubConfig{Enabled: true}, coord, WithSTTClient(&mockBlockingSTT{
			onTranscribe: func() { panic("unexpected boom") },
		}))
		defer func() {
			if recover() == nil || coord.GetState().State != StateError {
				t.Fatalf("expected panic and StateError, got state %s", coord.GetState().State)
			}
		}()
		_ = h.Interact(context.Background(), wavRdr(100), "", "", func(string, any) error { return nil }, EdgeTimings{})
	})
}

func TestDefaultSTTClient_Wyoming(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name           string
		drain          bool
		resp, wantText string
		wantErr        bool
	}{
		{"wyoming_success", true, fmt.Sprintf("{\"type\":\"transcript\",\"data_length\":%d}\n{\"text\":\"test transcript from wyoming\"}", len(`{"text":"test transcript from wyoming"}`)), "test transcript from wyoming", false},
		{"wyoming_advanced", true, fmt.Sprintf("not-json-line\n{\"type\":\"ping\",\"payload_length\":4}\npong{\"type\":\"transcript\",\"data_length\":%d}\n{\"text\":\"advanced transcript\"}", len(`{"text":"advanced transcript"}`)), "advanced transcript", false},
		{"server_early_close", false, "", "", true},
		{"raw_text_fallback", true, "{\"type\":\"transcript\",\"data_length\":23}\nraw transcript fallback", "raw transcript fallback", false},
		{"truncated_data_length", true, "{\"type\":\"event\",\"data_length\":100}\nshort", "", true},
		{"truncated_payload_length", true, "{\"type\":\"event\",\"payload_length\":100}\nshort", "", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			addr := startWyomingServer(t, func(conn net.Conn, r *bufio.Reader) {
				if tc.drain {
					drainMockWyomingRequest(r)
				}
				if len(tc.resp) > 0 {
					_, _ = conn.Write([]byte(tc.resp))
				}
			})
			txt, err := NewDefaultSTTClient(addr).Transcribe(context.Background(), makeValidWAV(100))
			if (err != nil) != tc.wantErr || (!tc.wantErr && txt != tc.wantText) {
				t.Fatalf("got (%q, %v), want (%q, wantErr=%v)", txt, err, tc.wantText, tc.wantErr)
			}
		})
	}
}

func TestDefaultSTTClient_HTTPAndErrors(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                       string
		status                     int
		respBody, rawURL, wantText string
		wantErr                    bool
		wantErrStr                 string
	}{
		{"http_success", http.StatusOK, `{"text":"test transcript from http"}`, "", "test transcript from http", false, ""},
		{"transcript_fallback", http.StatusOK, `{"transcript":"fallback transcript"}`, "", "fallback transcript", false, ""},
		{"malformed_json", http.StatusOK, `not valid json`, "", "", true, "stt decode error"},
		{"empty_url", 0, "", "", "", true, "stt url is empty"},
		{"tcp_dial_failure", 0, "", "tcp://127.0.0.1:59999", "", true, ""},
		{"http_500", http.StatusInternalServerError, `internal server error`, "", "", true, "500"},
		{"network_failure", 0, "", "http://127.0.0.1:59998/invalid", "", true, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			targetURL := tc.rawURL
			if tc.status > 0 {
				ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(tc.status)
					_, _ = w.Write([]byte(tc.respBody))
				}))
				defer ts.Close()
				targetURL = ts.URL
			}
			txt, err := NewDefaultSTTClient(targetURL).Transcribe(context.Background(), makeValidWAV(100))
			if (err != nil) != tc.wantErr || (tc.wantErr && tc.wantErrStr != "" && !strings.Contains(err.Error(), tc.wantErrStr)) || (!tc.wantErr && txt != tc.wantText) {
				t.Fatalf("got (%q, %v), want (%q, wantErr=%v, errStr=%q)", txt, err, tc.wantText, tc.wantErr, tc.wantErrStr)
			}
		})
	}
}

func TestDefaultBrainClient(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, contentType      string
		status                 int
		rawURL, respBody       string
		req                    AskRequest
		wantReply              string
		wantStatuses           []string
		wantErr                bool
		wantErrStr, wantNodeID string
	}{
		{"sse_success", "text/event-stream", 200, "", "event: status\ndata: {\"status\":\"⚡ executing tool\"}\n\nevent: reply\ndata: {\"reply\":\"The light is on\"}\n\nevent: done\ndata: {}\n\n", AskRequest{Prompt: "turn on light", SessionID: "s1"}, "The light is on", []string{"⚡ executing tool"}, false, "", ""},
		{"json_success", "application/json", 200, "", `{"reply":"JSON reply"}`, AskRequest{Prompt: "hello", SessionID: "s1"}, "JSON reply", nil, false, "", ""},
		{"turn_session_and_node_id", "application/json", 200, "", `{"reply":"ok"}`, AskRequest{Prompt: "hello", NodeID: "touch-kiosk-kitchen"}, "ok", nil, false, "", "touch-kiosk-kitchen"},
		{"empty_url_fallback", "", 0, "", "", AskRequest{Prompt: "test prompt", SessionID: "s1"}, "test prompt", nil, false, "", ""},
		{"status_502", "text/plain", 502, "", "bad gateway", AskRequest{Prompt: "fail", SessionID: "s1"}, "", nil, true, "502", ""},
		{"fallback_text", "application/json", 200, "", `{"text":"text fallback"}`, AskRequest{Prompt: "q", SessionID: "s"}, "text fallback", nil, false, "", ""},
		{"fallback_content", "application/json", 200, "", `{"content":"content fallback"}`, AskRequest{Prompt: "q", SessionID: "s"}, "content fallback", nil, false, "", ""},
		{"invalid_json", "application/json", 200, "", `invalid json`, AskRequest{Prompt: "q", SessionID: "s"}, "", nil, true, "", ""},
		{"sse_without_done", "text/event-stream", 200, "", "event: reply\ndata: {\"reply\":\"streamed without done\"}\n\n", AskRequest{Prompt: "q", SessionID: "s"}, "streamed without done", nil, false, "", ""},
		{"sse_malformed_data", "text/event-stream", 200, "", "event: status\ndata: {not-valid-json}\n\nevent: reply\ndata: {\"reply\":\"recovered\"}\n\nevent: done\ndata: {}\n\n", AskRequest{Prompt: "hi", SessionID: "s1"}, "recovered", nil, false, "", ""},
		{"sse_scanner_overflow", "text/event-stream", 200, "", "data: " + strings.Repeat("a", sseScannerMaxLine+1024) + "\n\n", AskRequest{Prompt: "hi", SessionID: "s1"}, "", nil, true, "", ""},
		{"network_failure", "", 0, "http://127.0.0.1:59998/invalid", "", AskRequest{Prompt: "hi", SessionID: "s1"}, "", nil, true, "", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			targetURL := tc.rawURL
			var mu sync.Mutex
			var lastBody map[string]any
			if tc.status > 0 {
				ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					mu.Lock()
					_ = json.NewDecoder(r.Body).Decode(&lastBody)
					mu.Unlock()
					if tc.contentType != "" {
						w.Header().Set("Content-Type", tc.contentType)
					}
					w.WriteHeader(tc.status)
					_, _ = w.Write([]byte(tc.respBody))
				}))
				defer ts.Close()
				targetURL = ts.URL
			}
			var statuses []string
			reply, err := NewDefaultBrainClient(targetURL, 5).Ask(context.Background(), tc.req, func(s string) { statuses = append(statuses, s) })
			if (err != nil) != tc.wantErr || (tc.wantErr && tc.wantErrStr != "" && !strings.Contains(err.Error(), tc.wantErrStr)) {
				t.Fatalf("err = %v, wantErr %v (%q)", err, tc.wantErr, tc.wantErrStr)
			}
			if !tc.wantErr && (!strings.Contains(reply, tc.wantReply) || (len(tc.wantStatuses) > 0 && !reflect.DeepEqual(statuses, tc.wantStatuses))) {
				t.Fatalf("got reply=%q statuses=%v, want %q %v", reply, statuses, tc.wantReply, tc.wantStatuses)
			}
			if tc.wantNodeID != "" {
				mu.Lock()
				defer mu.Unlock()
				if lastBody["node_id"] != tc.wantNodeID || !strings.HasPrefix(lastBody["session_id"].(string), "turn-") || lastBody["device_name"] != nil {
					t.Errorf("unexpected request body: %+v", lastBody)
				}
			}
		})
	}
}

func TestDefaultTTSClient(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, contentType                      string
		status                                 int
		rawURL                                 string
		body                                   []byte
		text                                   string
		sinkErr, wantErr                       error
		wantErrStr, wantFormat                 string
		wantTotalBytes, wantRate, wantChannels int
	}{
		{"wav_stream", "audio/wav", 200, "", makeValidWAV(4800), "hello world", nil, nil, "", "pcm", 9600, 16000, 1},
		{"pcm_stream", "audio/pcm", 200, "", bytes.Repeat([]byte{0x01, 0x02}, 2400), "hello", nil, nil, "", "pcm", 4800, 0, 0},
		{"mp3_unsupported", "audio/mpeg", 200, "", []byte("fake-mp3"), "hello", nil, ErrUnsupportedAudioFormat, "", "", 0, 0, 0},
		{"empty_url", "", 0, "", nil, "hello", nil, nil, "", "", 0, 0, 0},
		{"empty_text", "", 0, "http://127.0.0.1:9", nil, "   ", nil, nil, "", "", 0, 0, 0},
		{"server_error", "text/plain", 500, "", []byte("server dead"), "hello", nil, nil, "500", "", 0, 0, 0},
		{"wav_sink_error", "audio/wav", 200, "", makeValidWAV(4800), "hello", errors.New("sink abort"), nil, "sink abort", "", 0, 0, 0},
		{"pcm_sink_error", "audio/pcm", 200, "", []byte("header-body-payload"), "hello", errors.New("pcm sink abort"), nil, "pcm sink abort", "", 0, 0, 0},
		{"wav_zero_rate_channels", "audio/wav", 200, "", wavWithZeroRateAndChannels(2400), "hello", nil, nil, "", "pcm", 0, 24000, 1},
		{"network_failure", "", 0, "http://127.0.0.1:59998/invalid", nil, "hi", nil, nil, "error", "", 0, 0, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			targetURL := tc.rawURL
			if tc.status > 0 {
				ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					if tc.contentType != "" {
						w.Header().Set("Content-Type", tc.contentType)
					}
					w.WriteHeader(tc.status)
					_, _ = w.Write(tc.body)
				}))
				defer ts.Close()
				targetURL = ts.URL
			}
			var totalPCM int
			var lastChunk TTSAudioChunk
			err := NewDefaultTTSClient(targetURL, "kokoro", "af_bella", 5).Synthesize(context.Background(), tc.text, func(chunk TTSAudioChunk) error {
				if tc.sinkErr != nil {
					return tc.sinkErr
				}
				lastChunk, totalPCM = chunk, totalPCM+len(chunk.Data)
				return nil
			})
			if (tc.wantErr != nil && !errors.Is(err, tc.wantErr)) || (tc.wantErrStr != "" && (err == nil || (tc.wantErrStr != "error" && !strings.Contains(err.Error(), tc.wantErrStr)))) || (tc.wantErr == nil && tc.wantErrStr == "" && err != nil) {
				t.Fatalf("err = %v; wantErr=%v wantErrStr=%q", err, tc.wantErr, tc.wantErrStr)
			}
			if (tc.wantFormat != "" && lastChunk.Format != tc.wantFormat) || (tc.wantRate > 0 && lastChunk.SampleRate != tc.wantRate) || (tc.wantChannels > 0 && lastChunk.Channels != tc.wantChannels) || (tc.wantTotalBytes > 0 && totalPCM != tc.wantTotalBytes) {
				t.Errorf("chunk=%+v totalPCM=%d; want fmt=%q rate=%d ch=%d total=%d", lastChunk, totalPCM, tc.wantFormat, tc.wantRate, tc.wantChannels, tc.wantTotalBytes)
			}
		})
	}
}

type metricExpectation struct {
	name   string
	labels []string
	minVal float64
}

func mkExp(name string, minVal float64, kv ...string) metricExpectation {
	return metricExpectation{name: name, minVal: minVal, labels: kv}
}
func exps(e ...metricExpectation) []metricExpectation { return e }

func TestHub_MetricsRecording(t *testing.T) {
	t.Parallel()
	const durM, errM, turnM = "mirrormere_voice_stage_duration_seconds", "mirrormere_voice_errors_total", "mirrormere_voice_turns_total"
	tests := []struct {
		name    string
		stt     STTClient
		brain   BrainClient
		tts     TTSClient
		timings EdgeTimings
		wantErr error
		metrics []metricExpectation
	}{
		{"success", &mockSTT{delay: 2 * time.Millisecond, text: "lights on"}, mkBrain("turning on lights", nil), &mockTTS{delay: 2 * time.Millisecond, audio: []byte("tts-audio-bytes"), format: "pcm"}, EdgeTimings{WakeEvalSec: 0.05, UtteranceSpeechSec: 1.0, UtteranceSilenceSec: 0.25}, nil, exps(mkExp(durM, 1, "stage", "wake_eval", "status", "success"), mkExp(durM, 1, "stage", "utterance_speech", "status", "success"), mkExp(durM, 1, "stage", "utterance_silence", "status", "success"), mkExp(durM, 1, "stage", "stt", "status", "success"), mkExp(durM, 1, "stage", "brain", "status", "success"), mkExp(durM, 1, "stage", "tts", "status", "success"), mkExp(turnM, 1, "status", "success"), mkExp("mirrormere_voice_stt_rtf", 0, "engine", "whisper"), mkExp("mirrormere_voice_tts_chars_per_second", 0.001, "voice", "kokoro"))},
		{"stt_failure", mkSTT("", errors.New("whisper down")), nil, nil, EdgeTimings{}, ErrSTTFailed, exps(mkExp(durM, 1, "stage", "stt", "status", "error"), mkExp(errM, 1, "stage", "stt", "error_type", "stt_error"), mkExp(turnM, 1, "status", "error"))},
		{"brain_failure", mkSTT("hello", nil), mkBrain("", errors.New("brain failed")), nil, EdgeTimings{}, ErrBrainFailed, exps(mkExp(durM, 1, "stage", "brain", "status", "error"), mkExp(errM, 1, "stage", "brain", "error_type", "brain_error"), mkExp(turnM, 1, "status", "error"))},
		{"tts_failure", mkSTT("hello", nil), mkBrain("hi there", nil), mkTTS(nil, "", errors.New("tts failed")), EdgeTimings{}, nil, exps(mkExp(durM, 1, "stage", "tts", "status", "error"), mkExp(errM, 1, "stage", "tts", "error_type", "tts_error"), mkExp(turnM, 1, "status", "error"))},
		{"streaming_brain", &mockSTT{delay: 2 * time.Millisecond, text: "what is the weather"}, mkStreamBrain("It's sunny today. Enjoy the warmth.", nil, "It's sunny today.", "Enjoy the warmth."), &mockTTS{delay: 2 * time.Millisecond, audio: makeValidWAV(1600), format: "pcm"}, EdgeTimings{}, nil, exps(mkExp(durM, 1, "stage", "tts", "status", "success"), mkExp("mirrormere_voice_tts_chars_per_second", 0.001, "voice", "kokoro"), mkExp(turnM, 1, "status", "success"))},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			reg := prometheus.NewRegistry()
			h := NewHub(&config.VoiceHubConfig{Enabled: true, TTSModel: "kokoro"}, nil, WithSTTClient(tc.stt), WithBrainClient(tc.brain), WithTTSClient(tc.tts), WithMetrics(NewMetrics(reg)))
			err := h.Interact(context.Background(), wavRdr(16000), "kiosk-kitchen", "sess-1", func(string, any) error { return nil }, tc.timings)
			if (err != nil) != (tc.wantErr != nil) || (tc.wantErr != nil && !errors.Is(err, tc.wantErr)) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			for _, exp := range tc.metrics {
				if val, ok := metricVal(t, reg, exp.name, lbl(exp.labels...)); !ok || val < exp.minVal {
					t.Errorf("metric %s %v: got %f (ok=%v), want >= %f", exp.name, exp.labels, val, ok, exp.minVal)
				}
			}
		})
	}
}
