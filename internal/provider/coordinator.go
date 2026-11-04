package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/azylman/mirrormere/internal/chatlog"
	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/tasks"
	"github.com/santhosh-tekuri/jsonschema/v6"
)

// Broadcaster defines the interface for emitting Server-Sent Events over the event bus.
type Broadcaster interface {
	Publish(eventType string, data []byte) *events.Event
}

// StateSink defines the interface for updating shared display state snapshots.
type StateSink interface {
	SetWidgetState(widgetID string, data any, state string, timestamp ...string)
	SetProvidersStatus(providers map[string]string)
}

// CoordinatorConfig specifies dependencies and options for ProviderCoordinator.
type CoordinatorConfig struct {
	Registry           Registry
	Broadcaster        Broadcaster
	StateSink          StateSink
	HeaderWeatherSink  HeaderWeatherSink
	Cache              *SWRCache
	Logger             *slog.Logger
	NowFunc            func() time.Time
	Backoff            *BackoffPolicy
	OnBeforeRecordPush func(widgetID string)
	// ChatLog, when set, receives chat-log widget pushes.
	ChatLog             *chatlog.Store
	HeaderPollerClient  *http.Client
	HeaderPollerBaseURL string
}

type worker struct {
	widgetID   string
	widgetType string
	provider   Provider
	interval   time.Duration
	refreshCh  chan struct{}
	cancel     context.CancelFunc
	done       chan struct{}
	subWg      sync.WaitGroup
	stopped    atomic.Bool
}

type workerEvent struct {
	worker  *worker
	payload WidgetPayload
}

// ProviderCoordinator orchestrates lifecycle and sync loops for all active widget providers.
type ProviderCoordinator struct {
	mu                 sync.RWMutex
	registry           Registry
	broadcaster        Broadcaster
	stateSink          StateSink
	cache              *SWRCache
	logger             *slog.Logger
	nowFunc            func() time.Time
	backoff            *BackoffPolicy
	workers            map[string]*worker
	snapshot           *config.Snapshot
	eventSink          chan workerEvent
	sinkCancel         context.CancelFunc
	sinkWg             sync.WaitGroup
	onBeforeRecordPush func(widgetID string)
	chatLog            *chatlog.Store
	schemaMu           sync.RWMutex
	schemas            map[string]*jsonschema.Schema
	stopped            bool
	headerPoller       *HeaderWeatherPoller
}

// NewCoordinator constructs an initialized ProviderCoordinator.
func NewCoordinator(cfg CoordinatorConfig, initialSnapshot *config.Snapshot) *ProviderCoordinator {
	reg := cfg.Registry
	if reg == nil {
		reg = NewRegistry()
	}
	cache := cfg.Cache
	if cache == nil {
		cache = NewSWRCache()
	}
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	nowFn := cfg.NowFunc
	if nowFn == nil {
		nowFn = time.Now
	}
	bo := cfg.Backoff
	if bo == nil {
		bo = DefaultBackoffPolicy()
	}

	var headerSink HeaderWeatherSink
	if cfg.HeaderWeatherSink != nil {
		headerSink = cfg.HeaderWeatherSink
	} else if hs, ok := cfg.StateSink.(HeaderWeatherSink); ok {
		headerSink = hs
	}

	headerPoller := NewHeaderWeatherPoller(HeaderWeatherPollerConfig{
		Client:      cfg.HeaderPollerClient,
		BaseURL:     cfg.HeaderPollerBaseURL,
		Broadcaster: cfg.Broadcaster,
		StateSink:   headerSink,
		Logger:      logger,
		NowFunc:     nowFn,
	})

	sinkCtx, sinkCancel := context.WithCancel(context.Background())

	c := &ProviderCoordinator{
		registry:           reg,
		broadcaster:        cfg.Broadcaster,
		stateSink:          cfg.StateSink,
		cache:              cache,
		logger:             logger,
		nowFunc:            nowFn,
		backoff:            bo,
		workers:            make(map[string]*worker),
		snapshot:           initialSnapshot,
		eventSink:          make(chan workerEvent, 64),
		sinkCancel:         sinkCancel,
		onBeforeRecordPush: cfg.OnBeforeRecordPush,
		chatLog:            cfg.ChatLog,
		schemas:            make(map[string]*jsonschema.Schema),
		headerPoller:       headerPoller,
	}

	c.sinkWg.Add(1)
	go c.listenEventSink(sinkCtx)

	if initialSnapshot != nil && initialSnapshot.Config != nil {
		if initialSnapshot.Config.Display.Header.Weather != nil {
			c.headerPoller.Start(context.Background(), initialSnapshot.Config.Display.Header.Weather, initialSnapshot.Config.Timezone)
		}
		c.startWorkersLocked(initialSnapshot)
	}

	return c
}

