package display_test

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/display"
)

func TestGetDisplay_RoutingAndResolution(t *testing.T) {
	t.Parallel()
	tempDir := t.TempDir()
	primPath := filepath.Join(tempDir, "display.html")
	localPath := filepath.Join(tempDir, "local.html")
	_ = os.WriteFile(primPath, []byte("<h1>primary</h1>"), 0o644)
	_ = os.WriteFile(localPath, []byte("<h1>local</h1>"), 0o644)

	tests := []struct {
		name     string
		method   string
		kind     string
		wantCode int
		wantSub  string
		wantHead bool
	}{
		{"options", http.MethodOptions, "default", http.StatusNoContent, "", false},
		{"post_not_allowed", http.MethodPost, "default", http.StatusMethodNotAllowed, `"status":"error"`, false},
		{"head_ok", http.MethodHead, "content", http.StatusOK, "", true},
		{"primary_path", http.MethodGet, "primary", http.StatusOK, "<h1>primary</h1>", false},
		{"local_fallback", http.MethodGet, "local", http.StatusOK, "<h1>local</h1>", false},
		{"template_content", http.MethodGet, "content", http.StatusOK, "<h1>content</h1>", false},
		{"malformed_syntax", http.MethodGet, "malformed", http.StatusOK, "<div>{{ bad", false},
		{"not_found", http.MethodGet, "missing", http.StatusNotFound, `"status":"error"`, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var h *display.Handler
			legacyOpt := display.WithUIProvider(func() string { return "legacy" })
			switch tc.kind {
			case "primary":
				h = display.NewHandler(legacyOpt, display.WithTemplatePath(primPath), display.WithLocalTemplatePath(""))
			case "local":
				h = display.NewHandler(legacyOpt, display.WithTemplatePath("/no/such/file"), display.WithLocalTemplatePath(localPath))
			case "content":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent([]byte("<h1>content</h1>")))
			case "malformed":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent([]byte("<div>{{ bad")))
			case "missing":
				h = display.NewHandler(legacyOpt, display.WithTemplatePath("/no/such/file"), display.WithLocalTemplatePath("/no/such/local"))
			default:
				h = display.NewHandler()
			}
			rec := httptest.NewRecorder()
			h.GetDisplay(rec, httptest.NewRequest(tc.method, "/display", nil))
			if rec.Code != tc.wantCode {
				t.Fatalf("code: got %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantHead && rec.Body.Len() != 0 {
				t.Fatalf("head body: got %d bytes, want 0", rec.Body.Len())
			}
			if tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub) {
				t.Fatalf("body: got %q, want substring %q", rec.Body.String(), tc.wantSub)
			}
			if tc.method == http.MethodOptions && rec.Header().Get("Access-Control-Allow-Origin") != "*" {
				t.Fatalf("cors header missing: %v", rec.Header())
			}
		})
	}
}

