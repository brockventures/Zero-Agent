package rotation

import (
	"encoding/json"
	"errors"
	"runtime"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/layout"
)

type mockBroadcaster struct {
	mu     sync.Mutex
	events []string
	datas  [][]byte
}

func (m *mockBroadcaster) Publish(eventType string, data []byte) *events.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.events, m.datas = append(m.events, eventType), append(m.datas, data)
	return &events.Event{Type: eventType, Data: data}
}

func makeTestSnapshot(totalScreens, intervalSec, pauseDurationSec int) *config.Snapshot {
	screens := make([]layout.Screen, totalScreens)
	for i := range totalScreens {
		screens[i] = layout.Screen{Index: i, Widgets: []layout.PlacedWidget{{WidgetID: "widget-screen", Dimensions: domain.NewDimension(3, 2)}}}
	}
	return &config.Snapshot{
		Config: &config.Config{Display: config.DisplayConfig{Rotation: config.RotationConfig{IntervalSeconds: &intervalSec, PauseDurationSeconds: &pauseDurationSec}}},
		Layout: &layout.Layout{TotalScreens: totalScreens, Screens: screens},
	}
}

func TestCoordinator_LifecycleAndUpdateConfig(t *testing.T) {
	t.Parallel()
	mb := &mockBroadcaster{}
	snap := makeTestSnapshot(3, 30, 120)
	coord := NewCoordinator(Config{Broadcaster: mb}, snap)
	defer coord.Stop()

	if data := coord.ScreenRotateData(); coord.CurrentScreen() != 0 || coord.paused || data.CurrentScreen != 0 || data.TotalScreens != 3 || data.IntervalSeconds != 30 || len(data.Widgets) != 1 {
		t.Fatalf("unexpected initial state or screen rotate data")
	}

	_, _ = coord.SelectScreen(2)
	d2 := coord.UpdateConfig(makeTestSnapshot(2, 15, 60))
	if coord.CurrentScreen() != 0 || d2.TotalScreens != 2 || d2.IntervalSeconds != 15 {
		t.Fatalf("expected clamped screen 0 and 2 screens, got cur=%d total=%d", coord.CurrentScreen(), d2.TotalScreens)
	}
	if dNil := coord.UpdateConfig(nil); dNil.TotalScreens != 0 {
		t.Fatalf("expected 0 total screens on nil update")
	}

	NewCoordinator(Config{Broadcaster: nil}, snap).publishRotateLocked(events.ScreenRotateData{})
	coord.Stop()
	for _, fn := range []func() error{
		func() error {
			_, err := coord.SelectScreen(0)
			return err
		},
		func() error {
			_, err := coord.AdvanceScreen("next")
			return err
		},
		func() error {
			_, err := coord.PauseRotation(10)
			return err
		},
		func() error {
			_, err := coord.ResumeRotation()
			return err
		},
	} {
		if err := fn(); err == nil {
			t.Fatal("expected error on stopped coordinator")
		}
	}
}

func TestCoordinator_Navigation(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		total      int
		selectIdx  int
		advanceDir string
		isSelect   bool
		wantErr    error
		wantScreen int
	}{
		{"nil_snap_select", 0, 0, "", true, ErrNoScreens, 0},
		{"nil_snap_advance", 0, 0, "next", false, ErrNoScreens, 0},
		{"select_negative", 3, -1, "", true, ErrInvalidScreenIndex, 0},
		{"select_out_of_range", 3, 3, "", true, ErrInvalidScreenIndex, 0},
		{"select_valid_1", 3, 1, "", true, nil, 1},
		{"select_valid_2", 3, 2, "", true, nil, 2},
		{"advance_default_next", 3, 0, "", false, nil, 1},
		{"advance_explicit_next", 3, 0, "next", false, nil, 1},
		{"advance_prev_wrap", 3, 0, "prev", false, nil, 2},
		{"advance_invalid_dir", 3, 0, "up", false, ErrInvalidDirection, 0},
		{"single_screen_noop", 1, 0, "next", false, nil, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var snap *config.Snapshot
			if tc.total > 0 {
				snap = makeTestSnapshot(tc.total, 30, 120)
			}
			coord := NewCoordinator(Config{Broadcaster: &mockBroadcaster{}}, snap)
			defer coord.Stop()
			action := coord.AdvanceScreen
			if tc.isSelect {
				action = func(string) (events.ScreenRotateData, error) { return coord.SelectScreen(tc.selectIdx) }
			}
			data, err := action(tc.advanceDir)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("got error %v, want %v", err, tc.wantErr)
			}
			if tc.wantErr == nil && (data.CurrentScreen != tc.wantScreen || coord.CurrentScreen() != tc.wantScreen) {
				t.Fatalf("got screen %d, want %d", data.CurrentScreen, tc.wantScreen)
			}
		})
	}
}

