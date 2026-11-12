package config_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
)

func assertEq[T comparable](t *testing.T, label string, got, want T) {
	t.Helper()
	if got != want {
		t.Errorf("%s = %v, want %v", label, got, want)
	}
}

func mockGetenv(vars map[string]string) func(string) string {
	return func(k string) string {
		if vars == nil {
			return ""
		}
		return vars[k]
	}
}

func withBaseYAML(extra string) string {
	return "timezone: America/Los_Angeles\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n" + extra
}

func mkWidgetConfig(tokenEnv string, cfg map[string]any) *config.Config {
	return &config.Config{
		Timezone: "UTC",
		Display: config.DisplayConfig{
			Grid:    config.GridConfig{Columns: 6, Rows: 2},
			Widgets: []config.WidgetConfig{{ID: "w1", Type: "custom", TokenEnv: tokenEnv, Config: cfg}},
		},
	}
}

const (
	locationPropagateYAML      = "timezone: America/Los_Angeles\nlocation:\n  name: Oakland Hills\n  latitude: 37.8044\n  longitude: -122.2712\ndisplay:\n  theme:\n    mode: auto\n  header:\n    weather:\n      units: imperial\n  widgets:\n    - id: forecast-1\n      type: weather-forecast\n"
	legacyWeatherPropagateYAML = "timezone: America/Los_Angeles\ndisplay:\n  theme:\n    mode: auto\n  header:\n    weather:\n      location: Oakland\n      latitude: 37.8\n      longitude: -122.2\n  widgets:\n    - id: w1\n      type: spacer\n"
)

func TestLoad_ExampleConfig(t *testing.T) {
	t.Parallel()
	examplePath := filepath.Join("..", "..", "deploy", "examples", "config.yaml")
	if _, err := os.Stat(examplePath); err != nil {
		altPath := "/workspace/mirrormere/deploy/examples/config.yaml"
		if _, altErr := os.Stat(altPath); altErr == nil {
			examplePath = altPath
		}
	}

	_, err := config.LoadWithEnv(examplePath, mockGetenv(nil))
	if err == nil || !strings.Contains(err.Error(), "FAMILY_CALENDAR_URL") {
		t.Fatalf("expected error mentioning FAMILY_CALENDAR_URL, got: %v", err)
	}

	mockEnv := map[string]string{
		"FAMILY_CALENDAR_URL":     "https://calendar.google.com/calendar/ical/example%40gmail.com/private-token/basic.ics",
		"TODOIST_API_TOKEN":       "todoist-test-token",
		"GOOGLE_PHOTOS_ALBUM_URL": "https://photos.app.goo.gl/example-album",
	}
	cfg, err := config.LoadWithEnv(examplePath, mockGetenv(mockEnv))
	if err != nil {
		t.Fatalf("unexpected error loading example config: %v", err)
	}

	assertEq(t, "timezone", cfg.Timezone, "America/Los_Angeles")
	assertEq(t, "location", cfg.Location().String(), "America/Los_Angeles")
	assertEq(t, "rotation interval", cfg.Display.Rotation.GetIntervalSeconds(), 30)
	assertEq(t, "transition", cfg.Display.Rotation.Transition, "slide")
	assertEq(t, "pause_duration", cfg.Display.Rotation.GetPauseDurationSeconds(), 120)
	assertEq(t, "header enabled", cfg.Display.Header.IsEnabled(), true)
	assertEq(t, "elements len", len(cfg.Display.Header.Elements), 4)
	if cfg.Display.Header.Weather == nil {
		t.Fatal("expected header weather to be configured")
	}
	assertEq(t, "weather interval", cfg.Display.Header.Weather.GetRefreshIntervalSeconds(), 900)
	assertEq(t, "weather units", cfg.Display.Header.Weather.Units, "imperial")
	assertEq(t, "widgets len", len(cfg.Display.Widgets), 5)

	cal := cfg.Display.Widgets[0]
	if cal.ID != "family-calendar" || cal.Type != "calendar-feed" {
		t.Errorf("unexpected first widget: %+v", cal)
	}
	cals, _ := cal.Config["calendars"].([]any)
	firstCal, _ := cals[0].(map[string]any)
	if _, exists := firstCal["url"]; exists {
		t.Errorf("expected secret key 'url' NOT to exist in Config map, got %v", firstCal["url"])
	}
	assertEq(t, "secret url", cal.Secrets["calendars[0].url"], mockEnv["FAMILY_CALENDAR_URL"])

	photos := cfg.Display.Widgets[3]
	if photos.ID != "family-photos" || photos.Type != "photo-carousel" {
		t.Errorf("unexpected photos widget: %+v", photos)
	}
	assertEq(t, "secret album_url", photos.Secrets["albums[0].url"], mockEnv["GOOGLE_PHOTOS_ALBUM_URL"])
}

