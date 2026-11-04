package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"os"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
	"github.com/santhosh-tekuri/jsonschema/v6"
	"gopkg.in/yaml.v3"
)

const (
	ConfigStatusOK    ConfigStatus = "ok"
	ConfigStatusError ConfigStatus = "error"

	ChangeUnchanged InstanceChangeType = "unchanged"
	ChangeAdded     InstanceChangeType = "added"
	ChangeRemoved   InstanceChangeType = "removed"
	ChangeModified  InstanceChangeType = "modified"
	ChangeReplaced  InstanceChangeType = "replaced"
)

type (
	// PackageLoader discovers and resolves widget packages by type.
	PackageLoader interface {
		LoadPackage(widgetType string) (*domain.Package, error)
	}
	// ConfigStatus indicates the operational validity of the running configuration.
	ConfigStatus string
	// InstanceChangeType categorizes the diff between running and new widget instances.
	InstanceChangeType string
	// Status provides telemetry and health state for system.status reporting.
	Status struct {
		ConfigStatus ConfigStatus `json:"config_status"`
		ConfigError  *string      `json:"config_error"`
		LastChecked  time.Time    `json:"last_checked"`
		LastSuccess  time.Time    `json:"last_success,omitempty"`
	}
	// Snapshot encapsulates an immutable, synchronized state of runtime configuration, layout, and packages.
	Snapshot struct {
		Config      *Config
		Layout      *layout.Layout
		Packages    map[string]*domain.Package
		LoadedAt    time.Time
		HasOverride bool
	}
	// InstanceDiff details modifications to an existing widget instance.
	InstanceDiff struct {
		ID, Type                 string
		Change                   InstanceChangeType
		OldInstance, NewInstance *WidgetConfig
		DomainChange             bool
	}
	// ConfigDiff summarizes all instance-level diffs between running and new configurations.
	ConfigDiff struct {
		Unchanged, Added, Removed, LayoutModified []WidgetConfig
		Modified                                  []InstanceDiff
		TimezoneChanged, ShellChanged             bool
	}
	// Manager coordinates live configuration reloads and enforces LKGC resilience.
	Manager struct {
		reloadMu              sync.Mutex
		current               atomic.Pointer[Snapshot]
		status                atomic.Pointer[Status]
		loader                PackageLoader
		getenv                func(string) string
		logger                *slog.Logger
		configReloadErr       error
		packageErrors         map[string]string
		baseYAML              []byte
		overridePath          string
		useConfigOverridePath bool
	}
)

// NewManager creates and initializes an LKGC Manager with the initial configuration.
func NewManager(initialYAML []byte, loader PackageLoader, getenv func(string) string, logger *slog.Logger, opts ...ManagerOption) (*Manager, error) {
	if loader == nil {
		return nil, fmt.Errorf("package loader cannot be nil")
	}
	if getenv == nil {
		getenv = os.Getenv
	}
	if logger == nil {
		logger = slog.Default()
	}
	snapshot, err := ValidatePipeline(initialYAML, loader, getenv)
	if err != nil {
		return nil, fmt.Errorf("initial configuration validation failed: %w", err)
	}
	m := &Manager{
		baseYAML:      initialYAML,
		loader:        loader,
		getenv:        getenv,
		logger:        logger,
		packageErrors: make(map[string]string),
	}
	for _, opt := range opts {
		opt(m)
	}
	snapshot = m.applyDiskOverrideIfPresent(snapshot)
	m.current.Store(snapshot)
	m.status.Store(&Status{ConfigStatus: ConfigStatusOK, LastChecked: snapshot.LoadedAt, LastSuccess: snapshot.LoadedAt})
	return m, nil
}

