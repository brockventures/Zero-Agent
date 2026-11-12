package config

import (
	"fmt"
	"math"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// DefaultConfigPath is the canonical container filesystem path to the user configuration.
const DefaultConfigPath = "/config/config.yaml"

var interpolationRegex = regexp.MustCompile(`\$\{([^}]+)\}`)

func isValidHTTPURL(raw string) bool {
	if raw == "" || raw != strings.TrimSpace(raw) {
		return false
	}
	u, err := url.ParseRequestURI(raw)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != ""
}

func positiveIntOr(p *int, def int) int {
	if p == nil || *p <= 0 {
		return def
	}
	return *p
}

func trimmedOr(s, def string) string {
	t := strings.TrimSpace(s)
	if t == "" {
		return def
	}
	return t
}

func derefOr[T any](p *T, def T) T {
	if p == nil {
		return def
	}
	return *p
}

func checkPositiveInt(p *int, label string) error {
	if p != nil && *p <= 0 {
		return fmt.Errorf("%s must be positive (got %d)", label, *p)
	}
	return nil
}

func checkNonNegativeInt(p *int, label string) error {
	if p != nil && *p < 0 {
		return fmt.Errorf("%s must be non-negative (got %d)", label, *p)
	}
	return nil
}

func checkLatLon(lat, lon float64, prefix string) error {
	if lat < -90.0 || lat > 90.0 {
		return fmt.Errorf("%s latitude must be between -90 and 90 (got %f)", prefix, lat)
	}
	if lon < -180.0 || lon > 180.0 {
		return fmt.Errorf("%s longitude must be between -180 and 180 (got %f)", prefix, lon)
	}
	return nil
}

// DefaultLayoutOverridePath is the default on-disk layout override file path.
const DefaultLayoutOverridePath = "/data/layout-override.yaml"

// Config represents the root configuration schema of Mirrormere.
type Config struct {
	Host           string          `yaml:"host,omitempty"`
	Port           *int            `yaml:"port,omitempty"`
	Timezone       string          `yaml:"timezone"`
	LocationConfig *LocationConfig `yaml:"location,omitempty"`
	Display        DisplayConfig   `yaml:"display"`
	VoiceHub       *VoiceHubConfig `yaml:"voice_hub,omitempty"`
	ChatLog        *ChatLogConfig  `yaml:"chat_log,omitempty"`

	location *time.Location
}

// DisplayConfig specifies visual presentation, rotation parameters, and active widget instances.
type DisplayConfig struct {
	UI                 string              `yaml:"ui,omitempty"` // "react" (default) or "legacy"; ?ui= on /display overrides
	HideCursor         bool                `yaml:"hide_cursor,omitempty"`
	RemoteURL          string              `yaml:"remote_url,omitempty"`
	AllowLayoutEdit    *bool               `yaml:"allow_layout_edit,omitempty"`
	LayoutOverridePath string              `yaml:"layout_override_path,omitempty"`
	Rotation           RotationConfig      `yaml:"rotation"`
	Header             HeaderConfig        `yaml:"header"`
	Grid               GridConfig          `yaml:"grid"`
	Widgets            []WidgetConfig      `yaml:"widgets"`
	Theme              *ThemeConfig        `yaml:"theme,omitempty"`
	Voice              *DisplayVoiceConfig `yaml:"voice,omitempty"`
}

// GetUI returns the display shell selected by config: "react" (default) or "legacy".
func (dc DisplayConfig) GetUI() string {
	return strings.ToLower(trimmedOr(dc.UI, "react"))
}

// IsLayoutEditAllowed returns true if live canvas layout editing is permitted (default true).
func (dc DisplayConfig) IsLayoutEditAllowed() bool {
	return derefOr(dc.AllowLayoutEdit, true)
}

// GetLayoutOverridePath returns the target layout override path (default /data/layout-override.yaml).
func (dc DisplayConfig) GetLayoutOverridePath() string {
	return trimmedOr(dc.LayoutOverridePath, DefaultLayoutOverridePath)
}

// LiveViewWidgets returns all configured widgets of type "live-view" in declaration order.
func (dc DisplayConfig) LiveViewWidgets() []WidgetConfig {
	var out []WidgetConfig
	for _, w := range dc.Widgets {
		if w.IsLiveView() {
			out = append(out, w)
		}
	}
	return out
}

// DisplayVoiceConfig configures voice visuals on the display.
type DisplayVoiceConfig struct {
	Waveform *VoiceWaveformConfig `yaml:"waveform,omitempty"`
}

// VoiceWaveformConfig configures the waveform mouth fed by the ear daemon's /levels stream.
type VoiceWaveformConfig struct {
	Enabled   *bool    `yaml:"enabled,omitempty"`
	LevelsURL string   `yaml:"levels_url,omitempty"`
	Gain      *float64 `yaml:"gain,omitempty"`
}

// IsEnabled returns true if the voice waveform mouth is explicitly enabled.
func (vw *VoiceWaveformConfig) IsEnabled() bool {
	if vw == nil {
		return false
	}
	return derefOr(vw.Enabled, false)
}

// GetLevelsURL returns the ear daemon's levels stream URL (default: http://127.0.0.1:8765/levels).
func (vw *VoiceWaveformConfig) GetLevelsURL() string {
	if vw == nil {
		return "http://127.0.0.1:8765/levels"
	}
	return trimmedOr(vw.LevelsURL, "http://127.0.0.1:8765/levels")
}

// GetGain returns the waveform amplification factor (default: 2.2).
func (vw *VoiceWaveformConfig) GetGain() float64 {
	if vw == nil {
		return 2.2
	}
	return derefOr(vw.Gain, 2.2)
}

// LocationConfig defines physical coordinates and location name for the device/household.
type LocationConfig struct {
	Name      string   `yaml:"name,omitempty"`
	Latitude  *float64 `yaml:"latitude,omitempty"`
	Longitude *float64 `yaml:"longitude,omitempty"`
}

// GetLatitude returns the latitude, or 0 if unset or nil.
func (lc *LocationConfig) GetLatitude() float64 {
	if lc == nil {
		return 0
	}
	return derefOr(lc.Latitude, 0)
}

// GetLongitude returns the longitude, or 0 if unset or nil.
func (lc *LocationConfig) GetLongitude() float64 {
	if lc == nil {
		return 0
	}
	return derefOr(lc.Longitude, 0)
}

// ThemeConfig defines visual theme mode and Day/Night switching behavior.
type ThemeConfig struct {
	Mode string `yaml:"mode,omitempty"` // "auto", "day", "night"
}

// GetMode returns the configured theme mode, defaulting to "day" if unspecified.
func (tc *ThemeConfig) GetMode() string {
	if tc == nil {
		return "day"
	}
	return strings.ToLower(trimmedOr(tc.Mode, "day"))
}

// GetLocationConfig returns the device location configuration, falling back to header weather coordinates if present.
func (c *Config) GetLocationConfig() *LocationConfig {
	if c == nil {
		return nil
	}
	if c.LocationConfig != nil {
		return c.LocationConfig
	}
	// Backwards compatibility: fallback to display.header.weather if set
	if c.Display.Header.Weather != nil && (c.Display.Header.Weather.Latitude != 0 || c.Display.Header.Weather.Longitude != 0) {
		lat := c.Display.Header.Weather.Latitude
		lon := c.Display.Header.Weather.Longitude
		return &LocationConfig{
			Name:      c.Display.Header.Weather.Location,
			Latitude:  &lat,
			Longitude: &lon,
		}
	}
	return nil
}

// RotationConfig configures screen advance intervals and transition visual hints.
type RotationConfig struct {
	IntervalSeconds      *int   `yaml:"interval_seconds,omitempty"`
	Transition           string `yaml:"transition,omitempty"`
	PauseOnTouch         *bool  `yaml:"pause_on_touch,omitempty"`
	PauseDurationSeconds *int   `yaml:"pause_duration_seconds,omitempty"`
}

// HeaderConfig configures the persistent glance banner at the top of the display.
type HeaderConfig struct {
	Enabled  *bool                `yaml:"enabled,omitempty"`
	Elements []string             `yaml:"elements,omitempty"`
	Weather  *HeaderWeatherConfig `yaml:"weather,omitempty"`
	Video    *HeaderVideoConfig   `yaml:"video,omitempty"`
}

// HeaderVideoConfig configures the header action button for expanding fullscreen video streams.
type HeaderVideoConfig struct {
	Enabled    *bool  `yaml:"enabled,omitempty"`
	StreamID   string `yaml:"stream_id,omitempty"`
	StreamURL  string `yaml:"stream_url,omitempty"`
	ControlURL string `yaml:"control_url,omitempty"`
	Type       string `yaml:"type,omitempty"`
}

// IsEnabled returns true if the header video button is explicitly enabled.
func (hv *HeaderVideoConfig) IsEnabled() bool {
	if hv == nil {
		return false
	}
	return derefOr(hv.Enabled, false)
}

// GetStreamID returns the configured stream identifier (default: "chromecast").
func (hv *HeaderVideoConfig) GetStreamID() string {
	if hv == nil {
		return "chromecast"
	}
	return trimmedOr(hv.StreamID, "chromecast")
}

// GetType returns the configured stream type (default: "webrtc").
func (hv *HeaderVideoConfig) GetType() string {
	if hv == nil {
		return "webrtc"
	}
	return trimmedOr(hv.Type, "webrtc")
}

// HeaderWeatherConfig configures the autonomous top-banner weather poller.
type HeaderWeatherConfig struct {
	RefreshIntervalSeconds *int    `yaml:"refresh_interval_seconds,omitempty"`
	Latitude               float64 `yaml:"latitude"`
	Longitude              float64 `yaml:"longitude"`
	Units                  string  `yaml:"units,omitempty"`
	Location               string  `yaml:"location,omitempty"`
}

// GetRefreshIntervalSeconds returns the weather polling cadence in seconds (default: 900).
func (hw *HeaderWeatherConfig) GetRefreshIntervalSeconds() int {
	if hw == nil {
		return 900
	}
	return derefOr(hw.RefreshIntervalSeconds, 900)
}

// GridConfig defines the discrete tile geometry (6x2 landscape or 3x6 portrait).
type GridConfig struct {
	Columns int `yaml:"columns,omitempty"`
	Rows    int `yaml:"rows,omitempty"`
}

// IsPortrait reports whether the grid uses the 3x6 portrait layout geometry.
func (g GridConfig) IsPortrait() bool {
	return g.Columns == 3 && g.Rows == 6
}

// CellCount returns the total number of discrete grid cells (columns * rows).
func (g GridConfig) CellCount() int {
	if g.Columns <= 0 || g.Rows <= 0 {
		return 0
	}
	return g.Columns * g.Rows
}

func validateGridAndWidgetDimensions(grid GridConfig, widgetID string, dims []int) error {
	if (grid.Columns != 6 || grid.Rows != 2) && (grid.Columns != 3 || grid.Rows != 6) {
		return fmt.Errorf("grid dimensions must be 6x2 or 3x6 (got %dx%d)", grid.Columns, grid.Rows)
	}
	if len(dims) == 0 {
		return nil
	}
	if len(dims) != 2 {
		return fmt.Errorf("widget '%s': dimensions must contain exactly 2 integers: [cols, rows]", widgetID)
	}
	cols, rows := dims[0], dims[1]
	if cols < 1 || cols > grid.Columns || rows < 1 || rows > grid.Rows {
		return fmt.Errorf("widget '%s': invalid dimensions [%d, %d]: cols must be 1..%d, rows must be 1..%d", widgetID, cols, rows, grid.Columns, grid.Rows)
	}
	return nil
}

// WidgetConfig declares a self-contained widget instance with placement and domain settings.
type WidgetConfig struct {
	ID                     string            `yaml:"id"`
	Type                   string            `yaml:"type"`
	Dimensions             []int             `yaml:"dimensions,omitempty"`
	Pinned                 bool              `yaml:"pinned"`
	RefreshIntervalSeconds *int              `yaml:"refresh_interval_seconds,omitempty"`
	Endpoint               string            `yaml:"endpoint,omitempty"`
	Method                 string            `yaml:"method,omitempty"`
	TokenEnv               string            `yaml:"token_env,omitempty"`
	Token                  string            `yaml:"-"` // Resolved secret populated from TokenEnv
	Secrets                map[string]string `yaml:"-"` // Resolved secrets populated from *_env keys in Config
	Config                 map[string]any    `yaml:"config,omitempty"`
}

// IsLiveView reports whether this widget is a live-view camera/video tile.
func (w WidgetConfig) IsLiveView() bool {
	return strings.TrimSpace(w.Type) == "live-view"
}

// StreamType returns the normalized live-view stream type ("webrtc", "mjpeg", or "hls"),
// inferring "mjpeg" for Hearth camera proxy endpoints when stream_type is omitted.
func (w WidgetConfig) StreamType() string {
	if w.Config != nil {
		if raw, ok := w.Config["stream_type"].(string); ok {
			switch norm := strings.ToLower(strings.TrimSpace(raw)); norm {
			case "mjpeg", "hls", "webrtc":
				return norm
			}
		}
		if rawURL, ok := w.Config["stream_url"].(string); ok {
			u := strings.ToLower(strings.TrimSpace(rawURL))
			if strings.Contains(u, "/hearth-api/camera/") || strings.Contains(u, "mjpeg") || strings.Contains(u, "camera_proxy") {
				return "mjpeg"
			}
		}
	}
	return "webrtc"
}

// ChatLogConfig tunes the in-memory conversation store behind the chat-log
// widget (nothing is persisted). Changes take effect on restart.
type ChatLogConfig struct {
	// TTLMinutes is how long a message stays visible (default 20).
	TTLMinutes *int `yaml:"ttl_minutes,omitempty"`
	// MaxMessagesPerNode caps retained messages per voice node (default 50).
	MaxMessagesPerNode *int `yaml:"max_messages_per_node,omitempty"`
}

// GetTTL returns the message lifetime (default: 20 minutes).
func (c *ChatLogConfig) GetTTL() time.Duration {
	if c == nil {
		return 20 * time.Minute
	}
	return time.Duration(positiveIntOr(c.TTLMinutes, 20)) * time.Minute
}

// GetMaxMessagesPerNode returns the per-node message cap (default: 50).
func (c *ChatLogConfig) GetMaxMessagesPerNode() int {
	if c == nil {
		return 50
	}
	return positiveIntOr(c.MaxMessagesPerNode, 50)
}

// VoiceHubConfig configures the LAN voice pipeline coordinator.
type VoiceHubConfig struct {
	Enabled             bool   `yaml:"enabled"`
	STTURL              string `yaml:"stt_url,omitempty"`
	BrainURL            string `yaml:"brain_url,omitempty"`
	BrainTimeoutSeconds *int   `yaml:"brain_timeout_seconds,omitempty"`
	TTSURL              string `yaml:"tts_url,omitempty"`
	TTSVoice            string `yaml:"tts_voice,omitempty"`
	TTSModel            string `yaml:"tts_model,omitempty"`
	TTSTimeoutSeconds   *int   `yaml:"tts_timeout_seconds,omitempty"`

	// SpeakerID enables matching each utterance against enrolled voice
	// fingerprints. Optional: when absent,
	// every turn carries an empty speaker.
	SpeakerID *SpeakerIDConfig `yaml:"speaker_id,omitempty"`

	// FastPath optionally routes each transcript to a deterministic intent
	// endpoint (POST /intent) before the brain. Absent or an empty url means
	// the feature is off and the hub never calls out.
	FastPath *FastPathConfig `yaml:"fast_path,omitempty"`
}

// FastPathConfig configures the optional deterministic voice fast path.
type FastPathConfig struct {
	// URL is the full POST /intent endpoint. Empty disables the fast path.
	URL string `yaml:"url,omitempty"`
	// TimeoutMS bounds the wait for the response headers (default 800). A
	// slower answer counts as no match and the turn goes to the brain.
	TimeoutMS *int `yaml:"timeout_ms,omitempty"`
}

// IsEnabled reports whether a fast-path URL is configured.
func (f *FastPathConfig) IsEnabled() bool {
	return f != nil && strings.TrimSpace(f.URL) != ""
}

// GetTimeoutMS returns the fast-path response timeout in milliseconds (default: 800).
func (f *FastPathConfig) GetTimeoutMS() int {
	if f == nil {
		return 800
	}
	return positiveIntOr(f.TimeoutMS, 800)
}

// SpeakerFingerprint is one enrolled speaker voiceprint centroid.
type SpeakerFingerprint struct {
	ID        string    `yaml:"id" json:"id"`
	Embedding []float64 `yaml:"embedding" json:"embedding"`
}

// SpeakerIDConfig configures voice-fingerprint speaker matching.
type SpeakerIDConfig struct {
	// EmbedURL is an HTTP endpoint that accepts a raw WAV body and returns
	// {"embedding": [float...], "model": "<model id>"}.
	EmbedURL string `yaml:"embed_url"`
	// Model is the embedding model the fingerprints were generated with.
	// Embeddings from different models are not comparable.
	Model string `yaml:"model,omitempty"`
	// Speakers is the list of enrolled speaker fingerprints configured inline.
	Speakers []SpeakerFingerprint `yaml:"speakers,omitempty"`
	// Threshold is the minimum cosine similarity to accept a match (default 0.70).
	Threshold *float64 `yaml:"threshold,omitempty"`
	// TimeoutSeconds bounds the embed call (default 5).
	TimeoutSeconds *int `yaml:"timeout_seconds,omitempty"`
}

// IsEnabled reports whether speaker matching is configured with at least one enrolled speaker.
func (s *SpeakerIDConfig) IsEnabled() bool {
	return s != nil && strings.TrimSpace(s.EmbedURL) != "" && len(s.Speakers) > 0
}

// GetThreshold returns the match threshold (default: 0.70).
func (s *SpeakerIDConfig) GetThreshold() float64 {
	if s == nil {
		return 0.70
	}
	return derefOr(s.Threshold, 0.70)
}

// GetTimeoutSeconds returns the embed timeout in seconds (default: 5).
func (s *SpeakerIDConfig) GetTimeoutSeconds() int {
	if s == nil {
		return 5
	}
	return positiveIntOr(s.TimeoutSeconds, 5)
}

// IsEnabled reports whether the voice hub is enabled.
func (vh *VoiceHubConfig) IsEnabled() bool {
	return vh != nil && vh.Enabled
}

// GetBrainTimeoutSeconds returns the brain deliberation timeout in seconds (default: 30).
func (vh *VoiceHubConfig) GetBrainTimeoutSeconds() int {
	if vh == nil {
		return 30
	}
	return positiveIntOr(vh.BrainTimeoutSeconds, 30)
}

// GetTTSTimeoutSeconds returns the TTS synthesis timeout in seconds (default: 10).
func (vh *VoiceHubConfig) GetTTSTimeoutSeconds() int {
	if vh == nil {
		return 10
	}
	return positiveIntOr(vh.TTSTimeoutSeconds, 10)
}

// GetTTSVoice returns the voice model identifier (default: "af_bella").
func (vh *VoiceHubConfig) GetTTSVoice() string {
	if vh == nil {
		return "af_bella"
	}
	return trimmedOr(vh.TTSVoice, "af_bella")
}

// GetTTSModel returns the TTS engine/model identifier (default: "kokoro").
func (vh *VoiceHubConfig) GetTTSModel() string {
	if vh == nil {
		return "kokoro"
	}
	return trimmedOr(vh.TTSModel, "kokoro")
}

// Location returns the validated IANA time.Location pointer for the household.
func (c *Config) Location() *time.Location {
	if c.location != nil {
		return c.location
	}
	loc, err := time.LoadLocation(c.Timezone)
	if err != nil {
		return time.UTC
	}
	c.location = loc
	return loc
}

// GetIntervalSeconds returns rotation interval in seconds (default: 30).
func (r *RotationConfig) GetIntervalSeconds() int {
	if r == nil {
		return 30
	}
	return derefOr(r.IntervalSeconds, 30)
}

// GetPauseDurationSeconds returns touch interaction pause duration in seconds (default: 120).
func (r *RotationConfig) GetPauseDurationSeconds() int {
	if r == nil {
		return 120
	}
	return derefOr(r.PauseDurationSeconds, 120)
}

// IsEnabled returns whether the top banner header is active (default: true).
func (h *HeaderConfig) IsEnabled() bool {
	if h == nil {
		return true
	}
	return derefOr(h.Enabled, true)
}

// DefaultHeaderElements returns the standard header elements if none specified.
func DefaultHeaderElements() []string {
	return []string{"clock", "date", "weather_badge", "sync_status"}
}

func checkNodeForInterpolation(node *yaml.Node) error {
	if node == nil {
		return nil
	}
	if node.Kind == yaml.ScalarNode && interpolationRegex.MatchString(node.Value) {
		return fmt.Errorf("line %d: arbitrary '${...}' string interpolation is prohibited in '%s'; use explicit '*_env' keys instead (e.g. token_env, url_env)", node.Line, node.Value)
	}
	for _, child := range node.Content {
		if err := checkNodeForInterpolation(child); err != nil {
			return err
		}
	}
	return nil
}

func validateStructuralKeys(root *yaml.Node) error {
	if len(root.Content) == 0 {
		return fmt.Errorf("configuration file is empty")
	}
	topNode := root.Content[0]
	if topNode.Kind != yaml.MappingNode {
		return fmt.Errorf("configuration root must be a YAML mapping")
	}
	for i := 0; i < len(topNode.Content); i += 2 {
		keyNode, valNode := topNode.Content[i], topNode.Content[i+1]
		switch key := keyNode.Value; key {
		case "screens":
			return fmt.Errorf("line %d: configuration contains disallowed top-level key 'screens': screens are dynamically computed by the 6x2 layout solver", keyNode.Line)
		case "providers":
			return fmt.Errorf("line %d: configuration contains disallowed top-level key 'providers': widget instances configure their own data sources under display.widgets", keyNode.Line)
		case "header":
			return fmt.Errorf("line %d: configuration contains disallowed top-level key 'header': header must be configured under display.header", keyNode.Line)
		case "timezone", "host", "port", "voice_hub", "chat_log", "location":
		case "display":
			if valNode.Kind == yaml.MappingNode {
				if err := validateDisplayKeys(valNode); err != nil {
					return err
				}
			}
		default:
			return fmt.Errorf("line %d: unknown or disallowed top-level key '%s'", keyNode.Line, key)
		}
	}
	return nil
}

// validateVoiceKeys rejects unknown keys under display.voice and display.voice.waveform,
// so a typo such as levles_url fails validation instead of being silently ignored.
func validateVoiceKeys(voiceNode *yaml.Node) error {
	if voiceNode.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(voiceNode.Content); i += 2 {
		keyNode, valNode := voiceNode.Content[i], voiceNode.Content[i+1]
		if keyNode.Value != "waveform" {
			return fmt.Errorf("line %d: unknown key '%s' under display.voice", keyNode.Line, keyNode.Value)
		}
		if valNode.Kind != yaml.MappingNode {
			continue
		}
		for j := 0; j+1 < len(valNode.Content); j += 2 {
			wk := valNode.Content[j]
			switch wk.Value {
			case "enabled", "levels_url", "gain":
			default:
				return fmt.Errorf("line %d: unknown key '%s' under display.voice.waveform", wk.Line, wk.Value)
			}
		}
	}
	return nil
}

func validateDisplayKeys(displayNode *yaml.Node) error {
	for i := 0; i < len(displayNode.Content); i += 2 {
		keyNode := displayNode.Content[i]
		switch key := keyNode.Value; key {
		case "screens":
			return fmt.Errorf("line %d: display contains disallowed key 'screens': screens are dynamically computed by the 6x2 layout solver", keyNode.Line)
		case "providers":
			return fmt.Errorf("line %d: display contains disallowed key 'providers': widget instances configure their own data sources under display.widgets", keyNode.Line)
		case "voice":
			if err := validateVoiceKeys(displayNode.Content[i+1]); err != nil {
				return err
			}
		case "rotation", "grid", "header", "widgets", "hide_cursor", "remote_url", "theme", "ui", "allow_layout_edit", "layout_override_path":
		default:
			return fmt.Errorf("line %d: unknown key '%s' under display", keyNode.Line, key)
		}
	}
	return nil
}

func (c *Config) applyWeatherForecastDefaults(lat, lon float64, name string) {
	for i := range c.Display.Widgets {
		w := &c.Display.Widgets[i]
		if w.Type != "weather-forecast" {
			continue
		}
		if w.Config == nil {
			w.Config = make(map[string]any)
		}
		if w.Config["latitude"] == nil {
			w.Config["latitude"] = lat
		}
		if w.Config["longitude"] == nil {
			w.Config["longitude"] = lon
		}
		if w.Config["name"] == nil && w.Config["location_name"] == nil && name != "" {
			w.Config["location_name"] = name
		}
	}
}

func (c *Config) applyLocationDefaults() {
	lc := c.LocationConfig
	if lc != nil && lc.Latitude != nil && lc.Longitude != nil {
		c.applyConfiguredLocation(*lc.Latitude, *lc.Longitude, lc.Name)
		return
	}

	hw := c.Display.Header.Weather
	if hw == nil || (hw.Latitude == 0 && hw.Longitude == 0) {
		return
	}

	lat, lon, name := hw.Latitude, hw.Longitude, hw.Location
	if c.LocationConfig == nil {
		c.LocationConfig = &LocationConfig{Name: name, Latitude: &lat, Longitude: &lon}
		return
	}
	if c.LocationConfig.Latitude == nil {
		c.LocationConfig.Latitude = &lat
	}
	if c.LocationConfig.Longitude == nil {
		c.LocationConfig.Longitude = &lon
	}
	if c.LocationConfig.Name == "" {
		c.LocationConfig.Name = name
	}
}

func (c *Config) applyConfiguredLocation(lat, lon float64, name string) {
	if hw := c.Display.Header.Weather; hw != nil {
		if hw.Latitude == 0 && hw.Longitude == 0 {
			hw.Latitude = lat
			hw.Longitude = lon
		}
		if hw.Location == "" && name != "" {
			hw.Location = name
		}
	}
	c.applyWeatherForecastDefaults(lat, lon, name)
}

func (c *Config) applyHeaderAndThemeDefaults() {
	if hw := c.Display.Header.Weather; hw != nil {
		if hw.Units == "" {
			hw.Units = "imperial"
		}
		if hw.RefreshIntervalSeconds == nil {
			defaultInterval := 900
			hw.RefreshIntervalSeconds = &defaultInterval
		}
	}

	if c.Display.Theme == nil {
		c.Display.Theme = &ThemeConfig{Mode: "day"}
	} else if c.Display.Theme.Mode == "" {
		c.Display.Theme.Mode = "day"
	}

	if c.Display.Header.Elements == nil {
		c.Display.Header.Elements = DefaultHeaderElements()
	}
}

func (c *Config) applyDefaults() {
	if c.Display.Rotation.Transition == "" {
		c.Display.Rotation.Transition = "slide"
	}
	if c.Display.Grid.Columns == 0 {
		c.Display.Grid.Columns = 6
	}
	if c.Display.Grid.Rows == 0 {
		c.Display.Grid.Rows = 2
	}

	c.applyLocationDefaults()
	c.applyHeaderAndThemeDefaults()
}

func (c *Config) validateTheme() error {
	tc := c.Display.Theme
	if tc == nil {
		return nil
	}
	mode := tc.GetMode()
	switch mode {
	case "auto", "day", "night":
	default:
		return fmt.Errorf("invalid theme mode '%s': must be one of auto, day, night", tc.Mode)
	}
	if mode == "auto" {
		locCfg := c.GetLocationConfig()
		if locCfg == nil || locCfg.Latitude == nil || locCfg.Longitude == nil {
			return fmt.Errorf("theme mode 'auto' requires location coordinates (latitude and longitude) to be configured")
		}
	}
	return nil
}

func (c *Config) validateUI() error {
	switch ui := c.Display.GetUI(); ui {
	case "legacy", "react":
		return nil
	default:
		return fmt.Errorf("invalid display ui '%s': must be one of legacy, react", c.Display.UI)
	}
}

func (c *Config) validateHeaderWeather() error {
	hw := c.Display.Header.Weather
	if hw == nil {
		return nil
	}
	if err := checkPositiveInt(hw.RefreshIntervalSeconds, "header weather refresh_interval_seconds"); err != nil {
		return err
	}
	if err := checkLatLon(hw.Latitude, hw.Longitude, "header weather"); err != nil {
		return err
	}
	switch strings.ToLower(hw.Units) {
	case "imperial", "metric":
		return nil
	default:
		return fmt.Errorf("invalid header weather units '%s': must be 'imperial' or 'metric'", hw.Units)
	}
}

func (c *Config) validateDisplayVoice() error {
	v := c.Display.Voice
	if v == nil || v.Waveform == nil {
		return nil
	}
	vw := v.Waveform
	if strings.TrimSpace(vw.LevelsURL) != "" && !isValidHTTPURL(vw.LevelsURL) {
		return fmt.Errorf("display voice waveform levels_url must be an http(s) URL (got %q)", vw.LevelsURL)
	}
	if vw.Gain != nil && !(*vw.Gain > 0) {
		return fmt.Errorf("display voice waveform gain must be positive (got %v)", *vw.Gain)
	}
	return nil
}

func (c *Config) validateDisplayAndHeader() error {
	if err := validateGridAndWidgetDimensions(c.Display.Grid, "", nil); err != nil {
		return err
	}

	if err := checkNonNegativeInt(c.Display.Rotation.IntervalSeconds, "rotation interval_seconds"); err != nil {
		return err
	}
	if err := checkNonNegativeInt(c.Display.Rotation.PauseDurationSeconds, "rotation pause_duration_seconds"); err != nil {
		return err
	}
	switch c.Display.Rotation.Transition {
	case "slide", "fade", "instant", "none":
	default:
		return fmt.Errorf("invalid rotation transition '%s': must be one of slide, fade, instant, none", c.Display.Rotation.Transition)
	}

	if lc := c.LocationConfig; lc != nil && (lc.Latitude != nil || lc.Longitude != nil) {
		if err := checkLatLon(derefOr(lc.Latitude, 0), derefOr(lc.Longitude, 0), "location"); err != nil {
			return err
		}
	}

	if err := c.validateTheme(); err != nil {
		return err
	}
	if err := c.validateUI(); err != nil {
		return err
	}
	if err := c.validateHeaderWeather(); err != nil {
		return err
	}
	return c.validateDisplayVoice()
}

func (c *Config) validateWidgets() error {
	seenIDs := make(map[string]int, len(c.Display.Widgets))
	for i := range c.Display.Widgets {
		w := &c.Display.Widgets[i]
		if strings.TrimSpace(w.ID) == "" {
			return fmt.Errorf("widget at index %d is missing required field 'id'", i)
		}
		if prevIdx, exists := seenIDs[w.ID]; exists {
			return fmt.Errorf("duplicate widget id '%s' found in display.widgets (indices %d and %d)", w.ID, prevIdx, i)
		}
		seenIDs[w.ID] = i

		if strings.TrimSpace(w.Type) == "" {
			return fmt.Errorf("widget '%s' is missing required field 'type'", w.ID)
		}

		if err := validateGridAndWidgetDimensions(c.Display.Grid, w.ID, w.Dimensions); err != nil {
			return err
		}

		if err := checkPositiveInt(w.RefreshIntervalSeconds, fmt.Sprintf("widget '%s': refresh_interval_seconds", w.ID)); err != nil {
			return err
		}

		if w.Method != "" {
			upper := strings.ToUpper(strings.TrimSpace(w.Method))
			if upper != "GET" && upper != "POST" {
				return fmt.Errorf("widget '%s': invalid http method '%s': must be GET or POST", w.ID, w.Method)
			}
			w.Method = upper
		} else if w.Endpoint != "" {
			w.Method = "POST"
		}

		if w.Endpoint != "" && !isValidHTTPURL(w.Endpoint) {
			return fmt.Errorf("widget '%s': invalid endpoint URL '%s' (must be http:// or https:// with host)", w.ID, w.Endpoint)
		}
	}
	return nil
}

func validateVoiceHubFastPath(fp *FastPathConfig) error {
	if fp == nil {
		return nil
	}
	if err := checkPositiveInt(fp.TimeoutMS, "voice_hub fast_path timeout_ms"); err != nil {
		return err
	}
	if fp.IsEnabled() && !isValidHTTPURL(fp.URL) {
		return fmt.Errorf("voice_hub fast_path url must be an http(s) URL (got %q)", fp.URL)
	}
	return nil
}

func (c *Config) validateVoiceAndChatLog() error {
	if cl := c.ChatLog; cl != nil {
		if err := checkPositiveInt(cl.TTLMinutes, "chat_log ttl_minutes"); err != nil {
			return err
		}
		if err := checkPositiveInt(cl.MaxMessagesPerNode, "chat_log max_messages_per_node"); err != nil {
			return err
		}
	}

	vh := c.VoiceHub
	if vh == nil || !vh.Enabled {
		return nil
	}
	if err := checkPositiveInt(vh.BrainTimeoutSeconds, "voice_hub brain_timeout_seconds"); err != nil {
		return err
	}
	if err := checkPositiveInt(vh.TTSTimeoutSeconds, "voice_hub tts_timeout_seconds"); err != nil {
		return err
	}
	if err := validateVoiceHubFastPath(vh.FastPath); err != nil {
		return err
	}
	return validateSpeakerID(vh.SpeakerID)
}

// Validate checks the semantic correctness of the parsed configuration.
func (c *Config) Validate() error {
	// 1. Timezone
	if strings.TrimSpace(c.Timezone) == "" {
		return fmt.Errorf("missing required configuration field 'timezone'")
	}
	loc, err := time.LoadLocation(c.Timezone)
	if err != nil {
		return fmt.Errorf("invalid timezone '%s': %w", c.Timezone, err)
	}
	c.location = loc

	// 2. Display, Header, Location, Theme
	if err := c.validateDisplayAndHeader(); err != nil {
		return err
	}

	// 3. Widgets
	if err := c.validateWidgets(); err != nil {
		return err
	}

	// 4. Voice and ChatLog
	return c.validateVoiceAndChatLog()
}

func validateSpeakerVector(id string, vec []float64) error {
	if len(vec) == 0 {
		return fmt.Errorf("voice_hub speaker_id speaker %q has an empty embedding", id)
	}
	var norm float64
	for _, x := range vec {
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return fmt.Errorf("voice_hub speaker_id speaker %q: embedding contains NaN or Inf", id)
		}
		norm += x * x
	}
	if norm == 0 {
		return fmt.Errorf("voice_hub speaker_id speaker %q: embedding has zero magnitude", id)
	}
	return nil
}

