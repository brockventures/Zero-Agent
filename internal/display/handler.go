package display

import (
	"bytes"
	"encoding/json"
	"html/template"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/azylman/mirrormere/internal/api"
)

const (
	// DefaultContainerTemplatePath is the standard container path for the dashboard HTML shell.
	DefaultContainerTemplatePath = "/app/web/templates/display.html"
	// DefaultContainerStaticDir is the standard container path for static web assets.
	DefaultContainerStaticDir = "/app/web/static"
	// LocalFallbackTemplatePath is the relative development path for display.html.
	LocalFallbackTemplatePath = "web/templates/display.html"
	// LocalFallbackStaticDir is the relative development path for static web assets.
	LocalFallbackStaticDir = "web/static"
	// DefaultContainerReactTemplatePath is the container path for the React display shell (?ui=react).
	DefaultContainerReactTemplatePath = "/app/web/templates/display_react.html"
	// LocalFallbackReactTemplatePath is the relative development path for the React display shell.
	LocalFallbackReactTemplatePath = "web/templates/display_react.html"
)

// HandlerOption configures Handler instances.
type HandlerOption func(*Handler)

func WithTimezoneProvider(provider func() string) HandlerOption {
	return func(h *Handler) { h.timezoneProvider = provider }
}

func WithHideCursorProvider(provider func() bool) HandlerOption {
	return func(h *Handler) { h.hideCursorProvider = provider }
}

func WithRemoteURLProvider(provider func() string) HandlerOption {
	return func(h *Handler) { h.remoteURLProvider = provider }
}

func WithHeaderVideoProvider(p func() *HeaderVideoTemplateData) HandlerOption {
	return func(h *Handler) { h.headerVideoProvider = p }
}

func WithVoiceWaveformProvider(p func() *VoiceWaveformTemplateData) HandlerOption {
	return func(h *Handler) { h.voiceWaveformProvider = p }
}

func WithThemeProvider(provider func() string) HandlerOption {
	return func(h *Handler) { h.themeProvider = provider }
}

func WithGridProvider(provider func() string) HandlerOption {
	return func(h *Handler) { h.gridProvider = provider }
}

// WithUIProvider supplies display.ui from config ("legacy" or "react"). ?ui= on the request overrides it.
func WithUIProvider(provider func() string) HandlerOption {
	return func(h *Handler) { h.uiProvider = provider }
}

// Handler serves the web display HUD shell and static web runtime assets.
type Handler struct {
	templatePath           string
	localTemplatePath      string
	reactTemplatePath      string
	localReactTemplatePath string
	staticDir              string
	localStaticDir         string
	templateContent        []byte
	timezoneProvider       func() string
	hideCursorProvider     func() bool
	remoteURLProvider      func() string
	headerVideoProvider    func() *HeaderVideoTemplateData
	voiceWaveformProvider  func() *VoiceWaveformTemplateData
	themeProvider          func() string
	gridProvider           func() string
	uiProvider             func() string
}

func (h *Handler) Timezone() string {
	if h.timezoneProvider != nil {
		if tz := h.timezoneProvider(); tz != "" {
			return tz
		}
	}
	return "UTC"
}

func (h *Handler) HideCursor() bool {
	if h.hideCursorProvider != nil {
		return h.hideCursorProvider()
	}
	return false
}

func (h *Handler) RemoteURL() string {
	if h.remoteURLProvider != nil {
		if url := h.remoteURLProvider(); url != "" {
			return url
		}
	}
	return ""
}

func (h *Handler) HeaderVideo() *HeaderVideoTemplateData {
	if h.headerVideoProvider != nil {
		return h.headerVideoProvider()
	}
	return nil
}

func (h *Handler) VoiceWaveform() *VoiceWaveformTemplateData {
	if h.voiceWaveformProvider != nil {
		return h.voiceWaveformProvider()
	}
	return nil
}

func (h *Handler) Theme() string {
	if h.themeProvider != nil {
		if t := h.themeProvider(); t != "" {
			return t
		}
	}
	return "day"
}

// UI returns the configured display shell, "react" unless the config provider says "legacy".
func (h *Handler) UI() string {
	if h.uiProvider != nil && h.uiProvider() == "legacy" {
		return "legacy"
	}
	return "react"
}

// useReact reports whether the request should get the React shell: ?ui= wins when it is
// "react" or "legacy"; any other value falls through to the configured default.
func (h *Handler) useReact(r *http.Request) bool {
	switch r.URL.Query().Get("ui") {
	case "react":
		return true
	case "legacy":
		return false
	}
	return h.UI() == "react"
}

func (h *Handler) Grid() string {
	if h.gridProvider != nil {
		if g := h.gridProvider(); g == "3x6" || g == "6x2" {
			return g
		}
	}
	return "6x2"
}

// HeaderVideoTemplateData holds parameters for the fullscreen video TV button in the header.
type HeaderVideoTemplateData struct {
	Enabled    bool
	StreamID   string
	StreamURL  string
	ControlURL string
	Type       string
}

// VoiceWaveformTemplateData holds the voice waveform mouth parameters for the display template.
type VoiceWaveformTemplateData struct {
	Enabled   bool
	LevelsURL string
	Gain      float64
}

// DisplayTemplateData holds context parameters passed to display.html template.
type DisplayTemplateData struct {
	Timezone      string
	HideCursor    bool
	RemoteURL     string
	HeaderVideo   *HeaderVideoTemplateData
	VoiceWaveform *VoiceWaveformTemplateData
	Theme         string
	Grid          string
	// Perf loads the client-side performance instrumentation (static/js/perf.js) when the page is requested with ?perf=1.
	Perf bool
}

