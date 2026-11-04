package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	defaultOpenMeteoBaseURL = "https://api.open-meteo.com/v1/forecast"
	defaultWeatherTimeout   = 10 * time.Second
)

// MapWMOCode maps a WMO 4501 weather code to human-readable condition text and vector icon token.
// Defaults to daytime mapping for backward compatibility.
func MapWMOCode(code int) (string, string) {
	return MapWMOCodeWithDay(code, 1)
}

func mapWMONightCode(code int) (string, string, bool) {
	switch code {
	case 0:
		return "Clear", "weather-clear-night", true
	case 1:
		return "Mainly Clear", "weather-night-partly-cloudy", true
	case 2:
		return "Partly Cloudy", "weather-night-partly-cloudy", true
	default:
		return "", "", false
	}
}

// MapWMOCodeWithDay maps a WMO 4501 weather code and day/night solar flag (1 for day, 0 for night)
// to human-readable condition text and vector icon token.
func MapWMOCodeWithDay(code int, isDay int) (string, string) {
	if isDay == 0 {
		if cond, icon, ok := mapWMONightCode(code); ok {
			return cond, icon
		}
	}
	switch code {
	case 0:
		return "Clear Sky", "weather-sunny"
	case 1:
		return "Mainly Clear", "weather-partly-cloudy"
	case 2:
		return "Partly Cloudy", "weather-partly-cloudy"
	case 3:
		return "Overcast", "weather-cloudy"
	case 45, 48:
		return "Fog", "weather-fog"
	case 51, 53, 55:
		return "Drizzle", "weather-rainy"
	case 56, 57:
		return "Freezing Drizzle", "weather-snowy-rainy"
	case 61, 63, 65:
		return "Rain", "weather-rainy"
	case 66, 67:
		return "Freezing Rain", "weather-snowy-rainy"
	case 71, 73, 75:
		return "Snow Fall", "weather-snowy"
	case 77:
		return "Snow Grains", "weather-snowy"
	case 80, 81, 82:
		return "Rain Showers", "weather-pouring"
	case 85, 86:
		return "Snow Showers", "weather-snowy"
	case 95:
		return "Thunderstorm", "weather-lightning"
	case 96, 99:
		return "Thunderstorm with Hail", "weather-lightning-rainy"
	default:
		return "Unknown", "weather-cloudy"
	}
}

// WeatherSnapshot matches the normalized internal weather schema in .
type WeatherSnapshot struct {
	Location string          `json:"location,omitempty"`
	Current  WeatherCurrent  `json:"current"`
	Hourly   []WeatherHourly `json:"hourly"`
	Daily    []WeatherDaily  `json:"daily"`
}

// TodayPrecipProbMax returns today's maximum precipitation probability percentage (0-100).
func (s WeatherSnapshot) TodayPrecipProbMax() int {
	if len(s.Daily) > 0 {
		return s.Daily[0].PrecipProbMax
	}
	return s.Current.PrecipProbMax
}

// WeatherCurrent encapsulates current ambient observations.
type WeatherCurrent struct {
	Temperature   float64      `json:"temperature"`
	FeelsLike     float64      `json:"feels_like"`
	Humidity      int          `json:"humidity"`
	WindSpeed     float64      `json:"wind_speed"`
	PrecipProbMax int          `json:"precip_prob_max"`
	Units         WeatherUnits `json:"units"`
	ConditionCode int          `json:"condition_code"`
	ConditionText string       `json:"condition_text"`
	Icon          string       `json:"icon"`
}

// WeatherUnits represents measurement unit labels.
type WeatherUnits struct {
	Temperature string `json:"temperature"`
	WindSpeed   string `json:"wind_speed"`
}

// WeatherHourly represents hourly forecasted data.
type WeatherHourly struct {
	Time       string  `json:"time"`
	Temp       float64 `json:"temp"`
	PrecipProb int     `json:"precip_prob"`
	Icon       string  `json:"icon"`
}

// WeatherDaily represents daily forecasted summary.
type WeatherDaily struct {
	Date          string  `json:"date"`
	TempMax       float64 `json:"temp_max"`
	TempMin       float64 `json:"temp_min"`
	PrecipProbMax int     `json:"precip_prob_max"`
	ConditionText string  `json:"condition_text"`
	Icon          string  `json:"icon"`
}

