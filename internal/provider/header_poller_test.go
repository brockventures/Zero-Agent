package provider_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/provider"
)

type mockHeaderSink struct {
	mu      sync.Mutex
	weather *events.HeaderWeather
}

func (s *mockHeaderSink) SetHeaderWeather(w *events.HeaderWeather) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.weather = w
}

func (s *mockHeaderSink) GetWeather() *events.HeaderWeather {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.weather
}

func sampleHeaderOpenMeteoJSON() string {
	return `{"current":{"temperature_2m":72.4,"weather_code":1}}`
}

const sampleHeaderNightJSON = `{"current":{"temperature_2m":58.2,"weather_code":0,"is_day":0}}`

func mkHeaderCfg(lat, lon float64, units string, interval *int) *config.HeaderWeatherConfig {
	return &config.HeaderWeatherConfig{Latitude: lat, Longitude: lon, Units: units, RefreshIntervalSeconds: interval}
}

func mkHeaderCfgWithLoc(lat, lon float64, units string, interval *int, loc string) *config.HeaderWeatherConfig {
	return &config.HeaderWeatherConfig{Latitude: lat, Longitude: lon, Units: units, RefreshIntervalSeconds: interval, Location: loc}
}

func TestNewHeaderWeatherPoller_Defaults(t *testing.T) {
	t.Parallel()
	poller := provider.NewHeaderWeatherPoller(provider.HeaderWeatherPollerConfig{})
	if poller == nil {
		t.Fatal("expected non-nil poller")
	}
	poller.Stop()
}

func TestHeaderWeatherPoller_FetchOnce(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		status     int
		body       string
		baseURL    string
		weatherCfg *config.HeaderWeatherConfig
		tz         string
		wantTemp   float64
		wantUnits  string
		wantCode   int
		wantIcon   string
		wantCond   string
		wantLoc    string
		wantQuery  []string
		wantErrSub string
		checkSink  bool
	}{
		{"imperial", 200, sampleHeaderOpenMeteoJSON(), "", mkHeaderCfg(37.8044, -122.2712, "imperial", new(900)), "America/Los_Angeles", 72.4, "F", 1, "weather-partly-cloudy", "", "", []string{"temperature_unit=fahrenheit", "latitude=37.8044"}, "", true},
		{"custom_location", 200, sampleHeaderOpenMeteoJSON(), "", mkHeaderCfgWithLoc(37.8044, -122.2712, "imperial", new(900), "San Francisco"), "America/Los_Angeles", 72.4, "F", 1, "weather-partly-cloudy", "", "San Francisco", []string{"temperature_unit=fahrenheit"}, "", false},
		{"metric", 200, sampleHeaderOpenMeteoJSON(), "", mkHeaderCfg(51.5074, -0.1278, "metric", new(900)), "Europe/London", 0, "C", 0, "", "", "", []string{"temperature_unit=celsius"}, "", false},
		{"nighttime", 200, sampleHeaderNightJSON, "", mkHeaderCfg(37.8044, -122.2712, "imperial", nil), "America/Los_Angeles", 0, "", 0, "weather-clear-night", "Clear", "", nil, "", false},
		{"empty_tz_zero_interval", 200, sampleHeaderOpenMeteoJSON(), "", mkHeaderCfg(37.8, -122.2, "", new(0)), "", 0, "", 0, "", "", "", nil, "", false},
		{"nil_config", 0, "", "", nil, "", 0, "", 0, "", "", "", nil, "", false},
		{"http_500", 500, "upstream failure", "", mkHeaderCfg(37.8, -122.2, "", nil), "UTC", 0, "", 0, "", "", "", nil, "HTTP 500", false},
		{"malformed_json", 200, "not valid json", "", mkHeaderCfg(37.8, -122.2, "", nil), "UTC", 0, "", 0, "", "", "", nil, "failed to decode header Open-Meteo response", false},
		{"invalid_base_url", 0, "", "http://invalid-host-%-escape", mkHeaderCfg(37.8, -122.2, "", nil), "UTC", 0, "", 0, "", "", "", nil, "invalid header weather baseURL", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var reqMu sync.Mutex
			var reqQuery string
			baseURL, client := tc.baseURL, (*http.Client)(nil)
			if tc.status > 0 {
				ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					reqMu.Lock()
					reqQuery = r.URL.RawQuery
					reqMu.Unlock()
					w.Header().Set("Content-Type", "application/json")
					w.WriteHeader(tc.status)
					_, _ = w.Write([]byte(tc.body))
				}))
				defer ts.Close()
				baseURL, client = ts.URL, ts.Client()
			}
			sink, broadcaster := &mockHeaderSink{}, &mockBroadcaster{}
			poller := provider.NewHeaderWeatherPoller(provider.HeaderWeatherPollerConfig{
				Client: client, BaseURL: baseURL, Broadcaster: broadcaster, StateSink: sink,
			})
			if tc.weatherCfg != nil {
				poller.Start(context.Background(), tc.weatherCfg, tc.tz)
				defer poller.Stop()
			}
			hw, err := poller.FetchOnce(context.Background())
			if tc.wantErrSub != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
					t.Fatalf("want error containing %q, got %v", tc.wantErrSub, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.weatherCfg == nil {
				if hw != nil {
					t.Fatalf("expected nil for nil cfg, got %+v", hw)
				}
				return
			}
			if tc.wantTemp != 0 && hw.Temperature != tc.wantTemp {
				t.Errorf("temp = %v, want %v", hw.Temperature, tc.wantTemp)
			}
			if tc.wantUnits != "" && hw.Units != tc.wantUnits {
				t.Errorf("units = %q, want %q", hw.Units, tc.wantUnits)
			}
			if tc.wantCode != 0 && hw.WeatherCode != tc.wantCode {
				t.Errorf("code = %v, want %v", hw.WeatherCode, tc.wantCode)
			}
			if tc.wantIcon != "" && hw.Icon != tc.wantIcon {
				t.Errorf("icon = %q, want %q", hw.Icon, tc.wantIcon)
			}
			if tc.wantCond != "" && hw.Condition != tc.wantCond {
				t.Errorf("cond = %q, want %q", hw.Condition, tc.wantCond)
			}
			if hw.Location != tc.wantLoc {
				t.Errorf("location = %q, want %q", hw.Location, tc.wantLoc)
			}
			reqMu.Lock()
			qCaptured := reqQuery
			reqMu.Unlock()
			for _, q := range tc.wantQuery {
				if !strings.Contains(qCaptured, q) {
					t.Errorf("query %q missing %q", qCaptured, q)
				}
			}
			if tc.checkSink {
				if saved := sink.GetWeather(); saved == nil || saved.Temperature != tc.wantTemp {
					t.Errorf("sink mismatch: %+v", saved)
				}
				evts := broadcaster.getEvents()
				if len(evts) == 0 || evts[len(evts)-1].Type != events.EventHeaderUpdate {
					t.Fatalf("expected header.update event, got %+v", evts)
				}
				var data events.HeaderUpdateData
				if err := json.Unmarshal(evts[len(evts)-1].Data, &data); err != nil || data.Timezone != tc.tz || data.Weather == nil || data.Weather.Temperature != tc.wantTemp {
					t.Errorf("unexpected broadcast data: %+v", data)
				}
			}
		})
	}
}

