package adapters_test

import (
	"context"
	"errors"
	"maps"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/azylman/mirrormere/internal/tasks/adapters"
)

type mockRoute struct {
	status     int
	body       string
	nextStatus int
	nextBody   string
}

func newRouteServer(t *testing.T, routes map[string]mockRoute, authOut *string) *httptest.Server {
	t.Helper()
	var mu sync.Mutex
	m := make(map[string]mockRoute, len(routes))
	maps.Copy(m, routes)
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if authOut != nil {
			*authOut = r.Header.Get("Authorization")
		}
		route, ok := m[r.URL.RequestURI()]
		if !ok {
			route, ok = m[r.URL.Path]
		}
		if !ok {
			http.NotFound(w, r)
			return
		}
		status := route.status
		if status == 0 {
			status = http.StatusOK
		}
		body := route.body
		if route.nextStatus != 0 {
			route.status, route.body = route.nextStatus, route.nextBody
			route.nextStatus, route.nextBody = 0, ""
			m[r.URL.RequestURI()], m[r.URL.Path] = route, route
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(s.Close)
	return s
}

func TestNewGTasksAdapter_Validation(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		cfg     adapters.GTasksAdapterConfig
		wantErr string
	}{
		{"empty_tasklist_id", adapters.GTasksAdapterConfig{}, "tasklist_id is required"},
		{"invalid_base_url", adapters.GTasksAdapterConfig{TaskListID: "t1", BaseURL: "ftp://invalid"}, "must be http:// or https://"},
		{"invalid_token_url", adapters.GTasksAdapterConfig{TaskListID: "t1", TokenURL: "://bad-url"}, "must be http:// or https://"},
		{"valid_config", adapters.GTasksAdapterConfig{TaskListID: "t1", Token: "tok", ListName: "My List"}, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ad, err := adapters.NewGTasksAdapter(tc.cfg)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if ad.Name() != "gtasks" {
				t.Errorf("expected Name() to be 'gtasks', got %q", ad.Name())
			}
		})
	}
}

