package rotation

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/events"
)

// ScreenController abstracts operations needed by the screen rotation HTTP handler.
type ScreenController interface {
	SelectScreen(index int) (events.ScreenRotateData, error)
	AdvanceScreen(direction string) (events.ScreenRotateData, error)
	PauseRotation(duration time.Duration) (int, error)
	ResumeRotation() (int, error)
	CurrentScreen() int
	ScreenLayout() ScreenLayoutSnapshot
}

// Handler serves HTTP endpoints for screen navigation.
type Handler struct {
	controller ScreenController
}

// NewHandler constructs a Handler wrapping a ScreenController.
func NewHandler(controller ScreenController) *Handler {
	return &Handler{controller: controller}
}

func (h *Handler) checkPostAndController(w http.ResponseWriter, r *http.Request) bool {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return false
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST, OPTIONS")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return false
	}
	if h.controller == nil {
		writeError(w, http.StatusInternalServerError, "screen controller not configured")
		return false
	}
	return true
}

func (h *Handler) PostScreenSelect(w http.ResponseWriter, r *http.Request) {
	if !h.checkPostAndController(w, r) {
		return
	}
	var req api.ScreenSelectRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error())
		return
	}
	data, err := h.controller.SelectScreen(req.ScreenIndex)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, api.ScreenNavigationResponse{Status: "ok", CurrentScreen: data.CurrentScreen, TotalScreens: data.TotalScreens})
}

func (h *Handler) PostScreenAdvance(w http.ResponseWriter, r *http.Request) {
	if !h.checkPostAndController(w, r) {
		return
	}
	direction := "next"
	if r.Body != nil {
		var req api.ScreenAdvanceRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && !errors.Is(err, io.EOF) {
			writeError(w, http.StatusBadRequest, "invalid request body: "+err.Error())
			return
		} else if req.Direction != "" {
			direction = req.Direction
		}
	}
	data, err := h.controller.AdvanceScreen(direction)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, api.ScreenNavigationResponse{Status: "ok", CurrentScreen: data.CurrentScreen, TotalScreens: data.TotalScreens})
}

func (h *Handler) PostScreenPause(w http.ResponseWriter, r *http.Request) {
	if !h.checkPostAndController(w, r) {
		return
	}
	var req api.ScreenPauseRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Paused == nil {
		writeError(w, http.StatusBadRequest, "invalid payload: paused must be a boolean")
		return
	}
	if req.DurationSeconds != nil && *req.DurationSeconds < 0 {
		writeError(w, http.StatusBadRequest, "duration_seconds must be >= 0")
		return
	}

	var currentScreen int
	var err error
	if *req.Paused {
		dur := time.Duration(-1)
		if req.DurationSeconds != nil {
			dur = time.Duration(*req.DurationSeconds) * time.Second
		}
		currentScreen, err = h.controller.PauseRotation(dur)
	} else {
		currentScreen, err = h.controller.ResumeRotation()
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, api.ScreenPauseResponse{Status: "ok", Paused: *req.Paused, CurrentScreen: currentScreen})
}

// GetScreens handles GET /api/screens requests, returning the solved layout of every screen so the
// client can keep adjacent screens mounted.
func (h *Handler) GetScreens(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD, OPTIONS")
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	if h.controller == nil {
		writeError(w, http.StatusInternalServerError, "screen controller not configured")
		return
	}

	snap := h.controller.ScreenLayout()
	resp := api.ScreensResponse{
		CurrentScreen:   snap.CurrentScreen,
		IntervalSeconds: snap.IntervalSeconds,
		Screens:         make([]api.ScreenLayout, 0, len(snap.Screens)),
	}
	for i, screen := range snap.Screens {
		widgets := make([]api.WidgetPlacement, 0, len(screen.Widgets))
		for _, pw := range screen.Widgets {
			widgets = append(widgets, api.WidgetPlacement{
				WidgetID:   pw.WidgetID,
				Type:       pw.Type,
				Origin:     []int{pw.Origin[0], pw.Origin[1]},
				Dimensions: []int{pw.Dimensions.Cols, pw.Dimensions.Rows},
			})
		}
		resp.Screens = append(resp.Screens, api.ScreenLayout{Index: i, Widgets: widgets})
	}
	writeJSON(w, resp)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(api.ErrorResponse{Status: "error", Error: msg}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
