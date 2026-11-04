package provider_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/provider"
	"github.com/azylman/mirrormere/internal/tasks"
)

type mockBroadcaster struct {
	mu        sync.Mutex
	events    []*events.Event
	publishCh chan *events.Event
}

func (m *mockBroadcaster) Publish(eventType string, data []byte) *events.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	evt := &events.Event{ID: "evt_mock", Type: eventType, Data: data, Timestamp: time.Now()}
	m.events = append(m.events, evt)
	if m.publishCh == nil {
		m.publishCh = make(chan *events.Event, 64)
	}
	select {
	case m.publishCh <- evt:
	default:
	}
	return evt
}

func (m *mockBroadcaster) waitForPublish(timeout time.Duration) (*events.Event, bool) {
	m.mu.Lock()
	if m.publishCh == nil {
		m.publishCh = make(chan *events.Event, 64)
	}
	ch := m.publishCh
	m.mu.Unlock()
	select {
	case evt := <-ch:
		return evt, true
	case <-time.After(timeout):
		return nil, false
	}
}

func (m *mockBroadcaster) getEvents() []*events.Event {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]*events.Event(nil), m.events...)
}

type widgetStateEntry struct {
	data             any
	state, timestamp string
}

type mockStateSink struct {
	mu              sync.Mutex
	widgetStates    map[string]widgetStateEntry
	providersStatus map[string]string
}

func newMockStateSink() *mockStateSink {
	return &mockStateSink{widgetStates: make(map[string]widgetStateEntry)}
}

func (s *mockStateSink) SetWidgetState(id string, data any, state string, timestamp ...string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.widgetStates[id] = widgetStateEntry{data: data, state: state, timestamp: strings.Join(timestamp, "")}
}

func (s *mockStateSink) SetProvidersStatus(p map[string]string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.providersStatus = p
}

func (s *mockStateSink) getState(id string) (any, string, string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.widgetStates[id]
	return e.data, e.state, e.timestamp, ok
}

type controllableMockProvider struct {
	mu                           sync.Mutex
	fetchFunc                    func(ctx context.Context) (any, error)
	initFunc                     func(ctx context.Context, config map[string]any, opts provider.InitOptions) error
	subSink                      chan<- provider.WidgetPayload
	initCalled, shutDown         bool
	initCfg                      map[string]any
	initOpts                     provider.InitOptions
	fetchCount                   atomic.Int64
	fetchSignal, subscribeSignal chan struct{}
}

func newControllableMockProvider() *controllableMockProvider {
	return &controllableMockProvider{fetchSignal: make(chan struct{}, 10), subscribeSignal: make(chan struct{}, 10)}
}

func (p *controllableMockProvider) setFetch(fn func(ctx context.Context) (any, error)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.fetchFunc = fn
}

func (p *controllableMockProvider) Init(ctx context.Context, cfg map[string]any, opts provider.InitOptions) error {
	p.mu.Lock()
	p.initCalled, p.initCfg, p.initOpts = true, cfg, opts
	fn := p.initFunc
	p.mu.Unlock()
	if fn != nil {
		return fn(ctx, cfg, opts)
	}
	return nil
}

func (p *controllableMockProvider) Fetch(ctx context.Context) (any, error) {
	p.fetchCount.Add(1)
	select {
	case p.fetchSignal <- struct{}{}:
	default:
	}
	p.mu.Lock()
	fn := p.fetchFunc
	p.mu.Unlock()
	if fn != nil {
		return fn(ctx)
	}
	return map[string]any{"status": "ok"}, nil
}

func (p *controllableMockProvider) Subscribe(ctx context.Context, sink chan<- provider.WidgetPayload) error {
	p.mu.Lock()
	p.subSink = sink
	p.mu.Unlock()
	select {
	case p.subscribeSignal <- struct{}{}:
	default:
	}
	<-ctx.Done()
	return ctx.Err()
}

func (p *controllableMockProvider) Shutdown(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.shutDown = true
	return nil
}

func (p *controllableMockProvider) emitPush(payload provider.WidgetPayload) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.subSink != nil {
		p.subSink <- payload
	}
}

func waitForState(t *testing.T, cache *provider.SWRCache, id, state string) provider.WidgetPayload {
	t.Helper()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); <-ticker.C {
		if p, ok := cache.Get(id); ok && p.State == state {
			return p
		}
	}
	p, _ := cache.Get(id)
	t.Fatalf("timeout %q -> %q (current: %s)", id, state, p.State)
	return p
}

func waitSignal(t *testing.T, ch <-chan struct{}, desc string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(2 * time.Second):
		t.Fatalf("timeout waiting for %s", desc)
	}
}

