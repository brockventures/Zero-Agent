package voice

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
)

type mockEmbedder struct {
	emb   []float64
	model string
	err   error
}

func (m *mockEmbedder) Embed(context.Context, []byte) ([]float64, string, error) {
	return m.emb, m.model, m.err
}

type mockIdentifier struct {
	id    string
	err   error
	block bool
}

func (m *mockIdentifier) Identify(ctx context.Context, _ []byte) (SpeakerMatch, error) {
	if m.block {
		<-ctx.Done()
		return SpeakerMatch{}, ctx.Err()
	}
	return SpeakerMatch{Speaker: m.id, Score: 0.83}, m.err
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, errors.New("read error") }

func TestSpeakerMatchingAndIdentifier(t *testing.T) {
	t.Parallel()
	s1, err1 := CosineSimilarity([]float64{1, 0}, []float64{1, 0})
	s2, err2 := CosineSimilarity([]float64{1, 0}, []float64{0, 1})
	_, err3 := CosineSimilarity([]float64{1}, []float64{1, 0})
	_, err4 := CosineSimilarity([]float64{0, 0}, []float64{1, 0})
	if err1 != nil || s1 != 1 || err2 != nil || s2 != 0 || err3 == nil || err4 == nil {
		t.Fatalf("CosineSimilarity failed: (%v,%v) (%v,%v) %v %v", s1, err1, s2, err2, err3, err4)
	}

	spk := []Fingerprint{{ID: "mike", Embedding: []float64{1, 0, 0}}, {ID: "lauren", Embedding: []float64{0, 1, 0}}}
	mixed := []Fingerprint{{ID: "broken", Embedding: []float64{1, 0}}, spk[0]}
	uncomp := []Fingerprint{{ID: "broken", Embedding: []float64{1, 0}}}
	for _, tc := range []struct {
		emb     []float64
		list    []Fingerprint
		wantID  string
		wantErr bool
	}{
		{[]float64{0.9, 0.1, 0}, spk, "mike", false},
		{[]float64{0.1, 0.95, 0}, spk, "lauren", false},
		{[]float64{0, 0, 1}, spk, "", false},
		{[]float64{1, 0}, spk, "", true},
		{[]float64{1, 0, 0}, mixed, "mike", false},
		{[]float64{1, 0, 0}, uncomp, "", true},
		{[]float64{math.NaN(), 0, 0}, spk, "", true},
		{[]float64{0, 0, 0}, spk, "", true},
	} {
		if id, sim, err := MatchSpeaker(tc.emb, tc.list, 0.7); (err != nil) != tc.wantErr || id != tc.wantID || (id != "" && sim < 0.7) {
			t.Fatalf("id=%q sim=%v err=%v", id, sim, err)
		}
	}

	enrolled := []Fingerprint{{ID: "mike", Embedding: []float64{1, 0}}, {ID: "lauren", Embedding: []float64{0, 1}}}
	for _, tc := range []struct {
		wantSpeaker string
		emb         SpeakerEmbedder
		model       string
		speakers    []Fingerprint
		wantErr     bool
	}{
		{"lauren", &mockEmbedder{emb: []float64{0.2, 0.98}, model: "ecapa"}, "ecapa", enrolled, false},
		{"", &mockEmbedder{emb: []float64{0, 1}, model: "wavlm"}, "ecapa", enrolled, true},
		{"", &mockEmbedder{err: errors.New("down")}, "ecapa", enrolled, true},
		{"", &mockEmbedder{err: errors.New("unused")}, "ecapa", nil, false},
		{"", &mockEmbedder{emb: []float64{0, 0}}, "ecapa", enrolled, true},
	} {
		if m, err := NewFingerprintIdentifier(tc.emb, tc.model, tc.speakers, 0.7).Identify(context.Background(), nil); (err != nil) != tc.wantErr || m.Speaker != tc.wantSpeaker {
			t.Fatalf("got %q err=%v", m.Speaker, err)
		}
	}

	validCfg := &config.SpeakerIDConfig{EmbedURL: "http://x", Model: "ecapa", Speakers: enrolled}
	var nilHub *Hub
	nilHub.ReloadSpeakerID(&config.VoiceHubConfig{Enabled: true, SpeakerID: validCfg})
	hub := NewHub(&config.VoiceHubConfig{Enabled: true}, nil)
	if NewSpeakerIdentifierFromConfig(nil) != nil || NewSpeakerIdentifierFromConfig(&config.SpeakerIDConfig{EmbedURL: "http://x"}) != nil ||
		NewSpeakerIdentifierFromConfig(validCfg) == nil || NewHub(&config.VoiceHubConfig{Enabled: true, SpeakerID: validCfg}, nil).speaker == nil ||
		hub.speaker != nil {
		t.Fatal("speaker config wiring failed")
	}
	hub.ReloadSpeakerID(&config.VoiceHubConfig{Enabled: true, SpeakerID: validCfg})
	if hub.speaker == nil {
		t.Fatal("expected speaker identifier after ReloadSpeakerID")
	}
	hub.ReloadSpeakerID(nil)
	if hub.speaker != nil {
		t.Fatal("expected nil speaker identifier after ReloadSpeakerID(nil)")
	}
}

