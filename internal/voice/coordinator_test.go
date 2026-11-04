package voice

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/events"
)

type mockSink struct {
	mu   sync.Mutex
	last *events.VoiceStateData
}

func (m *mockSink) SetVoiceState(state *events.VoiceStateData) {
	m.mu.Lock()
	m.last = state
	m.mu.Unlock()
}

func TestCoordinator_StateAndStatusTransitions(t *testing.T) {
	t.Parallel()
	sinkInit := &mockSink{}
	if st := NewCoordinator(nil, sinkInit).GetState(); st.State != StateIdle || st.Transcript != nil || sinkInit.last == nil || sinkInit.last.State != StateIdle {
		t.Fatalf("expected initial idle state, got %+v sink=%+v", st, sinkInit.last)
	}

	sink := &mockSink{}
	hub := events.NewHub(events.HubConfig{RingCapacity: 10, RingTTL: time.Minute}, nil, nil)
	t.Cleanup(hub.Close)
	subCtx := t.Context()
	sub, unsub := hub.Subscribe(subCtx)
	defer unsub()

	c := NewCoordinator(hub, sink)
	for _, tc := range []struct {
		state                             string
		transcript, reply, engine, status *string
	}{
		{StateIdle, nil, nil, nil, nil},
		{StateListening, new("listening..."), nil, nil, nil},
		{StateTranscribing, new("Turn on kitchen"), nil, nil, nil},
		{StateThinking, new("Turn on kitchen lights"), nil, nil, new("⚡ Running tool...")},
		{StateSynthesizing, new("Turn on kitchen lights"), new("Turning on lights"), new("kokoro"), nil},
		{StateSpeaking, new("Turn on kitchen lights"), new("Turning on lights"), new("kokoro"), nil},
		{StateError, nil, nil, nil, new("network error")},
	} {
		st, err := c.SetFullState(tc.state, tc.transcript, tc.reply, tc.engine, tc.status)
		if !IsValidState(tc.state) || err != nil || st.State != tc.state || sink.last == nil || sink.last.State != tc.state {
			t.Fatalf("SetFullState(%s): err=%v state=%s", tc.state, err, st.State)
		}
		select {
		case evt := <-sub:
			var vs events.VoiceStateData
			if json.Unmarshal(evt.Data, &vs) != nil || vs.State != tc.state {
				t.Fatalf("SSE mismatch for %s: %+v", tc.state, vs)
			}
		case <-time.After(time.Second):
			t.Fatalf("timeout waiting for SSE %s", tc.state)
		}
	}

	if st, err := c.SetState(StateThinking, new("q"), nil, nil); err != nil || st.Status != nil {
		t.Fatalf("SetState should clear status: %+v", st)
	}
	<-sub
	if st := c.SetStatus(new("⚡ Querying...")); st.State != StateThinking || st.Status == nil || *st.Status != "⚡ Querying..." {
		t.Fatalf("SetStatus mismatch: %+v", st)
	}
	<-sub
	if st := c.Reset(); st.State != StateIdle || st.Transcript != nil || st.Reply != nil || st.TTSEngine != nil || st.Status != nil {
		t.Fatalf("Reset mismatch: %+v", st)
	}
	<-sub

	for _, inv := range []string{"", "IDLE", "running", "LISTENING", "pending", "waiting", "unknown", " listening"} {
		if _, err := c.SetState(inv, nil, nil, nil); IsValidState(inv) || !errors.Is(err, ErrInvalidState) {
			t.Errorf("SetState(%q) err = %v", inv, err)
		}
		if _, err := c.SetFullState(inv, nil, nil, nil, nil); !errors.Is(err, ErrInvalidState) {
			t.Errorf("SetFullState(%q) err = %v", inv, err)
		}
	}

	txt := "test"
	for _, st := range []string{StateListening, StateTranscribing, StateThinking, StateSpeaking, StateSynthesizing, StateError} {
		ci := NewCoordinator(nil)
		ci.Transition(st, &txt, &txt, &txt, &txt)
		_, err1 := ci.SetState(StateIdle, nil, nil, nil)
		ci.Transition(st, &txt, &txt, &txt, &txt)
		_, err2 := ci.SetFullState(StateIdle, nil, nil, nil, nil)
		ci.Transition(st, &txt, &txt, &txt, &txt)
		s1 := ci.Reset()
		ci.Transition(st, &txt, &txt, &txt, &txt)
		s2 := ci.Transition(StateIdle, nil, nil, nil, nil)
		if err1 != nil || err2 != nil || s1.State != StateIdle || s2.State != StateIdle {
			t.Fatalf("idle transitions from %s failed", st)
		}
	}
}

