package rotation

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/layout"
)

type mockController struct {
	ctrlErr     error
	totalScreen int
	layout      ScreenLayoutSnapshot
}

func doReq(h http.Handler, method, path, body string) *httptest.ResponseRecorder {
	var r io.Reader
	if body != "<nil>" {
		r = strings.NewReader(body)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, path, r))
	return rec
}

func (m *mockController) SelectScreen(index int) (events.ScreenRotateData, error) {
	if m.ctrlErr != nil {
		return events.ScreenRotateData{}, m.ctrlErr
	}
	return events.ScreenRotateData{CurrentScreen: index, TotalScreens: m.totalScreen}, nil
}

func (m *mockController) AdvanceScreen(dir string) (events.ScreenRotateData, error) {
	if m.ctrlErr != nil {
		return events.ScreenRotateData{}, m.ctrlErr
	}
	return events.ScreenRotateData{CurrentScreen: 1, TotalScreens: m.totalScreen}, nil
}
func (m *mockController) PauseRotation(d time.Duration) (int, error) { return 0, m.ctrlErr }
func (m *mockController) ResumeRotation() (int, error)               { return 0, m.ctrlErr }
func (m *mockController) CurrentScreen() int                         { return 0 }
func (m *mockController) ScreenLayout() ScreenLayoutSnapshot         { return m.layout }

func TestHandler_PostScreenSelect(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		method   string
		body     string
		nilCtrl  bool
		ctrlErr  error
		wantCode int
		wantSub  string
	}{
		{"valid_index_2", http.MethodPost, `{"screen_index":2}`, false, nil, http.StatusOK, `"current_screen":2`},
		{"options_cors", http.MethodOptions, "", false, nil, http.StatusNoContent, ""},
		{"get_not_allowed", http.MethodGet, "", false, nil, http.StatusMethodNotAllowed, ""},
		{"nil_controller", http.MethodPost, `{"screen_index":1}`, true, nil, http.StatusInternalServerError, "not configured"},
		{"invalid_json", http.MethodPost, `{bad`, false, nil, http.StatusBadRequest, "invalid request body"},
		{"ctrl_err", http.MethodPost, `{"screen_index":9}`, false, ErrInvalidScreenIndex, http.StatusBadRequest, ErrInvalidScreenIndex.Error()},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var ctrl ScreenController
			if !tc.nilCtrl {
				ctrl = &mockController{totalScreen: 3, ctrlErr: tc.ctrlErr}
			}
			h := NewHandler(ctrl)
			rec := doReq(http.HandlerFunc(h.PostScreenSelect), tc.method, "/api/screen/select", tc.body)
			if rec.Code != tc.wantCode || (tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub)) {
				t.Fatalf("got (%d, %q), want (%d, sub %q)", rec.Code, rec.Body.String(), tc.wantCode, tc.wantSub)
			}
		})
	}
}

func TestHandler_PostScreenAdvance(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		method   string
		body     string
		nilCtrl  bool
		ctrlErr  error
		wantCode int
		wantSub  string
	}{
		{"default_next", http.MethodPost, "", false, nil, http.StatusOK, `"current_screen":1`},
		{"nil_body", http.MethodPost, "<nil>", false, nil, http.StatusOK, `"current_screen":1`},
		{"explicit_prev", http.MethodPost, `{"direction":"prev"}`, false, nil, http.StatusOK, `"current_screen":1`},
		{"options_cors", http.MethodOptions, "", false, nil, http.StatusNoContent, ""},
		{"method_not_allowed", http.MethodPut, "", false, nil, http.StatusMethodNotAllowed, ""},
		{"nil_controller", http.MethodPost, "", true, nil, http.StatusInternalServerError, "not configured"},
		{"invalid_json", http.MethodPost, `{broken`, false, nil, http.StatusBadRequest, "invalid request body"},
		{"ctrl_err", http.MethodPost, `{"direction":"bad"}`, false, ErrInvalidDirection, http.StatusBadRequest, ErrInvalidDirection.Error()},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var ctrl ScreenController
			if !tc.nilCtrl {
				ctrl = &mockController{totalScreen: 2, ctrlErr: tc.ctrlErr}
			}
			h := NewHandler(ctrl)
			rec := doReq(http.HandlerFunc(h.PostScreenAdvance), tc.method, "/api/screen/advance", tc.body)
			if rec.Code != tc.wantCode || (tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub)) {
				t.Fatalf("got (%d, %q), want (%d, sub %q)", rec.Code, rec.Body.String(), tc.wantCode, tc.wantSub)
			}
		})
	}
}

