package config

import (
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"time"

	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/layout"
	"gopkg.in/yaml.v3"
)

// ErrLayoutEditDisabled indicates that live canvas layout modifications are forbidden by configuration.
var (
	ErrLayoutEditDisabled = errors.New("layout editing is disabled by configuration")
	ErrOverrideNotFound   = errors.New("layout override file not found")
)

// WidgetOverride specifies an explicit coordinate and dimension override for an existing widget.
type WidgetOverride struct {
	ID         string `yaml:"id" json:"widget_id"`
	Screen     int    `yaml:"screen" json:"screen"`
	Origin     [2]int `yaml:"origin" json:"origin"`
	Dimensions []int  `yaml:"dimensions" json:"dimensions"`
	Pinned     bool   `yaml:"pinned,omitempty" json:"pinned,omitempty"`
}

// LayoutOverrideFile defines the serialized structure of the atomic layout override file.
type LayoutOverrideFile struct {
	Version int              `yaml:"version" json:"version"`
	Widgets []WidgetOverride `yaml:"widgets" json:"widgets"`
}

// ManagerOption configures an LKGC Manager instance.
type ManagerOption func(*Manager)

// WithOverridePath forces a specific filesystem path for the layout override file.
func WithOverridePath(path string) ManagerOption {
	return func(m *Manager) {
		m.overridePath = path
	}
}

// WithUseConfigOverridePath enables resolving the override path from the display configuration.
func WithUseConfigOverridePath() ManagerOption {
	return func(m *Manager) {
		m.useConfigOverridePath = true
	}
}

func (m *Manager) resolveOverridePath(snap *Snapshot) string {
	if m.overridePath != "" {
		return m.overridePath
	}
	if m.useConfigOverridePath && snap != nil && snap.Config != nil {
		return snap.Config.Display.GetLayoutOverridePath()
	}
	return ""
}

// LoadLayoutOverride reads and parses the layout override YAML file at path.
func LoadLayoutOverride(path string) (*LayoutOverrideFile, error) {
	if path == "" {
		return nil, ErrOverrideNotFound
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, ErrOverrideNotFound
		}
		return nil, fmt.Errorf("failed to read layout override %s: %w", path, err)
	}
	var file LayoutOverrideFile
	if err := yaml.Unmarshal(data, &file); err != nil {
		return nil, fmt.Errorf("failed to parse layout override %s: %w", path, err)
	}
	if file.Version <= 0 {
		file.Version = 1
	}
	return &file, nil
}

func closeFile(f *os.File) {
	if err := f.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
		_ = err
	}
}

func removeTempFile(path string) {
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		_ = err
	}
}

