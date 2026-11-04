package server_test

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/server"
)

func doServerReq(srv *server.Server, method, path string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)
	return rec
}

func TestConfig_ApplyDefaults(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		in       server.Config
		wantHost string
		wantPort int
		wantRead time.Duration
	}{
		{"defaults", server.Config{}, server.DefaultHost, server.DefaultPort, server.DefaultReadTimeout},
		{"negative_port", server.Config{Port: -1}, server.DefaultHost, 0, server.DefaultReadTimeout},
		{"custom", server.Config{Host: "127.0.0.1", Port: 9090, ReadTimeout: time.Second, ReadHeaderTimeout: 2 * time.Second, WriteTimeout: 3 * time.Second, IdleTimeout: 4 * time.Second}, "127.0.0.1", 9090, time.Second},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := tc.in
			cfg.ApplyDefaults()
			if cfg.Host != tc.wantHost || cfg.Port != tc.wantPort || cfg.ReadTimeout != tc.wantRead {
				t.Fatalf("ApplyDefaults() = %+v, want host=%s port=%d read=%v", cfg, tc.wantHost, tc.wantPort, tc.wantRead)
			}
			if tc.name == "custom" && (cfg.ReadHeaderTimeout != 2*time.Second || cfg.WriteTimeout != 3*time.Second || cfg.IdleTimeout != 4*time.Second) {
				t.Fatal("custom timeouts not preserved")
			}
		})
	}
}

func TestServer_CoreEndpoints(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name        string
		method      string
		path        string
		wantCode    int
		wantAllow   string
		wantBodySub string
	}{
		{"healthz_get", http.MethodGet, "/healthz", http.StatusOK, "", `"status":"ok"`},
		{"healthz_head", http.MethodHead, "/healthz", http.StatusOK, "", ""},
		{"healthz_options", http.MethodOptions, "/healthz", http.StatusNoContent, "", ""},
		{"healthz_post_405", http.MethodPost, "/healthz", http.StatusMethodNotAllowed, "GET, HEAD, OPTIONS", ""},
		{"health_get", http.MethodGet, "/health", http.StatusOK, "", `"status":"ok"`},
		{"health_head", http.MethodHead, "/health", http.StatusOK, "", ""},
		{"health_options", http.MethodOptions, "/health", http.StatusNoContent, "", ""},
		{"health_post_405", http.MethodPost, "/health", http.StatusMethodNotAllowed, "GET, HEAD, OPTIONS", ""},
		{"root_get", http.MethodGet, "/", http.StatusOK, "", `"service":"mirrormere"`},
		{"root_head", http.MethodHead, "/", http.StatusOK, "", ""},
		{"root_options", http.MethodOptions, "/", http.StatusNoContent, "", ""},
		{"root_post_405", http.MethodPost, "/", http.StatusMethodNotAllowed, "GET, HEAD, OPTIONS", ""},
		{"root_unknown_404", http.MethodGet, "/unknown/path", http.StatusNotFound, "", ""},
		{"metrics_get", http.MethodGet, "/metrics", http.StatusOK, "", "# HELP"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srv := server.New(server.Config{})
			rec := doServerReq(srv, tc.method, tc.path)
			if rec.Code != tc.wantCode {
				t.Fatalf("status = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantAllow != "" && rec.Header().Get("Allow") != tc.wantAllow {
				t.Errorf("Allow = %q, want %q", rec.Header().Get("Allow"), tc.wantAllow)
			}
			if tc.wantBodySub != "" && !strings.Contains(rec.Body.String(), tc.wantBodySub) {
				t.Errorf("body = %q, want %q", rec.Body.String(), tc.wantBodySub)
			}
			if tc.method == http.MethodHead && rec.Body.Len() != 0 {
				t.Errorf("expected empty body for HEAD, got %d bytes", rec.Body.Len())
			}
		})
	}
}

