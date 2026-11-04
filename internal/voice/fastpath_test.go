package voice

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/chatlog"
	"github.com/azylman/mirrormere/internal/config"
)

type recordingTTS struct {
	mu    sync.Mutex
	texts []string
}

func (r *recordingTTS) Synthesize(_ context.Context, text string, onChunk func(TTSAudioChunk) error) error {
	r.mu.Lock()
	r.texts = append(r.texts, text)
	r.mu.Unlock()
	return onChunk(TTSAudioChunk{Data: make([]byte, 9600), Format: "pcm", SampleRate: 24000, Channels: 1})
}

type fastPathHarness struct {
	hub    *Hub
	brain  *mockBrain
	store  *chatlog.Store
	tts    *recordingTTS
	events []recordedEvent
}

func newFastPathHarness(t *testing.T, fp *config.FastPathConfig) *fastPathHarness {
	t.Helper()
	h := &fastPathHarness{brain: &mockBrain{reply: "brain answer"}, store: chatlog.NewStore(), tts: &recordingTTS{}}
	h.hub = NewHub(&config.VoiceHubConfig{Enabled: true, FastPath: fp}, NewCoordinator(nil),
		WithSTTClient(&mockSTT{text: "pause the music"}), WithBrainClient(h.brain),
		WithTTSClient(h.tts), WithSpeakerIdentifier(fixedSpeaker{name: "sam"}), WithChatLog(h.store, nil))
	return h
}

func (h *fastPathHarness) turn() ([]string, error) {
	err := h.hub.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "kitchen-ear", "turn-1", func(ev string, d any) error {
		h.events = append(h.events, recordedEvent{Event: ev, Data: d})
		return nil
	}, EdgeTimings{})
	var chat []string
	for _, m := range h.store.Messages(func(k string) bool { return k == chatlog.NodeKey("kitchen-ear") }) {
		if m.Role == chatlog.RoleAgent {
			chat = append(chat, m.Text)
		}
	}
	return chat, err
}

func sseHandler(events ...string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i+1 < len(events); i += 2 {
			_, _ = w.Write([]byte("event: " + events[i] + "\ndata: " + events[i+1] + "\n\n"))
			w.(http.Flusher).Flush()
		}
	}
}

func statusHandler(code int, ct, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		if ct != "" {
			w.Header().Set("Content-Type", ct)
		}
		w.WriteHeader(code)
		_, _ = w.Write([]byte(body))
	}
}

func TestFastPath_Outcomes(t *testing.T) {
	t.Parallel()
	streamH := func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		if r.Method != http.MethodPost || body["text"] != "pause the music" || body["speaker"] != "sam" || body["node"] != "kitchen-ear" || body["turn_id"] != "turn-1" || body["tts"] != "none" {
			t.Errorf("unexpected req: %+v", body)
		}
		sseHandler("turn", `{"turn_id":"turn-1"}`, "sentence", `{"text":"Paused."}`, "sentence", `{"text":"Anything else?"}`, "reply", `{"reply":"Paused. Anything else?"}`, "done", `{}`)(w, r)
	}
	abortH := func(w http.ResponseWriter, r *http.Request) {
		sseHandler("sentence", `{"text":"Paused."}`)(w, r)
		panic(http.ErrAbortHandler)
	}
	slowH := func(_ http.ResponseWriter, r *http.Request) {
		tm := time.NewTimer(150 * time.Millisecond)
		defer tm.Stop()
		select {
		case <-tm.C:
		case <-r.Context().Done():
		}
	}
	bAns := []string{"brain answer"}
	for _, tc := range []struct {
		name                 string
		handler              http.HandlerFunc
		timeout              time.Duration
		wantBrain            bool
		wantSpoken, wantChat []string
	}{
		{"streamed", streamH, time.Second, false, []string{"Paused.", "Anything else?"}, []string{"Paused. Anything else?"}},
		{"abort", abortH, time.Second, false, []string{"Paused."}, []string{"Paused."}},
		{"reply_only", sseHandler("reply", `{"reply":"It is 3 PM."}`, "done", `{}`), time.Second, false, []string{"It is 3 PM."}, []string{"It is 3 PM."}},
		{"204_no_match", statusHandler(http.StatusNoContent, "", ""), time.Second, true, bAns, bAns},
		{"500_error", statusHandler(http.StatusInternalServerError, "", "boom"), time.Second, true, bAns, bAns},
		{"404_not_found", statusHandler(http.StatusNotFound, "", ""), time.Second, true, bAns, bAns},
		{"not_sse", statusHandler(http.StatusOK, "application/json", `{"reply":"json"}`), time.Second, true, bAns, bAns},
		{"empty_stream", sseHandler("turn", `{}`, "done", `{}`), time.Second, true, bAns, bAns},
		{"malformed_stream", statusHandler(http.StatusOK, "text/event-stream", "event: sentence\ndata: {bad\n\n"), time.Second, true, bAns, bAns},
		{"timeout", slowH, 50 * time.Millisecond, true, bAns, bAns},
		{"connection_error", nil, time.Second, true, bAns, bAns},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := newFastPathHarness(t, nil)
			url := "http://127.0.0.1:1"
			if tc.handler != nil {
				srv := httptest.NewServer(tc.handler)
				defer srv.Close()
				url = srv.URL
			}
			h.hub.fastPath = NewDefaultFastPathClient(url, tc.timeout)
			chat, err := h.turn()
			if err != nil || (h.brain.calledWith != "") != tc.wantBrain || !slices.Equal(h.tts.texts, tc.wantSpoken) || !slices.Equal(chat, tc.wantChat) {
				t.Fatalf("err=%v brain=%q spoken=%v chat=%v", err, h.brain.calledWith, h.tts.texts, chat)
			}
			if names := eventNames(h.events); len(names) == 0 || names[len(names)-1] != "done" || !slices.Contains(names, "reply") || !slices.Contains(names, "audio_chunk") {
				t.Errorf("events: %v", names)
			}
		})
	}
}

