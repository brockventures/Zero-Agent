package provider_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/provider"
)

func wCfg(lat, lon any, units, name, locName string) map[string]any {
	m := map[string]any{}
	if lat != nil {
		m["latitude"] = lat
	}
	if lon != nil {
		m["longitude"] = lon
	}
	if units != "" {
		m["units"] = units
	}
	if name != "" {
		m["name"] = name
	}
	if locName != "" {
		m["location_name"] = locName
	}
	return m
}

func sampleOpenMeteoJSON() string {
	return `{"latitude":37.8,"longitude":-122.27,"timezone":"America/Los_Angeles","current":{"time":"2026-09-26T12:00","temperature_2m":68.4,"relative_humidity_2m":58,"apparent_temperature":67.8,"precipitation":0.0,"weather_code":1,"wind_speed_10m":7.2},"hourly":{"time":["2026-09-26T12:00","2026-09-26T13:00","2026-09-26T14:00"],"temperature_2m":[68.4,70.1,71.5],"precipitation_probability":[0,5,10],"weather_code":[1,2,2]},"daily":{"time":["2026-09-26","2026-09-27"],"weather_code":[1,2],"temperature_2m_max":[72.5,74.0],"temperature_2m_min":[55.0,56.5],"precipitation_probability_max":[10,20]}}`
}

const sampleNightJSON = `{"latitude":37.8,"longitude":-122.2,"current":{"time":"2026-10-12T20:00","temperature_2m":62.5,"weather_code":0,"is_day":0},"hourly":{"time":["2026-10-12T20:00","2026-10-12T21:00","2026-10-12T22:00","2026-10-13T08:00"],"temperature_2m":[62.5,60.0,58.0,65.0],"precipitation_probability":[0,0,0,10],"weather_code":[0,1,2,0],"is_day":[0,0]},"daily":{"time":["2026-10-12"],"weather_code":[0],"temperature_2m_max":[70.0],"temperature_2m_min":[55.0],"precipitation_probability_max":[0]}}`

func makeHourlyDailyJSON(numHours, numDays int, curTime string) []byte {
	hours, temps, probs, codes := make([]string, numHours), make([]float64, numHours), make([]int, numHours), make([]int, numHours)
	for i := range numHours {
		hours[i], temps[i], probs[i], codes[i] = fmt.Sprintf("2026-09-26T%02d:00", i), 60.0+float64(i), i, 1
	}
	days, dMax, dMin, dCodes, dProbs := make([]string, numDays), make([]float64, numDays), make([]float64, numDays), make([]int, numDays), make([]int, numDays)
	for i := range numDays {
		days[i], dMax[i], dMin[i], dCodes[i], dProbs[i] = fmt.Sprintf("2026-09-%02d", i+1), 70.0+float64(i), 50.0+float64(i), 1, 10
	}
	if curTime == "" {
		curTime = "2026-09-26T12:00"
	}
	b, _ := json.Marshal(map[string]any{
		"current": map[string]any{"time": curTime, "temperature_2m": 70.0, "weather_code": 1, "wind_speed_10m": 5.0, "relative_humidity_2m": 50, "apparent_temperature": 69.0},
		"hourly":  map[string]any{"time": hours, "temperature_2m": temps, "precipitation_probability": probs, "weather_code": codes},
		"daily":   map[string]any{"time": days, "temperature_2m_max": dMax, "temperature_2m_min": dMin, "weather_code": dCodes, "precipitation_probability_max": dProbs},
	})
	return b
}

