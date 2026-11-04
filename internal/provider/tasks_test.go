package provider_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/provider"
	"github.com/azylman/mirrormere/internal/tasks"
	"github.com/azylman/mirrormere/internal/tasks/adapters"
)

type fakeAdapter struct {
	name     string
	list     *tasks.List
	items    []tasks.ListItem
	fetchErr error
}

func (f *fakeAdapter) Name() string { return f.name }
func (f *fakeAdapter) FetchList(_ context.Context) (*tasks.List, []tasks.ListItem, error) {
	return f.list, f.items, f.fetchErr
}

type mockCompleterAdapter struct {
	lastItem string
	calls    int
	err      error
}

func (m *mockCompleterAdapter) Name() string { return "mock-completer" }
func (m *mockCompleterAdapter) FetchList(_ context.Context) (*tasks.List, []tasks.ListItem, error) {
	return &tasks.List{ID: "test"}, nil, nil
}

func (m *mockCompleterAdapter) CompleteItem(_ context.Context, itemID string) error {
	m.calls++
	m.lastItem = itemID
	return m.err
}

func mkTaskList(id, name, source string) tasks.List {
	return tasks.List{ID: id, Name: name, Source: source}
}

func mkTaskItem(id, listID, title, section string, done bool) tasks.ListItem {
	return tasks.ListItem{ID: id, ListID: listID, Title: title, Section: section, Done: done}
}

func mkOpts(id string, sec map[string]string, tok string) provider.InitOptions {
	return provider.InitOptions{ID: id, Secrets: sec, Token: tok}
}

func mkCfg(src, subKey string, sub map[string]any) map[string]any {
	if subKey == "" {
		return map[string]any{"source": src}
	}
	return map[string]any{"source": src, subKey: sub}
}

func TestTasksProvider_Init(t *testing.T) {
	t.Parallel()
	reg := provider.NewRegistry()
	if p, err := reg.Create("tasks"); err != nil || p == nil {
		t.Fatal("registry tasks provider creation failed")
	}
	if provider.NewTasksProviderWithAdapter(nil) == nil {
		t.Fatal("expected non-nil provider from constructor")
	}

	tmpDir := t.TempDir()
	for f, c := range map[string]string{
		"creds.json":     `{"client_id":"cid","client_secret":"csec","refresh_token":"rt","token":"tok"}`,
		"installed.json": `{"installed":{"client_id":"iid","client_secret":"isec"}}`,
		"web.json":       `{"web":{"client_id":"wid","client_secret":"wsec"}}`,
		"bad.json":       "not-json",
	} {
		_ = os.WriteFile(filepath.Join(tmpDir, f), []byte(c), 0o600)
	}
	tests := []struct {
		name, wantErrSub string
		cfg              map[string]any
		opts             provider.InitOptions
	}{
		{"missing_source", "requires valid upstream source", map[string]any{}, mkOpts("w", nil, "")},
		{"local_source_rejected", "requires valid upstream source", map[string]any{"source": "local"}, mkOpts("w", nil, "")},
		{"http_source_rejected", "requires valid upstream source", map[string]any{"source": "http"}, mkOpts("w", nil, "")},
		{"gtasks_missing_block", "requires 'gtasks' config block", map[string]any{"source": "gtasks"}, mkOpts("w", nil, "")},
		{"gtasks_missing_tasklist", "requires 'gtasks.tasklist_id'", mkCfg("gtasks", "gtasks", map[string]any{}), mkOpts("w", nil, "")},
		{"gtasks_invalid_url", "failed to create gtasks adapter", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "base_url": "ftp://bad"}), mkOpts("w", nil, "")},
		{"gtasks_secrets_success", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "token_env": "GTOK"}), mkOpts("w", map[string]string{"gtasks.token": "sec"}, "")},
		{"gtasks_oauth_secrets", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "client_id": "cid", "client_secret": "csec", "refresh_token_env": "RTOK", "token_url": "https://x.com/tok"}), mkOpts("w", map[string]string{"RTOK": "rsec"}, "")},
		{"gtasks_creds_file", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "credentials_file": filepath.Join(tmpDir, "creds.json"), "token_url": "https://x.com/tok"}), mkOpts("w", nil, "")},
		{"gtasks_installed_file", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "client_secret_file": filepath.Join(tmpDir, "installed.json"), "refresh_token_env": "RTOK", "token_url": "https://x.com/tok"}), mkOpts("w", map[string]string{"RTOK": "rsec"}, "")},
		{"gtasks_web_file", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "client_secret_file_env": "FENV", "refresh_token": "rtok", "token_url": "https://x.com/tok"}), mkOpts("w", map[string]string{"FENV": filepath.Join(tmpDir, "web.json")}, "")},
		{"gtasks_bad_file_fallback", "", mkCfg("gtasks", "gtasks", map[string]any{"tasklist_id": "tl", "credentials_file": filepath.Join(tmpDir, "bad.json"), "token": "fallback-tok"}), mkOpts("w", nil, "")},
		{"todoist_missing_block", "requires 'todoist' config block", map[string]any{"source": "todoist"}, mkOpts("w", nil, "")},
		{"todoist_missing_token", "requires a valid token", mkCfg("todoist", "todoist", map[string]any{"project_id": "1"}), mkOpts("w", nil, "")},
		{"todoist_inline_token", "", mkCfg("todoist", "todoist", map[string]any{"token": "tok", "project_name": "Inbox"}), mkOpts("w", nil, "")},
		{"todoist_token_env_sec", "", mkCfg("todoist", "todoist", map[string]any{"token_env": "TTOK"}), mkOpts("w", map[string]string{"TTOK": "tok"}, "")},
		{"todoist_opts_token", "", mkCfg("todoist", "todoist", map[string]any{}), mkOpts("w", nil, "tok")},
		{"todoist_namespaced_secret", "", mkCfg("todoist", "todoist", map[string]any{}), mkOpts("w", map[string]string{"todoist.token": "tok"}, "")},
		{"todoist_invalid_url", "failed to create todoist adapter", mkCfg("todoist", "todoist", map[string]any{"token": "tok", "base_url": "ftp://bad"}), mkOpts("w", nil, "")},
		{"custom_float_show", "", map[string]any{"source": "todoist", "todoist": map[string]any{"token": "tok"}, "show_completed": float64(7), "list_name": "Custom"}, mkOpts("w", nil, "tok")},
		{"custom_str_show", "", map[string]any{"source": "todoist", "todoist": map[string]any{"token": "tok"}, "show_completed": "10"}, mkOpts("w", nil, "tok")},
		{"custom_neg_show", "", map[string]any{"source": "todoist", "todoist": map[string]any{"token": "tok"}, "show_completed": -2}, mkOpts("w", nil, "tok")},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			p := provider.NewTasksProvider()
			err := p.Init(context.Background(), tc.cfg, tc.opts)
			if tc.wantErrSub != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErrSub)) {
				t.Fatalf("expected error containing %q, got %v", tc.wantErrSub, err)
			}
			if tc.wantErrSub == "" && err != nil {
				t.Fatalf("unexpected Init error: %v", err)
			}
			_ = p.Subscribe(context.Background(), nil)
			_ = p.Shutdown(context.Background())
		})
	}
}