func waitForPayload(t *testing.T, b *mockBroadcaster, id, key, state string) provider.WidgetPayload {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); {
		if evt, ok := b.waitForPublish(200 * time.Millisecond); ok {
			var p provider.WidgetPayload
			if json.Unmarshal(evt.Data, &p) == nil && (id == "" || p.WidgetID == id) && (state == "" || p.State == state) && (key == "" || (p.Data != nil && p.Data.(map[string]any)[key] != nil)) {
				return p
			}
		}
	}
	t.Fatalf("timeout broadcast %q", id)
	return provider.WidgetPayload{}
}

func hasEvent(b *mockBroadcaster, id, state string) bool {
	for _, e := range b.getEvents() {
		var p provider.WidgetPayload
		if json.Unmarshal(e.Data, &p) == nil && p.WidgetID == id && p.State == state {
			return true
		}
	}
	return false
}

func refresh(t *testing.T, c *provider.ProviderCoordinator, id string) {
	t.Helper()
	if err := c.RefreshWidget(id); err != nil {
		t.Fatalf("refresh %s: %v", id, err)
	}
}

func mkWidget(id, typ string, interval int, cfg map[string]any) config.WidgetConfig {
	c := config.WidgetConfig{ID: id, Type: typ, Config: cfg}
	if interval > 0 {
		c.RefreshIntervalSeconds = new(interval)
	}
	return c
}

func mkSnap(tz string, pkgs map[string]*domain.Package, widgets ...config.WidgetConfig) *config.Snapshot {
	return &config.Snapshot{Config: &config.Config{Timezone: tz, Display: config.DisplayConfig{Widgets: widgets}}, Packages: pkgs}
}

func mkPkg(name, prov string, interval int, schema map[string]any) *domain.Package {
	return &domain.Package{Type: name, Manifest: domain.WidgetManifest{Name: name, Provider: prov, Refresh: domain.ManifestRefresh{IntervalSeconds: interval}, ResponseSchema: schema}}
}

func regOne(name string, p provider.Provider) provider.Registry {
	r := provider.NewRegistry()
	r.Register(name, func() provider.Provider { return p })
	return r
}

func regSeq(name string, p1, p2 provider.Provider) provider.Registry {
	var count atomic.Int32
	r := provider.NewRegistry()
	r.Register(name, func() provider.Provider {
		if count.Add(1) == 1 {
			return p1
		}
		return p2
	})
	return r
}

func regTypes(types ...string) provider.Registry {
	r := provider.NewRegistry()
	for _, typ := range types {
		r.Register(typ, func() provider.Provider { return newControllableMockProvider() })
	}
	return r
}

func newCoord(r provider.Registry, b *mockBroadcaster, s *mockStateSink, snap *config.Snapshot) *provider.ProviderCoordinator {
	cfg := provider.CoordinatorConfig{Registry: r}
	if b != nil {
		cfg.Broadcaster = b
	}
	if s != nil {
		cfg.StateSink = s
	}
	return provider.NewCoordinator(cfg, snap)
}

func TestProviderCoordinator_ZeroPollingOptimizations(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ id, typ string }{{"spacer-tile", "spacer"}, {"chromecast-tile", "live-view"}} {
		b, s := &mockBroadcaster{}, newMockStateSink()
		coord := newCoord(nil, b, s, mkSnap("", map[string]*domain.Package{tc.typ: mkPkg(tc.typ, tc.typ, 0, nil)}, mkWidget(tc.id, tc.typ, 30, nil)))
		p, ok := coord.Cache().Get(tc.id)
		d, st, _, sOk := s.getState(tc.id)
		if !ok || p.State != provider.StateHealthy || !sOk || st != provider.StateHealthy || len(p.Data.(map[string]any)) != 0 || len(d.(map[string]any)) != 0 || len(b.getEvents()) != 1 || coord.RefreshWidget(tc.id) == nil {
			t.Fatalf("unexpected state or error for %s", tc.id)
		}
		_ = coord.Stop()
	}
}