func makeMismatchedJSON() []byte {
	hours, temps, probs, codes := make([]string, 30), make([]float64, 28), make([]int, 27), make([]int, 26)
	for i := range 30 {
		hours[i] = fmt.Sprintf("2026-09-26T%02d:00", i)
		if i < 28 {
			temps[i] = 65.0 + float64(i)
		}
		if i < 27 {
			probs[i] = i
		}
		if i < 26 {
			codes[i] = 1
		}
	}
	days, dMax, dMin, dCodes, dProbs := make([]string, 10), make([]float64, 9), make([]float64, 8), make([]int, 8), make([]int, 8)
	for i := range 10 {
		days[i] = fmt.Sprintf("2026-09-%02d", i+1)
		if i < 9 {
			dMax[i] = 70.0 + float64(i)
		}
		if i < 8 {
			dMin[i], dCodes[i], dProbs[i] = 50.0+float64(i), 0, 5
		}
	}
	b, _ := json.Marshal(map[string]any{
		"current": map[string]any{"temperature_2m": 70.0, "weather_code": 0, "wind_speed_10m": 5.0, "relative_humidity_2m": 50, "apparent_temperature": 69.0},
		"hourly":  map[string]any{"time": hours, "temperature_2m": temps, "precipitation_probability": probs, "weather_code": codes},
		"daily":   map[string]any{"time": days, "temperature_2m_max": dMax, "temperature_2m_min": dMin, "weather_code": dCodes, "precipitation_probability_max": dProbs},
	})
	return b
}

func TestMapWMOCode(t *testing.T) {
	t.Parallel()
	cases := []struct {
		code, isDay int
		text, icon  string
	}{
		{0, 1, "Clear Sky", "weather-sunny"},
		{0, 0, "Clear", "weather-clear-night"},
		{1, 1, "Mainly Clear", "weather-partly-cloudy"},
		{1, 0, "Mainly Clear", "weather-night-partly-cloudy"},
		{2, 1, "Partly Cloudy", "weather-partly-cloudy"},
		{2, 0, "Partly Cloudy", "weather-night-partly-cloudy"},
		{3, 1, "Overcast", "weather-cloudy"},
		{3, 0, "Overcast", "weather-cloudy"},
		{45, 1, "Fog", "weather-fog"},
		{48, 1, "Fog", "weather-fog"},
		{51, 1, "Drizzle", "weather-rainy"},
		{53, 1, "Drizzle", "weather-rainy"},
		{55, 1, "Drizzle", "weather-rainy"},
		{56, 1, "Freezing Drizzle", "weather-snowy-rainy"},
		{57, 1, "Freezing Drizzle", "weather-snowy-rainy"},
		{61, 1, "Rain", "weather-rainy"},
		{61, 0, "Rain", "weather-rainy"},
		{63, 1, "Rain", "weather-rainy"},
		{65, 1, "Rain", "weather-rainy"},
		{66, 1, "Freezing Rain", "weather-snowy-rainy"},
		{67, 1, "Freezing Rain", "weather-snowy-rainy"},
		{71, 1, "Snow Fall", "weather-snowy"},
		{73, 1, "Snow Fall", "weather-snowy"},
		{75, 1, "Snow Fall", "weather-snowy"},
		{77, 1, "Snow Grains", "weather-snowy"},
		{80, 1, "Rain Showers", "weather-pouring"},
		{81, 1, "Rain Showers", "weather-pouring"},
		{82, 1, "Rain Showers", "weather-pouring"},
		{85, 1, "Snow Showers", "weather-snowy"},
		{86, 1, "Snow Showers", "weather-snowy"},
		{95, 1, "Thunderstorm", "weather-lightning"},
		{95, 0, "Thunderstorm", "weather-lightning"},
		{96, 1, "Thunderstorm with Hail", "weather-lightning-rainy"},
		{99, 1, "Thunderstorm with Hail", "weather-lightning-rainy"},
		{999, 1, "Unknown", "weather-cloudy"},
		{999, 0, "Unknown", "weather-cloudy"},
	}
	for _, tc := range cases {
		text, icon := provider.MapWMOCodeWithDay(tc.code, tc.isDay)
		if text != tc.text || icon != tc.icon {
			t.Errorf("MapWMOCodeWithDay(%d, %d) = (%q, %q), want (%q, %q)", tc.code, tc.isDay, text, icon, tc.text, tc.icon)
		}
		if tc.isDay == 1 {
			if defText, defIcon := provider.MapWMOCode(tc.code); defText != tc.text || defIcon != tc.icon {
				t.Errorf("MapWMOCode(%d) = (%q, %q), want (%q, %q)", tc.code, defText, defIcon, tc.text, tc.icon)
			}
		}
	}
}

