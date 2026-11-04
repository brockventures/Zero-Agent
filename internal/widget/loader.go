package widget

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"gopkg.in/yaml.v3"
)

const (
	// DefaultBuiltinDir is the standard container location for built-in widgets.
	DefaultBuiltinDir = "/app/widgets"
	// DefaultCustomDir is the standard volume mount location for custom user widgets.
	DefaultCustomDir = "/config/widgets"

	// ManifestFilename is the required widget manifest definition file.
	ManifestFilename = "manifest.yaml"
	// ViewFilename is the required semantic HTML template file.
	ViewFilename = "views/widget.html"
	// AssetsDirname is the optional static assets directory.
	AssetsDirname = "assets"
)

// Loader discovers and loads widget packages from disk.
type Loader struct {
	builtinDir string
	customDir  string
}

// NewLoader constructs a Loader for the given directories.
func NewLoader(builtinDir, customDir string) *Loader {
	return &Loader{builtinDir: builtinDir, customDir: customDir}
}

// LoadPackage resolves a single widget package by type, adhering to whole-package precedence.
func (l *Loader) LoadPackage(widgetType string) (*domain.Package, error) {
	if strings.HasPrefix(widgetType, ".") {
		return nil, fmt.Errorf("widget package '%s' not found", widgetType)
	}

	for _, s := range []struct{ dir, source string }{{l.customDir, "custom"}, {l.builtinDir, "builtin"}} {
		pkgDir := filepath.Join(s.dir, widgetType)
		if fi, err := os.Stat(pkgDir); err == nil && fi.IsDir() {
			return l.loadPackageFromDir(pkgDir, widgetType, s.source)
		}
	}

	return nil, fmt.Errorf("widget package '%s' not found", widgetType)
}

func (l *Loader) loadPackageFromDir(pkgDir, widgetType, source string) (*domain.Package, error) {
	if !CheckPackageCompleteness(pkgDir) {
		return nil, fmt.Errorf("widget package at %s is incomplete: missing required file manifest.yaml (or views/widget.html); overriding a widget type requires a complete package", pkgDir)
	}

	manifestPath := filepath.Join(pkgDir, ManifestFilename)
	viewPath := filepath.Join(pkgDir, ViewFilename)

	var assetsDir string
	if fi, err := os.Stat(filepath.Join(pkgDir, AssetsDirname)); err == nil && fi.IsDir() {
		assetsDir = filepath.Join(pkgDir, AssetsDirname)
	}

	manifestData, err := os.ReadFile(manifestPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read manifest at %q: %w", manifestPath, err)
	}

	var manifest domain.WidgetManifest
	if err := yaml.Unmarshal(manifestData, &manifest); err != nil {
		return nil, fmt.Errorf("manifest syntax error in %q: %w", manifestPath, err)
	}
	if err := manifest.Validate(); err != nil {
		return nil, fmt.Errorf("invalid manifest at %q: %w", manifestPath, err)
	}

	return &domain.Package{
		Type:         widgetType,
		Source:       source,
		Dir:          pkgDir,
		ManifestPath: manifestPath,
		ViewPath:     viewPath,
		AssetsDir:    assetsDir,
		Manifest:     manifest,
	}, nil
}

// CheckPackageCompleteness verifies whether a package directory contains both required files
// (manifest.yaml and views/widget.html).
func CheckPackageCompleteness(pkgDir string) bool {
	_, mErr := os.Stat(filepath.Join(pkgDir, ManifestFilename))
	_, vErr := os.Stat(filepath.Join(pkgDir, ViewFilename))
	return mErr == nil && vErr == nil
}

// ApplyDefaults applies manifest default_dimensions and refresh intervals to widget instances in cfg.
func (l *Loader) ApplyDefaults(cfg *config.Config, registry map[string]*domain.Package) error {
	if cfg == nil {
		return fmt.Errorf("configuration cannot be nil")
	}

	for i := range cfg.Display.Widgets {
		w := &cfg.Display.Widgets[i]
		pkg, exists := registry[w.Type]
		if !exists {
			return fmt.Errorf("widget '%s': unknown widget type '%s'", w.ID, w.Type)
		}

		if len(w.Dimensions) == 0 {
			if !pkg.Manifest.DefaultDimensions.IsValid() {
				return fmt.Errorf("[Mirrormere Config Error] widget '%s' omits 'dimensions' and package manifest '%s' has no valid default_dimensions", w.ID, w.Type)
			}
			w.Dimensions = pkg.Manifest.DefaultDimensions.Slice()
		}

		if w.RefreshIntervalSeconds == nil && pkg.Manifest.Refresh.IntervalSeconds > 0 {
			interval := pkg.Manifest.Refresh.IntervalSeconds
			w.RefreshIntervalSeconds = &interval
		}
	}
	return nil
}
