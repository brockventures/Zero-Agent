package render_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
	"github.com/azylman/mirrormere/internal/render"
)

func setupTestServer(t *testing.T) (*render.Handler, string, *domain.Package) {
	t.Helper()
	tmpDir := t.TempDir()
	pkg := createTestPackage(t, tmpDir, "sensor", `<div class="sensor">{{.Data.temp}}</div>`)
	subDir := filepath.Join(pkg.Dir, "assets", "sub")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatalf("failed to create sub assets dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(subDir, "nested.png"), []byte("pngdata"), 0o644); err != nil {
		t.Fatalf("failed to write nested asset: %v", err)
	}

	brokenPkg := createTestPackage(t, tmpDir, "broken", `{{.Data.Bad.Property}}`)
	calPkg := createTestPackage(t, tmpDir, "cal", `<div class="cal" data-date="{{.Config.date}}" data-view="{{.Config.default_view}}">OK</div>`)
	pkgs := map[string]*domain.Package{"sensor": pkg, "broken": brokenPkg, "cal": calPkg, "no-assets": {Type: "no-assets"}}

	snap := &config.Snapshot{
		Config: &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{
			{ID: "sensor-1", Type: "sensor"}, {ID: "b1", Type: "broken"}, {ID: "cal-1", Type: "cal", Config: map[string]any{"default_view": "week"}},
		}}},
		Layout: &layout.Layout{Screens: []layout.Screen{{Index: 0, Widgets: []layout.PlacedWidget{
			{WidgetID: "sensor-1", Type: "sensor", Dimensions: domain.NewDimension(2, 1)},
			{WidgetID: "b1", Type: "broken", Dimensions: domain.NewDimension(2, 1)},
			{WidgetID: "cal-1", Type: "cal", Dimensions: domain.NewDimension(2, 1)},
		}}}},
		Packages: pkgs,
	}
	provider := &mockSnapshotProvider{
		snapshot: snap,
		states:   map[string]mockState{"sensor-1": {data: map[string]any{"temp": "68F"}, state: "healthy"}, "b1": {data: "string-not-map", state: "healthy"}, "cal-1": {data: map[string]any{}, state: "healthy"}},
	}
	resolver := &mockResolver{packages: pkgs}
	return render.NewHandler(render.NewEngine(resolver, provider), resolver, render.WithCSSPath(filepath.Join(tmpDir, "hud.css"))), tmpDir, pkg
}

