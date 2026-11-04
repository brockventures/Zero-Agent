package events

import (
	"context"
	"strconv"
	"sync"
	"testing"
	"time"
)

func TestHub_PublishAndSubscribe(t *testing.T) {
	t.Parallel()
	cfg := HubConfig{RingCapacity: 10, RingTTL: 5 * time.Minute, HeartbeatInterval: 15 * time.Second, SubscriberBuffer: 4, NowFunc: func() time.Time { return time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC) }}
	cfg.ApplyDefaults()
	hub := NewHub(cfg, nil, nil)
	defer hub.Close()
	if hub.stateProvider != nil || hub.RotationCoordinator() != nil || hub.ProviderCoordinator() != nil || hub.ring == nil || hub.idGen == nil || hub.cfg.RingCapacity != 10 {
		t.Fatal("unexpected initial state")
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ch1, unsub1 := hub.Subscribe(ctx)
	ch2, _ := hub.Subscribe(ctx)
	evt := hub.Publish(EventWidgetUpdate, []byte(`{"status":"ok"}`))

	for _, ch := range []<-chan *Event{ch1, ch2} {
		select {
		case r := <-ch:
			if r.ID != evt.ID || r.Type != EventWidgetUpdate {
				t.Fatalf("unexpected event: %+v", r)
			}
		case <-time.After(time.Second):
			t.Fatal("timeout")
		}
	}
	unsub1()
	hub.Unsubscribe(99999)
	if len(hub.subscribers) != 1 {
		t.Fatalf("expected 1, got %d", len(hub.subscribers))
	}
}

func TestHub_SlowConsumerDrop(t *testing.T) {
	t.Parallel()
	hub := NewHub(HubConfig{SubscriberBuffer: 1}, nil, nil)
	defer hub.Close()
	ch, unsub := hub.Subscribe(context.Background())
	defer unsub()

	if (&subscriber{closed: true}).trySend(&Event{}) != sendClosed {
		t.Fatal("expected sendClosed")
	}
	hub.Publish(EventWidgetUpdate, []byte("1"))
	hub.Publish(EventWidgetUpdate, []byte("2"))
	if len(hub.subscribers) != 0 || string((<-ch).Data) != "1" || (<-ch != nil) {
		t.Fatal("expected evicted and closed")
	}
}

func TestHub_ConcurrentPublishAndSubscribe(t *testing.T) {
	t.Parallel()
	hub := NewHub(HubConfig{RingCapacity: 500, SubscriberBuffer: 100}, nil, nil)
	defer hub.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var wg, pubWg sync.WaitGroup
	for range 5 {
		ch, unsub := hub.Subscribe(ctx)
		wg.Go(func() {
			defer unsub()
			for range ch {
			}
		})
	}
	for range 5 {
		pubWg.Go(func() {
			for range 20 {
				hub.Publish(EventWidgetUpdate, []byte("{}"))
			}
		})
	}
	pubWg.Wait()
	cancel()
	hub.Close()
	wg.Wait()
}

func TestHub_CloseAndEdgeCases(t *testing.T) {
	t.Parallel()
	hub := NewHub(HubConfig{}, nil, nil)
	if replayed, found := hub.ReplaySince(hub.Publish(EventSystemStatus, []byte("{}")).ID); !found || len(replayed) != 0 {
		t.Fatalf("unexpected replay hit: (%v, %v)", replayed, found)
	}
	if batch := hub.BuildHydration(); len(batch) < 6 {
		t.Fatalf("expected >= 6, got %d", len(batch))
	}
	hub.PublishEvent(nil)
	evtBlank := &Event{Type: "test_blank"}
	hub.PublishEvent(evtBlank)
	if evtBlank.ID == "" || evtBlank.Timestamp.IsZero() {
		t.Fatal("expected populated ID")
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	hub.Subscribe(ctx)
	hub.Publish(EventWidgetUpdate, []byte("{}"))
	ch, _ := hub.Subscribe(context.Background())
	hub.Close()
	hub.Close()
	hub.Subscribe(context.Background())
	if len(hub.subscribers) != 0 || (<-ch != nil) {
		t.Fatal("expected closed")
	}
	hub.Publish(EventWidgetUpdate, []byte("{}"))
}

func TestHub_PublishSyncsStateProvider(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, eventType, payload, wantVal string
	}{
		{"screen_rotate", EventScreenRotate, `{"current_screen":1,"total_screens":2,"interval_seconds":20}`, "1"},
		{"audio_state", EventAudioState, `{"volume":85,"muted":true}`, "85"},
		{"video_state", EventVideoState, `{"mode":"video","primary":{"id":"cast"}}`, "video"},
		{"voice_state", EventVoiceState, `{"state":"thinking","transcript":"hello"}`, "thinking"},
		{"theme_update", EventThemeUpdate, `{"theme":"night"}`, "night"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			provider := NewInMemoryStateProvider(nil)
			hub := NewHub(HubConfig{}, provider, nil)
			defer hub.Close()
			hub.Publish(tc.eventType, []byte(tc.payload))
			var got string
			switch tc.eventType {
			case EventScreenRotate:
				got = strconv.Itoa(provider.GetScreenRotateData().CurrentScreen)
			case EventAudioState:
				got = strconv.Itoa(provider.GetAudioState().Volume)
			case EventVideoState:
				got = provider.GetVideoState().Mode
			case EventVoiceState:
				got = provider.GetVoiceState().State
			case EventThemeUpdate:
				got = provider.GetTheme()
			}
			if got != tc.wantVal {
				t.Fatalf("got %q, want %q", got, tc.wantVal)
			}
		})
	}
}
