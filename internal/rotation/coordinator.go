package rotation

import (
	"encoding/json"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/events"
	"github.com/azylman/mirrormere/internal/layout"
)

var (
	ErrInvalidScreenIndex = errors.New("invalid screen_index: must be between 0 and total_screens - 1")
	ErrInvalidDirection   = errors.New("invalid direction: must be 'next' or 'prev'")
	ErrNoScreens          = errors.New("no screens available in active layout")
)

// Broadcaster decouples the rotation coordinator from the concrete events.Hub.
type Broadcaster interface {
	Publish(eventType string, data []byte) *events.Event
}

// Config specifies dependencies for Coordinator initialization.
type Config struct {
	Broadcaster Broadcaster
	NowFunc     func() time.Time
	Logger      *slog.Logger
}

// Coordinator manages screen layout indexing, automated periodic rotation, and pause/resume lifecycle.
type Coordinator struct {
	mu            sync.RWMutex
	broadcaster   Broadcaster
	nowFunc       func() time.Time
	logger        *slog.Logger
	snapshot      *config.Snapshot
	currentScreen int
	paused        bool
	rotateTimer   *time.Timer
	pauseTimer    *time.Timer
	timerGen      uint64
	pauseGen      uint64
	stopped       bool
}

// NewCoordinator constructs and activates a Coordinator instance.
func NewCoordinator(cfg Config, initialSnapshot *config.Snapshot) *Coordinator {
	now, logger := cfg.NowFunc, cfg.Logger
	if now == nil {
		now = time.Now
	}
	if logger == nil {
		logger = slog.Default()
	}
	c := &Coordinator{broadcaster: cfg.Broadcaster, nowFunc: now, logger: logger, snapshot: initialSnapshot}
	c.mu.Lock()
	if initialSnapshot != nil && initialSnapshot.Layout != nil {
		c.armRotationTimerLocked()
	}
	c.mu.Unlock()
	return c
}

// ScreenLayoutSnapshot is the solved rotation layout as currently held by the coordinator.
type ScreenLayoutSnapshot struct {
	CurrentScreen   int
	IntervalSeconds int
	Screens         []layout.Screen
}

// ScreenLayout returns the current screen index, the rotation interval and every solved screen of the active layout.
func (c *Coordinator) ScreenLayout() ScreenLayoutSnapshot {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := ScreenLayoutSnapshot{CurrentScreen: c.currentScreen, IntervalSeconds: 30}
	if c.snapshot != nil {
		if c.snapshot.Config != nil {
			out.IntervalSeconds = c.snapshot.Config.Display.Rotation.GetIntervalSeconds()
		}
		if c.snapshot.Layout != nil {
			out.Screens = c.snapshot.Layout.Screens
		}
	}
	return out
}

func (c *Coordinator) CurrentScreen() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.currentScreen
}

func (c *Coordinator) ScreenRotateData() events.ScreenRotateData {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.buildRotateDataLocked()
}

func (c *Coordinator) validateActiveScreensLocked() (int, error) {
	if c.stopped {
		return 0, errors.New("coordinator stopped")
	}
	if c.snapshot == nil || c.snapshot.Layout == nil || c.snapshot.Layout.TotalScreens <= 0 {
		return 0, ErrNoScreens
	}
	return c.snapshot.Layout.TotalScreens, nil
}

func (c *Coordinator) commitScreenChangeLocked() events.ScreenRotateData {
	c.armRotationTimerLocked()
	data := c.buildRotateDataLocked()
	c.publishRotateLocked(data)
	return data
}

func (c *Coordinator) SelectScreen(index int) (events.ScreenRotateData, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	total, err := c.validateActiveScreensLocked()
	if err != nil {
		return events.ScreenRotateData{}, err
	}
	if index < 0 || index >= total {
		return events.ScreenRotateData{}, ErrInvalidScreenIndex
	}
	c.currentScreen = index
	return c.commitScreenChangeLocked(), nil
}

func (c *Coordinator) AdvanceScreen(direction string) (events.ScreenRotateData, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	total, err := c.validateActiveScreensLocked()
	if err != nil {
		return events.ScreenRotateData{}, err
	}
	if total == 1 {
		c.currentScreen = 0
		return c.commitScreenChangeLocked(), nil
	}
	switch direction {
	case "", "next":
		c.currentScreen = (c.currentScreen + 1) % total
	case "prev":
		c.currentScreen = (c.currentScreen - 1 + total) % total
	default:
		return events.ScreenRotateData{}, ErrInvalidDirection
	}
	return c.commitScreenChangeLocked(), nil
}

