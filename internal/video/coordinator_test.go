package video

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/events"
)

func WithHTTPClient(client *http.Client) CoordinatorOption {
	return func(c *Coordinator) {
		if client != nil {
			c.httpClient = client
		}
	}
}

func WithTimerFunc(tf TimerFunc) CoordinatorOption {
	return func(c *Coordinator) {
		if tf != nil {
			c.timerFunc = tf
		}
	}
}

type mockTimer struct {
	stopped bool
	fn      func()
}

func (m *mockTimer) Stop() bool {
	m.stopped = true
	return true
}

func (m *mockTimer) fire() {
	if !m.stopped && m.fn != nil {
		m.fn()
	}
}

type mockTimerFactory struct {
	mu     sync.Mutex
	timers []*mockTimer
}

func (f *mockTimerFactory) TimerFunc(_ time.Duration, fn func()) Timer {
	f.mu.Lock()
	defer f.mu.Unlock()
	t := &mockTimer{fn: fn}
	f.timers = append(f.timers, t)
	return t
}

func (f *mockTimerFactory) fireAll() {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, t := range f.timers {
		t.fire()
	}
}

type mockSink struct {
	mu    sync.Mutex
	state *events.VideoStateData
}

func (s *mockSink) SetVideoState(st *events.VideoStateData) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state = st
}

func mkStream(id, url, sType, prio string, timeout int) VideoStream {
	return VideoStream{ID: id, StreamURL: url, Type: sType, Priority: prio, TimeoutSeconds: timeout}
}

func TestCoordinator_ValidationAndClose(t *testing.T) {
	t.Parallel()
	sink := &mockSink{}
	hub := events.NewHub(events.HubConfig{}, nil, nil)
	t.Cleanup(hub.Close)
	coord := NewCoordinator(hub, WithSink(sink))
	t.Cleanup(coord.Close)

	if st := coord.GetState(); st.Mode != ModeWidgets || st.Primary != nil || st.Pip != nil {
		t.Fatalf("unexpected init state: %+v", st)
	}
	if sink.state == nil || sink.state.Mode != ModeWidgets {
		t.Fatalf("unexpected seeded sink: %+v", sink.state)
	}

	tests := []struct {
		name    string
		stream  VideoStream
		dismiss string
		isDis   bool
		wantErr error
	}{
		{"trig_empty", VideoStream{}, "", false, ErrInvalidStream},
		{"trig_no_url", VideoStream{ID: "c"}, "", false, ErrInvalidStream},
		{"trig_res_all", VideoStream{ID: "all", StreamURL: "u"}, "", false, ErrInvalidStream},
		{"trig_res_star", VideoStream{ID: "*", StreamURL: "u"}, "", false, ErrInvalidStream},
		{"trig_bad_type", VideoStream{ID: "c", StreamURL: "u", Type: "mp4"}, "", false, ErrInvalidStream},
		{"trig_bad_prio", VideoStream{ID: "c", StreamURL: "u", Priority: "urgent"}, "", false, ErrInvalidStream},
		{"trig_bad_ps", VideoStream{ID: "c", StreamURL: "u", PlayerState: "stopped"}, "", false, ErrInvalidPlayerState},
		{"dis_empty", VideoStream{}, "", true, ErrInvalidStream},
		{"dis_missing", VideoStream{}, "missing", true, ErrStreamNotFound},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			action := coord.Trigger
			if tc.isDis {
				action = func(VideoStream) (VideoState, error) { return coord.Dismiss(tc.dismiss) }
			}
			if _, err := action(tc.stream); !errors.Is(err, tc.wantErr) {
				t.Fatalf("got %v, want %v", err, tc.wantErr)
			}
		})
	}

	if _, err := coord.Dismiss("all"); !errors.Is(err, ErrStreamNotFound) {
		t.Fatalf("expected ErrStreamNotFound on idle Dismiss, got %v", err)
	}

	coord.Close()
	_, e1 := coord.Trigger(mkStream("c", "u", "", "", 0))
	_, e2 := coord.Dismiss("c")
	_, e3 := coord.Dismiss("all")
	_, e4 := coord.UpdatePlayerState("c", VideoStream{PlayerState: PlayerStatePlaying})
	for _, err := range []error{e1, e2, e3, e4} {
		if !errors.Is(err, ErrStreamNotFound) {
			t.Fatal("expected ErrStreamNotFound on closed coordinator")
		}
	}

	var capturedCB func()
	c2 := NewCoordinator(nil, WithTimerFunc(func(_ time.Duration, f func()) Timer {
		capturedCB = f
		return &mockTimer{}
	}))
	_, _ = c2.Trigger(mkStream("t", "u", "", PriorityTemporary, 10))
	c2.Close()
	if capturedCB != nil {
		capturedCB()
	}
	if (&realTimer{}).Stop() {
		t.Fatal("expected false on nil timer Stop")
	}
}