func TestCoordinator_AutoRotateAndPauseTimers(t *testing.T) {
	t.Parallel()
	coord := NewCoordinator(Config{Broadcaster: &mockBroadcaster{}}, makeTestSnapshot(2, 1, 120))
	defer coord.Stop()

	if cur, err := coord.PauseRotation(-1); err != nil || cur != 0 || !coord.paused {
		t.Fatal("unexpected PauseRotation(-1)")
	}
	if cur, err := coord.PauseRotation(0); err != nil || cur != 0 || !coord.paused {
		t.Fatal("unexpected PauseRotation(0)")
	}
	if cur, err := coord.ResumeRotation(); err != nil || cur != 0 || coord.paused {
		t.Fatal("unexpected ResumeRotation")
	}
	if _, err := coord.ResumeRotation(); err != nil || coord.paused {
		t.Fatal("expected idempotent resume")
	}

	_, _ = coord.PauseRotation(time.Hour)
	coord.mu.Lock()
	pGen := coord.pauseGen
	coord.mu.Unlock()
	coord.onPauseTimeout(pGen)
	if coord.paused {
		t.Fatal("expected auto-resume to clear paused flag")
	}

	coord.onPauseTimeout(pGen - 1)
	coord.onPauseTimeout(pGen)
	coord.Stop()
	coord.onPauseTimeout(pGen)

	coord2 := NewCoordinator(Config{Broadcaster: &mockBroadcaster{}}, makeTestSnapshot(2, 1, 120))
	defer coord2.Stop()
	coord2.mu.Lock()
	rGen := coord2.timerGen
	coord2.mu.Unlock()
	coord2.onRotateTimeout(rGen)
	if coord2.CurrentScreen() != 1 {
		t.Fatalf("expected screen 1, got %d", coord2.CurrentScreen())
	}
	coord2.mu.Lock()
	tGen1 := coord2.timerGen
	coord2.mu.Unlock()
	coord2.onRotateTimeout(tGen1)
	if coord2.CurrentScreen() != 0 {
		t.Fatalf("expected screen 0, got %d", coord2.CurrentScreen())
	}

	coord2.onRotateTimeout(rGen)
	coord2.mu.Lock()
	coord2.paused = true
	coord2.mu.Unlock()
	coord2.mu.Lock()
	tGen2 := coord2.timerGen
	coord2.mu.Unlock()
	coord2.onRotateTimeout(tGen2)
	coord2.Stop()
	coord2.mu.Lock()
	tGen3 := coord2.timerGen
	coord2.mu.Unlock()
	coord2.onRotateTimeout(tGen3)
	coord2.mu.Lock()
	coord2.stopped, coord2.snapshot = false, nil
	tGen4 := coord2.timerGen
	coord2.mu.Unlock()
	coord2.onRotateTimeout(tGen4)

	NewCoordinator(Config{}, makeTestSnapshot(2, 0, 120)).Stop()
}

func TestCoordinator_Concurrency(t *testing.T) {
	t.Parallel()
	mb := &mockBroadcaster{}
	coord := NewCoordinator(Config{Broadcaster: mb}, makeTestSnapshot(4, 30, 120))
	defer coord.Stop()

	var wg sync.WaitGroup
	for i := range 20 {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			if id%2 == 0 {
				_, _ = coord.AdvanceScreen("next")
			} else {
				_, _ = coord.SelectScreen(id % 4)
				_, _ = coord.PauseRotation(10 * time.Millisecond)
				_, _ = coord.ResumeRotation()
			}
			runtime.Gosched()
		}(i)
	}
	wg.Wait()

	if coord.CurrentScreen() < 0 || coord.CurrentScreen() >= 4 {
		t.Fatalf("currentScreen out of bounds: %d", coord.CurrentScreen())
	}

	runWorker := func(fn func()) {
		wg.Go(func() {
			for range 50 {
				fn()
				runtime.Gosched()
			}
		})
	}
	runWorker(func() {
		coord.mu.Lock()
		gen := coord.timerGen
		coord.mu.Unlock()
		coord.onRotateTimeout(gen)
	})
	runWorker(func() { _, _ = coord.AdvanceScreen("next") })
	runWorker(func() { _, _ = coord.SelectScreen(1) })
	wg.Wait()

	mb.mu.Lock()
	defer mb.mu.Unlock()
	for i, v := range slices.Backward(mb.events) {
		if v == events.EventScreenRotate {
			var lastRotate events.ScreenRotateData
			if err := json.Unmarshal(mb.datas[i], &lastRotate); err == nil && lastRotate.CurrentScreen == coord.CurrentScreen() {
				return
			}
		}
	}
	t.Fatal("expected matching screen.rotate event")
}

func TestCoordinator_ScreenLayout(t *testing.T) {
	t.Parallel()
	coord := NewCoordinator(Config{Broadcaster: &mockBroadcaster{}}, makeTestSnapshot(3, 45, 120))
	if _, err := coord.SelectScreen(2); err != nil {
		t.Fatalf("SelectScreen: %v", err)
	}
	got := coord.ScreenLayout()
	if got.CurrentScreen != 2 || got.IntervalSeconds != 45 || len(got.Screens) != 3 {
		t.Fatalf("ScreenLayout = %+v, want current 2, interval 45, 3 screens", got)
	}
	if got.Screens[0].Widgets[0].WidgetID != "widget-screen" {
		t.Errorf("screen widgets not carried through: %+v", got.Screens[0])
	}

	empty := NewCoordinator(Config{}, nil).ScreenLayout()
	if empty.CurrentScreen != 0 || empty.IntervalSeconds != 30 || empty.Screens != nil {
		t.Errorf("nil snapshot ScreenLayout = %+v, want defaults", empty)
	}

	noConfig := NewCoordinator(Config{}, &config.Snapshot{Layout: &layout.Layout{TotalScreens: 1, Screens: []layout.Screen{{Index: 0}}}}).ScreenLayout()
	if noConfig.IntervalSeconds != 30 || len(noConfig.Screens) != 1 {
		t.Errorf("layout without config ScreenLayout = %+v", noConfig)
	}
}