func (m *Manager) applyDiskOverrideIfPresent(snapshot *Snapshot) *Snapshot {
	overridePath := m.resolveOverridePath(snapshot)
	if overridePath == "" {
		return snapshot
	}
	ov, err := LoadLayoutOverride(overridePath)
	if err != nil {
		if !errors.Is(err, ErrOverrideNotFound) {
			m.logger.Warn("failed to load layout override file", "path", overridePath, "error", err)
		}
		return snapshot
	}
	overriddenSnap, err := applyOverrideToSnapshot(snapshot, ov, m.loader)
	if err != nil {
		m.logger.Warn("failed to apply layout override; retaining base configuration", "path", overridePath, "error", err)
		return snapshot
	}
	return overriddenSnap
}

// Current returns the active running configuration snapshot (lock-free).
func (m *Manager) Current() *Config {
	if s := m.current.Load(); s != nil {
		return s.Config
	}
	return nil
}

// CurrentSnapshot returns the complete running snapshot including packages and layout (lock-free).
func (m *Manager) CurrentSnapshot() *Snapshot { return m.current.Load() }

// Status returns the current configuration telemetry status (lock-free).
func (m *Manager) Status() Status {
	if s := m.status.Load(); s != nil {
		return *s
	}
	return Status{ConfigStatus: ConfigStatusOK}
}

func (m *Manager) updateStatusLocked() {
	now, lastSuccess := time.Now(), time.Time{}
	if prev := m.status.Load(); prev != nil {
		lastSuccess = prev.LastSuccess
	}
	var errMsg string
	if m.configReloadErr != nil {
		errMsg = m.configReloadErr.Error()
	} else if len(m.packageErrors) > 0 {
		errMsg = m.packageErrors[slices.Sorted(maps.Keys(m.packageErrors))[0]]
	}
	if errMsg != "" {
		m.status.Store(&Status{ConfigStatus: ConfigStatusError, ConfigError: &errMsg, LastChecked: now, LastSuccess: lastSuccess})
	} else {
		m.status.Store(&Status{ConfigStatus: ConfigStatusOK, LastChecked: now, LastSuccess: now})
	}
}

func (m *Manager) updateLocked(fn func()) {
	m.reloadMu.Lock()
	defer m.reloadMu.Unlock()
	fn()
	m.updateStatusLocked()
}

// SetPackageError records a package incompleteness error in Manager's status.
func (m *Manager) SetPackageError(widgetType string, err error) {
	if err != nil {
		m.updateLocked(func() { m.packageErrors[widgetType] = err.Error() })
	}
}

// ClearPackageError clears an incomplete package error for widgetType.
func (m *Manager) ClearPackageError(widgetType string) bool {
	var ok bool
	m.updateLocked(func() {
		_, ok = m.packageErrors[widgetType]
		delete(m.packageErrors, widgetType)
	})
	return ok
}

// SetErrorStatus explicitly records an error status (e.g. for configuration read errors).
func (m *Manager) SetErrorStatus(err error) {
	if err != nil {
		m.updateLocked(func() { m.configReloadErr = err })
	}
}

// Reload executes the 6-stage validation pipeline on candidate YAML data.
func (m *Manager) Reload(data []byte) (*Snapshot, *ConfigDiff, error) {
	m.reloadMu.Lock()
	defer m.reloadMu.Unlock()
	candidate, err := ValidatePipeline(data, m.loader, m.getenv)
	if err != nil {
		m.logger.Error(fmt.Sprintf("[config.reloader] error=\"live config validation failed: %s; retaining LKGC\"", err.Error()))
		m.configReloadErr = err
		m.updateStatusLocked()
		return nil, nil, err
	}
	m.baseYAML = data
	candidate = m.applyDiskOverrideIfPresent(candidate)
	var oldCfg *Config
	if oldSnap := m.current.Load(); oldSnap != nil {
		oldCfg = oldSnap.Config
	}
	diff := DiffConfigs(oldCfg, candidate.Config)
	m.configReloadErr, m.packageErrors = nil, make(map[string]string)
	m.current.Store(candidate)
	m.updateStatusLocked()
	return candidate, diff, nil
}

