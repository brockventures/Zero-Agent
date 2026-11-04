package voice

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/azylman/mirrormere/internal/chatlog"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/events"
)

type fixedSpeaker struct{ name string }

func (f fixedSpeaker) Identify(context.Context, []byte) (SpeakerMatch, error) {
	return SpeakerMatch{Speaker: f.name, Score: 0.9}, nil
}

func chatTurn(t *testing.T, h *Hub, node string) error {
	t.Helper()
	return h.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), node, "", func(string, any) error { return nil }, EdgeTimings{})
}

type funcTTS func(text string) ([]byte, string, error)

func (f funcTTS) Synthesize(_ context.Context, text string, onChunk func(chunk TTSAudioChunk) error) error {
	data, format, err := f(text)
	if err != nil || len(data) == 0 || onChunk == nil {
		return err
	}
	if format == "" {
		format = "pcm"
	}
	return onChunk(TTSAudioChunk{Data: data, Format: format, SampleRate: 24000, Channels: 1})
}

type recordedEvent struct {
	Event string
	Data  any
}

func eventNames(evs []recordedEvent) []string {
	out := make([]string, len(evs))
	for i, e := range evs {
		out[i] = e.Event
	}
	return out
}

func hasErrorCode(evs []recordedEvent, code string) bool {
	for _, e := range evs {
		if m, ok := e.Data.(map[string]string); ok && e.Event == "error" && m["error"] == code {
			return true
		}
	}
	return false
}

type mockStreamingBrain struct {
	mu                            sync.Mutex
	reply                         string
	statuses                      []string
	sentences                     []BrainAudioChunk
	err                           error
	lastReq                       AskRequest
	askCalled, askStreamingCalled bool
}

func (m *mockStreamingBrain) Ask(_ context.Context, req AskRequest, onStatus func(status string)) (string, error) {
	m.mu.Lock()
	m.askCalled, m.lastReq = true, req
	statuses, reply, err := append([]string(nil), m.statuses...), m.reply, m.err
	m.mu.Unlock()
	for _, s := range statuses {
		if onStatus != nil {
			onStatus(s)
		}
	}
	return reply, err
}

func (m *mockStreamingBrain) AskStreaming(_ context.Context, req AskRequest, onStatus func(status string), onAudio func(chunk BrainAudioChunk)) (string, error) {
	m.mu.Lock()
	m.askStreamingCalled, m.lastReq = true, req
	statuses, sentences := append([]string(nil), m.statuses...), append([]BrainAudioChunk(nil), m.sentences...)
	reply, err := m.reply, m.err
	m.mu.Unlock()
	for _, s := range statuses {
		if onStatus != nil {
			onStatus(s)
		}
	}
	for _, c := range sentences {
		if onAudio != nil {
			onAudio(c)
		}
	}
	return reply, err
}

func collectEvents() (func(event string, data any) error, func() []recordedEvent) {
	var mu sync.Mutex
	var evs []recordedEvent
	sink := func(event string, data any) error {
		mu.Lock()
		evs = append(evs, recordedEvent{Event: event, Data: data})
		mu.Unlock()
		return nil
	}
	get := func() []recordedEvent {
		mu.Lock()
		defer mu.Unlock()
		return append([]recordedEvent(nil), evs...)
	}
	return sink, get
}

func extractAudioChunkEvents(t *testing.T, evs []recordedEvent) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, e := range evs {
		if e.Event == "audio_chunk" {
			out = append(out, e.Data.(map[string]any))
		}
	}
	return out
}

type recordingStateSink struct {
	mu     sync.Mutex
	states []events.VoiceStateData
}

func (r *recordingStateSink) SetVoiceState(s *events.VoiceStateData) {
	r.mu.Lock()
	r.states = append(r.states, *s)
	r.mu.Unlock()
}

type mockStreamingTTS struct {
	mu          sync.Mutex
	streamingFn func(ctx context.Context, text string, onChunk func(TTSAudioChunk) error) error
	streamCalls []string
}

