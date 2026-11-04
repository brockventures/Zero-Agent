package render_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/chatlog"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
	"github.com/azylman/mirrormere/internal/provider"
	"github.com/azylman/mirrormere/internal/render"
	"github.com/azylman/mirrormere/internal/tasks"
	"github.com/azylman/mirrormere/internal/widget"
)

type mockSnapshotProvider struct {
	snapshot           *config.Snapshot
	states             map[string]mockState
	status             config.Status
	fallbackTimestamps map[string]string
}

type mockState struct {
	data      any
	state     string
	timestamp string
}

func (m *mockSnapshotProvider) CurrentSnapshot() *config.Snapshot { return m.snapshot }

func (m *mockSnapshotProvider) GetWidgetState(widgetID string) (any, string, string, bool) {
	if m.states == nil {
		return nil, "", "", false
	}
	st, ok := m.states[widgetID]
	return st.data, st.state, st.timestamp, ok
}

func (m *mockSnapshotProvider) CurrentStatus() config.Status { return m.status }

func (m *mockSnapshotProvider) GetWidgetTimestamp(widgetID string) (string, bool) {
	if m.fallbackTimestamps == nil {
		return "", false
	}
	ts, ok := m.fallbackTimestamps[widgetID]
	return ts, ok
}

type mockResolver struct {
	packages map[string]*domain.Package
}

func (m *mockResolver) LoadPackage(widgetType string) (*domain.Package, error) {
	if m.packages != nil {
		if pkg, ok := m.packages[widgetType]; ok {
			return pkg, nil
		}
	}
	return nil, errors.New("package not found")
}

func createTestPackage(t *testing.T, dir, widgetType, viewContent string) *domain.Package {
	t.Helper()
	pkgDir := filepath.Join(dir, widgetType)
	viewsDir := filepath.Join(pkgDir, "views")
	assetsDir := filepath.Join(pkgDir, "assets")
	if err := os.MkdirAll(viewsDir, 0o755); err != nil {
		t.Fatalf("failed to create views dir: %v", err)
	}
	if err := os.MkdirAll(assetsDir, 0o755); err != nil {
		t.Fatalf("failed to create assets dir: %v", err)
	}
	manifestPath := filepath.Join(pkgDir, "manifest.yaml")
	manifestContent := "id: " + widgetType + "\nversion: 1.0.0\nname: Test " + widgetType + "\ndescription: Test widget\nauthor: Tester\nentrypoint: views/widget.html\ndefault_dimensions:\n  cols: 2\n  rows: 1\n"
	if err := os.WriteFile(manifestPath, []byte(manifestContent), 0o644); err != nil {
		t.Fatalf("failed to write manifest: %v", err)
	}
	viewPath := filepath.Join(viewsDir, "widget.html")
	if err := os.WriteFile(viewPath, []byte(viewContent), 0o644); err != nil {
		t.Fatalf("failed to write view: %v", err)
	}
	if err := os.WriteFile(filepath.Join(assetsDir, "icon.svg"), []byte("<svg>icon</svg>"), 0o644); err != nil {
		t.Fatalf("failed to write asset: %v", err)
	}
	return &domain.Package{
		Type: widgetType, Source: "builtin", Dir: pkgDir, ManifestPath: manifestPath, ViewPath: viewPath, AssetsDir: assetsDir,
		Manifest: domain.WidgetManifest{Name: widgetType, Version: "1.0.0", Provider: "http", DefaultDimensions: domain.NewDimension(2, 1)},
	}
}

func sampleWeatherData() provider.WeatherSnapshot {
	return provider.WeatherSnapshot{
		Current: provider.WeatherCurrent{
			Temperature: 72.4, FeelsLike: 70.5, Humidity: 45, WindSpeed: 5.5, PrecipProbMax: 10,
			Units: provider.WeatherUnits{Temperature: "°F", WindSpeed: "mph"}, ConditionCode: 1, ConditionText: "Mainly Clear", Icon: "weather-partly-cloudy",
		},
		Hourly: []provider.WeatherHourly{{Time: "2026-09-26T12:00:00Z", Temp: 72.4, PrecipProb: 0, Icon: "weather-partly-cloudy"}},
		Daily:  []provider.WeatherDaily{{Date: "2026-09-26", TempMax: 75.0, TempMin: 55.0, PrecipProbMax: 10, ConditionText: "Mainly Clear", Icon: "weather-partly-cloudy"}},
	}
}