// ValidatePipeline executes the 6-stage LKGC validation pipeline.
func ValidatePipeline(data []byte, loader PackageLoader, getenv func(string) string) (*Snapshot, error) {
	if loader == nil {
		return nil, fmt.Errorf("package loader cannot be nil")
	}
	if getenv == nil {
		getenv = os.Getenv
	}
	cfg, err := parseYAMLStage(data)
	if err != nil {
		return nil, fmt.Errorf("stage 1 (syntax & structure): %w", err)
	} else if err = validateCoreInstancesStage(cfg, getenv); err != nil {
		return nil, fmt.Errorf("stage 2 (instance schemas): %w", err)
	}
	pkgs, err := validatePackagesStage(cfg, loader)
	if err != nil {
		return nil, fmt.Errorf("stage 3 (package existence & completeness): %w", err)
	} else if err = validateManifestConfigSchemasStage(cfg, pkgs); err != nil {
		return nil, fmt.Errorf("stage 4 (manifest config_schema): %w", err)
	} else if err = validateDomainRulesStage(cfg, pkgs); err != nil {
		return nil, fmt.Errorf("stage 5 (domain rules): %w", err)
	}
	inputs := make([]layout.WidgetInput, len(cfg.Display.Widgets))
	for i, w := range cfg.Display.Widgets {
		dim, err := domain.DimensionFromSlice(w.Dimensions)
		if err != nil {
			return nil, fmt.Errorf("stage 6 (layout solver): %w", err)
		}
		inputs[i] = layout.WidgetInput{ID: w.ID, Type: w.Type, Dimensions: dim, Pinned: w.Pinned}
	}
	l, err := solveForConfigGrid(inputs, cfg.Display.Grid)
	if err != nil {
		return nil, fmt.Errorf("stage 6 (layout solver): layout solver error: %w", err)
	}
	return &Snapshot{Config: cfg, Layout: l, Packages: pkgs, LoadedAt: time.Now()}, nil
}

func solveForConfigGrid(inputs []layout.WidgetInput, grid GridConfig) (*layout.Layout, error) {
	if grid.Columns == 3 && grid.Rows == 6 {
		return layout.SolveGridWithBudget(inputs, 3, 6, 0)
	}
	return layout.Solve(inputs)
}

func validateCoreInstancesStage(cfg *Config, getenv func(string) string) error {
	if err := cfg.Validate(); err != nil {
		return err
	}
	return cfg.ResolveEnv(getenv)
}

func parseYAMLStage(data []byte) (*Config, error) {
	if len(strings.TrimSpace(string(data))) == 0 {
		return nil, fmt.Errorf("configuration file is empty")
	}
	var root yaml.Node
	if err := yaml.Unmarshal(data, &root); err != nil {
		return nil, fmt.Errorf("YAML syntax error: %w", err)
	} else if err := checkNodeForInterpolation(&root); err != nil {
		return nil, err
	} else if err := validateStructuralKeys(&root); err != nil {
		return nil, err
	}
	var cfg Config
	if err := root.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("failed to decode configuration: %w", err)
	}
	cfg.applyDefaults()
	return &cfg, nil
}

var portraitDefaultDimMap = map[domain.Dimension]domain.Dimension{
	{Cols: 6, Rows: 2}: {Cols: 3, Rows: 6},
	{Cols: 4, Rows: 2}: {Cols: 3, Rows: 4},
	{Cols: 3, Rows: 2}: {Cols: 3, Rows: 3},
	{Cols: 2, Rows: 2}: {Cols: 3, Rows: 2},
	{Cols: 2, Rows: 1}: {Cols: 3, Rows: 1},
}