type openMeteoResponse struct {
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Timezone  string  `json:"timezone"`
	Current   struct {
		Time                string  `json:"time"`
		Temperature2m       float64 `json:"temperature_2m"`
		RelativeHumidity2m  int     `json:"relative_humidity_2m"`
		ApparentTemperature float64 `json:"apparent_temperature"`
		Precipitation       float64 `json:"precipitation"`
		WeatherCode         int     `json:"weather_code"`
		WindSpeed10m        float64 `json:"wind_speed_10m"`
		IsDay               *int    `json:"is_day"`
	} `json:"current"`
	Hourly struct {
		Time                     []string  `json:"time"`
		Temperature2m            []float64 `json:"temperature_2m"`
		PrecipitationProbability []int     `json:"precipitation_probability"`
		WeatherCode              []int     `json:"weather_code"`
		IsDay                    []int     `json:"is_day"`
	} `json:"hourly"`
	Daily struct {
		Time                        []string  `json:"time"`
		WeatherCode                 []int     `json:"weather_code"`
		Temperature2mMax            []float64 `json:"temperature_2m_max"`
		Temperature2mMin            []float64 `json:"temperature_2m_min"`
		PrecipitationProbabilityMax []int     `json:"precipitation_probability_max"`
	} `json:"daily"`
}

// WeatherProvider ingests weather data from Open-Meteo.
type WeatherProvider struct {
	client    *http.Client
	baseURL   string
	latitude  float64
	longitude float64
	units     string
	timezone  string
	location  string
	logger    *slog.Logger
}

func init() {
	registerBuiltin("weather-forecast", NewWeatherProvider)
}

// NewWeatherProvider constructs a standard WeatherProvider instance.
func NewWeatherProvider() Provider {
	return NewWeatherProviderWithClient(nil, "")
}

// NewWeatherProviderWithClient constructs a WeatherProvider with injected HTTP client and optional base URL override.
func NewWeatherProviderWithClient(client *http.Client, baseURL string) *WeatherProvider {
	if client == nil {
		client = &http.Client{Timeout: defaultWeatherTimeout}
	}
	if baseURL == "" {
		baseURL = defaultOpenMeteoBaseURL
	}
	return &WeatherProvider{
		client:  client,
		baseURL: baseURL,
		units:   "imperial",
		logger:  slog.Default(),
	}
}

// WeatherConfig holds optional subconfiguration fields for WeatherProvider.
type WeatherConfig struct {
	Units    string
	Timezone string
	Location string
}

func resolveWeatherCoord(config map[string]any, key string) (float64, error) {
	val, ok := toFloat(config[key])
	if !ok {
		return 0, errors.New("weather provider requires valid latitude and longitude")
	}
	if key == "latitude" && (val < -90 || val > 90) {
		return 0, fmt.Errorf("latitude %.4f out of range [-90, 90]", val)
	}
	if key == "longitude" && (val < -180 || val > 180) {
		return 0, fmt.Errorf("longitude %.4f out of range [-180, 180]", val)
	}
	return val, nil
}

func applyWeatherSubConfigs(config map[string]any, cfg *WeatherConfig) {
	if n, ok := config["name"].(string); ok && strings.TrimSpace(n) != "" {
		cfg.Location = strings.TrimSpace(n)
	} else if loc, ok := config["location_name"].(string); ok && strings.TrimSpace(loc) != "" {
		cfg.Location = strings.TrimSpace(loc)
	}
}

// Init initializes the provider with instance domain config and framework options.
func (p *WeatherProvider) Init(ctx context.Context, config map[string]any, opts InitOptions) error {
	latVal, err := resolveWeatherCoord(config, "latitude")
	if err != nil {
		return err
	}
	lonVal, err := resolveWeatherCoord(config, "longitude")
	if err != nil {
		return err
	}

	units := "imperial"
	if u, ok := config["units"].(string); ok && strings.TrimSpace(u) != "" {
		lower := strings.ToLower(strings.TrimSpace(u))
		if lower == "metric" || lower == "imperial" {
			units = lower
		} else {
			return fmt.Errorf("invalid units %q: must be 'imperial' or 'metric'", u)
		}
	}

	tz := "UTC"
	if strings.TrimSpace(opts.Timezone) != "" {
		tz = strings.TrimSpace(opts.Timezone)
	}

	if opts.Endpoint != "" {
		p.baseURL = opts.Endpoint
	}

	subCfg := WeatherConfig{
		Units:    units,
		Timezone: tz,
	}
	applyWeatherSubConfigs(config, &subCfg)

	p.latitude = latVal
	p.longitude = lonVal
	p.units = units
	p.timezone = tz
	p.location = subCfg.Location

	return nil
}

