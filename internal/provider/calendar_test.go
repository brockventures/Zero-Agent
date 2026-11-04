package provider_test

import (
	"context"
	"maps"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

func icsDoc(events ...string) string {
	return "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Mirrormere//Test//EN\n" + strings.Join(events, "\n") + "\nEND:VCALENDAR"
}

func fetchTestCalendar(t *testing.T, ics string, refTime time.Time, cfgOverrides map[string]any) (provider.CalendarSnapshot, error) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/calendar")
		_, _ = w.Write([]byte(ics))
	}))
	t.Cleanup(srv.Close)

	p := provider.NewCalendarProviderWithClient(srv.Client(), func() time.Time { return refTime })
	cfg := map[string]any{
		"calendars": []any{map[string]any{"name": "Cal", "url": srv.URL, "color": "#3b82f6"}},
	}
	maps.Copy(cfg, cfgOverrides)
	if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
		return provider.CalendarSnapshot{}, err
	}
	res, err := p.Fetch(context.Background())
	if err != nil {
		return provider.CalendarSnapshot{}, err
	}
	return res.(provider.CalendarSnapshot), nil
}

func TestCalendarProvider_Init_Validations(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name       string
		cfg        map[string]any
		opts       provider.InitOptions
		wantErrSub string
	}{
		{"missing calendars", map[string]any{}, provider.InitOptions{}, "requires at least one calendar"},
		{"empty calendars list", map[string]any{"calendars": []any{}}, provider.InitOptions{}, "requires at least one calendar"},
		{"calendar item not a map", map[string]any{"calendars": []any{"invalid"}}, provider.InitOptions{}, "not a map"},
		{"missing name", map[string]any{"calendars": []any{map[string]any{"url": "https://example.com/cal.ics"}}}, provider.InitOptions{}, "requires non-empty 'name'"},
		{"missing url and url_env", map[string]any{"calendars": []any{map[string]any{"name": "Cal1"}}}, provider.InitOptions{}, "requires either 'url' or 'url_env'"},
		{"both url and url_env provided", map[string]any{"calendars": []any{map[string]any{"name": "Cal1", "url": "https://x", "url_env": "E"}}}, provider.InitOptions{}, "cannot specify both 'url' and 'url_env'"},
		{"url_env unset in environment and secrets", map[string]any{"calendars": []any{map[string]any{"name": "Cal1", "url_env": "UNSET_123"}}}, provider.InitOptions{}, "environment variable \"UNSET_123\" is not set"},
		{"url_env resolved via secrets", map[string]any{"calendars": []any{map[string]any{"name": "Cal1", "url_env": "CAL_URL"}}}, provider.InitOptions{Secrets: map[string]string{"CAL_URL": "https://x"}}, ""},
		{"url_env resolved via structured secret path", map[string]any{"calendars": []any{map[string]any{"name": "Cal1", "url_env": "F"}}}, provider.InitOptions{Secrets: map[string]string{"calendars[0].url": "https://x"}}, ""},
		{"valid calendars with view", map[string]any{"view": "week", "calendars": []map[string]any{{"name": "Work", "url": "https://x", "color": "#10b981", "enabled": true}}}, provider.InitOptions{}, ""},
		{"valid calendars with strip_recurring and exclude_recurring", map[string]any{"view": "week", "strip_recurring": true, "calendars": []map[string]any{{"name": "W", "url": "https://x", "strip_recurring": false}, {"name": "P", "url": "https://y", "exclude_recurring": true}}}, provider.InitOptions{}, ""},
		{"valid calendars with exclude_recurring at root", map[string]any{"exclude_recurring": true, "calendars": []any{map[string]any{"name": "C", "url": "https://x"}}}, provider.InitOptions{}, ""},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			p := provider.NewCalendarProvider()
			err := p.Init(context.Background(), tc.cfg, tc.opts)
			if tc.wantErrSub != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
					t.Fatalf("expected error containing %q, got: %v", tc.wantErrSub, err)
				}
			} else if err != nil {
				t.Fatalf("unexpected init error: %v", err)
			}
		})
	}
}

