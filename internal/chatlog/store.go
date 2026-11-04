// Package chatlog holds the in-memory conversation transcript that backs the
// chat-log widget. Voice turns recorded by the voice hub and payloads pushed to
// a chat-log widget both land here. Nothing is persisted: a restart starts with
// an empty transcript, and entries expire after a TTL.
package chatlog

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// DefaultTTL is how long a message stays visible after it was said.
	DefaultTTL = 20 * time.Minute
	// DefaultMaxPerKey caps the messages retained per conversation key.
	DefaultMaxPerKey = 50

	// RoleHuman and RoleAgent are the two message roles the widget renders.
	RoleHuman = "human"
	RoleAgent = "agent"

	nodePrefix   = "node:"
	widgetPrefix = "widget:"
)

// NodeKey is the conversation key for a voice node (edge device).
func NodeKey(nodeID string) string {
	nodeID = strings.TrimSpace(nodeID)
	if nodeID == "" {
		nodeID = "default"
	}
	return nodePrefix + nodeID
}

// WidgetKey is the conversation key for messages pushed to a widget instance
// through POST /api/widgets/{id}/push.
func WidgetKey(widgetID string) string { return widgetPrefix + widgetID }

// IsNodeKey reports whether key was produced by NodeKey.
func IsNodeKey(key string) bool { return strings.HasPrefix(key, nodePrefix) }

// Message is one line of conversation.
type Message struct {
	Role   string
	Author string
	Text   string
	At     time.Time
}

// Option configures a Store.
type Option func(*Store)

// WithTTL sets the message lifetime. Non-positive values keep the default.
func WithTTL(ttl time.Duration) Option {
	return func(s *Store) {
		if ttl > 0 {
			s.ttl = ttl
		}
	}
}

// WithMaxPerKey sets the per-key message cap. Non-positive values keep the default.
func WithMaxPerKey(n int) Option {
	return func(s *Store) {
		if n > 0 {
			s.max = n
		}
	}
}

// Store is a thread-safe, TTL-bounded, per-key message store.
type Store struct {
	mu   sync.Mutex
	ttl  time.Duration
	max  int
	now  func() time.Time
	data map[string][]Message
}

// NewStore constructs a Store with defaults (20 minute TTL, 50 messages per key).
func NewStore(opts ...Option) *Store {
	s := &Store{
		ttl:  DefaultTTL,
		max:  DefaultMaxPerKey,
		now:  time.Now,
		data: make(map[string][]Message),
	}
	for _, opt := range opts {
		opt(s)
	}
	return s
}

// Add appends a message under key. A zero At is stamped with the current time.
func (s *Store) Add(key string, m Message) {
	s.mu.Lock()
	if m.At.IsZero() {
		m.At = s.now()
	}
	s.data[key] = append(s.data[key], m)
	s.sortAndPruneLocked(key)
	s.mu.Unlock()
}

// Replace swaps the whole transcript under key (used by the push webhook,
// whose payload is a full transcript). Messages with a zero At are stamped now.
func (s *Store) Replace(key string, msgs []Message) {
	s.mu.Lock()
	now := s.now()
	out := make([]Message, len(msgs))
	copy(out, msgs)
	for i := range out {
		if out[i].At.IsZero() {
			out[i].At = now
		}
	}
	s.data[key] = out
	s.sortAndPruneLocked(key)
	s.mu.Unlock()
}

