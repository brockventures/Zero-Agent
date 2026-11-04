package server_test

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/server"
)

type mockLoader struct {
	pkgs map[string]*domain.Package
}

func (m *mockLoader) LoadPackage(name string) (*domain.Package, error) {
	if m == nil || m.pkgs == nil {
		return nil, fmt.Errorf("package not found: %s", name)
	}
	p, ok := m.pkgs[name]
	if !ok {
		return nil, fmt.Errorf("package not found: %s", name)
	}
	return p, nil
}

type mockEditorHub struct {
	dispatched bool
	err        error
}

func (m *mockEditorHub) DispatchConfigReload(snap *config.Snapshot, diff *config.ConfigDiff) error {
	m.dispatched = true
	return m.err
}

const (
	testValidYAML = `
timezone: "America/Los_Angeles"
display:
  grid:
    columns: 6
    rows: 2
  widgets:
    - id: test-clock
      type: clock
      dimensions: [3, 2]
    - id: test-weather
      type: weather
      dimensions: [3, 2]
`
	testDisabledYAML = `
timezone: "America/Los_Angeles"
display:
  allow_layout_edit: false
  grid:
    columns: 6
    rows: 2
  widgets:
    - id: test-clock
      type: clock
      dimensions: [3, 2]
    - id: test-weather
      type: weather
      dimensions: [3, 2]
`
	testMultiScreenYAML = `
timezone: "America/Los_Angeles"
display:
  grid:
    columns: 6
    rows: 2
  widgets:
    - id: test-clock
      type: clock
      dimensions: [3, 2]
      pinned: true
    - id: test-weather
      type: weather
      dimensions: [3, 2]
    - id: test-spacer
      type: spacer
      dimensions: [3, 2]
    - id: test-weather-2
      type: weather
      dimensions: [3, 2]
`
)

func newTestManager(t *testing.T, yamlStr, overridePath string) (*config.Manager, *mockLoader) {
	t.Helper()
	loader := &mockLoader{
		pkgs: map[string]*domain.Package{
			"clock": {
				Type: "clock",
				Manifest: domain.WidgetManifest{
					Name:     "clock",
					Version:  "1.0.0",
					Provider: "builtin",
					SupportedDimensions: []domain.Dimension{
						domain.NewDimension(3, 2),
						domain.NewDimension(6, 2),
					},
				},
			},
			"weather": {
				Type: "weather",
				Manifest: domain.WidgetManifest{
					Name:     "weather",
					Version:  "1.0.0",
					Provider: "builtin",
					SupportedDimensions: []domain.Dimension{
						domain.NewDimension(3, 2),
					},
				},
			},
			"spacer": {
				Type: "spacer",
				Manifest: domain.WidgetManifest{
					Name:     "spacer",
					Version:  "1.0.0",
					Provider: "builtin",
				},
			},
		},
	}
	var opts []config.ManagerOption
	if overridePath != "" {
		opts = append(opts, config.WithOverridePath(overridePath))
	}
	mgr, err := config.NewManager([]byte(yamlStr), loader, os.Getenv, slog.Default(), opts...)
	if err != nil {
		t.Fatalf("failed to create config manager: %v", err)
	}
	return mgr, loader
}

func doEditorReq(h http.HandlerFunc, method, path, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(method, path, strings.NewReader(body)))
	return rec
}

func TestDefaultLayoutEditorHandler_GetLayoutEditor(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		method     string
		nilMgr     bool
		wantCode   int
		wantSubstr string
	}{
		{name: "ok_get", method: http.MethodGet, wantCode: http.StatusOK, wantSubstr: `"allow_layout_edit":true`},
		{name: "ok_head", method: http.MethodHead, wantCode: http.StatusOK},
		{name: "options_cors", method: http.MethodOptions, wantCode: http.StatusNoContent},
		{name: "method_not_allowed", method: http.MethodPost, wantCode: http.StatusMethodNotAllowed},
		{name: "nil_manager", method: http.MethodGet, nilMgr: true, wantCode: http.StatusInternalServerError},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := filepath.Join(t.TempDir(), "layout-override.yaml")
			var mgr *config.Manager
			var loader *mockLoader
			if !tc.nilMgr {
				mgr, loader = newTestManager(t, testValidYAML, tmp)
			}
			h := server.NewDefaultLayoutEditorHandler(mgr, nil, loader, nil)
			rec := doEditorReq(h.GetLayoutEditor, tc.method, "/api/layout/editor", "")
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantSubstr != "" && !strings.Contains(rec.Body.String(), tc.wantSubstr) {
				t.Fatalf("body = %q, want substring %q", rec.Body.String(), tc.wantSubstr)
			}
			if tc.method == http.MethodHead && rec.Body.Len() != 0 {
				t.Fatalf("expected empty body for HEAD, got %d bytes", rec.Body.Len())
			}
		})
	}
}

