package events

import (
	"sync"
	"time"
)

const (
	// DefaultRingCapacity is 1,000 events.
	DefaultRingCapacity = 1000
	// DefaultRingTTL is 5 minutes.
	DefaultRingTTL = 5 * time.Minute
	// MaxEventIDLength prevents processing adversarial header values.
	MaxEventIDLength = 128
)

type ringEntry struct {
	event     *Event
	createdAt time.Time
}

// RingBuffer stores a circular buffer of recent events with dual capacity and TTL boundaries.
type RingBuffer struct {
	mu       sync.RWMutex
	entries  []ringEntry
	capacity int
	ttl      time.Duration
	head     int // next write index
	count    int // number of valid entries currently stored
	nowFunc  func() time.Time
}

// NewRingBuffer constructs a RingBuffer with specified capacity, TTL, and clock.
func NewRingBuffer(capacity int, ttl time.Duration, nowFunc func() time.Time) *RingBuffer {
	if capacity <= 0 {
		capacity = DefaultRingCapacity
	}
	if ttl <= 0 {
		ttl = DefaultRingTTL
	}
	if nowFunc == nil {
		nowFunc = time.Now
	}

	return &RingBuffer{
		entries:  make([]ringEntry, capacity),
		capacity: capacity,
		ttl:      ttl,
		nowFunc:  nowFunc,
	}
}

// Add appends an event to the ring buffer, overwriting the oldest entry if full.
func (r *RingBuffer) Add(event *Event) {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := r.nowFunc()
	if event.Timestamp.IsZero() {
		event.Timestamp = now
	}

	r.entries[r.head] = ringEntry{
		event:     event,
		createdAt: now,
	}

	r.head = (r.head + 1) % r.capacity
	if r.count < r.capacity {
		r.count++
	}
}

// ReplaySince searches for lastEventID and returns all events chronologically subsequent to it.
// If lastEventID is empty, not found, or older than TTL, it returns (nil, false) to indicate a cache miss.
func (r *RingBuffer) ReplaySince(lastEventID string) ([]*Event, bool) {
	if lastEventID == "" || len(lastEventID) > MaxEventIDLength {
		return nil, false
	}

	r.mu.RLock()
	defer r.mu.RUnlock()

	if r.count == 0 {
		return nil, false
	}

	now := r.nowFunc()

	// Calculate oldest index: (head - count + capacity) % capacity
	startIdx := (r.head - r.count + r.capacity) % r.capacity

	foundIdx := -1
	for i := range r.count {
		idx := (startIdx + i) % r.capacity
		entry := r.entries[idx]
		if entry.event != nil && entry.event.ID == lastEventID {
			// Found matching event; check TTL
			elapsed := now.Sub(entry.createdAt)
			if elapsed < 0 || elapsed > r.ttl {
				// Expired event
				return nil, false
			}
			foundIdx = i
			break
		}
	}

	if foundIdx == -1 {
		return nil, false
	}

	// Collect subsequent events
	numSubsequent := r.count - 1 - foundIdx
	if numSubsequent <= 0 {
		return []*Event{}, true
	}

	result := make([]*Event, 0, numSubsequent)
	for i := foundIdx + 1; i < r.count; i++ {
		idx := (startIdx + i) % r.capacity
		entry := r.entries[idx]
		if entry.event != nil {
			// Check if subsequent event has also expired (defensive check)
			elapsed := now.Sub(entry.createdAt)
			if elapsed >= 0 && elapsed <= r.ttl {
				result = append(result, entry.event)
			}
		}
	}

	return result, true
}
