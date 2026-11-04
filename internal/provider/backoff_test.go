package provider_test

import (
	"errors"
	"net"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

type customHTTPError int

func (c customHTTPError) Error() string   { return "http" }
func (c customHTTPError) StatusCode() int { return int(c) }

func TestIsNetworkOrServerError(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"ECONNREFUSED", syscall.ECONNREFUSED, true},
		{"ECONNRESET", syscall.ECONNRESET, true},
		{"ETIMEDOUT", syscall.ETIMEDOUT, true},
		{"EHOSTUNREACH", syscall.EHOSTUNREACH, true},
		{"ENETUNREACH", syscall.ENETUNREACH, true},
		{"wrapped ECONNREFUSED", errors.Join(errors.New("dial"), syscall.ECONNREFUSED), true},
		{"net timeout", &net.DNSError{IsTimeout: true}, true},
		{"net OpError", &net.OpError{Op: "dial", Net: "tcp", Err: errors.New("closed")}, true},
		{"HTTP 500", customHTTPError(500), true},
		{"HTTP 502", customHTTPError(502), true},
		{"HTTP 503", customHTTPError(503), true},
		{"HTTP 429", customHTTPError(429), true},
		{"HTTP 400", customHTTPError(400), false},
		{"HTTP 401", customHTTPError(401), false},
		{"HTTP 404", customHTTPError(404), false},
		{"str conn refused", errors.New("connection refused"), true},
		{"str timeout", errors.New("deadline exceeded"), true},
		{"str 502", errors.New("502 Bad Gateway"), true},
		{"str 429", errors.New("429 Too Many Requests"), true},
		{"str status 503", errors.New("status 503"), true},
		{"str HTTP 503", errors.New("HTTP 503: down"), true},
		{"str HTTP 429", errors.New("HTTP 429"), true},
		{"HTTPStatusError 500", &provider.HTTPStatusError{Code: 500, Body: "err"}, true},
		{"HTTPStatusError 503 empty", &provider.HTTPStatusError{Code: 503}, true},
		{"HTTPStatusError 429", &provider.HTTPStatusError{Code: 429, Body: "rate"}, true},
		{"HTTPStatusError 404", &provider.HTTPStatusError{Code: 404, Body: "nf"}, false},
		{"schema failure", errors.New("validation failed against response_schema"), false},
		{"json parse error", errors.New("cannot unmarshal"), false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := provider.IsNetworkOrServerError(tc.err); got != tc.want {
				t.Errorf("IsNetworkOrServerError(%v) = %v; want %v", tc.err, got, tc.want)
			}
		})
	}
}

func TestBackoffPolicy(t *testing.T) {
	t.Parallel()
	bo := func(initSec, maxSec int, mult, jit, r float64) *provider.BackoffPolicy {
		return &provider.BackoffPolicy{InitialInterval: time.Duration(initSec) * time.Second, MaxInterval: time.Duration(maxSec) * time.Second, Multiplier: mult, JitterFraction: jit, RandFunc: func() float64 { return r }}
	}
	zero, def := bo(5, 60, 2, 0.2, 0.5), provider.DefaultBackoffPolicy()
	tests := []struct {
		name                     string
		policy                   *provider.BackoffPolicy
		err                      error
		failures                 int
		capDur, wantMin, wantMax time.Duration
	}{
		{"att1", zero, nil, 1, 30 * time.Second, 5 * time.Second, 5 * time.Second},
		{"att2", zero, nil, 2, 30 * time.Second, 10 * time.Second, 10 * time.Second},
		{"att3", zero, nil, 3, 30 * time.Second, 20 * time.Second, 20 * time.Second},
		{"att4_clamp", zero, nil, 4, 30 * time.Second, 30 * time.Second, 30 * time.Second},
		{"default_zero", &provider.BackoffPolicy{}, nil, 0, 0, 1 * time.Nanosecond, 10 * time.Minute},
		{"min_jitter", bo(10, 60, 2, 0.2, 0.0), nil, 1, 60 * time.Second, 8 * time.Second, 8 * time.Second},
		{"max_jitter", bo(10, 60, 2, 0.2, 1.0), nil, 1, 60 * time.Second, 12 * time.Second, 12 * time.Second},
		{"over_jitter", bo(50, 100, 2, 0.5, 1.0), nil, 1, 60 * time.Second, 60 * time.Second, 60 * time.Second},
		{"negative_cfg", bo(-1, -1, 0.5, -0.5, 0), nil, 1, 10 * time.Second, 1 * time.Nanosecond, 10 * time.Minute},
		{"jitter_gt_1", bo(5, 60, 2, 1.5, 0.5), nil, 1, 60 * time.Second, 1 * time.Nanosecond, 10 * time.Minute},
		{"compute_nil", def, nil, 1, 60 * time.Second, 60 * time.Second, 60 * time.Second},
		{"compute_400", def, customHTTPError(400), 3, 60 * time.Second, 60 * time.Second, 60 * time.Second},
		{"compute_net", def, syscall.ECONNREFUSED, 1, 60 * time.Second, 1 * time.Nanosecond, 59 * time.Second},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			d := tc.policy.NextDelay(tc.failures, tc.capDur)
			if strings.HasPrefix(tc.name, "compute") {
				d = tc.policy.ComputeRetryDelay(tc.err, tc.failures, tc.capDur)
			}
			if d < tc.wantMin || d > tc.wantMax {
				t.Fatalf("got %v, want [%v, %v]", d, tc.wantMin, tc.wantMax)
			}
		})
	}
}
