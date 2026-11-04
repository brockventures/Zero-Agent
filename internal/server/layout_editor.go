package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
)

type layoutEditorHub interface {
	DispatchConfigReload(snapshot *config.Snapshot, diff *config.ConfigDiff) error
}

// DefaultLayoutEditorHandler implements LayoutEditorHandler for live canvas layout editing.
type DefaultLayoutEditorHandler struct {
	mgr    *config.Manager
	hub    layoutEditorHub
	loader config.PackageLoader
	logger *slog.Logger
}

// NewDefaultLayoutEditorHandler constructs a DefaultLayoutEditorHandler.
func NewDefaultLayoutEditorHandler(mgr *config.Manager, hub layoutEditorHub, loader config.PackageLoader, logger *slog.Logger) *DefaultLayoutEditorHandler {
	if logger == nil {
		logger = slog.Default()
	}
	return &DefaultLayoutEditorHandler{
		mgr:    mgr,
		hub:    hub,
		loader: loader,
		logger: logger,
	}
}

// GetLayoutEditor handles GET /api/layout/editor.
func (h *DefaultLayoutEditorHandler) GetLayoutEditor(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "GET, HEAD, OPTIONS", http.MethodGet, http.MethodHead) {
		return
	}
	if h.mgr == nil {
		writeAPIError(w, http.StatusInternalServerError, "config manager not configured")
		return
	}
	snap := h.mgr.CurrentSnapshot()
	if snap == nil || snap.Config == nil {
		writeAPIError(w, http.StatusInternalServerError, "current configuration not available")
		return
	}
	if r.Method == http.MethodHead {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		return
	}
	writeJSON(w, http.StatusOK, h.buildResponse(snap))
}

// PutLayoutEditor handles PUT /api/layout/editor.
func (h *DefaultLayoutEditorHandler) PutLayoutEditor(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "PUT, OPTIONS", http.MethodPut) {
		return
	}
	if h.mgr == nil {
		writeAPIError(w, http.StatusInternalServerError, "config manager not configured")
		return
	}
	snap := h.mgr.CurrentSnapshot()
	if snap == nil || snap.Config == nil {
		writeAPIError(w, http.StatusInternalServerError, "current configuration not available")
		return
	}
	if !snap.Config.Display.IsLayoutEditAllowed() {
		writeAPIError(w, http.StatusForbidden, "layout editing is disabled by configuration")
		return
	}
	var req api.LayoutEditorUpdateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeAPIError(w, http.StatusBadRequest, fmt.Sprintf("invalid request payload: %v", err))
		return
	}
	overrides, err := parseWidgetPlacements(req.Widgets)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	newSnap, diff, err := h.mgr.ApplyLayoutOverride(&config.LayoutOverrideFile{
		Version: 1,
		Widgets: overrides,
	})
	if err != nil {
		if errors.Is(err, config.ErrLayoutEditDisabled) {
			writeAPIError(w, http.StatusForbidden, err.Error())
			return
		}
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}
	if h.hub != nil {
		if err := h.hub.DispatchConfigReload(newSnap, diff); err != nil {
			h.logger.Error("failed to dispatch config reload", "error", err)
		}
	}
	writeJSON(w, http.StatusOK, h.buildResponse(newSnap))
}

func parseWidgetPlacements(widgets []api.LayoutEditorWidgetPlacement) ([]config.WidgetOverride, error) {
	overrides := make([]config.WidgetOverride, 0, len(widgets))
	for _, pw := range widgets {
		if pw.WidgetID == "" {
			return nil, fmt.Errorf("widget_id cannot be empty")
		}
		if len(pw.Origin) != 2 {
			return nil, fmt.Errorf("widget %q: origin must be [col, row]", pw.WidgetID)
		}
		if len(pw.Dimensions) != 2 {
			return nil, fmt.Errorf("widget %q: dimensions must be [cols, rows]", pw.WidgetID)
		}
		overrides = append(overrides, config.WidgetOverride{
			ID:         pw.WidgetID,
			Screen:     pw.Screen,
			Origin:     [2]int{pw.Origin[0], pw.Origin[1]},
			Dimensions: []int{pw.Dimensions[0], pw.Dimensions[1]},
			Pinned:     pw.Pinned,
		})
	}
	return overrides, nil
}