func TestGetDisplay_TemplateDataAndProviders(t *testing.T) {
	t.Parallel()
	tmpl := []byte(`<body data-tz="{{ .Timezone }}" class="{{ if .HideCursor }}cursor-hidden{{ end }}" data-remote="{{ .RemoteURL }}" data-theme="{{ .Theme }}" data-grid="{{ .Grid }}">{{ if .HeaderVideo }}<div id="video" data-id="{{ .HeaderVideo.StreamID }}" data-url="{{ .HeaderVideo.StreamURL }}" data-ctrl="{{ .HeaderVideo.ControlURL }}" data-type="{{ .HeaderVideo.Type }}"></div>{{ end }}</body>`)

	tests := []struct {
		name       string
		mode       string
		path       string
		wantGetter string
		wantSub    string
		wantNotSub string
	}{
		{"default_values", "default", "/display", "UTC", `data-tz="UTC"`, "cursor-hidden"},
		{"static_timezone", "static_tz", "/display", "America/Los_Angeles", `data-tz="America/Los_Angeles"`, ""},
		{"dynamic_tz_setter", "dyn_tz", "/display", "Asia/Tokyo", `data-tz="Europe/London"`, ""},
		{"static_hide_cursor", "static_hide", "/display", "true", "cursor-hidden", ""},
		{"dynamic_hide_cursor", "dyn_hide", "/display", "true", "cursor-hidden", ""},
		{"setter_hide_cursor", "set_hide", "/display", "true", "cursor-hidden", ""},
		{"query_hide_true", "static_unhide", "/display?hide_cursor=true", "false", "cursor-hidden", ""},
		{"query_hide_false", "static_hide", "/display?hide_cursor=false", "true", "", "cursor-hidden"},
		{"static_remote_url", "static_remote", "/display", "http://remote.lan", `data-remote="http://remote.lan"`, ""},
		{"dynamic_remote_setter", "dyn_remote", "/display", "http://setter.lan", `data-remote="http://dyn.lan"`, ""},
		{"static_header_video", "static_video", "/display", "cam1", `data-id="cam1"`, ""},
		{"dynamic_header_video", "dyn_video", "/display", "live-cam", "", ""},
		{"dynamic_theme", "dyn_theme", "/display", "night", `data-theme="night"`, ""},
		{"dynamic_grid_3x6", "dyn_grid", "/display", "3x6", `data-grid="3x6"`, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var h *display.Handler
			legacyOpt := display.WithUIProvider(func() string { return "legacy" })
			switch tc.mode {
			case "static_tz":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithTimezoneProvider(func() string { return "America/Los_Angeles" }))
				if h.Timezone() != tc.wantGetter {
					t.Fatalf("tz getter: got %s, want %s", h.Timezone(), tc.wantGetter)
				}
			case "dyn_tz":
				tz := "Europe/London"
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithTimezoneProvider(func() string { return tz }))
				if h.Timezone() != "Europe/London" {
					t.Fatalf("tz initial: got %s, want Europe/London", h.Timezone())
				}
				tz = tc.wantGetter
				if h.Timezone() != tc.wantGetter {
					t.Fatalf("tz dynamic: got %s, want %s", h.Timezone(), tc.wantGetter)
				}
				tz = "Europe/London"
			case "static_hide":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHideCursorProvider(func() bool { return true }))
				if !h.HideCursor() {
					t.Fatalf("hide getter: expected true")
				}
			case "static_unhide":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHideCursorProvider(func() bool { return false }))
				if h.HideCursor() {
					t.Fatalf("hide getter: expected false")
				}
			case "dyn_hide":
				val := false
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHideCursorProvider(func() bool { return val }))
				val = true
				if !h.HideCursor() {
					t.Fatalf("dyn hide getter: expected true")
				}
			case "set_hide":
				val := false
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHideCursorProvider(func() bool { return val }))
				val = true
				if !h.HideCursor() {
					t.Fatalf("set hide getter: expected true")
				}
			case "static_remote":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithRemoteURLProvider(func() string { return "http://remote.lan" }))
				if h.RemoteURL() != tc.wantGetter {
					t.Fatalf("remote getter: got %s, want %s", h.RemoteURL(), tc.wantGetter)
				}
			case "dyn_remote":
				url := "http://dyn.lan"
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithRemoteURLProvider(func() string { return url }))
				if h.RemoteURL() != "http://dyn.lan" {
					t.Fatalf("remote initial: got %s, want http://dyn.lan", h.RemoteURL())
				}
				url = tc.wantGetter
				if h.RemoteURL() != tc.wantGetter {
					t.Fatalf("remote dynamic: got %s, want %s", h.RemoteURL(), tc.wantGetter)
				}
				url = "http://dyn.lan"
			case "static_video":
				vData := &display.HeaderVideoTemplateData{Enabled: true, StreamID: "cam1", StreamURL: "http://cam.lan", ControlURL: "http://ctrl.lan", Type: "webrtc"}
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHeaderVideoProvider(func() *display.HeaderVideoTemplateData { return vData }))
				if h.HeaderVideo() != vData {
					t.Fatalf("video getter mismatch")
				}
			case "dyn_video":
				vData := &display.HeaderVideoTemplateData{Enabled: true, StreamID: "live-cam"}
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithHeaderVideoProvider(func() *display.HeaderVideoTemplateData { return vData }))
				if h.HeaderVideo().StreamID != tc.wantGetter {
					t.Fatalf("dyn video getter mismatch")
				}
			case "dyn_theme":
				th := "night"
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithThemeProvider(func() string { return th }))
				if h.Theme() != tc.wantGetter {
					t.Fatalf("theme getter: got %s, want %s", h.Theme(), tc.wantGetter)
				}
			case "dyn_grid":
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithGridProvider(func() string { return "3x6" }))
				if h.Grid() != tc.wantGetter {
					t.Fatalf("grid getter: got %s, want %s", h.Grid(), tc.wantGetter)
				}
			default:
				h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl))
				if h.Timezone() != "UTC" || h.HideCursor() || h.RemoteURL() != "" || h.Theme() != "day" || h.HeaderVideo() != nil {
					t.Fatalf("defaults mismatch")
				}
			}
			rec := httptest.NewRecorder()
			h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, tc.path, nil))
			if tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub) {
				t.Fatalf("body missing %q: %s", tc.wantSub, rec.Body.String())
			}
			if tc.wantNotSub != "" && strings.Contains(rec.Body.String(), tc.wantNotSub) {
				t.Fatalf("body contains unwanted %q: %s", tc.wantNotSub, rec.Body.String())
			}
		})
	}
}

