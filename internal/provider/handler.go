package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
)

// WidgetPushCaller abstracts the push data and photo ingestion methods implemented by ProviderCoordinator.
type WidgetPushCaller interface {
	PushWidgetData(ctx context.Context, widgetID string, data map[string]any) (WidgetPayload, error)
	PushWidgetPhotos(ctx context.Context, widgetID string, photos []string, albumName string) (WidgetPayload, error)
}

// WidgetPusher is an alias for WidgetPushCaller for backwards compatibility.
type WidgetPusher = WidgetPushCaller

// PushHandler serves the POST /api/widgets/{widget_id}/push and /api/widgets/{widget_id}/photos webhook endpoints.
type PushHandler struct {
	pusher WidgetPushCaller
	logger *slog.Logger
}

// NewPushHandler constructs an initialized PushHandler.
func NewPushHandler(pusher WidgetPushCaller, logger *slog.Logger) *PushHandler {
	if logger == nil {
		logger = slog.Default()
	}
	return &PushHandler{
		pusher: pusher,
		logger: logger,
	}
}

// PostWidgetPush handles POST /api/widgets/{widget_id}/push requests.
func (h *PushHandler) PostWidgetPush(w http.ResponseWriter, r *http.Request, widgetID string) {
	if !h.handlePreflightAndMethod(w, r) {
		return
	}

	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, MaxHTTPResponseBodySize))
	if err != nil {
		h.writeErrorJSON(w, http.StatusBadRequest, "failed to read request body: "+err.Error())
		return
	}

	var rawDoc any
	if err := json.Unmarshal(bodyBytes, &rawDoc); err != nil {
		h.writeErrorJSON(w, http.StatusBadRequest, "invalid JSON payload: "+err.Error())
		return
	}

	obj, ok := rawDoc.(map[string]any)
	if !ok {
		h.writeErrorJSON(w, http.StatusBadRequest, "payload must be a JSON object")
		return
	}

	payload, err := h.pusher.PushWidgetData(r.Context(), widgetID, obj)
	if err != nil {
		if errors.Is(err, ErrWidgetNotFound) {
			h.writeErrorJSON(w, http.StatusNotFound, fmt.Sprintf("widget '%s' not found in active configuration", widgetID))
			return
		}
		if errors.Is(err, ErrListWidgetPushForbidden) {
			h.writeErrorJSON(w, http.StatusConflict, fmt.Sprintf("cannot push state to list-backed widget '%s'; task lists are read-only and ingested from configured upstream providers", widgetID))
			return
		}
		if errors.Is(err, ErrNonHTTPWidgetPushForbidden) {
			h.writeErrorJSON(w, http.StatusConflict, fmt.Sprintf("cannot push state to widget '%s'; push webhook is strictly limited to widgets using the http provider", widgetID))
			return
		}
		if errors.Is(err, ErrSchemaValidation) {
			h.writeErrorJSON(w, http.StatusBadRequest, err.Error())
			return
		}
		h.writeErrorJSON(w, http.StatusInternalServerError, "push ingestion error: "+err.Error())
		return
	}

	h.writeSuccessJSON(w, widgetID, payload.Timestamp)
}

// PostWidgetPhotos handles POST /api/widgets/{widget_id}/photos requests.
func (h *PushHandler) PostWidgetPhotos(w http.ResponseWriter, r *http.Request, widgetID string) {
	if !h.handlePreflightAndMethod(w, r) {
		return
	}

	var req struct {
		Photos    []string `json:"photos"`
		AlbumName string   `json:"album_name"`
	}
	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, MaxHTTPResponseBodySize))
	if err != nil {
		h.writeErrorJSON(w, http.StatusBadRequest, "failed to read request body: "+err.Error())
		return
	}
	if err := json.Unmarshal(bodyBytes, &req); err != nil {
		h.writeErrorJSON(w, http.StatusBadRequest, "invalid JSON payload: "+err.Error())
		return
	}
	if req.Photos == nil {
		h.writeErrorJSON(w, http.StatusBadRequest, "photos field is required")
		return
	}

	payload, err := h.pusher.PushWidgetPhotos(r.Context(), widgetID, req.Photos, req.AlbumName)
	if err != nil {
		h.handlePhotoPushError(w, widgetID, err)
		return
	}

	h.writeSuccessJSON(w, widgetID, payload.Timestamp)
}

func (h *PushHandler) handlePhotoPushError(w http.ResponseWriter, widgetID string, err error) {
	switch {
	case errors.Is(err, ErrWidgetNotFound):
		h.writeErrorJSON(w, http.StatusNotFound, fmt.Sprintf("widget '%s' not found in active configuration", widgetID))
	case errors.Is(err, ErrPhotoPushNotSupported):
		h.writeErrorJSON(w, http.StatusConflict, fmt.Sprintf("target widget '%s' does not support photo push updates: %s", widgetID, err.Error()))
	case errors.Is(err, ErrInvalidPhotoPushPayload):
		h.writeErrorJSON(w, http.StatusBadRequest, err.Error())
	default:
		h.writeErrorJSON(w, http.StatusInternalServerError, "photo push error: "+err.Error())
	}
}

func (h *PushHandler) handlePreflightAndMethod(w http.ResponseWriter, r *http.Request) bool {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")

	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return false
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST, OPTIONS")
		h.writeErrorJSON(w, http.StatusMethodNotAllowed, "method not allowed")
		return false
	}
	if h.pusher == nil {
		h.writeErrorJSON(w, http.StatusInternalServerError, "pusher not configured")
		return false
	}
	return true
}

func (h *PushHandler) writeSuccessJSON(w http.ResponseWriter, widgetID, updatedAt string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if err := json.NewEncoder(w).Encode(map[string]any{
		"status":     "ok",
		"widget_id":  widgetID,
		"updated_at": updatedAt,
	}); err != nil {
		h.logger.Error("failed to encode push response", "error", err)
	}
}

func (h *PushHandler) writeErrorJSON(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(map[string]string{
		"status": "error",
		"error":  msg,
	}); err != nil {
		h.logger.Error("failed to encode error response", "error", err)
	}
}
