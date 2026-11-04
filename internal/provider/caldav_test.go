package provider_test

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

func mkCalSource(name, url, user, pass, token string) provider.CalendarSource {
	return provider.CalendarSource{Name: name, Type: "caldav", URL: url, Username: user, Password: pass, Token: token, Enabled: true}
}

func msXML(href, status, innerProp string) string {
	return fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>%s</d:href><d:propstat><d:prop>%s</d:prop><d:status>%s</d:status></d:propstat></d:response></d:multistatus>`, href, innerProp, status)
}

func TestCalDAV_DiscoveryAndReport(t *testing.T) {
	t.Parallel()

	t.Run("direct collection", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		cal1 := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:e1\nDTSTART:20260925T140000Z\nDTEND:20260925T150000Z\nSUMMARY:CalDAV Timed Event\nLOCATION:Room A\nEND:VEVENT\nEND:VCALENDAR"
		cal2 := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:e2\nDTSTART:20260926T100000Z\nDTEND:20260926T110000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:CalDAV Recurring Standup\nEND:VEVENT\nEND:VCALENDAR"
		reportXML := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/cal/e1.ics</d:href><d:propstat><d:prop><c:calendar-data>%s</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response><d:response><d:href>/cal/e2.ics</d:href><d:propstat><d:prop><c:calendar-data>%s</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>`, cal1, cal2)

		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			if r.Method == "PROPFIND" {
				_, _ = w.Write([]byte(msXML("/cal/", "HTTP/1.1 200 OK", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Test Calendar</d:displayname>")))
				return
			}
			if r.Method == "REPORT" {
				u, p, ok := r.BasicAuth()
				body, _ := io.ReadAll(r.Body)
				if !ok || u != "user" || p != "pass" || r.Header.Get("Depth") != "1" || !strings.Contains(string(body), "calendar-query") || !strings.Contains(string(body), "20260924T120000Z") {
					t.Errorf("auth/query check failed: u=%s p=%s depth=%s", u, p, r.Header.Get("Depth"))
				}
				_, _ = w.Write([]byte(reportXML))
				return
			}
			w.WriteHeader(http.StatusMethodNotAllowed)
		}))
		defer srv.Close()

		client := provider.NewCalDAVClient(srv.Client(), nil)
		source := mkCalSource("Test Calendar", srv.URL+"/cal/", "user", "pass", "")
		events, err := client.FetchCalendarEvents(context.Background(), source, ref.AddDate(0, 0, -1), ref.AddDate(0, 0, 14))
		if err != nil || len(events) != 4 {
			t.Fatalf("fetch failed: err=%v len=%d", err, len(events))
		}
		if events[0].Title != "CalDAV Timed Event" || events[0].Location != "Room A" || events[1].Title != "CalDAV Recurring Standup" {
			t.Errorf("events mismatch: %+v", events)
		}
	})

	t.Run("bearer token auth", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		cal := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:b1\nDTSTART:20260925T160000Z\nDTEND:20260925T170000Z\nSUMMARY:Bearer Event\nEND:VEVENT\nEND:VCALENDAR"
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("Authorization") != "Bearer secret-tok-123" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			if r.Method == "PROPFIND" {
				_, _ = w.Write([]byte(msXML("/cal/", "HTTP/1.1 200 OK", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>")))
				return
			}
			if r.Method == "REPORT" {
				_, _ = w.Write([]byte(msXML("/cal/1.ics", "HTTP/1.1 200 OK", fmt.Sprintf("<c:calendar-data>%s</c:calendar-data>", cal))))
				return
			}
			w.WriteHeader(http.StatusMethodNotAllowed)
		}))
		defer srv.Close()

		client := provider.NewCalDAVClient(srv.Client(), nil)
		source := mkCalSource("Bearer Cal", srv.URL+"/cal/", "", "", "secret-tok-123")
		events, err := client.FetchCalendarEvents(context.Background(), source, ref.AddDate(0, 0, -1), ref.AddDate(0, 0, 14))
		if err != nil || len(events) != 1 || events[0].Title != "Bearer Event" {
			t.Fatalf("fetch failed: err=%v events=%+v", err, events)
		}
	})

	t.Run("propfind discovery flow", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		cal := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:disc\nDTSTART:20260925T180000Z\nDTEND:20260925T190000Z\nSUMMARY:Discovered Event\nEND:VEVENT\nEND:VCALENDAR"

		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			switch {
			case r.Method == "PROPFIND" && r.URL.Path == "/dav/":
				_, _ = w.Write([]byte(msXML("/dav/", "HTTP/1.1 200 OK", "<d:current-user-principal><d:href>/dav/principals/u/</d:href></d:current-user-principal>")))
			case r.Method == "PROPFIND" && r.URL.Path == "/dav/principals/u/":
				_, _ = w.Write([]byte(msXML("/dav/principals/u/", "HTTP/1.1 200 OK", "<c:calendar-home-set><d:href>/dav/calendars/u/</d:href></c:calendar-home-set>")))
			case r.Method == "PROPFIND" && r.URL.Path == "/dav/calendars/u/":
				_, _ = w.Write([]byte(`<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/dav/calendars/u/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response><d:response><d:href>/dav/calendars/u/family/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Family</d:displayname></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>`))
			case r.Method == "REPORT" && r.URL.Path == "/dav/calendars/u/family/":
				_, _ = w.Write([]byte(msXML("/dav/calendars/u/family/1.ics", "HTTP/1.1 200 OK", fmt.Sprintf("<c:calendar-data>%s</c:calendar-data>", cal))))
			default:
				w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer srv.Close()

		client := provider.NewCalDAVClient(srv.Client(), nil)
		source := mkCalSource("Family", srv.URL+"/dav/", "alex", "pass", "")
		events, err := client.FetchCalendarEvents(context.Background(), source, ref.AddDate(0, 0, -1), ref.AddDate(0, 0, 14))
		if err != nil || len(events) != 1 || events[0].Title != "Discovered Event" {
			t.Fatalf("discovery fetch failed: err=%v events=%+v", err, events)
		}
		cached, err := client.FetchCalendarEvents(context.Background(), source, ref.AddDate(0, 0, -1), ref.AddDate(0, 0, 14))
		if err != nil || len(cached) != 1 {
			t.Fatalf("cached fetch failed: err=%v len=%d", err, len(cached))
		}
	})

	t.Run("multi calendar with caldav", func(t *testing.T) {
		t.Parallel()
		ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		sICal := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/calendar")
			_, _ = w.Write([]byte("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:i1\nDTSTART:20260925T100000Z\nDTEND:20260925T110000Z\nSUMMARY:iCal Standard Feed Event\nEND:VEVENT\nEND:VCALENDAR"))
		}))
		defer sICal.Close()
		sCalDAV := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			if r.Method == "PROPFIND" {
				_, _ = w.Write([]byte(msXML("/cal/", "HTTP/1.1 200 OK", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>")))
				return
			}
			_, _ = w.Write([]byte(msXML("/cal/1.ics", "HTTP/1.1 200 OK", "<c:calendar-data>BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:c1\nDTSTART:20260925T140000Z\nDTEND:20260925T150000Z\nSUMMARY:CalDAV Synchronized Event\nEND:VEVENT\nEND:VCALENDAR</c:calendar-data>")))
		}))
		defer sCalDAV.Close()
		sFail := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
		}))
		defer sFail.Close()

		p := provider.NewCalendarProviderWithClient(sICal.Client(), func() time.Time { return ref })
		cfg := map[string]any{
			"calendars": []any{
				map[string]any{"name": "Work", "type": "ical", "url": sICal.URL, "color": "#10b981", "enabled": true},
				map[string]any{"name": "Personal", "type": "caldav", "url": sCalDAV.URL + "/cal/", "username": "u", "password": "p", "color": "#3b82f6", "enabled": true},
				map[string]any{"name": "Broken", "type": "caldav", "url": sFail.URL + "/fail/", "username": "bu", "password": "bp", "color": "#ef4444", "enabled": true},
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
		if snap.SyncStatus != "partial" || len(snap.Events) != 2 || snap.Events[0].Title != "iCal Standard Feed Event" || snap.Events[1].Title != "CalDAV Synchronized Event" {
			t.Fatalf("unexpected snapshot: %+v", snap)
		}
	})
}

