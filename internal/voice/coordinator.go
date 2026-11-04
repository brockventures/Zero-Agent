package voice

import (
	"encoding/json"
	"errors"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/events"
)

// Standard voice lifecycle states.G and .
const (
	StateIdle         = "idle"
	StateListening    = "listening"
	StateTranscribing = "transcribing"
	StateThinking     = "thinking"
	StateSynthesizing = "synthesizing"
	StateSpeaking     = "speaking"
	StateError        = "error"
)

const defaultSpeakingWatchdogTimeout = 30 * time.Second

// ErrInvalidState is returned when state is not one of the allowed lifecycle states.
var ErrInvalidState = errors.New("invalid voice state: must be one of 'idle', 'listening', 'transcribing', 'thinking', 'synthesizing', 'speaking', 'error'")

// ValidStates defines the set of valid voice state enum values.
var ValidStates = map[string]struct{}{
	StateIdle:         {},
	StateListening:    {},
	StateTranscribing: {},
	StateThinking:     {},
	StateSynthesizing: {},
	StateSpeaking:     {},
	StateError:        {},
}

// IsValidState reports whether s is a recognized voice lifecycle state.
func IsValidState(s string) bool {
	_, ok := ValidStates[s]
	return ok
}

// VoiceStateSink accepts updated voice state for initial connection hydration sync.
type VoiceStateSink interface {
	SetVoiceState(state *events.VoiceStateData)
}

// Coordinator tracks and manages authoritative in-memory voice pipeline state.
// It broadcasts updates across the SSE event hub and synchronizes initial connection hydration.
type Coordinator struct {
	mu              sync.RWMutex
	state           string
	transcript      *string
	reply           *string
	ttsEngine       *string
	status          *string
	hub             *events.Hub
	sink            VoiceStateSink
	speakingTimer   *time.Timer
	speakingTimeout time.Duration
}

// NewCoordinator constructs a VoiceCoordinator with default settings (idle state, nil fields).
func NewCoordinator(hub *events.Hub, sink ...VoiceStateSink) *Coordinator {
	var s VoiceStateSink
	if len(sink) > 0 {
		s = sink[0]
	}
	c := &Coordinator{
		state:           StateIdle,
		hub:             hub,
		sink:            s,
		speakingTimeout: defaultSpeakingWatchdogTimeout,
	}
	if s != nil {
		s.SetVoiceState(&events.VoiceStateData{
			State: StateIdle,
		})
	}
	return c
}

// GetState returns the current voice pipeline state data.
func (c *Coordinator) GetState() events.VoiceStateData {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.snapshotLocked()
}

func (c *Coordinator) snapshotLocked() events.VoiceStateData {
	return events.VoiceStateData{
		State:      c.state,
		Transcript: c.transcript,
		Reply:      c.reply,
		TTSEngine:  c.ttsEngine,
		Status:     c.status,
	}
}

// Stop stops and clears any active timers.
func (c *Coordinator) Stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.speakingTimer != nil {
		c.speakingTimer.Stop()
		c.speakingTimer = nil
	}
}

// SetState updates the voice pipeline state and broadcasts voice.state (clearing status).
func (c *Coordinator) SetState(state string, transcript, reply, ttsEngine *string) (events.VoiceStateData, error) {
	return c.SetFullState(state, transcript, reply, ttsEngine, nil)
}

// SetFullState updates the complete voice pipeline state including intermediate status.
func (c *Coordinator) SetFullState(state string, transcript, reply, ttsEngine, status *string) (events.VoiceStateData, error) {
	if !IsValidState(state) {
		return events.VoiceStateData{}, ErrInvalidState
	}

	return c.updateState(state, transcript, reply, ttsEngine, status), nil
}

// Transition updates the voice pipeline state without validating string inputs (used internally for known constants).
func (c *Coordinator) Transition(state string, transcript, reply, ttsEngine, status *string) events.VoiceStateData {
	return c.updateState(state, transcript, reply, ttsEngine, status)
}

// SetStatus updates intermediate tool/thinking status without altering state or transcript.
func (c *Coordinator) SetStatus(status *string) events.VoiceStateData {
	c.mu.Lock()
	c.status = status
	data := c.snapshotLocked()
	c.mu.Unlock()

	c.publishState(data)
	return data
}

// Reset resets the coordinator to default idle state and broadcasts voice.state.
func (c *Coordinator) Reset() events.VoiceStateData {
	return c.updateState(StateIdle, nil, nil, nil, nil)
}

func (c *Coordinator) armSpeakingWatchdogLocked() {
	if c.speakingTimer != nil {
		c.speakingTimer.Stop()
	}
	timeout := c.speakingTimeout
	if timeout <= 0 {
		timeout = defaultSpeakingWatchdogTimeout
	}
	var timer *time.Timer
	timer = time.AfterFunc(timeout, func() {
		c.mu.Lock()
		if c.speakingTimer != timer || c.state != StateSpeaking {
			c.mu.Unlock()
			return
		}
		c.mu.Unlock()
		c.Reset()
	})
	c.speakingTimer = timer
}

func (c *Coordinator) disarmSpeakingWatchdogLocked() {
	if c.speakingTimer != nil {
		c.speakingTimer.Stop()
		c.speakingTimer = nil
	}
}

func (c *Coordinator) updateState(state string, transcript, reply, ttsEngine, status *string) events.VoiceStateData {
	c.mu.Lock()

	c.state = state
	c.transcript = transcript
	c.reply = reply
	c.ttsEngine = ttsEngine
	c.status = status

	if state == StateSpeaking {
		c.armSpeakingWatchdogLocked()
	} else {
		c.disarmSpeakingWatchdogLocked()
	}

	data := c.snapshotLocked()
	c.mu.Unlock()

	c.publishState(data)
	return data
}

func (c *Coordinator) publishState(data events.VoiceStateData) {
	if c.sink != nil {
		c.sink.SetVoiceState(&data)
	}

	if c.hub != nil {
		payload, err := json.Marshal(data)
		if err == nil {
			c.hub.Publish(events.EventVoiceState, payload)
		}
	}
}
