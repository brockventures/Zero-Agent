package events

import (
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
)

func TestInMemoryStateProvider(t *testing.T) {
	t.Parallel()
	provider := NewInMemoryStateProvider(nil)

	if provider.CurrentSnapshot() != nil || provider.CurrentStatus().ConfigStatus != config.ConfigStatusOK ||
		provider.GetVideoState().Mode != "widgets" || provider.GetAudioState().Volume != 75 || provider.GetAudioState().Muted ||
		provider.GetVoiceState().State != "idle" || provider.GetTheme() != "day" || provider.GetProvidersStatus() != nil ||
		provider.GetScreenRotateData() != nil || provider.BootID() == "" {
		t.Fatal("unexpected default values in provider")
	}
	if _, ok := provider.GetHeaderWeather(); ok {
		t.Fatal("expected ok=false for unset weather")
	}
	if _, _, _, ok := provider.GetWidgetState("w"); ok {
		t.Fatal("expected ok=false for unset widget state")
	}
	if _, ok := provider.GetWidgetTimestamp("w"); ok {
		t.Fatal("expected ok=false for unset widget timestamp")
	}

	snap := &config.Snapshot{}
	provider.SetSnapshot(snap)
	if provider.CurrentSnapshot() != snap {
		t.Fatal("SetSnapshot failed")
	}

	errStr := "err"
	provider.SetStatus(config.Status{ConfigStatus: config.ConfigStatusError, ConfigError: &errStr})
	if provider.CurrentStatus().ConfigStatus != config.ConfigStatusError {
		t.Fatal("SetStatus failed")
	}

	provider.SetWidgetState("w1", map[string]string{"k": "v"}, "healthy", "2026-09-25T10:00:00Z")
	d, s, ts, ok := provider.GetWidgetState("w1")
	if !ok || s != "healthy" || d == nil || ts != "2026-09-25T10:00:00Z" {
		t.Fatal("SetWidgetState failed")
	}
	if ts, ok := provider.GetWidgetTimestamp("w1"); !ok || ts != "2026-09-25T10:00:00Z" {
		t.Fatal("GetWidgetTimestamp failed")
	}

	provider.SetHeaderWeather(&HeaderWeather{Temperature: 72, Units: "F"})
	if w, ok := provider.GetHeaderWeather(); !ok || w.Temperature != 72 {
		t.Fatal("SetHeaderWeather failed")
	}

	provider.SetVideoState(&VideoStateData{Mode: "cast"})
	if provider.GetVideoState().Mode != "cast" {
		t.Fatal("SetVideoState failed")
	}
	provider.SetVideoState(nil)
	if provider.GetVideoState().Mode != "widgets" {
		t.Fatal("expected fallback video mode widgets")
	}

	provider.SetAudioState(&AudioStateData{Volume: 50, Muted: true})
	if provider.GetAudioState().Volume != 50 || !provider.GetAudioState().Muted {
		t.Fatal("SetAudioState failed")
	}
	provider.SetAudioState(nil)
	if provider.GetAudioState().Volume != 75 {
		t.Fatal("expected fallback audio volume 75")
	}

	txt := "hi"
	provider.SetVoiceState(&VoiceStateData{State: "speaking", Transcript: &txt})
	if provider.GetVoiceState().State != "speaking" || *provider.GetVoiceState().Transcript != "hi" {
		t.Fatal("SetVoiceState failed")
	}
	provider.SetVoiceState(nil)
	if provider.GetVoiceState().State != "idle" {
		t.Fatal("expected fallback voice state idle")
	}

	provider.SetTheme("night")
	if provider.GetTheme() != "night" {
		t.Fatal("SetTheme failed")
	}
	provider.SetTheme("")
	if provider.GetTheme() != "day" {
		t.Fatal("expected fallback theme day")
	}

	provider.SetProvidersStatus(map[string]string{"ha": "ok"})
	if ps := provider.GetProvidersStatus(); ps["ha"] != "ok" {
		t.Fatal("SetProvidersStatus failed")
	}

	provider.SetScreenRotateData(&ScreenRotateData{CurrentScreen: 2})
	if rd := provider.GetScreenRotateData(); rd == nil || rd.CurrentScreen != 2 {
		t.Fatal("SetScreenRotateData failed")
	}
}

