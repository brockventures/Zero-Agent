package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"net/url"
	"strings"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/provider"
)

// ErrWidgetActionNotConfigured indicates that neither a caller nor direct proxy URL is configured.
var ErrWidgetActionNotConfigured = errors.New("widget action caller not configured")

// WidgetActionCaller abstracts widget action execution across upstream providers.
type WidgetActionCaller interface {
	CallWidgetAction(ctx context.Context, widgetID, domain, service string, serviceData map[string]any) error
}

// WidgetActionResponseCaller executes an action and returns updated entity states or results.
type WidgetActionResponseCaller interface {
	CallWidgetActionWithResponse(ctx context.Context, widgetID, domain, service string, serviceData map[string]any) (any, error)
}

// DefaultWidgetActionHandler serves POST /api/widgets/{widget_id}/action.
type DefaultWidgetActionHandler struct {
	caller  WidgetActionCaller
	baseURL string
	token   string
	client  *http.Client
	logger  *slog.Logger
}

var _ WidgetActionHandler = (*DefaultWidgetActionHandler)(nil)

// WidgetActionOption configures a DefaultWidgetActionHandler.
type WidgetActionOption func(*DefaultWidgetActionHandler)

// WithWidgetActionCaller injects an external WidgetActionCaller.
func WithWidgetActionCaller(c WidgetActionCaller) WidgetActionOption {
	return func(h *DefaultWidgetActionHandler) { h.caller = c }
}

// WithHAActionClient configures direct Home Assistant proxying with an HTTP client, base URL, and bearer token.
func WithHAActionClient(client *http.Client, baseURL, token string) WidgetActionOption {
	return func(h *DefaultWidgetActionHandler) {
		h.client = client
		h.baseURL = strings.TrimRight(baseURL, "/")
		h.token = token
	}
}

// WithWidgetActionLogger injects a logger for action dispatch events.
func WithWidgetActionLogger(logger *slog.Logger) WidgetActionOption {
	return func(h *DefaultWidgetActionHandler) { h.logger = logger }
}

// NewDefaultWidgetActionHandler constructs a DefaultWidgetActionHandler.
func NewDefaultWidgetActionHandler(opts ...WidgetActionOption) *DefaultWidgetActionHandler {
	h := &DefaultWidgetActionHandler{
		client: http.DefaultClient,
	}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

// WidgetActionRequest represents the incoming JSON body for a widget action.
type WidgetActionRequest = api.WidgetActionRequest

// WidgetActionResponse represents the JSON response for a successful widget action.
type WidgetActionResponse = api.WidgetActionResponse

// handleWidgetAction handles POST /api/widgets/{widget_id}/action on Server.
func (s *Server) handleWidgetAction(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	h := s.widgetActionHandler
	s.mu.RUnlock()
	if h == nil {
		http.NotFound(w, r)
		return
	}
	h.PostWidgetAction(w, r, r.PathValue("widget_id"))
}

// PostWidgetAction implements the WidgetActionHandler interface.
func (h *DefaultWidgetActionHandler) PostWidgetAction(w http.ResponseWriter, r *http.Request, widgetID string) {
	h.handleWidgetAction(w, r, widgetID)
}

// handleWidgetAction processes a POST /api/widgets/{widget_id}/action request.
func (h *DefaultWidgetActionHandler) handleWidgetAction(w http.ResponseWriter, r *http.Request, widgetID string) {
	if !requireCORSAndMethod(w, r, "POST, OPTIONS", http.MethodPost) {
		return
	}
	widgetID = strings.TrimSpace(widgetID)
	if widgetID == "" || strings.Contains(widgetID, "..") || strings.Contains(widgetID, "/") {
		writeAPIError(w, http.StatusBadRequest, "invalid widget_id")
		return
	}

	domain, service, serviceData, err := h.parseRequest(w, r)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, err.Error())
		return
	}

	// 1. Injected WidgetActionCaller
	if h.caller != nil {
		if respCaller, ok := h.caller.(WidgetActionResponseCaller); ok {
			data, err := respCaller.CallWidgetActionWithResponse(r.Context(), widgetID, domain, service, serviceData)
			if err != nil {
				code, msg := h.mapActionError(err)
				writeAPIError(w, code, msg)
				return
			}
			writeJSON(w, http.StatusOK, WidgetActionResponse{
				Status:   "ok",
				WidgetID: widgetID,
				Data:     data,
			})
			return
		}

		if err := h.caller.CallWidgetAction(r.Context(), widgetID, domain, service, serviceData); err != nil {
			code, msg := h.mapActionError(err)
			writeAPIError(w, code, msg)
			return
		}
		writeJSON(w, http.StatusOK, WidgetActionResponse{
			Status:   "ok",
			WidgetID: widgetID,
		})
		return
	}

	// 2. Direct Home Assistant proxy
	if h.baseURL != "" {
		h.proxyDirectHA(w, r, widgetID, domain, service, serviceData)
		return
	}

	writeAPIError(w, http.StatusInternalServerError, ErrWidgetActionNotConfigured.Error())
}

