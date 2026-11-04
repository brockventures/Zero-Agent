package widget_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/widget"
)

func createPackageFixture(t *testing.T, baseDir, widgetType, manifest, view string, hasAssets bool) string {
	t.Helper()
	pkgDir := filepath.Join(baseDir, widgetType)
	_ = os.MkdirAll(filepath.Join(pkgDir, "views"), 0o755)
	if manifest != "" {
		_ = os.WriteFile(filepath.Join(pkgDir, widget.ManifestFilename), []byte(manifest), 0o644)
	}
	if view != "" {
		_ = os.WriteFile(filepath.Join(pkgDir, widget.ViewFilename), []byte(view), 0o644)
	}
	if hasAssets {
		_ = os.MkdirAll(filepath.Join(pkgDir, widget.AssetsDirname), 0o755)
		_ = os.WriteFile(filepath.Join(pkgDir, widget.AssetsDirname, "icon.svg"), []byte("<svg></svg>"), 0o644)
	}
	return pkgDir
}

const (
	calManifest     = "name: Cal\nversion: \"1.0.0\"\nprovider: calendar-feed\ndefault_dimensions: [4, 2]\nrefresh:\n  interval_seconds: 300\n"
	weatherManifest = "name: Weather\nversion: \"1.0.0\"\nprovider: weather-forecast\ndefault_dimensions: [2, 1]\nrefresh:\n  interval_seconds: 900\n"
	defaultManifest = "name: T\nversion: \"1.0.0\"\nprovider: t\ndefault_dimensions: [2, 1]\nconfig_schema:\n  type: object\n  properties:\n    theme:\n      type: string\n      default: dark\n"
)

func TestLoader_WholePackageOverride(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	b, c := filepath.Join(tmp, "b"), filepath.Join(tmp, "c")
	createPackageFixture(t, b, "calendar-feed", calManifest, "<div></div>", true)
	createPackageFixture(t, b, "weather-forecast", weatherManifest, "<div></div>", false)
	createPackageFixture(t, c, "calendar-feed", "name: Custom Cal\nversion: \"2.0.0\"\nprovider: calendar-feed\ndefault_dimensions: [4, 2]\nrefresh:\n  interval_seconds: 600\n", "<div>custom</div>", false)
	createPackageFixture(t, c, "sensor-card", "name: Sensor\nversion: \"1.0.0\"\nprovider: http\ndefault_dimensions: [2, 1]\nresponse_schema:\n  type: object\n  required: [value]\n  properties:\n    value: {type: number}\n", "<div>sensor</div>", true)

	loader := widget.NewLoader(b, c)
	cal, err1 := loader.LoadPackage("calendar-feed")
	w, err2 := loader.LoadPackage("weather-forecast")
	s, err3 := loader.LoadPackage("sensor-card")
	if err1 != nil || err2 != nil || err3 != nil || cal == nil || cal.Source != "custom" || cal.Manifest.Version != "2.0.0" || cal.HasAssets() || w == nil || w.Source != "builtin" || s == nil || s.Source != "custom" || !s.HasAssets() {
		t.Fatalf("unexpected LoadPackage(): err1=%v err2=%v err3=%v", err1, err2, err3)
	}
}

func TestLoader_LoadPackageErrorsAndEdgeCases(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, pkgName, manifest, view, wantErr string
		inCustom                               bool
	}{
		{"missing_manifest", "bad", "", "<div></div>", "missing required file manifest.yaml", true},
		{"missing_view", "bad", weatherManifest, "", "missing required file manifest.yaml (or views/widget.html)", true},
		{"yaml_syntax_error", "bad-yaml", "name: [unclosed", "<div></div>", "manifest syntax error", false},
		{"disallowed_default", "has-default", defaultManifest, "<div></div>", "declares disallowed keyword 'default'", false},
		{"hidden_dirs_skipped", ".git", "", "", "not found", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			b, c := filepath.Join(tmp, "b"), filepath.Join(tmp, "c")
			_ = os.MkdirAll(b, 0o755)
			_ = os.MkdirAll(c, 0o755)
			if tc.pkgName == ".git" {
				_ = os.MkdirAll(filepath.Join(c, ".git"), 0o755)
			} else {
				dir := b
				if tc.inCustom {
					dir = c
				}
				createPackageFixture(t, dir, tc.pkgName, tc.manifest, tc.view, false)
			}
			loader := widget.NewLoader(b, c)
			pkg, err := loader.LoadPackage(tc.pkgName)
			if (err != nil) != (tc.wantErr != "") || (tc.wantErr != "" && !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("LoadPackage(%q) = (%v, %v), wantErr=%q", tc.pkgName, pkg, err, tc.wantErr)
			}
		})
	}
}