func TestDefaultLayoutEditorHandler_PutLayoutEditor(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		method     string
		body       string
		disabled   bool
		nilMgr     bool
		hubErr     error
		wantCode   int
		wantSubstr string
	}{
		{
			name:       "ok_valid_put",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0,0],"dimensions":[3,2]},{"widget_id":"test-weather","screen":0,"origin":[3,0],"dimensions":[3,2]}]}`,
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":true`,
		},
		{
			name:     "options_cors",
			method:   http.MethodOptions,
			wantCode: http.StatusNoContent,
		},
		{
			name:     "method_not_allowed",
			method:   http.MethodGet,
			wantCode: http.StatusMethodNotAllowed,
		},
		{
			name:     "nil_manager",
			method:   http.MethodPut,
			body:     `{"widgets":[]}`,
			nilMgr:   true,
			wantCode: http.StatusInternalServerError,
		},
		{
			name:       "disabled_editing",
			method:     http.MethodPut,
			body:       `{"widgets":[]}`,
			disabled:   true,
			wantCode:   http.StatusForbidden,
			wantSubstr: "layout editing is disabled",
		},
		{
			name:       "invalid_json",
			method:     http.MethodPut,
			body:       `{invalid-json`,
			wantCode:   http.StatusBadRequest,
			wantSubstr: "invalid request payload",
		},
		{
			name:       "empty_widget_id",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"","screen":0,"origin":[0,0],"dimensions":[3,2]}]}`,
			wantCode:   http.StatusBadRequest,
			wantSubstr: "widget_id cannot be empty",
		},
		{
			name:       "invalid_origin_len",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0],"dimensions":[3,2]}]}`,
			wantCode:   http.StatusBadRequest,
			wantSubstr: "origin must be [col, row]",
		},
		{
			name:       "invalid_dimensions_len",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0,0],"dimensions":[3]}]}`,
			wantCode:   http.StatusBadRequest,
			wantSubstr: "dimensions must be [cols, rows]",
		},
		{
			name:       "overlap_error",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0,0],"dimensions":[3,2]},{"widget_id":"test-weather","screen":0,"origin":[1,0],"dimensions":[3,2]}]}`,
			wantCode:   http.StatusBadRequest,
			wantSubstr: "overlap",
		},
		{
			name:       "hub_error_tolerated",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0,0],"dimensions":[3,2]}]}`,
			hubErr:     fmt.Errorf("hub failed"),
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":true`,
		},
		{
			name:       "pinned_flag_true",
			method:     http.MethodPut,
			body:       `{"widgets":[{"widget_id":"test-clock","screen":0,"origin":[0,0],"dimensions":[3,2],"pinned":true}]}`,
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":true`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := filepath.Join(t.TempDir(), "layout-override.yaml")
			var mgr *config.Manager
			var loader *mockLoader
			if !tc.nilMgr {
				yamlStr := testValidYAML
				if tc.disabled {
					yamlStr = testDisabledYAML
				}
				mgr, loader = newTestManager(t, yamlStr, tmp)
			}
			hub := &mockEditorHub{err: tc.hubErr}
			h := server.NewDefaultLayoutEditorHandler(mgr, hub, loader, slog.Default())
			rec := doEditorReq(h.PutLayoutEditor, tc.method, "/api/layout/editor", tc.body)
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantSubstr != "" && !strings.Contains(rec.Body.String(), tc.wantSubstr) {
				t.Fatalf("body = %q, want substring %q", rec.Body.String(), tc.wantSubstr)
			}
			if tc.wantCode == http.StatusOK && !hub.dispatched {
				t.Fatal("expected hub dispatch on successful PUT")
			}
		})
	}
}