func sampleCalendarData() provider.CalendarSnapshot {
	return provider.CalendarSnapshot{
		LastSync: "2026-09-26T12:00:00Z", SyncStatus: "ok",
		Events: []provider.CalendarEvent{
			{ID: "evt_past_86d", CalendarName: "Old Calendar", Color: "#ef4444", Title: "Ancient Practice 86 Days Ago", Start: "2026-07-02T10:00:00Z", End: "2026-07-02T11:00:00Z"},
			{ID: "evt_1", CalendarName: "Family Calendar", Color: "#3b82f6", Title: "Soccer Practice", Start: "2026-09-26T18:30:00Z", End: "2026-09-26T19:30:00Z", Location: "Community Park Field 2", Description: "Bring soccer balls and water bottles"},
			{ID: "evt_2", CalendarName: "Home", Color: "#10b981", Title: "Trash & Recycling", Start: "2026-09-27", End: "2026-09-27", AllDay: true},
		},
	}
}

func sampleTasksData(now time.Time) (tasks.TasksSnapshot, tasks.TasksSnapshot, tasks.TasksSnapshot) {
	alex := "Alex"
	dueDate := now.Add(24 * time.Hour).Format(time.RFC3339)
	loaded := tasks.TasksSnapshot{
		List: tasks.List{ID: "groceries", Name: "Groceries", Source: "todoist", CreatedAt: now, UpdatedAt: now},
		Items: []tasks.ListItem{
			{ID: "i1", ListID: "groceries", Title: "Oat Milk", Done: false, Section: "Dairy", Position: 0, Assignee: &alex, DueDate: &dueDate, CreatedAt: now, UpdatedAt: now},
			{ID: "i2", ListID: "groceries", Title: "Coffee Beans", Done: true, Section: "Pantry", Position: 1, CreatedAt: now, UpdatedAt: now},
		},
	}
	empty := tasks.TasksSnapshot{List: tasks.List{ID: "empty-list", Name: "Chores", Source: "gtasks", CreatedAt: now, UpdatedAt: now}, Items: []tasks.ListItem{}}
	unnamed := tasks.TasksSnapshot{
		List:  tasks.List{ID: "unnamed-list", CreatedAt: now, UpdatedAt: now},
		Items: []tasks.ListItem{{ID: "u1", ListID: "unnamed-list", Title: "Thing to do", Done: false, CreatedAt: now, UpdatedAt: now}},
	}
	return loaded, empty, unnamed
}

func samplePhotoData() provider.PhotoSnapshot {
	return provider.PhotoSnapshot{
		AlbumName: "Family Memories", AlbumURL: "https://photos.app.goo.gl/TEST", TotalPhotos: 1,
		Photos: []provider.PhotoItem{{ID: "p1", URL: "https://lh3.googleusercontent.com/pw/TEST", AspectRatio: 1.33}},
	}
}