// SaveLayoutOverrideAtomic writes ov to path atomically using an ephemeral sibling temporary file.
func SaveLayoutOverrideAtomic(path string, ov *LayoutOverrideFile) error {
	if path == "" {
		return errors.New("layout override path cannot be empty")
	}
	if ov == nil {
		return errors.New("layout override cannot be nil")
	}
	if ov.Version <= 0 {
		ov.Version = 1
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("failed to create directory %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, ".layout-override-*.tmp")
	if err != nil {
		return fmt.Errorf("failed to create temp override file: %w", err)
	}
	tmpPath := tmp.Name()
	success := false
	defer func() {
		closeFile(tmp)
		if !success {
			removeTempFile(tmpPath)
		}
	}()

	data, err := yaml.Marshal(ov)
	if err != nil {
		return fmt.Errorf("failed to marshal layout override: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		return fmt.Errorf("failed to write layout override temp file: %w", err)
	}
	if err := tmp.Chmod(0o644); err != nil {
		return fmt.Errorf("failed to chmod layout override temp file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return fmt.Errorf("failed to sync layout override temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("failed to close layout override temp file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("failed to replace layout override %s: %w", path, err)
	}
	success = true
	return nil
}

// RemoveLayoutOverride deletes the layout override file at path if present.
func RemoveLayoutOverride(path string) error {
	if path == "" {
		return nil
	}
	err := os.Remove(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("failed to remove layout override %s: %w", path, err)
	}
	return nil
}

func findSlotOnScreen(sMask uint32, dim domain.Dimension, gridCols, gridRows int) ([2]int, bool) {
	for r := 0; r <= gridRows-dim.Rows; r++ {
		for c := 0; c <= gridCols-dim.Cols; c++ {
			mask := layout.ComputeWidgetBitmaskForGrid(dim.Cols, dim.Rows, c, r, gridCols)
			if (sMask & mask) == 0 {
				return [2]int{c, r}, true
			}
		}
	}
	return [2]int{}, false
}

func findFirstOpenSlot(screenMasks []uint32, pinnedMask uint32, pinned bool, dim domain.Dimension, gridCols, gridRows int) (int, [2]int, bool) {
	if pinned {
		combined := pinnedMask
		for _, sMask := range screenMasks {
			combined |= sMask
		}
		origin, ok := findSlotOnScreen(combined, dim, gridCols, gridRows)
		return 0, origin, ok
	}
	for s, sMask := range screenMasks {
		if origin, ok := findSlotOnScreen(sMask|pinnedMask, dim, gridCols, gridRows); ok {
			return s, origin, true
		}
	}
	if len(screenMasks) < 16 {
		if origin, ok := findSlotOnScreen(pinnedMask, dim, gridCols, gridRows); ok {
			return len(screenMasks), origin, true
		}
	}
	return 0, [2]int{}, false
}

func trackScreenMask(masks []uint32, screen int, origin [2]int, dim domain.Dimension, gridCols int, pinned bool, pinnedMask *uint32) []uint32 {
	for len(masks) <= screen {
		masks = append(masks, *pinnedMask)
	}
	mask := layout.ComputeWidgetBitmaskForGrid(dim.Cols, dim.Rows, origin[0], origin[1], gridCols)
	if pinned {
		*pinnedMask |= mask
		for i := range masks {
			masks[i] |= mask
		}
		return masks
	}
	masks[screen] |= mask | *pinnedMask
	return masks
}

func validatePlacementBounds(id string, screen int, origin [2]int, dim domain.Dimension, gridCols, gridRows int) error {
	if !dim.IsValidForGrid(gridCols, gridRows) {
		return fmt.Errorf("invalid dimensions %v for widget %q on %dx%d grid", dim, id, gridCols, gridRows)
	}
	if screen < 0 || screen >= 16 {
		return fmt.Errorf("screen index %d out of bounds [0..15] for widget %q", screen, id)
	}
	c, r := origin[0], origin[1]
	if c < 0 || c+dim.Cols > gridCols || r < 0 || r+dim.Rows > gridRows {
		return fmt.Errorf("widget %q origin [%d,%d] with dimensions [%d,%d] exceeds %dx%d grid bounds", id, c, r, dim.Cols, dim.Rows, gridCols, gridRows)
	}
	return nil
}

func validateOverrideDimension(w *WidgetConfig, pkgs map[string]*domain.Package, loader PackageLoader) error {
	var pkg *domain.Package
	if pkgs != nil {
		pkg = pkgs[w.Type]
	}
	if pkg == nil && loader != nil {
		if p, err := loader.LoadPackage(w.Type); err == nil {
			pkg = p
		}
	}
	if pkg == nil {
		return nil
	}
	return validateWidgetSupportedDimensions(w, pkg)
}

func buildPlacementsFromOverrides(widgets []WidgetConfig, ovMap map[string]WidgetOverride, pkgs map[string]*domain.Package, loader PackageLoader, gridCols, gridRows int) ([]layout.ExplicitPlacement, []WidgetConfig, error) {
	placements := make([]layout.ExplicitPlacement, 0, len(widgets))
	updatedWidgets := make([]WidgetConfig, 0, len(widgets))
	screenMasks := make([]uint32, 1)
	var pinnedMask uint32

	for _, w := range widgets {
		ov, ok := ovMap[w.ID]
		if !ok {
			continue
		}
		dimSlice := ov.Dimensions
		if len(dimSlice) == 0 {
			dimSlice = w.Dimensions
		}
		dim, err := domain.DimensionFromSlice(dimSlice)
		if err != nil {
			return nil, nil, err
		}
		if err := validatePlacementBounds(w.ID, ov.Screen, ov.Origin, dim, gridCols, gridRows); err != nil {
			return nil, nil, err
		}
		wUpdated := w
		wUpdated.Dimensions = dimSlice
		wUpdated.Pinned = ov.Pinned
		if err := validateOverrideDimension(&wUpdated, pkgs, loader); err != nil {
			return nil, nil, err
		}
		updatedWidgets = append(updatedWidgets, wUpdated)
		placements = append(placements, layout.ExplicitPlacement{
			ID:         w.ID,
			Type:       w.Type,
			Screen:     ov.Screen,
			Origin:     ov.Origin,
			Dimensions: dim,
			Pinned:     ov.Pinned,
		})
		screenMasks = trackScreenMask(screenMasks, ov.Screen, ov.Origin, dim, gridCols, ov.Pinned, &pinnedMask)
	}

	for _, w := range widgets {
		if _, ok := ovMap[w.ID]; ok || w.Type == "spacer" {
			continue
		}
		dim, err := domain.DimensionFromSlice(w.Dimensions)
		if err != nil {
			return nil, nil, err
		}
		s, origin, ok := findFirstOpenSlot(screenMasks, pinnedMask, w.Pinned, dim, gridCols, gridRows)
		if !ok {
			return nil, nil, fmt.Errorf("no open slot found for unplaced widget %s", w.ID)
		}
		updatedWidgets = append(updatedWidgets, w)
		placements = append(placements, layout.ExplicitPlacement{
			ID:         w.ID,
			Type:       w.Type,
			Screen:     s,
			Origin:     origin,
			Dimensions: dim,
			Pinned:     w.Pinned,
		})
		screenMasks = trackScreenMask(screenMasks, s, origin, dim, gridCols, w.Pinned, &pinnedMask)
	}
	return placements, updatedWidgets, nil
}

func appendAutoSpacers(widgets []WidgetConfig, spacers []layout.PlacedWidget) []WidgetConfig {
	for _, sp := range spacers {
		widgets = append(widgets, WidgetConfig{
			ID:         sp.WidgetID,
			Type:       "spacer",
			Dimensions: sp.Dimensions.Slice(),
		})
	}
	return widgets
}

func ensureSpacerPackage(pkgs map[string]*domain.Package, loader PackageLoader) map[string]*domain.Package {
	if loader == nil {
		return pkgs
	}
	if _, ok := pkgs["spacer"]; ok {
		return pkgs
	}
	spacerPkg, err := loader.LoadPackage("spacer")
	if err != nil {
		return pkgs
	}
	pkgsCopy := make(map[string]*domain.Package, len(pkgs)+1)
	maps.Copy(pkgsCopy, pkgs)
	pkgsCopy["spacer"] = spacerPkg
	return pkgsCopy
}

func applyOverrideToSnapshot(baseSnap *Snapshot, ov *LayoutOverrideFile, loader PackageLoader) (*Snapshot, error) {
	gridCols, gridRows := layout.NormalizeGrid(baseSnap.Config.Display.Grid.Columns, baseSnap.Config.Display.Grid.Rows)
	ovMap := make(map[string]WidgetOverride, len(ov.Widgets))
	for _, w := range ov.Widgets {
		ovMap[w.ID] = w
	}
	placements, updatedWidgets, err := buildPlacementsFromOverrides(baseSnap.Config.Display.Widgets, ovMap, baseSnap.Packages, loader, gridCols, gridRows)
	if err != nil {
		return nil, err
	}
	solvedLayout, autoSpacers, err := layout.SolveExplicit(placements, gridCols, gridRows)
	if err != nil {
		return nil, err
	}
	updatedWidgets = appendAutoSpacers(updatedWidgets, autoSpacers)
	pkgs := baseSnap.Packages
	if len(autoSpacers) > 0 {
		pkgs = ensureSpacerPackage(pkgs, loader)
	}
	cfgCopy := *baseSnap.Config
	cfgCopy.Display.Widgets = updatedWidgets
	return &Snapshot{
		Config:      &cfgCopy,
		Layout:      solvedLayout,
		Packages:    pkgs,
		LoadedAt:    time.Now(),
		HasOverride: true,
	}, nil
}

// ApplyLayoutOverride validates and persists an atomic layout override file and applies it to the active configuration.
func (m *Manager) ApplyLayoutOverride(ov *LayoutOverrideFile) (*Snapshot, *ConfigDiff, error) {
	m.reloadMu.Lock()
	defer m.reloadMu.Unlock()

	cur := m.current.Load()
	if cur == nil || !cur.Config.Display.IsLayoutEditAllowed() {
		return nil, nil, ErrLayoutEditDisabled
	}
	if ov == nil {
		return nil, nil, errors.New("layout override cannot be nil")
	}
	path := m.resolveOverridePath(cur)
	if path == "" {
		return nil, nil, errors.New("layout override path not configured")
	}

	baseSnap, err := ValidatePipeline(m.baseYAML, m.loader, m.getenv)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to validate base configuration: %w", err)
	}
	newSnap, err := applyOverrideToSnapshot(baseSnap, ov, m.loader)
	if err != nil {
		return nil, nil, err
	}

	if err := SaveLayoutOverrideAtomic(path, ov); err != nil {
		return nil, nil, fmt.Errorf("failed to save layout override: %w", err)
	}

	diff := DiffConfigs(cur.Config, newSnap.Config)
	m.current.Store(newSnap)
	m.updateStatusLocked()
	return newSnap, diff, nil
}

// ResetLayoutOverride removes the persistent layout override file and reverts the active configuration to the base snapshot.
func (m *Manager) ResetLayoutOverride() (*Snapshot, *ConfigDiff, error) {
	m.reloadMu.Lock()
	defer m.reloadMu.Unlock()

	cur := m.current.Load()
	if cur == nil || !cur.Config.Display.IsLayoutEditAllowed() {
		return nil, nil, ErrLayoutEditDisabled
	}
	path := m.resolveOverridePath(cur)
	if path != "" {
		if err := RemoveLayoutOverride(path); err != nil {
			return nil, nil, fmt.Errorf("failed to remove layout override file: %w", err)
		}
	}

	baseSnap, err := ValidatePipeline(m.baseYAML, m.loader, m.getenv)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to validate base configuration: %w", err)
	}

	diff := DiffConfigs(cur.Config, baseSnap.Config)
	m.current.Store(baseSnap)
	m.updateStatusLocked()
	return baseSnap, diff, nil
}