// Cache returns the coordinator's in-memory SWR cache.
func (c *ProviderCoordinator) Cache() *SWRCache {
	return c.cache
}

// GetWidgetState retrieves current state, domain data, and timestamp for a widget instance,
// directly satisfying the render.StateSnapshotProvider contract.
func (c *ProviderCoordinator) GetWidgetState(widgetID string) (any, string, string, bool) {
	if p, ok := c.cache.Get(widgetID); ok {
		return p.Data, p.State, p.Timestamp, true
	}
	return nil, "", "", false
}

// GetWidgetTimestamp retrieves the cached timestamp for a widget instance,
// directly satisfying the render.Engine timestamp provider contract.
func (c *ProviderCoordinator) GetWidgetTimestamp(widgetID string) (string, bool) {
	if p, ok := c.cache.Get(widgetID); ok && p.Timestamp != "" {
		return p.Timestamp, true
	}
	return "", false
}

// RefreshWidget triggers an immediate, out-of-band poll for the given widgetID.
func (c *ProviderCoordinator) RefreshWidget(widgetID string) error {
	c.mu.RLock()
	w, ok := c.workers[widgetID]
	c.mu.RUnlock()

	if !ok {
		return fmt.Errorf("no active provider worker for widget %q", widgetID)
	}

	select {
	case w.refreshCh <- struct{}{}:
	default:
		// Refresh already queued
	}
	return nil
}

// CompleteListItem marks a task item complete on the matching tasks provider worker, or returns ErrListNotFound.
func (c *ProviderCoordinator) CompleteListItem(ctx context.Context, listID, itemID string) error {
	c.mu.RLock()
	var targetProvider *TasksProvider
	var targetWidgetID string
	for _, w := range c.workers {
		if tp, ok := w.provider.(*TasksProvider); ok {
			if w.widgetID == listID || tp.ListID() == listID {
				targetProvider = tp
				targetWidgetID = w.widgetID
				break
			}
		}
	}
	c.mu.RUnlock()

	if targetProvider == nil {
		return tasks.ErrListNotFound
	}

	if err := targetProvider.CompleteItem(ctx, itemID); err != nil {
		return err
	}

	return c.RefreshWidget(targetWidgetID)
}

// CallWidgetAction executes a service call on the matching widget provider worker.
func (c *ProviderCoordinator) CallWidgetAction(ctx context.Context, widgetID, domain, service string, serviceData map[string]any) error {
	_, err := c.CallWidgetActionWithResponse(ctx, widgetID, domain, service, serviceData)
	return err
}

// CallWidgetActionWithResponse executes a service call and returns the response from the provider worker.
func (c *ProviderCoordinator) CallWidgetActionWithResponse(ctx context.Context, widgetID, domain, service string, serviceData map[string]any) (any, error) {
	c.mu.RLock()
	var targetProvider *HomeAssistantProvider
	var targetWidgetID string
	if w, ok := c.workers[widgetID]; ok {
		if hap, ok := w.provider.(*HomeAssistantProvider); ok {
			targetProvider = hap
			targetWidgetID = w.widgetID
		}
	}
	if targetProvider == nil {
		for _, w := range c.workers {
			if hap, ok := w.provider.(*HomeAssistantProvider); ok && w.widgetID == widgetID {
				targetProvider = hap
				targetWidgetID = w.widgetID
				break
			}
		}
	}
	c.mu.RUnlock()

	if targetProvider == nil {
		return nil, fmt.Errorf("%w: widget provider not found: %s", ErrWidgetNotFound, widgetID)
	}

	res, err := targetProvider.CallServiceWithResponse(ctx, domain, service, serviceData)
	if err != nil {
		return nil, err
	}
	if refreshErr := c.RefreshWidget(targetWidgetID); refreshErr != nil {
		c.logger.Warn("failed to refresh widget after action", "widget_id", targetWidgetID, "error", refreshErr)
	}
	return res, nil
}

