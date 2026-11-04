package chatlog

import (
	"sync"
	"testing"
	"time"
)

// WithClock injects the time source (tests).
func WithClock(now func() time.Time) Option {
	return func(s *Store) {
		if now != nil {
			s.now = now
		}
	}
}

type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.t = c.t.Add(d)
	c.mu.Unlock()
}

func newClock() *fakeClock { return &fakeClock{t: time.Date(2026, 1, 2, 18, 0, 0, 0, time.UTC)} }

func texts(msgs []Message) []string {
	out := make([]string, 0, len(msgs))
	for _, m := range msgs {
		out = append(out, m.Text)
	}
	return out
}

func TestStore_TTLExpiry(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now), WithTTL(20*time.Minute))
	k := NodeKey("kitchen")

	s.Add(k, Message{Role: RoleHuman, Text: "first"})
	clk.Advance(15 * time.Minute)
	s.Add(k, Message{Role: RoleAgent, Text: "second"})

	if got := texts(s.Messages(nil)); len(got) != 2 {
		t.Fatalf("expected 2 live messages, got %v", got)
	}

	clk.Advance(6 * time.Minute) // first is now 21 min old, second 6 min
	if got := texts(s.Messages(nil)); len(got) != 1 || got[0] != "second" {
		t.Fatalf("expected only second to survive, got %v", got)
	}

	clk.Advance(15 * time.Minute)
	if got := s.Messages(nil); len(got) != 0 {
		t.Fatalf("expected everything expired, got %v", got)
	}
}

func TestStore_PerNodeIsolation(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now))
	s.Add(NodeKey("kitchen"), Message{Text: "k1"})
	clk.Advance(time.Second)
	s.Add(NodeKey("office"), Message{Text: "o1"})
	clk.Advance(time.Second)
	s.Add(NodeKey("kitchen"), Message{Text: "k2"})

	only := func(id string) func(string) bool {
		return func(key string) bool { return key == NodeKey(id) }
	}
	if got := texts(s.Messages(only("kitchen"))); len(got) != 2 || got[0] != "k1" || got[1] != "k2" {
		t.Fatalf("kitchen: %v", got)
	}
	if got := texts(s.Messages(only("office"))); len(got) != 1 || got[0] != "o1" {
		t.Fatalf("office: %v", got)
	}
	if got := texts(s.Messages(IsNodeKey)); len(got) != 3 || got[1] != "o1" {
		t.Fatalf("merged should interleave by time: %v", got)
	}
	if got := s.Messages(only("garage")); len(got) != 0 {
		t.Fatalf("unknown node must be empty: %v", got)
	}
}

func TestStore_Cap(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now), WithMaxPerKey(3))
	for _, txt := range []string{"1", "2", "3", "4", "5"} {
		s.Add(NodeKey("n"), Message{Text: txt})
		clk.Advance(time.Second)
	}
	s.Add(NodeKey("other"), Message{Text: "o"})
	got := texts(s.Messages(func(k string) bool { return k == NodeKey("n") }))
	if len(got) != 3 || got[0] != "3" || got[2] != "5" {
		t.Fatalf("expected newest 3 kept, got %v", got)
	}
	if got := s.Messages(func(k string) bool { return k == NodeKey("other") }); len(got) != 1 {
		t.Fatalf("cap must be per key, got %v", got)
	}
}

func TestStore_Replace(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now), WithTTL(time.Hour))
	k := WidgetKey("w1")
	s.Replace(k, []Message{{Text: "a"}, {Text: "b", At: clk.Now().Add(-2 * time.Hour)}})
	if got := texts(s.Messages(nil)); len(got) != 1 || got[0] != "a" {
		t.Fatalf("expired replacement message should be dropped: %v", got)
	}
	s.Replace(k, nil)
	if got := s.Messages(nil); len(got) != 0 {
		t.Fatalf("replace with nothing clears: %v", got)
	}
}

