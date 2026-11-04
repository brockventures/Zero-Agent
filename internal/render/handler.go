package render

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/api"
)

const (
	// DefaultCSSPath is the standard filesystem path for the core HUD stylesheet.
	DefaultCSSPath = "web/static/css/hud.css"

	// DefaultCustomCSSPath is the standard volume mount location for custom CSS (for backward compatibility).
	DefaultCustomCSSPath = "/config/custom.css"
)

// HandlerOption configures Handler instances.
type HandlerOption func(*Handler)

// WithCSSPath sets the stylesheet disk path.
func WithCSSPath(path string) HandlerOption {
	return func(h *Handler) {
		h.cssPath = path
	}
}

// WithCustomCSSPath sets the optional custom override stylesheet path layered on top of the base stylesheet.
func WithCustomCSSPath(path string) HandlerOption {
	return func(h *Handler) {
		h.override = path
	}
}

// Handler serves HTTP endpoints for widget rendering, static package assets, and stylesheets.
type Handler struct {
	mu       sync.RWMutex
	engine   *Engine
	resolver PackageResolver
	cssPath  string
	override string
}

// NewHandler constructs a Handler.
func NewHandler(engine *Engine, resolver PackageResolver, opts ...HandlerOption) *Handler {
	h := &Handler{
		engine:   engine,
		resolver: resolver,
		cssPath:  DefaultCSSPath,
	}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

// SetCSSPath dynamically updates the stylesheet path (e.g. on configuration reload).
func (h *Handler) SetCSSPath(path string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.cssPath = path
}

// GetWidgetRender handles GET /api/widgets/{widget_id}/render requests. The optional
// "node" query parameter is the requesting display's voice node_id.
func (h *Handler) GetWidgetRender(w http.ResponseWriter, r *http.Request, widgetID string) {
	if !handleCORSAndMethod(w, r) {
		return
	}

	if h.engine == nil {
		writeJSONError(w, http.StatusInternalServerError, "rendering engine not configured")
		return
	}

	overrides, err := parseWidgetOverrides(r.URL.Query())
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err.Error())
		return
	}

	htmlBytes, err := h.engine.RenderWidgetWithOverrides(r.Context(), widgetID, r.URL.Query().Get("node"), overrides)
	if err != nil {
		if notFound, ok := errors.AsType[WidgetNotFoundError](err); ok {
			writeJSONError(w, http.StatusNotFound, notFound.Error())
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to render widget: "+err.Error())
		return
	}

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodGet {
		if _, err := w.Write(htmlBytes); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
		}
	}
}

// parseWidgetOverrides validates the date and view query parameters shared by the HTML and JSON widget routes
// and returns them as configuration overrides.
func parseWidgetOverrides(query url.Values) (map[string]any, error) {
	overrides := make(map[string]any)
	if dateParam := strings.TrimSpace(query.Get("date")); dateParam != "" {
		if _, err := time.Parse("2006-01-02", dateParam); err != nil {
			return nil, errors.New("invalid date parameter: must be YYYY-MM-DD")
		}
		overrides["date"] = dateParam
	}
	if viewParam := strings.TrimSpace(query.Get("view")); viewParam != "" {
		lowerView := strings.ToLower(viewParam)
		if lowerView != "day" && lowerView != "week" && lowerView != "month" {
			return nil, errors.New("invalid view parameter: must be day, week, or month")
		}
		overrides["default_view"] = lowerView
	}
	return overrides, nil
}

// GetWidgetJSON handles GET /api/widgets/{widget_id} requests, returning the widget's data, config, placement
// and runtime state as JSON for client-side rendering. Calendar-grid widgets include view_model, the
// server-resolved week and month geometry over the merged config.
func (h *Handler) GetWidgetJSON(w http.ResponseWriter, r *http.Request, widgetID string) {
	if !handleCORSAndMethod(w, r) {
		return
	}

	if h.engine == nil {
		writeJSONError(w, http.StatusInternalServerError, "rendering engine not configured")
		return
	}

	overrides, err := parseWidgetOverrides(r.URL.Query())
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, err.Error())
		return
	}

	ctx, err := h.engine.WidgetContext(r.Context(), widgetID, r.URL.Query().Get("node"), overrides)
	if err != nil {
		if notFound, ok := errors.AsType[WidgetNotFoundError](err); ok {
			writeJSONError(w, http.StatusNotFound, notFound.Error())
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to resolve widget: "+err.Error())
		return
	}

	resp := api.WidgetDetailResponse{
		WidgetID:   ctx.ID,
		Type:       ctx.Type,
		Origin:     []int{ctx.Origin[0], ctx.Origin[1]},
		Dimensions: []int{ctx.Dimensions.Cols, ctx.Dimensions.Rows},
		Config:     ctx.Config,
		State:      ctx.State,
		Data:       ctx.Data,
		Timestamp:  ctx.Timestamp,
		Online:     ctx.Online,
		AssetsBase: ctx.Assets + "/",
	}
	if ctx.Type == "calendar-grid" {
		viewModel, err := h.calendarViewModel(ctx)
		if err != nil {
			writeJSONError(w, http.StatusInternalServerError, "failed to build calendar view model: "+err.Error())
			return
		}
		resp.ViewModel = viewModel
	}

	body, err := json.Marshal(resp)
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to encode widget: "+err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodGet {
		if _, err := w.Write(body); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
		}
	}
}