// ParsePush converts a pushed chat-log payload ({"messages":[{role,text,author?,ts?}]})
// into messages. Shape errors (missing role/text) are normally caught earlier by the
// manifest response_schema; this only fails on what a schema cannot check (RFC 3339 ts).
func ParsePush(data map[string]any) ([]Message, error) {
	raw, ok := data["messages"].([]any)
	if !ok && data["messages"] != nil {
		return nil, fmt.Errorf("messages must be an array")
	}
	out := make([]Message, 0, len(raw))
	for i, r := range raw {
		obj, ok := r.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("messages[%d] must be an object", i)
		}
		role, roleOK := obj["role"].(string)
		text, textOK := obj["text"].(string)
		if !textOK {
			return nil, fmt.Errorf("messages[%d].text must be a string", i)
		}
		if !roleOK || (role != RoleHuman && role != RoleAgent) {
			return nil, fmt.Errorf("messages[%d].role must be %q or %q", i, RoleHuman, RoleAgent)
		}
		m := Message{Role: role, Text: text}
		if author, ok := obj["author"].(string); ok {
			m.Author = author
		}
		if ts, ok := obj["ts"].(string); ok && ts != "" {
			at, err := time.Parse(time.RFC3339, ts)
			if err != nil {
				return nil, fmt.Errorf("messages[%d].ts must be RFC 3339", i)
			}
			m.At = at
		}
		out = append(out, m)
	}
	return out, nil
}

// View returns the chat-log widget's render data for one display: the widget's
// own pushed transcript plus the voice conversation of nodeID, or of the most
// recently active node when nodeID is empty. Expired messages are never included.
func (s *Store) View(widgetID, nodeID string) map[string]any {
	own := WidgetKey(widgetID)
	nodeKey := ""
	if nodeID = strings.TrimSpace(nodeID); nodeID != "" {
		nodeKey = NodeKey(nodeID)
	} else if k, ok := s.MostRecentKey(IsNodeKey); ok {
		nodeKey = k
	}
	msgs := s.Messages(func(key string) bool { return key == own || (nodeKey != "" && key == nodeKey) })

	out := make([]any, 0, len(msgs))
	for _, m := range msgs {
		entry := map[string]any{
			"role": m.Role,
			"text": m.Text,
			"ts":   m.At.UTC().Format(time.RFC3339),
		}
		if m.Author != "" {
			entry["author"] = m.Author
		}
		out = append(out, entry)
	}
	data := map[string]any{"messages": out}
	if n := len(msgs); n > 0 {
		data["updated_at"] = msgs[n-1].At.UTC().Format(time.RFC3339)
	}
	return data
}

// Messages returns the live messages of every key accepted by match (all keys
// when match is nil), merged oldest first.
func (s *Store) Messages(match func(key string) bool) []Message {
	s.mu.Lock()
	defer s.mu.Unlock()
	keys := make([]string, 0, len(s.data))
	for key := range s.data {
		keys = append(keys, key)
	}
	sort.Strings(keys) // deterministic order for messages with equal timestamps
	var out []Message
	for _, key := range keys {
		s.sortAndPruneLocked(key)
		if match != nil && !match(key) {
			continue
		}
		out = append(out, s.data[key]...)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].At.Before(out[j].At) })
	return out
}

// MostRecentKey returns the accepted key holding the newest live message.
func (s *Store) MostRecentKey(match func(key string) bool) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var (
		best  string
		bestT time.Time
		found bool
	)
	for key := range s.data {
		s.sortAndPruneLocked(key)
		msgs := s.data[key]
		if len(msgs) == 0 || (match != nil && !match(key)) {
			continue
		}
		if last := msgs[len(msgs)-1].At; !found || last.After(bestT) {
			best, bestT, found = key, last, true
		}
	}
	return best, found
}

// sortAndPruneLocked orders one key oldest first, drops expired messages and
// applies the cap. Caller holds s.mu.
func (s *Store) sortAndPruneLocked(key string) {
	msgs := s.data[key]
	sort.SliceStable(msgs, func(i, j int) bool { return msgs[i].At.Before(msgs[j].At) })
	cutoff := s.now().Add(-s.ttl)
	start := 0
	for start < len(msgs) && !msgs[start].At.After(cutoff) {
		start++
	}
	msgs = msgs[start:]
	if len(msgs) > s.max {
		msgs = msgs[len(msgs)-s.max:]
	}
	if len(msgs) == 0 {
		delete(s.data, key)
		return
	}
	s.data[key] = append([]Message(nil), msgs...)
}