func TestWeatherProvider_Init(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name        string
		cfg         map[string]any
		opts        provider.InitOptions
		expectError bool
		errorSubstr string
	}{
		{"valid_imperial", wCfg(37.8044, -122.2712, "imperial", "", ""), provider.InitOptions{Timezone: "America/Los_Angeles"}, false, ""},
		{"valid_metric_strings", wCfg("51.5074", "-0.1278", "metric", "", ""), provider.InitOptions{Timezone: "Europe/London"}, false, ""},
		{"valid_int_coords", wCfg(37, -122, "", "", ""), provider.InitOptions{}, false, ""},
		{"valid_float32_int64", wCfg(float32(37.5), int64(-122), "", "", ""), provider.InitOptions{}, false, ""},
		{"endpoint_override", wCfg(37.8044, -122.2712, "", "", ""), provider.InitOptions{Endpoint: "https://custom.open-meteo.internal"}, false, ""},
		{"location_name", wCfg(37.8, -122.2, "", "", "Ayda's School"), provider.InitOptions{}, false, ""},
		{"name", wCfg(37.8, -122.2, "", "Home", ""), provider.InitOptions{}, false, ""},
		{"missing_lat", wCfg(nil, -122.2712, "", "", ""), provider.InitOptions{}, true, "valid latitude and longitude"},
		{"missing_lon", wCfg(37.8044, nil, "", "", ""), provider.InitOptions{}, true, "valid latitude and longitude"},
		{"bad_coord_string", wCfg("not-a-number", -122.2712, "", "", ""), provider.InitOptions{}, true, "valid latitude and longitude"},
		{"bad_coord_type", wCfg([]string{"37.8"}, -122.2712, "", "", ""), provider.InitOptions{}, true, "valid latitude and longitude"},
		{"lat_high", wCfg(95.0, -122.2712, "", "", ""), provider.InitOptions{}, true, "latitude 95.0000 out of range"},
		{"lat_low", wCfg(-95.0, -122.2712, "", "", ""), provider.InitOptions{}, true, "latitude -95.0000 out of range"},
		{"lon_high", wCfg(37.0, 185.0, "", "", ""), provider.InitOptions{}, true, "longitude 185.0000 out of range"},
		{"lon_low", wCfg(37.0, -185.0, "", "", ""), provider.InitOptions{}, true, "longitude -185.0000 out of range"},
		{"invalid_units", wCfg(37.0, -122.0, "kelvin", "", ""), provider.InitOptions{}, true, "invalid units \"kelvin\""},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			p := provider.NewWeatherProvider()
			err := p.Init(context.Background(), tc.cfg, tc.opts)
			if tc.expectError {
				if err == nil || !strings.Contains(err.Error(), tc.errorSubstr) {
					t.Fatalf("want error containing %q, got %v", tc.errorSubstr, err)
				}
			} else if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
		})
	}
}