func TestHandler_GetWidgetRender(t *testing.T) {
	t.Parallel()
	handler, _, _ := setupTestServer(t)

	tests := []struct {
		name       string
		method     string
		path       string
		widgetID   string
		nilEngine  bool
		wantStatus int
		wantBody   []string
		wantEmpty  bool
		wantErrMsg string
		checkCORS  bool
		wantAllow  string
	}{
		{name: "success GET 200", method: http.MethodGet, path: "/api/widgets/sensor-1/render", widgetID: "sensor-1", wantStatus: http.StatusOK, wantBody: []string{`<div class="sensor">68F</div>`}, checkCORS: true},
		{name: "HEAD 200 empty body", method: http.MethodHead, path: "/api/widgets/sensor-1/render", widgetID: "sensor-1", wantStatus: http.StatusOK, wantEmpty: true},
		{name: "OPTIONS 204", method: http.MethodOptions, path: "/api/widgets/sensor-1/render", widgetID: "sensor-1", wantStatus: http.StatusNoContent},
		{name: "POST 405 method not allowed", method: http.MethodPost, path: "/api/widgets/sensor-1/render", widgetID: "sensor-1", wantStatus: http.StatusMethodNotAllowed, wantAllow: "GET, HEAD, OPTIONS", wantErrMsg: "method not allowed"},
		{name: "404 widget not found", method: http.MethodGet, path: "/api/widgets/missing-widget/render", widgetID: "missing-widget", wantStatus: http.StatusNotFound, wantErrMsg: "widget 'missing-widget' not found"},
		{name: "500 nil engine", method: http.MethodGet, path: "/api/widgets/sensor-1/render", widgetID: "sensor-1", nilEngine: true, wantStatus: http.StatusInternalServerError, wantErrMsg: "rendering engine not configured"},
		{name: "500 template execution error", method: http.MethodGet, path: "/api/widgets/b1/render", widgetID: "b1", wantStatus: http.StatusInternalServerError, wantErrMsg: "failed to render widget"},
		{name: "valid date override", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=2026-10-15", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-date="2026-10-15"`, `data-view="week"`}},
		{name: "valid view override day", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=day", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-view="day"`}},
		{name: "valid view override week", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=week", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-view="week"`}},
		{name: "valid view override month", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=month", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-view="month"`}},
		{name: "valid view override DAY uppercase", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=DAY", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-view="day"`}},
		{name: "valid view override Month mixed case", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=Month", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-view="month"`}},
		{name: "valid date and view combined", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=2026-11-20&view=month", widgetID: "cal-1", wantStatus: http.StatusOK, wantBody: []string{`data-date="2026-11-20"`, `data-view="month"`}},
		{name: "invalid date bad text", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=invalid", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid date bad month", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=2026-13-01", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid date leap day nonleap", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=2026-02-30", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid date US format", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=10-15-2026", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid date slash format", method: http.MethodGet, path: "/api/widgets/cal-1/render?date=2026/10/15", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid view year", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=year", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid view parameter: must be day, week, or month"},
		{name: "invalid view agenda", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=agenda", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid view parameter: must be day, week, or month"},
		{name: "invalid view list", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=list", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid view parameter: must be day, week, or month"},
		{name: "invalid view random", method: http.MethodGet, path: "/api/widgets/cal-1/render?view=invalid", widgetID: "cal-1", wantStatus: http.StatusBadRequest, wantErrMsg: "invalid view parameter: must be day, week, or month"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := handler
			if tc.nilEngine {
				h = render.NewHandler(nil, nil)
			}
			rec := httptest.NewRecorder()
			h.GetWidgetRender(rec, httptest.NewRequest(tc.method, tc.path, nil), tc.widgetID)
			if rec.Code != tc.wantStatus || (tc.wantEmpty && rec.Body.Len() != 0) || (tc.wantAllow != "" && rec.Header().Get("Allow") != tc.wantAllow) {
				t.Fatalf("status=%d (want %d), len=%d, allow=%q", rec.Code, tc.wantStatus, rec.Body.Len(), rec.Header().Get("Allow"))
			}
			if tc.checkCORS && (rec.Header().Get("Access-Control-Allow-Origin") != "*" || rec.Header().Get("Content-Type") != "text/html; charset=utf-8") {
				t.Errorf("headers mismatch: %v", rec.Header())
			}
			for _, s := range tc.wantBody {
				if !strings.Contains(rec.Body.String(), s) {
					t.Errorf("body missing %q: %s", s, rec.Body.String())
				}
			}
			if tc.wantErrMsg != "" {
				var errResp api.ErrorResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &errResp); err != nil || !strings.Contains(errResp.Error, tc.wantErrMsg) {
					t.Errorf("error = %v (%+v), want %q", err, errResp, tc.wantErrMsg)
				}
			}
		})
	}
}

func TestHandler_GetWidgetAsset(t *testing.T) {
	t.Parallel()
	handler, _, _ := setupTestServer(t)

	tests := []struct {
		name        string
		method      string
		widgetType  string
		assetPath   string
		nilResolver bool
		wantStatus  int
		wantBody    string
		wantErrMsg  string
	}{
		{name: "valid svg asset GET", method: http.MethodGet, widgetType: "sensor", assetPath: "icon.svg", wantStatus: http.StatusOK, wantBody: "<svg>icon</svg>"},
		{name: "nested png asset GET", method: http.MethodGet, widgetType: "sensor", assetPath: "sub/nested.png", wantStatus: http.StatusOK, wantBody: "pngdata"},
		{name: "traversal dot dot prefix", method: http.MethodGet, widgetType: "sensor", assetPath: "../icon.svg", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal nested escape", method: http.MethodGet, widgetType: "sensor", assetPath: "sub/../../manifest.yaml", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal absolute path", method: http.MethodGet, widgetType: "sensor", assetPath: "/etc/passwd", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal suffix escape", method: http.MethodGet, widgetType: "sensor", assetPath: "icon.svg/../..", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal bare dot dot", method: http.MethodGet, widgetType: "sensor", assetPath: "..", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal bare dot", method: http.MethodGet, widgetType: "sensor", assetPath: ".", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "traversal empty string", method: http.MethodGet, widgetType: "sensor", assetPath: "", wantStatus: http.StatusBadRequest, wantErrMsg: "path traversal detected"},
		{name: "unknown package 404", method: http.MethodGet, widgetType: "unknown", assetPath: "icon.svg", wantStatus: http.StatusNotFound, wantErrMsg: "asset not found"},
		{name: "missing file 404", method: http.MethodGet, widgetType: "sensor", assetPath: "nonexistent.png", wantStatus: http.StatusNotFound, wantErrMsg: "asset not found"},
		{name: "directory path 404", method: http.MethodGet, widgetType: "sensor", assetPath: "sub", wantStatus: http.StatusNotFound, wantErrMsg: "asset not found"},
		{name: "OPTIONS preflight 204", method: http.MethodOptions, widgetType: "sensor", assetPath: "icon.svg", wantStatus: http.StatusNoContent},
		{name: "POST method not allowed 405", method: http.MethodPost, widgetType: "sensor", assetPath: "icon.svg", wantStatus: http.StatusMethodNotAllowed, wantErrMsg: "method not allowed"},
		{name: "nil resolver 500", method: http.MethodGet, widgetType: "sensor", assetPath: "icon.svg", nilResolver: true, wantStatus: http.StatusInternalServerError, wantErrMsg: "package resolver not configured"},
		{name: "empty assets dir 404", method: http.MethodGet, widgetType: "no-assets", assetPath: "any.png", wantStatus: http.StatusNotFound, wantErrMsg: "asset not found"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := handler
			if tc.nilResolver {
				h = render.NewHandler(nil, nil)
			}
			rec := httptest.NewRecorder()
			h.GetWidgetAsset(rec, httptest.NewRequest(tc.method, "/widget-types/"+tc.widgetType+"/assets/"+tc.assetPath, nil), tc.widgetType, tc.assetPath)
			if rec.Code != tc.wantStatus || (tc.wantBody != "" && !strings.Contains(rec.Body.String(), tc.wantBody)) {
				t.Fatalf("status = %d (want %d), body = %q", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantErrMsg != "" {
				var errResp api.ErrorResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &errResp); err != nil || !strings.Contains(errResp.Error, tc.wantErrMsg) {
					t.Errorf("error = %v (%+v), want %q", err, errResp, tc.wantErrMsg)
				}
			}
		})
	}
}

func TestHandler_ServeStyle(t *testing.T) {
	t.Parallel()
	tmpDir := t.TempDir()
	validCSS := filepath.Join(tmpDir, "valid.css")
	customCSS := filepath.Join(tmpDir, "custom.css")
	olderCustomCSS := filepath.Join(tmpDir, "older_custom.css")
	_ = os.WriteFile(validCSS, []byte("/* test stylesheet */"), 0o644)
	_ = os.WriteFile(customCSS, []byte("/* custom stylesheet */"), 0o644)
	_ = os.WriteFile(olderCustomCSS, []byte("/* older custom stylesheet */"), 0o644)
	baseTime := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	newerTime := time.Date(2026, 10, 18, 13, 0, 0, 0, time.UTC)
	olderTime := time.Date(2026, 10, 18, 11, 0, 0, 0, time.UTC)
	_ = os.Chtimes(validCSS, baseTime, baseTime)
	_ = os.Chtimes(customCSS, newerTime, newerTime)
	_ = os.Chtimes(olderCustomCSS, olderTime, olderTime)
	missingCSS := filepath.Join(tmpDir, "nonexistent.css")

	tests := []struct {
		name            string
		method          string
		cssPath         string
		customCSSPath   string
		setPath         string
		wantStatus      int
		wantBody        string
		wantEmpty       bool
		wantContentType string
		wantCacheCtrl   string
		wantLastMod     string
		wantErrMsg      string
	}{
		{name: "missing stylesheet 404", method: http.MethodGet, cssPath: missingCSS, wantStatus: http.StatusNotFound, wantErrMsg: "stylesheet not found"},
		{name: "empty stylesheet path 404", method: http.MethodGet, cssPath: "", wantStatus: http.StatusNotFound, wantErrMsg: "stylesheet not configured"},
		{name: "valid stylesheet GET 200", method: http.MethodGet, cssPath: validCSS, wantStatus: http.StatusOK, wantBody: "/* test stylesheet */", wantContentType: "text/css; charset=utf-8", wantCacheCtrl: "public, max-age=0, must-revalidate", wantLastMod: baseTime.Format(http.TimeFormat)},
		{name: "custom override layered on base 200 (custom newer)", method: http.MethodGet, cssPath: validCSS, customCSSPath: customCSS, wantStatus: http.StatusOK, wantBody: "/* test stylesheet */\n/* custom stylesheet */", wantContentType: "text/css; charset=utf-8", wantLastMod: newerTime.Format(http.TimeFormat)},
		{name: "custom override layered on base 200 (base newer)", method: http.MethodGet, cssPath: validCSS, customCSSPath: olderCustomCSS, wantStatus: http.StatusOK, wantBody: "/* test stylesheet */\n/* older custom stylesheet */", wantContentType: "text/css; charset=utf-8", wantLastMod: baseTime.Format(http.TimeFormat)},
		{name: "missing custom override falls back to base 200", method: http.MethodGet, cssPath: validCSS, customCSSPath: missingCSS, wantStatus: http.StatusOK, wantBody: "/* test stylesheet */"},
		{name: "custom stylesheet SetCSSPath 200", method: http.MethodGet, cssPath: validCSS, setPath: customCSS, wantStatus: http.StatusOK, wantBody: "/* custom stylesheet */"},
		{name: "OPTIONS preflight 204", method: http.MethodOptions, cssPath: validCSS, wantStatus: http.StatusNoContent},
		{name: "HEAD request 200", method: http.MethodHead, cssPath: validCSS, wantStatus: http.StatusOK, wantEmpty: true},
		{name: "POST method not allowed 405", method: http.MethodPost, cssPath: validCSS, wantStatus: http.StatusMethodNotAllowed, wantErrMsg: "method not allowed"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := render.NewHandler(nil, nil, render.WithCSSPath(tc.cssPath), render.WithCustomCSSPath(tc.customCSSPath))
			if tc.setPath != "" {
				h.SetCSSPath(tc.setPath)
			}
			rec := httptest.NewRecorder()
			h.ServeStyle(rec, httptest.NewRequest(tc.method, "/style.css", nil))
			if rec.Code != tc.wantStatus || (tc.wantEmpty && rec.Body.Len() != 0) || (tc.wantBody != "" && rec.Body.String() != tc.wantBody) {
				t.Fatalf("status=%d (want %d), body=%q", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if (tc.wantContentType != "" && rec.Header().Get("Content-Type") != tc.wantContentType) || (tc.wantCacheCtrl != "" && rec.Header().Get("Cache-Control") != tc.wantCacheCtrl) || (tc.wantLastMod != "" && rec.Header().Get("Last-Modified") != tc.wantLastMod) {
				t.Errorf("headers = %v", rec.Header())
			}
			if tc.wantErrMsg != "" {
				var errResp api.ErrorResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &errResp); err != nil || !strings.Contains(errResp.Error, tc.wantErrMsg) {
					t.Errorf("error = %v (%+v), want %q", err, errResp, tc.wantErrMsg)
				}
			}
		})
	}
}
