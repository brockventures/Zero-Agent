package adapters_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/tasks/adapters"
)

func TestNewTodoistAdapter_Validation(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, token, url string
		wantErr          string
	}{
		{"empty_token", "", "https://example.com", "token is required"},
		{"invalid_base_url", "tok", "ftp://invalid", "must be http:// or https://"},
		{"valid_config", "tok", "https://api.todoist.com", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ad, err := adapters.NewTodoistAdapter(adapters.TodoistAdapterConfig{Token: tc.token, BaseURL: tc.url})
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil || ad.Name() != "todoist" {
				t.Fatalf("unexpected adapter creation result: %v", err)
			}
		})
	}
}

func TestTodoistAdapter_FetchList(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		cfg       adapters.TodoistAdapterConfig
		routes    map[string]mockRoute
		wantName  string
		wantItems int
		checkSec  string
		checkDue  bool
		wantErr   string
		wantErrIs error
	}{
		{
			name: "project_id_success", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":            {body: `{"id":"p1","name":"Work"}`},
				"/api/v1/sections?project_id=p1": {body: `[{"id":"s1","name":"Dev"}]`},
				"/api/v1/tasks?project_id=p1":    {body: `[{"id":"t1","content":"Code","section_id":"s1"}]`},
			}, wantName: "Work", wantItems: 1,
		},
		{
			name: "multi_page_cursor_pagination",
			cfg:  adapters.TodoistAdapterConfig{ProjectName: "Work"},
			routes: map[string]mockRoute{
				"/api/v1/projects":                               {body: `{"results":[{"id":"p0","name":"Other"}],"next_cursor":"c2"}`},
				"/api/v1/projects?cursor=c2":                     {body: `{"results":[{"id":"p1","name":"Work"}],"next_cursor":"c1"}`},
				"/api/v1/projects?cursor=c1":                     {body: `{"results":[],"next_cursor":"c2"}`},
				"/api/v1/sections?project_id=p1":                 {body: `{"results":[{"id":"s1","name":"Sec1","order":1}],"next_cursor":"c2"}`},
				"/api/v1/sections?project_id=p1&cursor=c2":       {body: `{"results":[{"id":"s2","name":"Sec2","order":2}],"next_cursor":"c1"}`},
				"/api/v1/sections?project_id=p1&cursor=c1":       {body: `{"results":[],"next_cursor":"c2"}`},
				"/api/v1/tasks?project_id=p1":                    {body: `{"results":[{"id":"t1","content":"Task 1","section_id":"s1"}],"next_cursor":"c2"}`},
				"/api/v1/tasks?project_id=p1&cursor=c2":          {body: `{"results":[{"id":"t2","content":"Task 2","section_id":"s2"}],"next_cursor":"c1"}`},
				"/api/v1/tasks?project_id=p1&cursor=c1":          {body: `{"results":[],"next_cursor":"c2"}`},
				"/api/v1/tasks/completed?project_id=p1&limit=30": {body: `{"items":[]}`},
			},
			wantName:  "Work",
			wantItems: 2,
			checkSec:  "Sec2",
		},
		{
			name: "project_name_match", cfg: adapters.TodoistAdapterConfig{ProjectName: "Shopping"},
			routes: map[string]mockRoute{
				"/api/v1/projects":               {body: `[{"id":"p2","name":"Shopping"}]`},
				"/api/v1/sections?project_id=p2": {body: `[]`},
				"/api/v1/tasks?project_id=p2":    {body: `[{"id":"t2","content":"Milk"}]`},
			}, wantName: "Shopping", wantItems: 1,
		},
		{
			name: "project_name_not_found", cfg: adapters.TodoistAdapterConfig{ProjectName: "Missing"},
			routes:    map[string]mockRoute{"/api/v1/projects": {body: `[{"id":"p1","name":"Other"}]`}},
			wantErrIs: adapters.ErrUpstreamNotFound,
		},
		{
			name: "inbox_fallback", cfg: adapters.TodoistAdapterConfig{},
			routes: map[string]mockRoute{
				"/api/v1/projects":                    {body: `[{"id":"p1","name":"Other"},{"id":"p-inbox","name":"Inbox","inbox_project":true}]`},
				"/api/v1/sections?project_id=p-inbox": {body: `[]`},
				"/api/v1/tasks?project_id=p-inbox":    {body: `[{"id":"t3","content":"Task"}]`},
			}, wantName: "Inbox", wantItems: 1,
		},
		{
			name: "non_inbox_first_project_fallback", cfg: adapters.TodoistAdapterConfig{},
			routes: map[string]mockRoute{
				"/api/v1/projects":                    {body: `[{"id":"p-first","name":"General"}]`},
				"/api/v1/sections?project_id=p-first": {body: `[]`},
				"/api/v1/tasks?project_id=p-first":    {body: `[{"id":"t4","content":"Task"}]`},
			}, wantName: "General", wantItems: 1,
		},
		{
			name: "empty_projects_fallback", cfg: adapters.TodoistAdapterConfig{},
			routes: map[string]mockRoute{
				"/api/v1/projects": {body: `[]`},
				"/api/v1/tasks":    {body: `[{"id":"t5","content":"Task"}]`},
			}, wantName: "Inbox", wantItems: 1,
		},
		{
			name: "list_name_override", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1", ListName: "Custom"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":            {body: `{"id":"p1","name":"Work"}`},
				"/api/v1/sections?project_id=p1": {body: `[]`},
				"/api/v1/tasks?project_id=p1":    {body: `[]`},
			}, wantName: "Custom", wantItems: 0,
		},
		{
			name: "hierarchy_and_sections", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":            {body: `{"id":"p1"}`},
				"/api/v1/sections?project_id=p1": {body: `[{"id":"s1","name":"Sprint"}]`},
				"/api/v1/tasks?project_id=p1":    {body: `[{"id":"t1","section_id":"s1","order":1},{"id":"t2","parent_id":"t1","child_order":1},{"id":"t3","parent_id":"missing"}]`},
			}, wantName: "p1", wantItems: 3, checkSec: "Sprint",
		},
		{
			name: "cycle_protection", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":            {body: `{"id":"p1"}`},
				"/api/v1/sections?project_id=p1": {body: `[]`},
				"/api/v1/tasks?project_id=p1":    {body: `[{"id":"t1","parent_id":"t2"},{"id":"t2","parent_id":"t1"}]`},
			}, wantName: "p1", wantItems: 2,
		},
		{
			name: "numeric_bool_null_ids", cfg: adapters.TodoistAdapterConfig{ProjectID: "123"},
			routes: map[string]mockRoute{
				"/api/v1/projects/123":            {body: `{"id":123,"name":"Numeric"}`},
				"/api/v1/sections?project_id=123": {body: `[{"id":true,"name":"Sec"}]`},
				"/api/v1/tasks?project_id=123":    {body: `[{"id":456,"content":"Num","parent_id":null,"section_id":true,"added_at":"invalid-date","order":2}]`},
			}, wantName: "Numeric", wantItems: 1,
		},
		{
			name: "due_dates_date_and_datetime", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":            {body: `{"id":"p1"}`},
				"/api/v1/sections?project_id=p1": {body: `[]`},
				"/api/v1/tasks?project_id=p1":    {body: `[{"id":"t1","due":{"date":"2026-10-01"}},{"id":"t2","due":{"datetime":"2026-10-02T15:04:05Z"}}]`},
			}, wantName: "p1", wantItems: 2, checkDue: true,
		},
		{
			name: "sync_completed_degradation", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":                            {body: `{"id":"p1"}`},
				"/api/v1/sections?project_id=p1":                 {body: `[]`},
				"/api/v1/tasks?project_id=p1":                    {body: `[{"id":"t1","content":"Active"}]`},
				"/api/v1/tasks/completed?project_id=p1&limit=30": {status: 500},
			}, wantName: "p1", wantItems: 1,
		},
		{
			name: "completed_tasks_success", cfg: adapters.TodoistAdapterConfig{ProjectID: "p1"},
			routes: map[string]mockRoute{
				"/api/v1/projects/p1":                            {body: `{"id":"p1"}`},
				"/api/v1/sections?project_id=p1":                 {body: `[{"id":"s1","name":"DoneSec"}]`},
				"/api/v1/tasks?project_id=p1":                    {body: `[{"id":"t1","content":"Active"}]`},
				"/api/v1/tasks/completed?project_id=p1&limit=30": {body: `{"items":[{"task_id":"c1","content":"Done","section_id":"other_sec","completed_date":"2026-09-27"},{"id":"c2","content":"NoTaskID"}]}`},
			}, wantName: "p1", wantItems: 3,
		},
		{
			name: "envelope_results_format", cfg: adapters.TodoistAdapterConfig{},
			routes: map[string]mockRoute{
				"/api/v1/projects":                  {body: `{"results":[{"id":"p-env","name":"Env"}]}`},
				"/api/v1/sections?project_id=p-env": {body: `{"results":[{"id":"s-env","name":"EnvSec"}]}`},
				"/api/v1/tasks?project_id=p-env":    {body: `{"results":[{"id":"t-env","content":"EnvTask"}]}`},
			}, wantName: "Env", wantItems: 1,
		},
		{"payload_too_large", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {body: strings.Repeat("x", adapters.MaxPayloadBytes+10)}}, "", 0, "", false, "", adapters.ErrPayloadTooLarge},
		{"error_401", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {status: 401}}, "", 0, "", false, "", adapters.ErrUnauthorized},
		{"error_403", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {status: 403}}, "", 0, "", false, "", adapters.ErrUnauthorized},
		{"error_404", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {status: 404}}, "", 0, "", false, "", adapters.ErrUpstreamNotFound},
		{"error_429", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {status: 429}}, "", 0, "", false, "status 429", nil},
		{"error_500", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {status: 500}}, "", 0, "", false, "status 500", nil},
		{"malformed_project_json", adapters.TodoistAdapterConfig{ProjectID: "p1"}, map[string]mockRoute{"/api/v1/projects/p1": {body: `{bad`}}, "", 0, "", false, "failed to parse todoist project", nil},
		{"malformed_projects_list_json", adapters.TodoistAdapterConfig{}, map[string]mockRoute{"/api/v1/projects": {body: `{bad`}}, "", 0, "", false, "failed to parse todoist projects", nil},
		{"project_name_500", adapters.TodoistAdapterConfig{ProjectName: "P"}, map[string]mockRoute{"/api/v1/projects": {status: 500}}, "", 0, "", false, "status 500", nil},
		{"sections_500", adapters.TodoistAdapterConfig{ProjectID: "p1"}, map[string]mockRoute{"/api/v1/projects/p1": {body: `{"id":"p1"}`}, "/api/v1/sections?project_id=p1": {status: 500}}, "", 0, "", false, "failed to fetch todoist sections", nil},
		{"malformed_sections_json", adapters.TodoistAdapterConfig{ProjectID: "p1"}, map[string]mockRoute{"/api/v1/projects/p1": {body: `{"id":"p1"}`}, "/api/v1/sections?project_id=p1": {body: `{bad`}}, "", 0, "", false, "failed to parse todoist sections", nil},
		{"tasks_500", adapters.TodoistAdapterConfig{ProjectID: "p1"}, map[string]mockRoute{"/api/v1/projects/p1": {body: `{"id":"p1"}`}, "/api/v1/sections?project_id=p1": {body: `[]`}, "/api/v1/tasks?project_id=p1": {status: 500}}, "", 0, "", false, "failed to fetch todoist tasks", nil},
		{"malformed_tasks_json", adapters.TodoistAdapterConfig{ProjectID: "p1"}, map[string]mockRoute{"/api/v1/projects/p1": {body: `{"id":"p1"}`}, "/api/v1/sections?project_id=p1": {body: `[]`}, "/api/v1/tasks?project_id=p1": {body: `{bad`}}, "", 0, "", false, "failed to parse todoist tasks", nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srv := newRouteServer(t, tc.routes, nil)
			cfg := tc.cfg
			cfg.Token, cfg.BaseURL, cfg.Client = "tok", srv.URL, srv.Client()
			ad, err := adapters.NewTodoistAdapter(cfg)
			if err != nil {
				t.Fatalf("failed to create adapter: %v", err)
			}
			list, items, err := ad.FetchList(context.Background())
			switch {
			case tc.wantErrIs != nil && !errors.Is(err, tc.wantErrIs):
				t.Fatalf("expected error %v, got %v", tc.wantErrIs, err)
			case tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)):
				t.Fatalf("expected error containing %q, got %v", tc.wantErr, err)
			case tc.wantErr == "" && tc.wantErrIs == nil:
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if tc.wantName != "" && list.Name != tc.wantName {
					t.Errorf("list name = %q, want %q", list.Name, tc.wantName)
				}
				if len(items) != tc.wantItems {
					t.Errorf("items count = %d, want %d", len(items), tc.wantItems)
				}
				if tc.checkSec != "" && (len(items) < 2 || items[1].Section != tc.checkSec) {
					t.Errorf("expected items[1].Section == %q, got %+v", tc.checkSec, items)
				}
				if tc.checkDue && (len(items) < 2 || items[0].DueDate == nil || items[1].DueDate == nil) {
					t.Errorf("expected due dates parsed, got %+v", items)
				}
			}
		})
	}
}
