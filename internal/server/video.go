package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/video"
)

// VideoCoordinator abstracts video priority stack and action forwarding coordination.
type VideoCoordinator interface {
	GetState() video.VideoState
	Trigger(stream video.VideoStream) (video.VideoState, error)
	Dismiss(id string) (video.VideoState, error)
	UpdatePlayerState(id string, update video.VideoStream) (video.VideoState, error)
	ForwardAction(ctx context.Context, id, action string, value any) error
}

// DefaultVideoHandler serves /api/video/* endpoints.
type DefaultVideoHandler struct {
	coord VideoCoordinator
}

// NewDefaultVideoHandler constructs a DefaultVideoHandler backed by coord.
func NewDefaultVideoHandler(coord VideoCoordinator) *DefaultVideoHandler {
	return &DefaultVideoHandler{coord: coord}
}

func (h *DefaultVideoHandler) ready(w http.ResponseWriter) bool {
	if h.coord == nil {
		writeAPIError(w, http.StatusInternalServerError, "video coordinator not configured")
		return false
	}
	return true
}

// GetVideoState handles GET /api/video/state.
func (h *DefaultVideoHandler) GetVideoState(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "GET, HEAD, OPTIONS", http.MethodGet, http.MethodHead) || !h.ready(w) {
		return
	}
	if r.Method == http.MethodHead {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		return
	}
	writeJSON(w, http.StatusOK, toAPIVideoStateResponse(h.coord.GetState()))
}

// PostVideoTrigger handles POST /api/video/trigger.
func (h *DefaultVideoHandler) PostVideoTrigger(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) || !h.ready(w) {
		return
	}
	stream := video.VideoStream{
		Type:        video.TypeWebRTC,
		Priority:    video.PriorityPersistent,
		PlayerState: video.PlayerStatePlaying,
	}
	if err := json.NewDecoder(r.Body).Decode(&stream); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid video trigger request: malformed JSON")
		return
	}
	reqID := strings.TrimSpace(stream.ID)
	if reqID == "" || strings.TrimSpace(stream.StreamURL) == "" || reqID == "all" || reqID == "*" {
		msg := "invalid video trigger request: id and stream_url are required"
		if reqID == "all" || reqID == "*" {
			msg = "invalid video trigger request: 'all' and '*' are reserved sentinels"
		}
		writeAPIError(w, http.StatusBadRequest, msg)
		return
	}
	if stream.Type == "" {
		stream.Type = video.TypeWebRTC
	}
	if stream.Priority == "" {
		stream.Priority = video.PriorityPersistent
	}
	if stream.PlayerState == "" {
		stream.PlayerState = video.PlayerStatePlaying
	}

	newState, err := h.coord.Trigger(stream)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, toAPIVideoStateResponse(newState))
}

// PostVideoDismiss handles POST /api/video/dismiss.
func (h *DefaultVideoHandler) PostVideoDismiss(w http.ResponseWriter, r *http.Request) {
	var req api.VideoDismissRequest
	if !h.decodeRequiredVideoPost(w, r, &req,
		"invalid video dismiss request: malformed JSON",
		"invalid video dismiss request: id is required",
		func() bool { return strings.TrimSpace(req.ID) == "" }) {
		return
	}
	newState, err := h.coord.Dismiss(req.ID)
	writeStreamMutationResult(w, err, toAPIVideoStateResponse(newState))
}

// PostVideoAction handles POST /api/video/action.
func (h *DefaultVideoHandler) PostVideoAction(w http.ResponseWriter, r *http.Request) {
	var req video.ActionRequest
	if !h.decodeRequiredVideoPost(w, r, &req,
		"invalid video action request: malformed JSON",
		"invalid video action request: id and action are required",
		func() bool { return strings.TrimSpace(req.ID) == "" || req.Action == "" }) {
		return
	}
	err := h.coord.ForwardAction(r.Context(), req.ID, req.Action, req.Value)
	if err != nil {
		switch {
		case errors.Is(err, video.ErrStreamNotFound):
			writeAPIError(w, http.StatusNotFound, "stream not active")
		case errors.Is(err, video.ErrNotControllable):
			writeAPIError(w, http.StatusUnprocessableEntity, "stream is not controllable")
		case errors.Is(err, video.ErrControllerUnreachable):
			writeAPIError(w, http.StatusBadGateway, "stream controller unreachable")
		default:
			writeAPIError(w, http.StatusBadRequest, err.Error())
		}
		return
	}
	writeJSON(w, http.StatusOK, api.ActionResponse{Status: "ok"})
}

// PostVideoState handles POST /api/video/state.
func (h *DefaultVideoHandler) PostVideoState(w http.ResponseWriter, r *http.Request) {
	var update video.VideoStream
	if !h.decodeRequiredVideoPost(w, r, &update,
		"invalid video player state request: malformed JSON",
		"invalid video player state request: id and player_state are required",
		func() bool { return strings.TrimSpace(update.ID) == "" || update.PlayerState == "" }) {
		return
	}
	_, err := h.coord.UpdatePlayerState(update.ID, update)
	writeStreamMutationResult(w, err, api.VideoPlayerStateResponse{
		Status: "ok", ID: update.ID, PlayerState: update.PlayerState,
	})
}

func (h *DefaultVideoHandler) decodeRequiredVideoPost(w http.ResponseWriter, r *http.Request, dst any, malformedMsg, missingMsg string, missing func() bool) bool {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) || !h.ready(w) {
		return false
	}
	if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
		writeAPIError(w, http.StatusBadRequest, malformedMsg)
		return false
	}
	if missing != nil && missing() {
		writeAPIError(w, http.StatusBadRequest, missingMsg)
		return false
	}
	return true
}

func writeStreamMutationResult(w http.ResponseWriter, err error, resp any) {
	if err != nil {
		if errors.Is(err, video.ErrStreamNotFound) {
			writeAPIError(w, http.StatusNotFound, "stream not active")
			return
		}
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func toAPIVideoStateResponse(state video.VideoState) api.VideoStateResponse {
	mode := video.ModeWidgets
	if state.Mode == video.ModeVideo {
		mode = video.ModeVideo
	}
	return api.VideoStateResponse{
		Status:  "ok",
		Mode:    mode,
		Primary: state.Primary,
		Pip:     state.Pip,
	}
}