func TestStore_ViewPerNodeAndFallback(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now), WithTTL(10*time.Minute))
	s.Add(NodeKey("kitchen"), Message{Role: RoleHuman, Author: "sam", Text: "kitchen q"})
	clk.Advance(time.Second)
	s.Add(NodeKey("office"), Message{Role: RoleAgent, Text: "office a"})
	s.Replace(WidgetKey("w1"), []Message{{Role: RoleAgent, Text: "pushed", At: clk.Now().Add(time.Second)}})

	msgsOf := func(v map[string]any) []string {
		items := v["messages"].([]any)
		out := make([]string, 0, len(items))
		for _, m := range items {
			out = append(out, m.(map[string]any)["text"].(string))
		}
		return out
	}
	if got := msgsOf(s.View("w1", "kitchen")); len(got) != 2 || got[0] != "kitchen q" || got[1] != "pushed" {
		t.Fatalf("kitchen view: %v", got)
	}
	if got := msgsOf(s.View("w1", "office")); len(got) != 2 || got[0] != "office a" {
		t.Fatalf("office view: %v", got)
	}
	if got := msgsOf(s.View("w1", "")); len(got) != 2 || got[0] != "office a" {
		t.Fatalf("no node should show the most recent node: %v", got)
	}
	if got := msgsOf(s.View("w2", "garage")); len(got) != 0 {
		t.Fatalf("unknown node and other widget: %v", got)
	}
	v := s.View("w1", "kitchen")
	first := v["messages"].([]any)[0].(map[string]any)
	if first["author"] != "sam" || first["role"] != RoleHuman || v["updated_at"] == nil {
		t.Fatalf("unexpected entry %v / %v", first, v)
	}

	clk.Advance(11 * time.Minute) // everything expires, no timer needed
	if got := msgsOf(s.View("w1", "kitchen")); len(got) != 0 {
		t.Fatalf("expected expired view empty: %v", got)
	}
}

func TestParsePush(t *testing.T) {
	msgs, err := ParsePush(map[string]any{"messages": []any{
		map[string]any{"role": "human", "text": "hi", "author": "sam", "ts": "2026-01-02T18:00:00Z"},
		map[string]any{"role": "agent", "text": "yo"},
	}})
	if err != nil || len(msgs) != 2 || msgs[0].Author != "sam" || msgs[0].At.IsZero() || !msgs[1].At.IsZero() {
		t.Fatalf("got %+v, %v", msgs, err)
	}
	for _, bad := range []map[string]any{
		{"messages": []any{"x"}},
		{"messages": []any{map[string]any{"role": "robot", "text": "x"}}},
		{"messages": []any{map[string]any{"role": "human", "text": "x", "ts": "yesterday"}}},
	} {
		if _, err := ParsePush(bad); err == nil {
			t.Errorf("expected error for %v", bad)
		}
	}
}

func TestStore_ConcurrentUse(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	for i := range 8 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for range 100 {
				s.Add(NodeKey("n"), Message{Text: "x"})
				_ = s.Messages(nil)
			}
		}(i)
	}
	wg.Wait()
	if n := len(s.Messages(nil)); n != DefaultMaxPerKey {
		t.Fatalf("expected cap %d, got %d", DefaultMaxPerKey, n)
	}
}

func TestStore_MostRecentKey(t *testing.T) {
	clk := newClock()
	s := NewStore(WithClock(clk.Now), WithTTL(10*time.Minute))
	if _, ok := s.MostRecentKey(IsNodeKey); ok {
		t.Fatal("empty store has no recent key")
	}
	s.Add(NodeKey("kitchen"), Message{Text: "k"})
	clk.Advance(time.Minute)
	s.Add(NodeKey("office"), Message{Text: "o"})
	if k, ok := s.MostRecentKey(IsNodeKey); !ok || k != NodeKey("office") {
		t.Fatalf("got %q %v", k, ok)
	}
	clk.Advance(9*time.Minute + 30*time.Second) // kitchen expired, office still live
	if k, ok := s.MostRecentKey(IsNodeKey); !ok || k != NodeKey("office") {
		t.Fatalf("got %q %v", k, ok)
	}
}
