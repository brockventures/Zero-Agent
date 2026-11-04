package server

import (
	"net/http"

	"github.com/azylman/mirrormere/internal/api"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
)

// Routes returns the configured http.Handler for in-memory testing.
func (s *Server) Routes() http.Handler { return s.mux }

// Ready returns a channel closed as soon as the server listener is bound.
func (s *Server) Ready() <-chan struct{} { return s.ready }

// Addr returns the network address the server is actively listening on.
func (s *Server) Addr() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.addr
}

// SetWriteCountersDirForTest overrides writeCountersDirFn for testing purposes.
func SetWriteCountersDirForTest(fn func(string) error) func() {
	writeCountersDirMu.Lock()
	prev := writeCountersDirFn
	writeCountersDirFn = fn
	writeCountersDirMu.Unlock()
	return func() {
		writeCountersDirMu.Lock()
		writeCountersDirFn = prev
		writeCountersDirMu.Unlock()
	}
}

// ResolveSupportedDimensionsForTest exports resolveSupportedDimensions for testing.
func (h *DefaultLayoutEditorHandler) ResolveSupportedDimensionsForTest(wType string, dim domain.Dimension, pkgs map[string]*domain.Package) [][]int {
	return h.resolveSupportedDimensions(wType, dim, pkgs)
}

// BuildResponseForTest exports buildResponse for testing.
func (h *DefaultLayoutEditorHandler) BuildResponseForTest(snap *config.Snapshot) api.LayoutEditorResponse {
	return h.buildResponse(snap)
}
