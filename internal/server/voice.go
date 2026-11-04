package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/voice"
)

// VoiceCoordinator abstracts voice coordinator state manipulation.
type VoiceCoordinator interface {
	GetState() events.VoiceStateData
	SetState(state string, transcript, reply, ttsEngine *string) (events.VoiceStateData, error)
}

// VoiceHub abstracts voice interaction pipeline coordination.
type VoiceHub interface {
	IsEnabled() bool
	Interact(ctx context.Context, audio io.Reader, nodeID, sessionID string, sink voice.SSEEventSink, timings voice.EdgeTimings) error
}

// DefaultVoiceHandler serves /api/voice/state, /api/voice/interact, and /api/voice/heartbeat endpoints.
type DefaultVoiceHandler struct {
	coord   VoiceCoordinator
	hub     VoiceHub
	metrics *voice.Metrics
}

// DefaultVoiceHandlerOption configures DefaultVoiceHandler.
type DefaultVoiceHandlerOption func(*DefaultVoiceHandler)

// WithVoiceMetrics configures the Prometheus voice metrics collector for the handler.
func WithVoiceMetrics(m *voice.Metrics) DefaultVoiceHandlerOption {
	return func(h *DefaultVoiceHandler) { h.metrics = m }
}

// NewDefaultVoiceHandler constructs a DefaultVoiceHandler backed by coord and hub.
func NewDefaultVoiceHandler(coord VoiceCoordinator, hub VoiceHub, opts ...DefaultVoiceHandlerOption) *DefaultVoiceHandler {
	h := &DefaultVoiceHandler{coord: coord, hub: hub}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

// PostVoiceState handles POST /api/voice/state.
func (h *DefaultVoiceHandler) PostVoiceState(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) {
		return
	}
	if h.coord == nil {
		writeAPIError(w, http.StatusInternalServerError, "voice coordinator not configured")
		return
	}
	var req api.VoiceStateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid voice state request payload: "+err.Error())
		return
	} else if !voice.IsValidState(req.State) {
		writeAPIError(w, http.StatusBadRequest, voice.ErrInvalidState.Error())
		return
	}
	if _, err := h.coord.SetState(req.State, req.Transcript, req.Reply, req.TTSEngine); err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
	} else {
		writeJSON(w, http.StatusOK, api.VoiceStateResponse{Status: "ok"})
	}
}

// PostVoiceHeartbeat handles POST /api/voice/heartbeat.
func (h *DefaultVoiceHandler) PostVoiceHeartbeat(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	var req api.VoiceHeartbeatRequest
	var id string
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid heartbeat payload: "+err.Error())
		return
	} else if id = strings.TrimSpace(req.NodeID); id == "" {
		writeAPIError(w, http.StatusBadRequest, "missing required 'node_id' field")
		return
	}
	if m := h.metrics; m != nil {
		m.RecordEdgeHeartbeat(id)
		if req.AmbientRMSDBFS != nil {
			m.RecordAmbientRMS(id, *req.AmbientRMSDBFS)
		}
		if req.FalseWakes != nil {
			m.RecordFalseWakes(id, *req.FalseWakes)
		}
		if req.LastPlaybackSec != nil && *req.LastPlaybackSec > 0 {
			m.RecordPlaybackDuration(id, *req.LastPlaybackSec)
		}
	}
	writeJSON(w, http.StatusOK, api.ActionResponse{Status: "ok"})
}

func parseTimingSec(r *http.Request, field string) float64 {
	val := r.FormValue(field)
	if ms, err := strconv.ParseFloat(val, 64); val != "" && err == nil && ms >= 0 && ms <= 60000.0 {
		return ms / 1000.0
	} else if val != "" && err != nil {
		slog.Warn("malformed "+field+" in interact form", "val", val, "error", err)
	}
	return 0
}

func voiceInteractErrCode(err error) int {
	for e, c := range map[error]int{voice.ErrHubDisabled: http.StatusServiceUnavailable, voice.ErrInteractionBusy: http.StatusConflict, voice.ErrInvalidAudio: http.StatusBadRequest} {
		if errors.Is(err, e) {
			return c
		}
	}
	return http.StatusInternalServerError
}

// PostVoiceInteract handles POST /api/voice/interact.
func (h *DefaultVoiceHandler) PostVoiceInteract(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) {
		return
	}
	flusher, ok := w.(http.Flusher)
	if h.hub == nil || !h.hub.IsEnabled() {
		writeAPIError(w, http.StatusServiceUnavailable, "voice hub is disabled")
		return
	} else if !ok {
		writeAPIError(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}

	rc := http.NewResponseController(w)
	if err := rc.SetReadDeadline(time.Now().Add(60 * time.Second)); err != nil && !errors.Is(err, http.ErrNotSupported) {
		slog.Debug("failed to set read deadline", "error", err)
	}
	r.Body = http.MaxBytesReader(w, r.Body, 10<<20)
	if err := r.ParseMultipartForm(10 << 20); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid multipart form: "+err.Error())
		return
	}
	if r.MultipartForm != nil {
		defer func() {
			if err := r.MultipartForm.RemoveAll(); err != nil {
				slog.Debug("failed to remove multipart form", "error", err)
			}
		}()
	}
	clearDeadlines(rc)

	file, _, err := r.FormFile("audio")
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, "missing required 'audio' form field")
		return
	}
	defer file.Close()

	timings := voice.EdgeTimings{
		WakeEvalSec:         parseTimingSec(r, "wake_eval_ms"),
		UtteranceSpeechSec:  parseTimingSec(r, "speech_duration_ms"),
		UtteranceSilenceSec: parseTimingSec(r, "silence_duration_ms"),
	}

	var (
		wroteHeader bool
		sinkMu      sync.Mutex
	)
	sink := func(event string, data any) error {
		sinkMu.Lock()
		defer sinkMu.Unlock()
		if !wroteHeader {
			setSSEHeaders(w, rc)
			wroteHeader = true
		}
		payload, err := json.Marshal(data)
		if err == nil {
			_, err = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, payload)
			flusher.Flush()
		}
		return err
	}

	nodeID, sessionID := strings.TrimSpace(r.FormValue("node_id")), strings.TrimSpace(r.FormValue("session_id"))
	if err := h.hub.Interact(r.Context(), file, nodeID, sessionID, sink, timings); err != nil {
		sinkMu.Lock()
		defer sinkMu.Unlock()
		if !wroteHeader {
			writeAPIError(w, voiceInteractErrCode(err), err.Error())
		}
	}
}
