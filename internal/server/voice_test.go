package server_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/server"
	"github.com/azylman/mirrormere/internal/voice"
)

type mockVoiceCoord struct {
	state      string
	transcript *string
	reply      *string
	ttsEngine  *string
	setErr     error
}

func (m *mockVoiceCoord) GetState() events.VoiceStateData {
	return events.VoiceStateData{State: m.state, Transcript: m.transcript, Reply: m.reply, TTSEngine: m.ttsEngine}
}

func (m *mockVoiceCoord) SetState(state string, transcript, reply, ttsEngine *string) (events.VoiceStateData, error) {
	if m.setErr != nil {
		return events.VoiceStateData{}, m.setErr
	}
	m.state, m.transcript, m.reply, m.ttsEngine = state, transcript, reply, ttsEngine
	return m.GetState(), nil
}

type sseEvt struct {
	event string
	data  any
}

type mockVoiceHub struct {
	enabled           bool
	err               error
	events            []sseEvt
	capturedNodeID    string
	capturedSessionID string
	capturedTimings   voice.EdgeTimings
}

func (m *mockVoiceHub) IsEnabled() bool { return m.enabled }

func (m *mockVoiceHub) Interact(ctx context.Context, audio io.Reader, nodeID, sessionID string, sink voice.SSEEventSink, timings voice.EdgeTimings) error {
	m.capturedNodeID, m.capturedSessionID, m.capturedTimings = nodeID, sessionID, timings
	for _, evt := range m.events {
		if err := sink(evt.event, evt.data); err != nil {
			return err
		}
	}
	return m.err
}

var dummyWAV = append([]byte("RIFF1234WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"), make([]byte, 100)...)

func createMultipartReq(method, url, fieldName string, extraFields map[string]string) *http.Request {
	var b bytes.Buffer
	w := multipart.NewWriter(&b)
	if fieldName != "" {
		part, _ := w.CreateFormFile(fieldName, "test.wav")
		if part != nil {
			_, _ = part.Write(dummyWAV)
		}
	}
	for k, v := range extraFields {
		_ = w.WriteField(k, v)
	}
	_ = w.Close()
	req := httptest.NewRequest(method, url, &b)
	req.Header.Set("Content-Type", w.FormDataContentType())
	return req
}

type nonFlusherResponseWriter struct {
	header http.Header
	code   int
	body   bytes.Buffer
}

func (w *nonFlusherResponseWriter) Header() http.Header         { return w.header }
func (w *nonFlusherResponseWriter) Write(b []byte) (int, error) { return w.body.Write(b) }
func (w *nonFlusherResponseWriter) WriteHeader(code int) {
	if w.code == 0 {
		w.code = code
	}
}

func TestDefaultVoiceHandler_PostVoiceState(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		method    string
		body      string
		nilCoord  bool
		coordErr  error
		wantCode  int
		wantState string
	}{
		{"ok_state", http.MethodPost, `{"state":"listening"}`, false, nil, http.StatusOK, "listening"},
		{"ok_full_state", http.MethodPost, `{"state":"speaking","transcript":"hi","reply":"hello","tts_engine":"kokoro"}`, false, nil, http.StatusOK, "speaking"},
		{"options_cors", http.MethodOptions, "", false, nil, http.StatusNoContent, ""},
		{"method_not_allowed", http.MethodGet, "", false, nil, http.StatusMethodNotAllowed, ""},
		{"nil_coord", http.MethodPost, `{"state":"listening"}`, true, nil, http.StatusInternalServerError, ""},
		{"bad_json", http.MethodPost, `{bad`, false, nil, http.StatusBadRequest, ""},
		{"invalid_state", http.MethodPost, `{"state":"invalid"}`, false, nil, http.StatusBadRequest, ""},
		{"coord_err", http.MethodPost, `{"state":"listening"}`, false, errors.New("coord err"), http.StatusBadRequest, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var coord server.VoiceCoordinator
			var mock *mockVoiceCoord
			if !tc.nilCoord {
				mock = &mockVoiceCoord{setErr: tc.coordErr}
				coord = mock
			}
			h := server.NewDefaultVoiceHandler(coord, nil)
			rec := httptest.NewRecorder()
			h.PostVoiceState(rec, httptest.NewRequest(tc.method, "/api/voice/state", strings.NewReader(tc.body)))
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
			if tc.wantState != "" && mock != nil && mock.state != tc.wantState {
				t.Fatalf("state = %s, want %s", mock.state, tc.wantState)
			}
		})
	}
}

func TestDefaultVoiceHandler_PostVoiceHeartbeat(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name        string
		method      string
		body        string
		withMetrics bool
		wantCode    int
	}{
		{"ok_heartbeat", http.MethodPost, `{"node_id":"kiosk","ambient_rms_dbfs":-42.5,"false_wakes":2,"last_playback_sec":15.0}`, true, http.StatusOK},
		{"nil_metrics", http.MethodPost, `{"node_id":"kiosk"}`, false, http.StatusOK},
		{"zero_playback", http.MethodPost, `{"node_id":"kiosk","last_playback_sec":0}`, true, http.StatusOK},
		{"options_cors", http.MethodOptions, "", true, http.StatusNoContent},
		{"method_not_allowed", http.MethodGet, "", true, http.StatusMethodNotAllowed},
		{"bad_json", http.MethodPost, `{bad`, true, http.StatusBadRequest},
		{"missing_node_id", http.MethodPost, `{"node_id":""}`, true, http.StatusBadRequest},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var opts []server.DefaultVoiceHandlerOption
			if tc.withMetrics {
				opts = append(opts, server.WithVoiceMetrics(voice.DefaultMetrics()))
			}
			h := server.NewDefaultVoiceHandler(nil, nil, opts...)
			rec := httptest.NewRecorder()
			h.PostVoiceHeartbeat(rec, httptest.NewRequest(tc.method, "/api/voice/heartbeat", strings.NewReader(tc.body)))
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d", rec.Code, tc.wantCode)
			}
		})
	}
}