func TestEngine_RenderWidget(t *testing.T) {
	t.Parallel()
	tmpDir := t.TempDir()
	viewTmpl := `<div id="{{.ID}}" class="widget-{{.Type}}" data-theme="{{.Theme}}" data-online="{{.Online}}">
  <span class="state">{{.State}}</span><span class="dim">{{.Dimensions.Cols}}x{{.Dimensions.Rows}}</span>
  <span class="origin">{{index .Origin 0}},{{index .Origin 1}}</span><span class="assets">{{.Assets}}</span>
  <span class="data">{{.Data.temperature}}</span><span class="config-title">{{.Config.title}}</span>
  <span class="author">{{.Config.author}}</span><span class="show-passed">{{.Config.show_passed}}</span>
  <span class="bypass-cache">{{.Config.bypass_cache}}</span><span class="max-tokens">{{.Config.max_tokens}}</span>
  <span class="timestamp">{{.Timestamp}}</span><span class="count">{{.Config.count}}</span>
</div>`

	pkg := createTestPackage(t, tmpDir, "generic", viewTmpl)
	origConfig := map[string]any{"title": "Original", "count": 1}

	snap := &config.Snapshot{
		Config: &config.Config{
			Display: config.DisplayConfig{
				Widgets: []config.WidgetConfig{
					{
						ID: "w-placed", Type: "generic",
						Config: map[string]any{
							"title": "Living Room", "author": "J.R.R. Tolkien", "show_passed": true, "bypass_cache": true,
							"max_tokens": 500, "token_env": "SECRET_KEY", "password": "secret123",
							"nested": map[string]any{"auth_token": "bearer123", "safe_label": "Sensors"},
						},
					},
					{ID: "w-cached", Type: "generic"},
					{ID: "w-uncached", Type: "generic"},
					{ID: "w-fallback-ts", Type: "generic"},
					{ID: "w-unplaced-dims", Type: "generic", Dimensions: []int{2, 1}},
					{ID: "w-unplaced-nodims", Type: "generic"},
					{ID: "w-offline", Type: "generic"},
					{ID: "w-overrides", Type: "generic", Config: origConfig},
				},
			},
		},
		Layout:   &layout.Layout{Screens: []layout.Screen{{Index: 0, Widgets: []layout.PlacedWidget{{WidgetID: "w-placed", Type: "generic", Origin: [2]int{2, 1}, Dimensions: domain.NewDimension(3, 1)}}}}},
		Packages: map[string]*domain.Package{"generic": pkg},
	}

	states := map[string]mockState{
		"w-placed":      {data: map[string]any{"temperature": 72.5}, state: "healthy", timestamp: "2026-09-25T11:00:00Z"},
		"w-cached":      {data: map[string]any{"temperature": 42.0}, state: "healthy", timestamp: "2026-09-25T11:00:00Z"},
		"w-uncached":    {data: map[string]any{"temperature": 42.0}, state: "healthy", timestamp: ""},
		"w-fallback-ts": {data: map[string]any{"temperature": 42.0}, state: "healthy", timestamp: ""},
		"w-overrides":   {data: map[string]any{}, state: "healthy"},
	}
	fallbackTS := map[string]string{"w-fallback-ts": "2026-09-25T10:30:00Z"}
	resolver := &mockResolver{packages: map[string]*domain.Package{"generic": pkg}}
	fixedNow := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)

	tests := []struct {
		name            string
		widgetID        string
		overrides       map[string]any
		providerStatus  config.Status
		wantContains    []string
		wantNotContains []string
		checkUnmutated  bool
	}{
		{
			name:     "placed widget full rendering",
			widgetID: "w-placed",
			wantContains: []string{
				`id="w-placed"`, `class="widget-generic"`, `data-theme="dark"`, `data-online=" true "`,
				`class="state">healthy<`, `class="dim">3x1<`, `class="origin">2,1<`, `class="assets">widget-types/generic/assets<`,
				`class="data">72.5<`, `class="config-title">Living Room<`, `class="author">J.R.R. Tolkien<`,
				`class="show-passed">true<`, `class="bypass-cache">true<`, `class="max-tokens">500<`, `<span class="timestamp">2026-09-25T11:00:00Z</span>`,
			},
			wantNotContains: []string{"SECRET_KEY", "secret123", "bearer123"},
		},
		{name: "cached timestamp staleness", widgetID: "w-cached", wantContains: []string{`<span class="timestamp">2026-09-25T11:00:00Z</span>`}, wantNotContains: []string{"2026-09-25T12:00:00Z"}},
		{name: "uncached timestamp staleness", widgetID: "w-uncached", wantContains: []string{`<span class="timestamp"></span>`}, wantNotContains: []string{"2026-09-25T12:00:00Z"}},
		{name: "fallback GetWidgetTimestamp interface", widgetID: "w-fallback-ts", wantContains: []string{`<span class="timestamp">2026-09-25T10:30:00Z</span>`}},
		{name: "unplaced widget explicit dimensions", widgetID: "w-unplaced-dims", wantContains: []string{`<span class="dim">2x1</span>`, `<span class="origin">0,0</span>`}},
		{name: "unplaced widget default dimensions", widgetID: "w-unplaced-nodims", wantContains: []string{`<span class="dim">2x1</span>`, `<span class="origin">0,0</span>`}},
		{name: "offline status when config error", widgetID: "w-offline", providerStatus: config.Status{ConfigStatus: config.ConfigStatusError}, wantContains: []string{`data-online=" false "`}},
		{name: "overrides without mutating snapshot", widgetID: "w-overrides", overrides: map[string]any{"title": "Overridden", "count": 42}, wantContains: []string{`<span class="config-title">Overridden</span>`, `<span class="count">42</span>`}, checkUnmutated: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			status := config.Status{ConfigStatus: config.ConfigStatusOK}
			if tc.providerStatus.ConfigStatus != "" {
				status = tc.providerStatus
			}
			p := &mockSnapshotProvider{snapshot: snap, states: states, status: status, fallbackTimestamps: fallbackTS}
			eng := render.NewEngine(resolver, p, render.WithNowFunc(func() time.Time { return fixedNow }))
			htmlBytes, err := eng.RenderWidgetWithOverrides(context.Background(), tc.widgetID, "", tc.overrides)
			if err != nil {
				t.Fatalf("RenderWidgetWithOverrides failed: %v", err)
			}
			out := string(htmlBytes)
			for _, want := range tc.wantContains {
				if !strings.Contains(out, want) {
					t.Errorf("output missing %q: %s", want, out)
				}
			}
			for _, notWant := range tc.wantNotContains {
				if strings.Contains(out, notWant) {
					t.Errorf("output unexpectedly contains %q: %s", notWant, out)
				}
			}
			if tc.checkUnmutated && (origConfig["title"] != "Original" || origConfig["count"] != 1) {
				t.Errorf("original config mutated: %v", origConfig)
			}
		})
	}
}