func TestCoordinator_PriorityStackAndTimers(t *testing.T) {
	t.Parallel()
	tf := &mockTimerFactory{}
	sink := &mockSink{}
	hub := events.NewHub(events.HubConfig{}, nil, nil)
	defer hub.Close()
	subCtx, subCancel := context.WithCancel(context.Background())
	defer subCancel()
	subCh, unsub := hub.Subscribe(subCtx)
	defer unsub()

	coord := NewCoordinator(hub, WithSink(sink), WithTimerFunc(tf.TimerFunc))
	defer coord.Close()

	// 1-6. Priority stack transitions
	s1, _ := coord.Trigger(mkStream("chromecast", "http://cast", TypeWebRTC, PriorityPersistent, 0))
	select {
	case evt := <-subCh:
		if evt.Type != events.EventVideoState {
			t.Fatalf("expected video.state event, got %s", evt.Type)
		}
	default:
		t.Fatal("expected SSE event published")
	}
	s2, _ := coord.Trigger(mkStream("doorbell", "http://doorbell", TypeWebRTC, PriorityTemporary, 30))
	s3, _ := coord.Trigger(mkStream("backyard", "http://backyard", TypeWebRTC, PriorityTemporary, 0))
	s4, _ := coord.Trigger(mkStream("backyard", "http://backyard-hd", TypeHLS, PriorityTemporary, 0))
	s5, _ := coord.Dismiss("chromecast")
	s6, _ := coord.Dismiss("backyard")
	if s1.Mode != ModeVideo || s1.Primary == nil || s1.Primary.Muted || s1.Pip != nil ||
		s2.Primary.ID != "chromecast" || s2.Pip == nil || s2.Pip.ID != "doorbell" || !s2.Pip.Muted ||
		s3.Pip.ID != "backyard" || !s3.Pip.Muted ||
		s4.Pip.StreamURL != "http://backyard-hd" || s4.Pip.Type != TypeHLS || !s4.Pip.Muted ||
		s5.Primary == nil || s5.Primary.ID != "backyard" || s5.Primary.Muted || s5.Pip != nil ||
		s6.Mode != ModeWidgets || s6.Primary != nil || s6.Pip != nil {
		t.Fatalf("unexpected priority stack transitions: s1=%+v s2=%+v s3=%+v s4=%+v s5=%+v s6=%+v", s1, s2, s3, s4, s5, s6)
	}

	// 7. Demotion and timer expiry
	_, _ = coord.Trigger(mkStream("doorbell", "http://doorbell", "", PriorityTemporary, 45))
	s7, _ := coord.Trigger(mkStream("chromecast", "http://cast", "", PriorityPersistent, 0))
	tf.fireAll()
	st7 := coord.GetState()
	if s7.Primary.ID != "chromecast" || s7.Pip == nil || !s7.Pip.Muted || st7.Primary == nil || st7.Pip != nil {
		t.Fatalf("unexpected demotion/timer: s7=%+v st7=%+v", s7, st7)
	}

	// 8-11. Primary timer expiry, Dismiss("all"), and Dismiss("*")
	_, _ = coord.Dismiss("chromecast")
	_, _ = coord.Trigger(mkStream("alert", "http://alert", "", PriorityTemporary, 45))
	tf.fireAll()
	_, _ = coord.Trigger(mkStream("p", "http://p", "", PriorityPersistent, 0))
	_, _ = coord.Trigger(mkStream("a", "http://a", "", PriorityTemporary, 30))
	sAll, _ := coord.Dismiss("all")
	_, _ = coord.Trigger(mkStream("p", "http://p", "", PriorityPersistent, 0))
	sW, _ := coord.Dismiss("*")
	defSnap, _ := coord.Trigger(VideoStream{ID: "def", StreamURL: "http://def"})
	if sAll.Mode != ModeWidgets || sW.Mode != ModeWidgets || defSnap.Primary.Priority != PriorityPersistent || defSnap.Primary.TimeoutSeconds != 0 {
		t.Fatal("unexpected dismiss/defaults")
	}
}

