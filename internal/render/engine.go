package render

import (
	"bytes"
	"context"
	"fmt"
	"html/template"
	"maps"
	"os"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
)

// PackageResolver defines operations needed to look up widget packages.
type PackageResolver interface {
	LoadPackage(widgetType string) (*domain.Package, error)
}

// StateSnapshotProvider supplies runtime configuration snapshots and widget states.
type StateSnapshotProvider interface {
	CurrentSnapshot() *config.Snapshot
	GetWidgetState(widgetID string) (data any, state string, timestamp string, ok bool)
	CurrentStatus() config.Status
}

// WidgetNotFoundError signals that a widget instance does not exist in the active configuration.
type WidgetNotFoundError struct {
	WidgetID string
}

func (e WidgetNotFoundError) Error() string {
	return fmt.Sprintf("widget '%s' not found in active configuration", e.WidgetID)
}

// EngineOption configures Engine instances.
type EngineOption func(*Engine)

// WithNowFunc overrides the clock for deterministic testing.
func WithNowFunc(fn func() time.Time) EngineOption {
	return func(e *Engine) {
		e.nowFunc = fn
	}
}

// WithNodeDataFunc supplies per-display widget data. fn is called at render
// time with the widget ID and the requesting display's node_id (possibly empty);
// when it reports ok, its data replaces the shared cached data for that render.
func WithNodeDataFunc(fn func(widgetID, nodeID string) (any, bool)) EngineOption {
	return func(e *Engine) {
		e.nodeData = fn
	}
}

// Engine manages widget template compilation, in-memory template caching, and server-side rendering.
type Engine struct {
	mu       sync.RWMutex
	cache    map[string]*template.Template
	resolver PackageResolver
	provider StateSnapshotProvider
	nowFunc  func() time.Time
	nodeData func(widgetID, nodeID string) (any, bool)
}

// NewEngine constructs a thread-safe Engine.
func NewEngine(resolver PackageResolver, provider StateSnapshotProvider, opts ...EngineOption) *Engine {
	e := &Engine{
		cache:    make(map[string]*template.Template),
		resolver: resolver,
		provider: provider,
		nowFunc:  time.Now,
	}
	for _, opt := range opts {
		opt(e)
	}
	return e
}

// Invalidate evicts a single widget type from the compiled template cache.
func (e *Engine) Invalidate(widgetType string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.cache, widgetType)
}

// RenderWidgetWithOverrides renders a widget instance for one display with optional runtime configuration overrides.
func (e *Engine) RenderWidgetWithOverrides(ctx context.Context, widgetID, nodeID string, overrides map[string]any) ([]byte, error) {
	pkg, tmplCtx, err := e.resolveWidget(widgetID, nodeID, overrides)
	if err != nil {
		return nil, err
	}

	// Obtain or compile template
	tmpl, err := e.getOrCompileTemplate(pkg)
	if err != nil {
		return nil, err
	}

	// Execute template
	var buf bytes.Buffer
	if err := tmpl.Execute(&buf, tmplCtx); err != nil {
		return nil, fmt.Errorf("failed to execute template for widget %q: %w", widgetID, err)
	}

	return buf.Bytes(), nil
}

// WidgetContext returns the execution context for one widget instance on one display: placement,
// sanitized config with overrides applied, runtime state and the per-node data override. It is the
// context the HTML template receives, so the JSON widget route reports the same values.
func (e *Engine) WidgetContext(ctx context.Context, widgetID, nodeID string, overrides map[string]any) (Context, error) {
	_, tmplCtx, err := e.resolveWidget(widgetID, nodeID, overrides)
	return tmplCtx, err
}

