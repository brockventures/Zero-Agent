package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

const (
	// MaxHTTPResponseBodySize limits inbound payload size to 1MB to prevent memory exhaustion.
	MaxHTTPResponseBodySize = 1 << 20
)

// HTTPStatusError represents a non-2xx HTTP response from an upstream provider.
// It implements HTTPStatusCoder and error.
type HTTPStatusError struct {
	Code int
	Body string
}

func (e *HTTPStatusError) Error() string {
	if e.Body == "" {
		return fmt.Sprintf("HTTP %d", e.Code)
	}
	return fmt.Sprintf("HTTP %d: %s", e.Code, e.Body)
}

func (e *HTTPStatusError) StatusCode() int {
	return e.Code
}

// HTTPProvider implements the Provider interface for out-of-process generic HTTP sidecars
// .
type HTTPProvider struct {
	client       *http.Client
	logger       *slog.Logger
	endpoint     string
	method       string
	token        string
	widgetID     string
	widgetType   string
	dimensions   []int
	domainConfig map[string]any
	schema       *jsonschema.Schema
}

func init() {
	registerBuiltin("http", NewHTTPProvider)
}

// NewHTTPProvider constructs a new HTTPProvider with standard Slowloris defense client.
func NewHTTPProvider() Provider {
	return NewHTTPProviderWithClient(&http.Client{
		Timeout: 10 * time.Second,
	}, slog.Default())
}

// NewHTTPProviderWithClient constructs a new HTTPProvider with injected HTTP client and logger.
func NewHTTPProviderWithClient(client *http.Client, logger *slog.Logger) Provider {
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &HTTPProvider{
		client: client,
		logger: logger,
	}
}

// Init initializes the provider with instance domain config and framework transport options.
func (p *HTTPProvider) Init(ctx context.Context, config map[string]any, opts InitOptions) error {
	endpoint := strings.TrimSpace(opts.Endpoint)
	if endpoint == "" {
		return errors.New("http provider requires non-empty endpoint")
	}

	method := strings.ToUpper(strings.TrimSpace(opts.Method))
	if method == "" {
		method = http.MethodPost
	}
	if method != http.MethodPost && method != http.MethodGet {
		return fmt.Errorf("unsupported HTTP method %q: only POST and GET are supported", opts.Method)
	}

	token := opts.Token
	if token == "" {
		token = opts.GetSecret("token")
	}
	if token == "" {
		token = opts.GetSecret("token_env")
	}

	if len(opts.ResponseSchema) == 0 {
		return errors.New("http provider requires non-empty response_schema")
	}

	sch, err := compileResponseSchema(opts.Type, opts.ResponseSchema)
	if err != nil {
		return err
	}

	p.endpoint = endpoint
	p.method = method
	p.token = token
	p.widgetID = opts.ID
	p.widgetType = opts.Type
	if len(opts.Dimensions) > 0 {
		p.dimensions = make([]int, len(opts.Dimensions))
		copy(p.dimensions, opts.Dimensions)
	}
	p.domainConfig = config
	p.schema = sch

	return nil
}

func compileResponseSchema(widgetType string, schemaMap map[string]any) (*jsonschema.Schema, error) {
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	schemaJSON, err := json.Marshal(schemaMap)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal response_schema: %w", err)
	}
	doc, err := jsonschema.UnmarshalJSON(bytes.NewReader(schemaJSON))
	if err != nil {
		return nil, fmt.Errorf("failed to unmarshal response_schema: %w", err)
	}
	schemaURL := fmt.Sprintf("schema://widgets/%s/response_schema.json", widgetType)
	if err := compiler.AddResource(schemaURL, doc); err != nil {
		return nil, fmt.Errorf("failed to add response_schema resource: %w", err)
	}
	sch, err := compiler.Compile(schemaURL)
	if err != nil {
		return nil, fmt.Errorf("failed to compile response_schema: %w", err)
	}
	return sch, nil
}

// Fetch executes a polling request against the configured endpoint and validates response schema.
func (p *HTTPProvider) Fetch(ctx context.Context) (any, error) {
	var bodyReader io.Reader
	if p.method == http.MethodPost {
		bodyData := p.domainConfig
		if bodyData == nil {
			bodyData = make(map[string]any)
		}
		payloadBytes, err := json.Marshal(bodyData)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal domain config: %w", err)
		}
		bodyReader = bytes.NewReader(payloadBytes)
	}

	req, err := http.NewRequestWithContext(ctx, p.method, p.endpoint, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("failed to create HTTP request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if p.widgetID != "" {
		req.Header.Set("X-Widget-Id", p.widgetID)
	}
	if p.widgetType != "" {
		req.Header.Set("X-Widget-Type", p.widgetType)
	}
	if len(p.dimensions) == 2 {
		req.Header.Set("X-Widget-Dimensions", fmt.Sprintf("%dx%d", p.dimensions[0], p.dimensions[1]))
	}
	if p.token != "" {
		req.Header.Set("Authorization", "Bearer "+p.token)
	}

	resp, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, MaxHTTPResponseBodySize))
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &HTTPStatusError{
			Code: resp.StatusCode,
			Body: strings.TrimSpace(string(bodyBytes)),
		}
	}

	valDoc, err := jsonschema.UnmarshalJSON(bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, fmt.Errorf("invalid response JSON: %w", err)
	}
	if _, ok := valDoc.(map[string]any); !ok {
		return nil, errors.New("response body must be a JSON object")
	}

	if p.schema != nil {
		if err := p.schema.Validate(valDoc); err != nil {
			return nil, fmt.Errorf("response_schema validation failed: %w", err)
		}
	}

	var res map[string]any
	if err := json.Unmarshal(bodyBytes, &res); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}
	return res, nil
}

// Subscribe returns nil as HTTP polling does not maintain persistent push connections.
func (p *HTTPProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown cleanly stops the provider.
func (p *HTTPProvider) Shutdown(ctx context.Context) error {
	return nil
}