func TestCoordinator_SpeakingSafetyWatchdog(t *testing.T) {
	t.Parallel()
	txt := "hello"
	cDefault := NewCoordinator(nil)
	cDefault.speakingTimeout = 0
	cDefault.Transition(StateSpeaking, &txt, nil, nil, nil)
	cDefault.Stop()

	hub := events.NewHub(events.HubConfig{RingCapacity: 10, RingTTL: time.Minute}, nil, nil)
	t.Cleanup(hub.Close)
	subCtx := t.Context()
	sub, unsub := hub.Subscribe(subCtx)
	defer unsub()

	c := NewCoordinator(hub)
	c.speakingTimeout = 10 * time.Millisecond
	c.Transition(StateSpeaking, &txt, nil, nil, nil)
	for _, want := range []string{StateSpeaking, StateIdle} {
		select {
		case evt := <-sub:
			var data events.VoiceStateData
			if json.Unmarshal(evt.Data, &data) != nil || data.State != want {
				t.Fatalf("expected %s event, got %+v", want, data)
			}
		case <-time.After(time.Second):
			t.Fatalf("timed out waiting for %s event", want)
		}
	}
	if st := c.GetState(); st.State != StateIdle {
		t.Fatalf("expected state idle, got %s", st.State)
	}

	cErr, cRst, cStop := NewCoordinator(nil), NewCoordinator(nil), NewCoordinator(nil)
	for _, coord := range []*Coordinator{cErr, cRst, cStop} {
		coord.speakingTimeout = 10 * time.Millisecond
		coord.Transition(StateSpeaking, &txt, nil, nil, nil)
	}
	cErr.Transition(StateError, nil, nil, nil, nil)
	cRst.Reset()
	cRst.Transition(StateListening, nil, nil, nil, nil)
	cStop.Stop()

	tm := time.NewTimer(25 * time.Millisecond)
	defer tm.Stop()
	<-tm.C
	if cErr.GetState().State != StateError || cRst.GetState().State != StateListening || cStop.GetState().State != StateSpeaking {
		t.Fatalf("watchdog cancel mismatch: err=%s rst=%s stop=%s", cErr.GetState().State, cRst.GetState().State, cStop.GetState().State)
	}
	cStop.Reset()
}

func TestCoordinator_ConcurrentAndSchema(t *testing.T) {
	t.Parallel()
	c := NewCoordinator(nil)
	states := []string{StateIdle, StateListening, StateTranscribing, StateThinking, StateSynthesizing, StateSpeaking, StateError}
	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			txt := "query"
			_, _ = c.SetState(states[idx%len(states)], &txt, nil, nil)
			_ = c.GetState()
		}(i)
	}
	wg.Wait()

	schemaBytes, err := os.ReadFile(filepath.Join("..", "..", "api", "schemas", "voice.state.json"))
	if err != nil {
		t.Skipf("skipping schema file test: %v", err)
	}
	var schema, parsed map[string]any
	tr, rep, eng := "Hello", "Hi", "piper"
	payload, err := json.Marshal(events.VoiceStateData{State: StateSpeaking, Transcript: &tr, Reply: &rep, TTSEngine: &eng})
	if err != nil || json.Unmarshal(schemaBytes, &schema) != nil || json.Unmarshal(payload, &parsed) != nil || parsed["state"] != StateSpeaking || parsed["tts_engine"] != eng {
		t.Fatalf("schema compliance failed: err=%v parsed=%+v", err, parsed)
	}
}