func TestTasksProvider_Init_Todoist_EnvFallback(t *testing.T) {
	for _, tc := range []struct{ name, key, val string }{
		{"api_token_env", "TODOIST_API_TOKEN", "api-token-from-env"},
		{"token_env", "TODOIST_TOKEN", "token-from-env"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TODOIST_API_TOKEN", "")
			t.Setenv("TODOIST_TOKEN", "")
			t.Setenv(tc.key, tc.val)
			p := provider.NewTasksProvider()
			err := p.Init(context.Background(), map[string]any{"source": "todoist", "list_name": "EnvList", "todoist": map[string]any{}}, provider.InitOptions{ID: tc.name})
			if err != nil {
				t.Fatalf("Init failed: %v", err)
			}
			_ = p.Shutdown(context.Background())
		})
	}
}

func TestTasksProvider_Fetch(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/projects/p-family":
			_ = json.NewEncoder(w).Encode(map[string]any{"id": "p-family", "name": "Family Checklist"})
		case "/api/v1/sections":
			_ = json.NewEncoder(w).Encode([]map[string]any{{"id": "sec-1", "name": "Chores"}})
		case "/api/v1/tasks":
			_ = json.NewEncoder(w).Encode([]map[string]any{{"id": "task-101", "section_id": "sec-1", "content": "Costumes"}})
		case "/api/v1/tasks/completed":
			_ = json.NewEncoder(w).Encode(map[string]any{"items": []any{}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)

	due := "2026-10-01"
	lCh := mkTaskList("daily-chores", "Daily Chores", "todoist")
	fakeWithDue := &fakeAdapter{name: "todoist", list: &lCh, items: []tasks.ListItem{{ID: "t1", ListID: "daily-chores", Title: "Water plants", DueDate: &due}}}
	tests := []struct {
		name, wantErrSub, wantListID string
		adapter                      adapters.Adapter
		cfg                          map[string]any
		opts                         provider.InitOptions
		skipInit                     bool
		wantItems                    int
	}{
		{"uninitialized", "not initialized", "", nil, nil, provider.InitOptions{}, true, 0},
		{"adapter_success", "", "daily-chores", fakeWithDue, map[string]any{"list_id": "daily-chores", "source": "todoist", "show_completed": 5}, mkOpts("widget-chores", nil, ""), false, 1},
		{"adapter_failure", "upstream timeout", "", &fakeAdapter{name: "todoist", fetchErr: errors.New("upstream timeout")}, map[string]any{"list_id": "daily-chores"}, mkOpts("widget-chores", nil, ""), false, 0},
		{"id_normalization", "", "canonical-chores", &fakeAdapter{name: "gtasks", list: &tasks.List{ID: "ext-123", Name: "Ext Title", Source: "gtasks"}, items: []tasks.ListItem{mkTaskItem("task-1", "ext-123", "Clean kitchen", "", false)}}, map[string]any{"list_id": "canonical-chores", "source": "gtasks"}, mkOpts("my-widget", nil, ""), false, 1},
		{"adapter_nil_list", "adapter returned nil list", "", &fakeAdapter{name: "todoist"}, map[string]any{"list_id": "nil-test", "source": "todoist"}, mkOpts("my-widget", nil, ""), false, 0},
		{"todoist_integration", "", "family-tasks", nil, map[string]any{"list_id": "family-tasks", "source": "todoist", "todoist": map[string]any{"project_id": "p-family", "token": "mock-token", "base_url": server.URL}}, mkOpts("family-tasks", nil, ""), false, 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ctx := context.Background()
			p := provider.NewTasksProviderWithAdapter(tc.adapter)
			if !tc.skipInit {
				if err := p.Init(ctx, tc.cfg, tc.opts); err != nil {
					t.Fatalf("Init failed: %v", err)
				}
				defer p.Shutdown(ctx)
			}
			res, err := p.Fetch(ctx)
			if tc.wantErrSub != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErrSub)) {
				t.Fatalf("expected error containing %q, got %v", tc.wantErrSub, err)
			}
			if tc.wantErrSub == "" && err != nil {
				t.Fatalf("unexpected Fetch error: %v", err)
			}
			if tc.wantErrSub != "" {
				return
			}
			snap, ok := res.(*tasks.TasksSnapshot)
			if !ok || snap == nil {
				t.Fatalf("expected *tasks.TasksSnapshot, got %T", res)
			}
			if tc.wantListID != "" && snap.List.ID != tc.wantListID {
				t.Errorf("expected List.ID %q, got %q", tc.wantListID, snap.List.ID)
			}
			for _, itm := range snap.Items {
				if tc.wantListID != "" && itm.ListID != tc.wantListID {
					t.Errorf("expected item ListID %q, got %q", tc.wantListID, itm.ListID)
				}
			}
			if tc.wantItems >= 0 && len(snap.Items) != tc.wantItems {
				t.Errorf("expected %d items, got %d", tc.wantItems, len(snap.Items))
			}
		})
	}
}

