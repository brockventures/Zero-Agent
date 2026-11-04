package events

import (
	"bytes"
	"fmt"
	"sync/atomic"
	"time"
)

// Supported Server-Sent Event types.
const (
	EventWidgetUpdate = "widget.update"
	EventHeaderUpdate = "header.update"
	EventScreenRotate = "screen.rotate"
	EventThemeUpdate  = "theme.update"
	EventSystemStatus = "system.status"
	EventVideoState   = "video.state"
	EventAudioState   = "audio.state"
	EventVoiceState   = "voice.state"
	EventWidgetReload = "widget.reload"
	EventStyleReload  = "style.reload"
	EventSystemReload = "system.reload"
)

// Event represents an individual SSE message formatted per WHATWG specifications.
type Event struct {
	ID        string    `json:"id"`
	Type      string    `json:"event"`
	Data      []byte    `json:"data"`
	Timestamp time.Time `json:"timestamp"`
}

// Format serializes the event into standard SSE wire format:
// event: <type>\n
// id: <id>\n
// data: <data>\n\n
func (e *Event) Format() []byte {
	var buf bytes.Buffer
	buf.WriteString("event: ")
	buf.WriteString(e.Type)
	buf.WriteByte('\n')

	if e.ID != "" {
		buf.WriteString("id: ")
		buf.WriteString(e.ID)
		buf.WriteByte('\n')
	}

	buf.WriteString("data: ")
	// Normalize any carriage returns in data to pure newlines for SSE spec compliance
	data := e.Data
	if bytes.Contains(data, []byte("\r\n")) {
		data = bytes.ReplaceAll(data, []byte("\r\n"), []byte("\n"))
	}
	buf.Write(data)
	buf.WriteString("\n\n")

	return buf.Bytes()
}

// FormatPing formats an SSE keep-alive comment line.
func FormatPing(t time.Time) []byte {
	return []byte(fmt.Sprintf(": ping %d\n\n", t.Unix()))
}

// IDGenerator generates monotonic, time-ordered unique event IDs.
type IDGenerator struct {
	seq atomic.Uint64
}

// NewIDGenerator constructs an IDGenerator.
func NewIDGenerator() *IDGenerator {
	return &IDGenerator{}
}

// Next generates a unique ID of the form "evt_<unix_seconds>_<seq>".
func (g *IDGenerator) Next(now time.Time) string {
	n := g.seq.Add(1)
	return fmt.Sprintf("evt_%d_%06d", now.Unix(), n)
}