// PushUpdate records an unsolicited domain data update (e.g. from inbound webhook)
// into the SWR cache and broadcasts widget.update immediately.
func (c *ProviderCoordinator) PushUpdate(widgetID string, data any) (WidgetPayload, error) {
	now := c.nowFunc()
	payload, _ := c.cache.RecordPush(widgetID, data, now)
	c.syncAndBroadcast(payload)
	return payload, nil
}

// UpdateConfig reconciles active providers and workers against a new configuration snapshot.
func (c *ProviderCoordinator) UpdateConfig(snap *config.Snapshot) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.stopped {
		return errors.New("coordinator is stopped")
	}
	if snap == nil || snap.Config == nil {
		return errors.New("snapshot or config is nil")
	}

	oldSnap := c.snapshot
	c.snapshot = snap

	c.schemaMu.Lock()
	c.schemas = make(map[string]*jsonschema.Schema)
	c.schemaMu.Unlock()

	// Diff widgets to determine added, removed, and modified
	var diff *config.ConfigDiff
	if oldSnap != nil && oldSnap.Config != nil {
		diff = config.DiffConfigs(oldSnap.Config, snap.Config)
	} else {
		diff = &config.ConfigDiff{
			Added: snap.Config.Display.Widgets,
		}
	}

	// 1. Terminate removed widgets
	for _, w := range diff.Removed {
		c.stopWorkerLocked(w.ID)
		c.cache.Purge(w.ID)
	}
	if len(diff.Removed) > 0 && c.stateSink != nil {
		c.stateSink.SetProvidersStatus(c.cache.GetStatusMap())
	}

	// 2. Restart or reconfigure modified widgets
	for _, m := range diff.Modified {
		c.stopWorkerLocked(m.ID)
		if m.DomainChange {
			c.cache.Purge(m.ID)
			nowStr := c.nowFunc().UTC().Format(time.RFC3339)
			c.syncAndBroadcast(WidgetPayload{
				WidgetID:  m.ID,
				Timestamp: nowStr,
				State:     StateDegraded,
				Data:      map[string]any{},
			})
		}
		if m.NewInstance != nil {
			c.startSingleWorkerLocked(m.NewInstance, snap)
		}
	}

	// If household timezone changed, restart unchanged widgets with updated timezone
	if diff.TimezoneChanged {
		for _, u := range diff.Unchanged {
			c.stopWorkerLocked(u.ID)
			c.cache.Purge(u.ID)
			c.startSingleWorkerLocked(&u, snap)
		}
	}

	// 3. Start newly added widgets
	for i := range diff.Added {
		c.startSingleWorkerLocked(&diff.Added[i], snap)
	}

	// 4. Update autonomous header weather poller
	if snap.Config.Display.Header.Weather != nil {
		c.headerPoller.UpdateConfig(context.Background(), snap.Config.Display.Header.Weather, snap.Config.Timezone)
	} else {
		c.headerPoller.Stop()
	}

	return nil
}

// Stop gracefully shuts down all worker goroutines, subscriptions, and event listeners.
func (c *ProviderCoordinator) Stop() error {
	return c.Shutdown(context.Background())
}

// Shutdown gracefully shuts down with context timeout/cancellation support.
func (c *ProviderCoordinator) Shutdown(ctx context.Context) error {
	c.mu.Lock()
	if c.stopped {
		c.mu.Unlock()
		return nil
	}
	c.stopped = true

	if c.headerPoller != nil {
		c.headerPoller.Stop()
	}

	workersToStop := make([]*worker, 0, len(c.workers))
	for _, w := range c.workers {
		w.stopped.Store(true)
		workersToStop = append(workersToStop, w)
	}
	c.workers = make(map[string]*worker)
	c.mu.Unlock()

	defer func() {
		c.sinkCancel()
		c.sinkWg.Wait()
	}()

	// 1. Signal all workers to stop
	for _, w := range workersToStop {
		w.cancel()
	}

	// 2. Wait for all workers to terminate
	for _, w := range workersToStop {
		select {
		case <-w.done:
		case <-ctx.Done():
			return ctx.Err()
		}
		w.subWg.Wait()
		if err := w.provider.Shutdown(ctx); err != nil {
			c.logger.Warn("provider shutdown error", "widget_id", w.widgetID, "error", err)
		}
	}

	return nil
}

