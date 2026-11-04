package events

import (
	"encoding/json"
	"maps"
	"strconv"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/domain"
)

type ScreenRotateWidget struct {
	WidgetID   string           `json:"widget_id"`
	Origin     [2]int           `json:"origin"`
	Dimensions domain.Dimension `json:"dimensions"`
}

type ScreenRotateData struct {
	CurrentScreen   int                  `json:"current_screen"`
	TotalScreens    int                  `json:"total_screens"`
	IntervalSeconds int                  `json:"interval_seconds"`
	Widgets         []ScreenRotateWidget `json:"widgets"`
}

type WidgetUpdateData struct {
	WidgetID  string `json:"widget_id"`
	Timestamp string `json:"timestamp"`
	State     string `json:"state"`
	Data      any    `json:"data"`
}

type HeaderWeather struct {
	Temperature float64 `json:"temperature"`
	Units       string  `json:"units"`
	WeatherCode int     `json:"weather_code"`
	Icon        string  `json:"icon"`
	Condition   string  `json:"condition,omitempty"`
	Location    string  `json:"location,omitempty"`
}

type HeaderUpdateData struct {
	Timestamp string         `json:"timestamp"`
	Timezone  string         `json:"timezone"`
	Weather   *HeaderWeather `json:"weather,omitempty"`
}

type VideoStateData struct {
	Mode    string `json:"mode"`
	Primary any    `json:"primary"`
	Pip     any    `json:"pip"`
}

type AudioStateData struct {
	Volume int  `json:"volume"`
	Muted  bool `json:"muted"`
}

type VoiceStateData struct {
	State      string  `json:"state"`
	Transcript *string `json:"transcript"`
	Reply      *string `json:"reply"`
	TTSEngine  *string `json:"tts_engine"`
	Status     *string `json:"status"`
}

type ThemeUpdateData struct {
	Theme string `json:"theme"`
}

type SystemStatusData struct {
	Online       bool              `json:"online"`
	Time         string            `json:"time"`
	ConfigStatus string            `json:"config_status"`
	ConfigError  *string           `json:"config_error"`
	Providers    map[string]string `json:"providers,omitempty"`
	BootID       string            `json:"boot_id,omitempty"`
}

type StateProvider interface {
	CurrentSnapshot() *config.Snapshot
	CurrentStatus() config.Status
	GetWidgetState(widgetID string) (data any, state string, timestamp string, ok bool)
	GetHeaderWeather() (*HeaderWeather, bool)
	GetVideoState() *VideoStateData
	GetAudioState() *AudioStateData
	GetVoiceState() *VoiceStateData
	GetTheme() string
	GetProvidersStatus() map[string]string
	GetScreenRotateData() *ScreenRotateData
	BootID() string
}

type widgetStateEntry struct {
	data      any
	state     string
	timestamp string
}

type InMemoryStateProvider struct {
	mu           sync.RWMutex
	mgr          *config.Manager
	snapshot     *config.Snapshot
	status       *config.Status
	widgetStates map[string]widgetStateEntry
	weather      *HeaderWeather
	videoState   *VideoStateData
	audioState   *AudioStateData
	voiceState   *VoiceStateData
	theme        string
	providers    map[string]string
	screenRotate *ScreenRotateData
	bootID       string
}

func NewInMemoryStateProvider(mgr *config.Manager) *InMemoryStateProvider {
	return &InMemoryStateProvider{
		mgr:          mgr,
		bootID:       strconv.FormatInt(time.Now().UnixNano(), 10),
		widgetStates: make(map[string]widgetStateEntry),
		providers:    make(map[string]string),
		theme:        "day",
		videoState:   &VideoStateData{Mode: "widgets"},
		audioState:   &AudioStateData{Volume: 75},
		voiceState:   &VoiceStateData{State: "idle"},
	}
}

func (p *InMemoryStateProvider) SetSnapshot(snap *config.Snapshot) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.snapshot = snap
}

func (p *InMemoryStateProvider) CurrentSnapshot() *config.Snapshot {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.mgr != nil {
		return p.mgr.CurrentSnapshot()
	}
	return p.snapshot
}

func (p *InMemoryStateProvider) SetStatus(status config.Status) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.status = &status
}

func (p *InMemoryStateProvider) CurrentStatus() config.Status {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.mgr != nil {
		return p.mgr.Status()
	}
	if p.status != nil {
		return *p.status
	}
	return config.Status{ConfigStatus: config.ConfigStatusOK}
}