func TestHandler_PostScreenPause(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		method   string
		body     string
		nilCtrl  bool
		ctrlErr  error
		wantCode int
		wantSub  string
	}{
		{"pause_default", http.MethodPost, `{"paused":true}`, false, nil, http.StatusOK, `"paused":true`},
		{"pause_explicit", http.MethodPost, `{"paused":true,"duration_seconds":60}`, false, nil, http.StatusOK, `"paused":true`},
		{"resume", http.MethodPost, `{"paused":false}`, false, nil, http.StatusOK, `"paused":false`},
		{"options_cors", http.MethodOptions, "", false, nil, http.StatusNoContent, ""},
		{"method_not_allowed", http.MethodDelete, "", false, nil, http.StatusMethodNotAllowed, ""},
		{"nil_controller", http.MethodPost, `{"paused":true}`, true, nil, http.StatusInternalServerError, "not configured"},
		{"invalid_json", http.MethodPost, `{bad`, false, nil, http.StatusBadRequest, "invalid payload"},
		{"missing_paused", http.MethodPost, `{}`, false, nil, http.StatusBadRequest, "invalid payload"},
		{"missing_paused_dur", http.MethodPost, `{"duration_seconds":120}`, false, nil, http.StatusBadRequest, "invalid payload"},
		{"non_bool_paused", http.MethodPost, `{"paused":"true"}`, false, nil, http.StatusBadRequest, "invalid payload"},
		{"negative_dur", http.MethodPost, `{"paused":true,"duration_seconds":-5}`, false, nil, http.StatusBadRequest, "duration_seconds"},
		{"pause_err", http.MethodPost, `{"paused":true}`, false, errors.New("pause err"), http.StatusBadRequest, "pause err"},
		{"resume_err", http.MethodPost, `{"paused":false}`, false, errors.New("resume err"), http.StatusBadRequest, "resume err"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var ctrl ScreenController
			if !tc.nilCtrl {
				ctrl = &mockController{ctrlErr: tc.ctrlErr}
			}
			h := NewHandler(ctrl)
			rec := doReq(http.HandlerFunc(h.PostScreenPause), tc.method, "/api/screen/pause", tc.body)
			if rec.Code != tc.wantCode || (tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub)) {
				t.Fatalf("got (%d, %q), want (%d, sub %q)", rec.Code, rec.Body.String(), tc.wantCode, tc.wantSub)
			}
		})
	}
}

func TestWriteJSONAndErrorHelpers(t *testing.T) {
	t.Parallel()
	rec := httptest.NewRecorder()
	writeError(rec, http.StatusBadRequest, "sample error")
	if rec.Code != http.StatusBadRequest || rec.Header().Get("Content-Type") != "application/json" {
		t.Errorf("unexpected writeError")
	}

	rec2 := httptest.NewRecorder()
	writeJSON(rec2, map[string]string{"result": "ok"})
	if rec2.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", rec2.Code)
	}
}

func TestHandler_GetScreens(t *testing.T) {
	t.Parallel()
	layoutSnap := ScreenLayoutSnapshot{
		CurrentScreen:   1,
		IntervalSeconds: 60,
		Screens: []layout.Screen{
			{Index: 0, Widgets: []layout.PlacedWidget{{WidgetID: "clock", Type: "clock", Origin: [2]int{0, 0}, Dimensions: domain.NewDimension(2, 1)}}},
			{Index: 1, Widgets: []layout.PlacedWidget{{WidgetID: "weather", Origin: [2]int{2, 1}, Dimensions: domain.NewDimension(3, 2)}}},
		},
	}
	tests := []struct {
		name     string
		method   string
		ctrl     bool
		layout   ScreenLayoutSnapshot
		wantCode int
		wantSub  string
	}{
		{"get_layout", http.MethodGet, true, layoutSnap, http.StatusOK, `"widget_id":"weather"`},
		{"head_layout", http.MethodHead, true, layoutSnap, http.StatusOK, ""},
		{"empty_layout", http.MethodGet, true, ScreenLayoutSnapshot{IntervalSeconds: 30}, http.StatusOK, `"screens":[]`},
		{"options", http.MethodOptions, true, layoutSnap, http.StatusNoContent, ""},
		{"post_not_allowed", http.MethodPost, true, layoutSnap, http.StatusMethodNotAllowed, "method not allowed"},
		{"nil_controller", http.MethodGet, false, layoutSnap, http.StatusInternalServerError, "not configured"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var ctrl ScreenController
			if tc.ctrl {
				ctrl = &mockController{layout: tc.layout}
			}
			h := NewHandler(ctrl)
			rec := doReq(http.HandlerFunc(h.GetScreens), tc.method, "/api/screens", "<nil>")
			if rec.Code != tc.wantCode || (tc.wantSub != "" && !strings.Contains(rec.Body.String(), tc.wantSub)) {
				t.Fatalf("got (%d, %q), want (%d, sub %q)", rec.Code, rec.Body.String(), tc.wantCode, tc.wantSub)
			}
		})
	}
}

func TestHandler_GetScreensResponseShape(t *testing.T) {
	t.Parallel()
	h := NewHandler(&mockController{layout: ScreenLayoutSnapshot{CurrentScreen: 1, IntervalSeconds: 60, Screens: []layout.Screen{
		{Index: 0, Widgets: []layout.PlacedWidget{{WidgetID: "clock", Type: "clock", Origin: [2]int{0, 0}, Dimensions: domain.NewDimension(2, 1)}}},
	}}})
	rec := doReq(http.HandlerFunc(h.GetScreens), http.MethodGet, "/api/screens", "<nil>")
	var resp api.ScreensResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.CurrentScreen != 1 || resp.IntervalSeconds != 60 || len(resp.Screens) != 1 || resp.Screens[0].Index != 0 {
		t.Fatalf("unexpected response: %+v", resp)
	}
	if w := resp.Screens[0].Widgets[0]; w.WidgetID != "clock" || w.Type != "clock" || w.Origin[0] != 0 || w.Dimensions[0] != 2 || w.Dimensions[1] != 1 {
		t.Errorf("unexpected widget placement: %+v", w)
	}
}