func TestHeaderWeatherPoller_StartStopUpdate(t *testing.T) {
	t.Parallel()
	fetched := make(chan struct{}, 10)
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(sampleHeaderOpenMeteoJSON()))
		select {
		case fetched <- struct{}{}:
		default:
		}
	}))
	defer ts.Close()

	poller := provider.NewHeaderWeatherPoller(provider.HeaderWeatherPollerConfig{Client: ts.Client(), BaseURL: ts.URL})
	poller.Start(context.Background(), nil, "UTC")
	poller.Start(context.Background(), mkHeaderCfg(37.8, -122.2, "", new(1)), "America/Los_Angeles")
	select {
	case <-fetched:
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for initial fetch")
	}
	poller.UpdateConfig(context.Background(), mkHeaderCfg(51.5, -0.1, "", new(900)), "Europe/London")
	poller.Stop()
	poller.Stop()
}

func TestProviderCoordinator_HeaderWeatherPoller_Lifecycle(t *testing.T) {
	t.Parallel()
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(sampleHeaderOpenMeteoJSON()))
	}))
	defer ts.Close()

	sink, broadcaster := &mockHeaderSink{}, &mockBroadcaster{}
	snap := &config.Snapshot{Config: &config.Config{Timezone: "America/Los_Angeles", Display: config.DisplayConfig{
		Header: config.HeaderConfig{Enabled: new(true), Weather: mkHeaderCfg(37.8044, -122.2712, "imperial", new(900))},
	}}}
	coord := provider.NewCoordinator(provider.CoordinatorConfig{
		Broadcaster: broadcaster, HeaderWeatherSink: sink, HeaderPollerClient: ts.Client(), HeaderPollerBaseURL: ts.URL,
	}, snap)
	if _, ok := broadcaster.waitForPublish(2 * time.Second); !ok {
		t.Fatal("waitForPublish timed out")
	}
	if saved := sink.GetWeather(); saved == nil || saved.Temperature != 72.4 {
		t.Errorf("expected initial header weather in sink, got %+v", saved)
	}

	newSnap := &config.Snapshot{Config: &config.Config{Timezone: "Europe/London", Display: config.DisplayConfig{
		Header: config.HeaderConfig{Enabled: new(true), Weather: mkHeaderCfg(51.5074, -0.1278, "metric", new(600))},
	}}}
	if err := coord.UpdateConfig(newSnap); err != nil {
		t.Fatalf("UpdateConfig failed: %v", err)
	}
	noWeatherSnap := &config.Snapshot{Config: &config.Config{Timezone: "UTC", Display: config.DisplayConfig{Header: config.HeaderConfig{Enabled: new(true)}}}}
	if err := coord.UpdateConfig(noWeatherSnap); err != nil {
		t.Fatalf("UpdateConfig removing weather failed: %v", err)
	}
	if err := coord.Shutdown(context.Background()); err != nil {
		t.Fatalf("Shutdown failed: %v", err)
	}
}