func TestParseWithEnv_ValidationErrors(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name, yaml, wantErr string
	}{
		{"empty input", "", "configuration file is empty"},
		{"whitespace only", "   \n\t  \n", "configuration file is empty"},
		{"comments only", "# Just a comment\n# Another comment\n", "configuration file is empty"},
		{"syntax error", "display: [unclosed", "YAML syntax error"},
		{"root not mapping", "- item 1\n- item 2\n", "configuration root must be a YAML mapping"},
		{"decode error", "timezone: UTC\ndisplay:\n  rotation: \"scalar-not-mapping\"\n  widgets:\n    - id: w1\n      type: spacer\n", "failed to decode configuration"},
		{"interpolation prohibited", "timezone: ${TZ}\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n", "arbitrary '${...}' string interpolation is prohibited"},
		{"disallowed screens top-level", "screens:\n  - id: s1\n" + withBaseYAML(""), "disallowed top-level key 'screens'"},
		{"disallowed providers top-level", "providers:\n  p1: {}\n" + withBaseYAML(""), "disallowed top-level key 'providers'"},
		{"disallowed header top-level", "header:\n  enabled: true\n" + withBaseYAML(""), "disallowed top-level key 'header'"},
		{"unknown top-level key", "bogus_key: 123\n" + withBaseYAML(""), "unknown or disallowed top-level key 'bogus_key'"},
		{"disallowed screens under display", "timezone: UTC\ndisplay:\n  screens: []\n  widgets:\n    - id: w1\n      type: spacer\n", "display contains disallowed key 'screens'"},
		{"disallowed providers under display", "timezone: UTC\ndisplay:\n  providers: {}\n  widgets:\n    - id: w1\n      type: spacer\n", "display contains disallowed key 'providers'"},
		{"unknown display key", "timezone: UTC\ndisplay:\n  bogus_display: 1\n  widgets:\n    - id: w1\n      type: spacer\n", "unknown key 'bogus_display' under display"},
		{"missing timezone", "display:\n  widgets:\n    - id: w1\n      type: spacer\n", "missing required configuration field 'timezone'"},
		{"invalid timezone", "timezone: Invalid/Zone_Name\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n", "invalid timezone 'Invalid/Zone_Name'"},
		{"grid columns invalid", withBaseYAML("  grid:\n    columns: 5\n    rows: 2\n"), "grid dimensions must be 6x2 or 3x6"},
		{"grid rows invalid", withBaseYAML("  grid:\n    columns: 6\n    rows: 4\n"), "grid dimensions must be 6x2 or 3x6"},
		{"grid arbitrary 3x4 invalid", withBaseYAML("  grid:\n    columns: 3\n    rows: 4\n"), "grid dimensions must be 6x2 or 3x6"},
		{"negative rotation interval", withBaseYAML("  rotation:\n    interval_seconds: -5\n"), "rotation interval_seconds must be non-negative"},
		{"negative rotation pause", withBaseYAML("  rotation:\n    pause_duration_seconds: -1\n"), "rotation pause_duration_seconds must be non-negative"},
		{"invalid rotation transition", withBaseYAML("  rotation:\n    transition: zoom\n"), "invalid rotation transition 'zoom'"},
		{"location lat out of bounds", withBaseYAML("location:\n  latitude: 95.0\n  longitude: -122.2\n"), "location latitude must be between -90 and 90"},
		{"location lon out of bounds", withBaseYAML("location:\n  latitude: 37.8\n  longitude: -195.0\n"), "location longitude must be between -180 and 180"},
		{"theme invalid mode", withBaseYAML("  theme:\n    mode: cyberpunk\n"), "invalid theme mode 'cyberpunk'"},
		{"theme auto without coordinates", withBaseYAML("  theme:\n    mode: auto\n"), "theme mode 'auto' requires location coordinates"},
		{"header weather non-positive interval", withBaseYAML("  header:\n    weather:\n      refresh_interval_seconds: 0\n      latitude: 37.0\n      longitude: -122.0\n      units: imperial\n"), "header weather refresh_interval_seconds must be positive"},
		{"voice unknown key", withBaseYAML("  voice:\n    wave:\n      enabled: true\n"), "unknown key 'wave' under display.voice"},
		{"voice waveform unknown key", withBaseYAML("  voice:\n    waveform:\n      levles_url: \"http://127.0.0.1:8765/levels\"\n"), "unknown key 'levles_url' under display.voice.waveform"},
		{"voice waveform bad levels url", withBaseYAML("  voice:\n    waveform:\n      levels_url: \"ftp://127.0.0.1/levels\"\n"), "display voice waveform levels_url must be an http(s) URL"},
		{"voice waveform non-positive gain", withBaseYAML("  voice:\n    waveform:\n      gain: 0\n"), "display voice waveform gain must be positive"},
		{"header weather lat out of bounds", "location:\n  latitude: 0\n  longitude: 0\n" + withBaseYAML("  header:\n    weather:\n      latitude: -95.0\n      longitude: -122.0\n      units: imperial\n"), "header weather latitude must be between -90 and 90"},
		{"header weather lon out of bounds", "location:\n  latitude: 0\n  longitude: 0\n" + withBaseYAML("  header:\n    weather:\n      latitude: 37.0\n      longitude: 200.0\n      units: imperial\n"), "header weather longitude must be between -180 and 180"},
		{"header weather invalid units", withBaseYAML("  header:\n    weather:\n      latitude: 37.0\n      longitude: -122.0\n      units: kelvin\n"), "invalid header weather units 'kelvin'"},
		{"widget missing id", "timezone: UTC\ndisplay:\n  widgets:\n    - type: spacer\n", "missing required field 'id'"},
		{"widget duplicate id", "timezone: UTC\ndisplay:\n  widgets:\n    - id: dup\n      type: spacer\n    - id: dup\n      type: spacer\n", "duplicate widget id 'dup'"},
		{"widget missing type", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n    - id: w2\n", "widget 'w2' is missing required field 'type'"},
		{"widget invalid dimensions count", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [1, 2, 3]\n", "dimensions must contain exactly 2 integers"},
		{"widget dimensions cols out of range", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [7, 1]\n", "cols must be 1..6, rows must be 1..2"},
		{"widget dimensions rows out of range", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [1, 3]\n", "cols must be 1..6, rows must be 1..2"},
		{"widget dimensions out of range for 3x6 grid", "timezone: UTC\ndisplay:\n  grid:\n    columns: 3\n    rows: 6\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [4, 2]\n", "cols must be 1..3, rows must be 1..6"},
		{"widget dimensions zero", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [0, 1]\n", "cols must be 1..6, rows must be 1..2"},
		{"widget non-positive refresh", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      refresh_interval_seconds: 0\n", "refresh_interval_seconds must be positive"},
		{"widget invalid http method", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      method: DELETE\n", "invalid http method 'DELETE'"},
		{"widget invalid endpoint url", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      endpoint: ftp://example.com/api\n", "invalid endpoint URL 'ftp://example.com/api'"},
		{"widget whitespace padded endpoint url", "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      endpoint: \" http://example.com/api \"\n", "invalid endpoint URL"},
		{"chat_log non-positive ttl", withBaseYAML("chat_log:\n  ttl_minutes: 0\n"), "chat_log ttl_minutes must be positive"},
		{"chat_log non-positive max_messages", withBaseYAML("chat_log:\n  max_messages_per_node: -1\n"), "chat_log max_messages_per_node must be positive"},
		{"voice_hub brain timeout non-positive", withBaseYAML("voice_hub:\n  enabled: true\n  brain_timeout_seconds: 0\n"), "voice_hub brain_timeout_seconds must be positive"},
		{"voice_hub tts timeout non-positive", withBaseYAML("voice_hub:\n  enabled: true\n  tts_timeout_seconds: -2\n"), "voice_hub tts_timeout_seconds must be positive"},
		{"fast_path timeout_ms non-positive", withBaseYAML("voice_hub:\n  enabled: true\n  fast_path:\n    url: http://127.0.0.1:8000/intent\n    timeout_ms: 0\n"), "voice_hub fast_path timeout_ms must be positive"},
		{"fast_path invalid url", withBaseYAML("voice_hub:\n  enabled: true\n  fast_path:\n    url: not-a-valid-url\n"), "voice_hub fast_path url must be an http(s) URL"},
		{"speaker_id missing embed_url", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    model: ecapa\n"), "voice_hub speaker_id requires embed_url"},
		{"speaker_id empty speaker id", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: \"\"\n        embedding: [1.0]\n"), "voice_hub speaker_id speaker 0 has an empty id"},
		{"speaker_id duplicate speaker id", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: a\n        embedding: [1.0]\n      - id: a\n        embedding: [1.0]\n"), "voice_hub speaker_id duplicate speaker id \"a\""},
		{"speaker_id empty embedding", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: a\n"), "voice_hub speaker_id speaker \"a\" has an empty embedding"},
		{"speaker_id nan embedding", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: a\n        embedding: [.nan]\n"), "voice_hub speaker_id speaker \"a\": embedding contains NaN or Inf"},
		{"speaker_id zero magnitude embedding", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: a\n        embedding: [0.0, 0.0]\n"), "voice_hub speaker_id speaker \"a\": embedding has zero magnitude"},
		{"speaker_id dimension mismatch", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    speakers:\n      - id: a\n        embedding: [1.0, 0.0]\n      - id: b\n        embedding: [1.0]\n"), "voice_hub speaker_id speaker \"b\" has dimension 1, expected 2"},
		{"speaker_id threshold zero", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    threshold: 0.0\n"), "voice_hub speaker_id threshold must be in (0, 1]"},
		{"speaker_id threshold greater than 1", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    threshold: 1.5\n"), "voice_hub speaker_id threshold must be in (0, 1]"},
		{"speaker_id timeout_seconds non-positive", withBaseYAML("voice_hub:\n  enabled: true\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    timeout_seconds: -1\n"), "voice_hub speaker_id timeout_seconds must be positive"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, err := config.ParseWithEnv([]byte(tc.yaml), mockGetenv(nil))
			if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("ParseWithEnv() err = %v, want substring %q", err, tc.wantErr)
			}
		})
	}
}

func TestParseWithEnv_ValidConfigsAndDefaults(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name, yaml                                                                   string
		nilGetenv                                                                    bool
		wantTransition, wantWeatherUnits, wantMethod, wantRemoteURL, wantVideoStream string
		wantLocName, wantThemeMode, wantForecastLoc                                  string
		wantWeatherInterval, wantChatMax                                             int
		wantHideCursor, wantVoiceHub, wantSpeakerID, wantFastPath, wantVideoEnabled  bool
		wantChatTTL                                                                  time.Duration
		wantLat                                                                      float64
	}{
		{name: "comment interpolation allowed and default transition", yaml: "# ${VAR} in comment is allowed\n" + withBaseYAML(""), wantTransition: "slide"},
		{name: "rotation transition fade", yaml: withBaseYAML("  rotation:\n    transition: fade\n"), wantTransition: "fade"},
		{name: "rotation transition instant", yaml: withBaseYAML("  rotation:\n    transition: instant\n"), wantTransition: "instant"},
		{name: "rotation transition none", yaml: withBaseYAML("  rotation:\n    transition: none\n"), wantTransition: "none"},
		{name: "portrait 3x6 grid valid", yaml: "timezone: UTC\ndisplay:\n  grid:\n    columns: 3\n    rows: 6\n  widgets:\n    - id: w1\n      type: spacer\n      dimensions: [3, 6]\n", wantTransition: "slide"},
		{name: "header weather metric and omitted refresh interval", yaml: withBaseYAML("  header:\n    weather:\n      units: metric\n      latitude: 37.0\n      longitude: -122.0\n"), wantWeatherUnits: "metric", wantWeatherInterval: 900},
		{name: "widget http method get normalization", yaml: "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      method: get\n", wantMethod: "GET"},
		{name: "widget default method POST with endpoint", yaml: "timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      endpoint: http://example.com/api\n", wantMethod: "POST"},
		{name: "hide_cursor and remote_url", yaml: withBaseYAML("  hide_cursor: true\n  remote_url: http://localhost:8080/display\n"), wantHideCursor: true, wantRemoteURL: "http://localhost:8080/display"},
		{name: "nil getenv fallback", yaml: withBaseYAML(""), nilGetenv: true, wantTransition: "slide"},
		{name: "voice hub speaker id and fast path valid", yaml: withBaseYAML("voice_hub:\n  enabled: true\n  fast_path:\n    url: http://127.0.0.1:8000/intent\n  speaker_id:\n    embed_url: http://127.0.0.1:8080/embed\n    model: ecapa\n    speakers:\n      - id: mike\n        embedding: [1.0, 0.0]\n"), wantVoiceHub: true, wantFastPath: true, wantSpeakerID: true},
		{name: "header video valid config", yaml: withBaseYAML("  header:\n    video:\n      enabled: true\n      stream_id: frontdoor\n      type: webrtc\n"), wantVideoEnabled: true, wantVideoStream: "frontdoor"},
		{name: "chat log defaults when omitted", yaml: withBaseYAML(""), wantChatTTL: 20 * time.Minute, wantChatMax: 50},
		{name: "chat log custom values", yaml: withBaseYAML("chat_log:\n  ttl_minutes: 5\n  max_messages_per_node: 12\n"), wantChatTTL: 5 * time.Minute, wantChatMax: 12},
		{name: "location and theme propagation", yaml: locationPropagateYAML, wantLocName: "Oakland Hills", wantLat: 37.8044, wantThemeMode: "auto", wantForecastLoc: "Oakland Hills"},
		{name: "empty theme mode defaults to day", yaml: withBaseYAML("  theme:\n    mode: \"\"\n"), wantThemeMode: "day"},
		{name: "partial location config filled from header weather", yaml: "timezone: UTC\nlocation:\n  name: \"\"\ndisplay:\n  header:\n    weather:\n      location: Oakland\n      latitude: 37.8\n      longitude: -122.2\n  widgets:\n    - id: w1\n      type: spacer\n", wantLocName: "Oakland", wantLat: 37.8},
		{name: "legacy header weather to location propagation", yaml: legacyWeatherPropagateYAML, wantLocName: "Oakland", wantLat: 37.8, wantThemeMode: "auto"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var getenv func(string) string
			if !tc.nilGetenv {
				getenv = mockGetenv(nil)
			}
			cfg, err := config.ParseWithEnv([]byte(tc.yaml), getenv)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tc.wantTransition != "" {
				assertEq(t, "transition", cfg.Display.Rotation.Transition, tc.wantTransition)
			}
			if tc.wantWeatherUnits != "" && cfg.Display.Header.Weather != nil {
				assertEq(t, "weather units", cfg.Display.Header.Weather.Units, tc.wantWeatherUnits)
			}
			if tc.wantWeatherInterval != 0 && cfg.Display.Header.Weather != nil {
				assertEq(t, "weather interval", cfg.Display.Header.Weather.GetRefreshIntervalSeconds(), tc.wantWeatherInterval)
			}
			if tc.wantMethod != "" && len(cfg.Display.Widgets) > 0 {
				assertEq(t, "widget method", cfg.Display.Widgets[0].Method, tc.wantMethod)
			}
			if tc.wantHideCursor {
				assertEq(t, "hide cursor", cfg.Display.HideCursor, true)
			}
			if tc.wantRemoteURL != "" {
				assertEq(t, "remote url", cfg.Display.RemoteURL, tc.wantRemoteURL)
			}
			if tc.wantVoiceHub {
				assertEq(t, "voice hub", cfg.VoiceHub.IsEnabled(), true)
			}
			if tc.wantFastPath {
				assertEq(t, "fast path", cfg.VoiceHub != nil && cfg.VoiceHub.FastPath.IsEnabled(), true)
			}
			if tc.wantSpeakerID {
				assertEq(t, "speaker id", cfg.VoiceHub != nil && cfg.VoiceHub.SpeakerID.IsEnabled(), true)
			}
			if tc.wantVideoEnabled {
				assertEq(t, "video enabled", cfg.Display.Header.Video != nil && cfg.Display.Header.Video.IsEnabled(), true)
			}
			if tc.wantVideoStream != "" && cfg.Display.Header.Video != nil {
				assertEq(t, "video stream", cfg.Display.Header.Video.GetStreamID(), tc.wantVideoStream)
			}
			if tc.wantChatTTL != 0 {
				assertEq(t, "chat ttl", cfg.ChatLog.GetTTL(), tc.wantChatTTL)
			}
			if tc.wantChatMax != 0 {
				assertEq(t, "chat max", cfg.ChatLog.GetMaxMessagesPerNode(), tc.wantChatMax)
			}
			if tc.wantLocName != "" && cfg.GetLocationConfig() != nil {
				assertEq(t, "loc name", cfg.GetLocationConfig().Name, tc.wantLocName)
			}
			if tc.wantLat != 0 && cfg.GetLocationConfig() != nil {
				assertEq(t, "latitude", cfg.GetLocationConfig().GetLatitude(), tc.wantLat)
			}
			if tc.wantThemeMode != "" && cfg.Display.Theme != nil {
				assertEq(t, "theme mode", cfg.Display.Theme.GetMode(), tc.wantThemeMode)
			}
			if tc.wantForecastLoc != "" {
				for _, w := range cfg.Display.Widgets {
					if w.Type == "weather-forecast" && w.Config != nil {
						assertEq(t, "forecast loc", w.Config["location_name"].(string), tc.wantForecastLoc)
					}
				}
			}
		})
	}
}

func TestResolveEnv(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name         string
		cfg          *config.Config
		env          map[string]string
		useNilGetenv bool
		wantToken    string
		wantSecrets  map[string]string
		wantErr      string
	}{
		{name: "token_env valid", cfg: mkWidgetConfig("MY_TOKEN", nil), env: map[string]string{"MY_TOKEN": "secret-123"}, wantToken: "secret-123", wantSecrets: map[string]string{"token": "secret-123"}},
		{name: "token_env unset error", cfg: mkWidgetConfig("UNSET_TOKEN", nil), wantErr: "environment variable 'UNSET_TOKEN' defined in token_env is unset or empty"},
		{name: "nested map valid", cfg: mkWidgetConfig("", map[string]any{"sub": map[string]any{"secret_env": "SUB_SECRET"}}), env: map[string]string{"SUB_SECRET": "nested-val"}, wantSecrets: map[string]string{"sub.secret": "nested-val"}},
		{name: "nested slice valid", cfg: mkWidgetConfig("", map[string]any{"items": []any{map[string]any{"url_env": "ITEM_URL"}}}), env: map[string]string{"ITEM_URL": "http://api.example.com"}, wantSecrets: map[string]string{"items[0].url": "http://api.example.com"}},
		{name: "nested slice of slices matrix valid", cfg: mkWidgetConfig("", map[string]any{"matrix": []any{[]any{map[string]any{"m_env": "M_VAL"}}}}), env: map[string]string{"M_VAL": "matrix-ok"}, wantSecrets: map[string]string{"matrix[0][0].m": "matrix-ok"}},
		{name: "map[any]any in map and slice valid", cfg: mkWidgetConfig("", map[string]any{"sub": map[any]any{"token_env": "ANY_TOK"}, "slice": []any{map[any]any{"url_env": "SLICE_URL"}}}), env: map[string]string{"ANY_TOK": "token-123", "SLICE_URL": "http://example.org"}, wantSecrets: map[string]string{"sub.token": "token-123", "slice[0].url": "http://example.org"}},
		{name: "error unset env in nested map", cfg: mkWidgetConfig("", map[string]any{"sub": map[string]any{"secret_env": "MISSING_VAR"}}), wantErr: "environment variable 'MISSING_VAR' defined in secret_env is unset or empty"},
		{name: "error non-string *_env", cfg: mkWidgetConfig("", map[string]any{"sub_env": 123}), wantErr: "key 'sub_env' must specify a non-empty environment variable name"},
		{name: "error conflicting key and key_env", cfg: mkWidgetConfig("", map[string]any{"api_key": "val", "api_key_env": "ENV_VAR"}), wantErr: "cannot specify both 'api_key' and 'api_key_env'"},
		{name: "error map[any]any unset var", cfg: mkWidgetConfig("", map[string]any{"sub": map[any]any{"token_env": "UNSET_ANY"}}), wantErr: "environment variable 'UNSET_ANY' defined in token_env is unset or empty"},
		{name: "error slice map[any]any unset var", cfg: mkWidgetConfig("", map[string]any{"slice": []any{map[any]any{"token_env": "UNSET_SLICE"}}}), wantErr: "environment variable 'UNSET_SLICE' defined in token_env is unset or empty"},
		{name: "ResolveEnv nil getenv defaults to os.Getenv", cfg: mkWidgetConfig("", nil), useNilGetenv: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			getenv := mockGetenv(tc.env)
			if tc.useNilGetenv {
				getenv = nil
			}
			err := tc.cfg.ResolveEnv(getenv)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("ResolveEnv() err = %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("ResolveEnv() unexpected error: %v", err)
			}
			w := tc.cfg.Display.Widgets[0]
			if tc.wantToken != "" {
				assertEq(t, "Token", w.Token, tc.wantToken)
			}
			for k, wantV := range tc.wantSecrets {
				assertEq(t, "Secrets["+k+"]", w.Secrets[k], wantV)
			}
		})
	}
}

func TestLoad(t *testing.T) {
	if _, err := config.Load(""); err == nil {
		t.Fatal("expected error reading DefaultConfigPath, got nil")
	}
	if _, err := config.LoadWithEnv("/nonexistent/path/never/exists.yaml", os.Getenv); err == nil {
		t.Fatal("expected error loading nonexistent file, got nil")
	}

	t.Setenv("MIRRORMERE_TEST_TOKEN", "sys-token-val")
	raw := []byte("timezone: UTC\ndisplay:\n  widgets:\n    - id: w1\n      type: spacer\n      token_env: MIRRORMERE_TEST_TOKEN\n")
	cfg, err := config.Parse(raw)
	if err != nil {
		t.Fatalf("Parse() unexpected error: %v", err)
	}
	assertEq(t, "Token", cfg.Display.Widgets[0].Token, "sys-token-val")
	assertEq(t, "Secrets[token]", cfg.Display.Widgets[0].Secrets["token"], "sys-token-val")

	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	loaded, err := config.Load(path)
	if err != nil {
		t.Fatalf("Load() unexpected error: %v", err)
	}
	assertEq(t, "Timezone", loaded.Timezone, "UTC")
}

func TestDisplayVoiceWaveform_Parse(t *testing.T) {
	t.Parallel()

	cfg, err := config.ParseWithEnv([]byte(withBaseYAML("  voice:\n    waveform:\n      enabled: true\n      levels_url: \"http://127.0.0.1:9100/levels\"\n      gain: 3.0\n")), mockGetenv(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Display.Voice == nil || cfg.Display.Voice.Waveform == nil {
		t.Fatal("display.voice.waveform not parsed")
	}
	wave := cfg.Display.Voice.Waveform
	assertEq(t, "wave enabled", wave.IsEnabled(), true)
	assertEq(t, "wave url", wave.GetLevelsURL(), "http://127.0.0.1:9100/levels")
	assertEq(t, "wave gain", wave.GetGain(), 3.0)

	cfg, err = config.ParseWithEnv([]byte(withBaseYAML("")), mockGetenv(nil))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg.Display.Voice != nil {
		t.Fatal("display.voice should be nil when omitted")
	}
}

func TestConfig_Accessors(t *testing.T) {
	t.Parallel()
	var nilLoc *config.LocationConfig
	assertEq(t, "nil loc lat", nilLoc.GetLatitude(), 0)
	assertEq(t, "nil loc lon", nilLoc.GetLongitude(), 0)
	assertEq(t, "zero loc lat", (&config.LocationConfig{}).GetLatitude(), 0)
	assertEq(t, "zero loc lon", (&config.LocationConfig{}).GetLongitude(), 0)
	popLoc := &config.LocationConfig{Latitude: new(37.8), Longitude: new(-122.2)}
	assertEq(t, "pop loc lat", popLoc.GetLatitude(), 37.8)
	assertEq(t, "pop loc lon", popLoc.GetLongitude(), -122.2)

	var nilTheme *config.ThemeConfig
	assertEq(t, "theme nil", nilTheme.GetMode(), "day")
	assertEq(t, "theme zero", (&config.ThemeConfig{}).GetMode(), "day")
	assertEq(t, "theme ws", (&config.ThemeConfig{Mode: "   "}).GetMode(), "day")
	assertEq(t, "theme upper", (&config.ThemeConfig{Mode: "NIGHT"}).GetMode(), "night")

	var nilVid *config.HeaderVideoConfig
	assertEq(t, "vid nil", !nilVid.IsEnabled() && nilVid.GetStreamID() == "chromecast" && nilVid.GetType() == "webrtc", true)
	assertEq(t, "vid zero", !(&config.HeaderVideoConfig{}).IsEnabled() && (&config.HeaderVideoConfig{}).GetStreamID() == "chromecast" && (&config.HeaderVideoConfig{}).GetType() == "webrtc", true)
	popVid := &config.HeaderVideoConfig{Enabled: new(true), StreamID: "cam", Type: "hls"}
	assertEq(t, "vid pop", popVid.IsEnabled() && popVid.GetStreamID() == "cam" && popVid.GetType() == "hls", true)

	var nilWave *config.VoiceWaveformConfig
	assertEq(t, "wave nil", !nilWave.IsEnabled() && nilWave.GetLevelsURL() == "http://127.0.0.1:8765/levels" && nilWave.GetGain() == 2.2, true)
	assertEq(t, "wave zero", !(&config.VoiceWaveformConfig{}).IsEnabled() && (&config.VoiceWaveformConfig{}).GetLevelsURL() == "http://127.0.0.1:8765/levels" && (&config.VoiceWaveformConfig{}).GetGain() == 2.2, true)
	popWave := &config.VoiceWaveformConfig{Enabled: new(true), LevelsURL: "http://10.0.0.5:9000/levels", Gain: new(1.5)}
	assertEq(t, "wave pop", popWave.IsEnabled() && popWave.GetLevelsURL() == "http://10.0.0.5:9000/levels" && popWave.GetGain() == 1.5, true)
	assertEq(t, "wave blank url", (&config.VoiceWaveformConfig{LevelsURL: "  "}).GetLevelsURL(), "http://127.0.0.1:8765/levels")

	var nilWeather *config.HeaderWeatherConfig
	assertEq(t, "weather nil", nilWeather.GetRefreshIntervalSeconds(), 900)
	assertEq(t, "weather zero", (&config.HeaderWeatherConfig{}).GetRefreshIntervalSeconds(), 900)
	assertEq(t, "weather pop", (&config.HeaderWeatherConfig{RefreshIntervalSeconds: new(10)}).GetRefreshIntervalSeconds(), 10)

	var nilChat *config.ChatLogConfig
	assertEq(t, "chat nil", nilChat.GetTTL() == 20*time.Minute && nilChat.GetMaxMessagesPerNode() == 50, true)
	assertEq(t, "chat zero", (&config.ChatLogConfig{}).GetTTL() == 20*time.Minute && (&config.ChatLogConfig{}).GetMaxMessagesPerNode() == 50, true)
	nonPosChat := &config.ChatLogConfig{TTLMinutes: new(0), MaxMessagesPerNode: new(-5)}
	assertEq(t, "chat nonpos", nonPosChat.GetTTL() == 20*time.Minute && nonPosChat.GetMaxMessagesPerNode() == 50, true)
	popChat := &config.ChatLogConfig{TTLMinutes: new(5), MaxMessagesPerNode: new(10)}
	assertEq(t, "chat pop", popChat.GetTTL() == 5*time.Minute && popChat.GetMaxMessagesPerNode() == 10, true)

	var nilFP *config.FastPathConfig
	assertEq(t, "fp nil", !nilFP.IsEnabled() && nilFP.GetTimeoutMS() == 800, true)
	assertEq(t, "fp zero", !(&config.FastPathConfig{}).IsEnabled() && (&config.FastPathConfig{}).GetTimeoutMS() == 800, true)
	assertEq(t, "fp nonpos", (&config.FastPathConfig{URL: "http://intent", TimeoutMS: new(-5)}).IsEnabled() && (&config.FastPathConfig{URL: "http://intent", TimeoutMS: new(-5)}).GetTimeoutMS() == 800, true)
	popFP := &config.FastPathConfig{URL: "http://intent", TimeoutMS: new(500)}
	assertEq(t, "fp pop", popFP.IsEnabled() && popFP.GetTimeoutMS() == 500, true)

	var nilSID *config.SpeakerIDConfig
	assertEq(t, "sid nil", !nilSID.IsEnabled() && nilSID.GetThreshold() == 0.70 && nilSID.GetTimeoutSeconds() == 5, true)
	assertEq(t, "sid zero", !(&config.SpeakerIDConfig{}).IsEnabled() && (&config.SpeakerIDConfig{}).GetThreshold() == 0.70 && (&config.SpeakerIDConfig{}).GetTimeoutSeconds() == 5, true)
	assertEq(t, "sid nonpos", (&config.SpeakerIDConfig{TimeoutSeconds: new(-5)}).GetTimeoutSeconds(), 5)
	popSID := &config.SpeakerIDConfig{EmbedURL: "http://embed", Model: "ecapa", Speakers: []config.SpeakerFingerprint{{ID: "a", Embedding: []float64{1.0}}}, Threshold: new(0.85), TimeoutSeconds: new(10)}
	assertEq(t, "sid pop", popSID.IsEnabled() && popSID.GetThreshold() == 0.85 && popSID.GetTimeoutSeconds() == 10, true)

	var nilVH *config.VoiceHubConfig
	assertEq(t, "vh nil", !nilVH.IsEnabled() && nilVH.GetBrainTimeoutSeconds() == 30 && nilVH.GetTTSTimeoutSeconds() == 10 && nilVH.GetTTSVoice() == "af_bella" && nilVH.GetTTSModel() == "kokoro", true)
	assertEq(t, "vh zero", !(&config.VoiceHubConfig{}).IsEnabled() && (&config.VoiceHubConfig{}).GetBrainTimeoutSeconds() == 30 && (&config.VoiceHubConfig{}).GetTTSTimeoutSeconds() == 10 && (&config.VoiceHubConfig{}).GetTTSVoice() == "af_bella" && (&config.VoiceHubConfig{}).GetTTSModel() == "kokoro", true)
	nonPosVH := &config.VoiceHubConfig{BrainTimeoutSeconds: new(-5), TTSTimeoutSeconds: new(0)}
	assertEq(t, "vh nonpos", nonPosVH.GetBrainTimeoutSeconds() == 30 && nonPosVH.GetTTSTimeoutSeconds() == 10, true)
	popVH := &config.VoiceHubConfig{Enabled: true, BrainTimeoutSeconds: new(60), TTSTimeoutSeconds: new(20), TTSVoice: "custom_v", TTSModel: "custom_m"}
	assertEq(t, "vh pop", popVH.IsEnabled() && popVH.GetBrainTimeoutSeconds() == 60 && popVH.GetTTSTimeoutSeconds() == 20 && popVH.GetTTSVoice() == "custom_v" && popVH.GetTTSModel() == "custom_m", true)

	var nilRot *config.RotationConfig
	assertEq(t, "rot nil", nilRot.GetIntervalSeconds() == 30 && nilRot.GetPauseDurationSeconds() == 120, true)
	zeroRot := config.RotationConfig{}
	assertEq(t, "rot zero", zeroRot.GetIntervalSeconds() == 30 && zeroRot.GetPauseDurationSeconds() == 120, true)
	popRot := config.RotationConfig{IntervalSeconds: new(10), Transition: "fade", PauseOnTouch: new(false), PauseDurationSeconds: new(60)}
	assertEq(t, "rot pop", popRot.GetIntervalSeconds() == 10 && popRot.GetPauseDurationSeconds() == 60, true)

	var nilHdr *config.HeaderConfig
	assertEq(t, "hdr nil", nilHdr.IsEnabled(), true)
	assertEq(t, "hdr zero", (&config.HeaderConfig{}).IsEnabled(), true)
	assertEq(t, "hdr pop", (&config.HeaderConfig{Enabled: new(false)}).IsEnabled(), false)

	elems := config.DefaultHeaderElements()
	assertEq(t, "hdr elems", len(elems) == 4 && elems[0] == "clock", true)

	locCfg := &config.Config{Timezone: "America/Denver"}
	firstLoc := locCfg.Location()
	assertEq(t, "cached loc", firstLoc == locCfg.Location() && firstLoc.String() == "America/Denver", true)
	assertEq(t, "invalid loc fallback", (&config.Config{Timezone: "Invalid/Zone_Name"}).Location(), time.UTC)

	var nilCfg *config.Config
	assertEq(t, "nil cfg loc", nilCfg.GetLocationConfig() == nil, true)
	assertEq(t, "empty cfg loc", (&config.Config{}).GetLocationConfig() == nil, true)
	dl := (&config.Config{LocationConfig: &config.LocationConfig{Name: "Direct"}}).GetLocationConfig()
	assertEq(t, "direct loc", dl != nil && dl.Name == "Direct", true)
	weatherFallbackCfg := &config.Config{Display: config.DisplayConfig{Header: config.HeaderConfig{Weather: &config.HeaderWeatherConfig{Location: "San Jose", Latitude: 37.33, Longitude: -121.88}}}}
	fbLoc := weatherFallbackCfg.GetLocationConfig()
	assertEq(t, "fb loc", fbLoc != nil && fbLoc.Name == "San Jose" && *fbLoc.Latitude == 37.33, true)

	var zeroDisplay config.DisplayConfig
	assertEq(t, "layout edit default", zeroDisplay.IsLayoutEditAllowed(), true)
	assertEq(t, "override path default", zeroDisplay.GetLayoutOverridePath(), config.DefaultLayoutOverridePath)
	disabledDisplay := config.DisplayConfig{AllowLayoutEdit: new(false), LayoutOverridePath: "/tmp/custom.yaml"}
	assertEq(t, "layout edit disabled", disabledDisplay.IsLayoutEditAllowed(), false)
	assertEq(t, "override path custom", disabledDisplay.GetLayoutOverridePath(), "/tmp/custom.yaml")
}

func TestDisplayUI(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name    string
		yaml    string
		wantUI  string
		wantErr string
	}{
		{name: "omitted defaults to react", yaml: withBaseYAML(""), wantUI: "react"},
		{name: "explicit legacy", yaml: withBaseYAML("  ui: legacy\n"), wantUI: "legacy"},
		{name: "react", yaml: withBaseYAML("  ui: react\n"), wantUI: "react"},
		{name: "react normalized case and whitespace", yaml: withBaseYAML("  ui: \" React \"\n"), wantUI: "react"},
		{name: "unknown shell rejected", yaml: withBaseYAML("  ui: vue\n"), wantErr: "invalid display ui 'vue': must be one of legacy, react"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg, err := config.ParseWithEnv([]byte(tc.yaml), nil)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error: got %v, want substring %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got := cfg.Display.GetUI(); got != tc.wantUI {
				t.Fatalf("GetUI: got %q, want %q", got, tc.wantUI)
			}
		})
	}
}

func TestLiveViewAndPortraitGridHelpers(t *testing.T) {
	t.Parallel()
	yamlStr := `timezone: America/Los_Angeles
display:
  grid:
    columns: 3
    rows: 6
  widgets:
    - id: hearth-family-grid
      type: calendar-grid
      dimensions: [3, 3]
    - id: cam-doorbell
      type: live-view
      dimensions: [3, 2]
      config:
        title: Doorbell
        stream_url: /hearth-api/camera/stream/doorbell
        stream_type: mjpeg
        fit: cover
    - id: cam-front-yard
      type: live-view
      dimensions: [1, 2]
      config:
        title: Front Yard
        stream_url: /hearth-api/camera/stream/front_yard_camera
    - id: cam-rosie
      type: live-view
      dimensions: [1, 2]
      config:
        title: Rosie Cam
        stream_url: https://stream.example.com/webrtc
        stream_type: webrtc
`
	cfg, err := config.ParseWithEnv([]byte(yamlStr), nil)
	if err != nil {
		t.Fatalf("unexpected parse error: %v", err)
	}
	assertEq(t, "portrait grid", cfg.Display.Grid.IsPortrait(), true)
	assertEq(t, "portrait cells", cfg.Display.Grid.CellCount(), 18)
	assertEq(t, "zero cells", (config.GridConfig{}).CellCount(), 0)
	assertEq(t, "landscape portrait", (config.GridConfig{Columns: 6, Rows: 2}).IsPortrait(), false)

	lv := cfg.Display.LiveViewWidgets()
	assertEq(t, "live-view count", len(lv), 3)
	assertEq(t, "doorbell stream type", lv[0].StreamType(), "mjpeg")
	assertEq(t, "inferred camera proxy stream type", lv[1].StreamType(), "mjpeg")
	assertEq(t, "explicit webrtc stream type", lv[2].StreamType(), "webrtc")
	assertEq(t, "non live-view default stream type", cfg.Display.Widgets[0].StreamType(), "webrtc")
}
