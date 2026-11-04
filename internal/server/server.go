package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"runtime/coverage"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/covflush"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const (
	// DefaultPort is the standard HTTP port for mirrormere daemon.
	DefaultPort = 8080
	// DefaultHost is the standard bind host.
	DefaultHost = "0.0.0.0"
	// DefaultReadTimeout covers request body reading.
	DefaultReadTimeout = 5 * time.Second
	// DefaultReadHeaderTimeout mitigates Slowloris attacks.
	DefaultReadHeaderTimeout = 3 * time.Second
	// DefaultWriteTimeout covers response transmission.
	DefaultWriteTimeout = 10 * time.Second
	// DefaultIdleTimeout handles keep-alive connections.
	DefaultIdleTimeout = 120 * time.Second

	// Version of the mirrormere service.
	Version = "0.1.0"
)

// ScreenHandler handles screen navigation and pause endpoints.
type ScreenHandler interface {
	PostScreenAdvance(w http.ResponseWriter, r *http.Request)
	PostScreenPause(w http.ResponseWriter, r *http.Request)
	PostScreenSelect(w http.ResponseWriter, r *http.Request)
	GetScreens(w http.ResponseWriter, r *http.Request)
}

// RenderHandler handles widget rendering, package static assets, and stylesheet endpoints.
type RenderHandler interface {
	GetWidgetRender(w http.ResponseWriter, r *http.Request, widgetID string)
	GetWidgetJSON(w http.ResponseWriter, r *http.Request, widgetID string)
	GetWidgetAsset(w http.ResponseWriter, r *http.Request, widgetType string, assetPath string)
	ServeStyle(w http.ResponseWriter, r *http.Request)
}

// PushHandler handles realtime widget push webhooks.
type PushHandler interface {
	PostWidgetPush(w http.ResponseWriter, r *http.Request, widgetID string)
	PostWidgetPhotos(w http.ResponseWriter, r *http.Request, widgetID string)
}

// DisplayHandler handles display UI and static web runtime asset endpoints.
type DisplayHandler interface {
	GetDisplay(w http.ResponseWriter, r *http.Request)
	GetStatic(w http.ResponseWriter, r *http.Request, path string)
}

// ListsHandler handles task completion endpoints.
type ListsHandler interface {
	CompleteListItem(w http.ResponseWriter, r *http.Request, listID, itemID string)
}

// AudioHandler handles master audio volume and mute endpoints.
type AudioHandler interface {
	GetAudio(w http.ResponseWriter, r *http.Request)
	PostAudioVolume(w http.ResponseWriter, r *http.Request)
	PostAudioMute(w http.ResponseWriter, r *http.Request)
}

// VideoHandler handles video stream priority stack and transport manipulation endpoints.
type VideoHandler interface {
	PostVideoTrigger(w http.ResponseWriter, r *http.Request)
	PostVideoDismiss(w http.ResponseWriter, r *http.Request)
	PostVideoAction(w http.ResponseWriter, r *http.Request)
	PostVideoState(w http.ResponseWriter, r *http.Request)
	GetVideoState(w http.ResponseWriter, r *http.Request)
}

// VoiceHandler handles voice pipeline state relay endpoints.
type VoiceHandler interface {
	PostVoiceState(w http.ResponseWriter, r *http.Request)
	PostVoiceInteract(w http.ResponseWriter, r *http.Request)
	PostVoiceHeartbeat(w http.ResponseWriter, r *http.Request)
}

// WidgetActionHandler handles widget action execution endpoints matching Mirrormere specifications.
type WidgetActionHandler interface {
	PostWidgetAction(w http.ResponseWriter, r *http.Request, widgetID string)
}

// LayoutEditorHandler handles live canvas layout editor override operations.
type LayoutEditorHandler interface {
	GetLayoutEditor(w http.ResponseWriter, r *http.Request)
	PutLayoutEditor(w http.ResponseWriter, r *http.Request)
	DeleteLayoutEditor(w http.ResponseWriter, r *http.Request)
}

// Config encapsulates configuration for the HTTP server.
type Config struct {
	Host                                                      string
	Port                                                      int
	ReadTimeout, ReadHeaderTimeout, WriteTimeout, IdleTimeout time.Duration
	ActionHandler                                             WidgetActionHandler
	AudioHandler                                              AudioHandler
	DisplayHandler                                            DisplayHandler
	EventsHandler                                             http.Handler
	LayoutEditorHandler                                       LayoutEditorHandler
	ListsHandler                                              ListsHandler
	PushHandler                                               PushHandler
	RenderHandler                                             RenderHandler
	ScreenHandler                                             ScreenHandler
	VideoHandler                                              VideoHandler
	VoiceHandler                                              VoiceHandler
	WidgetActionHandler                                       WidgetActionHandler
}

