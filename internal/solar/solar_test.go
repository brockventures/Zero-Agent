package solar_test

import (
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/solar"
)

func TestCalculateSunriseSunset(t *testing.T) {
	t.Parallel()

	laLoc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed to load timezone: %v", err)
	}

	sydLoc, err := time.LoadLocation("Australia/Sydney")
	if err != nil {
		t.Fatalf("failed to load timezone: %v", err)
	}

	tests := []struct {
		name       string
		lat, lon   float64
		date       time.Time
		wantRiseH  int
		wantSetH   int
		wantErrSub string
	}{
		{
			name:      "Oakland_EarlyOctober",
			lat:       37.8044,
			lon:       -122.2712,
			date:      time.Date(2026, 10, 10, 12, 0, 0, 0, laLoc),
			wantRiseH: 7,
			wantSetH:  18,
		},
		{
			name:      "Sydney_October",
			lat:       -33.8688,
			lon:       151.2093,
			date:      time.Date(2026, 10, 10, 12, 0, 0, 0, sydLoc),
			wantRiseH: 6,
			wantSetH:  19,
		},
		{
			name:       "HighArctic_WinterPolarNight",
			lat:        85.0,
			lon:        0.0,
			date:       time.Date(2026, 12, 21, 12, 0, 0, 0, time.UTC),
			wantErrSub: "sun never rises",
		},
		{
			name:       "HighArctic_SummerPolarDay",
			lat:        85.0,
			lon:        0.0,
			date:       time.Date(2026, 6, 21, 12, 0, 0, 0, time.UTC),
			wantErrSub: "sun never sets",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			sunrise, sunset, err := solar.CalculateSunriseSunset(tc.lat, tc.lon, tc.date)
			if tc.wantErrSub != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
					t.Fatalf("expected error containing %q, got %v", tc.wantErrSub, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error calculating solar times: %v", err)
			}
			if sunrise.Hour() != tc.wantRiseH {
				t.Errorf("expected sunrise hour %d, got %s", tc.wantRiseH, sunrise.Format("15:04:05 MST"))
			}
			if sunset.Hour() != tc.wantSetH {
				t.Errorf("expected sunset hour %d, got %s", tc.wantSetH, sunset.Format("15:04:05 MST"))
			}
		})
	}
}

func TestIsNight(t *testing.T) {
	t.Parallel()

	loc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed to load timezone: %v", err)
	}

	lat := 37.8044
	lon := -122.2712

	tests := []struct {
		name      string
		lat, lon  float64
		date      time.Time
		wantNight bool
	}{
		{"Midday", lat, lon, time.Date(2026, 10, 10, 12, 0, 0, 0, loc), false},
		{"Midnight", lat, lon, time.Date(2026, 10, 10, 0, 0, 0, 0, loc), true},
		{"LateNight", lat, lon, time.Date(2026, 10, 10, 22, 0, 0, 0, loc), true},
		{"EarlyMorning", lat, lon, time.Date(2026, 10, 10, 5, 0, 0, 0, loc), true},
		{"AfterSunrise", lat, lon, time.Date(2026, 10, 10, 8, 0, 0, 0, loc), false},
		{"PolarNightFallback_Late", 85.0, 0.0, time.Date(2026, 12, 21, 22, 0, 0, 0, time.UTC), true},
		{"PolarNightFallback_Midday", 85.0, 0.0, time.Date(2026, 12, 21, 12, 0, 0, 0, time.UTC), false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := solar.IsNight(tc.lat, tc.lon, tc.date)
			if got != tc.wantNight {
				t.Fatalf("IsNight(%f, %f, %s) = %v, want %v", tc.lat, tc.lon, tc.date, got, tc.wantNight)
			}
		})
	}

	// Sunrise and Sunset exact boundaries
	midday := time.Date(2026, 10, 10, 12, 0, 0, 0, loc)
	sunrise, sunset, err := solar.CalculateSunriseSunset(lat, lon, midday)
	if err != nil {
		t.Fatalf("failed to calculate sunrise/sunset: %v", err)
	}

	boundTests := []struct {
		name      string
		t         time.Time
		wantNight bool
	}{
		{"BeforeSunrise", sunrise.Add(-1 * time.Minute), true},
		{"AfterSunrise", sunrise.Add(1 * time.Minute), false},
		{"BeforeSunset", sunset.Add(-1 * time.Minute), false},
		{"AfterSunset", sunset.Add(1 * time.Minute), true},
	}
	for _, tc := range boundTests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := solar.IsNight(lat, lon, tc.t); got != tc.wantNight {
				t.Errorf("IsNight(%s) = %v, want %v", tc.t.Format("15:04"), got, tc.wantNight)
			}
		})
	}
}
