package domain

// Package represents a discovered, validated widget package on disk.
type Package struct {
	Type         string         `json:"type"`
	Source       string         `json:"source"` // "builtin" or "custom"
	Dir          string         `json:"dir"`
	ManifestPath string         `json:"manifest_path"`
	ViewPath     string         `json:"view_path"`
	AssetsDir    string         `json:"assets_dir,omitempty"`
	Manifest     WidgetManifest `json:"manifest"`
}

// HasAssets returns whether the package supplies an optional assets directory.
func (p *Package) HasAssets() bool {
	return p.AssetsDir != ""
}