type streamingBrainStub struct{ called bool }

func (s *streamingBrainStub) Ask(context.Context, AskRequest, func(string)) (string, error) {
	return "", nil
}

func (s *streamingBrainStub) AskStreaming(_ context.Context, _ AskRequest, _ func(string), onAudio func(BrainAudioChunk)) (string, error) {
	s.called = true
	onAudio(BrainAudioChunk{Text: "From the brain."})
	return "From the brain.", nil
}

type stubFastPath struct{ res FastPathResult }

func (s stubFastPath) Try(context.Context, AskRequest, func(BrainAudioChunk)) FastPathResult {
	return s.res
}

func TestFastPath_ConfigAndBrainIntegration(t *testing.T) {
	t.Parallel()
	for _, cfg := range []*config.FastPathConfig{nil, {URL: "  "}} {
		h := newFastPathHarness(t, cfg)
		if _, err := h.turn(); h.hub.fastPath != nil || err != nil || h.brain.calledWith == "" {
			t.Fatalf("unset fastPath failed: %+v err=%v", cfg, err)
		}
	}
	ms := 250
	h := newFastPathHarness(t, &config.FastPathConfig{URL: "http://127.0.0.1:1/intent", TimeoutMS: &ms})
	c, ok := h.hub.fastPath.(*DefaultFastPathClient)
	if _, err := h.turn(); !ok || c.timeout != 250*time.Millisecond || c.url != "http://127.0.0.1:1/intent" || err != nil || h.brain.calledWith == "" {
		t.Fatalf("configured fastPath failed: %+v err=%v", h.hub.fastPath, err)
	}

	brain, tts := &streamingBrainStub{}, &recordingTTS{}
	hub := NewHub(&config.VoiceHubConfig{Enabled: true}, NewCoordinator(nil),
		WithSTTClient(&mockSTT{text: "tell me a story"}), WithBrainClient(brain), WithTTSClient(tts),
		WithFastPathClient(stubFastPath{res: FastPathResult{Outcome: fastPathMiss, Reason: "no_match"}}))
	if err := hub.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "kitchen-ear", "s", func(string, any) error { return nil }, EdgeTimings{}); err != nil || !brain.called || !slices.Equal(tts.texts, []string{"From the brain."}) {
		t.Fatalf("miss streaming brain: err=%v spoken=%v", err, tts.texts)
	}
	if _, err := (&fastPathBrain{fp: stubFastPath{res: FastPathResult{Outcome: fastPathMiss}}, metrics: DefaultMetrics()}).Ask(context.Background(), AskRequest{}, nil); err == nil {
		t.Error("expected nil brain error on miss")
	}

	d0, ok0 := brainStageSeconds(nil, 2*time.Second)
	d1, ok1 := brainStageSeconds(&fastPathBrain{elapsed: 500 * time.Millisecond}, 2*time.Second)
	d2, ok2 := brainStageSeconds(&fastPathBrain{elapsed: 500 * time.Millisecond}, 100*time.Millisecond)
	_, okHit := brainStageSeconds(&fastPathBrain{handled: true, elapsed: 40 * time.Millisecond}, 50*time.Millisecond)
	if !ok0 || d0 != 2 || !ok1 || d1 != 1.5 || !ok2 || d2 != 0 || okHit {
		t.Fatalf("brainStageSeconds failed: (%v,%v) (%v,%v) (%v,%v) hit=%v", d0, ok0, d1, ok1, d2, ok2, okHit)
	}
}