func (c *ProviderCoordinator) startWorkersLocked(snap *config.Snapshot) {
	for i := range snap.Config.Display.Widgets {
		w := &snap.Config.Display.Widgets[i]
		c.startSingleWorkerLocked(w, snap)
	}
}

func (c *ProviderCoordinator) startSingleWorkerLocked(w *config.WidgetConfig, snap *config.Snapshot) {
	providerName := resolveProviderName(w, snap)
	var pkg *domain.Package
	if snap.Packages != nil {
		pkg = snap.Packages[w.Type]
	}

	// Native Spacer and Live View Optimization: Zero-overhead padding/streaming, no polling ticker needed
	// The chat-log and live-view widgets take the same path: live-view streams directly via WebRTC/MJPEG/HLS
	// on the client, and chat-log lives in the in-memory chatlog store, so nothing polls on the backend.
	if w.Type == "spacer" || providerName == "spacer" || providerName == "chat-log" || providerName == "live-view" || w.Type == "live-view" {
		now := c.nowFunc()
		payload, _ := c.cache.RecordSuccess(w.ID, map[string]any{}, now)
		c.syncAndBroadcast(payload)
		return
	}

	p, err := c.registry.Create(providerName)
	if err != nil {
		c.logger.Error("failed to create provider for widget", "widget_id", w.ID, "provider", providerName, "error", err)
		c.recordColdBootError(w.ID, err)
		return
	}

	// Prepare configuration map and instance options
	cfgMap := c.buildProviderConfig(w)
	opts := c.buildInitOptions(w, pkg, snap)
	initCtx, cancelInit := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelInit()

	if err := p.Init(initCtx, cfgMap, opts); err != nil {
		c.logger.Error("failed to initialize provider for widget", "widget_id", w.ID, "provider", providerName, "error", err)
		c.recordColdBootError(w.ID, err)
		return
	}

	interval := resolveInterval(w, pkg)
	workerCtx, workerCancel := context.WithCancel(context.Background())
	wrk := &worker{
		widgetID:   w.ID,
		widgetType: w.Type,
		provider:   p,
		interval:   interval,
		refreshCh:  make(chan struct{}, 1),
		cancel:     workerCancel,
		done:       make(chan struct{}),
	}
	c.workers[w.ID] = wrk

	workerSink := make(chan WidgetPayload, 64)

	// Forward pushes from workerSink to coordinator eventSink, stamping the worker's widget ID
	// and dropping payloads once the worker context is cancelled.
	wrk.subWg.Go(func() {
		for {
			select {
			case <-workerCtx.Done():
				return
			case payload, ok := <-workerSink:
				if !ok {
					return
				}
				if workerCtx.Err() != nil {
					return
				}
				payload.WidgetID = wrk.widgetID
				select {
				case <-workerCtx.Done():
					return
				case c.eventSink <- workerEvent{worker: wrk, payload: payload}:
				}
			}
		}
	})

	// Start subscription if provider supports push events
	wrk.subWg.Go(func() {
		if err := p.Subscribe(workerCtx, workerSink); err != nil && !errors.Is(err, context.Canceled) {
			c.logger.Warn("provider subscribe error", "widget_id", wrk.widgetID, "error", err)
		}
	})

	// Start worker loop
	go c.runWorker(workerCtx, wrk)
}

func (c *ProviderCoordinator) stopWorkerLocked(widgetID string) {
	w, ok := c.workers[widgetID]
	if !ok {
		return
	}
	delete(c.workers, widgetID)
	w.stopped.Store(true)
	w.cancel()

	stopTimer := time.NewTimer(3 * time.Second)
	defer stopTimer.Stop()

	select {
	case <-w.done:
	case <-stopTimer.C:
		c.logger.Warn("worker termination timed out", "widget_id", widgetID)
	}

	w.subWg.Wait()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := w.provider.Shutdown(ctx); err != nil {
		c.logger.Warn("provider shutdown error", "widget_id", w.widgetID, "error", err)
	}
}

