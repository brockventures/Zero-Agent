package adapters

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/azylman/mirrormere/internal/tasks"
)

const MaxPayloadBytes = 2 * 1024 * 1024

var (
	ErrPayloadTooLarge  = errors.New("payload exceeds maximum permitted size of 2MB")
	ErrUpstreamNotFound = errors.New("upstream task list not found")
	ErrUnauthorized     = errors.New("upstream unauthorized: token expired or invalid")
)

type UpstreamHTTPError struct {
	Code int
	URL  string
}

func (e UpstreamHTTPError) Error() string {
	return fmt.Sprintf("upstream returned http status %d for %s", e.Code, sanitizeURL(e.URL))
}
func (e UpstreamHTTPError) StatusCode() int { return e.Code }

type Adapter interface {
	Name() string
	FetchList(ctx context.Context) (*tasks.List, []tasks.ListItem, error)
}

type Completer interface {
	CompleteItem(ctx context.Context, itemID string) error
}

func validateHTTPURL(raw, fallback, fieldName string) (string, error) {
	val := strings.TrimSpace(raw)
	if val == "" {
		val = fallback
	}
	if val == "" {
		return "", fmt.Errorf("%s is required", fieldName)
	}
	val = strings.TrimRight(val, "/")
	if u, err := url.ParseRequestURI(val); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", fmt.Errorf("invalid %s %q: must be http:// or https:// with host", fieldName, val)
	}
	return val, nil
}

func defaultHTTPClient(c *http.Client) *http.Client {
	if c != nil {
		return c
	}
	return &http.Client{Timeout: 10 * time.Second}
}

func mapUpstreamStatus(code int, targetURL string) error {
	switch code {
	case http.StatusOK, http.StatusNoContent:
		return nil
	case http.StatusUnauthorized, http.StatusForbidden:
		return ErrUnauthorized
	case http.StatusNotFound:
		return ErrUpstreamNotFound
	default:
		return UpstreamHTTPError{Code: code, URL: targetURL}
	}
}

func doBearerGet(ctx context.Context, client *http.Client, targetURL, token, errPrefix string) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return nil, 0, fmt.Errorf("failed to create http request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("%s failed for %s: %w", errPrefix, sanitizeURL(targetURL), err)
	}
	defer func() { _ = resp.Body.Close() }()
	if statusErr := mapUpstreamStatus(resp.StatusCode, targetURL); statusErr != nil {
		return nil, resp.StatusCode, statusErr
	}
	buf := bytes.NewBuffer(make([]byte, 0, 16*1024))
	if n, readErr := buf.ReadFrom(io.LimitReader(resp.Body, MaxPayloadBytes+1)); readErr != nil {
		return nil, resp.StatusCode, fmt.Errorf("failed to read response body: %w", readErr)
	} else if n > MaxPayloadBytes {
		return nil, resp.StatusCode, ErrPayloadTooLarge
	}
	return bytes.TrimSpace(buf.Bytes()), resp.StatusCode, nil
}

func sanitizeURL(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil || u == nil {
		return rawURL
	}
	u.User, u.RawQuery = nil, ""
	return u.String()
}
