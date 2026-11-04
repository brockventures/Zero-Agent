package video

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/events"
)

// VideoStateSink accepts updated video state for initial connection hydration sync.
type VideoStateSink interface {
	SetVideoState(state *events.VideoStateData)
}

// CoordinatorOption configures Coordinator instances.
type CoordinatorOption func(*Coordinator)

func WithSink(sink VideoStateSink) CoordinatorOption { return func(c *Coordinator) { c.sink = sink } }

// Coordinator manages the authoritative in-memory video priority stack, auto-dismiss timers,
// transport action forwarding, and real-time SSE broadcasts.
type Coordinator struct {
	mu         sync.RWMutex
	mode       string
	primary    *VideoStream
	pip        *VideoStream
	timers     map[string]Timer
	hub        *events.Hub
	sink       VideoStateSink
	httpClient *http.Client
	timerFunc  TimerFunc
	closed     bool
}

// NewCoordinator constructs a VideoCoordinator initialized in widgets mode.
func NewCoordinator(hub *events.Hub, opts ...CoordinatorOption) *Coordinator {
	c := &Coordinator{mode: ModeWidgets, timers: make(map[string]Timer), hub: hub, httpClient: &http.Client{Timeout: 2 * time.Second}, timerFunc: defaultTimerFunc}
	for _, opt := range opts {
		opt(c)
	}
	if c.sink != nil {
		c.sink.SetVideoState(&events.VideoStateData{Mode: ModeWidgets})
	}
	return c
}

func (c *Coordinator) GetState() VideoState {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.snapshotLocked()
}

func (c *Coordinator) snapshotLocked() VideoState {
	return VideoState{Mode: c.mode, Primary: cloneStream(c.primary), Pip: cloneStream(c.pip)}
}

func cloneStream(s *VideoStream) *VideoStream {
	if s == nil {
		return nil
	}
	cp := *s
	return &cp
}

func isValidPlayerState(ps string) bool {
	return ps == PlayerStatePlaying || ps == PlayerStatePaused || ps == PlayerStateBuffering || ps == PlayerStateIdle
}

func streamID(s *VideoStream) string {
	if s != nil {
		return s.ID
	}
	return ""
}

func (c *Coordinator) findStreamLocked(id string) *VideoStream {
	if streamID(c.primary) == id {
		return c.primary
	}
	if streamID(c.pip) == id {
		return c.pip
	}
	return nil
}

func (c *Coordinator) unlockAndPublish(snap VideoState) (VideoState, error) {
	c.mu.Unlock()
	c.publishState(snap)
	return snap, nil
}

func (c *Coordinator) retriggerSlotLocked(slot **VideoStream, stream VideoStream, muted bool) VideoState {
	c.stopTimerLocked(stream.ID)
	stream.Muted = muted
	preserveMetadata(&stream, *slot)
	if stream.PositionUpdatedAt == 0 && stream.Position > 0 {
		stream.PositionUpdatedAt = time.Now().UnixMilli()
	}
	*slot = &stream
	if stream.Priority == PriorityTemporary {
		c.startTimerLocked(stream.ID, time.Duration(stream.TimeoutSeconds)*time.Second)
	}
	return c.snapshotLocked()
}

func (c *Coordinator) promotePipOrResetLocked() {
	if c.pip != nil {
		c.pip.Muted = false
		c.primary, c.pip = c.pip, nil
	} else {
		c.primary, c.mode = nil, ModeWidgets
	}
}

func normalizeAndValidateStream(stream *VideoStream) error {
	stream.ID, stream.StreamURL = strings.TrimSpace(stream.ID), strings.TrimSpace(stream.StreamURL)
	if stream.ID == "" || stream.StreamURL == "" || stream.ID == "all" || stream.ID == "*" {
		return ErrInvalidStream
	}
	if stream.Type == "" {
		stream.Type = TypeWebRTC
	} else if stream.Type != TypeWebRTC && stream.Type != TypeHLS && stream.Type != TypeMJPEG {
		return ErrInvalidStream
	}
	if stream.Priority == "" {
		stream.Priority = PriorityPersistent
	} else if stream.Priority != PriorityPersistent && stream.Priority != PriorityTemporary {
		return ErrInvalidStream
	}
	if stream.Priority == PriorityTemporary && stream.TimeoutSeconds <= 0 {
		stream.TimeoutSeconds = DefaultTemporaryTimeoutSeconds
	} else if stream.Priority != PriorityTemporary {
		stream.TimeoutSeconds = 0
	}
	if stream.PlayerState == "" {
		stream.PlayerState = PlayerStatePlaying
	} else if !isValidPlayerState(stream.PlayerState) {
		return ErrInvalidPlayerState
	}
	return nil
}