// ApplyDefaults sets fallback values for any unspecified configuration fields.
// If Port is negative (e.g. -1), it is mapped to 0 (ephemeral port).
// If Port is 0, it defaults to DefaultPort (8080).
func (c *Config) ApplyDefaults() {
	if c.Host == "" {
		c.Host = DefaultHost
	}
	if c.Port < 0 {
		c.Port = 0
	} else if c.Port == 0 {
		c.Port = DefaultPort
	}
	if c.ReadTimeout <= 0 {
		c.ReadTimeout = DefaultReadTimeout
	}
	if c.ReadHeaderTimeout <= 0 {
		c.ReadHeaderTimeout = DefaultReadHeaderTimeout
	}
	if c.WriteTimeout <= 0 {
		c.WriteTimeout = DefaultWriteTimeout
	}
	if c.IdleTimeout <= 0 {
		c.IdleTimeout = DefaultIdleTimeout
	}
}

// HealthResponse represents the payload returned by healthcheck probes.
type HealthResponse struct {
	Status string `json:"status"`
}

// RootResponse represents the service identification payload.
type RootResponse struct {
	Service string `json:"service"`
	Status  string `json:"status"`
	Version string `json:"version"`
}

// Server wraps http.Server with lifecycle signaling and healthcheck routing.
type Server struct {
	cfg                 Config
	mux                 *http.ServeMux
	httpServer          *http.Server
	ready               chan struct{}
	readyOnce           sync.Once
	mu                  sync.RWMutex
	addr                string
	audioHandler        AudioHandler
	displayHandler      DisplayHandler
	eventsHandler       http.Handler
	layoutEditorHandler LayoutEditorHandler
	listsHandler        ListsHandler
	pushHandler         PushHandler
	renderHandler       RenderHandler
	screenHandler       ScreenHandler
	videoHandler        VideoHandler
	voiceHandler        VoiceHandler
	widgetActionHandler WidgetActionHandler
}

// New constructs a configured Server instance.
func New(cfg Config) *Server {
	cfg.ApplyDefaults()
	actionHandler := cfg.WidgetActionHandler
	if actionHandler == nil {
		actionHandler = cfg.ActionHandler
	}
	s := &Server{
		cfg: cfg, mux: http.NewServeMux(), ready: make(chan struct{}),
		audioHandler:        cfg.AudioHandler,
		displayHandler:      cfg.DisplayHandler,
		eventsHandler:       cfg.EventsHandler,
		layoutEditorHandler: cfg.LayoutEditorHandler,
		listsHandler:        cfg.ListsHandler,
		pushHandler:         cfg.PushHandler,
		renderHandler:       cfg.RenderHandler,
		screenHandler:       cfg.ScreenHandler,
		videoHandler:        cfg.VideoHandler,
		voiceHandler:        cfg.VoiceHandler,
		widgetActionHandler: actionHandler,
	}
	s.setupRoutes()
	s.httpServer = &http.Server{
		Handler:           s.mux,
		ReadTimeout:       s.cfg.ReadTimeout,
		ReadHeaderTimeout: s.cfg.ReadHeaderTimeout,
		WriteTimeout:      s.cfg.WriteTimeout,
		IdleTimeout:       s.cfg.IdleTimeout,
	}
	return s
}

func isNil[H any](h H) bool { return any(h) == nil }

func dispatchHandler[H any](s *Server, get func() H, isNil func(H) bool, fn func(H, http.ResponseWriter, *http.Request)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s.mu.RLock()
		h := get()
		s.mu.RUnlock()
		if isNil(h) {
			http.NotFound(w, r)
			return
		}
		fn(h, w, r)
	}
}