func TestCalendarProvider_Fetch_ParsingAndRecurrence(t *testing.T) {
	t.Parallel()

	t.Run("single and all day", func(t *testing.T) {
		t.Parallel()
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:evt-timed\nDTSTART:20260925T140000Z\nDTEND:20260925T150000Z\nSUMMARY:Soccer Practice\nLOCATION:Field 2\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:evt-allday\nDTSTART;VALUE=DATE:20260926\nDTEND;VALUE=DATE:20260927\nSUMMARY:Trash & Recycling\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:evt-multiday\nDTSTART;VALUE=DATE:20260927\nDTEND;VALUE=DATE:20260930\nSUMMARY:Camping Trip\nLOCATION:Yosemite\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:evt-past\nDTSTART:20260501T100000Z\nDTEND:20260501T110000Z\nSUMMARY:Old Meeting\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:evt-future\nDTSTART:20270501T100000Z\nDTEND:20270501T110000Z\nSUMMARY:Far Future Meeting\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:evt-dur\nDTSTART:20260925T160000Z\nDURATION:PT1H30M\nSUMMARY:Duration Event\nEND:VEVENT",
		)
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		snap, err := fetchTestCalendar(t, ics, ref, nil)
		if err != nil || len(snap.Events) != 4 || snap.SyncStatus != "ok" {
			t.Fatalf("fetch failed: err=%v len=%d", err, len(snap.Events))
		}
		if snap.Events[0].Title != "Soccer Practice" || snap.Events[0].Location != "Field 2" || snap.Events[0].AllDay {
			t.Errorf("ev[0] mismatch: %+v", snap.Events[0])
		}
		if snap.Events[1].Title != "Duration Event" {
			t.Errorf("ev[1] mismatch: %+v", snap.Events[1])
		}
		if snap.Events[2].Start != "2026-09-26" || snap.Events[2].End != "2026-09-26" || !snap.Events[2].AllDay {
			t.Errorf("ev[2] mismatch: %+v", snap.Events[2])
		}
		if snap.Events[3].Start != "2026-09-27" || snap.Events[3].End != "2026-09-29" {
			t.Errorf("ev[3] mismatch: %+v", snap.Events[3])
		}
	})

	t.Run("recurrence exdates and overrides", func(t *testing.T) {
		t.Parallel()
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:daily\nDTSTART:20260921T090000Z\nDTEND:20260921T093000Z\nRRULE:FREQ=DAILY;COUNT=5\nEXDATE:20260923T090000Z\nSUMMARY:Team Standup\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:daily\nRECURRENCE-ID:20260924T090000Z\nDTSTART:20260924T100000Z\nDTEND:20260924T104500Z\nSUMMARY:Team Standup (Delayed)\nLOCATION:Zoom Room A\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:tz-daily\nDTSTART;TZID=America/Los_Angeles:20260921T090000\nDTEND;TZID=America/Los_Angeles:20260921T093000\nRRULE:FREQ=DAILY;COUNT=4\nEXDATE;TZID=America/Los_Angeles:20260922T090000,,20260923,20260923T090000\nSUMMARY:LA Sync\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:tz-daily\nRECURRENCE-ID;TZID=America/Los_Angeles:20260924T090000\nDTSTART;TZID=America/Los_Angeles:20260924T110000\nDTEND;TZID=America/Los_Angeles:20260924T113000\nSUMMARY:LA Sync (Moved)\nEND:VEVENT",
		)
		ref := time.Date(2026, 9, 20, 0, 0, 0, 0, time.UTC)
		snap, err := fetchTestCalendar(t, ics, ref, nil)
		if err != nil || len(snap.Events) != 6 {
			t.Fatalf("fetch failed: err=%v len=%d events=%+v", err, len(snap.Events), snap.Events)
		}
		for _, ev := range snap.Events {
			if strings.Contains(ev.Start, "2026-09-23") {
				t.Fatalf("found excluded exdate event: %+v", ev)
			}
			if ev.Title == "LA Sync" && strings.Contains(ev.Start, "2026-09-22") {
				t.Fatalf("found excluded tz exdate event: %+v", ev)
			}
			if strings.Contains(ev.Start, "2026-09-24") && ev.Title == "Team Standup" {
				t.Errorf("unoverridden UTC event remained: %+v", ev)
			}
			if strings.Contains(ev.Start, "2026-09-24") && ev.Title == "LA Sync" {
				t.Errorf("unoverridden TZID event remained: %+v", ev)
			}
		}
	})

	t.Run("timezones and durations", func(t *testing.T) {
		t.Parallel()
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:tz1\nDTSTART;TZID=America/New_York:20260925T090000\nDURATION:P1D\nSUMMARY:NY Full Day\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:tz2\nDTSTART:20260925T140000Z\nDURATION:PT45S\nSUMMARY:Short\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:inv\nDTSTART:INVALID_DATE\nSUMMARY:Inv\nEND:VEVENT",
			"BEGIN:VEVENT\nSUMMARY:No DTSTART\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:long-uid-exceeding-thirty-two-chars-with-special-chars-@!#$%\nDTSTART:20260925T140000Z\nSUMMARY:Same Time A\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:same-b\nDTSTART:20260925T140000Z\nSUMMARY:Same Time B\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:\nDTSTART:20260925T140000Z\nSUMMARY:Empty UID\nEND:VEVENT",
		)
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		snap, err := fetchTestCalendar(t, ics, ref, nil)
		if err != nil || len(snap.Events) < 3 {
			t.Fatalf("fetch failed: err=%v len=%d", err, len(snap.Events))
		}
	})

	t.Run("recurrence edge cases", func(t *testing.T) {
		t.Parallel()
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:bad-rrule\nDTSTART:20260925T100000Z\nRRULE:INVALID_RRULE\nSUMMARY:Bad\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:recur-dur\nDTSTART:20260925T110000Z\nDURATION:PT2H\nRRULE:FREQ=DAILY;COUNT=2\nSUMMARY:Recurring with Duration\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:recur-bad-start\nDTSTART:INVALID\nRRULE:FREQ=DAILY;COUNT=2\nSUMMARY:Bad Start\nEND:VEVENT",
		)
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		snap, err := fetchTestCalendar(t, ics, ref, nil)
		if err != nil || len(snap.Events) != 2 {
			t.Fatalf("expected 2 recurring occurrences, got %d (err: %v)", len(snap.Events), err)
		}
	})

	t.Run("high frequency recurrence ignored", func(t *testing.T) {
		t.Parallel()
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:sec\nDTSTART:20260925T100000Z\nRRULE:FREQ=SECONDLY;INTERVAL=1\nSUMMARY:Sec\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:min\nDTSTART:20260925T100000Z\nRRULE:FREQ=MINUTELY;INTERVAL=5\nSUMMARY:Min\nEND:VEVENT",
		)
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		snap, err := fetchTestCalendar(t, ics, ref, nil)
		if err != nil || len(snap.Events) != 0 {
			t.Fatalf("expected 0 events, got %d (err: %v)", len(snap.Events), err)
		}
	})

	t.Run("strip recurring", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 9, 20, 0, 0, 0, 0, time.UTC)
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:dentist\nDTSTART:20260921T140000Z\nDTEND:20260921T150000Z\nSUMMARY:Dentist\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:standup\nDTSTART:20260921T090000Z\nDTEND:20260921T093000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:Standup\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:standup\nRECURRENCE-ID:20260922T090000Z\nDTSTART:20260922T100000Z\nDTEND:20260922T103000Z\nSUMMARY:Standup (Rescheduled)\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:rdate\nDTSTART:20260921T110000Z\nDTEND:20260921T120000Z\nRDATE:20260923T110000Z\nSUMMARY:Ad-hoc\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:dinner\nDTSTART:20260922T180000Z\nDTEND:20260922T200000Z\nSUMMARY:Dinner\nEND:VEVENT",
		)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/calendar")
			_, _ = w.Write([]byte(ics))
		}))
		t.Cleanup(srv.Close)

		for _, tc := range []struct {
			name      string
			cfg       map[string]any
			wantCount int
		}{
			{"default keeps recurring", map[string]any{"calendars": []any{map[string]any{"name": "A", "url": srv.URL}}}, 6},
			{"root strip_recurring", map[string]any{"strip_recurring": true, "calendars": []any{map[string]any{"name": "A", "url": srv.URL}}}, 2},
			{"root exclude_recurring alias", map[string]any{"exclude_recurring": true, "calendars": []any{map[string]any{"name": "A", "url": srv.URL}}}, 2},
			{"per-source override", map[string]any{"strip_recurring": true, "calendars": []any{map[string]any{"name": "S", "url": srv.URL, "strip_recurring": true}, map[string]any{"name": "I", "url": srv.URL, "strip_recurring": false}}}, 8},
			{"per-source exclude_recurring alias", map[string]any{"strip_recurring": false, "calendars": []any{map[string]any{"name": "S", "url": srv.URL, "exclude_recurring": true}}}, 2},
		} {
			p := provider.NewCalendarProviderWithClient(srv.Client(), func() time.Time { return ref })
			if err := p.Init(context.Background(), tc.cfg, provider.InitOptions{}); err != nil {
				t.Fatalf("%s init failed: %v", tc.name, err)
			}
			res, err := p.Fetch(context.Background())
			if err != nil {
				t.Fatalf("%s fetch failed: %v", tc.name, err)
			}
			snap := res.(provider.CalendarSnapshot)
			if len(snap.Events) != tc.wantCount {
				t.Errorf("%s: got %d events, want %d", tc.name, len(snap.Events), tc.wantCount)
			}
		}
	})
}

