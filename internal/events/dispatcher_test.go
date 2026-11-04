package events

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
)

type mockCoordinator struct{ calledWith *config.Snapshot }

func (m *mockCoordinator) UpdateConfig(snap *config.Snapshot) ScreenRotateData {
	m.calledWith = snap
	return ScreenRotateData{TotalScreens: 2, IntervalSeconds: 30}
}

type mockProviderCoordinator struct {
	calledWith *config.Snapshot
	err        error
}

func (m *mockProviderCoordinator) UpdateConfig(snap *config.Snapshot) error {
	m.calledWith = snap
	return m.err
}

func TestHub_DispatchConfigReload(t *testing.T) {
	t.Parallel()
	snapWithLayout := &config.Snapshot{
		Config: &config.Config{Display: config.DisplayConfig{Widgets: []config.WidgetConfig{{ID: "w1", Type: "clock"}}}},
		Layout: &layout.Layout{TotalScreens: 1, Screens: []layout.Screen{{Index: 0, Widgets: []layout.PlacedWidget{{WidgetID: "w1", Origin: [2]int{0, 0}, Dimensions: domain.NewDimension(3, 2)}}}}},
	}

	tests := []struct {
		name       string
		snap       *config.Snapshot
		diff       *config.ConfigDiff
		coord      bool
		provCoord  bool
		provErr    bool
		wantEvents []string
		wantSub    string
	}{
		{name: "nil_snapshot", snap: nil},
		{
			name: "layout_and_domain_change", snap: snapWithLayout,
			diff:       &config.ConfigDiff{Modified: []config.InstanceDiff{{ID: "w1", DomainChange: true}, {ID: "w2", DomainChange: false}}},
			wantEvents: []string{EventScreenRotate, EventWidgetUpdate}, wantSub: `"widget_id":"w1"`,
		},
		{
			name: "timezone_change", snap: &config.Snapshot{Config: &config.Config{Timezone: "America/Chicago"}},
			diff: &config.ConfigDiff{TimezoneChanged: true}, wantEvents: []string{EventHeaderUpdate}, wantSub: "America/Chicago",
		},
		{
			name: "shell_changed", snap: &config.Snapshot{Config: &config.Config{Timezone: "UTC"}},
			diff: &config.ConfigDiff{ShellChanged: true}, wantEvents: []string{EventSystemReload}, wantSub: "shell_change",
		},
		{name: "with_rotation_coordinator", snap: &config.Snapshot{Config: &config.Config{}}, coord: true},
		{name: "with_provider_coordinator", snap: &config.Snapshot{Config: &config.Config{}}, provCoord: true},
		{name: "with_provider_coordinator_error", snap: &config.Snapshot{Config: &config.Config{}}, provCoord: true, provErr: true},
		{
			name:       "provider_coordinator_suppresses_duplicate_widget_update",
			snap:       snapWithLayout,
			diff:       &config.ConfigDiff{Modified: []config.InstanceDiff{{ID: "w1", DomainChange: true}}},
			provCoord:  true,
			wantEvents: []string{EventScreenRotate},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			hub := NewHub(HubConfig{}, NewInMemoryStateProvider(nil), nil)
			defer hub.Close()

			var mockRC *mockCoordinator
			if tc.coord {
				mockRC = &mockCoordinator{}
				hub.SetRotationCoordinator(mockRC)
			}
			var mockPC *mockProviderCoordinator
			if tc.provCoord {
				var err error
				if tc.provErr {
					err = errors.New("reconcile err")
				}
				mockPC = &mockProviderCoordinator{err: err}
				hub.SetProviderCoordinator(mockPC)
			}

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			ch, unsub := hub.Subscribe(ctx)
			defer unsub()

			if err := hub.DispatchConfigReload(tc.snap, tc.diff); err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.coord && mockRC.calledWith != tc.snap {
				t.Error("expected rotation coordinator called with snapshot")
			}
			if tc.provCoord && mockPC.calledWith != tc.snap {
				t.Error("expected provider coordinator called with snapshot")
			}

			for _, wantType := range tc.wantEvents {
				select {
				case evt := <-ch:
					if evt.Type != wantType || (tc.wantSub != "" && !strings.Contains(string(evt.Data), tc.wantSub)) {
						t.Fatalf("got %s %s, want %s %s", evt.Type, string(evt.Data), wantType, tc.wantSub)
					}
				case <-time.After(1 * time.Second):
					t.Fatalf("timeout waiting for %s", wantType)
				}
			}

			select {
			case evt := <-ch:
				t.Fatalf("unexpected extra event: %s %s", evt.Type, string(evt.Data))
			default:
			}
		})
	}
}

func TestHub_DispatchReloadAndStatus(t *testing.T) {
	t.Parallel()
	hubErr := NewHub(HubConfig{}, nil, nil)
	t.Cleanup(hubErr.Close)
	if err := hubErr.publishJSON("test", make(chan int)); err == nil {
		t.Fatal("expected publishJSON to fail on unmarshalable payload")
	}

	tests := []struct {
		name     string
		action   string
		wantType string
		wantSub  string
	}{
		{"style_reload", "style", EventStyleReload, "custom.css"},
		{"widget_reload", "widget", EventWidgetReload, "sensor-card"},
		{"system_reload", "system", EventSystemReload, "manual_reload"},
		{"system_status", "status", EventSystemStatus, "syntax error"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			provider := NewInMemoryStateProvider(nil)
			hub := NewHub(HubConfig{}, provider, nil)
			defer hub.Close()

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			ch, unsub := hub.Subscribe(ctx)
			defer unsub()

			var err error
			switch tc.action {
			case "style":
				err = hub.DispatchStyleReload(StyleReloadEvent{File: "custom.css", Timestamp: "2026-09-25T20:00:00Z"})
			case "widget":
				err = hub.DispatchWidgetReload(WidgetReloadEvent{Type: "sensor-card"})
			case "system":
				err = hub.DispatchSystemReload(SystemReloadEvent{Reason: "manual_reload", Timestamp: "2026-09-25T20:00:00Z"})
			case "status":
				errStr := "syntax error"
				provider.SetProvidersStatus(map[string]string{"pkg": "missing manifest"})
				err = hub.DispatchStatus(config.Status{ConfigStatus: config.ConfigStatusError, ConfigError: &errStr})
				if provider.CurrentStatus().ConfigStatus != config.ConfigStatusError {
					t.Error("expected provider status updated to error")
				}
			}
			if err != nil {
				t.Fatalf("dispatch %s failed: %v", tc.action, err)
			}
			select {
			case evt := <-ch:
				if evt.Type != tc.wantType || !strings.Contains(string(evt.Data), tc.wantSub) {
					t.Fatalf("got %s %s, want %s %s", evt.Type, string(evt.Data), tc.wantType, tc.wantSub)
				}
			case <-time.After(1 * time.Second):
				t.Fatalf("timeout waiting for %s", tc.wantType)
			}
		})
	}
}