func (s *Server) setupRoutes() {
	s.mux.HandleFunc("/", s.handleRoot)
	s.mux.HandleFunc("/api/audio", dispatchHandler(s, func() AudioHandler { return s.audioHandler }, isNil, func(h AudioHandler, w http.ResponseWriter, r *http.Request) { h.GetAudio(w, r) }))
	s.mux.HandleFunc("/api/audio/mute", dispatchHandler(s, func() AudioHandler { return s.audioHandler }, isNil, func(h AudioHandler, w http.ResponseWriter, r *http.Request) { h.PostAudioMute(w, r) }))
	s.mux.HandleFunc("/api/audio/volume", dispatchHandler(s, func() AudioHandler { return s.audioHandler }, isNil, func(h AudioHandler, w http.ResponseWriter, r *http.Request) { h.PostAudioVolume(w, r) }))
	s.mux.HandleFunc("/api/events", dispatchHandler(s, func() http.Handler { return s.eventsHandler }, isNil, func(h http.Handler, w http.ResponseWriter, r *http.Request) { h.ServeHTTP(w, r) }))
	s.mux.HandleFunc("/api/layout/editor", dispatchHandler(s, func() LayoutEditorHandler { return s.layoutEditorHandler }, isNil, func(h LayoutEditorHandler, w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodHead:
			h.GetLayoutEditor(w, r)
		case http.MethodPut:
			h.PutLayoutEditor(w, r)
		case http.MethodDelete:
			h.DeleteLayoutEditor(w, r)
		case http.MethodOptions:
			w.Header().Set("Access-Control-Allow-Origin", "*")
			w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, PUT, DELETE, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Accept")
			w.WriteHeader(http.StatusNoContent)
		default:
			w.Header().Set("Allow", "GET, HEAD, PUT, DELETE, OPTIONS")
			writeAPIError(w, http.StatusMethodNotAllowed, "method not allowed")
		}
	}))
	s.mux.HandleFunc("/api/lists/{list_id}/items/{item_id}/complete", dispatchHandler(s, func() ListsHandler { return s.listsHandler }, isNil, func(h ListsHandler, w http.ResponseWriter, r *http.Request) {
		h.CompleteListItem(w, r, r.PathValue("list_id"), r.PathValue("item_id"))
	}))
	s.mux.HandleFunc("/api/perf", handlePerf(defaultPerfHistogramsOnce()))
	s.mux.HandleFunc("/api/screen/advance", dispatchHandler(s, func() ScreenHandler { return s.screenHandler }, isNil, func(h ScreenHandler, w http.ResponseWriter, r *http.Request) { h.PostScreenAdvance(w, r) }))
	s.mux.HandleFunc("/api/screen/pause", dispatchHandler(s, func() ScreenHandler { return s.screenHandler }, isNil, func(h ScreenHandler, w http.ResponseWriter, r *http.Request) { h.PostScreenPause(w, r) }))
	s.mux.HandleFunc("/api/screen/select", dispatchHandler(s, func() ScreenHandler { return s.screenHandler }, isNil, func(h ScreenHandler, w http.ResponseWriter, r *http.Request) { h.PostScreenSelect(w, r) }))
	s.mux.HandleFunc("/api/screens", dispatchHandler(s, func() ScreenHandler { return s.screenHandler }, isNil, func(h ScreenHandler, w http.ResponseWriter, r *http.Request) { h.GetScreens(w, r) }))
	s.mux.HandleFunc("/api/video/action", dispatchHandler(s, func() VideoHandler { return s.videoHandler }, isNil, func(h VideoHandler, w http.ResponseWriter, r *http.Request) { h.PostVideoAction(w, r) }))
	s.mux.HandleFunc("/api/video/dismiss", dispatchHandler(s, func() VideoHandler { return s.videoHandler }, isNil, func(h VideoHandler, w http.ResponseWriter, r *http.Request) { h.PostVideoDismiss(w, r) }))
	s.mux.HandleFunc("/api/video/state", dispatchHandler(s, func() VideoHandler { return s.videoHandler }, isNil, func(h VideoHandler, w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet || r.Method == http.MethodHead {
			h.GetVideoState(w, r)
		} else {
			h.PostVideoState(w, r)
		}
	}))
	s.mux.HandleFunc("/api/video/trigger", dispatchHandler(s, func() VideoHandler { return s.videoHandler }, isNil, func(h VideoHandler, w http.ResponseWriter, r *http.Request) { h.PostVideoTrigger(w, r) }))
	s.mux.HandleFunc("/api/voice/heartbeat", dispatchHandler(s, func() VoiceHandler { return s.voiceHandler }, isNil, func(h VoiceHandler, w http.ResponseWriter, r *http.Request) { h.PostVoiceHeartbeat(w, r) }))
	s.mux.HandleFunc("/api/voice/interact", dispatchHandler(s, func() VoiceHandler { return s.voiceHandler }, isNil, func(h VoiceHandler, w http.ResponseWriter, r *http.Request) { h.PostVoiceInteract(w, r) }))
	s.mux.HandleFunc("/api/voice/state", dispatchHandler(s, func() VoiceHandler { return s.voiceHandler }, isNil, func(h VoiceHandler, w http.ResponseWriter, r *http.Request) { h.PostVoiceState(w, r) }))
	s.mux.HandleFunc("/api/widgets/{widget_id}", dispatchHandler(s, func() RenderHandler { return s.renderHandler }, isNil, func(h RenderHandler, w http.ResponseWriter, r *http.Request) {
		h.GetWidgetJSON(w, r, r.PathValue("widget_id"))
	}))
	s.mux.HandleFunc("/api/widgets/{widget_id}/action", s.handleWidgetAction)
	s.mux.HandleFunc("/api/widgets/{widget_id}/photos", dispatchHandler(s, func() PushHandler { return s.pushHandler }, isNil, func(h PushHandler, w http.ResponseWriter, r *http.Request) {
		h.PostWidgetPhotos(w, r, r.PathValue("widget_id"))
	}))
	s.mux.HandleFunc("/api/widgets/{widget_id}/push", dispatchHandler(s, func() PushHandler { return s.pushHandler }, isNil, func(h PushHandler, w http.ResponseWriter, r *http.Request) {
		h.PostWidgetPush(w, r, r.PathValue("widget_id"))
	}))
	s.mux.HandleFunc("/api/widgets/{widget_id}/render", dispatchHandler(s, func() RenderHandler { return s.renderHandler }, isNil, func(h RenderHandler, w http.ResponseWriter, r *http.Request) {
		h.GetWidgetRender(w, r, r.PathValue("widget_id"))
	}))
	s.mux.HandleFunc("/debug/coverage/flush", s.handleCoverageFlush)
	s.mux.HandleFunc("/display", dispatchHandler(s, func() DisplayHandler { return s.displayHandler }, isNil, func(h DisplayHandler, w http.ResponseWriter, r *http.Request) { h.GetDisplay(w, r) }))
	s.mux.HandleFunc("/health", s.handleHealth)
	s.mux.HandleFunc("/healthz", s.handleHealth)
	s.mux.Handle("/metrics", promhttp.Handler())
	s.mux.HandleFunc("/static/{path...}", dispatchHandler(s, func() DisplayHandler { return s.displayHandler }, isNil, func(h DisplayHandler, w http.ResponseWriter, r *http.Request) { h.GetStatic(w, r, r.PathValue("path")) }))
	s.mux.HandleFunc("/style.css", dispatchHandler(s, func() RenderHandler { return s.renderHandler }, isNil, func(h RenderHandler, w http.ResponseWriter, r *http.Request) { h.ServeStyle(w, r) }))
	s.mux.HandleFunc("/widget-types/{type}/assets/{path...}", dispatchHandler(s, func() RenderHandler { return s.renderHandler }, isNil, func(h RenderHandler, w http.ResponseWriter, r *http.Request) {
		h.GetWidgetAsset(w, r, r.PathValue("type"), r.PathValue("path"))
	}))
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "*")
	switch r.Method {
	case http.MethodOptions:
		w.WriteHeader(http.StatusNoContent)
	case http.MethodHead, http.MethodGet:
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if r.Method == http.MethodGet {
			if err := json.NewEncoder(w).Encode(HealthResponse{Status: "ok"}); err != nil {
				http.Error(w, "failed to encode response", http.StatusInternalServerError)
			}
		}
	default:
		w.Header().Set("Allow", "GET, HEAD, OPTIONS")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

var (
	flushMu            sync.Mutex
	writeCountersDirMu sync.RWMutex
	writeMetaDirFn     = coverage.WriteMetaDir
	writeCountersDirFn = coverage.WriteCountersDir
)

func (s *Server) handleCoverageFlush(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		w.Header().Set("Allow", "GET, POST")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	flushMu.Lock()
	defer flushMu.Unlock()

	writeCountersDirMu.RLock()
	metaFn, fn := writeMetaDirFn, writeCountersDirFn
	writeCountersDirMu.RUnlock()

	var buf bytes.Buffer
	if err := covflush.WriteArchive(&buf, metaFn, fn); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/x-tar")
	w.WriteHeader(http.StatusOK)
	if _, err := w.Write(buf.Bytes()); err != nil {
		return
	}
}

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Access-Control-Allow-Origin", "*")
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD, OPTIONS")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodGet {
		if err := json.NewEncoder(w).Encode(RootResponse{Service: "mirrormere", Status: "running", Version: Version}); err != nil {
			http.Error(w, "failed to encode response", http.StatusInternalServerError)
		}
	}
}

// Serve accepts connections on the given net.Listener.
func (s *Server) Serve(l net.Listener) error {
	s.mu.Lock()
	s.addr = l.Addr().String()
	s.mu.Unlock()
	s.readyOnce.Do(func() { close(s.ready) })
	err := s.httpServer.Serve(l)
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

// Shutdown gracefully stops the HTTP server.
func (s *Server) Shutdown(ctx context.Context) error {
	return s.httpServer.Shutdown(ctx)
}