// Fetch queries Open-Meteo and returns normalized WeatherSnapshot domain data.
func (p *WeatherProvider) Fetch(ctx context.Context) (any, error) {
	tempUnit := "fahrenheit"
	windUnit := "mph"
	precipUnit := "inch"
	tempLabel := "°F"
	windLabel := "mph"

	if p.units == "metric" {
		tempUnit = "celsius"
		windUnit = "kmh"
		precipUnit = "mm"
		tempLabel = "°C"
		windLabel = "km/h"
	}

	parsedURL, err := url.Parse(p.baseURL)
	if err != nil {
		return nil, fmt.Errorf("invalid weather baseURL: %w", err)
	}

	q := parsedURL.Query()
	q.Set("latitude", fmt.Sprintf("%.4f", p.latitude))
	q.Set("longitude", fmt.Sprintf("%.4f", p.longitude))
	q.Set("current", "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day")
	q.Set("hourly", "temperature_2m,precipitation_probability,weather_code,is_day")
	q.Set("daily", "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max")
	q.Set("temperature_unit", tempUnit)
	q.Set("wind_speed_unit", windUnit)
	q.Set("precipitation_unit", precipUnit)
	q.Set("timezone", p.timezone)
	parsedURL.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, parsedURL.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create weather request: %w", err)
	}
	req.Header.Set("Accept", "application/json")

	resp, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, MaxHTTPResponseBodySize))
	if err != nil {
		return nil, fmt.Errorf("failed to read weather response: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(bodyBytes)))
	}

	var data openMeteoResponse
	if err := json.Unmarshal(bodyBytes, &data); err != nil {
		return nil, fmt.Errorf("failed to decode Open-Meteo response: %w", err)
	}

	currentIsDay := 1
	if data.Current.IsDay != nil {
		currentIsDay = *data.Current.IsDay
	}
	condText, iconToken := MapWMOCodeWithDay(data.Current.WeatherCode, currentIsDay)

	var todayPrecipMax int
	if len(data.Daily.PrecipitationProbabilityMax) > 0 {
		todayPrecipMax = data.Daily.PrecipitationProbabilityMax[0]
	}

	snapshot := WeatherSnapshot{
		Location: p.location,
		Current: WeatherCurrent{
			Temperature:   data.Current.Temperature2m,
			FeelsLike:     data.Current.ApparentTemperature,
			Humidity:      data.Current.RelativeHumidity2m,
			WindSpeed:     data.Current.WindSpeed10m,
			PrecipProbMax: todayPrecipMax,
			Units:         WeatherUnits{Temperature: tempLabel, WindSpeed: windLabel},
			ConditionCode: data.Current.WeatherCode,
			ConditionText: condText,
			Icon:          iconToken,
		},
		Hourly: make([]WeatherHourly, 0, len(data.Hourly.Time)),
		Daily:  make([]WeatherDaily, 0, len(data.Daily.Time)),
	}

	maxSafeLen := minSliceLen(len(data.Hourly.Time), len(data.Hourly.Temperature2m), len(data.Hourly.WeatherCode), len(data.Hourly.PrecipitationProbability))

	startIndex := 0
	if len(data.Current.Time) >= 13 {
		curHour := data.Current.Time[:13]
		for i := range maxSafeLen {
			hTime := data.Hourly.Time[i]
			if len(hTime) >= 13 && hTime[:13] >= curHour {
				startIndex = i
				break
			}
		}
	}
	if startIndex >= maxSafeLen {
		startIndex = 0
	}

	// Limit hourly timeline to next 24 entries
	endIndex := min(startIndex+24, maxSafeLen)

	for i := startIndex; i < endIndex; i++ {
		hIsDay := 1
		if i < len(data.Hourly.IsDay) {
			hIsDay = data.Hourly.IsDay[i]
		}
		_, hIcon := MapWMOCodeWithDay(data.Hourly.WeatherCode[i], hIsDay)
		snapshot.Hourly = append(snapshot.Hourly, WeatherHourly{
			Time:       data.Hourly.Time[i],
			Temp:       data.Hourly.Temperature2m[i],
			PrecipProb: data.Hourly.PrecipitationProbability[i],
			Icon:       hIcon,
		})
	}

	dailyCount := minSliceLen(7, len(data.Daily.Time), len(data.Daily.Temperature2mMax), len(data.Daily.Temperature2mMin), len(data.Daily.WeatherCode), len(data.Daily.PrecipitationProbabilityMax))

	for i := range dailyCount {
		dText, dIcon := MapWMOCode(data.Daily.WeatherCode[i])
		snapshot.Daily = append(snapshot.Daily, WeatherDaily{
			Date:          data.Daily.Time[i],
			TempMax:       data.Daily.Temperature2mMax[i],
			TempMin:       data.Daily.Temperature2mMin[i],
			PrecipProbMax: data.Daily.PrecipitationProbabilityMax[i],
			ConditionText: dText,
			Icon:          dIcon,
		})
	}

	return snapshot, nil
}

// Subscribe returns nil as Open-Meteo polling does not maintain persistent push connections.
func (p *WeatherProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown cleanly stops the provider.
func (p *WeatherProvider) Shutdown(ctx context.Context) error {
	return nil
}

func minSliceLen(n int, lens ...int) int {
	minVal := n
	for _, l := range lens {
		if l < minVal {
			minVal = l
		}
	}
	return minVal
}