func (h *DefaultWidgetActionHandler) proxyDirectHA(w http.ResponseWriter, r *http.Request, widgetID, domain, service string, serviceData map[string]any) {
	reqURL := fmt.Sprintf("%s/api/services/%s/%s", strings.TrimRight(h.baseURL, "/"), url.PathEscape(domain), url.PathEscape(service))
	bodyBytes, err := json.Marshal(serviceData)
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, "failed to marshal service data: "+err.Error())
		return
	}

	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, reqURL, bytes.NewReader(bodyBytes))
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to create upstream request: "+err.Error())
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if h.token != "" {
		req.Header.Set("Authorization", "Bearer "+h.token)
	}

	client := h.client
	if client == nil {
		client = http.DefaultClient
	}

	resp, err := client.Do(req)
	if err != nil {
		writeAPIError(w, http.StatusBadGateway, "upstream service call failed: "+err.Error())
		return
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		writeAPIError(w, http.StatusBadGateway, "failed to read response body: "+err.Error())
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		code := resp.StatusCode
		switch code {
		case http.StatusUnauthorized:
			writeAPIError(w, http.StatusUnauthorized, "upstream unauthorized")
		case http.StatusNotFound:
			writeAPIError(w, http.StatusNotFound, "upstream service or entity not found")
		default:
			if code >= 400 && code < 500 {
				writeAPIError(w, code, strings.TrimSpace(string(respBody)))
			} else {
				writeAPIError(w, http.StatusBadGateway, fmt.Sprintf("upstream returned status %d: %s", code, strings.TrimSpace(string(respBody))))
			}
		}
		return
	}

	var data any
	if len(respBody) > 0 {
		if err := json.Unmarshal(respBody, &data); err != nil {
			data = string(respBody)
		}
	}

	writeJSON(w, http.StatusOK, WidgetActionResponse{
		Status:   "ok",
		WidgetID: widgetID,
		Data:     data,
	})
}

func (h *DefaultWidgetActionHandler) parseRequest(w http.ResponseWriter, r *http.Request) (string, string, map[string]any, error) {
	if r.Body == nil {
		return "", "", nil, errors.New("request body is required")
	}
	defer r.Body.Close()

	var req WidgetActionRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		return "", "", nil, errors.New("invalid request payload")
	}

	domain, service, err := splitDomainService(req.Domain, req.Service, req.Action)
	if err != nil {
		return "", "", nil, err
	}

	if !provider.IsValidIdentifier(domain) {
		return "", "", nil, fmt.Errorf("invalid domain %q: must match ^[a-z0-9_]+$", domain)
	}
	if !provider.IsValidIdentifier(service) {
		return "", "", nil, fmt.Errorf("invalid service %q: must match ^[a-z0-9_]+$", service)
	}
	if !provider.IsSupportedDomain(domain) {
		return "", "", nil, fmt.Errorf("unsupported domain: %q", domain)
	}

	return domain, service, extractServiceData(req), nil
}

func splitDomainService(domain, service, action string) (string, string, error) {
	domain = strings.TrimSpace(domain)
	service = strings.TrimSpace(service)
	if service == "" && action != "" && action != "call_service" && action != "call-service" {
		service = strings.TrimSpace(action)
	}

	for _, sep := range []string{".", "/"} {
		if parts := strings.SplitN(service, sep, 2); len(parts) == 2 && parts[0] != "" && parts[1] != "" {
			if domain == "" {
				domain = strings.TrimSpace(parts[0])
				service = strings.TrimSpace(parts[1])
				break
			} else if strings.EqualFold(strings.TrimSpace(parts[0]), domain) {
				service = strings.TrimSpace(parts[1])
				break
			}
		}
	}

	if domain == "" || service == "" {
		return "", "", errors.New("domain and service are required")
	}
	return domain, service, nil
}

func extractServiceData(req WidgetActionRequest) map[string]any {
	serviceData := make(map[string]any)
	if req.ServiceData != nil {
		maps.Copy(serviceData, req.ServiceData)
	}
	if req.Data != nil {
		maps.Copy(serviceData, req.Data)
	}
	if req.Target != nil {
		for k, v := range req.Target {
			if _, exists := serviceData[k]; !exists {
				serviceData[k] = v
			}
		}
	}
	if req.EntityID != "" {
		if _, exists := serviceData["entity_id"]; !exists {
			serviceData["entity_id"] = req.EntityID
		}
	}
	return serviceData
}

func (h *DefaultWidgetActionHandler) mapActionError(err error) (int, string) {
	if err == nil {
		return http.StatusOK, ""
	}
	msg := err.Error()
	switch {
	case errors.Is(err, provider.ErrWidgetNotFound), errors.Is(err, provider.ErrUpstreamNotFound):
		return http.StatusNotFound, msg
	case errors.Is(err, provider.ErrUpstreamUnauthorized):
		return http.StatusUnauthorized, msg
	case errors.Is(err, provider.ErrActionValidation):
		return http.StatusBadRequest, msg
	case errors.Is(err, provider.ErrUpstreamUnavailable), errors.Is(err, provider.ErrUpstreamStatus):
		return http.StatusBadGateway, msg
	}
	low := strings.ToLower(msg)
	switch {
	case strings.Contains(low, "not found"):
		return http.StatusNotFound, msg
	case strings.Contains(low, "unauthorized") || strings.Contains(low, "forbidden"):
		return http.StatusUnauthorized, msg
	case strings.Contains(low, "invalid") || strings.Contains(low, "malformed") || strings.Contains(low, "required"):
		return http.StatusBadRequest, msg
	case strings.Contains(low, "unreachable") || strings.Contains(low, "connection refused") || strings.Contains(low, "bad gateway"):
		return http.StatusBadGateway, msg
	default:
		return http.StatusInternalServerError, msg
	}
}