func TestGTasksAdapter_FetchList(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		routes    map[string]mockRoute
		closeSrv  bool
		wantName  string
		wantItems int
		checkSub  bool
		wantErr   string
		wantErrIs error
	}{
		{
			name: "standard_fetch_soft_deleted",
			routes: map[string]mockRoute{
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1","title":"Chores","updated":"2026-09-26T12:00:00Z"}`},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"t1","title":"Clean","status":"needsAction","due":"2026-09-28T00:00:00Z"},{"id":"t2","title":"Fold","status":"completed"},{"id":"t3","deleted":true}]}`},
			},
			wantName: "Chores", wantItems: 2,
		},
		{
			name: "pagination_and_cycle_guard",
			routes: map[string]mockRoute{
				"/tasks/v1/users/@me/lists/l1": {status: 503},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"t1","title":"P1"}],"nextPageToken":"p2"}`},
				"/tasks/v1/lists/l1/tasks?maxResults=100&pageToken=p2&showCompleted=true&showHidden=true": {body: `{"items":[{"id":"t2","title":"P2"}],"nextPageToken":"p1"}`},
			},
			wantName: "l1", wantItems: 2,
		},
		{
			name: "hierarchical_subtask_ordering",
			routes: map[string]mockRoute{
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"t1","position":"01"},{"id":"t2","parent":"t1","position":"01"},{"id":"t3","position":"02"}]}`},
			},
			wantName: "l1", wantItems: 3, checkSub: true,
		},
		{
			name: "subtask_orphan_and_cycle_safety",
			routes: map[string]mockRoute{
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"orphan","parent":"missing","position":"01"},{"id":"c1","parent":"c2"},{"id":"c2","parent":"c1"}]}`},
			},
			wantName: "l1", wantItems: 3,
		},
		{
			name: "timestamps_and_default_title",
			routes: map[string]mockRoute{
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"t1","title":"Item","updated":"2026-09-26T10:00:00Z","due":"2026-09-28T00:00:00Z"}]}`},
			},
			wantName: "l1", wantItems: 1,
		},
		{"error_401", map[string]mockRoute{"/tasks/v1/users/@me/lists/l1": {status: 401}}, false, "", 0, false, "", adapters.ErrUnauthorized},
		{"error_404", map[string]mockRoute{"/tasks/v1/users/@me/lists/l1": {status: 404}}, false, "", 0, false, "", adapters.ErrUpstreamNotFound},
		{"error_500", map[string]mockRoute{"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`}, "/tasks/v1/lists/l1/tasks": {status: 500}}, false, "", 0, false, "status 500", nil},
		{"error_payload_too_large", map[string]mockRoute{"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`}, "/tasks/v1/lists/l1/tasks": {body: strings.Repeat("x", adapters.MaxPayloadBytes+10)}}, false, "", 0, false, "", adapters.ErrPayloadTooLarge},
		{"error_malformed_json", map[string]mockRoute{"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`}, "/tasks/v1/lists/l1/tasks": {body: `{bad`}}, false, "", 0, false, "failed to parse google tasks json response", nil},
		{"error_closed_server", nil, true, "", 0, false, "failed to fetch tasks from google tasks", nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srv := newRouteServer(t, tc.routes, nil)
			if tc.closeSrv {
				srv.Close()
			}
			ad, err := adapters.NewGTasksAdapter(adapters.GTasksAdapterConfig{
				TaskListID: "l1", Token: "static-tok", BaseURL: srv.URL, Client: srv.Client(),
			})
			if err != nil {
				t.Fatalf("failed to create adapter: %v", err)
			}
			list, items, err := ad.FetchList(context.Background())
			if tc.wantErrIs != nil {
				if !errors.Is(err, tc.wantErrIs) {
					t.Fatalf("expected error %v, got %v", tc.wantErrIs, err)
				}
				return
			}
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if list.Name != tc.wantName {
				t.Errorf("list name = %q, want %q", list.Name, tc.wantName)
			}
			if len(items) != tc.wantItems {
				t.Errorf("items count = %d, want %d", len(items), tc.wantItems)
			}
			if tc.checkSub && (len(items) < 2 || items[1].ID != "t2") {
				t.Errorf("expected items[1].ID == 't2', got %+v", items)
			}
		})
	}
}

func TestGTasksAdapter_OAuthRefresh(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		routes    map[string]mockRoute
		closeSrv  bool
		wantAuth  string
		wantErr   string
		doubleReq bool
	}{
		{
			name: "token_minting_and_caching",
			routes: map[string]mockRoute{
				"/token":                       {body: `{"access_token":"minted-tok"}`},
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`},
				"/tasks/v1/lists/l1/tasks":     {body: `{"items":[{"id":"t1"}]}`},
			},
			wantAuth:  "Bearer minted-tok",
			doubleReq: true,
		},
		{
			name: "401_refresh_retry_recovery",
			routes: map[string]mockRoute{
				"/token":                       {body: `{"access_token":"fresh-tok","expires_in":3600}`},
				"/tasks/v1/users/@me/lists/l1": {body: `{"id":"l1"}`},
				"/tasks/v1/lists/l1/tasks":     {status: 401, nextStatus: 200, nextBody: `{"items":[{"id":"t-retried"}]}`},
			},
			wantAuth: "Bearer fresh-tok",
		},
		{"closed_oauth_server", nil, true, "", "oauth token request failed", false},
		{"500_oauth_error", map[string]mockRoute{"/token": {status: 500}}, false, "", "oauth token refresh failed", false},
		{"invalid_oauth_json", map[string]mockRoute{"/token": {body: `{bad`}}, false, "", "failed to parse oauth token response", false},
		{"empty_access_token", map[string]mockRoute{"/token": {body: `{"access_token":""}`}}, false, "", "missing access_token", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var authHeader string
			srv := newRouteServer(t, tc.routes, &authHeader)
			if tc.closeSrv {
				srv.Close()
			}
			ad, err := adapters.NewGTasksAdapter(adapters.GTasksAdapterConfig{
				TaskListID: "l1", RefreshToken: "ref-tok", TokenURL: srv.URL + "/token",
				BaseURL: srv.URL, Client: srv.Client(),
			})
			if err != nil {
				t.Fatalf("failed to create adapter: %v", err)
			}
			ctx := context.Background()
			_, _, err = ad.FetchList(ctx)
			if tc.doubleReq && err == nil {
				_, _, err = ad.FetchList(ctx)
			}
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.wantAuth != "" && authHeader != tc.wantAuth {
				t.Errorf("auth header = %q, want %q", authHeader, tc.wantAuth)
			}
		})
	}
}

func TestGTasksAdapter_CompleteItem(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name        string
		itemID      string
		routes      map[string]mockRoute
		withRefresh bool
		closeSrv    bool
		wantErr     string
		wantErrIs   error
		wantCode    int
	}{
		{
			name:   "patch_200_ok",
			itemID: "task-1",
			routes: map[string]mockRoute{
				"/tasks/v1/lists/l1/tasks/task-1": {status: 200, body: `{"id":"task-1","status":"completed"}`},
			},
		},
		{
			name:        "401_oauth_refresh_retry",
			itemID:      "task-1",
			withRefresh: true,
			routes: map[string]mockRoute{
				"/token":                          {body: `{"access_token":"fresh-tok","expires_in":3600}`},
				"/tasks/v1/lists/l1/tasks/task-1": {status: 401, nextStatus: 200, nextBody: `{"id":"task-1","status":"completed"}`},
			},
		},
		{
			name:      "patch_404_not_found",
			itemID:    "ghost",
			routes:    map[string]mockRoute{"/tasks/v1/lists/l1/tasks/ghost": {status: 404}},
			wantErrIs: adapters.ErrUpstreamNotFound,
		},
		{
			name:     "patch_500_upstream_error",
			itemID:   "task-1",
			routes:   map[string]mockRoute{"/tasks/v1/lists/l1/tasks/task-1": {status: 500}},
			wantCode: 500,
		},
		{
			name:    "empty_item_id",
			itemID:  "",
			wantErr: "item ID is required",
		},
		{
			name:    "invalid_item_id_slash",
			itemID:  "task/1",
			wantErr: "invalid item ID",
		},
		{
			name:    "invalid_item_id_dotdot",
			itemID:  "../task-1",
			wantErr: "invalid item ID",
		},
		{
			name:        "token_fetch_error",
			itemID:      "task-1",
			withRefresh: true,
			routes:      map[string]mockRoute{"/token": {status: 500}},
			wantErr:     "failed to obtain gtasks access token",
		},
		{
			name:     "closed_server_network_error",
			itemID:   "task-1",
			closeSrv: true,
			wantErr:  "google tasks patch request failed",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srv := newRouteServer(t, tc.routes, nil)
			if tc.closeSrv {
				srv.Close()
			}
			cfg := adapters.GTasksAdapterConfig{
				TaskListID: "l1",
				Token:      "tok",
				BaseURL:    srv.URL,
				Client:     srv.Client(),
			}
			if tc.withRefresh {
				cfg.Token = ""
				cfg.RefreshToken = "ref-tok"
				cfg.TokenURL = srv.URL + "/token"
			}
			ad, err := adapters.NewGTasksAdapter(cfg)
			if err != nil {
				t.Fatalf("failed to create gtasks adapter: %v", err)
			}

			err = ad.CompleteItem(context.Background(), tc.itemID)
			switch {
			case tc.wantErrIs != nil && !errors.Is(err, tc.wantErrIs):
				t.Fatalf("expected error Is %v, got %v", tc.wantErrIs, err)
			case tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)):
				t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
			case tc.wantErr == "" && tc.wantErrIs == nil && tc.wantCode == 0 && err != nil:
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.wantCode != 0 {
				var httpErr adapters.UpstreamHTTPError
				if !errors.As(err, &httpErr) || httpErr.Code != tc.wantCode {
					t.Fatalf("expected UpstreamHTTPError with code %d, got %v", tc.wantCode, err)
				}
			}
		})
	}
}
