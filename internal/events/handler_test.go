package events

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type flushingRecorder struct {
	*httptest.ResponseRecorder

	mu      sync.Mutex
	flushCh chan struct{}
}

func newFlushingRecorder() *flushingRecorder {
	return &flushingRecorder{ResponseRecorder: httptest.NewRecorder(), flushCh: make(chan struct{}, 100)}
}

func (f *flushingRecorder) Write(b []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.ResponseRecorder.Write(b)
}

func (f *flushingRecorder) Flush() {
	select {
	case f.flushCh <- struct{}{}:
	default:
	}
}

func (f *flushingRecorder) BodyString() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.Body.String()
}

func (f *flushingRecorder) wait(n int) {
	for range n {
		select {
		case <-f.flushCh:
		case <-time.After(time.Second):
		}
	}
}

type nonFlusher struct {
	h    http.Header
	code int
	buf  bytes.Buffer
}

func (n *nonFlusher) Header() http.Header         { return n.h }
func (n *nonFlusher) Write(b []byte) (int, error) { return n.buf.Write(b) }
func (n *nonFlusher) WriteHeader(c int)           { n.code = c }

type failFlusher struct{ nonFlusher }

func (f *failFlusher) Write([]byte) (int, error) { return 0, errors.New("e") }
func (f *failFlusher) Flush()                    {}

type hookStateProvider struct {
	StateProvider

	onHydration func()
}

func (h *hookStateProvider) GetScreenRotateData() *ScreenRotateData {
	h.onHydration()
	return h.StateProvider.GetScreenRotateData()
}

type slowWriterRecorder struct {
	*flushingRecorder

	blockCh, entered chan struct{}
	liveBlock        bool
}

func (s *slowWriterRecorder) Write(b []byte) (int, error) {
	if s.liveBlock && strings.Contains(string(b), "slow_1") {
		s.entered <- struct{}{}
		<-s.blockCh
	}
	return s.flushingRecorder.Write(b)
}

func TestHandler_MethodsAndValidation(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, method, kind string
		wantCode           int
		wantSub            string
	}{
		{"options_preflight", http.MethodOptions, "flush", http.StatusNoContent, ""},
		{"method_not_allowed", http.MethodPost, "flush", http.StatusMethodNotAllowed, "method not allowed"},
		{"non_flusher", http.MethodGet, "non_flush", http.StatusInternalServerError, "streaming unsupported"},
		{"fail_write_initial_batch", http.MethodGet, "fail_write", http.StatusOK, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			hub := NewHub(HubConfig{}, nil, nil)
			defer hub.Close()
			rec := newFlushingRecorder()
			var w http.ResponseWriter = rec
			var nf *nonFlusher
			switch tc.kind {
			case "non_flush":
				nf = &nonFlusher{h: make(http.Header)}
				w = nf
			case "fail_write":
				ff := &failFlusher{nonFlusher{h: make(http.Header)}}
				nf, w = &ff.nonFlusher, ff
			}
			Handler(hub).ServeHTTP(w, httptest.NewRequest(tc.method, "/api/events", nil))
			code, body := rec.Code, rec.BodyString()
			if nf != nil {
				code, body = nf.code, nf.buf.String()
			}
			if code != tc.wantCode || (tc.wantSub != "" && !strings.Contains(body, tc.wantSub)) || (tc.name == "options_preflight" && w.Header().Get("Access-Control-Allow-Origin") != "*") {
				t.Fatalf("got %d %s, want %d %s", code, body, tc.wantCode, tc.wantSub)
			}
		})
	}
}

type replayTC struct {
	n, m, p, hk, ws, wn string
	ex                  int
}

func rtc(n, m, p, hk, ws, wn string, ex int) replayTC { return replayTC{n, m, p, hk, ws, wn, ex} }