func (m *mockStreamingTTS) Synthesize(ctx context.Context, text string, onChunk func(chunk TTSAudioChunk) error) error {
	m.mu.Lock()
	m.streamCalls = append(m.streamCalls, text)
	fn := m.streamingFn
	m.mu.Unlock()
	if fn != nil {
		return fn(ctx, text, onChunk)
	}
	chunk := TTSAudioChunk{Data: make([]byte, 2400), Format: "pcm", SampleRate: 24000, Channels: 1}
	if err := onChunk(chunk); err != nil {
		return err
	}
	return onChunk(chunk)
}

func mkStreamBrain(reply string, err error, sentences ...string) *mockStreamingBrain {
	chunks := make([]BrainAudioChunk, 0, len(sentences))
	for _, s := range sentences {
		chunks = append(chunks, BrainAudioChunk{Text: s})
	}
	return &mockStreamingBrain{reply: reply, err: err, sentences: chunks}
}

func mkBrain(reply string, err error) *mockBrain { return &mockBrain{reply: reply, err: err} }
func mkTTS(audio []byte, format string, err error) *mockTTS {
	return &mockTTS{audio: audio, format: format, err: err}
}

func mkStreamTTS(fn func(context.Context, string, func(TTSAudioChunk) error) error) *mockStreamingTTS {
	return &mockStreamingTTS{streamingFn: fn}
}

func TestDefaultBrainClient_AskStreaming(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, contentType, body  string
		statusCode               int
		emptyURL, collectAudio   bool
		wantReply                string
		wantErr                  bool
		wantStatuses, wantChunks []string
	}{
		{"wire_format", "text/event-stream", "event: status\ndata: {\"status\":\"thinking\"}\n\nevent: sentence\ndata: {\"text\":\"It's sunny.\"}\n\nevent: sentence\ndata: {\"text\":\"Bring sunglasses.\"}\n\nevent: reply\ndata: {\"reply\":\"It's sunny. Bring sunglasses.\"}\n\nevent: done\ndata: {}\n\n", 0, false, true, "It's sunny. Bring sunglasses.", false, []string{"thinking"}, []string{"It's sunny.", "Bring sunglasses."}},
		{"whitespace_sentence_ignored", "text/event-stream", "event: sentence\ndata: {\"text\":\"   \"}\n\nevent: sentence\ndata: {\"text\":\"Valid sentence.\"}\n\nevent: reply\ndata: {\"reply\":\"Valid sentence.\"}\n\nevent: done\ndata: {}\n\n", 0, false, true, "Valid sentence.", false, nil, []string{"Valid sentence."}},
		{"plain_ask_stream", "text/event-stream", "event: reply\ndata: {\"reply\":\"plain answer\"}\n\nevent: done\ndata: {}\n\n", 0, false, true, "plain answer", false, nil, nil},
		{"empty_url", "", "", 0, true, false, "I heard: hi", false, nil, nil},
		{"json_response", "application/json", `{"reply":"json answer"}`, 0, false, false, "json answer", false, nil, nil},
		{"http_error_502", "", "nope", http.StatusBadGateway, false, false, "", true, nil, nil},
		{"malformed_data_nil_audio", "text/event-stream", "event: status\ndata: not-json\n\nevent: sentence\ndata: {\"text\":\"A.\"}\n\nevent: reply\ndata: {\"reply\":\"  A.  \"}\n\n", 0, false, false, "A.", false, nil, nil},
		{"malformed_data_with_audio", "text/event-stream", "event: status\ndata: not-json\n\nevent: sentence\ndata: {\"text\":\"A.\"}\n\nevent: reply\ndata: {\"reply\":\"  A.  \"}\n\n", 0, false, true, "A.", false, nil, []string{"A."}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			url := ""
			if !tc.emptyURL {
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					if tc.statusCode != 0 {
						w.WriteHeader(tc.statusCode)
					} else if tc.contentType != "" {
						w.Header().Set("Content-Type", tc.contentType)
					}
					_, _ = w.Write([]byte(tc.body))
				}))
				defer srv.Close()
				url = srv.URL
			}
			b := NewDefaultBrainClient(url, 5).(*DefaultBrainClient)
			var statuses, gotChunks []string
			var onAudio func(BrainAudioChunk)
			if tc.collectAudio {
				onAudio = func(c BrainAudioChunk) { gotChunks = append(gotChunks, c.Text) }
			}
			reply, err := b.AskStreaming(context.Background(), AskRequest{Prompt: "hi", SessionID: "s1"}, func(s string) { statuses = append(statuses, s) }, onAudio)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr %v", err, tc.wantErr)
			}
			if !tc.wantErr && (reply != tc.wantReply || (len(tc.wantStatuses) > 0 && !reflect.DeepEqual(statuses, tc.wantStatuses)) || !reflect.DeepEqual(gotChunks, tc.wantChunks)) {
				t.Errorf("got reply=%q statuses=%v chunks=%v; want %q %v %v", reply, statuses, gotChunks, tc.wantReply, tc.wantStatuses, tc.wantChunks)
			}
		})
	}
}