func TestDefaultLayoutEditorHandler_DeleteLayoutEditor(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		method     string
		disabled   bool
		nilMgr     bool
		hubErr     error
		seedFile   bool
		wantCode   int
		wantSubstr string
	}{
		{
			name:       "ok_delete_no_override",
			method:     http.MethodDelete,
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":false`,
		},
		{
			name:       "ok_delete_with_override",
			method:     http.MethodDelete,
			seedFile:   true,
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":false`,
		},
		{
			name:     "options_cors",
			method:   http.MethodOptions,
			wantCode: http.StatusNoContent,
		},
		{
			name:     "method_not_allowed",
			method:   http.MethodPost,
			wantCode: http.StatusMethodNotAllowed,
		},
		{
			name:     "nil_manager",
			method:   http.MethodDelete,
			nilMgr:   true,
			wantCode: http.StatusInternalServerError,
		},
		{
			name:       "disabled_editing",
			method:     http.MethodDelete,
			disabled:   true,
			wantCode:   http.StatusForbidden,
			wantSubstr: "layout editing is disabled",
		},
		{
			name:       "hub_error_tolerated",
			method:     http.MethodDelete,
			hubErr:     fmt.Errorf("hub failed"),
			wantCode:   http.StatusOK,
			wantSubstr: `"has_override":false`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := filepath.Join(t.TempDir(), "layout-override.yaml")
			var mgr *config.Manager
			var loader *mockLoader
			if !tc.nilMgr {
				yamlStr := testValidYAML
				if tc.disabled {
					yamlStr = testDisabledYAML
				}
				mgr, loader = newTestManager(t, yamlStr, tmp)
				if tc.seedFile {
					ov := &config.LayoutOverrideFile{
						Version: 1,
						Widgets: []config.WidgetOverride{
							{ID: "test-clock", Screen: 0, Origin: [2]int{0, 0}, Dimensions: []int{3, 2}},
						},
					}
					if _, _, err := mgr.ApplyLayoutOverride(ov); err != nil {
						t.Fatalf("failed to seed override: %v", err)
					}
				}
			}
			hub := &mockEditorHub{err: tc.hubErr}
			h := server.NewDefaultLayoutEditorHandler(mgr, hub, loader, nil)
			rec := doEditorReq(h.DeleteLayoutEditor, tc.method, "/api/layout/editor", "")
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantSubstr != "" && !strings.Contains(rec.Body.String(), tc.wantSubstr) {
				t.Fatalf("body = %q, want substring %q", rec.Body.String(), tc.wantSubstr)
			}
			if tc.wantCode == http.StatusOK && !hub.dispatched {
				t.Fatal("expected hub dispatch on successful DELETE")
			}
		})
	}
}