func TestLoader_LoadPackage(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	b, c := filepath.Join(tmp, "b"), filepath.Join(tmp, "c")
	createPackageFixture(t, b, "builtin-w", calManifest, "<div></div>", false)
	createPackageFixture(t, c, "custom-w", weatherManifest, "<div></div>", false)

	loader := widget.NewLoader(b, c)
	for _, tc := range []struct{ name, pkgType, wantSource, wantErr string }{
		{"custom", "custom-w", "custom", ""},
		{"builtin", "builtin-w", "builtin", ""},
		{"missing", "non-existent", "", "not found"},
		{"hidden", ".hidden", "", "not found"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			pkg, err := loader.LoadPackage(tc.pkgType)
			if (err != nil) != (tc.wantErr != "") || (tc.wantErr != "" && !strings.Contains(err.Error(), tc.wantErr)) || (err == nil && pkg.Source != tc.wantSource) {
				t.Fatalf("LoadPackage(%q) = (%v, %v), want source=%q, err=%q", tc.pkgType, pkg, err, tc.wantSource, tc.wantErr)
			}
		})
	}
}

func TestLoader_ApplyDefaults(t *testing.T) {
	t.Parallel()
	mkPkg := func(name string, c, r, ref int) *domain.Package {
		return &domain.Package{Type: name, Manifest: domain.WidgetManifest{Name: name, DefaultDimensions: domain.NewDimension(c, r), Refresh: domain.ManifestRefresh{IntervalSeconds: ref}}}
	}
	reg := map[string]*domain.Package{"cal": mkPkg("C", 4, 2, 300), "weather": mkPkg("W", 2, 1, 900), "corrupt": {Type: "corrupt"}}
	ci := 120
	wCal, wWea := config.WidgetConfig{ID: "w1", Type: "cal"}, config.WidgetConfig{ID: "w2", Type: "weather", Dimensions: []int{3, 2}, RefreshIntervalSeconds: &ci}
	wCfg := func(ws ...config.WidgetConfig) *config.Config {
		return &config.Config{Display: config.DisplayConfig{Widgets: ws}}
	}
	for _, tc := range []struct {
		name, wantErr string
		cfg           *config.Config
		checkDims     [2]int
		checkRef      int
	}{
		{"success", "", wCfg(wCal, wWea), [2]int{4, 2}, 300},
		{"nil", "configuration cannot be nil", nil, [2]int{}, 0},
		{"unknown", "unknown widget type", wCfg(config.WidgetConfig{Type: "u"}), [2]int{}, 0},
		{"corrupt", "has no valid default_dimensions", wCfg(config.WidgetConfig{Type: "corrupt"}), [2]int{}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := widget.NewLoader("", "").ApplyDefaults(tc.cfg, reg)
			if (err != nil) != (tc.wantErr != "") || (tc.wantErr != "" && !strings.Contains(err.Error(), tc.wantErr)) {
				t.Fatalf("err = %v, want %q", err, tc.wantErr)
			}
			if tc.wantErr == "" {
				w1, w2 := tc.cfg.Display.Widgets[0], tc.cfg.Display.Widgets[1]
				if w1.Dimensions[0] != tc.checkDims[0] || w1.Dimensions[1] != tc.checkDims[1] || *w1.RefreshIntervalSeconds != tc.checkRef ||
					w2.Dimensions[0] != 3 || w2.Dimensions[1] != 2 || *w2.RefreshIntervalSeconds != 120 {
					t.Fatalf("widgets mismatch: w1=%+v, w2=%+v", w1, w2)
				}
			}
		})
	}
}