func resolveWidgetDefaultDimensions(w *WidgetConfig, pkg *domain.Package, grid GridConfig) error {
	if len(w.Dimensions) > 0 {
		return nil
	}
	defDim := pkg.Manifest.DefaultDimensions
	if !defDim.IsValid() {
		return fmt.Errorf("[Mirrormere Config Error] widget '%s' omits 'dimensions' and package manifest '%s' has no valid default_dimensions", w.ID, w.Type)
	}
	if grid.Columns == 3 && grid.Rows == 6 {
		if mapped, ok := portraitDefaultDimMap[defDim]; ok && (len(pkg.Manifest.SupportedDimensions) == 0 || slices.Contains(pkg.Manifest.SupportedDimensions, mapped)) {
			defDim = mapped
		}
	}
	if !defDim.IsValidForGrid(grid.Columns, grid.Rows) {
		flipped := domain.NewDimension(defDim.Rows, defDim.Cols)
		if flipped.IsValidForGrid(grid.Columns, grid.Rows) &&
			(len(pkg.Manifest.SupportedDimensions) == 0 || slices.Contains(pkg.Manifest.SupportedDimensions, flipped)) {
			defDim = flipped
		}
	}
	w.Dimensions = defDim.Slice()
	return nil
}

func validateWidgetSupportedDimensions(w *WidgetConfig, pkg *domain.Package) error {
	if len(pkg.Manifest.SupportedDimensions) == 0 {
		return nil
	}
	dim, err := domain.DimensionFromSlice(w.Dimensions)
	if err != nil {
		return err
	}
	if !slices.Contains(pkg.Manifest.SupportedDimensions, dim) {
		return fmt.Errorf("widget '%s': dimensions [%d, %d] not supported by package '%s' (supported: %v)", w.ID, dim.Cols, dim.Rows, w.Type, pkg.Manifest.SupportedDimensions)
	}
	return nil
}

func validatePackagesStage(cfg *Config, loader PackageLoader) (map[string]*domain.Package, error) {
	registry := make(map[string]*domain.Package)
	for i := range cfg.Display.Widgets {
		w := &cfg.Display.Widgets[i]
		pkg, ok := registry[w.Type]
		if !ok {
			var err error
			if pkg, err = loader.LoadPackage(w.Type); err != nil {
				return nil, fmt.Errorf("widget package '%s' error: %w", w.Type, err)
			}
			if err = pkg.Manifest.Validate(); err != nil {
				return nil, fmt.Errorf("widget package '%s' manifest invalid: %w", w.Type, err)
			}
			registry[w.Type] = pkg
		}
		if err := resolveWidgetDefaultDimensions(w, pkg, cfg.Display.Grid); err != nil {
			return nil, err
		}
		if w.RefreshIntervalSeconds == nil && pkg.Manifest.Refresh.IntervalSeconds > 0 {
			w.RefreshIntervalSeconds = &pkg.Manifest.Refresh.IntervalSeconds
		}
		if err := validateWidgetSupportedDimensions(w, pkg); err != nil {
			return nil, err
		}
	}
	return registry, nil
}

func unmarshalSchemaDoc(v any) (any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return jsonschema.UnmarshalJSON(bytes.NewReader(b))
}

func validateManifestConfigSchemasStage(cfg *Config, registry map[string]*domain.Package) error {
	compiled := make(map[string]*jsonschema.Schema)
	for _, w := range cfg.Display.Widgets {
		pkg := registry[w.Type]
		if len(pkg.Manifest.ConfigSchema) == 0 {
			continue
		}
		sch, ok := compiled[w.Type]
		if !ok {
			c := jsonschema.NewCompiler()
			c.DefaultDraft(jsonschema.Draft2020)
			doc, err := unmarshalSchemaDoc(pkg.Manifest.ConfigSchema)
			if err != nil {
				return fmt.Errorf("widget '%s': failed to unmarshal manifest config_schema: %w", w.ID, err)
			}
			url := "schema://widgets/" + w.Type + "/config_schema.json"
			if err = c.AddResource(url, doc); err != nil {
				return fmt.Errorf("widget '%s': failed to add schema resource: %w", w.ID, err)
			} else if sch, err = c.Compile(url); err != nil {
				return fmt.Errorf("widget '%s': failed to compile manifest config_schema: %w", w.ID, err)
			}
			compiled[w.Type] = sch
		}
		cfgVal := w.Config
		if cfgVal == nil {
			cfgVal = map[string]any{}
		}
		valDoc, err := unmarshalSchemaDoc(cfgVal)
		if err != nil {
			return fmt.Errorf("widget '%s': failed to unmarshal instance config: %w", w.ID, err)
		} else if err := sch.Validate(valDoc); err != nil {
			return fmt.Errorf("manifest config_schema validation error for widget '%s': %w", w.ID, err)
		}
	}
	return nil
}