func TestCalDAV_ErrorsAndEdgeCases(t *testing.T) {
	t.Parallel()

	t.Run("auth and status failures", func(t *testing.T) {
		t.Parallel()
		for _, tc := range []struct {
			name   string
			status int
			errSub string
		}{
			{"401", http.StatusUnauthorized, "caldav authentication failed (HTTP 401)"},
			{"403", http.StatusForbidden, "caldav authentication failed (HTTP 403)"},
			{"404", http.StatusNotFound, "caldav calendar collection not found (HTTP 404)"},
			{"500", http.StatusInternalServerError, "caldav report failed with HTTP 500"},
		} {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status) }))
			client := provider.NewCalDAVClient(srv.Client(), nil)
			_, err := client.FetchCalendarEvents(context.Background(), mkCalSource("F", srv.URL+"/cal/", "u", "p", ""), time.Now(), time.Now().Add(24*time.Hour))
			srv.Close()
			if err == nil || !strings.Contains(err.Error(), tc.errSub) {
				t.Errorf("%s: got %v, want %q", tc.name, err, tc.errSub)
			}
		}
	})

	t.Run("slowloris size limit", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			chunk := make([]byte, 1024*1024)
			for i := range chunk {
				chunk[i] = ' '
			}
			_, _ = w.Write(chunk)
			_, _ = w.Write(chunk)
			_, _ = w.Write([]byte("EXCESS"))
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		_, err := client.FetchCalendarEvents(context.Background(), mkCalSource("H", srv.URL+"/huge/", "", "", ""), time.Now(), time.Now().Add(24*time.Hour))
		if err == nil || !strings.Contains(err.Error(), "exceeds size limit") {
			t.Fatalf("expected size limit error, got: %v", err)
		}
	})

	t.Run("empty multi status", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			_, _ = w.Write([]byte(`<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"></d:multistatus>`))
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		events, err := client.FetchCalendarEvents(context.Background(), mkCalSource("E", srv.URL+"/empty/", "", "", ""), time.Now(), time.Now().Add(24*time.Hour))
		if err != nil || len(events) != 0 {
			t.Fatalf("expected 0 events, got %d (err: %v)", len(events), err)
		}
	})

	t.Run("fallback to raw ical", func(t *testing.T) {
		t.Parallel()
		rawICS := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:r1\nDTSTART:20260925T120000Z\nDTEND:20260925T130000Z\nSUMMARY:Raw Fallback\nEND:VEVENT\nEND:VCALENDAR"
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/calendar")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(rawICS))
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		now := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		events, err := client.FetchCalendarEvents(context.Background(), mkCalSource("R", srv.URL+"/raw/", "", "", ""), now.Add(-time.Hour), now.Add(24*time.Hour))
		if err != nil || len(events) != 1 || events[0].Title != "Raw Fallback" {
			t.Fatalf("expected 1 raw fallback event, got %+v (err: %v)", events, err)
		}
	})

	t.Run("skip empty and invalid ics", func(t *testing.T) {
		t.Parallel()
		validICS := "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:v1\nDTSTART:20260925T120000Z\nDTEND:20260925T130000Z\nSUMMARY:Valid\nEND:VEVENT\nEND:VCALENDAR"
		multi := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/1</d:href><d:propstat><d:prop><c:calendar-data></c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response><d:response><d:href>/2</d:href><d:propstat><d:prop><c:calendar-data>NOT_FOUND</c:calendar-data></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response><d:response><d:href>/3</d:href><d:propstat><d:prop><c:calendar-data>INVALID_GARBAGE</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response><d:response><d:href>/4</d:href><d:propstat><d:prop><c:calendar-data>%s</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>`, validICS)
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			_, _ = w.Write([]byte(multi))
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		now := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		events, err := client.FetchCalendarEvents(context.Background(), mkCalSource("M", srv.URL+"/m/", "", "", ""), now.Add(-time.Hour), now.Add(24*time.Hour))
		if err != nil || len(events) != 1 || events[0].Title != "Valid" {
			t.Fatalf("expected 1 valid event, got %+v (err: %v)", events, err)
		}
	})

	t.Run("first collection fallback when name unmatched", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			if r.Method == "PROPFIND" && r.URL.Path == "/home/" {
				_, _ = w.Write([]byte(msXML("/home/", "HTTP/1.1 200 OK", "<c:calendar-home-set><d:href>/home/c/</d:href></c:calendar-home-set>")))
				return
			}
			if r.Method == "PROPFIND" && r.URL.Path == "/home/c/" {
				_, _ = w.Write([]byte(msXML("/home/c/def/", "HTTP/1.1 200 OK", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Unmatched</d:displayname>")))
				return
			}
			if r.Method == "REPORT" {
				_, _ = w.Write([]byte(msXML("/home/c/def/1.ics", "HTTP/1.1 200 OK", "<c:calendar-data>BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:f1\nDTSTART:20260925T120000Z\nDTEND:20260925T130000Z\nSUMMARY:Fallback First\nEND:VEVENT\nEND:VCALENDAR</c:calendar-data>")))
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		now := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		events, err := client.FetchCalendarEvents(context.Background(), mkCalSource("Wanted Other", srv.URL+"/home/", "", "", ""), now.Add(-time.Hour), now.Add(24*time.Hour))
		if err != nil || len(events) != 1 || events[0].Title != "Fallback First" {
			t.Fatalf("expected 1 event, got %+v (err: %v)", events, err)
		}
	})

	t.Run("propfind failure falls back to configured url", func(t *testing.T) {
		t.Parallel()
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == "PROPFIND" {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			_, _ = w.Write([]byte(msXML("/cal/1.ics", "HTTP/1.1 200 OK", "<c:calendar-data>BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:d1\nDTSTART:20260925T120000Z\nDTEND:20260925T130000Z\nSUMMARY:Direct\nEND:VEVENT\nEND:VCALENDAR</c:calendar-data>")))
		}))
		defer srv.Close()
		client := provider.NewCalDAVClient(srv.Client(), nil)
		now := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
		events, err := client.FetchCalendarEvents(context.Background(), mkCalSource("Direct", srv.URL+"/cal/", "", "", ""), now.Add(-time.Hour), now.Add(24*time.Hour))
		if err != nil || len(events) != 1 || events[0].Title != "Direct" {
			t.Fatalf("expected 1 event, got %+v (err: %v)", events, err)
		}
	})

	t.Run("webcal scheme and network errors", func(t *testing.T) {
		t.Parallel()
		client := provider.NewCalDAVClient(&http.Client{Timeout: 50 * time.Millisecond}, nil)
		disc, err := client.DiscoverCalendarURL(context.Background(), mkCalSource("W", "webcal://127.0.0.1:54321/cal/", "", "", ""))
		if err != nil || !strings.HasPrefix(disc, "https://") {
			t.Fatalf("expected https prefix, got %q (err: %v)", disc, err)
		}
		now := time.Now()
		if _, err := client.FetchCalendarEvents(context.Background(), mkCalSource("Dead", "http://127.0.0.1:59999/cal/", "", "", ""), now, now.Add(time.Hour)); err == nil {
			t.Fatal("expected network error")
		}
		if _, err := client.FetchCalendarEvents(context.Background(), mkCalSource("Inv", "http:// invalid url with spaces", "", "", ""), now, now.Add(time.Hour)); err == nil {
			t.Fatal("expected invalid url error")
		}
		srv401 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
		}))
		defer srv401.Close()
		if _, err := client.FetchCalendarEvents(context.Background(), mkCalSource("A401", srv401.URL+"/dav/", "", "", ""), now, now.Add(time.Hour)); err == nil || !strings.Contains(err.Error(), "authentication failed") {
			t.Fatalf("expected auth failed in discovery, got: %v", err)
		}
	})
}

func TestCalendarProvider_Init_CalDAV_Validations(t *testing.T) {
	t.Parallel()

	mkCfg := func(name, typ, url, user, uEnv, pass, pEnv, tok, tEnv string) map[string]any {
		m := map[string]any{"name": name, "type": typ, "url": url}
		for _, kv := range [][2]string{{"username", user}, {"username_env", uEnv}, {"password", pass}, {"password_env", pEnv}, {"token", tok}, {"token_env", tEnv}} {
			if kv[1] != "" {
				m[kv[0]] = kv[1]
			}
		}
		return map[string]any{"calendars": []any{m}}
	}

	tests := []struct {
		name       string
		cfg        map[string]any
		opts       provider.InitOptions
		wantErrSub string
	}{
		{"invalid_calendar_type", mkCfg("Bad", "unsupported-protocol", "https://x", "", "", "", "", "", ""), provider.InitOptions{}, "invalid type"},
		{"username_and_username_env_both_provided", mkCfg("DU", "caldav", "https://x", "u", "U_ENV", "", "", "", ""), provider.InitOptions{}, "cannot specify both 'username' and 'username_env'"},
		{"password_and_password_env_both_provided", mkCfg("DP", "caldav", "https://x", "u", "", "p", "P_ENV", "", ""), provider.InitOptions{}, "cannot specify both 'password' and 'password_env'"},
		{"token_and_token_env_both_provided", mkCfg("DT", "caldav", "https://x", "", "", "", "", "t", "T_ENV"), provider.InitOptions{}, "cannot specify both 'token' and 'token_env'"},
		{"both_basic_auth_and_bearer_token_provided", mkCfg("Both", "caldav", "https://x", "u", "", "p", "", "t", ""), provider.InitOptions{}, "cannot specify both Basic auth"},
		{"username_without_password", mkCfg("UO", "caldav", "https://x", "u", "", "", "", "", ""), provider.InitOptions{}, "username provided without password"},
		{"password_without_username", mkCfg("PO", "caldav", "https://x", "", "", "p", "", "", ""), provider.InitOptions{}, "password provided without username"},
		{"username_env_unset_in_environment_and_secrets", mkCfg("UnsetUser", "caldav", "https://x", "", "UNSET_CALDAV_USER_VAR", "", "", "", ""), provider.InitOptions{}, "environment variable \"UNSET_CALDAV_USER_VAR\" is not set"},
		{"password_env_unset_in_environment_and_secrets", mkCfg("UnsetPass", "caldav", "https://x", "u", "", "", "UNSET_CALDAV_PASS_VAR", "", ""), provider.InitOptions{}, "environment variable \"UNSET_CALDAV_PASS_VAR\" is not set"},
		{"token_env_unset_in_environment_and_secrets", mkCfg("UnsetTok", "caldav", "https://x", "", "", "", "", "", "UNSET_CALDAV_TOKEN_VAR"), provider.InitOptions{}, "environment variable \"UNSET_CALDAV_TOKEN_VAR\" is not set"},
		{"credentials_resolved_via_structured_secret_paths", map[string]any{"calendars": []any{map[string]any{"name": "S", "type": "caldav", "url_env": "U", "username_env": "UN", "password_env": "PW"}}}, provider.InitOptions{Secrets: map[string]string{"calendars[0].url": "https://x", "calendars[0].username": "u", "calendars[0].password": "p"}}, ""},
		{"token_resolved_via_structured_secret_paths", map[string]any{"calendars": []any{map[string]any{"name": "S", "type": "caldav", "url": "https://x", "token_env": "T"}}}, provider.InitOptions{Secrets: map[string]string{"calendars[0].token": "tok"}}, ""},
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
