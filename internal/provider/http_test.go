package provider_test

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

func sampleResponseSchema() map[string]any {
	return map[string]any{
		"type":       "object",
		"required":   []any{"temperature", "humidity"},
		"properties": map[string]any{"temperature": map[string]any{"type": "number"}, "humidity": map[string]any{"type": "number"}, "status": map[string]any{"type": "string"}},
	}
}

func httpOpts(endpoint, method, token string, dims []int, secrets map[string]string, schema map[string]any) provider.InitOptions {
	return provider.InitOptions{ID: "w1", Type: "sensor-card", Endpoint: endpoint, Method: method, Token: token, Dimensions: dims, Secrets: secrets, ResponseSchema: schema}
}

func TestHTTPProvider_Init(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name       string
		opts       provider.InitOptions
		wantErrSub string
	}{
		{"empty_endpoint", httpOpts("", "GET", "", nil, nil, sampleResponseSchema()), "requires non-empty endpoint"},
		{"unsupported_method", httpOpts("http://x", "DELETE", "", nil, nil, sampleResponseSchema()), "unsupported HTTP method"},
		{"empty_schema", httpOpts("http://x", "GET", "", nil, nil, nil), "requires non-empty response_schema"},
		{"invalid_schema", httpOpts("http://x", "GET", "", nil, nil, map[string]any{"type": 123}), "failed to compile response_schema"},
		{"marshal_schema_err", httpOpts("http://x", "GET", "", nil, nil, map[string]any{"bad": make(chan int)}), "failed to marshal response_schema"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			p := provider.NewHTTPProvider()
			err := p.Init(context.Background(), nil, tc.opts)
			if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
				t.Fatalf("Init() error = %v, want substring %q", err, tc.wantErrSub)
			}
		})
	}
}

func TestHTTPProvider_Fetch(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name           string
		method         string
		status         int
		respBody       string
		token          string
		secrets        map[string]string
		dims           []int
		cfg            map[string]any
		wantVal        float64
		wantErrSub     string
		wantStatusCode int
		cancelCtx      bool
		badURL         bool
	}{
		{"fetch_post_ok", "POST", 200, `{"temperature":72.5,"humidity":45.0,"status":"good"}`, "secret-token-123", nil, []int{2, 1}, map[string]any{"entity_id": "sensor.lr", "unit": "F"}, 72.5, "", 0, false, false},
		{"fetch_get_ok", "GET", 200, `{"temperature":68.0,"humidity":50.0}`, "", map[string]string{"token_env": "env-token-456"}, nil, nil, 68.0, "", 0, false, false},
		{"token_env_dims", "POST", 200, `{"temperature":70.0,"humidity":40.0}`, "", map[string]string{"token_env": "secret-from-env"}, []int{3, 2}, nil, 70.0, "", 0, false, false},
		{"status_503_backoff", "GET", 503, "upstream unavailable", "", nil, nil, nil, 0, "HTTP 503", 503, false, false},
		{"status_400_permanent", "GET", 400, "bad request", "", nil, nil, nil, 0, "HTTP 400", 400, false, false},
		{"invalid_json", "GET", 200, "not valid json", "", nil, nil, nil, 0, "invalid response JSON", 0, false, false},
		{"non_object_json", "GET", 200, "[1, 2, 3]", "", nil, nil, nil, 0, "response body must be a JSON object", 0, false, false},
		{"schema_fail", "GET", 200, `{"temperature":75.0}`, "", nil, nil, nil, 0, "response_schema validation failed", 0, false, false},
		{"cancelled_context", "GET", 200, "{}", "", nil, nil, nil, 0, "context", 0, true, false},
		{"marshal_cfg_err", "POST", 200, "{}", "", nil, nil, map[string]any{"bad": make(chan int)}, 0, "failed to marshal domain config", 0, false, false},
		{"bad_request_url", "GET", 200, "{}", "", nil, nil, nil, 0, "failed to create HTTP request", 0, false, true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var gotMethod, gotDims, gotAuth, gotBody string
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if tc.cancelCtx {
					<-r.Context().Done()
					return
				}
				gotMethod, gotDims, gotAuth = r.Method, r.Header.Get("X-Widget-Dimensions"), r.Header.Get("Authorization")
				b, _ := io.ReadAll(r.Body)
				gotBody = string(b)
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.respBody))
			}))
			defer srv.Close()

			endpoint := srv.URL
			if tc.badURL {
				endpoint = "://invalid-url"
			}
			p := provider.NewHTTPProviderWithClient(srv.Client(), nil)
			if err := p.Init(context.Background(), tc.cfg, httpOpts(endpoint, tc.method, tc.token, tc.dims, tc.secrets, sampleResponseSchema())); err != nil {
				t.Fatalf("Init failed: %v", err)
			}

			ctx := context.Background()
			if tc.cancelCtx {
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}

			data, err := p.Fetch(ctx)
			if tc.wantErrSub != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
					t.Fatalf("Fetch() error = %v, want substring %q", err, tc.wantErrSub)
				}
				if tc.wantStatusCode != 0 {
					var sc provider.HTTPStatusCoder
					if !errors.As(err, &sc) || sc.StatusCode() != tc.wantStatusCode {
						t.Fatalf("want status code %d, got %v", tc.wantStatusCode, err)
					}
					delay := provider.DefaultBackoffPolicy().ComputeRetryDelay(err, 1, 900*time.Second)
					if (tc.wantStatusCode == 503 && delay >= 900*time.Second) || (tc.wantStatusCode == 400 && delay != 900*time.Second) {
						t.Fatalf("unexpected retry delay for %d: %v", tc.wantStatusCode, delay)
					}
				}
				return
			}
			if err != nil {
				t.Fatalf("Fetch() unexpected error: %v", err)
			}
			resMap, ok := data.(map[string]any)
			if !ok || resMap["temperature"] != tc.wantVal {
				t.Fatalf("unexpected data: %+v", data)
			}
			if tc.name == "fetch_post_ok" && (gotMethod != http.MethodPost || gotDims != "2x1" || gotAuth != "Bearer secret-token-123" || !strings.Contains(gotBody, "sensor.lr")) {
				t.Fatalf("unexpected headers/body: method=%s dims=%s auth=%s body=%s", gotMethod, gotDims, gotAuth, gotBody)
			}
			if tc.name == "token_env_dims" && (gotDims != "3x2" || gotAuth != "Bearer secret-from-env") {
				t.Fatalf("token_env dims header check failed: dims=%s auth=%s", gotDims, gotAuth)
			}
		})
	}
}

func TestHTTPProvider_LifecycleAndConstructors(t *testing.T) {
	t.Parallel()
	p := provider.NewHTTPProviderWithClient(nil, nil)
	if provider.NewHTTPProvider() == nil || p == nil || p.Subscribe(context.Background(), nil) != nil || p.Shutdown(context.Background()) != nil {
		t.Fatal("expected working constructors and no-op lifecycle methods")
	}
}