func TestHub_Interact_StreamingScenarios(t *testing.T) {
	t.Parallel()
	pcmRemTTS := mkStreamTTS(func(_ context.Context, text string, onChunk func(TTSAudioChunk) error) error {
		size := 3000
		if text == "Three." {
			size = 2000
		}
		return onChunk(TTSAudioChunk{Data: make([]byte, size), Format: "pcm", SampleRate: 24000, Channels: 1})
	})
	shortRemTTS := mkStreamTTS(func(_ context.Context, _ string, onChunk func(TTSAudioChunk) error) error {
		return onChunk(TTSAudioChunk{Data: make([]byte, 2000), Format: "pcm", SampleRate: 24000, Channels: 1})
	})
	fallbackTTS := funcTTS(func(text string) ([]byte, string, error) {
		if text == "One. Two." {
			return []byte("full"), "pcm", nil
		}
		return nil, "", errors.New("sentence synth failed")
	})
	mp3TTS := mkStreamTTS(func(_ context.Context, _ string, onChunk func(TTSAudioChunk) error) error {
		return onChunk(TTSAudioChunk{Data: []byte("mp3-frame"), Format: "mp3", SampleRate: 44100, Channels: 2})
	})
	rateChangeTTS := mkStreamTTS(func(_ context.Context, text string, onChunk func(TTSAudioChunk) error) error {
		if err := onChunk(TTSAudioChunk{Data: make([]byte, 1000), Format: "pcm", SampleRate: 24000, Channels: 1}); err != nil {
			return err
		}
		if text == "Sentence two." || text == "hello there" {
			return onChunk(TTSAudioChunk{Data: make([]byte, 600), Format: "pcm", SampleRate: 22050, Channels: 1})
		}
		return nil
	})
	dropTTS := mkStreamTTS(func(_ context.Context, _ string, _ func(TTSAudioChunk) error) error {
		return errors.New("streaming connection dropped")
	})

	tests := []struct {
		name                              string
		brain                             BrainClient
		tts                               TTSClient
		noTTSClient, ignoreSinkAudioChunk bool
		wantErr                           error
		wantErrorCode                     string
		wantChunks                        int
		wantNoChunks, wantFinal           bool
		wantReplyEvent                    string
		wantRemainderBytes                int
	}{
		{"sentence_synthesized_via_tts", mkStreamBrain("It's sunny. Bring sunglasses.", nil, "It's sunny.", "Bring sunglasses."), mkTTS([]byte("synthesized-pcm"), "pcm", nil), false, false, nil, "", 2, false, true, "", 0},
		{"pcm_buffering_and_remainder_flush", mkStreamBrain("Streaming PCM.", nil, "One.", "Two.", "Three."), pcmRemTTS, false, false, nil, "", 3, false, true, "", 3200},
		{"streaming_brain_remote_tts_streaming_client", mkStreamBrain("Sentence one. Sentence two.", nil, "Sentence one.", "Sentence two."), &mockStreamingTTS{}, false, false, nil, "", 3, false, true, "", 0},
		{"non_streaming_brain_streaming_tts", mkBrain("Full non-streaming reply.", nil), &mockStreamingTTS{}, false, false, nil, "", 2, false, true, "", 0},
		{"non_streaming_pcm_remainder", mkBrain("Short reply.", nil), shortRemTTS, false, false, nil, "", 2, false, true, "", 2000},
		{"empty_whitespace_reply", mkBrain("   ", nil), &mockStreamingTTS{}, false, false, nil, "", 0, true, false, "", 0},
		{"text_only_without_local_tts", mkStreamBrain("Hi there.", nil, "Hi there."), nil, true, false, nil, "", 0, true, false, "Hi there.", 0},
		{"sentence_fallback_to_full_reply_tts_success", mkStreamBrain("One. Two.", nil, "One.", "Two."), fallbackTTS, false, false, nil, "", 2, false, true, "", 0},
		{"sentence_fallback_to_full_reply_tts_failure", mkStreamBrain("One.", nil, "One."), mkTTS(nil, "", errors.New("tts down")), false, false, nil, "tts_error", 0, false, false, "", 0},
		{"no_sentence_tts_fails", mkStreamBrain("Plain.", nil), mkTTS(nil, "", errors.New("tts down")), false, false, nil, "tts_error", 0, false, false, "", 0},
		{"brain_error", &mockStreamingBrain{err: errors.New("boom")}, mkTTS([]byte("a"), "pcm", nil), false, false, ErrBrainFailed, "brain_error", 0, false, false, "", 0},
		{"streaming_mp3_fails_fast", mkStreamBrain("MP3 sentence.", nil, "MP3 sentence."), mp3TTS, false, false, ErrUnsupportedAudioFormat, "", 0, true, false, "", 0},
		{"non_streaming_mp3_fails_fast", mkBrain("Streaming MP3 reply.", nil), mp3TTS, false, false, ErrUnsupportedAudioFormat, "tts_error", 0, true, false, "", 0},
		{"streaming_rate_change_fails_fast", mkStreamBrain("Sentence one. Sentence two.", nil, "Sentence one.", "Sentence two."), rateChangeTTS, false, false, ErrMixedPCMFormat, "", 0, false, false, "", 0},
		{"non_streaming_rate_change_fails_fast", mkBrain("hello there", nil), rateChangeTTS, false, false, ErrMixedPCMFormat, "tts_error", 0, false, false, "", 0},
		{"streaming_tts_fallback_on_error", mkBrain("Fallback test.", nil), dropTTS, false, false, nil, "tts_error", 0, false, false, "", 0},
		{"audio_chunk_sink_error_tolerance", mkStreamBrain("One.", nil, "One."), mkTTS([]byte("x"), "pcm", nil), false, true, nil, "", 0, false, false, "", 0},
		{"streaming_brain_remote_tts_plain_client", mkStreamBrain("First sentence. Second sentence.", nil, "First sentence.", "Second sentence."), mkTTS(makeValidWAV(1600), "pcm", nil), false, false, nil, "", 3, false, true, "", 0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			opts := []HubOption{WithSTTClient(&mockSTT{text: "hello"}), WithBrainClient(tc.brain)}
			if tc.tts != nil {
				opts = append(opts, WithTTSClient(tc.tts))
			}
			h := NewHub(&config.VoiceHubConfig{Enabled: true}, NewCoordinator(nil), opts...)
			if tc.noTTSClient {
				h.tts = nil
			}
			sink, getEvents := collectEvents()
			if tc.ignoreSinkAudioChunk {
				origSink := sink
				sink = func(event string, data any) error {
					_ = origSink(event, data)
					if event == "audio_chunk" {
						return errors.New("dock went away")
					}
					return nil
				}
			}
			err := h.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "kiosk", "s1", sink, EdgeTimings{})
			if tc.wantErr != nil && !errors.Is(err, tc.wantErr) {
				t.Fatalf("want error %v, got %v", tc.wantErr, err)
			} else if tc.wantErr == nil && err != nil && tc.wantErrorCode == "" {
				t.Fatalf("unexpected error: %v", err)
			}
			evs := getEvents()
			if tc.wantErrorCode != "" && !hasErrorCode(evs, tc.wantErrorCode) {
				t.Errorf("expected error code %q in events: %v", tc.wantErrorCode, eventNames(evs))
			}
			if tc.wantReplyEvent != "" && !strings.Contains(strings.Join(eventNames(evs), ","), "reply") {
				t.Errorf("expected reply in events: %v", eventNames(evs))
			}
			chunks := extractAudioChunkEvents(t, evs)
			if (tc.wantNoChunks && len(chunks) != 0) || (tc.wantChunks > 0 && len(chunks) != tc.wantChunks) {
				t.Fatalf("chunks count: got %d, want %d (noChunks=%v)", len(chunks), tc.wantChunks, tc.wantNoChunks)
			}
			if tc.wantFinal && len(chunks) > 0 {
				if final, ok := chunks[len(chunks)-1]["is_final"].(bool); !ok || !final {
					t.Errorf("expected last chunk is_final: true, got %v", chunks[len(chunks)-1]["is_final"])
				}
			}
			if tc.wantRemainderBytes > 0 && len(chunks) >= 2 {
				dec, _ := base64.StdEncoding.DecodeString(chunks[len(chunks)-2]["data"].(string))
				if len(dec) != tc.wantRemainderBytes {
					t.Errorf("remainder bytes: want %d, got %d", tc.wantRemainderBytes, len(dec))
				}
			}
		})
	}
}

