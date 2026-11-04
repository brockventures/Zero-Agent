package adapters_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/tasks/adapters"
)

func TestUpstreamHTTPError(t *testing.T) {
	t.Parallel()
	err := adapters.UpstreamHTTPError{Code: 503, URL: "https://user:pass@example.com/api?token=secret"}
	if err.StatusCode() != 503 || !strings.Contains(err.Error(), "example.com") || strings.Contains(err.Error(), "secret") {
		t.Fatalf("unexpected err: %v", err)
	}
}

func TestTodoistAdapter_CompleteItem(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name, itemID  string
		route         mockRoute
		close, cancel bool
		wantErr       string
		wantErrIs     error
		wantCode      int
	}{
		{"204_no_content", "i1", mockRoute{status: 204}, false, false, "", nil, 0},
		{"200_ok", "i1", mockRoute{status: 200}, false, false, "", nil, 0},
		{"404_not_found", "i1", mockRoute{status: 404}, false, false, "", adapters.ErrUpstreamNotFound, 0},
		{"401_unauthorized", "i1", mockRoute{status: 401}, false, false, "", adapters.ErrUnauthorized, 0},
		{"500_error", "i1", mockRoute{status: 500}, false, false, "", nil, 500},
		{"empty_item_id", "", mockRoute{}, false, false, "item ID is required", nil, 0},
		{"invalid_item_id", "bad/..", mockRoute{}, false, false, "invalid item ID", nil, 0},
		{"network_error", "i1", mockRoute{}, true, false, "todoist close request failed", nil, 0},
		{"canceled_context", "i1", mockRoute{status: 200}, false, true, "context canceled", nil, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			srv := newRouteServer(t, map[string]mockRoute{"/api/v1/tasks/" + tc.itemID + "/close": tc.route}, nil)
			if tc.close {
				srv.Close()
			}
			ad, _ := adapters.NewTodoistAdapter(adapters.TodoistAdapterConfig{Token: "tok", BaseURL: srv.URL, Client: srv.Client()})
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if tc.cancel {
				cancel()
			}
			err := ad.CompleteItem(ctx, tc.itemID)
			if tc.wantErrIs != nil && !errors.Is(err, tc.wantErrIs) {
				t.Fatalf("expected %v, got %v", tc.wantErrIs, err)
			} else if tc.wantErr != "" && (err == nil || !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("expected %q, got %v", tc.wantErr, err)
			}
			var httpErr adapters.UpstreamHTTPError
			if tc.wantCode != 0 && (!errors.As(err, &httpErr) || httpErr.Code != tc.wantCode) {
				t.Fatalf("expected code %d, got %v", tc.wantCode, err)
			}
		})
	}
}
