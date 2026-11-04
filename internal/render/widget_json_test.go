package render_test

import (
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/render"
	"github.com/azylman/mirrormere/internal/widget"
)

var widgetJSONNow = time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)

func loadRepoPackages(t *testing.T, names ...string) map[string]*domain.Package {
	t.Helper()
	loader := widget.NewLoader(filepath.Join("..", "..", "widgets"), t.TempDir())
	pkgs := make(map[string]*domain.Package, len(names))
	for _, name := range names {
		pkg, err := loader.LoadPackage(name)
		if err != nil {
			t.Fatalf("failed to load package %q: %v", name, err)
		}
		pkgs[name] = pkg
	}
	return pkgs
}

func singleWidgetSnapshot(widgetID, widgetType string, dims []int, cfg map[string]any) *config.Snapshot {
	return &config.Snapshot{Config: &config.Config{Display: config.DisplayConfig{
		Widgets: []config.WidgetConfig{{ID: widgetID, Type: widgetType, Dimensions: dims, Config: cfg}},
	}}}
}

func decodeWidgetJSON(t *testing.T, body []byte) api.WidgetDetailResponse {
	t.Helper()
	var resp api.WidgetDetailResponse
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatalf("invalid widget JSON: %v: %s", err, body)
	}
	return resp
}

func TestHandler_GetWidgetJSON_Errors(t *testing.T) {
	t.Parallel()
	pkgs := loadRepoPackages(t, "weather-forecast", "calendar-grid")
	snap := &config.Snapshot{Config: &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{
		{ID: "w1", Type: "weather-forecast", Dimensions: []int{2, 1}},
		{ID: "ghost", Type: "unknown-type", Dimensions: []int{1, 1}},
		{ID: "cal-bad", Type: "calendar-grid", Dimensions: []int{4, 2}, Config: map[string]any{"members": []any{"not-a-mapping"}}},
		{ID: "nan", Type: "weather-forecast", Dimensions: []int{2, 1}},
	}}}}
	states := map[string]mockState{
		"w1":      {data: sampleWeatherData(), state: "healthy", timestamp: "2026-09-26T12:00:00Z"},
		"cal-bad": {data: sampleCalendarData(), state: "healthy"},
		"nan":     {data: map[string]any{"value": math.NaN()}, state: "healthy"},
	}
	engine := render.NewEngine(&mockResolver{packages: pkgs}, &mockSnapshotProvider{snapshot: snap, states: states}, render.WithNowFunc(func() time.Time { return widgetJSONNow }))
	handler := render.NewHandler(engine, &mockResolver{packages: pkgs})

	tests := []struct {
		name       string
		method     string
		path       string
		widgetID   string
		nilEngine  bool
		wantStatus int
		wantErr    string
		wantAllow  string
	}{
		{name: "options preflight", method: http.MethodOptions, path: "/api/widgets/w1", widgetID: "w1", wantStatus: http.StatusNoContent},
		{name: "post not allowed", method: http.MethodPost, path: "/api/widgets/w1", widgetID: "w1", wantStatus: http.StatusMethodNotAllowed, wantErr: "method not allowed", wantAllow: "GET, HEAD, OPTIONS"},
		{name: "nil engine", method: http.MethodGet, path: "/api/widgets/w1", widgetID: "w1", nilEngine: true, wantStatus: http.StatusInternalServerError, wantErr: "rendering engine not configured"},
		{name: "unknown widget", method: http.MethodGet, path: "/api/widgets/nope", widgetID: "nope", wantStatus: http.StatusNotFound, wantErr: "not found in active configuration"},
		{name: "invalid date", method: http.MethodGet, path: "/api/widgets/w1?date=2026-02-30", widgetID: "w1", wantStatus: http.StatusBadRequest, wantErr: "invalid date parameter: must be YYYY-MM-DD"},
		{name: "invalid view", method: http.MethodGet, path: "/api/widgets/w1?view=year", widgetID: "w1", wantStatus: http.StatusBadRequest, wantErr: "invalid view parameter: must be day, week, or month"},
		{name: "package unresolvable", method: http.MethodGet, path: "/api/widgets/ghost", widgetID: "ghost", wantStatus: http.StatusInternalServerError, wantErr: "failed to resolve widget"},
		{name: "calendar view model invalid", method: http.MethodGet, path: "/api/widgets/cal-bad", widgetID: "cal-bad", wantStatus: http.StatusInternalServerError, wantErr: "failed to build calendar view model"},
		{name: "data not encodable", method: http.MethodGet, path: "/api/widgets/nan", widgetID: "nan", wantStatus: http.StatusInternalServerError, wantErr: "failed to encode widget"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := handler
			if tc.nilEngine {
				h = render.NewHandler(nil, nil)
			}
			rec := httptest.NewRecorder()
			h.GetWidgetJSON(rec, httptest.NewRequest(tc.method, tc.path, nil), tc.widgetID)
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d: %s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantAllow != "" && rec.Header().Get("Allow") != tc.wantAllow {
				t.Errorf("Allow = %q, want %q", rec.Header().Get("Allow"), tc.wantAllow)
			}
			if tc.wantErr != "" {
				var errResp api.ErrorResponse
				if err := json.Unmarshal(rec.Body.Bytes(), &errResp); err != nil || !strings.Contains(errResp.Error, tc.wantErr) {
					t.Errorf("error = %v (%+v), want %q", err, errResp, tc.wantErr)
				}
			}
		})
	}
}

