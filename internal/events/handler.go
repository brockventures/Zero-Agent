package events

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

// Handler constructs an http.Handler serving GET /api/events for SSE clients.
func Handler(hub *Hub) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 1. CORS Preflight
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Cache-Control, Last-Event-ID, Content-Type, Accept")

		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}

		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET, OPTIONS")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// 2. http.Flusher Verification
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}

		// 3. Response Headers
		w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache, no-transform")
		w.Header().Set("Connection", "keep-alive")
		w.Header().Set("X-Accel-Buffering", "no")

		// 4. Disable write deadline for persistent streaming
		rc := http.NewResponseController(w)
		if err := rc.SetWriteDeadline(time.Time{}); err != nil && !errors.Is(err, http.ErrNotSupported) {
			hub.logger.Debug("failed to disable response write deadline", "error", err)
		}

		w.WriteHeader(http.StatusOK)
		flusher.Flush()

		// 5. Subscribe to live stream BEFORE snapshot/replay to eliminate race window (Issue #199)
		ch, unsubscribe := hub.Subscribe(r.Context())
		defer unsubscribe()

		// 6. Initial Hydration vs Replay
		lastEventID := resolveLastEventID(r)
		initialBatch, seenReplayIDs := prepareInitialEvents(hub, lastEventID)

		// 7. Flush initial batch (hydration or replay)
		for _, evt := range initialBatch {
			if _, err := w.Write(evt.Format()); err != nil {
				return
			}
		}
		flusher.Flush()

		// 8-10. Live Stream & Keep-alive SSE Loop
		streamSSELoop(r.Context(), w, flusher, ch, hub.cfg.HeartbeatInterval, seenReplayIDs)
	})
}

func resolveLastEventID(r *http.Request) string {
	lastEventID := strings.TrimSpace(r.Header.Get("Last-Event-ID"))
	if lastEventID != "" {
		return lastEventID
	}
	lastEventID = strings.TrimSpace(r.URL.Query().Get("lastEventId"))
	if lastEventID != "" {
		return lastEventID
	}
	return strings.TrimSpace(r.URL.Query().Get("last_event_id"))
}

func prepareInitialEvents(hub *Hub, lastEventID string) ([]*Event, map[string]struct{}) {
	if lastEventID == "" {
		return hub.BuildHydration(), nil
	}
	replayed, found := hub.ReplaySince(lastEventID)
	if !found {
		// Replay missed/expired: execute full initial state hydration
		return hub.BuildHydration(), nil
	}
	seenReplayIDs := make(map[string]struct{}, len(replayed)+1)
	seenReplayIDs[lastEventID] = struct{}{}
	for _, evt := range replayed {
		seenReplayIDs[evt.ID] = struct{}{}
	}
	return replayed, seenReplayIDs
}

func drainSSEQueue(ctx context.Context, ch <-chan *Event, deliver func(*Event) bool) bool {
	for {
		select {
		case <-ctx.Done():
			return false
		case evt, ok := <-ch:
			if !ok || !deliver(evt) {
				return false
			}
		default:
			return true
		}
	}
}

func streamSSELoop(ctx context.Context, w io.Writer, flusher http.Flusher, ch <-chan *Event, heartbeat time.Duration, seenReplayIDs map[string]struct{}) {
	ticker := time.NewTicker(heartbeat)
	defer ticker.Stop()

	deliverEvent := func(evt *Event) bool {
		if seenReplayIDs != nil {
			if _, seen := seenReplayIDs[evt.ID]; seen {
				return true
			}
			seenReplayIDs = nil
		}
		if _, err := w.Write(evt.Format()); err != nil {
			return false
		}
		flusher.Flush()
		return true
	}

	if !drainSSEQueue(ctx, ch, deliverEvent) {
		return
	}

	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-ch:
			if !ok || !deliverEvent(evt) {
				return
			}
		case t := <-ticker.C:
			if _, err := w.Write(FormatPing(t)); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}