func TestCheckPackageCompleteness(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	for _, tc := range []struct {
		name, manifest, view string
		want                 bool
	}{
		{"complete", "name: test", "<div></div>", true},
		{"no_manifest", "", "<div></div>", false},
		{"no_view", "name: test", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if widget.CheckPackageCompleteness(createPackageFixture(t, tmp, tc.name, tc.manifest, tc.view, false)) != tc.want {
				t.Fatalf("%s failed", tc.name)
			}
		})
	}
}

func TestBuiltinPackagesAndAliases(t *testing.T) {
	t.Parallel()
	loader := widget.NewLoader(filepath.Join("..", "..", "widgets"), t.TempDir())
	for _, tc := range []struct {
		name, wantType, wantProv     string
		wantCols, wantRows, suppDims int
		hasConf                      bool
	}{
		{"spacer", "spacer", "spacer", 1, 1, 24, false},
		{"weather-forecast", "weather-forecast", "weather-forecast", 2, 1, 0, false},
		{"live-view", "live-view", "live-view", 3, 2, 0, true},
		{"calendar-feed", "calendar-feed", "calendar-feed", 4, 2, 0, false},
		{"calendar-grid", "calendar-grid", "calendar-feed", 6, 2, 0, false},
		{"photo-carousel", "photo-carousel", "photo-carousel", 3, 2, 16, true},
		{"web-embed", "web-embed", "spacer", 2, 2, 13, true},
		{"plex-recent", "plex-recent", "spacer", 3, 2, 10, true},
		{"yt-music", "yt-music", "spacer", 3, 2, 10, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			pkg, err := loader.LoadPackage(tc.name)
			if err != nil || pkg.Type != tc.wantType || pkg.Manifest.Provider != tc.wantProv ||
				pkg.Manifest.DefaultDimensions.Cols != tc.wantCols || pkg.Manifest.DefaultDimensions.Rows != tc.wantRows ||
				(tc.suppDims > 0 && len(pkg.Manifest.SupportedDimensions) != tc.suppDims) ||
				(tc.hasConf && pkg.Manifest.ConfigSchema == nil) || pkg.Manifest.Validate() != nil {
				t.Fatalf("LoadPackage(%q) = (%+v, %v)", tc.name, pkg, err)
			}
		})
	}
}

// The legacy-test fixture is a custom package with only a legacy HTML template and a stylesheet,
// the shape the React UI renders through LegacyWidget.
func TestLoader_LegacyTestFixture(t *testing.T) {
	pkg, err := widget.NewLoader("", "testdata").LoadPackage("legacy-test")
	if err != nil {
		t.Fatalf("LoadPackage(legacy-test): %v", err)
	}
	if pkg.Source != "custom" {
		t.Errorf("Source = %q, want custom", pkg.Source)
	}
	if !pkg.HasAssets() {
		t.Error("legacy-test fixture should expose an assets directory")
	}
}

func TestLoader_PhotoCarouselProductionConfigValidation(t *testing.T) {
	t.Parallel()
	loader := widget.NewLoader(filepath.Join("..", "..", "widgets"), t.TempDir())
	yamlData := `
timezone: America/Los_Angeles
display:
  widgets:
    - id: photo-carousel
      type: photo-carousel
      dimensions: [3, 2]
      refresh_interval_seconds: 60
      config:
        share_url: "https://photos.app.goo.gl/example-album"
        cycle_interval_seconds: 30
        shuffle: true
    - id: pad
      type: spacer
      dimensions: [3, 2]
`
	snap, err := config.ValidatePipeline([]byte(yamlData), loader, nil)
	if err != nil {
		t.Fatalf("expected production photo-carousel configuration to validate: %v", err)
	}
	if snap == nil || len(snap.Config.Display.Widgets) != 2 {
		t.Fatalf("unexpected snapshot: %+v", snap)
	}
}