// DeleteLayoutEditor handles DELETE /api/layout/editor.
func (h *DefaultLayoutEditorHandler) DeleteLayoutEditor(w http.ResponseWriter, r *http.Request) {
	if !requireCORSAndMethod(w, r, "DELETE, OPTIONS", http.MethodDelete) {
		return
	}
	if h.mgr == nil {
		writeAPIError(w, http.StatusInternalServerError, "config manager not configured")
		return
	}
	snap := h.mgr.CurrentSnapshot()
	if snap == nil || snap.Config == nil {
		writeAPIError(w, http.StatusInternalServerError, "current configuration not available")
		return
	}
	if !snap.Config.Display.IsLayoutEditAllowed() {
		writeAPIError(w, http.StatusForbidden, "layout editing is disabled by configuration")
		return
	}
	newSnap, diff, err := h.mgr.ResetLayoutOverride()
	if err != nil {
		if errors.Is(err, config.ErrLayoutEditDisabled) {
			writeAPIError(w, http.StatusForbidden, err.Error())
			return
		}
		writeAPIError(w, http.StatusInternalServerError, err.Error())
		return
	}
	if h.hub != nil {
		if err := h.hub.DispatchConfigReload(newSnap, diff); err != nil {
			h.logger.Error("failed to dispatch config reload", "error", err)
		}
	}
	writeJSON(w, http.StatusOK, h.buildResponse(newSnap))
}

func (h *DefaultLayoutEditorHandler) buildResponse(snap *config.Snapshot) api.LayoutEditorResponse {
	gridCols, gridRows := snap.Config.Display.Grid.Columns, snap.Config.Display.Grid.Rows
	if gridCols == 0 || gridRows == 0 {
		gridCols, gridRows = 6, 2
	}
	totalScreens := 0
	if snap.Layout != nil {
		totalScreens = len(snap.Layout.Screens)
	}
	resp := api.LayoutEditorResponse{
		AllowLayoutEdit: snap.Config.Display.IsLayoutEditAllowed(),
		HasOverride:     snap.HasOverride,
		Grid:            []int{gridCols, gridRows},
		TotalScreens:    totalScreens,
		Widgets:         make([]api.LayoutEditorWidget, 0),
	}
	if snap.Layout == nil {
		return resp
	}
	seen := make(map[string]bool)
	for _, screen := range snap.Layout.Screens {
		h.collectScreenWidgets(screen, seen, snap.Packages, &resp.Widgets)
	}
	return resp
}

func (h *DefaultLayoutEditorHandler) collectScreenWidgets(screen layout.Screen, seen map[string]bool, pkgs map[string]*domain.Package, out *[]api.LayoutEditorWidget) {
	for _, pw := range screen.Widgets {
		if pw.Type == "spacer" || strings.HasPrefix(pw.WidgetID, "auto-spacer-") {
			continue
		}
		if seen[pw.WidgetID] {
			continue
		}
		seen[pw.WidgetID] = true
		*out = append(*out, api.LayoutEditorWidget{
			WidgetID:            pw.WidgetID,
			Type:                pw.Type,
			Screen:              screen.Index,
			Origin:              []int{pw.Origin[0], pw.Origin[1]},
			Dimensions:          []int{pw.Dimensions.Cols, pw.Dimensions.Rows},
			Pinned:              pw.Pinned,
			SupportedDimensions: h.resolveSupportedDimensions(pw.Type, pw.Dimensions, pkgs),
		})
	}
}

func (h *DefaultLayoutEditorHandler) resolveSupportedDimensions(wType string, dim domain.Dimension, pkgs map[string]*domain.Package) [][]int {
	if pkgs != nil {
		if pkg, ok := pkgs[wType]; ok && pkg != nil && len(pkg.Manifest.SupportedDimensions) > 0 {
			return dimensionsToSlices(pkg.Manifest.SupportedDimensions)
		}
	}
	if h.loader != nil {
		if pkg, err := h.loader.LoadPackage(wType); err == nil && pkg != nil && len(pkg.Manifest.SupportedDimensions) > 0 {
			return dimensionsToSlices(pkg.Manifest.SupportedDimensions)
		}
	}
	return [][]int{{dim.Cols, dim.Rows}}
}

func dimensionsToSlices(dims []domain.Dimension) [][]int {
	res := make([][]int, len(dims))
	for i, d := range dims {
		res[i] = []int{d.Cols, d.Rows}
	}
	return res
}