func derefStr(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func derefSlice(v any) []any {
	if s, ok := v.([]any); ok {
		return s
	}
	return nil
}

func validateTasksSourceConfig(widgetID, source string, cfg map[string]any) error {
	p := "[Mirrormere Config Error] tasks widget '" + widgetID + "'"
	if source != "gtasks" && source != "todoist" {
		return fmt.Errorf("%s declares invalid source '%s' (must be 'gtasks' or 'todoist')", p, source)
	}
	subCfg, ok := cfg[source].(map[string]any)
	if !ok || subCfg == nil {
		return fmt.Errorf("%s with source '%s' requires '%s' config block", p, source, source)
	}
	u := derefStr(subCfg["base_url"])
	if source == "todoist" && u != "" && !isValidHTTPURL(u) {
		return fmt.Errorf("%s: invalid todoist.base_url '%s' (must be http:// or https:// with host)", p, u)
	} else if source == "gtasks" && derefStr(subCfg["tasklist_id"]) == "" {
		return fmt.Errorf("%s with source 'gtasks' requires non-empty 'gtasks.tasklist_id'", p)
	}
	return nil
}

func validateDomainRulesStage(cfg *Config, registry map[string]*domain.Package) error {
	for _, w := range cfg.Display.Widgets {
		if pkg := registry[w.Type]; pkg != nil && pkg.Manifest.Provider == "http" && w.Endpoint == "" {
			return fmt.Errorf("widget '%s': provider 'http' requires non-empty 'endpoint' URL", w.ID)
		}
		if w.Type == "calendar-grid" && validateFamilyCalendarConfig(w.ID, w.Config) != nil {
			return validateFamilyCalendarConfig(w.ID, w.Config)
		}
		if w.Type != "tasks" {
			continue
		}
		src := derefStr(w.Config["source"])
		if src == "" {
			return fmt.Errorf("[Mirrormere Config Error] tasks widget '%s' requires non-empty 'source' ('gtasks' or 'todoist')", w.ID)
		}
		if err := validateTasksSourceConfig(w.ID, src, w.Config); err != nil {
			return err
		}
	}
	return nil
}

func validateFamilyCalendarConfig(widgetID string, rawConfig map[string]any) error {
	p := "[Mirrormere Config Error] calendar-family widget '" + widgetID + "': "
	cals := make(map[string]bool)
	for _, item := range derefSlice(rawConfig["calendars"]) {
		if m, ok := item.(map[string]any); ok && strings.TrimSpace(derefStr(m["name"])) != "" {
			cals[strings.TrimSpace(derefStr(m["name"]))] = true
		}
	}
	members := derefSlice(rawConfig["members"])
	if len(members) > 8 {
		return fmt.Errorf("%sat most 8 members are supported, found %d", p, len(members))
	}
	names, patterns := make(map[string]bool), make(map[string]string)
	for idx, item := range members {
		m, ok := item.(map[string]any)
		if !ok {
			return fmt.Errorf("%smembers[%d] is not a mapping", p, idx)
		}
		name := strings.TrimSpace(derefStr(m["name"]))
		if name == "" {
			return fmt.Errorf("%smembers[%d] requires non-empty 'name'", p, idx)
		} else if names[name] {
			return fmt.Errorf("%sduplicate member name %q", p, name)
		}
		names[name] = true
		pattern := trimmedOr(derefStr(m["pattern"]), "solid")
		if prev, ok := patterns[pattern]; ok {
			return fmt.Errorf("%smember %q and member %q both declare e-ink pattern %q; patterns must be distinct", p, prev, name, pattern)
		}
		patterns[pattern] = name
		for _, c := range derefSlice(m["calendars"]) {
			cal := strings.TrimSpace(derefStr(c))
			if cal == "" {
				return fmt.Errorf("%smember %q declares an empty calendar reference", p, name)
			} else if !cals[cal] {
				return fmt.Errorf("%smember %q claims unknown calendar %q (not declared in 'calendars')", p, name, cal)
			}
		}
	}
	if h := derefSlice(rawConfig["hours"]); len(h) == 2 {
		if s, e := derefStr(h[0]), derefStr(h[1]); s != "" && e != "" && s >= e {
			return fmt.Errorf("%s'hours' start %q must be before end %q", p, s, e)
		}
	}
	return nil
}

// DiffConfigs computes instance-level differences between old and new configurations.
func DiffConfigs(oldCfg, newCfg *Config) *ConfigDiff {
	diff := &ConfigDiff{}
	oldMap, oldWidgets := widgetMap(oldCfg)
	newMap, newWidgets := widgetMap(newCfg)
	if oldCfg != nil && newCfg != nil {
		diff.TimezoneChanged = oldCfg.Timezone != newCfg.Timezone
		diff.ShellChanged = !reflect.DeepEqual(oldCfg.Display.Header, newCfg.Display.Header) ||
			oldCfg.Display.Grid != newCfg.Display.Grid ||
			oldCfg.Display.HideCursor != newCfg.Display.HideCursor ||
			oldCfg.Display.RemoteURL != newCfg.Display.RemoteURL ||
			!reflect.DeepEqual(oldCfg.Display.Voice, newCfg.Display.Voice)
	}
	for _, w := range newWidgets {
		oldW, exists := oldMap[w.ID]
		switch {
		case !exists:
			diff.Added = append(diff.Added, w)
		case oldW.Type != w.Type:
			diff.Modified = append(diff.Modified, InstanceDiff{ID: w.ID, Type: w.Type, Change: ChangeReplaced, OldInstance: &oldW, NewInstance: &w, DomainChange: true})
		case !reflect.DeepEqual(oldW.Config, w.Config):
			diff.Modified = append(diff.Modified, InstanceDiff{ID: w.ID, Type: w.Type, Change: ChangeModified, OldInstance: &oldW, NewInstance: &w, DomainChange: true})
		case oldW.Endpoint != w.Endpoint || oldW.Method != w.Method || oldW.TokenEnv != w.TokenEnv || !equalIntPtr(oldW.RefreshIntervalSeconds, w.RefreshIntervalSeconds):
			diff.Modified = append(diff.Modified, InstanceDiff{ID: w.ID, Type: w.Type, Change: ChangeModified, OldInstance: &oldW, NewInstance: &w})
		default:
			diff.Unchanged = append(diff.Unchanged, w)
			if !reflect.DeepEqual(oldW.Dimensions, w.Dimensions) || oldW.Pinned != w.Pinned {
				diff.LayoutModified = append(diff.LayoutModified, w)
			}
		}
	}
	for _, oldW := range oldWidgets {
		if _, exists := newMap[oldW.ID]; !exists {
			diff.Removed = append(diff.Removed, oldW)
		}
	}
	return diff
}

func widgetMap(cfg *Config) (map[string]WidgetConfig, []WidgetConfig) {
	if cfg == nil {
		return nil, nil
	}
	m := make(map[string]WidgetConfig, len(cfg.Display.Widgets))
	for _, w := range cfg.Display.Widgets {
		m[w.ID] = w
	}
	return m, cfg.Display.Widgets
}

func equalIntPtr(a, b *int) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}