func (c *Coordinator) PauseRotation(duration time.Duration) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return 0, errors.New("coordinator stopped")
	}
	c.paused = true
	c.stopRotationTimerLocked()
	c.stopPauseTimerLocked()
	if duration < 0 {
		defaultSec := 120
		if c.snapshot != nil && c.snapshot.Config != nil {
			defaultSec = c.snapshot.Config.Display.Rotation.GetPauseDurationSeconds()
		}
		duration = time.Duration(defaultSec) * time.Second
	}
	if duration > 0 {
		c.pauseGen++
		gen := c.pauseGen
		c.pauseTimer = time.AfterFunc(duration, func() { c.onPauseTimeout(gen) })
	}
	return c.currentScreen, nil
}

func (c *Coordinator) ResumeRotation() (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return 0, errors.New("coordinator stopped")
	}
	c.paused = false
	c.stopPauseTimerLocked()
	c.armRotationTimerLocked()
	return c.currentScreen, nil
}

func (c *Coordinator) UpdateConfig(snap *config.Snapshot) events.ScreenRotateData {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.snapshot = snap
	if snap == nil || snap.Layout == nil || snap.Layout.TotalScreens <= 0 {
		c.currentScreen = 0
		c.stopRotationTimerLocked()
		return events.ScreenRotateData{}
	}
	if c.currentScreen >= snap.Layout.TotalScreens {
		c.currentScreen = 0
	}
	return c.commitScreenChangeLocked()
}

func (c *Coordinator) Stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stopped = true
	c.stopRotationTimerLocked()
	c.stopPauseTimerLocked()
}

func (c *Coordinator) armRotationTimerLocked() {
	c.stopRotationTimerLocked()
	if c.stopped || c.paused || c.snapshot == nil || c.snapshot.Layout == nil || c.snapshot.Layout.TotalScreens <= 1 {
		return
	}
	intervalSec := 30
	if c.snapshot.Config != nil {
		intervalSec = c.snapshot.Config.Display.Rotation.GetIntervalSeconds()
	}
	if intervalSec <= 0 {
		return
	}
	c.timerGen++
	gen := c.timerGen
	c.rotateTimer = time.AfterFunc(time.Duration(intervalSec)*time.Second, func() { c.onRotateTimeout(gen) })
}

func (c *Coordinator) stopRotationTimerLocked() {
	c.timerGen++
	if c.rotateTimer != nil {
		c.rotateTimer.Stop()
		c.rotateTimer = nil
	}
}

func (c *Coordinator) stopPauseTimerLocked() {
	c.pauseGen++
	if c.pauseTimer != nil {
		c.pauseTimer.Stop()
		c.pauseTimer = nil
	}
}

func (c *Coordinator) onRotateTimeout(gen uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.stopped && !c.paused && c.timerGen == gen && c.snapshot != nil && c.snapshot.Layout != nil && c.snapshot.Layout.TotalScreens > 1 {
		c.currentScreen = (c.currentScreen + 1) % c.snapshot.Layout.TotalScreens
		c.commitScreenChangeLocked()
	}
}

func (c *Coordinator) onPauseTimeout(gen uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.stopped && c.paused && c.pauseGen == gen {
		c.paused = false
		c.armRotationTimerLocked()
	}
}

func (c *Coordinator) buildRotateDataLocked() events.ScreenRotateData {
	data := events.ScreenRotateData{CurrentScreen: c.currentScreen, TotalScreens: 1, IntervalSeconds: 30, Widgets: make([]events.ScreenRotateWidget, 0)}
	if c.snapshot != nil {
		if c.snapshot.Config != nil {
			data.IntervalSeconds = c.snapshot.Config.Display.Rotation.GetIntervalSeconds()
		}
		if c.snapshot.Layout != nil {
			data.TotalScreens = c.snapshot.Layout.TotalScreens
			if c.currentScreen < len(c.snapshot.Layout.Screens) {
				for _, pw := range c.snapshot.Layout.Screens[c.currentScreen].Widgets {
					data.Widgets = append(data.Widgets, events.ScreenRotateWidget{
						WidgetID:   pw.WidgetID,
						Origin:     pw.Origin,
						Dimensions: pw.Dimensions,
					})
				}
			}
		}
	}
	return data
}

func (c *Coordinator) publishRotateLocked(data events.ScreenRotateData) {
	if c.broadcaster == nil {
		return
	}
	if bytes, err := json.Marshal(data); err == nil {
		c.broadcaster.Publish(events.EventScreenRotate, bytes)
	} else {
		c.logger.Error("failed to marshal screen.rotate event", "error", err)
	}
}