func TestGetStatic(t *testing.T) {
	t.Parallel()
	tempDir := t.TempDir()
	localDir := t.TempDir()

	files := map[string][]byte{
		"app.js":                []byte("console.log('primary');"),
		"test.css":              []byte(".a { color: red; }"),
		"test.js":               []byte("const x = 1;"),
		"test.svg":              []byte("<svg></svg>"),
		"test.png":              {0x89, 'P', 'N', 'G'},
		"test.jpg":              {0xFF, 0xD8, 0xFF},
		"test.woff2":            []byte("wOF2"),
		"test.html":             []byte("<html>test</html>"),
		"test.bin":              []byte("binary data"),
		"test.custom":           []byte("custom data"),
		"js/calendar_family.js": []byte("console.log('calendar controller');"),
	}
	_ = os.MkdirAll(filepath.Join(tempDir, "js"), 0o755)
	for relPath, data := range files {
		_ = os.WriteFile(filepath.Join(tempDir, relPath), data, 0o644)
	}
	_ = os.WriteFile(filepath.Join(localDir, "theme.css"), []byte("body { background: #000; }"), 0o644)

	h := display.NewHandler(display.WithStaticDir(tempDir), display.WithLocalStaticDir(localDir))
	hFallback := display.NewHandler(display.WithStaticDir(""), display.WithLocalStaticDir(localDir))

	tests := []struct {
		name     string
		method   string
		path     string
		useEmpty bool
		wantCode int
		wantMime string
		wantSub  string
		wantHead bool
	}{
		{"options", http.MethodOptions, "app.js", false, http.StatusNoContent, "", "", false},
		{"post_not_allowed", http.MethodPost, "app.js", false, http.StatusMethodNotAllowed, "", `"status":"error"`, false},
		{"traversal_dotdot", http.MethodGet, "..", false, http.StatusBadRequest, "", "", false},
		{"traversal_dotdot_file", http.MethodGet, "../secret.txt", false, http.StatusBadRequest, "", "", false},
		{"traversal_escape", http.MethodGet, "../../etc/passwd", false, http.StatusBadRequest, "", "", false},
		{"traversal_abs", http.MethodGet, "/etc/passwd", false, http.StatusBadRequest, "", "", false},
		{"traversal_win", http.MethodGet, `\windows\system32`, false, http.StatusBadRequest, "", "", false},
		{"traversal_dot", http.MethodGet, ".", false, http.StatusBadRequest, "", "", false},
		{"traversal_dot_rel", http.MethodGet, "./something", false, http.StatusBadRequest, "", "", false},
		{"primary_static", http.MethodGet, "app.js", false, http.StatusOK, "", "primary", false},
		{"local_fallback", http.MethodGet, "theme.css", true, http.StatusOK, "", "background: #000", false},
		{"head_method", http.MethodHead, "app.js", false, http.StatusOK, "", "", true},
		{"missing_file", http.MethodGet, "missing.txt", false, http.StatusNotFound, "", `"status":"error"`, false},
		{"calendar_alias", http.MethodGet, "js/calendar_grid.js", false, http.StatusOK, "", "calendar controller", false},
		{"mime_css", http.MethodGet, "test.css", false, http.StatusOK, "text/css; charset=utf-8", "", false},
		{"mime_js", http.MethodGet, "test.js", false, http.StatusOK, "text/javascript; charset=utf-8", "", false},
		{"mime_svg", http.MethodGet, "test.svg", false, http.StatusOK, "image/svg+xml", "", false},
		{"mime_png", http.MethodGet, "test.png", false, http.StatusOK, "image/png", "", false},
		{"mime_jpg", http.MethodGet, "test.jpg", false, http.StatusOK, "image/jpeg", "", false},
		{"mime_woff2", http.MethodGet, "test.woff2", false, http.StatusOK, "font/woff2", "", false},
		{"mime_html", http.MethodGet, "test.html", false, http.StatusOK, "text/html; charset=utf-8", "", false},
		{"mime_bin", http.MethodGet, "test.bin", false, http.StatusOK, "application/octet-stream", "", false},
		{"mime_custom", http.MethodGet, "test.custom", false, http.StatusOK, "application/octet-stream", "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			targetH := h
			if tc.useEmpty {
				targetH = hFallback
			}
			rec := httptest.NewRecorder()
			targetH.GetStatic(rec, httptest.NewRequest(tc.method, "/static/"+tc.path, nil), tc.path)
			if rec.Code != tc.wantCode {
				t.Fatalf("code: got %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantHead && rec.Body.Len() != 0 {
				t.Fatalf("head body: got %d bytes, want 0", rec.Body.Len())
			}
			if tc.wantMime != "" && rec.Header().Get("Content-Type") != tc.wantMime {
				t.Fatalf("mime: got %q, want %q", rec.Header().Get("Content-Type"), tc.wantMime)
			}
			if tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub) {
				t.Fatalf("body: got %q, want substring %q", rec.Body.String(), tc.wantSub)
			}
		})
	}
}

