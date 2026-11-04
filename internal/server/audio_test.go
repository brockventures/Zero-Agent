package server_test

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/server"
)

type mockAudioCoord struct {
	volume int
	muted  bool
	err    error
}

func (m *mockAudioCoord) GetState() (int, bool) { return m.volume, m.muted }
func (m *mockAudioCoord) SetVolume(v int) (int, bool, error) {
	if m.err == nil {
		m.volume = v
	}
	return m.volume, m.muted, m.err
}

func (m *mockAudioCoord) SetMute(mut *bool) (int, bool, error) {
	if m.err == nil && mut == nil {
		m.muted = !m.muted
	} else if m.err == nil {
		m.muted = *mut
	}
	return m.volume, m.muted, m.err
}

type audioTC struct {
	name, method, body string
	nilCoord           bool
	coordErr           error
	wantCode, wantVol  int
	wantMuted          bool
}

func doAudioReq(h http.HandlerFunc, m, p, b string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(m, p, strings.NewReader(b)))
	return rec
}

func checkAudioResp(t *testing.T, rec *httptest.ResponseRecorder, vol int, muted bool) {
	t.Helper()
	var resp api.AudioStateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil || (vol > 0 && resp.Volume != vol) || (vol == 0 && resp.Muted != muted) {
		t.Fatalf("unexpected resp: %v, %+v", err, resp)
	}
}

func audioHandler(nilCoord bool, err error) *server.DefaultAudioHandler {
	if nilCoord {
		return server.NewDefaultAudioHandler(nil)
	}
	return server.NewDefaultAudioHandler(&mockAudioCoord{volume: 75, err: err})
}

func TestDefaultAudioHandler_GetAudio(t *testing.T) {
	t.Parallel()
	tests := []audioTC{
		{name: "ok_get", method: http.MethodGet, wantCode: http.StatusOK},
		{name: "ok_head", method: http.MethodHead, wantCode: http.StatusOK},
		{name: "options_cors", method: http.MethodOptions, wantCode: http.StatusNoContent},
		{name: "method_not_allowed", method: http.MethodPost, wantCode: http.StatusMethodNotAllowed},
		{name: "nil_coord", method: http.MethodGet, nilCoord: true, wantCode: http.StatusInternalServerError},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := doAudioReq(audioHandler(tc.nilCoord, nil).GetAudio, tc.method, "/api/audio", "")
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.method == http.MethodGet && rec.Code == http.StatusOK {
				checkAudioResp(t, rec, 75, false)
			}
		})
	}
}

func TestDefaultAudioHandler_PostAudioVolume(t *testing.T) {
	t.Parallel()
	tests := []audioTC{
		{name: "ok_volume", method: http.MethodPost, body: `{"volume":80}`, wantCode: http.StatusOK, wantVol: 80},
		{name: "options_cors", method: http.MethodOptions, wantCode: http.StatusNoContent},
		{name: "method_not_allowed", method: http.MethodGet, wantCode: http.StatusMethodNotAllowed},
		{name: "nil_coord", method: http.MethodPost, body: `{"volume":50}`, nilCoord: true, wantCode: http.StatusInternalServerError},
		{name: "missing_volume", method: http.MethodPost, body: `{}`, wantCode: http.StatusBadRequest},
		{name: "negative_volume", method: http.MethodPost, body: `{"volume":-10}`, wantCode: http.StatusBadRequest},
		{name: "oob_volume", method: http.MethodPost, body: `{"volume":105}`, wantCode: http.StatusBadRequest},
		{name: "float_volume", method: http.MethodPost, body: `{"volume":75.5}`, wantCode: http.StatusBadRequest},
		{name: "invalid_json", method: http.MethodPost, body: `{invalid`, wantCode: http.StatusBadRequest},
		{name: "coord_error", method: http.MethodPost, body: `{"volume":50}`, coordErr: errors.New("err"), wantCode: http.StatusBadRequest},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := doAudioReq(audioHandler(tc.nilCoord, tc.coordErr).PostAudioVolume, tc.method, "/api/audio/volume", tc.body)
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantVol > 0 {
				checkAudioResp(t, rec, tc.wantVol, false)
			}
		})
	}
}

func TestDefaultAudioHandler_PostAudioMute(t *testing.T) {
	t.Parallel()
	tests := []audioTC{
		{name: "toggle_empty_json", method: http.MethodPost, body: `{}`, wantCode: http.StatusOK, wantMuted: true},
		{name: "toggle_empty_body", method: http.MethodPost, body: ``, wantCode: http.StatusOK, wantMuted: true},
		{name: "explicit_mute_true", method: http.MethodPost, body: `{"muted":true}`, wantCode: http.StatusOK, wantMuted: true},
		{name: "explicit_mute_false", method: http.MethodPost, body: `{"muted":false}`, wantCode: http.StatusOK, wantMuted: false},
		{name: "options_cors", method: http.MethodOptions, body: ``, wantCode: http.StatusNoContent},
		{name: "method_not_allowed", method: http.MethodGet, body: ``, wantCode: http.StatusMethodNotAllowed},
		{name: "nil_coord", method: http.MethodPost, body: `{"muted":true}`, nilCoord: true, wantCode: http.StatusInternalServerError},
		{name: "string_muted", method: http.MethodPost, body: `{"muted":"true"}`, wantCode: http.StatusBadRequest},
		{name: "null_muted", method: http.MethodPost, body: `{"muted":null}`, wantCode: http.StatusBadRequest},
		{name: "invalid_json", method: http.MethodPost, body: `{invalid`, wantCode: http.StatusBadRequest},
		{name: "coord_error", method: http.MethodPost, body: `{}`, coordErr: errors.New("err"), wantCode: http.StatusInternalServerError},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := doAudioReq(audioHandler(tc.nilCoord, tc.coordErr).PostAudioMute, tc.method, "/api/audio/mute", tc.body)
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantCode == http.StatusOK {
				checkAudioResp(t, rec, 0, tc.wantMuted)
			}
		})
	}
}
