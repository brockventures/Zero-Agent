package voice

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"
)

// Fast-path outcomes, used for logs and the "fast_path" stage-duration status.
const (
	fastPathHit   = "hit"
	fastPathMiss  = "miss"
	fastPathError = "error"
)

// FastPathResult is what one fast-path attempt produced.
type FastPathResult struct {
	// Handled is true when the endpoint matched and executed the command, so
	// the turn must not also go to the brain.
	Handled bool
	// Outcome is "hit", "miss" or "error".
	Outcome string
	// Reason qualifies miss and error outcomes (and a hit whose stream broke
	// after speech started).
	Reason string
	// Reply is the spoken text for a hit.
	Reply string
}

// FastPathClient asks a deterministic intent endpoint whether it can answer a
// transcript without the brain. Sentence text is delivered to onAudio exactly
// as a streaming brain delivers it.
type FastPathClient interface {
	Try(ctx context.Context, req AskRequest, onAudio func(chunk BrainAudioChunk)) FastPathResult
}

// DefaultFastPathClient implements POST /intent (LAMMAS) over HTTP + SSE.
type DefaultFastPathClient struct {
	url        string
	timeout    time.Duration
	httpClient *http.Client
}

// NewDefaultFastPathClient builds a client; timeout bounds the wait for the
// response headers.
func NewDefaultFastPathClient(url string, timeout time.Duration) *DefaultFastPathClient {
	// The header wait is bounded by the transport, not by cancelling the
	// request context, so a timer can never sever a stream whose headers
	// arrived just in time.
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: timeout}).DialContext,
		ResponseHeaderTimeout: timeout,
		MaxIdleConns:          4,
		IdleConnTimeout:       90 * time.Second,
	}
	return &DefaultFastPathClient{url: strings.TrimSpace(url), timeout: timeout, httpClient: &http.Client{Transport: transport}}
}

// Try posts the transcript. Any outcome other than a 200 event stream is
// reported as a miss or error so the caller falls through to the brain.
func (c *DefaultFastPathClient) Try(ctx context.Context, ask AskRequest, onAudio func(chunk BrainAudioChunk)) FastPathResult {
	body, err := json.Marshal(map[string]string{
		"text":    ask.Prompt,
		"speaker": ask.Speaker,
		"node":    ask.NodeID,
		"turn_id": ask.SessionID,
		"tts":     "none",
	})
	if err != nil {
		return FastPathResult{Outcome: fastPathError, Reason: "marshal: " + err.Error()}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return FastPathResult{Outcome: fastPathError, Reason: "request: " + err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		var netErr net.Error
		if errors.As(err, &netErr) && netErr.Timeout() {
			return FastPathResult{Outcome: fastPathError, Reason: "timeout"}
		}
		return FastPathResult{Outcome: fastPathError, Reason: "connect: " + err.Error()}
	}
	defer resp.Body.Close()
	// Headers arrived in time; the stream itself is bounded by the turn context.

	switch {
	case resp.StatusCode == http.StatusNoContent:
		return FastPathResult{Outcome: fastPathMiss, Reason: "no_match"}
	case resp.StatusCode != http.StatusOK:
		if _, err := io.Copy(io.Discard, io.LimitReader(resp.Body, 1024)); err != nil {
			slog.Debug("voice fast path: draining error body failed", "err", err)
		}
		return FastPathResult{Outcome: fastPathError, Reason: fmt.Sprintf("status_%d", resp.StatusCode)}
	case !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream"):
		return FastPathResult{Outcome: fastPathError, Reason: "not_event_stream"}
	}

	var sentences []string
	spoken := func(chunk BrainAudioChunk) {
		sentences = append(sentences, chunk.Text)
		if onAudio != nil {
			onAudio(chunk)
		}
	}
	reply, streamErr := consumeStreamingSSE(resp.Body, nil, spoken)
	reply = strings.TrimSpace(reply)
	if reply == "" {
		reply = strings.TrimSpace(strings.Join(sentences, " "))
	}
	if len(sentences) == 0 && reply == "" {
		// Nothing was said, so there is nothing to commit to: let the brain answer.
		reason := "empty_stream"
		if streamErr != nil {
			reason = "stream: " + streamErr.Error()
		}
		return FastPathResult{Outcome: fastPathError, Reason: reason}
	}
	res := FastPathResult{Handled: true, Outcome: fastPathHit, Reply: reply}
	if streamErr != nil {
		// The command already executed and speech started: end the turn
		// here instead of asking the brain to repeat it.
		res.Reason = "stream_broke_after_speech: " + streamErr.Error()
	}
	return res
}

// fastPathBrain wraps the real brain for one turn: the fast path is tried
// first, and the real brain is asked only when it did not handle the turn.
// It implements AudioStreamingBrainClient so a hit plays through the hub's
// existing sentence-streaming path.
type fastPathBrain struct {
	inner   BrainClient
	fp      FastPathClient
	metrics *Metrics
	nodeID  string

	// handled and elapsed are set by AskStreaming so the hub can keep the
	// fast path out of its "brain" stage metric (one wrapper per turn).
	handled bool
	elapsed time.Duration
}

// brainStageSeconds returns the brain's own share of a turn's ask duration and
// whether a "brain" stage should be recorded at all: a fast-path hit never
// reached the brain, and a miss spent part of the duration on the fast path.
func brainStageSeconds(b BrainClient, total time.Duration) (float64, bool) {
	fp, ok := b.(*fastPathBrain)
	if !ok {
		return total.Seconds(), true
	}
	if fp.handled {
		return 0, false
	}
	d := max(total-fp.elapsed, 0)
	return d.Seconds(), true
}

func (b *fastPathBrain) Ask(ctx context.Context, req AskRequest, onStatus func(status string)) (string, error) {
	return b.AskStreaming(ctx, req, onStatus, nil)
}

func (b *fastPathBrain) AskStreaming(ctx context.Context, req AskRequest, onStatus func(status string), onAudio func(chunk BrainAudioChunk)) (string, error) {
	start := time.Now()
	res := b.fp.Try(ctx, req, onAudio)
	elapsed := time.Since(start)
	b.metrics.RecordStageDuration(b.nodeID, "fast_path", res.Outcome, elapsed.Seconds())
	attrs := []any{"outcome", res.Outcome, "latency_ms", elapsed.Milliseconds(), "node", b.nodeID, "turn_id", req.SessionID}
	if res.Reason != "" {
		attrs = append(attrs, "reason", res.Reason)
	}
	slog.Info("voice fast path", attrs...)
	b.handled = res.Handled
	b.elapsed = elapsed
	if res.Handled {
		return res.Reply, nil
	}

	if b.inner == nil {
		return "", ErrBrainFailed
	}
	if s, ok := b.inner.(AudioStreamingBrainClient); ok {
		return s.AskStreaming(ctx, req, onStatus, onAudio)
	}
	return b.inner.Ask(ctx, req, onStatus)
}