func TestHub_Interact_StreamingBrain_KioskCaption(t *testing.T) {
	t.Parallel()
	rec := &recordingStateSink{}
	coord := NewCoordinator(nil, rec)
	brain := mkStreamBrain("It's sunny. High of 72.", nil, "It's sunny.", "High of 72.")
	h := NewHub(&config.VoiceHubConfig{Enabled: true}, coord, WithSTTClient(&mockSTT{text: "weather?"}), WithBrainClient(brain), WithTTSClient(mkTTS([]byte("local"), "pcm", nil)))
	sink, _ := collectEvents()
	if err := h.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "kiosk-kitchen", "sess-1", sink, EdgeTimings{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	rec.mu.Lock()
	var speaking []string
	for _, s := range rec.states {
		if s.State == StateSpeaking && s.Reply != nil {
			speaking = append(speaking, *s.Reply)
		}
	}
	want := []string{"It's sunny.", "It's sunny. High of 72.", "It's sunny. High of 72."}
	if !reflect.DeepEqual(speaking, want) || rec.states[len(rec.states)-1].State != StateSpeaking {
		t.Errorf("speaking captions: want %q, got %q", want, speaking)
	}
	rec.mu.Unlock()
	coord.Reset()
	rec.mu.Lock()
	defer rec.mu.Unlock()
	if last := rec.states[len(rec.states)-1]; last.State != StateIdle {
		t.Errorf("expected final state idle after reset, got %s", last.State)
	}
}

func TestHub_ChatLog(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, sttText                                 string
		brain                                         BrainClient
		tts                                           TTSClient
		speaker                                       SpeakerIdentifier
		withChatLog, wantErr                          bool
		wantHumanText, wantHumanAuthor, wantAgentText string
		wantChanges                                   int32
		wantMsgs                                      int
	}{
		{"records_turn_in_chatlog", "Turn off office lights", mkBrain("Turned off office lights.", nil), mkTTS([]byte("pcm"), "pcm", nil), fixedSpeaker{name: "sam"}, true, false, "Turn off office lights", "sam", "Turned off office lights.", 2, 2},
		{"brain_failure_keeps_user_turn", "hello", mkBrain("", ErrBrainFailed), nil, nil, true, true, "hello", "", "", 0, 1},
		{"without_chatlog_still_works", "hi", mkBrain("yo", nil), nil, nil, false, false, "", "", "", 0, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			hubEvents := events.NewHub(events.HubConfig{}, nil, nil)
			defer hubEvents.Close()
			store := chatlog.NewStore()
			var changes atomic.Int32
			opts := []HubOption{WithSTTClient(&mockSTT{text: tc.sttText}), WithBrainClient(tc.brain)}
			if tc.tts != nil {
				opts = append(opts, WithTTSClient(tc.tts))
			}
			if tc.speaker != nil {
				opts = append(opts, WithSpeakerIdentifier(tc.speaker))
			}
			if tc.withChatLog {
				opts = append(opts, WithChatLog(store, func() { changes.Add(1) }))
			}
			h := NewHub(&config.VoiceHubConfig{Enabled: true}, NewCoordinator(hubEvents), opts...)
			if err := chatTurn(t, h, "touch-kiosk-kitchen"); (err != nil) != tc.wantErr {
				t.Fatalf("interact err = %v, wantErr %v", err, tc.wantErr)
			}
			if !tc.withChatLog {
				return
			}
			msgs := store.Messages(func(k string) bool { return k == chatlog.NodeKey("touch-kiosk-kitchen") })
			if len(msgs) != tc.wantMsgs {
				t.Fatalf("messages count: want %d, got %d (%+v)", tc.wantMsgs, len(msgs), msgs)
			}
			if tc.wantMsgs >= 1 && (msgs[0].Role != chatlog.RoleHuman || msgs[0].Text != tc.wantHumanText || msgs[0].Author != tc.wantHumanAuthor || msgs[0].At.IsZero()) {
				t.Errorf("user message mismatch: %+v", msgs[0])
			}
			if tc.wantMsgs >= 2 && (msgs[1].Role != chatlog.RoleAgent || msgs[1].Text != tc.wantAgentText || msgs[1].At.Before(msgs[0].At)) {
				t.Errorf("agent message mismatch: %+v", msgs[1])
			}
			if tc.wantChanges > 0 && changes.Load() != tc.wantChanges {
				t.Errorf("changes count: want %d, got %d", tc.wantChanges, changes.Load())
			}
			if other := store.Messages(func(k string) bool { return k == chatlog.NodeKey("eink-display-livingroom") }); len(other) != 0 {
				t.Errorf("other node must not see this conversation: %+v", other)
			}
		})
	}
}