func TestHandler_GetWidgetJSON_Fields(t *testing.T) {
	t.Parallel()
	pkgs := loadRepoPackages(t, "weather-forecast")
	cfg := map[string]any{"name": "Home", "api_key_env": "WEATHER_KEY", "token": "secret", "units": "metric"}
	snap := singleWidgetSnapshot("w1", "weather-forecast", []int{2, 1}, cfg)
	states := map[string]mockState{"w1": {data: sampleWeatherData(), state: "degraded", timestamp: "2026-09-26T12:00:00Z"}}
	engine := render.NewEngine(&mockResolver{packages: pkgs}, &mockSnapshotProvider{snapshot: snap, states: states}, render.WithNowFunc(func() time.Time { return widgetJSONNow }))
	handler := render.NewHandler(engine, &mockResolver{packages: pkgs})

	rec := httptest.NewRecorder()
	handler.GetWidgetJSON(rec, httptest.NewRequest(http.MethodGet, "/api/widgets/w1", nil), "w1")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q", ct)
	}
	resp := decodeWidgetJSON(t, rec.Body.Bytes())
	if resp.WidgetID != "w1" || resp.Type != "weather-forecast" || resp.State != "degraded" || resp.Timestamp != "2026-09-26T12:00:00Z" || !resp.Online {
		t.Errorf("unexpected identity/state fields: %+v", resp)
	}
	if resp.AssetsBase != "widget-types/weather-forecast/assets/" {
		t.Errorf("assets_base = %q", resp.AssetsBase)
	}
	if !reflect.DeepEqual(resp.Dimensions, []int{2, 1}) || !reflect.DeepEqual(resp.Origin, []int{0, 0}) {
		t.Errorf("dimensions=%v origin=%v", resp.Dimensions, resp.Origin)
	}
	if resp.Config["name"] != "Home" || resp.Config["units"] != "metric" {
		t.Errorf("config dropped ordinary keys: %v", resp.Config)
	}
	for _, key := range []string{"api_key_env", "token"} {
		if _, ok := resp.Config[key]; ok {
			t.Errorf("config leaked sensitive key %q: %v", key, resp.Config)
		}
	}
	if resp.ViewModel != nil {
		t.Errorf("non-calendar widget returned view_model: %v", resp.ViewModel)
	}
	if strings.Contains(rec.Body.String(), `"view_model"`) {
		t.Errorf("non-calendar widget encoded view_model: %s", rec.Body.String())
	}
}

func TestHandler_GetWidgetJSON_HeadAndNodeData(t *testing.T) {
	t.Parallel()
	pkgs := loadRepoPackages(t, "weather-forecast")
	snap := singleWidgetSnapshot("w1", "weather-forecast", []int{2, 1}, nil)
	states := map[string]mockState{"w1": {data: sampleWeatherData(), state: "healthy"}}
	nodeData := func(widgetID, nodeID string) (any, bool) {
		if widgetID == "w1" && nodeID == "kitchen" {
			return map[string]any{"node": "kitchen"}, true
		}
		return nil, false
	}
	engine := render.NewEngine(&mockResolver{packages: pkgs}, &mockSnapshotProvider{snapshot: snap, states: states},
		render.WithNowFunc(func() time.Time { return widgetJSONNow }), render.WithNodeDataFunc(nodeData))
	handler := render.NewHandler(engine, &mockResolver{packages: pkgs})

	head := httptest.NewRecorder()
	handler.GetWidgetJSON(head, httptest.NewRequest(http.MethodHead, "/api/widgets/w1", nil), "w1")
	if head.Code != http.StatusOK || head.Body.Len() != 0 {
		t.Errorf("HEAD: status=%d len=%d", head.Code, head.Body.Len())
	}

	node := httptest.NewRecorder()
	handler.GetWidgetJSON(node, httptest.NewRequest(http.MethodGet, "/api/widgets/w1?node=kitchen", nil), "w1")
	if got := decodeWidgetJSON(t, node.Body.Bytes()).Data; !reflect.DeepEqual(got, map[string]any{"node": "kitchen"}) {
		t.Errorf("node data = %v, want kitchen override", got)
	}

	shared := httptest.NewRecorder()
	handler.GetWidgetJSON(shared, httptest.NewRequest(http.MethodGet, "/api/widgets/w1?node=den", nil), "w1")
	if got := decodeWidgetJSON(t, shared.Body.Bytes()).Data; reflect.DeepEqual(got, map[string]any{"node": "kitchen"}) || got == nil {
		t.Errorf("unmatched node should keep shared data, got %v", got)
	}
}