func TestDefaultLayoutEditorHandler_ResponseDetails(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name            string
		yamlStr         string
		wantMinScreens  int
		wantWidgetsLen  int
		wantFirstID     string
		wantFirstSupLen int
	}{
		{
			name:            "single_screen_valid_layout",
			yamlStr:         testValidYAML,
			wantMinScreens:  1,
			wantWidgetsLen:  2,
			wantFirstID:     "test-clock",
			wantFirstSupLen: 2,
		},
		{
			name:           "multi_screen_pinned_and_spacer_filtered",
			yamlStr:        testMultiScreenYAML,
			wantMinScreens: 2,
			wantWidgetsLen: 3,
			wantFirstID:    "test-clock",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := filepath.Join(t.TempDir(), "layout-override.yaml")
			mgr, loader := newTestManager(t, tc.yamlStr, tmp)
			h := server.NewDefaultLayoutEditorHandler(mgr, nil, loader, nil)

			rec := doEditorReq(h.GetLayoutEditor, http.MethodGet, "/api/layout/editor", "")
			if rec.Code != http.StatusOK {
				t.Fatalf("code = %d, want 200", rec.Code)
			}
			var resp api.LayoutEditorResponse
			if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
				t.Fatalf("failed to unmarshal JSON: %v", err)
			}
			if !resp.AllowLayoutEdit || resp.HasOverride {
				t.Errorf("unexpected flags: allow=%v, hasOverride=%v", resp.AllowLayoutEdit, resp.HasOverride)
			}
			if len(resp.Grid) != 2 || resp.Grid[0] != 6 || resp.Grid[1] != 2 {
				t.Errorf("unexpected grid: %v", resp.Grid)
			}
			if resp.TotalScreens < tc.wantMinScreens {
				t.Errorf("totalScreens = %d, want >= %d", resp.TotalScreens, tc.wantMinScreens)
			}
			if len(resp.Widgets) != tc.wantWidgetsLen {
				t.Fatalf("widgets len = %d, want %d", len(resp.Widgets), tc.wantWidgetsLen)
			}
			if resp.Widgets[0].WidgetID != tc.wantFirstID {
				t.Errorf("first widget = %s, want %s", resp.Widgets[0].WidgetID, tc.wantFirstID)
			}
			if tc.wantFirstSupLen > 0 && len(resp.Widgets[0].SupportedDimensions) != tc.wantFirstSupLen {
				t.Errorf("supported dims len = %d, want %d", len(resp.Widgets[0].SupportedDimensions), tc.wantFirstSupLen)
			}
			for _, w := range resp.Widgets {
				if w.Type == "spacer" || strings.HasPrefix(w.WidgetID, "auto-spacer-") {
					t.Errorf("expected no spacer in widgets response, found: %s", w.WidgetID)
				}
			}
		})
	}
}

func TestDefaultLayoutEditorHandler_DimensionResolution(t *testing.T) {
	t.Parallel()
	loader := &mockLoader{
		pkgs: map[string]*domain.Package{
			"custom-card": {
				Type: "custom-card",
				Manifest: domain.WidgetManifest{
					Name:                "custom-card",
					Version:             "1.0.0",
					Provider:            "builtin",
					SupportedDimensions: []domain.Dimension{domain.NewDimension(2, 2)},
				},
			},
			"no-sup": {
				Type:     "no-sup",
				Manifest: domain.WidgetManifest{Name: "no-sup", Version: "1.0.0", Provider: "builtin"},
			},
		},
	}

	tests := []struct {
		name     string
		wType    string
		fallback domain.Dimension
		wantDim  [2]int
	}{
		{name: "loader_supported_dims", wType: "custom-card", fallback: domain.NewDimension(2, 2), wantDim: [2]int{2, 2}},
		{name: "fallback_when_no_supported_dims", wType: "no-sup", fallback: domain.NewDimension(4, 2), wantDim: [2]int{4, 2}},
		{name: "fallback_when_unknown_pkg", wType: "unknown-pkg", fallback: domain.NewDimension(1, 1), wantDim: [2]int{1, 1}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := server.NewDefaultLayoutEditorHandler(nil, nil, loader, nil)
			dims := h.ResolveSupportedDimensionsForTest(tc.wType, tc.fallback, nil)
			if len(dims) != 1 || dims[0][0] != tc.wantDim[0] || dims[0][1] != tc.wantDim[1] {
				t.Fatalf("dims = %v, want [%v]", dims, tc.wantDim)
			}
		})
	}
}

func TestDefaultLayoutEditorHandler_BuildResponseNilLayout(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		cols     int
		rows     int
		wantGrid [2]int
	}{
		{name: "empty_grid_defaults_to_6x2", cols: 0, rows: 0, wantGrid: [2]int{6, 2}},
		{name: "explicit_portrait_grid_3x6", cols: 3, rows: 6, wantGrid: [2]int{3, 6}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := server.NewDefaultLayoutEditorHandler(nil, nil, nil, nil)
			resp := h.BuildResponseForTest(&config.Snapshot{
				Config: &config.Config{
					Display: config.DisplayConfig{
						Grid: config.GridConfig{Columns: tc.cols, Rows: tc.rows},
					},
				},
			})
			if resp.TotalScreens != 0 || len(resp.Grid) != 2 || resp.Grid[0] != tc.wantGrid[0] || resp.Grid[1] != tc.wantGrid[1] {
				t.Fatalf("unexpected nil layout response: %+v", resp)
			}
		})
	}
}