func TestWeatherProvider_Fetch(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		body       []byte
		cfg        map[string]any
		opts       provider.InitOptions
		wantTemp   float64
		wantUnitsT string
		wantUnitsW string
		wantIcon   string
		wantCond   string
		wantPrecip int
		wantHourly int
		wantDaily  int
		wantFirstH string
		wantLoc    string
		wantQuery  []string
		nightCheck bool
	}{
		{
			name: "imperial", body: []byte(sampleOpenMeteoJSON()), cfg: wCfg(37.8044, -122.2712, "imperial", "", ""), opts: provider.InitOptions{Timezone: "America/Los_Angeles"},
			wantTemp: 68.4, wantUnitsT: "°F", wantUnitsW: "mph", wantIcon: "weather-partly-cloudy", wantCond: "Mainly Clear", wantPrecip: 10, wantHourly: 3, wantDaily: 2,
			wantQuery: []string{"temperature_unit=fahrenheit", "wind_speed_unit=mph", "precipitation_unit=inch", "timezone=America%2FLos_Angeles"},
		},
		{
			name: "metric", body: []byte(sampleOpenMeteoJSON()), cfg: wCfg(51.5074, -0.1278, "metric", "", ""), opts: provider.InitOptions{},
			wantUnitsT: "°C", wantUnitsW: "km/h",
			wantQuery: []string{"temperature_unit=celsius", "wind_speed_unit=kmh", "precipitation_unit=mm", "timezone=UTC"},
		},
		{
			name: "nighttime", body: []byte(sampleNightJSON), cfg: wCfg(37.8044, -122.2712, "imperial", "", ""), opts: provider.InitOptions{Timezone: "America/Los_Angeles"},
			wantCond: "Clear", wantIcon: "weather-clear-night", wantHourly: 4, wantDaily: 1, nightCheck: true,
		},
		{
			name: "hourly_daily_limits", body: makeMismatchedJSON(), cfg: wCfg(37.0, -122.0, "", "", ""), opts: provider.InitOptions{},
			wantHourly: 24, wantDaily: 7,
		},
		{
			name: "current_hour_alignment", body: makeHourlyDailyJSON(48, 2, "2026-09-26T14:15"), cfg: wCfg(37.0, -122.0, "", "", ""), opts: provider.InitOptions{},
			wantHourly: 24, wantFirstH: "2026-09-26T14:00",
		},
		{
			name: "hour_alignment_tail", body: makeHourlyDailyJSON(48, 2, "2026-09-26T40:00"), cfg: wCfg(37.0, -122.0, "", "", ""), opts: provider.InitOptions{},
			wantHourly: 8,
		},
		{
			name: "hour_alignment_bad_time", body: makeHourlyDailyJSON(10, 2, "bad"), cfg: wCfg(37.0, -122.0, "", "", ""), opts: provider.InitOptions{},
			wantHourly: 10,
		},
		{
			name: "location_name", body: makeHourlyDailyJSON(1, 1, ""), cfg: wCfg(37.83, -122.28, "", "", "Ayda's School"), opts: provider.InitOptions{},
			wantLoc: "Ayda's School",
		},
		{
			name: "name", body: makeHourlyDailyJSON(1, 1, ""), cfg: wCfg(37.83, -122.28, "", "Home", ""), opts: provider.InitOptions{},
			wantLoc: "Home",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var reqQuery string
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				reqQuery = r.URL.RawQuery
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write(tc.body)
			}))
			defer ts.Close()

			p := provider.NewWeatherProviderWithClient(ts.Client(), ts.URL)
			if err := p.Init(context.Background(), tc.cfg, tc.opts); err != nil {
				t.Fatalf("Init failed: %v", err)
			}
			res, err := p.Fetch(context.Background())
			if err != nil {
				t.Fatalf("Fetch failed: %v", err)
			}
			snap := res.(provider.WeatherSnapshot)
			if tc.wantTemp != 0 && snap.Current.Temperature != tc.wantTemp {
				t.Errorf("temp = %v, want %v", snap.Current.Temperature, tc.wantTemp)
			}
			if tc.wantUnitsT != "" && snap.Current.Units.Temperature != tc.wantUnitsT {
				t.Errorf("units temp = %q, want %q", snap.Current.Units.Temperature, tc.wantUnitsT)
			}
			if tc.wantUnitsW != "" && snap.Current.Units.WindSpeed != tc.wantUnitsW {
				t.Errorf("units wind = %q, want %q", snap.Current.Units.WindSpeed, tc.wantUnitsW)
			}
			if tc.wantIcon != "" && snap.Current.Icon != tc.wantIcon {
				t.Errorf("icon = %q, want %q", snap.Current.Icon, tc.wantIcon)
			}
			if tc.wantCond != "" && snap.Current.ConditionText != tc.wantCond {
				t.Errorf("cond = %q, want %q", snap.Current.ConditionText, tc.wantCond)
			}
			if tc.wantPrecip != 0 && (snap.Current.PrecipProbMax != tc.wantPrecip || snap.TodayPrecipProbMax() != tc.wantPrecip) {
				t.Errorf("precip = %d, want %d", snap.Current.PrecipProbMax, tc.wantPrecip)
			}
			if tc.wantHourly != 0 && len(snap.Hourly) != tc.wantHourly {
				t.Errorf("hourly len = %d, want %d", len(snap.Hourly), tc.wantHourly)
			}
			if tc.wantDaily != 0 && len(snap.Daily) != tc.wantDaily {
				t.Errorf("daily len = %d, want %d", len(snap.Daily), tc.wantDaily)
			}
			if tc.wantFirstH != "" && snap.Hourly[0].Time != tc.wantFirstH {
				t.Errorf("first hourly = %q, want %q", snap.Hourly[0].Time, tc.wantFirstH)
			}
			if tc.wantLoc != "" && snap.Location != tc.wantLoc {
				t.Errorf("loc = %q, want %q", snap.Location, tc.wantLoc)
			}
			for _, q := range tc.wantQuery {
				if !strings.Contains(reqQuery, q) {
					t.Errorf("query %q missing %q", reqQuery, q)
				}
			}
			if tc.nightCheck {
				if snap.Hourly[0].Icon != "weather-clear-night" || snap.Hourly[1].Icon != "weather-night-partly-cloudy" || snap.Hourly[2].Icon != "weather-partly-cloudy" || snap.Hourly[3].Icon != "weather-sunny" || snap.Daily[0].Icon != "weather-sunny" {
					t.Errorf("night icon mismatch: %+v", snap.Hourly)
				}
			}
		})
	}
}