func TestProviderCoordinator_LifecycleAndSWR(t *testing.T) {
	t.Parallel()
	mockP1, b1, s1 := newControllableMockProvider(), &mockBroadcaster{}, newMockStateSink()
	coord1 := newCoord(regOne("mock-weather", mockP1), b1, s1, mkSnap("", nil, config.WidgetConfig{
		ID: "weather-tile", Type: "mock-weather", Dimensions: []int{2, 1}, RefreshIntervalSeconds: new(60),
		Config: map[string]any{"city": "Oakland"}, Endpoint: "https://api.example.com/weather", Token: "secret123",
	}))
	defer coord1.Stop()
	if _, ok := b1.waitForPublish(2 * time.Second); !ok {
		t.Fatal("timeout waiting for initial broadcast")
	}
	if p, ok := coord1.Cache().Get("weather-tile"); !ok || p.State != provider.StateHealthy {
		t.Fatalf("expected healthy weather-tile, got %+v", p)
	}
	for _, id := range []string{"weather-tile", "unknown-widget"} {
		d, st, ts, sOk := coord1.GetWidgetState(id)
		tTs, tOk := coord1.GetWidgetTimestamp(id)
		if expected := (id == "weather-tile"); sOk != expected || tOk != expected || (expected && (st != provider.StateHealthy || d == nil || ts == "" || tTs == "")) {
			t.Fatalf("unexpected state/timestamp for %s", id)
		}
	}
	refresh(t, coord1, "weather-tile")
	if _, ok := b1.waitForPublish(2 * time.Second); !ok || mockP1.fetchCount.Load() < 2 {
		t.Fatalf("manual refresh failed: ok=%v, count=%d", ok, mockP1.fetchCount.Load())
	}
	mockStocks := newControllableMockProvider()
	mockStocks.fetchFunc = func(ctx context.Context) (any, error) { return map[string]any{"price": 150.0}, nil }
	bStocks, sStocks := &mockBroadcaster{}, newMockStateSink()
	coordStocks := newCoord(regOne("mock-stocks", mockStocks), bStocks, sStocks, mkSnap("", nil, mkWidget("stock-tile", "mock-stocks", 10, nil)))
	defer coordStocks.Stop()
	waitSignal(t, mockStocks.fetchSignal, "initial fetch")
	p1 := waitForState(t, coordStocks.Cache(), "stock-tile", provider.StateHealthy)
	if _, ok := bStocks.waitForPublish(2 * time.Second); !ok {
		t.Fatal("timeout waiting for initial broadcast")
	}
	lkgTimestamp := p1.Timestamp
	mockStocks.setFetch(func(ctx context.Context) (any, error) { return nil, errors.New("502 bad gateway") })
	refresh(t, coordStocks, "stock-tile")
	waitSignal(t, mockStocks.fetchSignal, "first failure fetch")
	p2 := waitForState(t, coordStocks.Cache(), "stock-tile", provider.StateDegraded)
	if _, ok := bStocks.waitForPublish(2 * time.Second); !ok || p2.Timestamp != lkgTimestamp || p2.Data.(map[string]any)["price"] != 150.0 {
		t.Fatalf("expected preserved LKG data, got %+v", p2)
	}
	eventsCountDegraded := len(bStocks.getEvents())
	refresh(t, coordStocks, "stock-tile")
	waitSignal(t, mockStocks.fetchSignal, "consecutive failure fetch")
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for deadline := time.Now().Add(2 * time.Second); coordStocks.Cache().GetConsecutiveFailures("stock-tile") < 2; <-ticker.C {
		if !time.Now().Before(deadline) {
			t.Fatal("timeout waiting for consecutive failure record")
		}
	}
	if n := len(bStocks.getEvents()); n != eventsCountDegraded {
		t.Fatalf("redundant broadcast on degraded retry: %d != %d", n, eventsCountDegraded)
	}
	mockStocks.setFetch(func(ctx context.Context) (any, error) { return map[string]any{"price": 155.0}, nil })
	refresh(t, coordStocks, "stock-tile")
	p3 := waitForState(t, coordStocks.Cache(), "stock-tile", provider.StateHealthy)
	if p3.Data.(map[string]any)["price"] != 155.0 {
		t.Fatalf("expected price 155.0, got %v", p3.Data)
	}
	regFail := provider.NewRegistry()
	mockFail, mockInitFail := newControllableMockProvider(), newControllableMockProvider()
	mockFail.fetchFunc = func(ctx context.Context) (any, error) { return nil, errors.New("upstream unreachable") }
	mockInitFail.initFunc = func(ctx context.Context, config map[string]any, opts provider.InitOptions) error {
		return errors.New("bad credentials")
	}
	regFail.Register("mock-failing", func() provider.Provider { return mockFail })
	regFail.Register("failing-init", func() provider.Provider { return mockInitFail })
	bFail, sFail := &mockBroadcaster{}, newMockStateSink()
	coordFail := newCoord(regFail, bFail, sFail, mkSnap("", nil,
		mkWidget("failing-tile", "mock-failing", 0, nil),
		mkWidget("unregistered-widget", "unregistered-type", 0, nil),
		mkWidget("bad-init-widget", "failing-init", 0, nil),
	))
	defer coordFail.Stop()
	for _, id := range []string{"failing-tile", "unregistered-widget", "bad-init-widget"} {
		waitForState(t, coordFail.Cache(), id, provider.StateError)
	}
	payload, err := coordFail.PushUpdate("failing-tile", map[string]any{"pushed": true, "val": 42})
	pUpdated, _ := coordFail.Cache().Get("failing-tile")
	if err != nil || payload.State != provider.StateHealthy || pUpdated.State != provider.StateHealthy || !hasEvent(bFail, "failing-tile", provider.StateHealthy) {
		t.Fatalf("PushUpdate failed or inconsistent: %v, %s", err, payload.State)
	}
}

