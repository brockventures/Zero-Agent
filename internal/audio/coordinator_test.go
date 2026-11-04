package audio_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/audio"
	"github.com/azylman/mirrormere/internal/events"
)

type mockSink struct {
	mu    sync.Mutex
	state *events.AudioStateData
}

func (m *mockSink) SetAudioState(s *events.AudioStateData) {
	m.mu.Lock()
	m.state = &events.AudioStateData{Volume: s.Volume, Muted: s.Muted}
	m.mu.Unlock()
}

func assertAudioState(t *testing.T, sub <-chan *events.Event, sink *mockSink, wantVol int, wantMuted bool) {
	t.Helper()
	select {
	case evt, ok := <-sub:
		var data events.AudioStateData
		if !ok || evt.Type != events.EventAudioState || json.Unmarshal(evt.Data, &data) != nil || data.Volume != wantVol || data.Muted != wantMuted {
			t.Fatalf("unexpected event: ok=%v evt=%+v data=%+v", ok, evt, data)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for audio.state event")
	}
	sink.mu.Lock()
	last := sink.state
	sink.mu.Unlock()
	if last == nil || last.Volume != wantVol || last.Muted != wantMuted {
		t.Fatalf("sink mismatch: got %+v, want vol=%d muted=%v", last, wantVol, wantMuted)
	}
}

func TestCoordinator_VolumeAndMute(t *testing.T) {
	t.Parallel()
	sink := &mockSink{}
	if v, m := audio.NewCoordinator(nil, sink).GetState(); v != audio.DefaultVolume || m != audio.DefaultMuted || sink.state == nil || sink.state.Volume != audio.DefaultVolume {
		t.Fatalf("defaults mismatch: got vol %d, muted %v, sink %+v", v, m, sink.state)
	}

	hub := events.NewHub(events.HubConfig{RingCapacity: 10, RingTTL: time.Minute}, nil, nil)
	t.Cleanup(hub.Close)
	subCtx := t.Context()
	sub, unsub := hub.Subscribe(subCtx)
	defer unsub()

	coord := audio.NewCoordinator(hub, sink)
	for _, v := range []int{0, 50, 80, 100} {
		if vol, muted, err := coord.SetVolume(v); err != nil || vol != v || muted {
			t.Fatalf("SetVolume(%d): got (%d, %v, %v)", v, vol, muted, err)
		}
		assertAudioState(t, sub, sink, v, false)
	}
	for _, iv := range []int{-10, -1, 101, 200} {
		if _, _, err := coord.SetVolume(iv); !errors.Is(err, audio.ErrInvalidVolume) {
			t.Fatalf("SetVolume(%d) error = %v, want ErrInvalidVolume", iv, err)
		}
	}

	tVal, fVal := true, false
	for _, tc := range []struct {
		arg  *bool
		want bool
	}{{&tVal, true}, {&fVal, false}, {nil, true}, {nil, false}} {
		if vol, muted, err := coord.SetMute(tc.arg); err != nil || vol != 100 || muted != tc.want {
			t.Fatalf("SetMute(%v): got (%d, %v, %v)", tc.arg, vol, muted, err)
		}
		assertAudioState(t, sub, sink, 100, tc.want)
	}
}

func TestCoordinator_ConcurrentAndSchema(t *testing.T) {
	t.Parallel()
	hub := events.NewHub(events.HubConfig{RingCapacity: 100, RingTTL: time.Minute}, nil, nil)
	t.Cleanup(hub.Close)
	coord := audio.NewCoordinator(hub, &mockSink{})

	var wg sync.WaitGroup
	for i := range 4 {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for j := range 50 {
				_, _, _ = coord.SetVolume((id*20 + j) % 101)
				_, _, _ = coord.SetMute(nil)
				if v, _ := coord.GetState(); v < 0 || v > 100 {
					t.Errorf("invalid volume: %d", v)
				}
			}
		}(i)
	}
	wg.Wait()

	schemaBytes, err := os.ReadFile(filepath.Join("..", "..", "api", "schemas", "audio.state.json"))
	if err != nil {
		t.Skipf("skipping schema file test: %v", err)
	}
	var schema, parsed map[string]any
	payload, err := json.Marshal(events.AudioStateData{Volume: 75, Muted: false})
	if err != nil || json.Unmarshal(schemaBytes, &schema) != nil || json.Unmarshal(payload, &parsed) != nil || parsed["volume"] == nil || parsed["muted"] == nil {
		t.Fatalf("schema validation failed: err=%v parsed=%v", err, parsed)
	}
}