// resolveWidget locates the declared widget instance and its package and builds the merged execution context.
func (e *Engine) resolveWidget(widgetID, nodeID string, overrides map[string]any) (*domain.Package, Context, error) {
	if widgetID == "" {
		return nil, Context{}, WidgetNotFoundError{WidgetID: ""}
	}

	if e.provider == nil {
		return nil, Context{}, fmt.Errorf("state snapshot provider is unconfigured")
	}

	snap := e.provider.CurrentSnapshot()
	if snap == nil || snap.Config == nil {
		return nil, Context{}, fmt.Errorf("no active configuration snapshot available")
	}

	// 1. Locate declared widget instance
	var targetWidget *config.WidgetConfig
	for i := range snap.Config.Display.Widgets {
		if snap.Config.Display.Widgets[i].ID == widgetID {
			targetWidget = &snap.Config.Display.Widgets[i]
			break
		}
	}
	if targetWidget == nil {
		return nil, Context{}, WidgetNotFoundError{WidgetID: widgetID}
	}

	// 2. Resolve widget package
	var pkg *domain.Package
	if snap.Packages != nil {
		pkg = snap.Packages[targetWidget.Type]
	}
	if pkg == nil && e.resolver != nil {
		var err error
		pkg, err = e.resolver.LoadPackage(targetWidget.Type)
		if err != nil {
			return nil, Context{}, fmt.Errorf("failed to load package for widget type %q: %w", targetWidget.Type, err)
		}
	}
	if pkg == nil {
		return nil, Context{}, fmt.Errorf("widget package for type %q not found", targetWidget.Type)
	}

	// 3. Build execution context
	tmplCtx := e.buildContext(widgetID, targetWidget, pkg, snap)
	if len(overrides) > 0 {
		mergedConfig := make(map[string]any, len(tmplCtx.Config)+len(overrides))
		maps.Copy(mergedConfig, tmplCtx.Config)
		maps.Copy(mergedConfig, overrides)
		tmplCtx.Config = mergedConfig
	}
	if e.nodeData != nil {
		if d, ok := e.nodeData(widgetID, nodeID); ok {
			tmplCtx.Data = d
		}
	}

	return pkg, tmplCtx, nil
}

func (e *Engine) getOrCompileTemplate(pkg *domain.Package) (*template.Template, error) {
	e.mu.RLock()
	cached, ok := e.cache[pkg.Type]
	e.mu.RUnlock()
	if ok {
		return cached, nil
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	// Double-check after acquiring write lock
	if cached, ok := e.cache[pkg.Type]; ok {
		return cached, nil
	}

	content, err := os.ReadFile(pkg.ViewPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read widget view template at %q: %w", pkg.ViewPath, err)
	}

	funcMap := StandardFuncMap(e.nowFunc)
	tmpl, err := template.New(pkg.Type).Funcs(funcMap).Parse(string(content))
	if err != nil {
		return nil, fmt.Errorf("template syntax error in %q: %w", pkg.ViewPath, err)
	}

	e.cache[pkg.Type] = tmpl
	return tmpl, nil
}

func fallbackWidgetDimension(w *config.WidgetConfig, pkg *domain.Package) domain.Dimension {
	if len(w.Dimensions) == 2 {
		if d, err := domain.DimensionFromSlice(w.Dimensions); err == nil {
			return d
		}
	}
	if pkg != nil && pkg.Manifest.DefaultDimensions.IsValid() {
		return pkg.Manifest.DefaultDimensions
	}
	return domain.NewDimension(1, 1)
}

func resolveWidgetPlacement(widgetID string, w *config.WidgetConfig, pkg *domain.Package, snap *config.Snapshot) ([2]int, domain.Dimension) {
	origin := [2]int{0, 0}
	var dim domain.Dimension
	placed := false

	if snap.Layout != nil {
		for _, screen := range snap.Layout.Screens {
			for _, pw := range screen.Widgets {
				if pw.WidgetID == widgetID {
					origin = pw.Origin
					dim = pw.Dimensions
					placed = true
					break
				}
			}
			if placed {
				break
			}
		}
	}

	if !placed {
		dim = fallbackWidgetDimension(w, pkg)
	}

	return origin, dim
}

func (e *Engine) resolveWidgetRuntimeState(widgetID string) (any, string, string, bool) {
	var wData any = map[string]any{}
	wState := "healthy"
	var timestamp string
	if e.provider == nil {
		return wData, wState, timestamp, true
	}

	if d, s, ts, ok := e.provider.GetWidgetState(widgetID); ok {
		if d != nil {
			wData = d
		}
		if s != "" {
			wState = s
		}
		timestamp = ts
	}

	if timestamp == "" {
		if tsProvider, ok := e.provider.(interface {
			GetWidgetTimestamp(widgetID string) (string, bool)
		}); ok {
			if ts, ok := tsProvider.GetWidgetTimestamp(widgetID); ok && ts != "" {
				timestamp = ts
			}
		}
	}

	online := e.provider.CurrentStatus().ConfigStatus != config.ConfigStatusError
	return wData, wState, timestamp, online
}

func (e *Engine) buildContext(widgetID string, w *config.WidgetConfig, pkg *domain.Package, snap *config.Snapshot) Context {
	origin, dim := resolveWidgetPlacement(widgetID, w, pkg, snap)
	wData, wState, timestamp, online := e.resolveWidgetRuntimeState(widgetID)

	return Context{
		ID:         widgetID,
		Type:       w.Type,
		Dimensions: dim,
		Data:       wData,
		State:      wState,
		Timestamp:  timestamp,
		Config:     SanitizeConfig(w.Config),
		Origin:     origin,
		Theme:      "dark",
		Online:     online,
		Assets:     fmt.Sprintf("widget-types/%s/assets", w.Type),
	}
}