func TestEngine_RenderWidget_Errors(t *testing.T) {
	t.Parallel()
	tmpDir := t.TempDir()
	syntaxPkg := createTestPackage(t, tmpDir, "syntax-pkg", `{{.InvalidField | nonExistentFunc}}`)
	missingFilePkg := createTestPackage(t, tmpDir, "missing-file-pkg", `test`)
	_ = os.Remove(missingFilePkg.ViewPath)
	execErrPkg := createTestPackage(t, tmpDir, "exec-err-pkg", `{{.Data.NonExistentField.SubField}}`)

	pkgs := map[string]*domain.Package{"syntax-pkg": syntaxPkg, "missing-file-pkg": missingFilePkg, "exec-err-pkg": execErrPkg}
	snap := &config.Snapshot{
		Config: &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{
			{ID: "w-syntax", Type: "syntax-pkg"},
			{ID: "w-missing-file", Type: "missing-file-pkg"},
			{ID: "w-exec-err", Type: "exec-err-pkg"},
			{ID: "w-missing-pkg", Type: "nonexistent"},
		}}},
		Packages: pkgs,
	}
	baseProvider := &mockSnapshotProvider{snapshot: snap, states: map[string]mockState{"w-exec-err": {data: 12345, state: "healthy"}}}
	resolver := &mockResolver{packages: pkgs}

	tests := []struct {
		name        string
		widgetID    string
		nilProvider bool
		nilSnapshot bool
		wantErr     string
		isNotFound  bool
	}{
		{name: "empty widget ID", widgetID: "", wantErr: "widget '' not found in active configuration", isNotFound: true},
		{name: "nil provider", widgetID: "w-syntax", nilProvider: true, wantErr: "state snapshot provider is unconfigured"},
		{name: "nil snapshot", widgetID: "w-syntax", nilSnapshot: true, wantErr: "no active configuration snapshot available"},
		{name: "unknown widget ID", widgetID: "unknown-widget", wantErr: "widget 'unknown-widget' not found in active configuration", isNotFound: true},
		{name: "missing package in resolver", widgetID: "w-missing-pkg", wantErr: "failed to load package for widget type"},
		{name: "template syntax error", widgetID: "w-syntax", wantErr: "template syntax error"},
		{name: "missing template file on disk", widgetID: "w-missing-file", wantErr: "no such file or directory"},
		{name: "template execution error", widgetID: "w-exec-err", wantErr: "failed to execute template"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var p render.StateSnapshotProvider = baseProvider
			if tc.nilProvider {
				p = nil
			} else if tc.nilSnapshot {
				p = &mockSnapshotProvider{snapshot: nil}
			}
			_, err := render.NewEngine(resolver, p).RenderWidget(context.Background(), tc.widgetID)
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("err = %v, want substring %q", err, tc.wantErr)
			}
			if tc.isNotFound {
				var notFound render.WidgetNotFoundError
				if !errors.As(err, &notFound) || notFound.Error() != tc.wantErr {
					t.Errorf("expected WidgetNotFoundError(%q), got %T: %v", tc.wantErr, err, err)
				}
			}
		})
	}
}

