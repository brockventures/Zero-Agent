package audio

import (
	"encoding/json"
	"errors"
	"sync"

	"github.com/azylman/mirrormere/internal/events"
)

const (
	// DefaultVolume is the standard initial master volume level.
	DefaultVolume = 75
	// DefaultMuted is the standard initial master mute state.
	DefaultMuted = false
)

// ErrInvalidVolume is returned when volume is outside the valid range [0, 100].
var ErrInvalidVolume = errors.New("invalid volume: must be an integer between 0 and 100")

// AudioStateSink accepts updated audio state for initial connection hydration sync.
type AudioStateSink interface {
	SetAudioState(state *events.AudioStateData)
}

// Coordinator tracks and manages authoritative in-memory master volume and mute state.
// It broadcasts updates across the SSE event hub and synchronizes initial connection hydration.
type Coordinator struct {
	mu     sync.RWMutex
	volume int
	muted  bool
	hub    *events.Hub
	sink   AudioStateSink
}

// NewCoordinator constructs an AudioCoordinator with default settings (volume 75, unmuted).
func NewCoordinator(hub *events.Hub, sink ...AudioStateSink) *Coordinator {
	var s AudioStateSink
	if len(sink) > 0 {
		s = sink[0]
	}
	c := &Coordinator{
		volume: DefaultVolume,
		muted:  DefaultMuted,
		hub:    hub,
		sink:   s,
	}
	if s != nil {
		s.SetAudioState(&events.AudioStateData{
			Volume: DefaultVolume,
			Muted:  DefaultMuted,
		})
	}
	return c
}

// GetState returns the current master volume and mute state.
func (c *Coordinator) GetState() (int, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.volume, c.muted
}

// SetVolume sets the master audio volume level [0..100] and broadcasts audio.state.
func (c *Coordinator) SetVolume(vol int) (int, bool, error) {
	if vol < 0 || vol > 100 {
		return 0, false, ErrInvalidVolume
	}

	c.mu.Lock()
	c.volume = vol
	currentMuted := c.muted
	c.mu.Unlock()

	c.publishState(vol, currentMuted)
	return vol, currentMuted, nil
}

// SetMute sets or toggles the master audio mute state and broadcasts audio.state.
// If muted is nil, the mute state is toggled.
func (c *Coordinator) SetMute(muted *bool) (int, bool, error) {
	c.mu.Lock()
	if muted == nil {
		c.muted = !c.muted
	} else {
		c.muted = *muted
	}
	currentVol := c.volume
	currentMuted := c.muted
	c.mu.Unlock()

	c.publishState(currentVol, currentMuted)
	return currentVol, currentMuted, nil
}

func (c *Coordinator) publishState(volume int, muted bool) {
	data := events.AudioStateData{
		Volume: volume,
		Muted:  muted,
	}

	if c.sink != nil {
		c.sink.SetAudioState(&data)
	}

	if c.hub != nil {
		payload, err := json.Marshal(data)
		if err == nil {
			c.hub.Publish(events.EventAudioState, payload)
		}
	}
}