func TestProviderCoordinator_PushSubscriptionAndAtomicity(t *testing.T) {
	t.Parallel()
	mockStream, bStream, sStream := newControllableMockProvider(), &mockBroadcaster{}, newMockStateSink()
	coordStream := newCoord(regOne("streaming-provider", mockStream), bStream, sStream, mkSnap("", nil, mkWidget("streaming-tile", "streaming-provider", 0, nil)))
	defer coordStream.Stop()
	waitSignal(t, mockStream.fetchSignal, "initial fetch")
	waitForState(t, coordStream.Cache(), "streaming-tile", provider.StateHealthy)
	waitForPayload(t, bStream, "streaming-tile", "status", provider.StateHealthy)
	waitSignal(t, mockStream.subscribeSignal, "subscribe setup")
	mockStream.emitPush(provider.WidgetPayload{State: provider.StateHealthy, Data: map[string]any{"stream_event": "alert_fired"}, Timestamp: time.Now().UTC().Format(time.RFC3339)})
	pStream := waitForPayload(t, bStream, "streaming-tile", "stream_event", "")
	if d, ok := pStream.Data.(map[string]any); !ok || d["stream_event"] != "alert_fired" {
		t.Fatalf("unexpected broadcast data: %v", pStream.Data)
	}
	if pCached, ok := coordStream.Cache().Get("streaming-tile"); !ok || pCached.Data.(map[string]any)["stream_event"] != "alert_fired" {
		t.Fatalf("unexpected cached data: %+v", pCached)
	}
	mockP1, mockP2, mockRem := newControllableMockProvider(), newControllableMockProvider(), newControllableMockProvider()
	regDyn := regSeq("dynamic-provider", mockP1, mockP2)
	regDyn.Register("removable-provider", func() provider.Provider { return mockRem })
	bDyn, sDyn := &mockBroadcaster{}, newMockStateSink()
	coordDyn := newCoord(regDyn, bDyn, sDyn, mkSnap("", nil,
		mkWidget("tile-dyn", "dynamic-provider", 0, map[string]any{"city": "Tokyo"}),
		mkWidget("w-remove-me", "removable-provider", 0, nil),
	))
	defer coordDyn.Stop()
	waitForState(t, coordDyn.Cache(), "tile-dyn", provider.StateHealthy)
	waitSignal(t, mockRem.fetchSignal, "fetch")
	waitForState(t, coordDyn.Cache(), "w-remove-me", provider.StateHealthy)
	waitSignal(t, mockRem.subscribeSignal, "subscribe")
	mockP2.setFetch(func(ctx context.Context) (any, error) { return nil, errors.New("upstream connection pending") })
	mockP1.emitPush(provider.WidgetPayload{Data: map[string]any{"city": "Old-Tokyo-Data"}})
	mockRem.emitPush(provider.WidgetPayload{Data: map[string]any{"stale": true}})
	if err := coordDyn.UpdateConfig(mkSnap("", nil, mkWidget("tile-dyn", "dynamic-provider", 0, map[string]any{"city": "Paris"}))); err != nil {
		t.Fatalf("UpdateConfig failed: %v", err)
	}
	waitForPayload(t, bDyn, "tile-dyn", "", provider.StateError)
	pAfter, ok := coordDyn.Cache().Get("tile-dyn")
	if !ok || (pAfter.State == provider.StateHealthy && pAfter.Data.(map[string]any)["city"] == "Old-Tokyo-Data") || coordDyn.Cache().GetStatusMap()["w-remove-me"] != "" {
		t.Fatal("stale push resurrected purged cache entry with old domain data or removal failed")
	}
	waitSignal(t, mockP2.subscribeSignal, "mockP2 subscribe")
	mockP2.emitPush(provider.WidgetPayload{Data: map[string]any{"city": "New-Paris-Data"}})
	pDyn := waitForPayload(t, bDyn, "tile-dyn", "city", "")
	if d, ok := pDyn.Data.(map[string]any); !ok || d["city"] != "New-Paris-Data" {
		t.Fatalf("unexpected broadcast city: %v", pDyn.Data)
	}
	if pFinal, ok := coordDyn.Cache().Get("tile-dyn"); !ok || pFinal.State != provider.StateHealthy || pFinal.Data.(map[string]any)["city"] != "New-Paris-Data" {
		t.Fatalf("expected New-Paris-Data, got %+v", pFinal)
	}
	mockA1, mockA2 := newControllableMockProvider(), newControllableMockProvider()
	hookCalled, releaseHook := make(chan struct{}, 1), make(chan struct{})
	coordAtom := provider.NewCoordinator(provider.CoordinatorConfig{
		Registry: regSeq("interleaved-provider", mockA1, mockA2), Broadcaster: &mockBroadcaster{}, StateSink: newMockStateSink(),
		OnBeforeRecordPush: func(widgetID string) {
			if widgetID == "tile-atomic" {
				select {
				case hookCalled <- struct{}{}:
				default:
				}
				<-releaseHook
			}
		},
	}, mkSnap("", nil, mkWidget("tile-atomic", "interleaved-provider", 0, map[string]any{"city": "Tokyo"})))
	defer coordAtom.Stop()
	waitSignal(t, mockA1.fetchSignal, "initial fetch")
	waitForState(t, coordAtom.Cache(), "tile-atomic", provider.StateHealthy)
	waitSignal(t, mockA1.subscribeSignal, "subscribe")
	mockA1.emitPush(provider.WidgetPayload{Data: map[string]any{"city": "Old-Tokyo-Data"}})
	waitSignal(t, hookCalled, "hook called")
	updateDone := make(chan error, 1)
	go func() {
		updateDone <- coordAtom.UpdateConfig(mkSnap("", nil, mkWidget("tile-atomic", "interleaved-provider", 0, map[string]any{"city": "Paris"})))
	}()
	select {
	case <-updateDone:
		t.Fatal("UpdateConfig completed prematurely while RLock was held!")
	case <-time.After(50 * time.Millisecond):
	}
	close(releaseHook)
	select {
	case err := <-updateDone:
		if err != nil {
			t.Fatalf("UpdateConfig failed: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for UpdateConfig")
	}
	waitSignal(t, mockA2.fetchSignal, "mockA2 fetch")
	pAtom := waitForState(t, coordAtom.Cache(), "tile-atomic", provider.StateHealthy)
	if d, ok := pAtom.Data.(map[string]any); ok && d["city"] == "Old-Tokyo-Data" {
		t.Fatal("UpdateConfig failed to purge stale push data!")
	}
}

func TestProviderCoordinator_LiveConfigReload(t *testing.T) {
	t.Parallel()
	b, s := &mockBroadcaster{}, newMockStateSink()
	coord := newCoord(regTypes("type-one", "type-two"), b, s, mkSnap("", nil, mkWidget("w-retain", "type-one", 0, nil), mkWidget("w-remove", "type-one", 0, nil)))
	defer coord.Stop()
	waitForState(t, coord.Cache(), "w-retain", provider.StateHealthy)
	waitForState(t, coord.Cache(), "w-remove", provider.StateHealthy)
	snap2 := mkSnap("", nil, mkWidget("w-retain", "type-one", 0, map[string]any{"city": "San Francisco"}), mkWidget("w-add", "type-two", 0, nil))
	if err := coord.UpdateConfig(snap2); err != nil {
		t.Fatalf("UpdateConfig failed: %v", err)
	}
	waitForState(t, coord.Cache(), "w-add", provider.StateHealthy)
	waitForState(t, coord.Cache(), "w-retain", provider.StateHealthy)
	if _, ok := coord.Cache().Get("w-remove"); ok || !hasEvent(b, "w-retain", provider.StateDegraded) || coord.UpdateConfig(nil) == nil {
		t.Fatal("expected w-remove purged and degraded event on reload")
	}
	_ = coord.Stop()
	if coord.UpdateConfig(snap2) == nil {
		t.Fatal("expected error updating stopped coordinator")
	}
	var (
		instances []*controllableMockProvider
		mu        sync.Mutex
	)
	regTZ := provider.NewRegistry()
	regTZ.Register("mock-weather", func() provider.Provider {
		p := newControllableMockProvider()
		mu.Lock()
		defer mu.Unlock()
		instances = append(instances, p)
		return p
	})
	coordTZ := newCoord(regTZ, nil, nil, mkSnap("America/Los_Angeles", nil, mkWidget("weather-tile", "mock-weather", 30, map[string]any{"latitude": 37.8, "longitude": -122.2})))
	defer coordTZ.Stop()
	mu.Lock()
	p1 := instances[0]
	mu.Unlock()
	if p1.initOpts.Timezone != "America/Los_Angeles" {
		t.Errorf("expected initial Timezone == 'America/Los_Angeles', got %q", p1.initOpts.Timezone)
	}
	if err := coordTZ.UpdateConfig(mkSnap("America/New_York", nil, mkWidget("weather-tile", "mock-weather", 30, map[string]any{"latitude": 37.8, "longitude": -122.2}))); err != nil {
		t.Fatalf("UpdateConfig failed: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(instances) != 2 || instances[1].initOpts.Timezone != "America/New_York" {
		t.Fatalf("expected 2 instances with updated timezone, got %d", len(instances))
	}
}

func TestProviderCoordinator_ConfigAndOptionsResolution(t *testing.T) {
	t.Parallel()
	mockP := newControllableMockProvider()
	coord := provider.NewCoordinator(provider.CoordinatorConfig{
		Registry: regOne("periodic-type", mockP),
		Backoff:  &provider.BackoffPolicy{InitialInterval: 10 * time.Millisecond, MaxInterval: 20 * time.Millisecond, Multiplier: 1.0},
	}, mkSnap("", map[string]*domain.Package{"periodic-type": mkPkg("periodic-type", "periodic-type", 1, nil)}, config.WidgetConfig{ID: "periodic-tile", Type: "periodic-type", Endpoint: "/api/test", Method: "POST", TokenEnv: "TEST_TOKEN_ENV", Token: "test-secret"}))
	waitSignal(t, mockP.fetchSignal, "initial fetch")
	waitSignal(t, mockP.fetchSignal, "periodic timer tick")
	_ = coord.Stop()
	p1, p2 := newControllableMockProvider(), newControllableMockProvider()
	reg := provider.NewRegistry()
	reg.Register("custom-sensor", func() provider.Provider { return p1 })
	reg.Register("minimal-type", func() provider.Provider { return p2 })
	coord = newCoord(reg, nil, nil, mkSnap("America/Los_Angeles", nil,
		config.WidgetConfig{
			ID: "sensor-tile-1", Type: "custom-sensor", Dimensions: []int{2, 1}, RefreshIntervalSeconds: new(30),
			Endpoint: "https://sensor.local/api/metrics", Method: "POST", TokenEnv: "SENSOR_TOKEN", Token: "resolved-bearer-token",
			Secrets: map[string]string{"token": "resolved-bearer-token", "api_key": "secret-sensor-api-key"},
			Config:  map[string]any{"city": "Oakland", "endpoint": "domain-endpoint-override", "api_key_env": "SENSOR_API_KEY"},
		},
		config.WidgetConfig{ID: "min-tile", Type: "minimal-type", RefreshIntervalSeconds: new(30), Endpoint: "http://bare.local/poll", Token: "standalone-token"},
	))
	defer coord.Stop()
	if p1.initCfg["token"] != nil || p1.initCfg["token_env"] != nil || p1.initCfg["method"] != nil || p1.initCfg["endpoint"] != "domain-endpoint-override" || p1.initCfg["city"] != "Oakland" || p1.initCfg["api_key_env"] != "SENSOR_API_KEY" {
		t.Errorf("unexpected domain config: %v", p1.initCfg)
	}
	if p1.initOpts.ID != "sensor-tile-1" || p1.initOpts.Type != "custom-sensor" || p1.initOpts.Endpoint != "https://sensor.local/api/metrics" || p1.initOpts.Method != http.MethodPost || p1.initOpts.GetSecret("api_key") != "secret-sensor-api-key" || p1.initOpts.Timezone != "America/Los_Angeles" {
		t.Errorf("unexpected initOpts: %+v", p1.initOpts)
	}
	if !p2.initCalled || p2.initOpts.Method != http.MethodPost || p2.initOpts.Token != "standalone-token" || p2.initOpts.GetSecret("token") != "standalone-token" {
		t.Errorf("unexpected fallback options: called=%v, opts=%+v", p2.initCalled, p2.initOpts)
	}
}

func TestProviderCoordinator_ShutdownEdgeCases(t *testing.T) {
	t.Parallel()
	coord := newCoord(regTypes("edge-type"), nil, nil, mkSnap("", nil, mkWidget("edge-tile", "edge-type", 30, nil)))
	canceledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	_ = coord.Shutdown(canceledCtx)
	if err := coord.Stop(); err != nil || coord.Stop() != nil {
		t.Fatal("expected idempotent Stop")
	}
	if err := coord.CompleteListItem(context.Background(), "edge-tile", "item-1"); err == nil {
		t.Fatal("expected error on stopped coordinator")
	}
	tp := provider.NewTasksProviderWithAdapter(&mockCompleterAdapter{})
	coordTasks := newCoord(regOne("tasks", tp), nil, nil, mkSnap("", nil, config.WidgetConfig{ID: "w-tasks", Type: "tasks", Config: map[string]any{"list_id": "list-1"}}))
	defer coordTasks.Stop()
	if err := coordTasks.CompleteListItem(context.Background(), "unknown-list", "item-1"); !errors.Is(err, tasks.ErrListNotFound) || coordTasks.CompleteListItem(context.Background(), "w-tasks", "item-1") != nil {
		t.Fatalf("unexpected CompleteListItem results: %v", err)
	}
}

func TestProviderCoordinator_PushWidgetData(t *testing.T) {
	t.Parallel()
	b, s := &mockBroadcaster{}, newMockStateSink()
	pkgs := map[string]*domain.Package{
		"custom-sensor":     mkPkg("custom-sensor", "http", 0, map[string]any{"type": "object", "required": []any{"temperature", "humidity"}, "properties": map[string]any{"temperature": map[string]any{"type": "number"}, "humidity": map[string]any{"type": "number"}}}),
		"bad-schema-widget": mkPkg("bad-schema-widget", "http", 0, map[string]any{"type": 12345}),
	}
	widgets := make([]config.WidgetConfig, 0, 5)
	widgets = append(widgets,
		config.WidgetConfig{ID: "tile-custom", Type: "custom-sensor", Endpoint: "https://example.com/sensor"},
		config.WidgetConfig{ID: "tile-bad-schema", Type: "bad-schema-widget", Endpoint: "https://example.com/bad"},
	)
	for typ, id := range map[string]string{"tasks": "tile-tasks", "calendar-feed": "tile-calendar", "weather-forecast": "tile-weather"} {
		pkgs[typ] = mkPkg(typ, typ, 0, nil)
		widgets = append(widgets, mkWidget(id, typ, 0, nil))
	}
	coord := newCoord(nil, b, s, mkSnap("", pkgs, widgets...))
	ctx := context.Background()
	validData := map[string]any{"temperature": 23.5, "humidity": 60.0}
	p, err := coord.PushWidgetData(ctx, "tile-custom", validData)
	if err != nil || p.WidgetID != "tile-custom" || p.State != provider.StateHealthy {
		t.Fatalf("unexpected PushWidgetData: %v, %+v", err, p)
	}
	if cached, ok := coord.Cache().Get("tile-custom"); !ok || cached.State != provider.StateHealthy {
		t.Fatalf("cached invalid: %v, %+v", ok, cached)
	}
	if sinkData, sinkState, _, found := s.getState("tile-custom"); !found || sinkState != provider.StateHealthy || sinkData.(map[string]any)["temperature"] != 23.5 {
		t.Fatalf("stateSink invalid: %v, %s, %v", found, sinkState, sinkData)
	}
	if evt, ok := b.waitForPublish(time.Second); !ok || evt.Type != events.EventWidgetUpdate {
		t.Fatalf("broadcaster invalid: ok=%v, evt=%+v", ok, evt)
	}
	if p2, err := coord.PushWidgetData(ctx, "tile-custom", validData); err != nil || p2.WidgetID != "tile-custom" {
		t.Fatalf("second push failed: %v, %+v", err, p2)
	}
	coordUnloaded := newCoord(nil, b, s, mkSnap("", map[string]*domain.Package{"empty-schema": mkPkg("empty-schema", "http", 0, nil)}, mkWidget("tile-unloaded", "http", 0, nil), mkWidget("tile-empty", "empty-schema", 0, nil)))
	defer coordUnloaded.Stop()
	for _, id := range []string{"tile-unloaded", "tile-empty"} {
		if _, err := coordUnloaded.PushWidgetData(ctx, id, validData); err != nil {
			t.Fatalf("%s push failed: %v", id, err)
		}
	}
	coordStopped := provider.NewCoordinator(provider.CoordinatorConfig{}, mkSnap("", pkgs, widgets...))
	_ = coordStopped.Stop()
	coordNilSnap := provider.NewCoordinator(provider.CoordinatorConfig{}, nil)
	defer coordNilSnap.Stop()
	for _, tc := range []struct {
		name, id, sub string
		data          map[string]any
		c             *provider.ProviderCoordinator
		err           error
	}{
		{"not_found", "nonexistent-widget", "", validData, coord, provider.ErrWidgetNotFound},
		{"empty_id", "", "", validData, coord, provider.ErrWidgetNotFound},
		{"tasks_forbidden", "tile-tasks", "", map[string]any{"items": []any{}}, coord, provider.ErrListWidgetPushForbidden},
		{"calendar_forbidden", "tile-calendar", "", validData, coord, provider.ErrNonHTTPWidgetPushForbidden},
		{"weather_forbidden", "tile-weather", "", validData, coord, provider.ErrNonHTTPWidgetPushForbidden},
		{"bad_data_schema", "tile-custom", "", map[string]any{"temperature": "not a number", "humidity": 60.0}, coord, provider.ErrSchemaValidation},
		{"bad_schema_compile", "tile-bad-schema", "", map[string]any{"foo": "bar"}, coord, provider.ErrSchemaValidation},
		{"stopped_coord", "tile-custom", "coordinator is stopped", validData, coordStopped, nil},
		{"nil_snap", "tile-custom", "", validData, coordNilSnap, provider.ErrWidgetNotFound},
	} {
		if _, err := tc.c.PushWidgetData(ctx, tc.id, tc.data); (tc.err != nil && !errors.Is(err, tc.err)) || (tc.sub != "" && (err == nil || !strings.Contains(err.Error(), tc.sub))) {
			t.Fatalf("unexpected error for %s: %v", tc.name, err)
		}
	}
	_ = coord.Stop()
}

func TestProviderCoordinator_PushWidgetPhotos(t *testing.T) {
	t.Parallel()
	b, s := &mockBroadcaster{}, newMockStateSink()
	pkgs := map[string]*domain.Package{
		"photo-carousel": mkPkg("photo-carousel", "photos", 0, nil),
		"custom-sensor":  mkPkg("custom-sensor", "http", 0, nil),
	}
	widgets := []config.WidgetConfig{
		{ID: "tile-ember", Type: "photo-carousel", Config: map[string]any{"source": "ember_bridge", "album_url": "https://photos.app.goo.gl/ember"}},
		{ID: "tile-share", Type: "photo-carousel", Config: map[string]any{"source": "share_url", "album_url": "https://photos.app.goo.gl/share"}},
		{ID: "tile-sensor", Type: "custom-sensor", Endpoint: "https://example.com/sensor"},
	}
	reg := provider.NewRegistry()
	reg.Register("photos", func() provider.Provider { return provider.NewPhotosProvider() })
	reg.Register("http", func() provider.Provider { return newControllableMockProvider() })
	coord := newCoord(reg, b, s, mkSnap("", pkgs, widgets...))
	defer coord.Stop()

	waitForState(t, coord.Cache(), "tile-ember", provider.StateHealthy)
	_, _ = b.waitForPublish(time.Second)

	ctx := context.Background()
	validPhotos := []string{"https://lh3.googleusercontent.com/pw/AL9nZEW1", "https://lh3.googleusercontent.com/pw/AL9nZEW2"}

	payload, err := coord.PushWidgetPhotos(ctx, "tile-ember", validPhotos, "Ember Vacation")
	if err != nil || payload.WidgetID != "tile-ember" || payload.State != provider.StateHealthy {
		t.Fatalf("unexpected PushWidgetPhotos: %v, %+v", err, payload)
	}
	snap, ok := payload.Data.(*provider.PhotoSnapshot)
	if !ok || snap.AlbumName != "Ember Vacation" || snap.TotalPhotos != 2 || len(snap.Photos) != 2 {
		t.Fatalf("unexpected payload data: %+v", payload.Data)
	}
	if cached, found := coord.Cache().Get("tile-ember"); !found || cached.State != provider.StateHealthy {
		t.Fatalf("cached invalid: %v, %+v", found, cached)
	}
	if sinkData, sinkState, _, found := s.getState("tile-ember"); !found || sinkState != provider.StateHealthy || sinkData.(*provider.PhotoSnapshot).AlbumName != "Ember Vacation" {
		t.Fatalf("stateSink invalid: %v, %s, %v", found, sinkState, sinkData)
	}
	if evt, found := b.waitForPublish(time.Second); !found || evt.Type != events.EventWidgetUpdate {
		t.Fatalf("broadcaster invalid: found=%v, evt=%+v", found, evt)
	}

	coordStopped := provider.NewCoordinator(provider.CoordinatorConfig{}, mkSnap("", pkgs, widgets...))
	_ = coordStopped.Stop()
	coordNilSnap := provider.NewCoordinator(provider.CoordinatorConfig{}, nil)
	defer coordNilSnap.Stop()

	for _, tc := range []struct {
		name, id, sub string
		photos        []string
		album         string
		c             *provider.ProviderCoordinator
		err           error
	}{
		{"not_found", "nonexistent-widget", "", validPhotos, "Album", coord, provider.ErrWidgetNotFound},
		{"empty_id", "", "", validPhotos, "Album", coord, provider.ErrWidgetNotFound},
		{"non_photo_widget", "tile-sensor", "", validPhotos, "Album", coord, provider.ErrPhotoPushNotSupported},
		{"share_url_widget", "tile-share", "", validPhotos, "Album", coord, provider.ErrPhotoPushNotSupported},
		{"invalid_photos", "tile-ember", "", []string{"https://notgoogle.com/pic.jpg"}, "Album", coord, provider.ErrInvalidPhotoPushPayload},
		{"stopped_coord", "tile-ember", "coordinator is stopped", validPhotos, "Album", coordStopped, nil},
		{"nil_snap", "tile-ember", "", validPhotos, "Album", coordNilSnap, provider.ErrWidgetNotFound},
	} {
		if _, err := tc.c.PushWidgetPhotos(ctx, tc.id, tc.photos, tc.album); (tc.err != nil && !errors.Is(err, tc.err)) || (tc.sub != "" && (err == nil || !strings.Contains(err.Error(), tc.sub))) {
			t.Fatalf("unexpected error for %s: %v", tc.name, err)
		}
	}
}