func (c *ProviderCoordinator) runWorker(ctx context.Context, w *worker) {
	defer close(w.done)

	// 1. Initial immediate fetch
	err := c.fetchAndRecord(ctx, w)

	if pp, ok := w.provider.(*PhotosProvider); ok && pp.Source() == PhotoSourceEmberBridge {
		for {
			select {
			case <-ctx.Done():
				return
			case <-w.refreshCh:
				if fetchErr := c.fetchAndRecord(ctx, w); fetchErr != nil {
					c.logger.Debug("ember_bridge manual refresh fetch failed", "widget_id", w.widgetID, "error", fetchErr)
				}
			}
		}
	}

	delay := c.backoff.ComputeRetryDelay(err, c.cache.GetConsecutiveFailures(w.widgetID), w.interval)

	timer := time.NewTimer(delay)
	defer func() {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
	}()

	for {
		select {
		case <-ctx.Done():
			return
		case <-w.refreshCh:
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			fetchErr := c.fetchAndRecord(ctx, w)
			nextDelay := c.backoff.ComputeRetryDelay(fetchErr, c.cache.GetConsecutiveFailures(w.widgetID), w.interval)
			timer.Reset(nextDelay)
		case <-timer.C:
			fetchErr := c.fetchAndRecord(ctx, w)
			nextDelay := c.backoff.ComputeRetryDelay(fetchErr, c.cache.GetConsecutiveFailures(w.widgetID), w.interval)
			timer.Reset(nextDelay)
		}
	}
}

func (c *ProviderCoordinator) fetchAndRecord(ctx context.Context, w *worker) error {
	fetchCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	data, err := w.provider.Fetch(fetchCtx)
	if ctx.Err() != nil || errors.Is(err, context.Canceled) || w.stopped.Load() {
		return err
	}
	now := c.nowFunc()

	if err == nil {
		payload, _ := c.cache.RecordSuccess(w.widgetID, data, now)
		c.syncAndBroadcast(payload)
		return nil
	}

	c.logger.Warn("provider fetch failed", "widget_id", w.widgetID, "error", err)
	payload, transitioned := c.cache.RecordFailure(w.widgetID, err, now)
	c.syncStateSink(payload)

	// Only broadcast over SSE when operational state actually transitions (suppress degraded retry spam)
	if transitioned {
		c.broadcastUpdate(payload)
	}

	return err
}

func (c *ProviderCoordinator) recordColdBootError(widgetID string, err error) {
	now := c.nowFunc()
	payload, _ := c.cache.RecordFailure(widgetID, err, now)
	c.syncAndBroadcast(payload)
}

func (c *ProviderCoordinator) listenEventSink(sinkCtx context.Context) {
	defer c.sinkWg.Done()

	for {
		select {
		case <-sinkCtx.Done():
			return
		case ev, ok := <-c.eventSink:
			if !ok {
				return
			}
			p, recorded := c.recordWorkerPush(ev.worker, ev.payload)
			if recorded {
				c.broadcastUpdate(p)
			}
		}
	}
}

func (c *ProviderCoordinator) recordWorkerPush(w *worker, payload WidgetPayload) (WidgetPayload, bool) {
	if w == nil || w.stopped.Load() {
		return WidgetPayload{}, false
	}

	c.mu.RLock()
	defer c.mu.RUnlock()

	if !c.isWorkerActiveLocked(w, payload.WidgetID) {
		return WidgetPayload{}, false
	}

	if c.onBeforeRecordPush != nil {
		c.onBeforeRecordPush(payload.WidgetID)
	}

	now := c.nowFunc()
	p, _ := c.cache.RecordPush(payload.WidgetID, payload.Data, now)
	c.syncStateSink(p)
	return p, true
}

func (c *ProviderCoordinator) isWorkerActiveLocked(w *worker, widgetID string) bool {
	if w == nil || w.stopped.Load() {
		return false
	}
	current, ok := c.workers[widgetID]
	return ok && current == w && !w.stopped.Load()
}

func (c *ProviderCoordinator) syncStateSink(p WidgetPayload) {
	if c.stateSink != nil {
		c.stateSink.SetWidgetState(p.WidgetID, p.Data, p.State, p.Timestamp)
		c.stateSink.SetProvidersStatus(c.cache.GetStatusMap())
	}
}

func (c *ProviderCoordinator) syncAndBroadcast(p WidgetPayload) {
	c.syncStateSink(p)
	c.broadcastUpdate(p)
}

func (c *ProviderCoordinator) broadcastUpdate(payload WidgetPayload) {
	if c.broadcaster == nil {
		return
	}
	data, err := json.Marshal(payload)
	if err != nil {
		c.logger.Error("failed to marshal widget.update payload", "widget_id", payload.WidgetID, "error", err)
		return
	}
	c.broadcaster.Publish(events.EventWidgetUpdate, data)
}

