package provider

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"sync"
)

// Standard operational states for widget payloads.
const (
	StateHealthy  = "healthy"
	StateDegraded = "degraded"
	StateError    = "error"
)

// WidgetPayload represents the standard JSON payload envelope published over the SSE bus
// or stored in cache.A.
type WidgetPayload struct {
	WidgetID  string `json:"widget_id"`
	Timestamp string `json:"timestamp"`
	State     string `json:"state"`
	Data      any    `json:"data"`
}

// InitOptions contains instance metadata, transport parameters, and resolved secrets passed to Provider.Init.
type InitOptions struct {
	ID             string
	Type           string
	Dimensions     []int
	Endpoint       string
	Method         string
	Token          string
	Timezone       string
	Secrets        map[string]string
	ResponseSchema map[string]any
}

// GetSecret retrieves a secret by key from the Secrets map, or returns empty string if not found.
func (o InitOptions) GetSecret(key string) string {
	if o.Secrets == nil {
		return ""
	}
	return o.Secrets[key]
}

// Provider defines the lifecycle and synchronization contract for all widget data fetchers.
type Provider interface {
	Init(ctx context.Context, config map[string]any, opts InitOptions) error
	Fetch(ctx context.Context) (any, error)
	Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error
	Shutdown(ctx context.Context) error
}

// Factory constructs a new instance of a Provider.
type Factory func() Provider

// Registry manages provider factories for in-process widgets.
type Registry interface {
	Register(providerType string, factory Factory)
	Create(providerType string) (Provider, error)
}

// ErrProviderNotRegistered indicates no factory is registered for the requested provider type.
var (
	ErrProviderNotRegistered      = errors.New("provider not registered")
	ErrWidgetNotFound             = errors.New("widget not found in active configuration")
	ErrListWidgetPushForbidden    = errors.New("cannot push state to list-backed widget; task lists are read-only and ingested from configured upstream providers")
	ErrNonHTTPWidgetPushForbidden = errors.New("cannot push state to non-HTTP widget; push webhook is strictly limited to widgets using the http provider")
	ErrSchemaValidation           = errors.New("response_schema validation failed")
	ErrPhotoPushNotSupported      = errors.New("photo push not supported: widget is not configured for ember_bridge source")
	ErrInvalidPhotoPushPayload    = errors.New("invalid photo push payload")
)

// DefaultRegistry implements a thread-safe Registry.
type DefaultRegistry struct {
	mu        sync.RWMutex
	factories map[string]Factory
}

var (
	builtinFactoriesMu sync.RWMutex
	builtinFactories   = map[string]Factory{}
)

func registerBuiltin(providerType string, factory Factory) {
	builtinFactoriesMu.Lock()
	defer builtinFactoriesMu.Unlock()
	builtinFactories[providerType] = factory
}

// NewRegistry constructs a Registry pre-populated with built-in providers (e.g. spacer).
func NewRegistry() *DefaultRegistry {
	r := &DefaultRegistry{
		factories: make(map[string]Factory),
	}
	builtinFactoriesMu.RLock()
	defer builtinFactoriesMu.RUnlock()
	maps.Copy(r.factories, builtinFactories)
	return r
}

// Register registers a factory for a given provider type.
func (r *DefaultRegistry) Register(providerType string, factory Factory) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.factories[providerType] = factory
}

// Create instantiates a provider for the given provider type.
func (r *DefaultRegistry) Create(providerType string) (Provider, error) {
	r.mu.RLock()
	factory, ok := r.factories[providerType]
	r.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrProviderNotRegistered, providerType)
	}
	return factory(), nil
}