func validateSpeakerID(sid *SpeakerIDConfig) error {
	if sid == nil {
		return nil
	}
	if strings.TrimSpace(sid.EmbedURL) == "" {
		return fmt.Errorf("voice_hub speaker_id requires embed_url")
	}
	if sid.Threshold != nil && (*sid.Threshold <= 0 || *sid.Threshold > 1) {
		return fmt.Errorf("voice_hub speaker_id threshold must be in (0, 1] (got %v)", *sid.Threshold)
	}
	if err := checkPositiveInt(sid.TimeoutSeconds, "voice_hub speaker_id timeout_seconds"); err != nil {
		return err
	}
	seen := make(map[string]bool, len(sid.Speakers))
	dim := 0
	for i, sp := range sid.Speakers {
		id := strings.TrimSpace(sp.ID)
		if id == "" {
			return fmt.Errorf("voice_hub speaker_id speaker %d has an empty id", i)
		}
		if seen[id] {
			return fmt.Errorf("voice_hub speaker_id duplicate speaker id %q", id)
		}
		seen[id] = true
		if err := validateSpeakerVector(id, sp.Embedding); err != nil {
			return err
		}
		if dim == 0 {
			dim = len(sp.Embedding)
		} else if len(sp.Embedding) != dim {
			return fmt.Errorf("voice_hub speaker_id speaker %q has dimension %d, expected %d", id, len(sp.Embedding), dim)
		}
	}
	return nil
}

