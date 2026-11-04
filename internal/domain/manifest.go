package domain

import (
	"fmt"
	"strings"
)

// ManifestRefresh defines default ingestion intervals.
type ManifestRefresh struct {
	IntervalSeconds int `yaml:"interval_seconds,omitempty"`
}

// WidgetManifest represents the declarative metadata, capabilities, and schema declared in manifest.yaml.
type WidgetManifest struct {
	Name                string          `yaml:"name"`
	Version             string          `yaml:"version"`
	Description         string          `yaml:"description,omitempty"`
	Provider            string          `yaml:"provider"`
	Capabilities        []string        `yaml:"capabilities,omitempty"`
	DefaultDimensions   Dimension       `yaml:"default_dimensions,omitempty"`
	SupportedDimensions []Dimension     `yaml:"supported_dimensions,omitempty"`
	Refresh             ManifestRefresh `yaml:"refresh,omitempty"`
	ConfigSchema        map[string]any  `yaml:"config_schema,omitempty"`
	ResponseSchema      map[string]any  `yaml:"response_schema,omitempty"`
}

// Validate checks the semantic correctness of the manifest.
func (m *WidgetManifest) Validate() error {
	if strings.TrimSpace(m.Name) == "" {
		return fmt.Errorf("manifest missing required field 'name'")
	}
	if strings.TrimSpace(m.Version) == "" {
		return fmt.Errorf("manifest '%s' missing required field 'version'", m.Name)
	}
	if strings.TrimSpace(m.Provider) == "" {
		return fmt.Errorf("manifest '%s' missing required field 'provider'", m.Name)
	}

	// Validate default_dimensions only when present
	if m.DefaultDimensions.Cols != 0 || m.DefaultDimensions.Rows != 0 {
		if !m.DefaultDimensions.IsValid() {
			return fmt.Errorf("manifest '%s': default_dimensions [%d, %d] violates 6x2 grid bounds (cols 1..6, rows 1..2)",
				m.Name, m.DefaultDimensions.Cols, m.DefaultDimensions.Rows)
		}
	}

	// Validate supported_dimensions if specified
	for i, dim := range m.SupportedDimensions {
		if !dim.IsValid() {
			return fmt.Errorf("manifest '%s': supported_dimensions[%d] [%d, %d] violates 6x2 grid bounds (cols 1..6, rows 1..2)",
				m.Name, i, dim.Cols, dim.Rows)
		}
	}

	// Forbid 'default' keyword in config_schema default Keyword
	if m.ConfigSchema != nil {
		if err := checkForDisallowedDefaultKeyword(m.ConfigSchema, "config_schema"); err != nil {
			return fmt.Errorf("manifest '%s': %w", m.Name, err)
		}
	}

	// Forbid 'default' keyword in response_schema default Keyword
	if m.ResponseSchema != nil {
		if err := checkForDisallowedDefaultKeyword(m.ResponseSchema, "response_schema"); err != nil {
			return fmt.Errorf("manifest '%s': %w", m.Name, err)
		}
	}

	// Custom provider: http requires response_schema
	if m.Provider == "http" {
		if len(m.ResponseSchema) == 0 {
			return fmt.Errorf("manifest '%s': provider 'http' requires non-empty response_schema", m.Name)
		}
	}

	return nil
}

func isNamedSchemaMapKeyword(k string) bool {
	switch k {
	case "properties", "patternProperties", "$defs", "definitions", "dependentSchemas":
		return true
	default:
		return false
	}
}

func checkNamedSchemaMap(v any, path string) error {
	switch val := v.(type) {
	case map[string]any:
		for k, child := range val {
			childPath := path + "." + k
			if err := checkForDisallowedDefaultKeyword(child, childPath); err != nil {
				return err
			}
		}
	case map[any]any:
		for k, child := range val {
			childPath := fmt.Sprintf("%s.%v", path, k)
			if err := checkForDisallowedDefaultKeyword(child, childPath); err != nil {
				return err
			}
		}
	}
	return nil
}

func checkSchemaMapEntry(k, child any, path string) error {
	kStr := fmt.Sprint(k)
	childPath := path + "." + kStr
	if kStr == "default" {
		return fmt.Errorf("schema at '%s' declares disallowed keyword 'default'; default values in schemas are strictly prohibited", path)
	}
	if isNamedSchemaMapKeyword(kStr) {
		return checkNamedSchemaMap(child, childPath)
	}
	return checkForDisallowedDefaultKeyword(child, childPath)
}

func checkForDisallowedDefaultKeyword(v any, path string) error {
	switch val := v.(type) {
	case map[string]any:
		for k, child := range val {
			if err := checkSchemaMapEntry(k, child, path); err != nil {
				return err
			}
		}
	case map[any]any:
		for k, child := range val {
			if err := checkSchemaMapEntry(k, child, path); err != nil {
				return err
			}
		}
	case []any:
		for i, child := range val {
			childPath := fmt.Sprintf("%s[%d]", path, i)
			if err := checkForDisallowedDefaultKeyword(child, childPath); err != nil {
				return err
			}
		}
	}
	return nil
}