func TestTasksProvider_CompleteItem(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name                  string
		hasAdapter            bool
		nonCompleter          bool
		adapterErr, wantErrIs error
		itemID, wantErrSub    string
		wantAdapterCall       int
	}{
		{"nil_adapter", false, false, nil, nil, "i1", "not initialized", 0},
		{"non_completer_adapter", true, true, nil, tasks.ErrCompletionUnsupported, "item-1", "", 0},
		{"completer_success", true, false, nil, nil, "item-100", "", 1},
		{"upstream_not_found", true, false, adapters.ErrUpstreamNotFound, tasks.ErrItemNotFound, "item-ghost", "", 1},
		{"upstream_error", true, false, errors.New("upstream timeout"), nil, "item-fail", "upstream task completion failed", 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ctx := context.Background()
			var compAdapter *mockCompleterAdapter
			var adapter adapters.Adapter
			if tc.nonCompleter {
				adapter = &fakeAdapter{}
			} else if tc.hasAdapter {
				compAdapter = &mockCompleterAdapter{err: tc.adapterErr}
				adapter = compAdapter
			}

			var p *provider.TasksProvider
			if !tc.hasAdapter {
				p = provider.NewTasksProvider()
			} else {
				p = provider.NewTasksProviderWithAdapter(adapter)
				_ = p.Init(ctx, map[string]any{"list_id": "list-1"}, provider.InitOptions{ID: "w-tasks"})
			}

			err := p.CompleteItem(ctx, tc.itemID)
			switch {
			case tc.wantErrIs != nil && !errors.Is(err, tc.wantErrIs):
				t.Fatalf("expected error Is %v, got %v", tc.wantErrIs, err)
			case tc.wantErrSub != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErrSub)):
				t.Fatalf("expected error containing %q, got %v", tc.wantErrSub, err)
			case tc.wantErrIs == nil && tc.wantErrSub == "" && err != nil:
				t.Fatalf("unexpected complete error: %v", err)
			}
			if compAdapter != nil && compAdapter.calls != tc.wantAdapterCall {
				t.Errorf("expected %d adapter calls, got %d", tc.wantAdapterCall, compAdapter.calls)
			}
		})
	}

	pList := provider.NewTasksProviderWithAdapter(&fakeAdapter{name: "todoist"})
	_ = pList.Init(context.Background(), map[string]any{"list_id": "upstream-list"}, provider.InitOptions{ID: "w-tasks"})
	if pList.ListID() != "upstream-list" {
		t.Errorf("expected ListID 'upstream-list', got %q", pList.ListID())
	}
}
