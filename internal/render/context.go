package render

import (
	"strings"

	"github.com/azylman/mirrormere/internal/domain"
)

// Context encapsulates the full unified execution context exposed to widget HTML templates.
// Conforms strictly to  and .A.
type Context struct {
	ID         string           `json:"id"`
	Type       string           `json:"type"`
	Dimensions domain.Dimension `json:"dimensions"`
	Data       any              `json:"data"`
	State      string           `json:"state"`
	Timestamp  string           `json:"timestamp"`
	Config     map[string]any   `json:"config"`
	Origin     [2]int           `json:"origin"`
	Theme      string           `json:"theme"`
	Online     bool             `json:"online"`
	Assets     string           `json:"assets"`
}

// exactSensitiveKeys defines exact key names (case-insensitive) of credentials that must be omitted from template context.
var exactSensitiveKeys = map[string]struct{}{
	"token":        {},
	"secret":       {},
	"password":     {},
	"api_key":      {},
	"apikey":       {},
	"auth":         {},
	"auth_token":   {},
	"access_token": {},
}

// SanitizeConfig performs a deep clone of the widget instance custom configuration,
// omitting keys that end with "_env" (environment variable pointers) or match exact
// credential key names. Ordinary domain keys (e.g. "author", "show_passed", "bypass_cache",
// "compass", "max_tokens") are strictly preserved.
func SanitizeConfig(cfg map[string]any) map[string]any {
	if cfg == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(cfg))
	for k, v := range cfg {
		if isSensitiveKey(k) {
			continue
		}
		out[k] = sanitizeValue(v)
	}
	return out
}

func isSensitiveKey(key string) bool {
	lower := strings.ToLower(strings.TrimSpace(key))
	if strings.HasSuffix(lower, "_env") || lower == "_env" {
		return true
	}
	_, exact := exactSensitiveKeys[lower]
	return exact
}

func sanitizeValue(v any) any {
	switch val := v.(type) {
	case map[string]any:
		return SanitizeConfig(val)
	case []any:
		clone := make([]any, len(val))
		for i, item := range val {
			clone[i] = sanitizeValue(item)
		}
		return clone
	default:
		return val
	}
}
