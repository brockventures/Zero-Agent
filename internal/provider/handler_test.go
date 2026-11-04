package provider_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

type mockWidgetPusher struct {
	pushFunc      func(ctx context.Context, widgetID string, data map[string]any) (provider.WidgetPayload, error)
	photoPushFunc func(ctx context.Context, widgetID string, photos []string, albumName string) (provider.WidgetPayload, error)
}

func (m *mockWidgetPusher) PushWidgetData(ctx context.Context, id string, data map[string]any) (provider.WidgetPayload, error) {
	if m.pushFunc != nil {
		return m.pushFunc(ctx, id, data)
	}
	return provider.WidgetPayload{WidgetID: id, State: "ok"}, nil
}

func (m *mockWidgetPusher) PushWidgetPhotos(ctx context.Context, id string, photos []string, albumName string) (provider.WidgetPayload, error) {
	if m.photoPushFunc != nil {
		return m.photoPushFunc(ctx, id, photos, albumName)
	}
	return provider.WidgetPayload{WidgetID: id, State: "ok"}, nil
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, errors.New("read error simulated") }

func mkPusher(ts string, err error) provider.WidgetPusher {
	return &mockWidgetPusher{pushFunc: func(_ context.Context, id string, data map[string]any) (provider.WidgetPayload, error) {
		if ts == "" {
			ts = time.Now().UTC().Format(time.RFC3339)
		}
		return provider.WidgetPayload{WidgetID: id, Data: data, Timestamp: ts, State: "ok"}, err
	}}
}

func mkPhotoPusher(ts string, err error) provider.WidgetPusher {
	return &mockWidgetPusher{photoPushFunc: func(_ context.Context, id string, _ []string, _ string) (provider.WidgetPayload, error) {
		if ts == "" {
			ts = time.Now().UTC().Format(time.RFC3339)
		}
		return provider.WidgetPayload{WidgetID: id, Timestamp: ts, State: "ok"}, err
	}}
}

func TestNewPushHandler(t *testing.T) {
	t.Parallel()
	p := mkPusher("", nil)
	if provider.NewPushHandler(p, nil) == nil || provider.NewPushHandler(p, slog.Default()) == nil {
		t.Fatal("expected non-nil PushHandler")
	}
}

func TestPushHandler_PostWidgetPush(t *testing.T) {
	t.Parallel()
	pDefault := mkPusher("", nil)
	tests := []struct {
		name          string
		method        string
		id            string
		body          io.Reader
		pusher        provider.WidgetPusher
		wantStatus    int
		wantAllowHdr  string
		wantOrigin    string
		wantSubstr    string
		wantUpdatedAt string
	}{
		{"options_preflight", "OPTIONS", "tile-1", nil, pDefault, 204, "POST, OPTIONS", "*", "", ""},
		{"method_not_allowed", "GET", "tile-1", nil, pDefault, 405, "POST, OPTIONS", "", "method not allowed", ""},
		{"nil_pusher", "POST", "tile-1", strings.NewReader(`{}`), nil, 500, "", "", "pusher not configured", ""},
		{"read_body_err", "POST", "tile-1", errReader{}, pDefault, 400, "", "", "failed to read request body", ""},
		{"invalid_json", "POST", "tile-1", strings.NewReader(`{bad`), pDefault, 400, "", "", "invalid JSON payload", ""},
		{"non_object_json", "POST", "tile-1", strings.NewReader(`[1]`), pDefault, 400, "", "", "payload must be a JSON object", ""},
		{"not_found", "POST", "tile-missing", strings.NewReader(`{}`), mkPusher("", provider.ErrWidgetNotFound), 404, "", "", "not found in active configuration", ""},
		{"list_conflict", "POST", "tile-tasks", strings.NewReader(`{}`), mkPusher("", provider.ErrListWidgetPushForbidden), 409, "", "", "cannot push state to list-backed widget", ""},
		{"non_http_conflict", "POST", "tile-cal", strings.NewReader(`{}`), mkPusher("", provider.ErrNonHTTPWidgetPushForbidden), 409, "", "", "strictly limited to widgets using the http provider", ""},
		{"schema_val_err", "POST", "tile-weather", strings.NewReader(`{}`), mkPusher("", errors.Join(provider.ErrSchemaValidation, errors.New("type mismatch"))), 400, "", "", "type mismatch", ""},
		{"internal_err", "POST", "tile-err", strings.NewReader(`{}`), mkPusher("", errors.New("db down")), 500, "", "", "push ingestion error", ""},
		{"valid_push", "POST", "tile-sensor", strings.NewReader(`{"t":21.5}`), mkPusher("2026-09-26T00:00:00Z", nil), 200, "", "", "", "2026-09-26T00:00:00Z"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := provider.NewPushHandler(tc.pusher, nil)
			b := tc.body
			if b == nil {
				b = http.NoBody
			}
			req := httptest.NewRequest(tc.method, "/api/widgets/"+tc.id+"/push", b)
			rec := httptest.NewRecorder()
			h.PostWidgetPush(rec, req, tc.id)

			if rec.Code != tc.wantStatus {
				t.Fatalf("code=%d, want %d: %s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantAllowHdr != "" {
				hdr := rec.Header().Get("Allow")
				if tc.method == "OPTIONS" {
					hdr = rec.Header().Get("Access-Control-Allow-Methods")
				}
				if hdr != tc.wantAllowHdr {
					t.Errorf("allow=%q, want %q", hdr, tc.wantAllowHdr)
				}
			}
			if tc.wantOrigin != "" && rec.Header().Get("Access-Control-Allow-Origin") != tc.wantOrigin {
				t.Errorf("origin=%q, want %q", rec.Header().Get("Access-Control-Allow-Origin"), tc.wantOrigin)
			}
			if tc.wantSubstr != "" && !strings.Contains(rec.Body.String(), tc.wantSubstr) {
				t.Errorf("body %q does not contain %q", rec.Body.String(), tc.wantSubstr)
			}
			if tc.wantUpdatedAt != "" {
				var res map[string]any
				if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil || res["updated_at"] != tc.wantUpdatedAt || res["status"] != "ok" || res["widget_id"] != tc.id {
					t.Fatalf("unexpected body: %v", res)
				}
			}
		})
	}
}