// ResolveEnv resolves secret environment variables for all widget instances into isolated Secrets maps.
func (c *Config) ResolveEnv(getenv func(string) string) error {
	if getenv == nil {
		getenv = os.Getenv
	}

	for i := range c.Display.Widgets {
		w := &c.Display.Widgets[i]
		if w.Secrets == nil {
			w.Secrets = make(map[string]string)
		}

		if w.TokenEnv != "" {
			envVar := strings.TrimSpace(w.TokenEnv)
			val := getenv(envVar)
			if val == "" {
				return fmt.Errorf("widget '%s': environment variable '%s' defined in token_env is unset or empty", w.ID, envVar)
			}
			w.Token = val
			w.Secrets["token"] = val
		}

		if w.Config != nil {
			if err := resolveEnvInMap(w.ID, w.Config, "", w.Secrets, getenv); err != nil {
				return err
			}
		}
	}

	return nil
}

func resolveEnvInValue(widgetID string, v any, prefix string, secrets map[string]string, getenv func(string) string) (any, error) {
	switch val := v.(type) {
	case map[string]any:
		if err := resolveEnvInMap(widgetID, val, prefix, secrets, getenv); err != nil {
			return nil, err
		}
		return val, nil
	case map[any]any:
		converted := make(map[string]any, len(val))
		for subK, subV := range val {
			converted[fmt.Sprint(subK)] = subV
		}
		if err := resolveEnvInMap(widgetID, converted, prefix, secrets, getenv); err != nil {
			return nil, err
		}
		return converted, nil
	case []any:
		if err := resolveEnvInSlice(widgetID, val, prefix, secrets, getenv); err != nil {
			return nil, err
		}
		return val, nil
	default:
		return v, nil
	}
}

