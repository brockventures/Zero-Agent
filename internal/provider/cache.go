package provider

import (
	"sync"
	"time"
)

type cacheEntry struct {
	widgetID            string
	data                any
	state               string
	timestamp           string
	lastSuccess         time.Time
	lastAttempt         time.Time
	consecutiveFailures int
	lastError           error
	hasLKG              bool
}

func (e *cacheEntry) toPayload() WidgetPayload {
	return WidgetPayload{
		WidgetID:  e.widgetID,
		Timestamp: e.timestamp,
		State:     e.state,
		Data:      e.data,
	}
}

// SWRCache maintains the in-memory Stale-While-Revalidate state registry for all widgets.
// Thread-safe for concurrent read and write access.
type SWRCache struct {
	mu      sync.RWMutex
	entries map[string]*cacheEntry
}

// NewSWRCache constructs an initialized SWRCache.
func NewSWRCache() *SWRCache {
	return &SWRCache{
		entries: make(map[string]*cacheEntry),
	}
}

// Get returns the cached payload for the given widgetID, if present.
func (c *SWRCache) Get(widgetID string) (WidgetPayload, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	e, ok := c.entries[widgetID]
	if !ok {
		return WidgetPayload{}, false
	}
	return e.toPayload(), true
}

// RecordSuccess records a successful data fetch. It sets the state to "healthy",
// records LKG data, stamps the current RFC 3339 timestamp, resets failure counters,
// and reports whether the state transitioned.
func (c *SWRCache) RecordSuccess(widgetID string, data any, now time.Time) (WidgetPayload, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.entries[widgetID]
	if !ok {
		e = &cacheEntry{widgetID: widgetID}
		c.entries[widgetID] = e
	}

	oldState := e.state
	nowStr := now.UTC().Format(time.RFC3339)

	e.data = data
	e.state = StateHealthy
	e.timestamp = nowStr
	e.lastSuccess = now
	e.lastAttempt = now
	e.consecutiveFailures = 0
	e.lastError = nil
	e.hasLKG = true

	transitioned := (oldState != StateHealthy)

	return e.toPayload(), transitioned
}

// RecordFailure records a failed data fetch. Under Stale-While-Revalidate:
//   - If LKG data exists, state transitions to "degraded", LKG data is preserved,
//     and timestamp retains the timestamp of the last successful fetch.
//   - If no LKG data exists (cold-boot), state transitions to "error" with empty fallback data.
//
// Returns the active payload and whether an operational state transition occurred.
func (c *SWRCache) RecordFailure(widgetID string, err error, now time.Time) (WidgetPayload, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.entries[widgetID]
	if !ok {
		e = &cacheEntry{widgetID: widgetID}
		c.entries[widgetID] = e
	}

	oldState := e.state
	e.lastAttempt = now
	e.consecutiveFailures++
	e.lastError = err

	var payload WidgetPayload
	var transitioned bool

	if e.hasLKG {
		e.state = StateDegraded
		transitioned = (oldState != StateDegraded)
		payload = e.toPayload()
	} else {
		e.state = StateError
		if e.data == nil {
			e.data = map[string]any{}
		}
		nowStr := now.UTC().Format(time.RFC3339)
		e.timestamp = nowStr
		transitioned = (oldState != StateError)
		payload = e.toPayload()
	}

	return payload, transitioned
}

// RecordPush records an unsolicited push payload (e.g. from inbound webhook).
func (c *SWRCache) RecordPush(widgetID string, data any, now time.Time) (WidgetPayload, bool) {
	return c.RecordSuccess(widgetID, data, now)
}

// Purge evicts a widget instance from the cache (e.g. when removed or domain parameters change).
func (c *SWRCache) Purge(widgetID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.entries, widgetID)
}

// GetStatusMap returns a map of widgetID to operational state string for header/status reporting.
func (c *SWRCache) GetStatusMap() map[string]string {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if len(c.entries) == 0 {
		return nil
	}
	out := make(map[string]string, len(c.entries))
	for id, e := range c.entries {
		out[id] = e.state
	}
	return out
}

// GetConsecutiveFailures returns the number of consecutive fetch failures for widgetID.
func (c *SWRCache) GetConsecutiveFailures(widgetID string) int {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if e, ok := c.entries[widgetID]; ok {
		return e.consecutiveFailures
	}
	return 0
}