func TestHandler_GetWidgetJSON_CalendarViewModel(t *testing.T) {
	t.Parallel()
	pkgs := loadRepoPackages(t, "calendar-grid")
	snap := singleWidgetSnapshot("cal-1", "calendar-grid", []int{4, 2}, nil)
	states := map[string]mockState{"cal-1": {data: sampleCalendarData(), state: "healthy", timestamp: "2026-09-26T12:00:00Z"}}
	engine := render.NewEngine(&mockResolver{packages: pkgs}, &mockSnapshotProvider{snapshot: snap, states: states}, render.WithNowFunc(func() time.Time { return widgetJSONNow }))
	handler := render.NewHandler(engine, &mockResolver{packages: pkgs})

	fetch := func(t *testing.T, path string) map[string]any {
		t.Helper()
		rec := httptest.NewRecorder()
		handler.GetWidgetJSON(rec, httptest.NewRequest(http.MethodGet, path, nil), "cal-1")
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status = %d: %s", path, rec.Code, rec.Body.String())
		}
		vm := decodeWidgetJSON(t, rec.Body.Bytes()).ViewModel
		if vm == nil {
			t.Fatalf("%s: missing view_model", path)
		}
		return vm
	}

	base := fetch(t, "/api/widgets/cal-1")
	for _, key := range []string{"week", "month", "active_view"} {
		if _, ok := base[key]; !ok {
			t.Fatalf("view_model missing %q: %v", key, base)
		}
	}
	if base["active_view"] != "week" || base["month"].(map[string]any)["month"] != float64(9) {
		t.Errorf("default view_model: active_view=%v month=%v", base["active_view"], base["month"])
	}

	dated := fetch(t, "/api/widgets/cal-1?date=2026-11-20")
	if dated["month"].(map[string]any)["month"] != float64(11) || dated["month"].(map[string]any)["year"] != float64(2026) {
		t.Errorf("date override did not move month view: %v", dated["month"])
	}
	if reflect.DeepEqual(base["week"], dated["week"]) {
		t.Errorf("date override did not change week view")
	}

	monthView := fetch(t, "/api/widgets/cal-1?view=month")
	if monthView["active_view"] != "month" {
		t.Errorf("view=month active_view = %v", monthView["active_view"])
	}
	if reflect.DeepEqual(base, monthView) {
		t.Errorf("view=month did not change view_model")
	}
}

func TestEngine_WidgetContext(t *testing.T) {
	t.Parallel()
	pkgs := loadRepoPackages(t, "weather-forecast")
	cfg := map[string]any{"name": "Home", "token": "t", "nested": map[string]any{"password": "p", "keep": 1}}
	snap := singleWidgetSnapshot("w1", "weather-forecast", []int{2, 1}, cfg)
	states := map[string]mockState{"w1": {data: sampleWeatherData(), state: "healthy", timestamp: "2026-09-26T12:00:00Z"}}
	nodeData := func(widgetID, nodeID string) (any, bool) { return map[string]any{"node": nodeID}, nodeID != "" }
	engine := render.NewEngine(&mockResolver{packages: pkgs}, &mockSnapshotProvider{snapshot: snap, states: states}, render.WithNodeDataFunc(nodeData))

	ctxOut, err := engine.WidgetContext(t.Context(), "w1", "lobby", map[string]any{"date": "2026-10-15"})
	if err != nil {
		t.Fatalf("WidgetContext: %v", err)
	}
	if ctxOut.ID != "w1" || ctxOut.Assets != "widget-types/weather-forecast/assets" || ctxOut.State != "healthy" {
		t.Errorf("context = %+v", ctxOut)
	}
	if ctxOut.Config["date"] != "2026-10-15" || ctxOut.Config["name"] != "Home" {
		t.Errorf("overrides not merged: %v", ctxOut.Config)
	}
	if _, ok := ctxOut.Config["token"]; ok {
		t.Errorf("sensitive key leaked: %v", ctxOut.Config)
	}
	nested := ctxOut.Config["nested"].(map[string]any)
	if _, ok := nested["password"]; ok || nested["keep"] != 1 {
		t.Errorf("nested sanitization wrong: %v", nested)
	}
	if !reflect.DeepEqual(ctxOut.Data, map[string]any{"node": "lobby"}) {
		t.Errorf("node data not applied: %v", ctxOut.Data)
	}

	_, err = engine.WidgetContext(t.Context(), "missing", "", nil)
	var notFound render.WidgetNotFoundError
	if !errors.As(err, &notFound) || notFound.WidgetID != "missing" {
		t.Errorf("missing widget error = %v", err)
	}
}

