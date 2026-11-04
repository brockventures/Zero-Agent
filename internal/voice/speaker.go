package voice

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/azylman/mirrormere/internal/config"
)

// SpeakerEmbedder turns an utterance into a voice embedding.
type SpeakerEmbedder interface {
	// Embed returns the embedding vector and the model identifier that produced it.
	Embed(ctx context.Context, wavData []byte) ([]float64, string, error)
}

// SpeakerMatch is the result of speaker identification. Speaker is a hint for
// personalisation, not authentication.
type SpeakerMatch struct {
	// Speaker is the matched enrolled id, or "" when nobody cleared the threshold.
	Speaker string
	// Score is the best cosine similarity seen (0 when matching did not run).
	Score float64
}

// SpeakerIdentifier resolves the speaker of an utterance. An error is never
// fatal to the voice turn.
type SpeakerIdentifier interface {
	Identify(ctx context.Context, wavData []byte) (SpeakerMatch, error)
}

// Fingerprint is one enrolled speaker voiceprint centroid.
type Fingerprint = config.SpeakerFingerprint

// checkVector rejects vectors that cannot be compared: non-finite values or zero magnitude.
func checkVector(v []float64) error {
	var norm float64
	for _, x := range v {
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return errors.New("embedding contains NaN or Inf")
		}
		norm += x * x
	}
	if norm == 0 {
		return errors.New("embedding has zero magnitude")
	}
	return nil
}

// CosineSimilarity returns the cosine similarity of two equal-length vectors.
func CosineSimilarity(a, b []float64) (float64, error) {
	if len(a) != len(b) {
		return 0, fmt.Errorf("dimension mismatch: %d vs %d", len(a), len(b))
	}
	var dot, na, nb float64
	for i := range a {
		dot += a[i] * b[i]
		na += a[i] * a[i]
		nb += b[i] * b[i]
	}
	if na == 0 || nb == 0 {
		return 0, errors.New("zero-length vector")
	}
	return dot / (math.Sqrt(na) * math.Sqrt(nb)), nil
}

// MatchSpeaker returns the enrolled speaker most similar to the embedding, if
// that similarity is at least threshold. It returns "" when nobody qualifies.
// The utterance embedding must be finite and non-zero; a fingerprint that
// cannot be compared is skipped so it cannot block matching for everyone else.
func MatchSpeaker(embedding []float64, speakers []Fingerprint, threshold float64) (string, float64, error) {
	if err := checkVector(embedding); err != nil {
		return "", 0, fmt.Errorf("utterance: %w", err)
	}
	bestID := ""
	best := math.Inf(-1)
	for _, sp := range speakers {
		sim, err := CosineSimilarity(embedding, sp.Embedding)
		if err != nil || math.IsNaN(sim) {
			slog.Warn("skipping uncomparable fingerprint", "speaker", sp.ID, "error", err)
			continue
		}
		if sim > best {
			best, bestID = sim, sp.ID
		}
	}
	if bestID == "" {
		return "", 0, errors.New("no enrolled fingerprint is comparable with the utterance embedding")
	}
	if best < threshold {
		return "", best, nil
	}
	return bestID, best, nil
}

// FingerprintIdentifier matches utterances against inline enrolled speaker fingerprints.
type FingerprintIdentifier struct {
	embedder  SpeakerEmbedder
	model     string
	speakers  []Fingerprint
	threshold float64
}

// NewFingerprintIdentifier constructs an identifier from an embedder, model, speakers, and threshold.
func NewFingerprintIdentifier(embedder SpeakerEmbedder, model string, speakers []Fingerprint, threshold float64) *FingerprintIdentifier {
	return &FingerprintIdentifier{
		embedder:  embedder,
		model:     strings.TrimSpace(model),
		speakers:  speakers,
		threshold: threshold,
	}
}

// NewSpeakerIdentifierFromConfig returns nil when speaker matching is not configured.
func NewSpeakerIdentifierFromConfig(cfg *config.SpeakerIDConfig) SpeakerIdentifier {
	if !cfg.IsEnabled() {
		return nil
	}
	return NewFingerprintIdentifier(
		NewDefaultEmbedClient(cfg.EmbedURL, cfg.GetTimeoutSeconds()),
		cfg.Model,
		cfg.Speakers,
		cfg.GetThreshold(),
	)
}

// ReloadSpeakerID updates the active speaker identifier when configuration is reloaded on SIGHUP.
func (h *Hub) ReloadSpeakerID(cfg *config.VoiceHubConfig) {
	if h == nil {
		return
	}
	var sid SpeakerIdentifier
	if cfg != nil && cfg.Enabled {
		sid = NewSpeakerIdentifierFromConfig(cfg.SpeakerID)
	}
	h.mu.Lock()
	h.speaker = sid
	h.mu.Unlock()
}

// Identify embeds the utterance and matches it against enrolled fingerprints.
func (f *FingerprintIdentifier) Identify(ctx context.Context, wavData []byte) (SpeakerMatch, error) {
	if len(f.speakers) == 0 {
		return SpeakerMatch{}, nil
	}
	emb, model, err := f.embedder.Embed(ctx, wavData)
	if err != nil {
		return SpeakerMatch{}, fmt.Errorf("embed: %w", err)
	}
	if f.model != "" && model != "" && f.model != model {
		return SpeakerMatch{}, fmt.Errorf("model mismatch: embedding model %q does not match fingerprint model %q", model, f.model)
	}
	id, score, err := MatchSpeaker(emb, f.speakers, f.threshold)
	if err != nil {
		return SpeakerMatch{}, err
	}
	return SpeakerMatch{Speaker: id, Score: score}, nil
}

// DefaultEmbedClient calls an HTTP speaker-embedding endpoint.
//
// Request:  POST <url>, Content-Type: audio/wav, body = raw WAV bytes.
// Response: 200 {"embedding": [float...], "model": "<model id>"}.
type DefaultEmbedClient struct {
	url        string
	httpClient *http.Client
}

// NewDefaultEmbedClient constructs an embed client with the given timeout.
func NewDefaultEmbedClient(url string, timeoutSec int) *DefaultEmbedClient {
	return &DefaultEmbedClient{
		url:        strings.TrimSpace(url),
		httpClient: &http.Client{Timeout: time.Duration(timeoutSec) * time.Second},
	}
}

// Embed posts the WAV to the embed endpoint.
func (c *DefaultEmbedClient) Embed(ctx context.Context, wavData []byte) ([]float64, string, error) {
	if c.url == "" {
		return nil, "", errors.New("embed url is empty")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(wavData))
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Content-Type", "audio/wav")
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()

	if err := readHTTPError(resp, "embed"); err != nil {
		return nil, "", err
	}
	var out struct {
		Embedding []float64 `json:"embedding"`
		Model     string    `json:"model"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, "", err
	}
	if len(out.Embedding) == 0 {
		return nil, "", errors.New("embed response has an empty embedding")
	}
	return out.Embedding, out.Model, nil
}

func readHTTPError(resp *http.Response, prefix string) error {
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1024))
	if readErr != nil {
		body = []byte("unknown error")
	}
	return fmt.Errorf("%s http error %d: %s", prefix, resp.StatusCode, string(body))
}
