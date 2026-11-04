// Package api defines HTTP request and response payload models for the Mirrormere REST API.
package api

import (
	"encoding/json"

	"github.com/azylman/mirrormere/internal/video"
)

// Display UI label constants for performance telemetry batches.
const (
	UILegacy = "legacy"
	UIReact  = "react"
)

// ActionResponse represents a generic status confirmation response.
type ActionResponse struct {
	Status string `json:"status"`
}

// ErrorResponse represents a standard JSON error response.
type ErrorResponse struct {
	Status string `json:"status"`
	Error  string `json:"error"`
}

// AudioStateResponse represents the master audio volume and mute state.
type AudioStateResponse struct {
	Status string `json:"status"`
	Volume int    `json:"volume"`
	Muted  bool   `json:"muted"`
}

// AudioVolumeRequest represents the payload for POST /api/audio/volume.
type AudioVolumeRequest struct {
	Volume *int `json:"volume"`
}

// AudioMuteRequest represents the payload for POST /api/audio/mute.
type AudioMuteRequest struct {
	Muted json.RawMessage `json:"muted,omitempty"`
}

// LayoutEditorResponse represents the payload returned by /api/layout/editor.
type LayoutEditorResponse struct {
	AllowLayoutEdit bool                 `json:"allow_layout_edit"`
	HasOverride     bool                 `json:"has_override"`
	Grid            []int                `json:"grid"`
	TotalScreens    int                  `json:"total_screens"`
	Widgets         []LayoutEditorWidget `json:"widgets"`
}

// LayoutEditorUpdateRequest represents the payload for PUT /api/layout/editor.
type LayoutEditorUpdateRequest struct {
	Widgets []LayoutEditorWidgetPlacement `json:"widgets"`
}

// LayoutEditorWidget describes a placed widget in the layout editor response.
type LayoutEditorWidget struct {
	WidgetID            string  `json:"widget_id"`
	Type                string  `json:"type"`
	Screen              int     `json:"screen"`
	Origin              []int   `json:"origin"`
	Dimensions          []int   `json:"dimensions"`
	Pinned              bool    `json:"pinned"`
	SupportedDimensions [][]int `json:"supported_dimensions"`
}

// LayoutEditorWidgetPlacement describes a target widget placement in PUT /api/layout/editor.
type LayoutEditorWidgetPlacement struct {
	WidgetID   string `json:"widget_id"`
	Screen     int    `json:"screen"`
	Origin     []int  `json:"origin"`
	Dimensions []int  `json:"dimensions"`
	Pinned     bool   `json:"pinned,omitempty"`
}

// PerfBatchRequest represents a batch of client display timing samples for POST /api/perf.
type PerfBatchRequest struct {
	Samples []PerfSample `json:"samples"`
}

// PerfSample represents a single display timing measurement.
type PerfSample struct {
	Metric  string  `json:"metric"`
	UI      string  `json:"ui"`
	ValueMs float64 `json:"value_ms"`
}

// ScreenAdvanceRequest represents the payload for POST /api/screen/advance.
type ScreenAdvanceRequest struct {
	Direction string `json:"direction,omitempty"`
}

// ScreenSelectRequest represents the payload for POST /api/screen/select.
type ScreenSelectRequest struct {
	ScreenIndex int `json:"screen_index"`
}

// ScreenPauseRequest represents the payload for POST /api/screen/pause.
type ScreenPauseRequest struct {
	Paused          *bool `json:"paused"`
	DurationSeconds *int  `json:"duration_seconds,omitempty"`
}

// ScreenNavigationResponse represents the response for screen select/advance operations.
type ScreenNavigationResponse struct {
	Status        string `json:"status"`
	CurrentScreen int    `json:"current_screen"`
	TotalScreens  int    `json:"total_screens"`
}

// ScreenPauseResponse represents the response for POST /api/screen/pause.
type ScreenPauseResponse struct {
	Status        string `json:"status"`
	Paused        bool   `json:"paused"`
	CurrentScreen int    `json:"current_screen"`
}

// ScreensResponse represents the solved screen layout returned by GET /api/screens.
type ScreensResponse struct {
	CurrentScreen   int            `json:"current_screen"`
	IntervalSeconds int            `json:"interval_seconds"`
	Screens         []ScreenLayout `json:"screens"`
}

// ScreenLayout represents a single screen in the rotation layout.
type ScreenLayout struct {
	Index   int               `json:"index"`
	Widgets []WidgetPlacement `json:"widgets"`
}

// WidgetPlacement represents a placed widget on a screen.
type WidgetPlacement struct {
	WidgetID   string `json:"widget_id"`
	Type       string `json:"type"`
	Origin     []int  `json:"origin"`
	Dimensions []int  `json:"dimensions"`
}

// VideoDismissRequest represents the payload for POST /api/video/dismiss.
type VideoDismissRequest struct {
	ID string `json:"id"`
}

// VideoPlayerStateResponse represents the response for POST /api/video/state.
type VideoPlayerStateResponse struct {
	Status      string `json:"status"`
	ID          string `json:"id"`
	PlayerState string `json:"player_state"`
}

// VideoStateResponse represents the video priority stack state returned by /api/video/*.
type VideoStateResponse struct {
	Status  string             `json:"status"`
	Mode    string             `json:"mode"`
	Primary *video.VideoStream `json:"primary,omitempty"`
	Pip     *video.VideoStream `json:"pip,omitempty"`
}

// VoiceStateRequest represents the payload for POST /api/voice/state.
type VoiceStateRequest struct {
	State      string  `json:"state"`
	Transcript *string `json:"transcript,omitempty"`
	Reply      *string `json:"reply,omitempty"`
	TTSEngine  *string `json:"tts_engine,omitempty"`
}

// VoiceStateResponse represents the response for POST /api/voice/state.
type VoiceStateResponse struct {
	Status string `json:"status"`
}

// VoiceHeartbeatRequest represents the payload from edge voice device heartbeats.
type VoiceHeartbeatRequest struct {
	NodeID          string   `json:"node_id"`
	AmbientRMSDBFS  *float64 `json:"ambient_rms_dbfs,omitempty"`
	FalseWakes      *int     `json:"false_wakes,omitempty"`
	LastPlaybackSec *float64 `json:"last_playback_sec,omitempty"`
}

// WidgetDetailResponse represents the JSON payload returned by GET /api/widgets/{widget_id}.
type WidgetDetailResponse struct {
	WidgetID   string         `json:"widget_id"`
	Type       string         `json:"type"`
	Origin     []int          `json:"origin"`
	Dimensions []int          `json:"dimensions"`
	Config     map[string]any `json:"config"`
	State      string         `json:"state"`
	Data       any            `json:"data"`
	Timestamp  string         `json:"timestamp"`
	Online     bool           `json:"online"`
	AssetsBase string         `json:"assets_base"`
	ViewModel  map[string]any `json:"view_model,omitempty"`
}

// WidgetActionRequest represents the incoming JSON body for POST /api/widgets/{widget_id}/action.
type WidgetActionRequest struct {
	Action      string         `json:"action,omitempty"`
	Data        map[string]any `json:"data,omitempty"`
	Domain      string         `json:"domain,omitempty"`
	EntityID    string         `json:"entity_id,omitempty"`
	Service     string         `json:"service,omitempty"`
	ServiceData map[string]any `json:"service_data,omitempty"`
	Target      map[string]any `json:"target,omitempty"`
}

// WidgetActionResponse represents the JSON response for a successful widget action.
type WidgetActionResponse struct {
	Data     any    `json:"data,omitempty"`
	Status   string `json:"status"`
	WidgetID string `json:"widget_id"`
}