// calendarViewModel builds the calendar-grid week and month view models over the merged widget context,
// so the date and view overrides change the geometry. active_view mirrors the template's default-view rule.
func (h *Handler) calendarViewModel(ctx Context) (map[string]any, error) {
	now := h.engine.nowFunc()
	week, err := BuildFamilyView(ctx.Data, ctx.Config, ctx.Dimensions, now)
	if err != nil {
		return nil, err
	}
	month, err := BuildFamilyMonthView(ctx.Data, ctx.Config, ctx.Dimensions, now)
	if err != nil {
		return nil, err
	}
	activeView := "week"
	if strings.EqualFold(stringOr(ctx.Config, "default_view", "week"), "month") {
		activeView = "month"
	}
	return map[string]any{"week": week, "month": month, "active_view": activeView}, nil
}

// GetWidgetAsset handles GET /widget-types/{type}/assets/{path} requests with path traversal protection.
func (h *Handler) GetWidgetAsset(w http.ResponseWriter, r *http.Request, widgetType string, assetPath string) {
	if !handleCORSAndMethod(w, r) {
		return
	}

	// 1. Strict path traversal defense
	if assetPath == "" || strings.Contains(assetPath, "..") || filepath.IsAbs(assetPath) {
		writeJSONError(w, http.StatusBadRequest, "invalid asset path: path traversal detected")
		return
	}

	clean := filepath.Clean(assetPath)
	if clean == "." || strings.HasPrefix(clean, "..") || strings.HasPrefix(clean, "/") || strings.HasPrefix(clean, "\\") {
		writeJSONError(w, http.StatusBadRequest, "invalid asset path: path traversal detected")
		return
	}

	if h.resolver == nil {
		writeJSONError(w, http.StatusInternalServerError, "package resolver not configured")
		return
	}

	// 2. Resolve widget package
	pkg, err := h.resolver.LoadPackage(widgetType)
	if err != nil || pkg == nil || !pkg.HasAssets() {
		writeJSONError(w, http.StatusNotFound, "asset not found")
		return
	}

	// 3. Resolve and verify target file
	target := filepath.Join(pkg.AssetsDir, filepath.FromSlash(clean))
	rel, err := filepath.Rel(pkg.AssetsDir, target)
	if err != nil || strings.HasPrefix(rel, "..") || rel == "." {
		writeJSONError(w, http.StatusBadRequest, "invalid asset path: path traversal detected")
		return
	}

	fi, err := os.Stat(target)
	if err != nil || fi.IsDir() {
		writeJSONError(w, http.StatusNotFound, "asset not found")
		return
	}

	http.ServeFile(w, r, target)
}

func tryServeCombinedStyle(w http.ResponseWriter, r *http.Request, cssPath, customCSSPath string, baseModTime time.Time) bool {
	if customCSSPath == "" {
		return false
	}
	customFi, statErr := os.Stat(customCSSPath)
	if statErr != nil || customFi.IsDir() {
		return false
	}
	baseBytes, baseErr := os.ReadFile(cssPath)
	if baseErr != nil {
		return false
	}
	customBytes, customErr := os.ReadFile(customCSSPath)
	if customErr != nil {
		return false
	}
	combined := append(append(slices.Clip(baseBytes), '\n'), customBytes...)
	modTime := customFi.ModTime()
	if baseModTime.After(modTime) {
		modTime = baseModTime
	}
	http.ServeContent(w, r, "style.css", modTime, bytes.NewReader(combined))
	return true
}

// ServeStyle handles GET /style.css requests, serving the configured stylesheet from disk.
func (h *Handler) ServeStyle(w http.ResponseWriter, r *http.Request) {
	if !handleCORSAndMethod(w, r) {
		return
	}

	w.Header().Set("Content-Type", "text/css; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=0, must-revalidate")

	h.mu.RLock()
	cssPath, customCSSPath := h.cssPath, h.override
	h.mu.RUnlock()

	if cssPath == "" {
		writeJSONError(w, http.StatusNotFound, "stylesheet not configured")
		return
	}

	fi, err := os.Stat(cssPath)
	if err != nil || fi.IsDir() {
		writeJSONError(w, http.StatusNotFound, "stylesheet not found")
		return
	}

	if tryServeCombinedStyle(w, r, cssPath, customCSSPath, fi.ModTime()) {
		return
	}

	http.ServeFile(w, r, cssPath)
}

func handleCORSAndMethod(w http.ResponseWriter, r *http.Request) bool {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return false
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD, OPTIONS")
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
		return false
	}
	return true
}

func writeJSONError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(api.ErrorResponse{
		Status: "error",
		Error:  msg,
	}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
