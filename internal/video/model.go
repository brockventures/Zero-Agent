package video

import "errors"

const (
	// ModeWidgets indicates idle dashboard grid mode.
	ModeWidgets = "widgets"
	// ModeVideo indicates dedicated video presentation mode.
	ModeVideo = "video"

	// PriorityPersistent designates media streaming that remains active until stopped (e.g. Chromecast).
	PriorityPersistent = "persistent"
	// PriorityTemporary designates alert streams that auto-dismiss after timeout (e.g. Doorbell).
	PriorityTemporary = "temporary"

	// TypeWebRTC designates WebRTC video streaming.
	TypeWebRTC = "webrtc"
	// TypeHLS designates HTTP Live Streaming.
	TypeHLS = "hls"
	// TypeMJPEG designates Motion JPEG video streaming.
	TypeMJPEG = "mjpeg"

	// PlayerStatePlaying indicates active media playback.
	PlayerStatePlaying = "playing"
	// PlayerStatePaused indicates paused media playback.
	PlayerStatePaused = "paused"
	// PlayerStateBuffering indicates media buffering.
	PlayerStateBuffering = "buffering"
	// PlayerStateIdle indicates idle / ambient display state.
	PlayerStateIdle = "idle"

	// ActionTogglePlayback toggles play/pause on controllable streams.
	ActionTogglePlayback = "toggle_playback"
	// ActionPlay resumes playback on controllable streams.
	ActionPlay = "play"
	// ActionPause pauses playback on controllable streams.
	ActionPause = "pause"
	// ActionRewind skips media backwards on controllable streams.
	ActionRewind = "rewind"
	// ActionFastForward skips media forwards on controllable streams.
	ActionFastForward = "fast_forward"
	// ActionBack triggers back navigation on controllable streams.
	ActionBack = "back"
	// ActionHome triggers home navigation on controllable streams.
	ActionHome = "home"
	// ActionCaptions toggles closed captions on controllable streams.
	ActionCaptions = "captions"
	// ActionNext skips to next episode/track on controllable streams.
	ActionNext = "next"
	// ActionPrevious skips to previous episode/track on controllable streams.
	ActionPrevious = "previous"
	// ActionSeek sets playback position on controllable streams.
	ActionSeek = "seek"

	// DefaultTemporaryTimeoutSeconds is the fallback auto-dismiss timeout for temporary streams.
	DefaultTemporaryTimeoutSeconds = 45
)

var (
	// ErrStreamNotFound indicates the requested stream ID is not active in the video stack.
	ErrStreamNotFound = errors.New("stream not active")
	// ErrNotControllable indicates the stream does not support remote transport actions.
	ErrNotControllable = errors.New("stream is not controllable")
	// ErrControllerUnreachable indicates the upstream stream sidecar is unreachable or returned non-2xx.
	ErrControllerUnreachable = errors.New("stream controller unreachable")
	// ErrInvalidStream indicates missing or malformed stream parameters.
	ErrInvalidStream = errors.New("invalid stream: id and stream_url are required")
	// ErrInvalidAction indicates an unsupported transport action.
	ErrInvalidAction = errors.New("invalid action: must be toggle_playback, play, pause, rewind, fast_forward, back, home, captions, next, previous, or seek")
	// ErrInvalidPlayerState indicates an unsupported player transport state.
	ErrInvalidPlayerState = errors.New("invalid player_state: must be playing, paused, buffering, or idle")
)

// VideoStream defines an active video stream within the priority stack.
type VideoStream struct {
	ID                string  `json:"id"`
	StreamURL         string  `json:"stream_url"`
	Type              string  `json:"type"`
	Priority          string  `json:"priority,omitempty"`
	TimeoutSeconds    int     `json:"timeout_seconds,omitempty"`
	Controllable      bool    `json:"controllable,omitempty"`
	ControlURL        string  `json:"control_url,omitempty"`
	PlayerState       string  `json:"player_state,omitempty"`
	Muted             bool    `json:"muted,omitempty"`
	Title             string  `json:"title,omitempty"`
	Series            string  `json:"series,omitempty"`
	Season            string  `json:"season,omitempty"`
	Episode           string  `json:"episode,omitempty"`
	Position          float64 `json:"position,omitempty"`
	Duration          float64 `json:"duration,omitempty"`
	PositionUpdatedAt int64   `json:"position_updated_at,omitempty"`
}

// VideoState defines the authoritative video presentation snapshot matching .E and .
type VideoState struct {
	Mode    string       `json:"mode"`
	Primary *VideoStream `json:"primary"`
	Pip     *VideoStream `json:"pip"`
}

// ActionRequest encapsulates a transport control command forwarded to a sidecar control_url.
type ActionRequest struct {
	ID     string `json:"id"`
	Action string `json:"action"`
	Value  any    `json:"value"`
}
