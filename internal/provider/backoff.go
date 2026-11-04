package provider

import (
	"errors"
	"math"
	"math/rand"
	"net"
	"strings"
	"syscall"
	"time"
)

// HTTPStatusCoder is an interface optionally implemented by errors carrying an HTTP status code.
type HTTPStatusCoder interface {
	StatusCode() int
}

var transientErrorSubstrings = []string{
	"connection refused",
	"connection reset",
	"timeout",
	"deadline exceeded",
	"500 internal server error",
	"502 bad gateway",
	"503 service unavailable",
	"504 gateway timeout",
	"429 too many requests",
	"status 500",
	"status 502",
	"status 503",
	"status 504",
	"status 429",
	"http 500",
	"http 502",
	"http 503",
	"http 504",
	"http 429",
}

// IsNetworkOrServerError inspects whether an error represents a transient network fault,
// connection refusal, timeout, or upstream 5xx/429 status code.
func IsNetworkOrServerError(err error) bool {
	if err == nil {
		return false
	}

	// 1. Explicit syscall checks
	if errors.Is(err, syscall.ECONNREFUSED) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.ETIMEDOUT) ||
		errors.Is(err, syscall.EHOSTUNREACH) ||
		errors.Is(err, syscall.ENETUNREACH) {
		return true
	}

	// 2. Net errors (timeouts, DNS lookup failures, socket operation errors)
	if netErr, ok := errors.AsType[net.Error](err); ok && netErr != nil {
		return true
	}
	if opErr, ok := errors.AsType[*net.OpError](err); ok && opErr != nil {
		return true
	}

	// 3. HTTP status coder interface
	var httpErr HTTPStatusCoder
	if errors.As(err, &httpErr) {
		code := httpErr.StatusCode()
		if code >= 500 || code == 429 {
			return true
		}
		// Explicit client errors (400, 401, 403, 404, etc.) are permanent
		return false
	}

	// 4. String pattern matching for common error messages
	msg := strings.ToLower(err.Error())
	for _, sub := range transientErrorSubstrings {
		if strings.Contains(msg, sub) {
			return true
		}
	}

	return false
}

// BackoffPolicy configures exponential backoff with proportional jitter.
type BackoffPolicy struct {
	InitialInterval time.Duration
	MaxInterval     time.Duration
	Multiplier      float64
	JitterFraction  float64
	RandFunc        func() float64
}

// DefaultBackoffPolicy returns standard exponential backoff configuration.
func DefaultBackoffPolicy() *BackoffPolicy {
	return &BackoffPolicy{
		InitialInterval: 5 * time.Second,
		MaxInterval:     300 * time.Second,
		Multiplier:      2.0,
		JitterFraction:  0.2, // +/- 20%
		RandFunc:        rand.Float64,
	}
}

// NextDelay calculates the backoff delay for the given failure count, clamped
// to the minimum of MaxInterval and intervalCap (the widget's normal polling interval).
func (b *BackoffPolicy) NextDelay(failures int, intervalCap time.Duration) time.Duration {
	if failures <= 0 {
		failures = 1
	}

	initial := b.InitialInterval
	if initial <= 0 {
		initial = 5 * time.Second
	}

	multiplier := b.Multiplier
	if multiplier < 1.0 {
		multiplier = 2.0
	}

	maxDelay := b.MaxInterval
	if maxDelay <= 0 {
		maxDelay = 300 * time.Second
	}

	// Clamp to the widget's configured refresh interval
	if intervalCap > 0 && intervalCap < maxDelay {
		maxDelay = intervalCap
	}

	// Exponential delay = initial * (multiplier ^ (failures - 1))
	delay := float64(initial) * math.Pow(multiplier, float64(failures-1))
	if delay > float64(maxDelay) {
		delay = float64(maxDelay)
	}

	// Apply proportional jitter
	jitterFrac := b.JitterFraction
	if jitterFrac < 0 {
		jitterFrac = 0
	} else if jitterFrac > 1.0 {
		jitterFrac = 1.0
	}

	randFn := b.RandFunc
	if randFn == nil {
		randFn = rand.Float64
	}

	r := randFn()
	jitter := (r*2.0 - 1.0) * jitterFrac
	finalDelay := max(time.Duration(delay*(1.0+jitter)), 0)

	// Enforce hard ceiling against intervalCap
	if intervalCap > 0 && finalDelay > intervalCap {
		finalDelay = intervalCap
	}

	return finalDelay
}

// ComputeRetryDelay evaluates the error and returns the appropriate delay before the next sync.
// If err is a network refusal, timeout, or upstream 5xx/429 error, it calculates exponential backoff.
// If err is a non-retryable client error (or nil), it returns the regular intervalCap.
func (b *BackoffPolicy) ComputeRetryDelay(err error, failures int, intervalCap time.Duration) time.Duration {
	if err == nil {
		return intervalCap
	}
	if !IsNetworkOrServerError(err) {
		return intervalCap
	}
	return b.NextDelay(failures, intervalCap)
}