func TestCalendarProvider_Fetch_MultiFeedAndErrors(t *testing.T) {
	t.Parallel()

	t.Run("uninitialized error", func(t *testing.T) {
		t.Parallel()
		p := provider.NewCalendarProvider()
		if _, err := p.Fetch(context.Background()); err == nil || !strings.Contains(err.Error(), "not initialized") {
			t.Fatalf("expected not initialized error, got: %v", err)
		}
	})

	t.Run("multi calendar merge and partial failure", func(t *testing.T) {
		t.Parallel()
		s1 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/calendar")
			_, _ = w.Write([]byte(icsDoc("BEGIN:VEVENT\nUID:w1\nDTSTART:20260925T170000Z\nDTEND:20260925T180000Z\nSUMMARY:Work Meeting\nEND:VEVENT")))
		}))
		defer s1.Close()
		s2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "internal server error", http.StatusInternalServerError)
		}))
		defer s2.Close()

		p := provider.NewCalendarProviderWithClient(s1.Client(), func() time.Time { return time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC) })
		cfg := map[string]any{
			"calendars": []any{
				map[string]any{"name": "Work", "url": s1.URL, "color": "#10b981", "enabled": true},
				map[string]any{"name": "Broken", "url": s2.URL, "color": "#ef4444", "enabled": true},
				map[string]any{"name": "Disabled", "url": "http://invalid.local/disabled.ics", "enabled": false},
			},
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
			t.Fatalf("init failed: %v", err)
		}
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("fetch failed: %v", err)
		}
		snap := res.(provider.CalendarSnapshot)
		if snap.SyncStatus != "partial" || len(snap.Events) != 1 || snap.Events[0].Title != "Work Meeting" {
			t.Fatalf("unexpected snapshot: %+v", snap)
		}
	})

	t.Run("all calendars fail", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
		}))
		defer srv.Close()
		p := provider.NewCalendarProviderWithClient(srv.Client(), nil)
		_ = p.Init(context.Background(), map[string]any{"calendars": []any{map[string]any{"name": "F", "url": srv.URL}}}, provider.InitOptions{})
		if _, err := p.Fetch(context.Background()); err == nil || !strings.Contains(err.Error(), "all calendar feeds failed") {
			t.Fatalf("expected all failed error, got: %v", err)
		}
	})

	t.Run("webcal and invalid ics", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte("NOT A VALID ICS"))
		}))
		defer srv.Close()
		p := provider.NewCalendarProviderWithClient(srv.Client(), nil)
		_ = p.Init(context.Background(), map[string]any{"calendars": []any{map[string]any{"name": "W", "url": strings.Replace(srv.URL, "http://", "webcal://", 1)}}}, provider.InitOptions{})
		if _, err := p.Fetch(context.Background()); err == nil {
			t.Fatal("expected error on malformed ics")
		}
	})

	t.Run("invalid request url", func(t *testing.T) {
		t.Parallel()
		p := provider.NewCalendarProviderWithClient(nil, nil)
		_ = p.Init(context.Background(), map[string]any{"calendars": []any{map[string]any{"name": "B", "url": "http:// invalid url with spaces"}}}, provider.InitOptions{})
		if _, err := p.Fetch(context.Background()); err == nil {
			t.Fatal("expected error on invalid url")
		}
	})

	t.Run("slowloris size limit", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			chunk := make([]byte, 1024*1024)
			for i := range chunk {
				chunk[i] = 'A'
			}
			_, _ = w.Write(chunk)
			_, _ = w.Write(chunk)
			_, _ = w.Write([]byte("OVERFLOW"))
		}))
		defer srv.Close()
		p := provider.NewCalendarProviderWithClient(srv.Client(), nil)
		_ = p.Init(context.Background(), map[string]any{"calendars": []any{map[string]any{"name": "H", "url": srv.URL}}}, provider.InitOptions{})
		if _, err := p.Fetch(context.Background()); err == nil || !strings.Contains(err.Error(), "exceeds size limit") {
			t.Fatalf("expected size limit error, got: %v", err)
		}
	})

	t.Run("registry and lifecycle", func(t *testing.T) {
		t.Parallel()
		reg := provider.NewRegistry()
		p, err := reg.Create("calendar-feed")
		if err != nil {
			t.Fatalf("failed create: %v", err)
		}
		if err := p.Subscribe(context.Background(), nil); err != nil {
			t.Errorf("subscribe err: %v", err)
		}
		if err := p.Shutdown(context.Background()); err != nil {
			t.Errorf("shutdown err: %v", err)
		}
	})
}

