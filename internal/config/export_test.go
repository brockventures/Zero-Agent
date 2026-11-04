package config

import (
	"fmt"
	"os"
)

// Load reads and parses the configuration file from path with system environment resolution.
// If path is empty, DefaultConfigPath is used.
func Load(path string) (*Config, error) {
	return LoadWithEnv(path, os.Getenv)
}

// LoadWithEnv reads and parses the configuration file from path using the supplied environment resolver.
// If path is empty, DefaultConfigPath is used.
func LoadWithEnv(path string, getenv func(string) string) (*Config, error) {
	if path == "" {
		path = DefaultConfigPath
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read configuration file %q: %w", path, err)
	}
	return ParseWithEnv(data, getenv)
}

// Parse parses raw YAML bytes with system environment resolution.
func Parse(data []byte) (*Config, error) {
	return ParseWithEnv(data, os.Getenv)
}

// ParseWithEnv parses raw configuration YAML bytes, applies defaults, validates constraints,
// and resolves secrets through the provided environment lookup function.
func ParseWithEnv(data []byte, getenv func(string) string) (*Config, error) {
	if getenv == nil {
		getenv = os.Getenv
	}
	cfg, err := parseYAMLStage(data)
	if err != nil {
		return nil, err
	}
	if err := validateCoreInstancesStage(cfg, getenv); err != nil {
		return nil, err
	}
	return cfg, nil
}
