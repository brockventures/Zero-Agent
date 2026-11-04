package events

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestRingBuffer_DefaultsAndLimits(t *testing.T) {
	t.Parallel()
	r := NewRingBuffer(0, 0, nil)
	if r.capacity != DefaultRingCapacity || r.ttl != DefaultRingTTL || r.nowFunc == nil {
		t.Fatalf("unexpected defaults: cap %d, ttl %v", r.capacity, r.ttl)
	}
}

func TestRingBuffer_AddAndCount(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC)
	r := NewRingBuffer(3, 5*time.Minute, func() time.Time { return now })
	if r.count != 0 {
		t.Fatalf("expected count 0, got %d", r.count)
	}
	evt1 := &Event{ID: "evt_1", Type: "test"}
	r.Add(evt1)
	r.Add(&Event{ID: "evt_2", Type: "test"})
	r.Add(&Event{ID: "evt_3", Type: "test"})
	r.Add(&Event{ID: "evt_4", Type: "test"})
	if r.count != 3 || evt1.Timestamp != now {
		t.Fatalf("expected count 3, got %d", r.count)
	}
}

func TestRingBuffer_ReplaySince(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC)

	tests := []struct {
		name       string
		preAdd     int
		searchID   string
		advance    time.Duration
		wantFound  bool
		wantEvents int
	}{
		{"empty_buffer", 0, "evt_1", 0, false, 0},
		{"empty_id", 4, "", 0, false, 0},
		{"oversized_id", 4, strings.Repeat("x", MaxEventIDLength+1), 0, false, 0},
		{"unknown_id", 4, "evt_999", 0, false, 0},
		{"latest_event", 4, "evt_4", 0, true, 0},
		{"middle_event", 4, "evt_2", 0, true, 2},
		{"oldest_event", 4, "evt_1", 0, true, 3},
		{"ttl_expired", 4, "evt_2", 10 * time.Minute, false, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			curr := now
			r := NewRingBuffer(5, 5*time.Minute, func() time.Time { return curr })
			for i := 1; i <= tc.preAdd; i++ {
				r.Add(&Event{ID: fmt.Sprintf("evt_%d", i), Type: "test", Timestamp: curr})
			}
			curr = curr.Add(tc.advance)
			evts, found := r.ReplaySince(tc.searchID)
			if found != tc.wantFound || len(evts) != tc.wantEvents {
				t.Fatalf("got (found=%v, len=%d), want (found=%v, len=%d)", found, len(evts), tc.wantFound, tc.wantEvents)
			}
		})
	}
}