func (c *ProviderCoordinator) buildProviderConfig(w *config.WidgetConfig) map[string]any {
	cfgMap := make(map[string]any)
	if w.Config != nil {
		maps.Copy(cfgMap, w.Config)
	}
	return cfgMap
}

func (c *ProviderCoordinator) buildInitOptions(w *config.WidgetConfig, pkg *domain.Package, snap *config.Snapshot) InitOptions {
	method := w.Method
	if method == "" && w.Endpoint != "" {
		method = "POST"
	}

	secrets := make(map[string]string)
	if w.Secrets != nil {
		maps.Copy(secrets, w.Secrets)
	}
	token := w.Token
	if token == "" && secrets["token"] != "" {
		token = secrets["token"]
	} else if token != "" && secrets["token"] == "" {
		secrets["token"] = token
	}

	var dims []int
	if len(w.Dimensions) > 0 {
		dims = make([]int, len(w.Dimensions))
		copy(dims, w.Dimensions)
	}

	var respSchema map[string]any
	if pkg != nil && pkg.Manifest.ResponseSchema != nil {
		respSchema = pkg.Manifest.ResponseSchema
	}

	tz := ""
	if snap != nil && snap.Config != nil {
		tz = snap.Config.Timezone
	} else if c.snapshot != nil && c.snapshot.Config != nil {
		tz = c.snapshot.Config.Timezone
	}

	return InitOptions{
		ID:             w.ID,
		Type:           w.Type,
		Dimensions:     dims,
		Endpoint:       w.Endpoint,
		Method:         method,
		Token:          token,
		Timezone:       tz,
		Secrets:        secrets,
		ResponseSchema: respSchema,
	}
}

// PushWidgetData ingests an immediate domain data payload for a configured widget instance,
// validates against response_schema, records into SWR cache and state sink, and broadcasts widget.update.
func (c *ProviderCoordinator) PushWidgetData(ctx context.Context, widgetID string, data map[string]any) (WidgetPayload, error) {
	if strings.TrimSpace(widgetID) == "" {
		return WidgetPayload{}, fmt.Errorf("%w: empty widget ID", ErrWidgetNotFound)
	}

	p, err := c.recordPushLocked(widgetID, data)
	if err != nil {
		return WidgetPayload{}, err
	}

	c.broadcastUpdate(p)
	return p, nil
}

func (c *ProviderCoordinator) recordPushLocked(widgetID string, data map[string]any) (WidgetPayload, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if c.stopped {
		return WidgetPayload{}, errors.New("coordinator is stopped")
	}

	if c.snapshot == nil || c.snapshot.Config == nil {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrWidgetNotFound, widgetID)
	}

	var targetWidget *config.WidgetConfig
	for i := range c.snapshot.Config.Display.Widgets {
		if c.snapshot.Config.Display.Widgets[i].ID == widgetID {
			targetWidget = &c.snapshot.Config.Display.Widgets[i]
			break
		}
	}
	if targetWidget == nil {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrWidgetNotFound, widgetID)
	}

	providerName := resolveProviderName(targetWidget, c.snapshot)
	if targetWidget.Type == "tasks" || providerName == "tasks" {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrListWidgetPushForbidden, widgetID)
	}
	if providerName != "http" && providerName != "chat-log" {
		return WidgetPayload{}, fmt.Errorf("%w: widget %q uses provider %q", ErrNonHTTPWidgetPushForbidden, widgetID, providerName)
	}

	var pkg *domain.Package
	if c.snapshot.Packages != nil {
		pkg = c.snapshot.Packages[targetWidget.Type]
	}

	if pkg != nil {
		if err := c.validateResponseSchema(targetWidget.Type, pkg.Manifest.ResponseSchema, data); err != nil {
			return WidgetPayload{}, err
		}
	}

	if providerName == "chat-log" && c.chatLog != nil {
		// The pushed transcript becomes this widget's own conversation in the
		// shared in-memory store (same TTL and cap as voice turns).
		msgs, err := chatlog.ParsePush(data)
		if err != nil {
			return WidgetPayload{}, fmt.Errorf("%w: %w", ErrSchemaValidation, err)
		}
		c.chatLog.Replace(chatlog.WidgetKey(widgetID), msgs)
	}

	now := c.nowFunc()
	p, _ := c.cache.RecordPush(widgetID, data, now)
	c.syncStateSink(p)
	return p, nil
}