func TestCoordinator_PlayerStateAndMetadata(t *testing.T) {
	t.Parallel()
	coord := NewCoordinator(nil)
	defer coord.Close()

	_, _ = coord.Trigger(mkStream("cast", "http://cast", "", PriorityPersistent, 0))
	_, _ = coord.Trigger(mkStream("doorbell", "http://doorbell", "", PriorityTemporary, 45))

	for _, tc := range []struct {
		id, state string
		wantPS    string
		wantErr   error
	}{
		{"cast", PlayerStatePaused, PlayerStatePaused, nil},
		{"doorbell", PlayerStateBuffering, PlayerStateBuffering, nil},
		{"cast", PlayerStateIdle, PlayerStateIdle, nil},
		{"cast", "unknown", "", ErrInvalidPlayerState},
		{"missing", PlayerStatePlaying, "", ErrStreamNotFound},
		{"", PlayerStatePlaying, "", ErrInvalidStream},
	} {
		snap, err := coord.UpdatePlayerState(tc.id, VideoStream{PlayerState: tc.state})
		if !errors.Is(err, tc.wantErr) {
			t.Fatalf("UpdatePlayerState(%s, %s) err=%v, want %v", tc.id, tc.state, err, tc.wantErr)
		}
		if tc.wantErr == nil {
			var ps string
			if tc.id == "cast" && snap.Primary != nil {
				ps = snap.Primary.PlayerState
			}
			if tc.id == "doorbell" && snap.Pip != nil {
				ps = snap.Pip.PlayerState
			}
			if ps != tc.wantPS {
				t.Fatalf("got player state %s, want %s", ps, tc.wantPS)
			}
		}
	}

	// Metadata preservation and patching
	sMeta, _ := coord.Trigger(VideoStream{
		ID: "chromecast", StreamURL: "http://cast", Priority: PriorityPersistent,
		Title: "Episode 1", Series: "Show A", Season: "1", Episode: "1", Position: 10.5, Duration: 3600,
	})
	sPatch, _ := coord.UpdatePlayerState("chromecast", VideoStream{PlayerState: PlayerStatePaused, Position: 25.0})
	sRetrig, _ := coord.Trigger(VideoStream{ID: "chromecast", StreamURL: "http://cast-hd"})
	sPurge, _ := coord.UpdatePlayerState("chromecast", VideoStream{PlayerState: PlayerStateIdle})
	if sMeta.Primary.Title != "Episode 1" || sMeta.Primary.Position != 10.5 || sMeta.Primary.PositionUpdatedAt == 0 ||
		sPatch.Primary.Title != "Episode 1" || sPatch.Primary.Position != 25.0 || sPatch.Primary.PlayerState != PlayerStatePaused ||
		sRetrig.Primary.StreamURL != "http://cast-hd" || sRetrig.Primary.Title != "Episode 1" || sRetrig.Primary.Position != 25.0 ||
		sPurge.Primary.Title != "" || sPurge.Primary.Position != 0 || sPurge.Primary.Duration != 0 {
		t.Fatal("unexpected metadata preservation")
	}

	// Verify switching from TV episode to movie title clears Series, Season, Episode while preserving Duration if omitted (#613)
	_, _ = coord.Trigger(VideoStream{
		ID: "chromecast", StreamURL: "http://cast-tv", Priority: PriorityPersistent,
		Title: "Ep 1", Series: "Show A", Season: "1", Episode: "2", Duration: 3600,
	})
	sMovie, err := coord.UpdatePlayerState("chromecast", VideoStream{
		PlayerState: PlayerStatePlaying,
		Title:       "Movie Title",
	})
	if err != nil || sMovie.Primary.Title != "Movie Title" || sMovie.Primary.Series != "" || sMovie.Primary.Season != "" || sMovie.Primary.Episode != "" || sMovie.Primary.Duration != 3600 {
		t.Fatalf("expected series/season/episode cleared and duration preserved on movie title update, got: %+v (err: %v)", sMovie.Primary, err)
	}
	// Verify re-triggering active TV stream with a movie Title does not backfill stale Series/Season/Episode
	_, _ = coord.Trigger(VideoStream{
		ID: "chromecast", StreamURL: "http://cast-tv", Priority: PriorityPersistent,
		Title: "Ep 2", Series: "Show A", Season: "1", Episode: "3", Duration: 1800,
	})
	sRetrigMovie, err := coord.Trigger(VideoStream{
		ID: "chromecast", StreamURL: "http://cast-movie", Priority: PriorityPersistent,
		Title: "Standalone Movie",
	})
	if err != nil || sRetrigMovie.Primary.Title != "Standalone Movie" || sRetrigMovie.Primary.Series != "" || sRetrigMovie.Primary.Season != "" || sRetrigMovie.Primary.Episode != "" || sRetrigMovie.Primary.Duration != 0 {
		t.Fatalf("expected re-triggered movie stream not to inherit stale episode metadata, got: %+v (err: %v)", sRetrigMovie.Primary, err)
	}

	for _, check := range []struct {
		id, ps  string
		wantErr error
	}{
		{"", PlayerStatePlaying, ErrInvalidStream},
		{"missing", PlayerStatePlaying, ErrStreamNotFound},
		{"chromecast", "invalid", ErrInvalidPlayerState},
	} {
		if _, err := coord.UpdatePlayerState(check.id, VideoStream{PlayerState: check.ps}); !errors.Is(err, check.wantErr) {
			t.Fatalf("UpdatePlayerState(%s, %s) got %v, want %v", check.id, check.ps, err, check.wantErr)
		}
	}

	_, _ = coord.Trigger(VideoStream{ID: "pip-1", StreamURL: "http://pip", Priority: PriorityTemporary, TimeoutSeconds: 45, Position: 50.0})
	sPipRetrig, _ := coord.Trigger(VideoStream{ID: "pip-1", StreamURL: "http://pip-updated", Priority: PriorityTemporary, TimeoutSeconds: 45})
	sPipUp, _ := coord.UpdatePlayerState("pip-1", VideoStream{PlayerState: PlayerStatePlaying, Title: "PipT", Series: "PipS", Season: "S1", Episode: "E1", Duration: 1200.0, Position: 60.0, PositionUpdatedAt: 12345})
	sRewind, _ := coord.UpdatePlayerState("pip-1", VideoStream{Position: 0, PositionUpdatedAt: 98765})
	if sPipRetrig.Pip.Position != 50.0 || sPipUp.Pip.Title != "PipT" || sPipUp.Pip.Position != 60.0 || sRewind.Pip.Position != 0 || sRewind.Pip.PositionUpdatedAt != 98765 {
		t.Fatal("unexpected pip metadata")
	}
	preserveMetadata(&VideoStream{Title: "Keep"}, nil)
}