func TestEngine_CacheAndInvalidation(t *testing.T) {
	t.Parallel()
	tmpDir := t.TempDir()
	pkg := createTestPackage(t, tmpDir, "cached-w", `<span>v1: {{.ID}}</span>`)
	snap := &config.Snapshot{
		Config:   &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{{ID: "instance-1", Type: "cached-w"}}}},
		Packages: map[string]*domain.Package{"cached-w": pkg},
	}
	engine := render.NewEngine(&mockResolver{packages: map[string]*domain.Package{"cached-w": pkg}}, &mockSnapshotProvider{snapshot: snap})

	out1, err := engine.RenderWidget(context.Background(), "instance-1")
	if err != nil || !strings.Contains(string(out1), "v1: instance-1") {
		t.Fatalf("first render: err=%v, out=%s", err, out1)
	}
	if err := os.WriteFile(pkg.ViewPath, []byte(`<span>v2: {{.ID}}</span>`), 0o644); err != nil {
		t.Fatalf("failed to update template: %v", err)
	}
	if out2, err := engine.RenderWidget(context.Background(), "instance-1"); err != nil || !strings.Contains(string(out2), "v1: instance-1") {
		t.Errorf("expected cached v1 output, got err=%v, out=%s", err, out2)
	}
	engine.Invalidate("cached-w")
	if out3, err := engine.RenderWidget(context.Background(), "instance-1"); err != nil || !strings.Contains(string(out3), "v2: instance-1") {
		t.Errorf("expected updated v2 output after Invalidate, got err=%v, out=%s", err, out3)
	}
}

func TestSanitizeConfig(t *testing.T) {
	t.Parallel()
	if len(render.SanitizeConfig(nil)) != 0 {
		t.Fatalf("expected empty map for nil input")
	}
	got := render.SanitizeConfig(map[string]any{
		"title": "Safe", "author": "J.R.R. Tolkien", "authors": []any{"Frodo", "Sam"}, "show_passed": true,
		"passengers": 4, "bypass_cache": true, "compass": "north", "max_tokens": 100,
		"token": "leak1", "secret": "leak2", "password": "leak3", "api_key": "leak4", "apikey": "leak5",
		"auth": "leak6", "auth_token": "leak7", "access_token": "leak8", "token_env": "TOKEN_VAR", "my_env": "MY_VAR",
		"list":   []any{"simple-string", map[string]any{"safe_item": "ok", "show_passed": false, "author": "Nested Author", "secret": "hide", "nested_env": "ENV_VAR"}},
		"nested": map[string]any{"public": "show", "bypass_cache": true, "secret": "hide", "sub_token_env": "SUB_VAR"},
	})
	for k, wantVal := range map[string]any{"title": "Safe", "author": "J.R.R. Tolkien", "show_passed": true, "passengers": 4, "bypass_cache": true, "compass": "north", "max_tokens": 100} {
		if got[k] != wantVal {
			t.Errorf("key %q = %v, want %v", k, got[k], wantVal)
		}
	}
	for _, k := range []string{"token", "secret", "password", "api_key", "apikey", "auth", "auth_token", "access_token", "token_env", "my_env"} {
		if _, exists := got[k]; exists {
			t.Errorf("sensitive key %q leaked", k)
		}
	}
	subMap := got["list"].([]any)[1].(map[string]any)
	if subMap["safe_item"] != "ok" || subMap["show_passed"] != false || subMap["author"] != "Nested Author" || subMap["secret"] != nil || subMap["nested_env"] != nil {
		t.Errorf("nested slice map sanitization failed: %v", subMap)
	}
	nestedMap := got["nested"].(map[string]any)
	if nestedMap["public"] != "show" || nestedMap["bypass_cache"] != true || nestedMap["secret"] != nil || nestedMap["sub_token_env"] != nil {
		t.Errorf("nested map sanitization failed: %v", nestedMap)
	}
}