func (c *Coordinator) assignPersistentStreamSlotLocked(stream *VideoStream) {
	stream.Muted = false
	if c.primary != nil {
		if c.primary.Priority == PriorityTemporary {
			c.stopTimerLocked(streamID(c.pip))
			c.primary.Muted, c.pip = true, c.primary
		} else {
			c.stopTimerLocked(c.primary.ID)
		}
	}
	c.primary = stream
}

func (c *Coordinator) assignTemporaryStreamSlotLocked(stream *VideoStream) {
	timeout := time.Duration(stream.TimeoutSeconds) * time.Second
	if c.primary != nil && c.primary.Priority == PriorityPersistent {
		c.stopTimerLocked(streamID(c.pip))
		stream.Muted, c.pip = true, stream
		c.startTimerLocked(stream.ID, timeout)
		return
	}
	c.stopTimerLocked(streamID(c.primary))
	stream.Muted, c.primary = false, stream
	c.startTimerLocked(stream.ID, timeout)
}

func (c *Coordinator) assignStreamSlotLocked(stream *VideoStream) {
	if stream.Priority == PriorityPersistent {
		c.assignPersistentStreamSlotLocked(stream)
		return
	}
	c.assignTemporaryStreamSlotLocked(stream)
}

func (c *Coordinator) Trigger(stream VideoStream) (VideoState, error) {
	if err := normalizeAndValidateStream(&stream); err != nil {
		return VideoState{}, err
	}

	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return VideoState{}, ErrStreamNotFound
	}

	if streamID(c.primary) == stream.ID {
		return c.unlockAndPublish(c.retriggerSlotLocked(&c.primary, stream, false))
	}
	if streamID(c.pip) == stream.ID {
		return c.unlockAndPublish(c.retriggerSlotLocked(&c.pip, stream, true))
	}

	if stream.PositionUpdatedAt == 0 && stream.Position > 0 {
		stream.PositionUpdatedAt = time.Now().UnixMilli()
	}

	c.assignStreamSlotLocked(&stream)
	c.mode = ModeVideo

	return c.unlockAndPublish(c.snapshotLocked())
}

// Dismiss unmounts an active video stream by ID.
func (c *Coordinator) Dismiss(id string) (VideoState, error) {
	id = strings.TrimSpace(id)
	if id == "" {
		return VideoState{}, ErrInvalidStream
	}
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return VideoState{}, ErrStreamNotFound
	}

	switch {
	case id == "all" || id == "*":
		if c.primary == nil && c.pip == nil {
			c.mu.Unlock()
			return VideoState{}, ErrStreamNotFound
		}
		for tid := range c.timers {
			c.stopTimerLocked(tid)
		}
		c.primary, c.pip, c.mode = nil, nil, ModeWidgets
	case streamID(c.pip) == id:
		c.stopTimerLocked(id)
		c.pip = nil
	case streamID(c.primary) == id:
		c.stopTimerLocked(id)
		c.promotePipOrResetLocked()
	default:
		c.mu.Unlock()
		return VideoState{}, ErrStreamNotFound
	}

	return c.unlockAndPublish(c.snapshotLocked())
}

// UpdatePlayerState updates transport state and optional stream metadata without wiping existing data.
func (c *Coordinator) UpdatePlayerState(id string, update VideoStream) (VideoState, error) {
	id = strings.TrimSpace(id)
	if id == "" {
		return VideoState{}, ErrInvalidStream
	}
	if update.PlayerState != "" && !isValidPlayerState(update.PlayerState) {
		return VideoState{}, ErrInvalidPlayerState
	}
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return VideoState{}, ErrStreamNotFound
	}

	target := c.findStreamLocked(id)
	if target == nil {
		c.mu.Unlock()
		return VideoState{}, ErrStreamNotFound
	}

	if update.PlayerState != "" {
		target.PlayerState = update.PlayerState
		if update.PlayerState == PlayerStateIdle {
			target.Position, target.Duration, target.PositionUpdatedAt = 0, 0, 0
			if update.Title == "" {
				target.Title, target.Series, target.Season, target.Episode = "", "", "", ""
			}
		}
	}
	copyMetadata(target, &update, true)
	if update.Position > 0 {
		target.Position, target.PositionUpdatedAt = update.Position, update.PositionUpdatedAt
		if target.PositionUpdatedAt == 0 {
			target.PositionUpdatedAt = time.Now().UnixMilli()
		}
	} else if update.Position == 0 && update.PositionUpdatedAt > 0 {
		target.Position, target.PositionUpdatedAt = 0, update.PositionUpdatedAt
	}

	return c.unlockAndPublish(c.snapshotLocked())
}

