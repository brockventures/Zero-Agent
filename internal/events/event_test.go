package events

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestEvent_Format(t *testing.T) {
	t.Parallel()
	evt := &Event{ID: "evt_123", Type: EventWidgetUpdate, Data: []byte("{\"hello\":\r\n\"world\"}")}
	want := "event: widget.update\nid: evt_123\ndata: {\"hello\":\n\"world\"}\n\n"
	if got := string(evt.Format()); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}

	evtNoID := &Event{Type: EventSystemStatus, Data: []byte("{\"status\":\"ok\"}")}
	wantNoID := "event: system.status\ndata: {\"status\":\"ok\"}\n\n"
	if got := string(evtNoID.Format()); got != wantNoID {
		t.Fatalf("got %q, want %q", got, wantNoID)
	}
}

func TestFormatPing(t *testing.T) {
	t.Parallel()
	refTime := time.Date(2026, 9, 25, 20, 15, 30, 0, time.UTC)
	if ping, want := FormatPing(refTime), ": ping 1790367330\n\n"; !bytes.Equal(ping, []byte(want)) {
		t.Fatalf("got %q, want %q", string(ping), want)
	}
}

func TestIDGenerator(t *testing.T) {
	t.Parallel()
	gen := NewIDGenerator()
	now := time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC)
	id1, id2 := gen.Next(now), gen.Next(now)
	if !strings.HasPrefix(id1, "evt_") || !strings.HasPrefix(id2, "evt_") || id1 == id2 {
		t.Fatalf("unexpected IDs: %q, %q", id1, id2)
	}
}
