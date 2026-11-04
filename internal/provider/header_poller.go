package provider

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/config"
	"github.com/azylman/mirrormere/internal/events"
)

// HeaderWeatherSink abstracts state persistence for ambient header weather.
type HeaderWeatherSink interface {
	SetHeaderWeather(weather *events.HeaderWeather)
}

// HeaderWeatherPollerConfig defines dependencies for the autonomous header weather poller.
type HeaderWeatherPollerConfig struct {
	Client      *http.Client
	BaseURL     string
	Broadcaster Broadcaster
	StateSink   HeaderWeatherSink
	Logger      *slog.Logger
	NowFunc     func() time.Time
}

type headerOpenMeteoResponse struct {
	Current struct {
		Temperature2m float64 `json:"temperature_2m"`
		WeatherCode   int     `json:"weather_code"`
		IsDay         *int    `json:"is_day"`
	} `json:"current"`
}

// HeaderWeatherPoller manages autonomous polling for display.header.weather.
type HeaderWeatherPoller struct {
	client      *http.Client
	baseURL     string
	broadcaster Broadcaster
	stateSink   HeaderWeatherSink
	logger      *slog.Logger
	nowFunc     func() time.Time

	mu         sync.Mutex
	weatherCfg *config.HeaderWeatherConfig
	timezone   string
	stopCh     chan struct{}
	running    bool
}

// NewHeaderWeatherPoller constructs an autonomous HeaderWeatherPoller.
func NewHeaderWeatherPoller(cfg HeaderWeatherPollerConfig) *HeaderWeatherPoller {
	client := cfg.Client
	if client == nil {
		client = &http.Client{Timeout: defaultWeatherTimeout}
	}
	baseURL := cfg.BaseURL
	if baseURL == "" {
		baseURL = defaultOpenMeteoBaseURL
	}
	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	nowFunc := cfg.NowFunc
	if nowFunc == nil {
		nowFunc = time.Now
	}

	return &HeaderWeatherPoller{
		client:      client,
		baseURL:     baseURL,
		broadcaster: cfg.Broadcaster,
		stateSink:   cfg.StateSink,
		logger:      logger,
		nowFunc:     nowFunc,
	}
}

// Start begins background polling for the header weather badge if configured.
func (p *HeaderWeatherPoller) Start(ctx context.Context, weatherCfg *config.HeaderWeatherConfig, timezone string) {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		p.stopLocked()
	}

	if weatherCfg == nil {
		return
	}

	p.weatherCfg = weatherCfg
	p.timezone = timezone
	if p.timezone == "" {
		p.timezone = "UTC"
	}
	p.stopCh = make(chan struct{})
	p.running = true

	go p.pollLoop(ctx, weatherCfg, p.stopCh)
}

// Stop cleanly terminates the background poller loop.
func (p *HeaderWeatherPoller) Stop() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.stopLocked()
}

func (p *HeaderWeatherPoller) stopLocked() {
	if !p.running {
		return
	}
	close(p.stopCh)
	p.running = false
}

// UpdateConfig applies configuration changes during live reload.
func (p *HeaderWeatherPoller) UpdateConfig(ctx context.Context, weatherCfg *config.HeaderWeatherConfig, timezone string) {
	p.Start(ctx, weatherCfg, timezone)
}

func (p *HeaderWeatherPoller) pollLoop(ctx context.Context, weatherCfg *config.HeaderWeatherConfig, stopCh <-chan struct{}) {
	// Execute immediate initial fetch
	if _, err := p.FetchOnce(ctx); err != nil {
		p.logger.Warn("initial header weather fetch failed", "error", err)
	}

	interval := time.Duration(weatherCfg.GetRefreshIntervalSeconds()) * time.Second
	if interval <= 0 {
		interval = 900 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-stopCh:
			return
		case <-ticker.C:
			if _, err := p.FetchOnce(ctx); err != nil {
				p.logger.Warn("periodic header weather fetch failed", "error", err)
			}
		}
	}
}

// FetchOnce executes an immediate single weather fetch, updates state sink, and broadcasts header.update.
func (p *HeaderWeatherPoller) FetchOnce(ctx context.Context) (*events.HeaderWeather, error) {
	p.mu.Lock()
	cfg := p.weatherCfg
	tz := p.timezone
	p.mu.Unlock()

	if cfg == nil {
		return nil, nil
	}

	tempUnit := "fahrenheit"
	unitLabel := "F"
	if strings.ToLower(strings.TrimSpace(cfg.Units)) == "metric" {
		tempUnit = "celsius"
		unitLabel = "C"
	}

	parsedURL, err := url.Parse(p.baseURL)
	if err != nil {
		return nil, fmt.Errorf("invalid header weather baseURL: %w", err)
	}

	q := parsedURL.Query()
	q.Set("latitude", fmt.Sprintf("%.4f", cfg.Latitude))
	q.Set("longitude", fmt.Sprintf("%.4f", cfg.Longitude))
	q.Set("current", "temperature_2m,weather_code,is_day")
	q.Set("temperature_unit", tempUnit)
	if tz != "" {
		q.Set("timezone", tz)
	} else {
		q.Set("timezone", "UTC")
	}
	parsedURL.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, parsedURL.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create header weather request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, MaxHTTPResponseBodySize))
	if err != nil {
		return nil, fmt.Errorf("failed to read header weather response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(bodyBytes)))
	}

	var data headerOpenMeteoResponse
	if err := json.Unmarshal(bodyBytes, &data); err != nil {
		return nil, fmt.Errorf("failed to decode header Open-Meteo response: %w", err)
	}

	isDay := 1
	if data.Current.IsDay != nil {
		isDay = *data.Current.IsDay
	}
	condText, iconToken := MapWMOCodeWithDay(data.Current.WeatherCode, isDay)
	loc := strings.TrimSpace(cfg.Location)

	hw := &events.HeaderWeather{
		Temperature: data.Current.Temperature2m,
		Units:       unitLabel,
		WeatherCode: data.Current.WeatherCode,
		Icon:        iconToken,
		Condition:   condText,
		Location:    loc,
	}

	if p.stateSink != nil {
		p.stateSink.SetHeaderWeather(hw)
	}

	if p.broadcaster != nil {
		nowStr := p.nowFunc().UTC().Format(time.RFC3339)
		payload := events.HeaderUpdateData{
			Timestamp: nowStr,
			Timezone:  tz,
			Weather:   hw,
		}
		if payloadBytes, err := json.Marshal(payload); err == nil {
			p.broadcaster.Publish(events.EventHeaderUpdate, payloadBytes)
		}
	}

	return hw, nil
}
