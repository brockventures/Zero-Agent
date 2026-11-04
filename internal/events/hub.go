package events

import (
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/azylman/mirrormere/internal/config"
)

const (
	DefaultSubscriberBuffer  = 128
	DefaultHeartbeatInterval = 15 * time.Second
)

type RotationCoordinator interface {
	UpdateConfig(snap *config.Snapshot) ScreenRotateData
}

type ProviderCoordinator interface {
	UpdateConfig(snap *config.Snapshot) error
}

type HubConfig struct {
	RingCapacity      int
	RingTTL           time.Duration
	HeartbeatInterval time.Duration
	SubscriberBuffer  int
	NowFunc           func() time.Time
}

func (c *HubConfig) ApplyDefaults() {
	if c.RingCapacity <= 0 {
		c.RingCapacity = DefaultRingCapacity
	}
	if c.RingTTL <= 0 {
		c.RingTTL = DefaultRingTTL
	}
	if c.HeartbeatInterval <= 0 {
		c.HeartbeatInterval = DefaultHeartbeatInterval
	}
	if c.SubscriberBuffer <= 0 {
		c.SubscriberBuffer = DefaultSubscriberBuffer
	}
	if c.NowFunc == nil {
		c.NowFunc = time.Now
	}
}

type sendResult int

const (
	sendSuccess sendResult = iota
	sendBufferFull
	sendClosed
)

type subscriber struct {
	id     uint64
	ch     chan *Event
	done   <-chan struct{}
	mu     sync.Mutex
	closed bool
}

func (s *subscriber) trySend(evt *Event) sendResult {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return sendClosed
	}
	select {
	case s.ch <- evt:
		return sendSuccess
	default:
		return sendBufferFull
	}
}

func (s *subscriber) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.closed {
		s.closed = true
		close(s.ch)
	}
}

type Hub struct {
	cfg                 HubConfig
	mu                  sync.RWMutex
	subscribers         map[uint64]*subscriber
	ring                *RingBuffer
	idGen               *IDGenerator
	stateProvider       StateProvider
	rotationCoordinator RotationCoordinator
	providerCoordinator ProviderCoordinator
	nextSubID           atomic.Uint64
	logger              *slog.Logger
	closed              atomic.Bool
}

func NewHub(cfg HubConfig, stateProvider StateProvider, logger *slog.Logger) *Hub {
	cfg.ApplyDefaults()
	if logger == nil {
		logger = slog.Default()
	}
	return &Hub{
		cfg:           cfg,
		subscribers:   make(map[uint64]*subscriber),
		ring:          NewRingBuffer(cfg.RingCapacity, cfg.RingTTL, cfg.NowFunc),
		idGen:         NewIDGenerator(),
		stateProvider: stateProvider,
		logger:        logger,
	}
}

func (h *Hub) SetRotationCoordinator(c RotationCoordinator) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.rotationCoordinator = c
}

func (h *Hub) RotationCoordinator() RotationCoordinator {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.rotationCoordinator
}

func (h *Hub) SetProviderCoordinator(c ProviderCoordinator) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.providerCoordinator = c
}

func (h *Hub) ProviderCoordinator() ProviderCoordinator {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.providerCoordinator
}

func (h *Hub) Publish(eventType string, data []byte) *Event {
	now := h.cfg.NowFunc()
	evt := &Event{ID: h.idGen.Next(now), Type: eventType, Data: data, Timestamp: now}
	h.PublishEvent(evt)
	return evt
}

func (h *Hub) PublishEvent(evt *Event) {
	if h.closed.Load() || evt == nil {
		return
	}
	now := h.cfg.NowFunc()
	if evt.Timestamp.IsZero() {
		evt.Timestamp = now
	}
	if evt.ID == "" {
		evt.ID = h.idGen.Next(now)
	}

	h.ring.Add(evt)
	if isp, ok := h.stateProvider.(*InMemoryStateProvider); ok {
		syncStateProvider(isp, evt)
	}

	h.mu.RLock()
	subs := make([]*subscriber, 0, len(h.subscribers))
	for _, sub := range h.subscribers {
		subs = append(subs, sub)
	}
	h.mu.RUnlock()

	for _, sub := range subs {
		if sub.done != nil {
			select {
			case <-sub.done:
				h.Unsubscribe(sub.id)
				continue
			default:
			}
		}
		if sub.trySend(evt) == sendBufferFull {
			h.logger.Warn("slow subscriber evicted", "subscriber_id", sub.id, "buffer_size", h.cfg.SubscriberBuffer)
			h.Unsubscribe(sub.id)
		}
	}
}

func (h *Hub) Subscribe(ctx context.Context) (<-chan *Event, func()) {
	subID := h.nextSubID.Add(1)
	var done <-chan struct{}
	if ctx != nil {
		done = ctx.Done()
	}
	sub := &subscriber{id: subID, ch: make(chan *Event, h.cfg.SubscriberBuffer), done: done}
	h.mu.Lock()
	if !h.closed.Load() {
		h.subscribers[subID] = sub
	} else {
		sub.close()
	}
	h.mu.Unlock()
	return sub.ch, func() { h.Unsubscribe(subID) }
}

func (h *Hub) Unsubscribe(subID uint64) {
	h.mu.Lock()
	sub, exists := h.subscribers[subID]
	delete(h.subscribers, subID)
	h.mu.Unlock()
	if exists {
		sub.close()
	}
}

func (h *Hub) ReplaySince(id string) ([]*Event, bool) { return h.ring.ReplaySince(id) }
func (h *Hub) BuildHydration() []*Event {
	return BuildHydrationBatch(h.stateProvider, h.idGen, h.cfg.NowFunc())
}

func (h *Hub) Close() {
	if !h.closed.CompareAndSwap(false, true) {
		return
	}
	h.mu.Lock()
	subs := make([]*subscriber, 0, len(h.subscribers))
	for id, sub := range h.subscribers {
		delete(h.subscribers, id)
		subs = append(subs, sub)
	}
	h.mu.Unlock()
	for _, sub := range subs {
		sub.close()
	}
}

func unmarshalInto[T any](data []byte, set func(*T)) {
	var v T
	if json.Unmarshal(data, &v) == nil {
		set(&v)
	}
}

func syncStateProvider(isp *InMemoryStateProvider, evt *Event) {
	switch evt.Type {
	case EventScreenRotate:
		unmarshalInto(evt.Data, isp.SetScreenRotateData)
	case EventAudioState:
		unmarshalInto(evt.Data, isp.SetAudioState)
	case EventVideoState:
		unmarshalInto(evt.Data, isp.SetVideoState)
	case EventVoiceState:
		unmarshalInto(evt.Data, isp.SetVoiceState)
	case EventThemeUpdate:
		unmarshalInto(evt.Data, func(td *ThemeUpdateData) { isp.SetTheme(td.Theme) })
	}
}