func riffChunk(id string, body []byte) []byte {
	out := append([]byte(id), 0, 0, 0, 0)
	binary.LittleEndian.PutUint32(out[4:], uint32(len(body)))
	out = append(out, body...)
	if len(body)%2 == 1 {
		out = append(out, 0)
	}
	return out
}

func fmtBody(tag, ch uint16, rate uint32, bits uint16, extra int) []byte {
	b := make([]byte, 16+extra)
	binary.LittleEndian.PutUint16(b[0:], tag)
	binary.LittleEndian.PutUint16(b[2:], ch)
	binary.LittleEndian.PutUint32(b[4:], rate)
	binary.LittleEndian.PutUint32(b[8:], rate*uint32(ch)*uint32(bits)/8)
	binary.LittleEndian.PutUint16(b[12:], ch*bits/8)
	binary.LittleEndian.PutUint16(b[14:], bits)
	return b
}

func buildWAV(chunks ...[]byte) []byte {
	out := []byte("RIFF\x00\x00\x00\x00WAVE")
	for _, c := range chunks {
		out = append(out, c...)
	}
	binary.LittleEndian.PutUint32(out[4:], uint32(len(out)-8))
	return out
}

func TestDefaultTTSClient_WAVAndPCMParsing(t *testing.T) {
	t.Parallel()
	pcm := make([]byte, 4800*3)
	for i := range pcm {
		pcm[i] = byte(i/4800) + 1
	}
	extFmt := fmtBody(0xFFFE, 1, 22050, 16, 24)
	binary.LittleEndian.PutUint16(extFmt[16:], 22)
	binary.LittleEndian.PutUint16(extFmt[24:], 1)
	streamedWAV := func(size uint32) []byte {
		hdr := append([]byte("data"), 0, 0, 0, 0)
		binary.LittleEndian.PutUint32(hdr[4:], size)
		return append(append(buildWAV(riffChunk("fmt ", fmtBody(1, 2, 16000, 16, 0))), hdr...), pcm...)
	}

	tests := []struct {
		name, contentType string
		hdr               map[string]string
		body              []byte
		wantRate, wantCh  int
	}{
		{"LIST chunk before data", "audio/wav", nil, buildWAV(riffChunk("fmt ", fmtBody(1, 1, 22050, 16, 0)), riffChunk("LIST", []byte("INFOISFT\x05\x00\x00\x00Lavf\x00")), riffChunk("data", pcm)), 22050, 1},
		{"fmt size 18", "audio/wav", nil, buildWAV(riffChunk("fmt ", fmtBody(1, 1, 22050, 16, 2)), riffChunk("data", pcm)), 22050, 1},
		{"WAVE_FORMAT_EXTENSIBLE", "audio/wav", nil, buildWAV(riffChunk("fmt ", extFmt), riffChunk("data", pcm)), 22050, 1},
		{"streamed data size 0xFFFFFFFF", "audio/wav", nil, streamedWAV(0xFFFFFFFF), 16000, 2},
		{"streamed data size 0", "audio/wav", nil, streamedWAV(0), 16000, 2},
		{"known data size excludes trailing", "audio/wav", nil, buildWAV(riffChunk("fmt ", fmtBody(1, 1, 24000, 16, 0)), riffChunk("data", pcm), riffChunk("LIST", []byte("INFOtrailing!"))), 24000, 1},
		{"raw pcm content-type params", "audio/pcm;rate=22050;channels=2", nil, pcm, 22050, 2},
		{"raw pcm X-Sample-Rate header", "audio/pcm", map[string]string{"X-Sample-Rate": "16000"}, pcm, 16000, 1},
		{"raw pcm default rate", "audio/pcm", nil, pcm, 24000, 1},
		{"mp3 unsupported", "audio/mpeg", nil, bytes.Repeat([]byte("mp3frame"), 2000), 0, 0},
		{"wav float unsupported", "audio/wav", nil, buildWAV(riffChunk("fmt ", fmtBody(3, 1, 44100, 32, 0)), riffChunk("data", make([]byte, 12000))), 0, 0},
		{"wav 8-bit unsupported", "audio/wav", nil, buildWAV(riffChunk("fmt ", fmtBody(1, 1, 8000, 8, 0)), riffChunk("data", make([]byte, 4000))), 0, 0},
		{"unknown content type", "application/octet-stream", nil, make([]byte, 100), 0, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", tc.contentType)
				for k, v := range tc.hdr {
					w.Header().Set(k, v)
				}
				f := w.(http.Flusher)
				for i := 0; i < len(tc.body); i += 1000 {
					_, _ = w.Write(tc.body[i:min(i+1000, len(tc.body))])
					f.Flush()
				}
			}))
			defer ts.Close()
			var chunks []TTSAudioChunk
			err := NewDefaultTTSClient(ts.URL, "m", "v", 5).Synthesize(context.Background(), "hello", func(c TTSAudioChunk) error {
				chunks = append(chunks, c)
				return nil
			})
			if tc.wantRate == 0 {
				if !errors.Is(err, ErrUnsupportedAudioFormat) || len(chunks) != 0 {
					t.Fatalf("expected ErrUnsupportedAudioFormat and 0 chunks, got err=%v chunks=%d", err, len(chunks))
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			var gotPCM []byte
			for _, c := range chunks {
				gotPCM = append(gotPCM, c.Data...)
				if c.Format != "pcm" || c.SampleRate != tc.wantRate || c.Channels != tc.wantCh {
					t.Errorf("bad chunk: format=%s rate=%d ch=%d", c.Format, c.SampleRate, c.Channels)
				}
			}
			if !bytes.Equal(gotPCM, pcm) {
				t.Fatalf("PCM mismatch or buffer aliasing corruption: got %d bytes, want %d", len(gotPCM), len(pcm))
			}
		})
	}
}