// NewHandler constructs a Handler with default filesystem paths.
func NewHandler(opts ...HandlerOption) *Handler {
	h := &Handler{
		templatePath:           DefaultContainerTemplatePath,
		localTemplatePath:      LocalFallbackTemplatePath,
		reactTemplatePath:      DefaultContainerReactTemplatePath,
		localReactTemplatePath: LocalFallbackReactTemplatePath,
		staticDir:              DefaultContainerStaticDir,
		localStaticDir:         LocalFallbackStaticDir,
	}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

func checkGetOrHead(w http.ResponseWriter, r *http.Request) bool {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return false
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD, OPTIONS")
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
		return false
	}
	return true
}

func readFile(p string) ([]byte, bool) {
	if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
		if data, err := os.ReadFile(p); err == nil {
			return data, true
		}
	}
	return nil, false
}

func (h *Handler) resolveTemplateBytes(react bool) ([]byte, bool) {
	container, local := h.templatePath, h.localTemplatePath
	if react {
		container, local = h.reactTemplatePath, h.localReactTemplatePath
	}
	if data, ok := readFile(container); ok {
		return data, true
	}
	// Inline content is the legacy display.html only; it never stands in for the React shell.
	if !react && len(h.templateContent) > 0 {
		return h.templateContent, true
	}
	for _, c := range []string{local, filepath.Join("..", "..", local)} {
		if data, ok := readFile(c); ok {
			return data, true
		}
	}
	return nil, false
}

// GetDisplay handles GET /display requests, serving display.html with household timezone context.
func (h *Handler) GetDisplay(w http.ResponseWriter, r *http.Request) {
	if !checkGetOrHead(w, r) {
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "public, max-age=0, must-revalidate")

	react := h.useReact(r)
	raw, found := h.resolveTemplateBytes(react)
	if !found {
		writeJSONError(w, http.StatusNotFound, "display template not found")
		return
	}

	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}

	out := raw
	if tmpl, err := template.New("display.html").Parse(string(raw)); err == nil {
		hideCursor := h.HideCursor()
		if q := r.URL.Query().Get("hide_cursor"); q == "true" {
			hideCursor = true
		} else if q == "false" {
			hideCursor = false
		}
		var buf bytes.Buffer
		if err := tmpl.Execute(&buf, DisplayTemplateData{
			Timezone:      h.Timezone(),
			HideCursor:    hideCursor,
			RemoteURL:     h.RemoteURL(),
			HeaderVideo:   h.HeaderVideo(),
			VoiceWaveform: h.VoiceWaveform(),
			Theme:         h.Theme(),
			Grid:          h.Grid(),
			Perf:          r.URL.Query().Get("perf") == "1",
		}); err == nil {
			out = buf.Bytes()
		}
	}

	if _, err := w.Write(out); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}

func tryServeStaticFile(w http.ResponseWriter, r *http.Request, baseDir, clean string) bool {
	if baseDir == "" {
		return false
	}
	fullPath := filepath.Join(baseDir, clean)
	if rel, err := filepath.Rel(baseDir, fullPath); err == nil && !strings.HasPrefix(rel, "..") {
		if fi, err := os.Stat(fullPath); err == nil && !fi.IsDir() {
			w.Header().Set("Content-Type", detectContentType(clean))
			w.Header().Set("Cache-Control", "public, max-age=0, must-revalidate")
			http.ServeFile(w, r, fullPath)
			return true
		}
	}
	return false
}

// GetStatic handles GET /static/{path...} requests with strict path traversal defenses.
func (h *Handler) GetStatic(w http.ResponseWriter, r *http.Request, assetPath string) {
	if !checkGetOrHead(w, r) {
		return
	}

	clean := filepath.Clean(assetPath)
	if assetPath == "" || strings.Contains(assetPath, "..") || strings.HasPrefix(assetPath, ".") || filepath.IsAbs(assetPath) ||
		clean == "." || strings.HasPrefix(clean, "..") || strings.HasPrefix(clean, "/") || strings.HasPrefix(clean, "\\") {
		writeJSONError(w, http.StatusBadRequest, "invalid asset path: path traversal detected")
		return
	}

	if filepath.ToSlash(clean) == "js/calendar_grid.js" {
		clean = filepath.FromSlash("js/calendar_family.js")
	}

	for _, baseDir := range []string{h.staticDir, h.localStaticDir, filepath.Join("..", "..", h.localStaticDir)} {
		if tryServeStaticFile(w, r, baseDir, clean) {
			return
		}
	}

	writeJSONError(w, http.StatusNotFound, "static asset not found")
}

var contentTypes = map[string]string{
	".js":    "text/javascript; charset=utf-8",
	".css":   "text/css; charset=utf-8",
	".svg":   "image/svg+xml",
	".json":  "application/json; charset=utf-8",
	".png":   "image/png",
	".jpg":   "image/jpeg",
	".jpeg":  "image/jpeg",
	".webp":  "image/webp",
	".woff2": "font/woff2",
	".html":  "text/html; charset=utf-8",
}

func detectContentType(path string) string {
	ext := strings.ToLower(filepath.Ext(path))
	if ct, ok := contentTypes[ext]; ok {
		return ct
	}
	if t := mime.TypeByExtension(ext); t != "" {
		return t
	}
	return "application/octet-stream"
}

func writeJSONError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(api.ErrorResponse{
		Status: "error",
		Error:  msg,
	}); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
	}
}