func TestPushHandler_PostWidgetPhotos(t *testing.T) {
	t.Parallel()
	pDefault := mkPhotoPusher("", nil)
	const validJSON = `{"photos":["https://lh3.googleusercontent.com/pw/TEST"],"album_name":"Vacation"}`
	tests := []struct {
		name          string
		method        string
		id            string
		body          io.Reader
		pusher        provider.WidgetPusher
		wantStatus    int
		wantAllowHdr  string
		wantOrigin    string
		wantSubstr    string
		wantUpdatedAt string
	}{
		{"options_preflight", "OPTIONS", "tile-1", nil, pDefault, 204, "POST, OPTIONS", "*", "", ""},
		{"method_not_allowed", "GET", "tile-1", nil, pDefault, 405, "POST, OPTIONS", "", "method not allowed", ""},
		{"nil_pusher", "POST", "tile-1", strings.NewReader(validJSON), nil, 500, "", "", "pusher not configured", ""},
		{"read_body_err", "POST", "tile-1", errReader{}, pDefault, 400, "", "", "failed to read request body", ""},
		{"invalid_json", "POST", "tile-1", strings.NewReader(`{bad`), pDefault, 400, "", "", "invalid JSON payload", ""},
		{"missing_photos", "POST", "tile-1", strings.NewReader(`{"album_name":"test"}`), pDefault, 400, "", "", "photos field is required", ""},
		{"not_found", "POST", "tile-missing", strings.NewReader(validJSON), mkPhotoPusher("", provider.ErrWidgetNotFound), 404, "", "", "not found in active configuration", ""},
		{"photo_push_not_supported", "POST", "tile-share", strings.NewReader(validJSON), mkPhotoPusher("", provider.ErrPhotoPushNotSupported), 409, "", "", "does not support photo push", ""},
		{"invalid_photo_payload", "POST", "tile-bad", strings.NewReader(validJSON), mkPhotoPusher("", provider.ErrInvalidPhotoPushPayload), 400, "", "", "invalid photo push payload", ""},
		{"internal_err", "POST", "tile-err", strings.NewReader(validJSON), mkPhotoPusher("", errors.New("upstream failed")), 500, "", "", "photo push error", ""},
		{"valid_push", "POST", "tile-photos", strings.NewReader(validJSON), mkPhotoPusher("2026-11-01T00:00:00Z", nil), 200, "", "", "", "2026-11-01T00:00:00Z"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			h := provider.NewPushHandler(tc.pusher, nil)
			b := tc.body
			if b == nil {
				b = http.NoBody
			}
			req := httptest.NewRequest(tc.method, "/api/widgets/"+tc.id+"/photos", b)
			rec := httptest.NewRecorder()
			h.PostWidgetPhotos(rec, req, tc.id)

			if rec.Code != tc.wantStatus {
				t.Fatalf("code=%d, want %d: %s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if tc.wantAllowHdr != "" {
				hdr := rec.Header().Get("Allow")
				if tc.method == "OPTIONS" {
					hdr = rec.Header().Get("Access-Control-Allow-Methods")
				}
				if hdr != tc.wantAllowHdr {
					t.Errorf("allow=%q, want %q", hdr, tc.wantAllowHdr)
				}
			}
			if tc.wantOrigin != "" && rec.Header().Get("Access-Control-Allow-Origin") != tc.wantOrigin {
				t.Errorf("origin=%q, want %q", rec.Header().Get("Access-Control-Allow-Origin"), tc.wantOrigin)
			}
			if tc.wantSubstr != "" && !strings.Contains(rec.Body.String(), tc.wantSubstr) {
				t.Errorf("body %q does not contain %q", rec.Body.String(), tc.wantSubstr)
			}
			if tc.wantUpdatedAt != "" {
				var res map[string]any
				if err := json.Unmarshal(rec.Body.Bytes(), &res); err != nil || res["updated_at"] != tc.wantUpdatedAt || res["status"] != "ok" || res["widget_id"] != tc.id {
					t.Fatalf("unexpected body: %v", res)
				}
			}
		})
	}
}