func TestVoiceWaveformProvider(t *testing.T) {
	t.Parallel()
	tmpl := []byte(`<body>{{ if and .VoiceWaveform .VoiceWaveform.Enabled }}<wave url="{{ .VoiceWaveform.LevelsURL }}" gain="{{ .VoiceWaveform.Gain }}"></wave>{{ end }}</body>`)
	legacyOpt := display.WithUIProvider(func() string { return "legacy" })

	h := display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl))
	if h.VoiceWaveform() != nil {
		t.Fatal("voice waveform without provider should be nil")
	}
	rec := httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display", nil))
	if strings.Contains(rec.Body.String(), "<wave") {
		t.Fatalf("waveform rendered without provider: %s", rec.Body.String())
	}

	wave := &display.VoiceWaveformTemplateData{Enabled: true, LevelsURL: "http://127.0.0.1:8765/levels", Gain: 2.2}
	h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithVoiceWaveformProvider(func() *display.VoiceWaveformTemplateData { return wave }))
	if h.VoiceWaveform() != wave {
		t.Fatal("voice waveform getter did not return provider value")
	}
	rec = httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display", nil))
	body := rec.Body.String()
	if !strings.Contains(body, `url="http://127.0.0.1:8765/levels"`) || !strings.Contains(body, `gain="2.2"`) {
		t.Fatalf("waveform data missing from template: %s", body)
	}

	disabled := &display.VoiceWaveformTemplateData{Enabled: false, LevelsURL: "http://127.0.0.1:8765/levels", Gain: 2.2}
	h = display.NewHandler(legacyOpt, display.WithTemplateContent(tmpl), display.WithVoiceWaveformProvider(func() *display.VoiceWaveformTemplateData { return disabled }))
	rec = httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display", nil))
	if strings.Contains(rec.Body.String(), "<wave") {
		t.Fatalf("disabled waveform rendered: %s", rec.Body.String())
	}
}

func TestGetDisplay_UIShellSelection(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	legacyPath := filepath.Join(dir, "display.html")
	reactPath := filepath.Join(dir, "display_react.html")
	_ = os.WriteFile(legacyPath, []byte("LEGACY {{ .Timezone }}"), 0o644)
	_ = os.WriteFile(reactPath, []byte("REACT {{ .Timezone }} {{ .Grid }}"), 0o644)

	tests := []struct {
		name     string
		configUI string
		query    string
		want     string
	}{
		{"default is react", "", "", "REACT UTC 6x2"},
		{"explicit legacy config", "legacy", "", "LEGACY UTC"},
		{"react config", "react", "", "REACT UTC 6x2"},
		{"query react overrides legacy config", "legacy", "ui=react", "REACT UTC 6x2"},
		{"query legacy overrides react config", "react", "ui=legacy", "LEGACY UTC"},
		{"unknown query value falls back to react config", "react", "ui=bogus", "REACT UTC 6x2"},
		{"unknown query value falls back to legacy config", "legacy", "ui=bogus", "LEGACY UTC"},
		{"unknown query value falls back to default react", "", "ui=bogus", "REACT UTC 6x2"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := display.NewHandler(
				display.WithTemplatePath(legacyPath), display.WithLocalTemplatePath(""),
				display.WithReactTemplatePath(reactPath), display.WithLocalReactTemplatePath(""),
				display.WithUIProvider(func() string { return tc.configUI }),
				display.WithTimezoneProvider(func() string { return "UTC" }),
			)
			rec := httptest.NewRecorder()
			h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display?"+tc.query, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code: got %d, want 200", rec.Code)
			}
			if got := rec.Body.String(); got != tc.want {
				t.Fatalf("body: got %q, want %q", got, tc.want)
			}
		})
	}
}