func resolveEnvInMap(widgetID string, m map[string]any, prefix string, secrets map[string]string, getenv func(string) string) error {
	for k, v := range m {
		if before, ok := strings.CutSuffix(k, "_env"); ok {
			baseKey := before
			if _, exists := m[baseKey]; exists {
				return fmt.Errorf("widget '%s': cannot specify both '%s' and '%s'", widgetID, baseKey, k)
			}

			envVarName, ok := v.(string)
			if !ok || strings.TrimSpace(envVarName) == "" {
				return fmt.Errorf("widget '%s': key '%s' must specify a non-empty environment variable name", widgetID, k)
			}
			envVarName = strings.TrimSpace(envVarName)
			val := getenv(envVarName)
			if val == "" {
				return fmt.Errorf("widget '%s': environment variable '%s' defined in %s is unset or empty", widgetID, envVarName, k)
			}

			secretPath := baseKey
			if prefix != "" {
				secretPath = prefix + "." + baseKey
			}
			secrets[secretPath] = val
		}

		childPrefix := k
		if prefix != "" {
			childPrefix = prefix + "." + k
		}

		updated, err := resolveEnvInValue(widgetID, v, childPrefix, secrets, getenv)
		if err != nil {
			return err
		}
		m[k] = updated
	}
	return nil
}

func resolveEnvInSlice(widgetID string, s []any, prefix string, secrets map[string]string, getenv func(string) string) error {
	for i, item := range s {
		childPrefix := fmt.Sprintf("%s[%d]", prefix, i)
		updated, err := resolveEnvInValue(widgetID, item, childPrefix, secrets, getenv)
		if err != nil {
			return err
		}
		s[i] = updated
	}
	return nil
}