func TestCoverageFlushEndpoint(t *testing.T) {
	tests := []struct {
		name        string
		method      string
		writeErr    error
		wantCode    int
		wantBodySub string
	}{
		{name: "method_not_allowed", method: http.MethodDelete, wantCode: http.StatusMethodNotAllowed},
		{name: "valid_flush_tar", method: http.MethodPost, wantCode: http.StatusOK, wantBodySub: "covcounters.test"},
		{name: "write_error_500", method: http.MethodGet, writeErr: errors.New("simulated flush error"), wantCode: http.StatusInternalServerError, wantBodySub: "simulated flush error"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			defer server.SetWriteCountersDirForTest(func(dir string) error {
				if tc.writeErr != nil {
					return tc.writeErr
				}
				return os.WriteFile(filepath.Join(dir, "covcounters.test"), []byte("covdata"), 0o600)
			})()

			srv := server.New(server.Config{})
			rec := doServerReq(srv, tc.method, "/debug/coverage/flush")
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantBodySub != "" && !strings.Contains(rec.Body.String(), tc.wantBodySub) {
				t.Fatalf("body = %q, want %q", rec.Body.String(), tc.wantBodySub)
			}
		})
	}
}

func TestServer_Lifecycle(t *testing.T) {
	t.Parallel()
	srv := server.New(server.Config{Host: "127.0.0.1", Port: -1})
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("Listen failed: %v", err)
	}
	errCh := make(chan error, 1)
	go func() { errCh <- srv.Serve(l) }()

	select {
	case <-srv.Ready():
	case err := <-errCh:
		t.Fatalf("server failed to start: %v", err)
	case <-time.After(2 * time.Second):
		t.Fatal("server start timed out")
	}

	if srv.Addr() == "" {
		t.Fatal("expected non-empty bound address")
	}

	resp, err := http.Get("http://" + srv.Addr() + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz failed: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown failed: %v", err)
	}
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("server stopped with error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server shutdown timed out")
	}
}

type mockAll struct{ status int }

func (m *mockAll) w(w http.ResponseWriter, s string) {
	w.WriteHeader(m.status)
	_, _ = w.Write([]byte(s))
}

func (m *mockAll) ServeHTTP(w http.ResponseWriter, _ *http.Request)        { m.w(w, "events") }
func (m *mockAll) PostScreenSelect(w http.ResponseWriter, _ *http.Request) { m.w(w, "screen_select") }

func (m *mockAll) PostScreenAdvance(w http.ResponseWriter, _ *http.Request) { m.w(w, "screen_advance") }

func (m *mockAll) PostScreenPause(w http.ResponseWriter, _ *http.Request) { m.w(w, "screen_pause") }

func (m *mockAll) GetScreens(w http.ResponseWriter, _ *http.Request) { m.w(w, "screens") }

func (m *mockAll) GetWidgetJSON(w http.ResponseWriter, _ *http.Request, id string) {
	m.w(w, "widget:"+id)
}

func (m *mockAll) GetWidgetRender(w http.ResponseWriter, _ *http.Request, id string) {
	m.w(w, "render:"+id)
}

func (m *mockAll) GetWidgetAsset(w http.ResponseWriter, _ *http.Request, t, p string) {
	m.w(w, "asset:"+t+":"+p)
}
func (m *mockAll) ServeStyle(w http.ResponseWriter, _ *http.Request) { m.w(w, "style") }
func (m *mockAll) PostWidgetPush(w http.ResponseWriter, _ *http.Request, id string) {
	m.w(w, "push:"+id)
}

func (m *mockAll) PostWidgetPhotos(w http.ResponseWriter, _ *http.Request, id string) {
	m.w(w, "photos:"+id)
}
func (m *mockAll) GetDisplay(w http.ResponseWriter, _ *http.Request)          { m.w(w, "display") }
func (m *mockAll) GetStatic(w http.ResponseWriter, _ *http.Request, p string) { m.w(w, "static:"+p) }