func TestDefaultVoiceHandler_PostVoiceInteract(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name        string
		method      string
		nilHub      bool
		hubEnabled  bool
		nonFlusher  bool
		audioField  string
		extraFields map[string]string
		badMulti    bool
		hubErr      error
		hubEvents   []sseEvt
		wantCode    int
		wantNodeID  string
		wantEvalSec float64
	}{
		{"ok_stream", http.MethodPost, false, true, false, "audio", map[string]string{"node_id": "n1", "session_id": "s1"}, false, nil, []sseEvt{{"status", "listening"}}, http.StatusOK, "n1", 0},
		{"timings_parsed", http.MethodPost, false, true, false, "audio", map[string]string{"node_id": "n1", "wake_eval_ms": "123.4", "speech_duration_ms": "1000", "silence_duration_ms": "500"}, false, nil, nil, http.StatusOK, "n1", 0.1234},
		{"timings_invalid", http.MethodPost, false, true, false, "audio", map[string]string{"wake_eval_ms": "invalid", "speech_duration_ms": "999999"}, false, nil, nil, http.StatusOK, "", 0},
		{"options_cors", http.MethodOptions, false, true, false, "audio", nil, false, nil, nil, http.StatusNoContent, "", 0},
		{"method_not_allowed", http.MethodGet, false, true, false, "audio", nil, false, nil, nil, http.StatusMethodNotAllowed, "", 0},
		{"nil_hub", http.MethodPost, true, false, false, "audio", nil, false, nil, nil, http.StatusServiceUnavailable, "", 0},
		{"hub_disabled", http.MethodPost, false, false, false, "audio", nil, false, nil, nil, http.StatusServiceUnavailable, "", 0},
		{"non_flusher", http.MethodPost, false, true, true, "audio", nil, false, nil, nil, http.StatusInternalServerError, "", 0},
		{"bad_multipart", http.MethodPost, false, true, false, "", nil, true, nil, nil, http.StatusBadRequest, "", 0},
		{"missing_audio", http.MethodPost, false, true, false, "", map[string]string{"node_id": "n1"}, false, nil, nil, http.StatusBadRequest, "", 0},
		{"hub_invalid_audio", http.MethodPost, false, true, false, "audio", nil, false, voice.ErrInvalidAudio, nil, http.StatusBadRequest, "", 0},
		{"hub_busy", http.MethodPost, false, true, false, "audio", nil, false, voice.ErrInteractionBusy, nil, http.StatusConflict, "", 0},
		{"hub_err_disabled", http.MethodPost, false, true, false, "audio", nil, false, voice.ErrHubDisabled, nil, http.StatusServiceUnavailable, "", 0},
		{"hub_generic_err", http.MethodPost, false, true, false, "audio", nil, false, errors.New("fail"), nil, http.StatusInternalServerError, "", 0},
		{"err_after_header", http.MethodPost, false, true, false, "audio", nil, false, errors.New("stream cut"), []sseEvt{{"chunk", "data"}}, http.StatusOK, "", 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var hub server.VoiceHub
			var mock *mockVoiceHub
			if !tc.nilHub {
				mock = &mockVoiceHub{enabled: tc.hubEnabled, err: tc.hubErr, events: tc.hubEvents}
				hub = mock
			}
			h := server.NewDefaultVoiceHandler(nil, hub)
			var req *http.Request
			if tc.badMulti {
				req = httptest.NewRequest(tc.method, "/api/voice/interact", strings.NewReader("bad-multipart"))
				req.Header.Set("Content-Type", "multipart/form-data; boundary=---")
			} else {
				req = createMultipartReq(tc.method, "/api/voice/interact", tc.audioField, tc.extraFields)
			}
			var rec http.ResponseWriter
			recRec := httptest.NewRecorder()
			rec = recRec
			if tc.nonFlusher {
				rec = &nonFlusherResponseWriter{header: make(http.Header)}
			}
			h.PostVoiceInteract(rec, req)

			code := recRec.Code
			if tc.nonFlusher {
				code = rec.(*nonFlusherResponseWriter).code
			}
			if code != tc.wantCode {
				t.Fatalf("code = %d, want %d", code, tc.wantCode)
			}
			if tc.wantNodeID != "" && mock != nil && mock.capturedNodeID != tc.wantNodeID {
				t.Fatalf("nodeID = %s, want %s", mock.capturedNodeID, tc.wantNodeID)
			}
			if tc.wantEvalSec > 0 && mock != nil && (mock.capturedTimings.WakeEvalSec-tc.wantEvalSec > 1e-5 || tc.wantEvalSec-mock.capturedTimings.WakeEvalSec > 1e-5) {
				t.Fatalf("wakeEvalSec = %v, want %v", mock.capturedTimings.WakeEvalSec, tc.wantEvalSec)
			}
		})
	}
}