func TestDefaultEmbedClientAndHubIntegration(t *testing.T) {
	t.Parallel()
	var brainBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/500":
			http.Error(w, "boom", http.StatusInternalServerError)
		case "/empty":
			_, _ = w.Write([]byte(`{"embedding":[]}`))
		case "/bad":
			_, _ = w.Write([]byte("not json"))
		case "/brain":
			_ = json.NewDecoder(r.Body).Decode(&brainBody)
			_, _ = w.Write([]byte(`{"reply":"ok"}`))
		default:
			if b, _ := io.ReadAll(r.Body); r.Header.Get("Content-Type") != "audio/wav" || string(b) != "RIFFwav" {
				http.Error(w, "bad req", http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"embedding":[0.5,0.25],"model":"ecapa"}`))
		}
	}))
	defer srv.Close()

	if emb, model, err := NewDefaultEmbedClient(srv.URL, 1).Embed(context.Background(), []byte("RIFFwav")); err != nil || model != "ecapa" || len(emb) != 2 {
		t.Fatalf("emb=%v model=%q err=%v", emb, model, err)
	}
	for _, badURL := range []string{srv.URL + "/500", srv.URL + "/empty", srv.URL + "/bad", "  ", "http://127.0.0.1:1", "http://bad host"} {
		if _, _, err := NewDefaultEmbedClient(badURL, 1).Embed(context.Background(), nil); err == nil {
			t.Fatalf("expected error for %q", badURL)
		}
	}
	if err := readHTTPError(&http.Response{StatusCode: http.StatusBadGateway, Body: io.NopCloser(errReader{})}, "test"); err == nil || !strings.Contains(err.Error(), "unknown error") {
		t.Fatalf("expected unknown error, got %v", err)
	}

	for _, tc := range []struct {
		stt, spkID, wantSpk       string
		sttErr, spkErr            error
		block, checkBrain, wantEr bool
	}{
		{"what's on my calendar", "mike", "mike", nil, nil, false, true, false},
		{"hello", "", "", nil, errors.New("down"), false, false, false},
		{"hello", "mike", "", nil, nil, true, false, false},
		{"", "mike", "", errors.New("whisper down"), nil, true, false, true},
	} {
		brainBody = nil
		h := NewHub(&config.VoiceHubConfig{Enabled: true}, nil,
			WithSTTClient(&mockSTT{text: tc.stt, err: tc.sttErr}), WithBrainClient(NewDefaultBrainClient(srv.URL+"/brain", 5)),
			WithTTSClient(&mockTTS{}), WithSpeakerIdentifier(&mockIdentifier{id: tc.spkID, err: tc.spkErr, block: tc.block}))
		events := map[string]any{}
		start := time.Now()
		err := h.Interact(context.Background(), bytes.NewReader(makeValidWAV(1600)), "eink-display-livingroom", "sess-1", func(ev string, d any) error {
			events[ev] = d
			return nil
		}, EdgeTimings{})
		if (err != nil) != tc.wantEr || (tc.wantEr && time.Since(start) > time.Second) {
			t.Fatalf("interact: err=%v elapsed=%v", err, time.Since(start))
		}
		if tr, _ := events["transcript"].(map[string]string); !tc.wantEr && (tr["speaker"] != tc.wantSpk || events["done"] == nil || (tc.checkBrain && (brainBody["speaker"] != "mike" || brainBody["speaker_score"] != 0.83))) {
			t.Fatalf("tr=%+v done=%v bb=%+v", tr, events["done"], brainBody)
		}
	}
}
