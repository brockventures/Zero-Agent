package server_test

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/server"
)

// These tests share the process-wide default registry that /metrics exposes, so they
// assert deltas on the histograms rather than absolute counts and do not run in parallel.

func doPerfReq(srv *server.Server, method, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "/api/perf", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, req)
	return rec
}

// perfHistogramCount returns the current _count of a display timing histogram series, parsed
// from the /metrics exposition, or 0 if the series has not been observed yet.
func perfHistogramCount(t *testing.T, srv *server.Server, series string) float64 {
	t.Helper()
	rec := httptest.NewRecorder()
	srv.Routes().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	for line := range strings.SplitSeq(rec.Body.String(), "\n") {
		if !strings.HasPrefix(line, series+" ") {
			continue
		}
		v, err := strconv.ParseFloat(strings.TrimPrefix(line, series+" "), 64)
		if err != nil {
			t.Fatalf("parse %q: %v", line, err)
		}
		return v
	}
	return 0
}

func TestPerf_ValidBatchObservesHistograms(t *testing.T) {
	srv := server.New(server.Config{})
	swipeSeries := `mirrormere_display_swipe_settle_ms_count{ui="legacy"}`
	frameSeries := `mirrormere_display_frame_ms_count{ui="react"}`
	swipeBefore := perfHistogramCount(t, srv, swipeSeries)
	frameBefore := perfHistogramCount(t, srv, frameSeries)

	body := `{"samples":[{"metric":"swipe_settle","value_ms":42.5,"ui":"legacy"},{"metric":"frame","value_ms":16.7,"ui":"react"}]}`
	rec := doPerfReq(srv, http.MethodPost, body)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want %d; body=%s", rec.Code, http.StatusNoContent, rec.Body.String())
	}
	if got := perfHistogramCount(t, srv, swipeSeries) - swipeBefore; got != 1 {
		t.Errorf("swipe_settle count delta = %v, want 1", got)
	}
	if got := perfHistogramCount(t, srv, frameSeries) - frameBefore; got != 1 {
		t.Errorf("frame count delta = %v, want 1", got)
	}
}

func TestPerf_BucketsIncludeBoundary(t *testing.T) {
	srv := server.New(server.Config{})
	series := `mirrormere_display_screen_ready_ms_bucket{ui="legacy",le="16.7"}`
	before := perfHistogramCount(t, srv, series)
	rec := doPerfReq(srv, http.MethodPost, `{"samples":[{"metric":"screen_ready","value_ms":16.7,"ui":"legacy"}]}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
	// Upper bounds are inclusive, so 16.7 lands in the le="16.7" bucket.
	if got := perfHistogramCount(t, srv, series) - before; got != 1 {
		t.Errorf("le=16.7 bucket delta = %v, want 1", got)
	}
}

func TestPerf_RejectedBatchObservesNothing(t *testing.T) {
	srv := server.New(server.Config{})
	series := `mirrormere_display_long_task_ms_count{ui="legacy"}`
	before := perfHistogramCount(t, srv, series)
	body := `{"samples":[{"metric":"long_task","value_ms":80,"ui":"legacy"},{"metric":"bogus","value_ms":1,"ui":"legacy"}]}`
	if rec := doPerfReq(srv, http.MethodPost, body); rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	if got := perfHistogramCount(t, srv, series) - before; got != 0 {
		t.Errorf("partial batch observed %v samples, want 0", got)
	}
}

func TestPerf_Validation(t *testing.T) {
	tests := []struct {
		name     string
		body     string
		wantCode int
		wantSub  string
	}{
		{"unknown_metric", `{"samples":[{"metric":"nope","value_ms":1,"ui":"legacy"}]}`, http.StatusBadRequest, "unknown metric"},
		{"bad_ui", `{"samples":[{"metric":"frame","value_ms":1,"ui":"web"}]}`, http.StatusBadRequest, "ui must be"},
		{"negative_value", `{"samples":[{"metric":"frame","value_ms":-1,"ui":"legacy"}]}`, http.StatusBadRequest, "value_ms"},
		{"empty_samples", `{"samples":[]}`, http.StatusBadRequest, "between 1 and 200"},
		{"missing_samples", `{}`, http.StatusBadRequest, "between 1 and 200"},
		{"malformed_json", `{"samples":`, http.StatusBadRequest, "invalid JSON body"},
		{"oversize_batch", oversizeBatch(), http.StatusBadRequest, "between 1 and 200"},
		{"oversize_body", `{"samples":[{"metric":"frame","value_ms":1,"ui":"legacy"}],"pad":"` + strings.Repeat("x", 70<<10) + `"}`, http.StatusBadRequest, "invalid JSON body"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := server.New(server.Config{})
			rec := doPerfReq(srv, http.MethodPost, tc.body)
			if rec.Code != tc.wantCode {
				t.Fatalf("status = %d, want %d; body=%s", rec.Code, tc.wantCode, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), tc.wantSub) {
				t.Errorf("body = %q, want substring %q", rec.Body.String(), tc.wantSub)
			}
		})
	}
}

func TestPerf_MaxBatchAccepted(t *testing.T) {
	srv := server.New(server.Config{})
	if rec := doPerfReq(srv, http.MethodPost, fullBatch()); rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204 for a 200-sample batch; body=%s", rec.Code, rec.Body.String())
	}
}

func TestPerf_WrongMethod(t *testing.T) {
	srv := server.New(server.Config{})
	rec := doPerfReq(srv, http.MethodGet, "")
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", rec.Code)
	}
	if got := rec.Header().Get("Allow"); got != http.MethodPost {
		t.Errorf("Allow = %q, want POST", got)
	}
}

func oversizeBatch() string {
	var b strings.Builder
	b.WriteString(`{"samples":[`)
	for i := range 201 {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(`{"metric":"frame","value_ms":16,"ui":"legacy"}`)
	}
	b.WriteString(`]}`)
	return b.String()
}

func fullBatch() string {
	var b strings.Builder
	b.WriteString(`{"samples":[`)
	for i := range 200 {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, `{"metric":"interaction","value_ms":%d,"ui":"legacy"}`, i)
	}
	b.WriteString(`]}`)
	return b.String()
}