func TestBuildHydrationBatch(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC)
	idGen := NewIDGenerator()

	if evts := appendHydrationEvent(nil, idGen, now, "bad", make(chan int)); len(evts) != 0 {
		t.Fatal("expected no events appended on marshal failure")
	}
	if sysData := buildSystemStatusData(nil, config.Status{}, "2026-09-25T00:00:00Z"); sysData.ConfigStatus != "ok" {
		t.Fatalf("expected fallback config_status 'ok', got %s", sysData.ConfigStatus)
	}

	makeProvider := func(withRotate bool) *InMemoryStateProvider {
		p := NewInMemoryStateProvider(nil)
		p.SetSnapshot(&config.Snapshot{
			Config: &config.Config{Timezone: "America/New_York", Display: config.DisplayConfig{Widgets: []config.WidgetConfig{{ID: "w1", Type: "clock"}}}},
			Layout: &layout.Layout{TotalScreens: 1, Screens: []layout.Screen{{Index: 0, Widgets: []layout.PlacedWidget{{WidgetID: "w1", Origin: [2]int{0, 0}, Dimensions: domain.NewDimension(3, 2)}}}}},
		})
		p.SetWidgetState("w1", map[string]any{"time": "20:00"}, "healthy", "2026-09-25T20:00:00Z")
		p.SetHeaderWeather(&HeaderWeather{Temperature: 68.5, Units: "F"})
		p.SetTheme("night")
		if withRotate {
			p.SetScreenRotateData(&ScreenRotateData{CurrentScreen: 1, TotalScreens: 3, IntervalSeconds: 45})
		}
		return p
	}

	tests := []struct {
		name      string
		provider  StateProvider
		wantLen   int
		wantTypes []string
		wantSub   string
	}{
		{
			name: "nil_provider", provider: nil, wantLen: 7,
			wantTypes: []string{EventScreenRotate, EventHeaderUpdate, EventVideoState, EventAudioState, EventVoiceState, EventSystemStatus, EventThemeUpdate},
			wantSub:   `"timezone":"UTC"`,
		},
		{
			name: "full_provider_ordering_and_payloads", provider: makeProvider(false), wantLen: 8,
			wantTypes: []string{EventScreenRotate, EventWidgetUpdate, EventHeaderUpdate, EventVideoState, EventAudioState, EventVoiceState, EventSystemStatus, EventThemeUpdate},
			wantSub:   `"timezone":"America/New_York"`,
		},
		{
			name: "with_active_screen_rotate", provider: makeProvider(true), wantLen: 8,
			wantTypes: []string{EventScreenRotate, EventWidgetUpdate, EventHeaderUpdate, EventVideoState, EventAudioState, EventVoiceState, EventSystemStatus, EventThemeUpdate},
			wantSub:   `"current_screen":1`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			batch := BuildHydrationBatch(tc.provider, idGen, now)
			if len(batch) != tc.wantLen {
				t.Fatalf("expected batch length %d, got %d", tc.wantLen, len(batch))
			}
			for i, wantType := range tc.wantTypes {
				if batch[i].Type != wantType {
					t.Errorf("event %d: expected type %s, got %s", i, wantType, batch[i].Type)
				}
			}
			if tc.wantSub != "" {
				var found bool
				for _, evt := range batch {
					if strings.Contains(string(evt.Data), tc.wantSub) {
						found = true
						break
					}
				}
				if !found {
					t.Fatalf("expected batch to contain %q", tc.wantSub)
				}
			}
		})
	}
}