func copyMetadata(target, src *VideoStream, overwrite bool) {
	if src == nil {
		return
	}
	if overwrite && src.Title != "" {
		target.Title = src.Title
		target.Series = src.Series
		target.Season = src.Season
		target.Episode = src.Episode
		if src.Duration > 0 {
			target.Duration = src.Duration
		}
		return
	}
	if !overwrite && target.Title != "" {
		return
	}
	copyStr := func(dst *string, val string) {
		if val != "" && (overwrite || *dst == "") {
			*dst = val
		}
	}
	copyStr(&target.Title, src.Title)
	copyStr(&target.Series, src.Series)
	copyStr(&target.Season, src.Season)
	copyStr(&target.Episode, src.Episode)
	if src.Duration > 0 && (overwrite || target.Duration == 0) {
		target.Duration = src.Duration
	}
	if !overwrite && target.Position == 0 && src.Position > 0 {
		target.Position, target.PositionUpdatedAt = src.Position, src.PositionUpdatedAt
	}
}

func preserveMetadata(target, existing *VideoStream) { copyMetadata(target, existing, false) } // preserve

// ForwardAction proxies playback control commands to a controllable stream's sidecar webhook.
func (c *Coordinator) ForwardAction(ctx context.Context, id, action string, value any) error {
	id = strings.TrimSpace(id)
	if id == "" {
		return ErrInvalidStream
	}
	switch action {
	case ActionTogglePlayback, ActionPlay, ActionPause, ActionRewind, ActionFastForward,
		ActionBack, ActionHome, ActionCaptions, ActionNext, ActionPrevious, ActionSeek:
	default:
		return ErrInvalidAction
	}

	c.mu.RLock()
	target := c.findStreamLocked(id)
	if target == nil {
		c.mu.RUnlock()
		return ErrStreamNotFound
	}
	controlURL := strings.TrimSpace(target.ControlURL)
	controllable := target.Controllable && controlURL != ""
	c.mu.RUnlock()
	if !controllable {
		return ErrNotControllable
	}

	reqCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	payload, err := json.Marshal(ActionRequest{ID: id, Action: action, Value: value})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, controlURL, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return ErrControllerUnreachable
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return ErrControllerUnreachable
	}
	return nil
}

func (c *Coordinator) startTimerLocked(id string, d time.Duration) {
	if d > 0 {
		c.stopTimerLocked(id)
		c.timers[id] = c.timerFunc(d, func() { c.handleTimerExpired(id) })
	}
}

func (c *Coordinator) stopTimerLocked(id string) {
	if id != "" {
		if t, ok := c.timers[id]; ok {
			t.Stop()
			delete(c.timers, id)
		}
	}
}

func (c *Coordinator) handleTimerExpired(id string) {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	delete(c.timers, id)
	var snap VideoState
	shouldPublish := false
	if streamID(c.pip) == id {
		c.pip, snap, shouldPublish = nil, c.snapshotLocked(), true
	} else if streamID(c.primary) == id {
		c.promotePipOrResetLocked()
		snap, shouldPublish = c.snapshotLocked(), true
	}
	c.mu.Unlock()
	if shouldPublish {
		c.publishState(snap)
	}
}

func (c *Coordinator) publishState(state VideoState) {
	data := events.VideoStateData{Mode: state.Mode, Primary: state.Primary, Pip: state.Pip}
	if c.sink != nil {
		c.sink.SetVideoState(&data)
	}
	if c.hub != nil {
		if payload, err := json.Marshal(data); err == nil {
			c.hub.Publish(events.EventVideoState, payload)
		}
	}
}

func (c *Coordinator) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.closed = true
	for id, t := range c.timers {
		t.Stop()
		delete(c.timers, id)
	}
}