func (p *InMemoryStateProvider) SetWidgetState(widgetID string, data any, state string, timestamp ...string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	var ts string
	if len(timestamp) > 0 {
		ts = timestamp[0]
	}
	p.widgetStates[widgetID] = widgetStateEntry{data: data, state: state, timestamp: ts}
}

func (p *InMemoryStateProvider) GetWidgetState(widgetID string) (any, string, string, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	entry, ok := p.widgetStates[widgetID]
	if !ok {
		return nil, "", "", false
	}
	return entry.data, entry.state, entry.timestamp, true
}

func (p *InMemoryStateProvider) GetWidgetTimestamp(widgetID string) (string, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	entry, ok := p.widgetStates[widgetID]
	if !ok || entry.timestamp == "" {
		return "", false
	}
	return entry.timestamp, true
}

func (p *InMemoryStateProvider) SetHeaderWeather(weather *HeaderWeather) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.weather = weather
}

func (p *InMemoryStateProvider) GetHeaderWeather() (*HeaderWeather, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.weather, p.weather != nil
}

func (p *InMemoryStateProvider) SetVideoState(state *VideoStateData) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.videoState = state
}

func (p *InMemoryStateProvider) GetVideoState() *VideoStateData {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.videoState != nil {
		return p.videoState
	}
	return &VideoStateData{Mode: "widgets"}
}

func (p *InMemoryStateProvider) SetAudioState(state *AudioStateData) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.audioState = state
}

func (p *InMemoryStateProvider) GetAudioState() *AudioStateData {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.audioState != nil {
		return p.audioState
	}
	return &AudioStateData{Volume: 75}
}

func (p *InMemoryStateProvider) SetVoiceState(state *VoiceStateData) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.voiceState = state
}

func (p *InMemoryStateProvider) GetVoiceState() *VoiceStateData {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.voiceState != nil {
		return p.voiceState
	}
	return &VoiceStateData{State: "idle"}
}

func (p *InMemoryStateProvider) SetTheme(theme string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.theme = theme
}

func (p *InMemoryStateProvider) GetTheme() string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.theme != "" {
		return p.theme
	}
	return "day"
}

func (p *InMemoryStateProvider) SetProvidersStatus(providers map[string]string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.providers = providers
}

func (p *InMemoryStateProvider) GetProvidersStatus() map[string]string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if len(p.providers) == 0 {
		return nil
	}
	copied := make(map[string]string, len(p.providers))
	maps.Copy(copied, p.providers)
	return copied
}

func (p *InMemoryStateProvider) SetScreenRotateData(data *ScreenRotateData) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.screenRotate = data
}

func (p *InMemoryStateProvider) GetScreenRotateData() *ScreenRotateData {
	p.mu.RLock()
	defer p.mu.RUnlock()
	if p.screenRotate == nil {
		return nil
	}
	cp := *p.screenRotate
	return &cp
}

func (p *InMemoryStateProvider) BootID() string { return p.bootID }

func appendHydrationEvent(events []*Event, idGen *IDGenerator, now time.Time, eventType string, payload any) []*Event {
	data, err := json.Marshal(payload)
	if err != nil {
		return events
	}
	return append(events, &Event{ID: idGen.Next(now), Type: eventType, Data: data, Timestamp: now})
}

func buildScreenRotateFromSnapshot(snap *config.Snapshot) ScreenRotateData {
	rd := ScreenRotateData{TotalScreens: 1, IntervalSeconds: 30, Widgets: []ScreenRotateWidget{}}
	if snap == nil || snap.Config == nil {
		return rd
	}
	rd.IntervalSeconds = snap.Config.Display.Rotation.GetIntervalSeconds()
	if snap.Layout == nil || len(snap.Layout.Screens) == 0 {
		return rd
	}
	rd.TotalScreens = snap.Layout.TotalScreens
	for _, pw := range snap.Layout.Screens[0].Widgets {
		rd.Widgets = append(rd.Widgets, ScreenRotateWidget{WidgetID: pw.WidgetID, Origin: pw.Origin, Dimensions: pw.Dimensions})
	}
	return rd
}