func TestWeatherProvider_FetchErrors(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		status     int
		body       string
		baseURL    string
		cancel     bool
		wantErrSub string
	}{
		{"http_500", 500, "internal error", "", false, "HTTP 500"},
		{"malformed_json", 200, "not valid json", "", false, "failed to decode Open-Meteo response"},
		{"slowloris_limit", 200, fmt.Sprintf(`{"current":{"temperature_2m":60},"pad":"%s"}`, strings.Repeat("A", 1024*1024*2)), "", false, "failed to decode Open-Meteo response"},
		{"invalid_base_url", 0, "", "http://invalid-host-%-escape", false, "invalid weather baseURL"},
		{"context_cancelled", 200, "", "", true, "context canceled"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			baseURL := tc.baseURL
			var client *http.Client
			if tc.status > 0 {
				ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if tc.cancel {
						<-r.Context().Done()
						return
					}
					w.WriteHeader(tc.status)
					_, _ = w.Write([]byte(tc.body))
				}))
				defer ts.Close()
				baseURL = ts.URL
				client = ts.Client()
			}
			p := provider.NewWeatherProviderWithClient(client, baseURL)
			_ = p.Init(context.Background(), wCfg(37.0, -122.0, "", "", ""), provider.InitOptions{})

			ctx := context.Background()
			if tc.cancel {
				cctx, cancel := context.WithCancel(ctx)
				cancel()
				ctx = cctx
			}
			if _, err := p.Fetch(ctx); err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
				t.Fatalf("expected error containing %q, got %v", tc.wantErrSub, err)
			}
		})
	}
}

func TestWeatherProvider_LifecycleAndSerialization(t *testing.T) {
	t.Parallel()

	p := provider.NewWeatherProvider()
	ch := make(chan provider.WidgetPayload, 1)
	if err := p.Subscribe(context.Background(), ch); err != nil {
		t.Fatalf("Subscribe failed: %v", err)
	}
	if err := p.Shutdown(context.Background()); err != nil {
		t.Fatalf("Shutdown failed: %v", err)
	}

	reg := provider.NewRegistry()
	if p1, err := reg.Create("weather-forecast"); err != nil || p1 == nil {
		t.Fatalf("create forecast failed: %v", err)
	}

	snap := provider.WeatherSnapshot{
		Location: "Home",
		Current:  provider.WeatherCurrent{Temperature: 72.4, ConditionText: "Clear Sky", Icon: "weather-sunny", Units: provider.WeatherUnits{Temperature: "°F", WindSpeed: "mph"}},
		Hourly:   []provider.WeatherHourly{{Time: "2026-09-26T12:00", Temp: 72.4, Icon: "weather-sunny"}},
		Daily:    []provider.WeatherDaily{{Date: "2026-09-26", TempMax: 75.0, TempMin: 55.0, ConditionText: "Clear Sky", Icon: "weather-sunny"}},
	}
	b, err := json.Marshal(snap)
	if err != nil {
		t.Fatalf("Marshal failed: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(b, &parsed); err != nil {
		t.Fatalf("Unmarshal failed: %v", err)
	}
	if parsed["location"] != "Home" || parsed["current"].(map[string]any)["temperature"] != 72.4 {
		t.Errorf("unexpected json: %+v", parsed)
	}
}