func TestCoordinator_ForwardAction(t *testing.T) {
	t.Parallel()
	var receivedAction ActionRequest
	var mu sync.Mutex

	sidecar := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path == "/fail" {
			http.Error(w, "server error", http.StatusInternalServerError)
			return
		}
		_ = json.NewDecoder(r.Body).Decode(&receivedAction)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer sidecar.Close()

	coord := NewCoordinator(nil, WithHTTPClient(sidecar.Client()))
	defer coord.Close()

	if err := coord.ForwardAction(context.Background(), "cast", ActionPlay, nil); !errors.Is(err, ErrStreamNotFound) {
		t.Fatalf("expected ErrStreamNotFound, got %v", err)
	}
	_, _ = coord.Trigger(VideoStream{ID: "non-ctrl", StreamURL: "http://v", Priority: PriorityPersistent})
	if err := coord.ForwardAction(context.Background(), "non-ctrl", ActionPlay, nil); !errors.Is(err, ErrNotControllable) {
		t.Fatalf("expected ErrNotControllable, got %v", err)
	}
	_, _ = coord.Trigger(VideoStream{ID: "no-url", StreamURL: "http://v", Priority: PriorityPersistent, Controllable: true})
	if err := coord.ForwardAction(context.Background(), "no-url", ActionPlay, nil); !errors.Is(err, ErrNotControllable) {
		t.Fatalf("expected ErrNotControllable for empty control_url, got %v", err)
	}

	_, _ = coord.Trigger(VideoStream{ID: "cast", StreamURL: "http://v", Priority: PriorityPersistent, Controllable: true, ControlURL: sidecar.URL + "/action"})
	_, _ = coord.Trigger(VideoStream{ID: "pip-ctrl", StreamURL: "http://v", Priority: PriorityTemporary, Controllable: true, ControlURL: sidecar.URL + "/pip-action"})

	for _, act := range []string{ActionTogglePlayback, ActionRewind, ActionFastForward, ActionBack, ActionHome, ActionCaptions, ActionNext, ActionPrevious} {
		if err := coord.ForwardAction(context.Background(), "cast", act, nil); err != nil {
			t.Fatalf("action %s got err %v", act, err)
		}
	}

	for _, tc := range []struct {
		id, act string
		val     any
		wantErr error
	}{
		{"cast", ActionSeek, 128.5, nil},
		{"pip-ctrl", ActionPause, nil, nil},
		{"cast", "invalid_action", nil, ErrInvalidAction},
		{"", ActionPlay, nil, ErrInvalidStream},
	} {
		if err := coord.ForwardAction(context.Background(), tc.id, tc.act, tc.val); !errors.Is(err, tc.wantErr) {
			t.Fatalf("action %s got err %v, want %v", tc.act, err, tc.wantErr)
		} else if tc.wantErr == nil {
			mu.Lock()
			if receivedAction.ID != tc.id || receivedAction.Action != tc.act {
				t.Fatalf("received action mismatch: %+v", receivedAction)
			}
			if tc.act == ActionSeek && receivedAction.Value.(float64) != 128.5 {
				t.Fatalf("seek value mismatch: %v", receivedAction.Value)
			}
			mu.Unlock()
		}
	}

	_, _ = coord.Trigger(VideoStream{ID: "fail", StreamURL: "http://v", Priority: PriorityPersistent, Controllable: true, ControlURL: sidecar.URL + "/fail"})
	if err := coord.ForwardAction(context.Background(), "fail", ActionPlay, nil); !errors.Is(err, ErrControllerUnreachable) {
		t.Fatalf("expected ErrControllerUnreachable, got %v", err)
	}
}