// TestGetDisplay_PerfScriptGatedByQuery checks the real display.html template: the perf
// instrumentation script is emitted only for ?perf=1 and no other page load pulls it in.
func TestGetDisplay_PerfScriptGatedByQuery(t *testing.T) {
	t.Parallel()
	const perfScript = "static/js/perf.js"
	tests := []struct {
		name     string
		path     string
		wantPerf bool
	}{
		{"no_query", "/display", false},
		{"perf_zero", "/display?perf=0", false},
		{"perf_empty", "/display?perf=", false},
		{"perf_one", "/display?perf=1", true},
		{"perf_one_with_other_params", "/display?hide_cursor=true&perf=1", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := display.NewHandler(
				display.WithUIProvider(func() string { return "legacy" }),
				display.WithTemplatePath("../../web/templates/display.html"),
				display.WithLocalTemplatePath(""),
			)
			rec := httptest.NewRecorder()
			h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, tc.path, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code: got %d, want 200", rec.Code)
			}
			if got := strings.Contains(rec.Body.String(), perfScript); got != tc.wantPerf {
				t.Fatalf("perf.js present = %v, want %v", got, tc.wantPerf)
			}
		})
	}
}

func TestGetDisplay_ReactShellMissingAndHead(t *testing.T) {
	t.Parallel()
	// Inline legacy content must never stand in for the React shell.
	h := display.NewHandler(
		display.WithTemplateContent([]byte("<h1>legacy</h1>")),
		display.WithReactTemplatePath("/no/such/react"), display.WithLocalReactTemplatePath("/no/such/local"),
	)
	rec := httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display?ui=react", nil))
	if rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "display template not found") {
		t.Fatalf("missing react shell: code %d body %q", rec.Code, rec.Body.String())
	}

	rec = httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodHead, "/display?ui=react", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("HEAD missing react shell: code %d, want 404", rec.Code)
	}
}

func TestGetDisplay_ReactShellRepoTemplate(t *testing.T) {
	t.Parallel()
	h := display.NewHandler(
		display.WithReactTemplatePath("../../web/templates/display_react.html"),
		display.WithLocalReactTemplatePath(""),
		display.WithTemplateContent([]byte("<h1>legacy</h1>")),
		display.WithHideCursorProvider(func() bool { return true }),
		display.WithRemoteURLProvider(func() string { return "http://remote.example/display" }),
	)
	rec := httptest.NewRecorder()
	h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, "/display?ui=react", nil))
	body := rec.Body.String()
	for _, want := range []string{
		`<div id="root"></div>`,
		`<script type="module" src="static/ui/app.js"></script>`,
		`<base href="./">`,
		`data-hide-cursor="true"`,
		`class="mm-touch-display"`,
		`data-remote-url="http://remote.example/display"`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("react shell missing %q", want)
		}
	}
	if strings.Contains(body, "<h1>legacy</h1>") {
		t.Fatal("react request served legacy inline content")
	}
}

// TestGetDisplay_ReactPerfScriptGatedByQuery checks the real display_react.html template: the
// perf instrumentation loads ahead of the React bundle only for ?ui=react&perf=1 (#779).
func TestGetDisplay_ReactPerfScriptGatedByQuery(t *testing.T) {
	t.Parallel()
	const perfScript = `<script src="static/js/perf.js"></script>`
	const bundle = `<script type="module" src="static/ui/app.js"></script>`
	tests := []struct {
		name     string
		path     string
		wantPerf bool
	}{
		{"react_no_perf", "/display?ui=react", false},
		{"react_perf_zero", "/display?ui=react&perf=0", false},
		{"react_perf_one", "/display?ui=react&perf=1", true},
		{"react_perf_one_with_other_params", "/display?ui=react&hide_cursor=true&perf=1", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := display.NewHandler(
				display.WithReactTemplatePath("../../web/templates/display_react.html"),
				display.WithLocalReactTemplatePath(""),
				display.WithTemplateContent([]byte("<h1>legacy</h1>")),
			)
			rec := httptest.NewRecorder()
			h.GetDisplay(rec, httptest.NewRequest(http.MethodGet, tc.path, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code: got %d, want 200", rec.Code)
			}
			body := rec.Body.String()
			if got := strings.Contains(body, perfScript); got != tc.wantPerf {
				t.Fatalf("perf.js present = %v, want %v", got, tc.wantPerf)
			}
			if tc.wantPerf {
				// perf.js is ahead of the bundle, so it runs before the React shell mounts.
				if strings.Index(body, perfScript) > strings.Index(body, bundle) {
					t.Fatal("perf.js must load before the React bundle")
				}
			}
		})
	}
}