func (m *mockAll) CompleteListItem(w http.ResponseWriter, _ *http.Request, l, i string) {
	m.w(w, "complete:"+l+":"+i)
}
func (m *mockAll) GetAudio(w http.ResponseWriter, _ *http.Request)         { m.w(w, "audio") }
func (m *mockAll) PostAudioVolume(w http.ResponseWriter, _ *http.Request)  { m.w(w, "volume") }
func (m *mockAll) PostAudioMute(w http.ResponseWriter, _ *http.Request)    { m.w(w, "mute") }
func (m *mockAll) PostVideoTrigger(w http.ResponseWriter, _ *http.Request) { m.w(w, "v_trigger") }
func (m *mockAll) PostVideoDismiss(w http.ResponseWriter, _ *http.Request) { m.w(w, "v_dismiss") }
func (m *mockAll) PostVideoAction(w http.ResponseWriter, _ *http.Request)  { m.w(w, "v_action") }
func (m *mockAll) PostVideoState(w http.ResponseWriter, _ *http.Request)   { m.w(w, "v_post_state") }

func (m *mockAll) GetVideoState(w http.ResponseWriter, _ *http.Request) { m.w(w, "v_get_state") }

func (m *mockAll) PostVoiceState(w http.ResponseWriter, _ *http.Request) { m.w(w, "voice_state") }

func (m *mockAll) PostVoiceInteract(w http.ResponseWriter, _ *http.Request) { m.w(w, "voice_interact") }

func (m *mockAll) PostVoiceHeartbeat(w http.ResponseWriter, _ *http.Request) {
	m.w(w, "voice_heartbeat")
}

func (m *mockAll) GetLayoutEditor(w http.ResponseWriter, _ *http.Request) {
	m.w(w, "layout_editor_get")
}

func (m *mockAll) PutLayoutEditor(w http.ResponseWriter, _ *http.Request) {
	m.w(w, "layout_editor_put")
}

func (m *mockAll) DeleteLayoutEditor(w http.ResponseWriter, _ *http.Request) {
	m.w(w, "layout_editor_delete")
}

func testHandlerFamily(fam string, m1 *mockAll) *server.Server {
	var c server.Config
	switch fam {
	case "events":
		c.EventsHandler = m1
	case "screen":
		c.ScreenHandler = m1
	case "render":
		c.RenderHandler = m1
	case "display":
		c.DisplayHandler = m1
	case "push":
		c.PushHandler = m1
	case "lists":
		c.ListsHandler = m1
	case "audio":
		c.AudioHandler = m1
	case "video":
		c.VideoHandler = m1
	case "voice":
		c.VoiceHandler = m1
	case "layout_editor":
		c.LayoutEditorHandler = m1
	}
	return server.New(c)
}