func TestEngine_BuiltinWidgetPackages(t *testing.T) {
	t.Parallel()
	repoWidgetsDir := filepath.Join("..", "..", "widgets")
	loader := widget.NewLoader(repoWidgetsDir, t.TempDir())
	packages := make(map[string]*domain.Package)
	for _, name := range []string{"weather-forecast", "calendar-feed", "tasks", "photo-carousel", "chat-log"} {
		pkg, err := loader.LoadPackage(name)
		if err != nil {
			t.Fatalf("failed to load package %q: %v", name, err)
		}
		packages[name] = pkg
	}

	raw, err := os.ReadFile(filepath.Join(repoWidgetsDir, "chat-log", "testdata", "sample.json"))
	if err != nil {
		t.Fatalf("failed to read sample payload: %v", err)
	}
	var chatSample map[string]any
	if err := json.Unmarshal(raw, &chatSample); err != nil {
		t.Fatalf("invalid sample payload: %v", err)
	}

	fixedNow := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	weatherData := sampleWeatherData()
	calData := sampleCalendarData()
	emptyCalData := provider.CalendarSnapshot{LastSync: "2026-09-26T12:00:00Z", SyncStatus: "ok", Events: []provider.CalendarEvent{}}
	tasksLoaded, tasksEmpty, tasksUnnamed := sampleTasksData(fixedNow)
	photoData := samplePhotoData()

	tests := []struct {
		name            string
		widgetType      string
		dims            []int
		cfg             map[string]any
		state           mockState
		nilResolver     bool
		wantContains    []string
		wantNotContains []string
	}{
		{
			name: "weather-forecast loaded without name", widgetType: "weather-forecast", dims: []int{2, 1},
			state:           mockState{data: weatherData, state: "healthy", timestamp: "2026-09-26T12:00:00Z"},
			wantContains:    []string{"72", "Mainly Clear", "weather-partly-cloudy", "weather-meta-precip", "Precipitation: 10%", "10%", "weather-meta-humidity", "Humidity: 45%", "weather-meta-wind", "Now", "weather-micro-stats", "weather-icon-badge", "weather-condition-text", "weather-temp-range", "H: 75° • L: 55°"},
			wantNotContains: []string{"💧", "🍃", "📍", "weather-location-name"},
		},
		{name: "weather-forecast loading", widgetType: "weather-forecast", dims: []int{2, 1}, state: mockState{data: nil, state: "healthy"}, wantContains: []string{"Loading weather forecast..."}},
		{name: "weather-forecast named", widgetType: "weather-forecast", dims: []int{2, 1}, cfg: map[string]any{"name": "Home"}, state: mockState{data: weatherData, state: "healthy", timestamp: "2026-09-26T12:00:00Z"}, wantContains: []string{"weather-location-name", "Home"}},
		{name: "weather-forecast loc-named", widgetType: "weather-forecast", dims: []int{2, 1}, cfg: map[string]any{"location_name": "Ayda's School"}, state: mockState{data: weatherData, state: "healthy", timestamp: "2026-09-26T12:00:00Z"}, wantContains: []string{"weather-location-name", "Ayda"}},
		{name: "weather-forecast loading named", widgetType: "weather-forecast", dims: []int{2, 1}, cfg: map[string]any{"name": "Home"}, state: mockState{data: nil, state: "healthy"}, wantContains: []string{"Home", "Loading weather forecast..."}},
		{
			name: "calendar-feed loaded", widgetType: "calendar-feed", dims: []int{4, 2},
			cfg:             map[string]any{"title": "Family Schedule", "calendars": []any{map[string]any{"name": "Active Family", "color": "#3b82f6", "enabled": true}, map[string]any{"name": "Disabled Work", "color": "#ef4444", "enabled": false}}},
			state:           mockState{data: calData, state: "healthy", timestamp: "2026-09-26T12:00:00Z"},
			wantContains:    []string{"Soccer Practice", "Community Park Field 2", "Family Calendar", "Trash", "All Day", "Family Schedule", "Active Family", "1 Calendar"},
			wantNotContains: []string{"Ancient Practice", "Bring soccer balls and water bottles", "Upcoming", "Disabled Work"},
		},
		{name: "calendar-feed empty", widgetType: "calendar-feed", dims: []int{4, 2}, state: mockState{data: emptyCalData, state: "healthy", timestamp: "2026-09-26T12:00:00Z"}, wantContains: []string{"No upcoming events", "Upcoming"}, wantNotContains: []string{"calendar-header-chips"}},
		{name: "calendar-feed loading", widgetType: "calendar-feed", dims: []int{4, 2}, state: mockState{data: nil, state: "healthy"}, wantContains: []string{"Loading calendar"}},
		{
			name: "tasks loaded", widgetType: "tasks", dims: []int{3, 2}, state: mockState{data: tasksLoaded, state: "healthy", timestamp: "2026-09-26T12:00:00Z"},
			wantContains:    []string{"Groceries", "Oat Milk", "Coffee Beans", "task-item-done", "@Alex", "1 to do", "tasks-sparkle-icon", "tasks-icon-box", "tasks-progress"},
			wantNotContains: []string{"tasks-source-badge", "✅"},
		},
		{name: "tasks empty", widgetType: "tasks", dims: []int{3, 2}, state: mockState{data: tasksEmpty, state: "healthy", timestamp: "2026-09-26T12:00:00Z"}, wantContains: []string{"All tasks completed"}},
		{name: "tasks loading", widgetType: "tasks", dims: []int{3, 2}, state: mockState{data: nil, state: "healthy"}, wantContains: []string{"Loading checklist..."}},
		{name: "tasks unnamed", widgetType: "tasks", dims: []int{3, 2}, state: mockState{data: tasksUnnamed, state: "healthy", timestamp: "2026-09-26T12:00:00Z"}, wantContains: []string{"Checklist"}},
		{
			name: "photo-carousel contain", widgetType: "photo-carousel", dims: []int{3, 2}, state: mockState{data: photoData, state: "healthy"}, nilResolver: true,
			wantContains:    []string{`data-fit="contain"`, "=w3840-h2160", `photo-backdrop-current" src="https://lh3.googleusercontent.com/pw/TEST=w96-h54-c"`, `{"album_name":"Family Memories"`},
			wantNotContains: []string{`<script type="application/json" class="photo-carousel-data">"`},
		},
		{name: "chat-log loaded", widgetType: "chat-log", dims: []int{3, 2}, state: mockState{data: chatSample, state: "healthy", timestamp: "2026-01-02T18:42:00Z"}, wantContains: []string{"Kitchen assistant", "18:42", "sam", "What is on the calendar tomorrow?", "Dentist at 9:00 and soccer practice at 4:30.", "chat-log-human", "chat-log-agent"}},
		{name: "chat-log empty", widgetType: "chat-log", dims: []int{3, 2}, state: mockState{data: map[string]any{}, state: "healthy", timestamp: "2026-01-02T18:42:00Z"}, wantContains: []string{"No messages yet"}},
		{name: "chat-log escape", widgetType: "chat-log", dims: []int{3, 2}, state: mockState{data: map[string]any{"messages": []any{map[string]any{"role": "agent", "text": "<script>alert(1)</script>"}}}, state: "healthy", timestamp: "2026-01-02T18:42:00Z"}, wantContains: []string{"Chat"}, wantNotContains: []string{"<script>alert"}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			widgetID := "w-" + tc.widgetType
			snap := &config.Snapshot{
				Config:   &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{{ID: widgetID, Type: tc.widgetType, Dimensions: tc.dims, Config: tc.cfg}}}},
				Packages: packages,
			}
			var res render.PackageResolver
			if !tc.nilResolver {
				res = &mockResolver{packages: packages}
			}
			eng := render.NewEngine(res, &mockSnapshotProvider{snapshot: snap, states: map[string]mockState{widgetID: tc.state}}, render.WithNowFunc(func() time.Time { return fixedNow }))
			htmlBytes, err := eng.RenderWidget(context.Background(), widgetID)
			if err != nil {
				t.Fatalf("RenderWidget failed: %v", err)
			}
			out := string(htmlBytes)
			for _, want := range tc.wantContains {
				if !strings.Contains(out, want) {
					t.Errorf("output missing %q: %s", want, out)
				}
			}
			for _, notWant := range tc.wantNotContains {
				if strings.Contains(out, notWant) {
					t.Errorf("output unexpectedly contains %q: %s", notWant, out)
				}
			}
		})
	}
}