func TestCalendarProvider_Sorting(t *testing.T) {
	t.Parallel()

	laLoc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed load location: %v", err)
	}

	t.Run("SortCalendarEvents timezone and all day", func(t *testing.T) {
		t.Parallel()
		events := []provider.CalendarEvent{
			{ID: "e3", Title: "Weekend Bachelorette Party", Start: "2026-10-15", End: "2026-10-15", AllDay: true},
			{ID: "e2", Title: "Wednesday Game Night", Start: "2026-10-15T02:00:00Z", End: "2026-10-15T04:00:00Z"},
			{ID: "e1", Title: "Monday Evening Dinner", Start: "2026-10-13T02:00:00Z", End: "2026-10-13T03:00:00Z"},
			{ID: "e4", Title: "Brunch with Bridesmaids", Start: "2026-10-15T17:00:00Z", End: "2026-10-15T18:30:00Z"},
		}
		provider.SortCalendarEvents(events, laLoc)
		expected := []string{"Monday Evening Dinner", "Wednesday Game Night", "Weekend Bachelorette Party", "Brunch with Bridesmaids"}
		for i, exp := range expected {
			if events[i].Title != exp {
				t.Errorf("[%d] got %q, want %q", i, events[i].Title, exp)
			}
		}
	})

	t.Run("fetch timezone aware sorting", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 10, 10, 0, 0, 0, 0, time.UTC)
		ics := icsDoc(
			"BEGIN:VEVENT\nUID:e3\nDTSTART;VALUE=DATE:20261015\nDTEND;VALUE=DATE:20261016\nSUMMARY:Weekend Bachelorette Party\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:e2\nDTSTART:20261015T020000Z\nDTEND:20261015T040000Z\nSUMMARY:Wednesday Game Night\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:e1\nDTSTART:20261013T020000Z\nDTEND:20261013T030000Z\nSUMMARY:Monday Evening Dinner\nEND:VEVENT",
			"BEGIN:VEVENT\nUID:e4\nDTSTART:20261015T170000Z\nDTEND:20261015T183000Z\nSUMMARY:Brunch with Bridesmaids\nEND:VEVENT",
		)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/calendar")
			_, _ = w.Write([]byte(ics))
		}))
		defer srv.Close()

		p := provider.NewCalendarProviderWithClient(srv.Client(), func() time.Time { return ref })
		if err := p.Init(context.Background(), map[string]any{"calendars": []any{map[string]any{"name": "S", "url": srv.URL}}}, provider.InitOptions{Timezone: "America/Los_Angeles"}); err != nil {
			t.Fatalf("init failed: %v", err)
		}
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("fetch failed: %v", err)
		}
		snap := res.(provider.CalendarSnapshot)
		expected := []string{"Monday Evening Dinner", "Wednesday Game Night", "Weekend Bachelorette Party", "Brunch with Bridesmaids"}
		for i, exp := range expected {
			if snap.Events[i].Title != exp {
				t.Errorf("[%d] got %q, want %q", i, snap.Events[i].Title, exp)
			}
		}
	})
}