func TestServer_HandlerDelegation(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		family   string
		method   string
		path     string
		wantCode int
		wantBody string
	}{
		{name: "events", family: "events", method: http.MethodGet, path: "/api/events", wantBody: "events"},
		{name: "screen_select", family: "screen", method: http.MethodPost, path: "/api/screen/select", wantBody: "screen_select"},
		{name: "screen_advance", family: "screen", method: http.MethodPost, path: "/api/screen/advance", wantBody: "screen_advance"},
		{name: "screen_pause", family: "screen", method: http.MethodPost, path: "/api/screen/pause", wantBody: "screen_pause"},
		{name: "screens_get", family: "screen", method: http.MethodGet, path: "/api/screens", wantBody: "screens"},
		{name: "render_widget", family: "render", method: http.MethodGet, path: "/api/widgets/sensor-1/render", wantBody: "render:sensor-1"},
		{name: "widget_json", family: "render", method: http.MethodGet, path: "/api/widgets/sensor-1", wantBody: "widget:sensor-1"},
		{name: "render_asset", family: "render", method: http.MethodGet, path: "/widget-types/clock/assets/icon.png", wantBody: "asset:clock:icon.png"},
		{name: "render_style", family: "render", method: http.MethodGet, path: "/style.css", wantBody: "style"},
		{name: "display_main", family: "display", method: http.MethodGet, path: "/display", wantBody: "display"},
		{name: "display_static", family: "display", method: http.MethodGet, path: "/static/bundle.js", wantBody: "static:bundle.js"},
		{name: "push_webhook", family: "push", method: http.MethodPost, path: "/api/widgets/weather/push", wantBody: "push:weather"},
		{name: "photos_webhook", family: "push", method: http.MethodPost, path: "/api/widgets/family-photos/photos", wantBody: "photos:family-photos"},
		{name: "lists_complete", family: "lists", method: http.MethodPost, path: "/api/lists/groceries/items/i42/complete", wantBody: "complete:groceries:i42"},
		{name: "audio_get", family: "audio", method: http.MethodGet, path: "/api/audio", wantBody: "audio"},
		{name: "audio_volume", family: "audio", method: http.MethodPost, path: "/api/audio/volume", wantBody: "volume"},
		{name: "audio_mute", family: "audio", method: http.MethodPost, path: "/api/audio/mute", wantBody: "mute"},
		{name: "video_trigger", family: "video", method: http.MethodPost, path: "/api/video/trigger", wantBody: "v_trigger"},
		{name: "video_dismiss", family: "video", method: http.MethodPost, path: "/api/video/dismiss", wantBody: "v_dismiss"},
		{name: "video_action", family: "video", method: http.MethodPost, path: "/api/video/action", wantBody: "v_action"},
		{name: "video_state_get", family: "video", method: http.MethodGet, path: "/api/video/state", wantBody: "v_get_state"},
		{name: "video_state_post", family: "video", method: http.MethodPost, path: "/api/video/state", wantBody: "v_post_state"},
		{name: "voice_state", family: "voice", method: http.MethodPost, path: "/api/voice/state", wantBody: "voice_state"},
		{name: "voice_interact", family: "voice", method: http.MethodPost, path: "/api/voice/interact", wantBody: "voice_interact"},
		{name: "voice_heartbeat", family: "voice", method: http.MethodPost, path: "/api/voice/heartbeat", wantBody: "voice_heartbeat"},
		{name: "layout_editor_get", family: "layout_editor", method: http.MethodGet, path: "/api/layout/editor", wantBody: "layout_editor_get"},
		{name: "layout_editor_put", family: "layout_editor", method: http.MethodPut, path: "/api/layout/editor", wantBody: "layout_editor_put"},
		{name: "layout_editor_delete", family: "layout_editor", method: http.MethodDelete, path: "/api/layout/editor", wantBody: "layout_editor_delete"},
		{name: "layout_editor_options", family: "layout_editor", method: http.MethodOptions, path: "/api/layout/editor", wantCode: http.StatusNoContent},
		{name: "layout_editor_post_405", family: "layout_editor", method: http.MethodPost, path: "/api/layout/editor", wantCode: http.StatusMethodNotAllowed, wantBody: "method not allowed"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srvEmpty := server.New(server.Config{})
			if rec := doServerReq(srvEmpty, tc.method, tc.path); rec.Code != http.StatusNotFound {
				t.Fatalf("unset handler returned %d, want 404", rec.Code)
			}

			srvCfg := testHandlerFamily(tc.family, &mockAll{status: http.StatusOK})
			rec200 := doServerReq(srvCfg, tc.method, tc.path)
			wantCode := tc.wantCode
			if wantCode == 0 {
				wantCode = http.StatusOK
			}
			if rec200.Code != wantCode || !strings.Contains(rec200.Body.String(), tc.wantBody) {
				t.Fatalf("configured handler code=%d body=%q, want %d with %q", rec200.Code, rec200.Body.String(), wantCode, tc.wantBody)
			}
		})
	}
}