func TestChatLogWidget_RendersFromProviderPerNode(t *testing.T) {
	t.Parallel()
	loader := widget.NewLoader(filepath.Join("..", "..", "widgets"), t.TempDir())
	pkg, err := loader.LoadPackage("chat-log")
	if err != nil || pkg.Manifest.Provider != "chat-log" {
		t.Fatalf("load chat-log package: err=%v, provider=%v", err, pkg)
	}

	store := chatlog.NewStore()
	store.Add(chatlog.NodeKey("kitchen"), chatlog.Message{Role: chatlog.RoleHuman, Author: "sam", Text: "kitchen question"})
	store.Add(chatlog.NodeKey("kitchen"), chatlog.Message{Role: chatlog.RoleAgent, Text: "kitchen answer"})
	store.Add(chatlog.NodeKey("office"), chatlog.Message{Role: chatlog.RoleHuman, Text: "office question <b>", At: time.Now().Add(time.Second)})

	snap := &config.Snapshot{
		Config:   &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{{ID: "chat", Type: "chat-log", Dimensions: []int{3, 2}, Config: map[string]any{"title": "Home"}}}}},
		Packages: map[string]*domain.Package{"chat-log": pkg},
	}
	res := &mockResolver{packages: map[string]*domain.Package{"chat-log": pkg}}
	engine := render.NewEngine(res, &mockSnapshotProvider{snapshot: snap, states: map[string]mockState{}},
		render.WithNodeDataFunc(func(widgetID, nodeID string) (any, bool) {
			if widgetID != "chat" {
				return nil, false
			}
			return store.View(widgetID, nodeID), true
		}),
	)

	for _, tc := range []struct {
		node    string
		want    []string
		notWant string
	}{
		{node: "kitchen", want: []string{"Home", "kitchen question", "kitchen answer", "sam", "chat-log-human", "chat-log-agent"}, notWant: "office question"},
		{node: "office", want: []string{"office question &lt;b&gt;"}, notWant: "kitchen question"},
		{node: "", want: []string{"office question"}},
		{node: "garage", want: []string{"No messages yet"}},
	} {
		out, err := engine.RenderWidgetForNode(context.Background(), "chat", tc.node)
		if err != nil {
			t.Fatalf("render node %q: %v", tc.node, err)
		}
		for _, w := range tc.want {
			if !strings.Contains(string(out), w) {
				t.Errorf("node %q missing %q", tc.node, w)
			}
		}
		if tc.notWant != "" && strings.Contains(string(out), tc.notWant) {
			t.Errorf("node %q leaked %q", tc.node, tc.notWant)
		}
	}

	h := render.NewHandler(engine, res)
	rec := httptest.NewRecorder()
	h.GetWidgetRender(rec, httptest.NewRequest(http.MethodGet, "/api/widgets/chat/render?node=kitchen", nil), "chat")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "kitchen answer") {
		t.Errorf("handler render: %d %s", rec.Code, rec.Body.String())
	}
}