// TestHandler_GetWidgetJSON_MatchesHydration checks that, for every built-in widget type, the data served by
// GET /api/widgets/{id} equals the data of the widget.update payload hydrated for the same widget.
func TestHandler_GetWidgetJSON_MatchesHydration(t *testing.T) {
	t.Parallel()
	widgetsDir := filepath.Join("..", "..", "widgets")
	entries, err := os.ReadDir(widgetsDir)
	if err != nil {
		t.Fatalf("read widgets dir: %v", err)
	}
	chatRaw, err := os.ReadFile(filepath.Join(widgetsDir, "chat-log", "testdata", "sample.json"))
	if err != nil {
		t.Fatalf("read chat sample: %v", err)
	}
	var chatSample map[string]any
	if err := json.Unmarshal(chatRaw, &chatSample); err != nil {
		t.Fatalf("invalid chat sample: %v", err)
	}
	tasksLoaded, _, _ := sampleTasksData(widgetJSONNow)
	samples := map[string]any{
		"calendar-feed":    sampleCalendarData(),
		"calendar-grid":    sampleCalendarData(),
		"chat-log":         chatSample,
		"home-assistant":   map[string]any{},
		"live-view":        map[string]any{"status": "ready"},
		"photo-carousel":   samplePhotoData(),
		"plex-recent":      map[string]any{},
		"spacer":           map[string]any{},
		"tasks":            tasksLoaded,
		"weather-forecast": sampleWeatherData(),
		"web-embed":        map[string]any{},
		"yt-music":         map[string]any{},
	}

	var names []string
	for _, e := range entries {
		if e.IsDir() {
			names = append(names, e.Name())
		}
	}
	for _, name := range names {
		if _, ok := samples[name]; !ok {
			t.Fatalf("built-in widget %q has no hydration sample; add one to this table", name)
		}
	}

	pkgs := loadRepoPackages(t, names...)
	widgets := make([]config.WidgetConfig, 0, len(names))
	for _, name := range names {
		widgets = append(widgets, config.WidgetConfig{ID: "w-" + name, Type: name, Dimensions: []int{2, 2}})
	}
	provider := events.NewInMemoryStateProvider(nil)
	provider.SetSnapshot(&config.Snapshot{Config: &config.Config{Display: config.DisplayConfig{Widgets: widgets}}, Packages: pkgs})
	for _, name := range names {
		provider.SetWidgetState("w-"+name, samples[name], "healthy", "2026-09-26T12:00:00Z")
	}

	engine := render.NewEngine(&mockResolver{packages: pkgs}, provider, render.WithNowFunc(func() time.Time { return widgetJSONNow }))
	handler := render.NewHandler(engine, &mockResolver{packages: pkgs})

	hydrated := map[string]any{}
	for _, ev := range events.BuildHydrationBatch(provider, events.NewIDGenerator(), widgetJSONNow) {
		if ev.Type != events.EventWidgetUpdate {
			continue
		}
		var payload struct {
			WidgetID string `json:"widget_id"`
			Data     any    `json:"data"`
		}
		if err := json.Unmarshal(ev.Data, &payload); err != nil {
			t.Fatalf("invalid widget.update payload: %v", err)
		}
		hydrated[payload.WidgetID] = payload.Data
	}

	for _, name := range names {
		id := "w-" + name
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			rec := httptest.NewRecorder()
			handler.GetWidgetJSON(rec, httptest.NewRequest(http.MethodGet, "/api/widgets/"+id, nil), id)
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d: %s", rec.Code, rec.Body.String())
			}
			resp := decodeWidgetJSON(t, rec.Body.Bytes())
			want, ok := hydrated[id]
			if !ok {
				t.Fatalf("no widget.update hydration payload for %s", id)
			}
			if !reflect.DeepEqual(resp.Data, want) {
				t.Errorf("data mismatch\n json: %v\n hydr: %v", resp.Data, want)
			}
		})
	}
}