func TestCoordinator_SchemaValidation(t *testing.T) {
	t.Parallel()
	if b, err := os.ReadFile(filepath.Join("..", "..", "api", "schemas", "video.state.json")); err == nil {
		var s map[string]any
		if err := json.Unmarshal(b, &s); err != nil {
			t.Fatalf("failed to parse schema: %v", err)
		}
	}
	st := VideoState{Mode: ModeVideo, Primary: &VideoStream{ID: "c", StreamURL: "u", Priority: PriorityPersistent}, Pip: &VideoStream{ID: "d", StreamURL: "u"}}
	b1, _ := json.Marshal(st)
	b2, _ := json.Marshal(VideoState{Mode: ModeWidgets})
	var m1, m2 map[string]any
	_ = json.Unmarshal(b1, &m1)
	_ = json.Unmarshal(b2, &m2)
	if m1["mode"] != "video" || m1["primary"] == nil || m1["pip"] == nil || m2["mode"] != "widgets" || m2["primary"] != nil || m2["pip"] != nil {
		t.Fatalf("schema mismatch: m1=%+v m2=%+v", m1, m2)
	}
}

func TestCoordinator_Concurrency(t *testing.T) {
	t.Parallel()
	coord := NewCoordinator(nil)
	defer coord.Close()

	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			if id%2 == 0 {
				_, _ = coord.Trigger(mkStream("cast", "http://stream", "", PriorityPersistent, 0))
			} else {
				_, _ = coord.Trigger(mkStream("doorbell", "http://stream", "", PriorityTemporary, 45))
			}
			_ = coord.GetState()
			_, _ = coord.UpdatePlayerState("cast", VideoStream{PlayerState: PlayerStatePlaying})
			if id%5 == 0 {
				_, _ = coord.Dismiss("doorbell")
			}
		}(i)
	}
	wg.Wait()
}