func TestHandler_InitialHydration(t *testing.T) {
	t.Parallel()
	for _, tc := range []replayTC{
		rtc("fresh_initial_hydration", "sync", "/api/events", "", "event: screen.rotate", "", 0),
		rtc("header_replay", "sync", "/api/events", "Last-Event-ID", "card2", "card1", 0),
		rtc("query_lastEventId", "sync", "/api/events?lastEventId=", "", "card2", "card1", 0),
		rtc("query_snake_case", "sync", "/api/events?last_event_id=", "", "card2", "", 0),
		rtc("unknown_id_fallback", "sync", "/api/events", "Last-Event-ID", "event: screen.rotate", "", 0),
		rtc("during_hydration", "during", "/api/events", "", "simulated failure", "", 0),
		rtc("replay_deduplication", "live_after_replay", "/api/events", "Last-Event-ID", "card2", "card1", 1),
		rtc("up_to_date_replay", "live_up_to_date", "/api/events", "Last-Event-ID", "card2", "card1", 0),
	} {
		t.Run(tc.n, func(t *testing.T) {
			t.Parallel()
			baseProvider := NewInMemoryStateProvider(nil)
			hub := NewHub(HubConfig{}, baseProvider, nil)
			defer hub.Close()
			evt1 := hub.Publish(EventWidgetReload, []byte(`{"type":"card1"}`))
			evt2 := hub.Publish(EventWidgetReload, []byte(`{"type":"card2"}`))
			reqPath := tc.p
			if strings.HasSuffix(reqPath, "=") {
				reqPath += evt1.ID
			}
			rec := newFlushingRecorder()

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			req := httptest.NewRequest(http.MethodGet, reqPath, nil).WithContext(ctx)
			if tc.hk != "" {
				hVal := evt1.ID
				if tc.n == "unknown_id_fallback" {
					hVal = "unknown_999"
				}
				if tc.m == "live_up_to_date" {
					hVal = evt2.ID
				}
				req.Header.Set(tc.hk, hVal)
			}
			if tc.m == "during" {
				hub.stateProvider = &hookStateProvider{baseProvider, func() {
					hub.Publish(EventSystemStatus, []byte(`{"config_error":"simulated failure"}`))
				}}
			}
			if tc.m == "sync" {
				cancel()
				Handler(hub).ServeHTTP(rec, req)
			} else {
				done := make(chan struct{})
				go func() {
					defer close(done)
					Handler(hub).ServeHTTP(rec, req)
				}()
				rec.wait(2)
				typ := "card2"
				if tc.m == "live_after_replay" {
					typ = "card3"
				}
				hub.Publish(EventWidgetReload, []byte(`{"type":"`+typ+`"}`))
				rec.wait(1)
				cancel()
				<-done
			}
			body := rec.BodyString()
			if (tc.ex > 0 && strings.Count(body, tc.ws) != tc.ex) || !strings.Contains(body, tc.ws) || (tc.wn != "" && strings.Contains(body, tc.wn)) {
				t.Fatalf("unexpected body: %s", body)
			}
		})
	}
}

func TestHandler_LiveStreamingAndHeartbeat(t *testing.T) {
	t.Parallel()
	hub := NewHub(HubConfig{HeartbeatInterval: 10 * time.Millisecond}, nil, nil)
	defer hub.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	rec := newFlushingRecorder()
	go Handler(hub).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/events", nil).WithContext(ctx))

	rec.wait(2)
	hub.Publish(EventStyleReload, []byte(`{"file":"custom.css"}`))
	rec.wait(2)
	cancel()

	if b := rec.BodyString(); !strings.Contains(b, "event: style.reload") || !strings.Contains(b, ": ping") {
		t.Fatalf("missing reload/ping: %s", b)
	}
}

func TestHandler_SlowConsumerDisconnect(t *testing.T) {
	t.Parallel()
	hub := NewHub(HubConfig{SubscriberBuffer: 1}, nil, nil)
	defer hub.Close()
	blockCh := make(chan struct{})
	rec := &slowWriterRecorder{flushingRecorder: newFlushingRecorder(), blockCh: blockCh, entered: make(chan struct{}, 1), liveBlock: true}
	go Handler(hub).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/events", nil))

	rec.wait(2)
	hub.Publish(EventWidgetUpdate, []byte("slow_1"))
	<-rec.entered
	hub.Publish(EventWidgetUpdate, []byte("slow_2"))
	hub.Publish(EventWidgetUpdate, []byte("slow_3"))

	hub.mu.RLock()
	n := len(hub.subscribers)
	hub.mu.RUnlock()
	if n != 0 {
		t.Fatalf("expected evicted, got %d", n)
	}
	close(blockCh)
}