func buildSystemStatusData(provider StateProvider, status config.Status, nowStr string) SystemStatusData {
	configStatus := string(status.ConfigStatus)
	if configStatus == "" {
		configStatus = "ok"
	}
	var providers map[string]string
	var bootID string
	if provider != nil {
		providers = provider.GetProvidersStatus()
		bootID = provider.BootID()
	}
	return SystemStatusData{
		Online:       true,
		Time:         nowStr,
		ConfigStatus: configStatus,
		ConfigError:  status.ConfigError,
		Providers:    providers,
		BootID:       bootID,
	}
}

func buildWidgetUpdatePayload(provider StateProvider, widgetID, nowStr string) WidgetUpdateData {
	payload := WidgetUpdateData{WidgetID: widgetID, Timestamp: nowStr, State: "healthy", Data: map[string]any{}}
	if provider == nil {
		return payload
	}
	if d, s, ts, ok := provider.GetWidgetState(widgetID); ok {
		if d != nil {
			payload.Data = d
		}
		if s != "" {
			payload.State = s
		}
		if ts != "" {
			payload.Timestamp = ts
		}
	}
	if tsP, ok := provider.(interface {
		GetWidgetTimestamp(widgetID string) (string, bool)
	}); ok {
		if ts, ok := tsP.GetWidgetTimestamp(widgetID); ok && ts != "" {
			payload.Timestamp = ts
		}
	}
	return payload
}

func appendWidgetHydrationEvents(events []*Event, snap *config.Snapshot, provider StateProvider, idGen *IDGenerator, now time.Time, nowStr string) []*Event {
	if snap == nil || snap.Config == nil {
		return events
	}
	for _, w := range snap.Config.Display.Widgets {
		payload := buildWidgetUpdatePayload(provider, w.ID, nowStr)
		events = appendHydrationEvent(events, idGen, now, EventWidgetUpdate, payload)
	}
	return events
}

func resolveSubsystemHydrationState(provider StateProvider, snap *config.Snapshot) (*HeaderWeather, string, *VideoStateData, *AudioStateData, *VoiceStateData, config.Status, string) {
	var weather *HeaderWeather
	tz := "UTC"
	vid := &VideoStateData{Mode: "widgets"}
	aud := &AudioStateData{Volume: 75}
	vc := &VoiceStateData{State: "idle"}
	var status config.Status
	theme := "day"

	if provider == nil {
		return weather, tz, vid, aud, vc, status, theme
	}

	if w, ok := provider.GetHeaderWeather(); ok {
		weather = w
	}
	if snap != nil && snap.Config != nil && snap.Config.Timezone != "" {
		tz = snap.Config.Timezone
	}
	if v := provider.GetVideoState(); v != nil {
		vid = v
	}
	if a := provider.GetAudioState(); a != nil {
		aud = a
	}
	if v := provider.GetVoiceState(); v != nil {
		vc = v
	}
	if t := provider.GetTheme(); t != "" {
		theme = t
	}
	status = provider.CurrentStatus()

	return weather, tz, vid, aud, vc, status, theme
}

func BuildHydrationBatch(provider StateProvider, idGen *IDGenerator, now time.Time) []*Event {
	events := make([]*Event, 0, 8)
	nowStr := now.UTC().Format(time.RFC3339)
	var snap *config.Snapshot
	if provider != nil {
		snap = provider.CurrentSnapshot()
	}

	rotateData := buildScreenRotateFromSnapshot(snap)
	if provider != nil {
		if rd := provider.GetScreenRotateData(); rd != nil {
			rotateData = *rd
		}
	}
	events = appendHydrationEvent(events, idGen, now, EventScreenRotate, rotateData)

	events = appendWidgetHydrationEvents(events, snap, provider, idGen, now, nowStr)

	weather, tz, vid, aud, vc, status, theme := resolveSubsystemHydrationState(provider, snap)

	events = appendHydrationEvent(events, idGen, now, EventHeaderUpdate, HeaderUpdateData{Timestamp: nowStr, Timezone: tz, Weather: weather})
	events = appendHydrationEvent(events, idGen, now, EventVideoState, vid)
	events = appendHydrationEvent(events, idGen, now, EventAudioState, aud)
	events = appendHydrationEvent(events, idGen, now, EventVoiceState, vc)
	events = appendHydrationEvent(events, idGen, now, EventSystemStatus, buildSystemStatusData(provider, status, nowStr))
	events = appendHydrationEvent(events, idGen, now, EventThemeUpdate, ThemeUpdateData{Theme: theme})
	return events
}