// PushWidgetPhotos ingests a batch of photo URLs for a photo-carousel widget instance,
// invokes the PhotosProvider IngestPushedPhotos lifecycle, updates SWR cache, and broadcasts widget.update.
func (c *ProviderCoordinator) PushWidgetPhotos(ctx context.Context, widgetID string, photos []string, albumName string) (WidgetPayload, error) {
	if strings.TrimSpace(widgetID) == "" {
		return WidgetPayload{}, fmt.Errorf("%w: empty widget ID", ErrWidgetNotFound)
	}

	p, err := c.recordPhotoPushLocked(widgetID, photos, albumName)
	if err != nil {
		return WidgetPayload{}, err
	}

	c.broadcastUpdate(p)
	return p, nil
}

func (c *ProviderCoordinator) recordPhotoPushLocked(widgetID string, photos []string, albumName string) (WidgetPayload, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	if c.stopped {
		return WidgetPayload{}, errors.New("coordinator is stopped")
	}

	if c.snapshot == nil || c.snapshot.Config == nil {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrWidgetNotFound, widgetID)
	}

	var targetWidget *config.WidgetConfig
	for i := range c.snapshot.Config.Display.Widgets {
		if c.snapshot.Config.Display.Widgets[i].ID == widgetID {
			targetWidget = &c.snapshot.Config.Display.Widgets[i]
			break
		}
	}
	if targetWidget == nil {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrWidgetNotFound, widgetID)
	}

	w, ok := c.workers[widgetID]
	if !ok {
		return WidgetPayload{}, fmt.Errorf("%w: %q", ErrWidgetNotFound, widgetID)
	}

	pp, ok := w.provider.(*PhotosProvider)
	if !ok {
		return WidgetPayload{}, fmt.Errorf("%w: widget %q does not use photo provider", ErrPhotoPushNotSupported, widgetID)
	}

	snap, err := pp.IngestPushedPhotos(photos, albumName)
	if err != nil {
		return WidgetPayload{}, err
	}

	now := c.nowFunc()
	p, _ := c.cache.RecordPush(widgetID, snap, now)
	c.syncStateSink(p)
	return p, nil
}

func (c *ProviderCoordinator) validateResponseSchema(widgetType string, schemaMap map[string]any, data map[string]any) error {
	if len(schemaMap) == 0 {
		return nil
	}
	sch, err := c.getCompiledResponseSchema(widgetType, schemaMap)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrSchemaValidation, err)
	}
	valJSON, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("%w: %w", ErrSchemaValidation, err)
	}
	valDoc, err := jsonschema.UnmarshalJSON(bytes.NewReader(valJSON))
	if err != nil {
		return fmt.Errorf("%w: %w", ErrSchemaValidation, err)
	}
	if err := sch.Validate(valDoc); err != nil {
		return fmt.Errorf("%w: %w", ErrSchemaValidation, err)
	}
	return nil
}

func (c *ProviderCoordinator) getCompiledResponseSchema(widgetType string, schemaMap map[string]any) (*jsonschema.Schema, error) {
	c.schemaMu.RLock()
	sch, ok := c.schemas[widgetType]
	c.schemaMu.RUnlock()
	if ok {
		return sch, nil
	}

	c.schemaMu.Lock()
	defer c.schemaMu.Unlock()
	if sch, ok := c.schemas[widgetType]; ok {
		return sch, nil
	}

	sch, err := compileResponseSchema(widgetType, schemaMap)
	if err != nil {
		return nil, err
	}
	c.schemas[widgetType] = sch
	return sch, nil
}

func resolveProviderName(w *config.WidgetConfig, snap *config.Snapshot) string {
	if snap != nil && snap.Packages != nil {
		if pkg := snap.Packages[w.Type]; pkg != nil && pkg.Manifest.Provider != "" {
			return pkg.Manifest.Provider
		}
	}
	return w.Type
}

func resolveInterval(w *config.WidgetConfig, pkg *domain.Package) time.Duration {
	if w.RefreshIntervalSeconds != nil && *w.RefreshIntervalSeconds > 0 {
		return time.Duration(*w.RefreshIntervalSeconds) * time.Second
	}
	if pkg != nil && pkg.Manifest.Refresh.IntervalSeconds > 0 {
		return time.Duration(pkg.Manifest.Refresh.IntervalSeconds) * time.Second
	}
	return 60 * time.Second
}
