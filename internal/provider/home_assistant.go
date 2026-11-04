package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	defaultHATimeout = 10 * time.Second
)

// Sentinel errors for Home Assistant widget actions and upstream calls.
var (
	ErrActionValidation     = errors.New("invalid widget action")
	ErrUpstreamUnauthorized = errors.New("upstream unauthorized")
	ErrUpstreamNotFound     = errors.New("upstream not found")
	ErrUpstreamUnavailable  = errors.New("upstream unavailable")
	ErrUpstreamStatus       = errors.New("upstream error status")
)

// SupportedDomains lists the 12 Home Assistant entity domains supported by the native widget.
var SupportedDomains = []string{
	"light",
	"switch",
	"fan",
	"cover",
	"lock",
	"climate",
	"media_player",
	"camera",
	"scene",
	"script",
	"sensor",
	"binary_sensor",
}

var haIdentifierRegex = regexp.MustCompile(`^[a-z0-9_]+$`)

func init() {
	registerBuiltin("home-assistant", func() Provider {
		return NewHomeAssistantProvider()
	})
}

// IsValidIdentifier checks whether a domain or service string conforms to ^[a-z0-9_]+$.
func IsValidIdentifier(s string) bool {
	return haIdentifierRegex.MatchString(s)
}

// IsSupportedDomain checks if the given domain string is one of the 12 supported domains.
func IsSupportedDomain(domain string) bool {
	domain = strings.ToLower(strings.TrimSpace(domain))
	return slices.Contains(SupportedDomains, domain)
}

// EntityState represents an entity state returned from Home Assistant /api/states.
type EntityState struct {
	EntityID    string         `json:"entity_id"`
	State       string         `json:"state"`
	Attributes  map[string]any `json:"attributes"`
	LastChanged string         `json:"last_changed,omitempty"`
	LastUpdated string         `json:"last_updated,omitempty"`
}

// Domain returns the domain prefix of the entity (e.g. "light" for "light.kitchen").
func (e EntityState) Domain() string {
	parts := strings.SplitN(e.EntityID, ".", 2)
	if len(parts) > 0 {
		return parts[0]
	}
	return ""
}

// LovelaceConfig represents the root Lovelace dashboard structure returned by /api/lovelace/config.
type LovelaceConfig struct {
	Title string         `json:"title,omitempty"`
	Views []LovelaceView `json:"views"`
	Raw   map[string]any `json:"raw,omitempty"`
}

// LovelaceView represents a view within a Lovelace dashboard.
type LovelaceView struct {
	Title    string            `json:"title,omitempty"`
	Path     string            `json:"path,omitempty"`
	Icon     string            `json:"icon,omitempty"`
	Type     string            `json:"type,omitempty"` // e.g. "sections", "masonry", "panel"
	Cards    []LovelaceCard    `json:"cards,omitempty"`
	Sections []LovelaceSection `json:"sections,omitempty"`
	Raw      map[string]any    `json:"raw,omitempty"`
}

// UnmarshalJSON preserves all raw fields into Raw while unmarshaling typed fields.
func (v *LovelaceView) UnmarshalJSON(data []byte) error {
	type Alias LovelaceView
	var alias Alias
	if err := json.Unmarshal(data, &alias); err != nil {
		return err
	}
	*v = LovelaceView(alias)
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err == nil {
		v.Raw = raw
	}
	return nil
}

// LovelaceSection represents a section inside a sections-view.
type LovelaceSection struct {
	Title string         `json:"title,omitempty"`
	Cards []LovelaceCard `json:"cards,omitempty"`
	Raw   map[string]any `json:"raw,omitempty"`
}

// UnmarshalJSON preserves all raw fields into Raw while unmarshaling typed fields.
func (s *LovelaceSection) UnmarshalJSON(data []byte) error {
	type Alias LovelaceSection
	var alias Alias
	if err := json.Unmarshal(data, &alias); err != nil {
		return err
	}
	*s = LovelaceSection(alias)
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err == nil {
		s.Raw = raw
	}
	return nil
}

// LovelaceCard represents a card in a Lovelace view/section.
type LovelaceCard struct {
	Type        string            `json:"type"` // sections, grid, tile, entities, button, thermostat, picture-entity, etc.
	Title       string            `json:"title,omitempty"`
	Entity      string            `json:"entity,omitempty"`
	Entities    []any             `json:"entities,omitempty"`
	Cards       []LovelaceCard    `json:"cards,omitempty"`
	Sections    []LovelaceSection `json:"sections,omitempty"`
	Name        string            `json:"name,omitempty"`
	Icon        string            `json:"icon,omitempty"`
	Image       string            `json:"image,omitempty"`
	CameraImage string            `json:"camera_image,omitempty"`
	CameraView  string            `json:"camera_view,omitempty"`
	TapAction   map[string]any    `json:"tap_action,omitempty"`
	HoldAction  map[string]any    `json:"hold_action,omitempty"`
	Columns     int               `json:"columns,omitempty"`
	Square      bool              `json:"square,omitempty"`
	Raw         map[string]any    `json:"raw,omitempty"`
}

// UnmarshalJSON preserves all raw fields into Raw while unmarshaling typed fields.
func (c *LovelaceCard) UnmarshalJSON(data []byte) error {
	type Alias LovelaceCard
	var alias Alias
	if err := json.Unmarshal(data, &alias); err != nil {
		return err
	}
	*c = LovelaceCard(alias)
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err == nil {
		c.Raw = raw
	}
	return nil
}

// LovelaceLayout represents the discovered layout for the configured view.
type LovelaceLayout struct {
	Title              string         `json:"title,omitempty"`
	View               LovelaceView   `json:"view"`
	Views              []LovelaceView `json:"views,omitempty"`
	Cards              []LovelaceCard `json:"cards"`
	DiscoveredEntities []string       `json:"discovered_entities"`
}

// HomeAssistantSnapshot is the normalized data payload returned by Fetch.
type HomeAssistantSnapshot struct {
	Title              string                 `json:"title,omitempty"`
	Dashboard          string                 `json:"dashboard,omitempty"`
	View               LovelaceView           `json:"view"`
	Views              []LovelaceView         `json:"views,omitempty"`
	Cards              []LovelaceCard         `json:"cards"`
	Entities           map[string]EntityState `json:"entities"`
	DiscoveredEntities []string               `json:"discovered_entities"`
	LastUpdated        string                 `json:"last_updated"`
}

// HomeAssistantConfig holds parsed configuration.
type HomeAssistantConfig struct {
	BaseURL   string
	Token     string
	Dashboard string
	View      string
	Title     string
	Domains   []string
	Entities  []string
}

// HomeAssistantProvider implements Provider for Home Assistant Lovelace & entity states.
type HomeAssistantProvider struct {
	mu      sync.RWMutex
	client  *http.Client
	baseURL string
	token   string
	cfg     HomeAssistantConfig
	logger  *slog.Logger
}

var _ Provider = (*HomeAssistantProvider)(nil)

// NewHomeAssistantProvider constructs a standard HomeAssistantProvider instance.
func NewHomeAssistantProvider() *HomeAssistantProvider {
	return NewHomeAssistantProviderWithClient(nil, "", "")
}

// NewHomeAssistantProviderWithClient constructs a HomeAssistantProvider with injected HTTP client, base URL, and token.
func NewHomeAssistantProviderWithClient(client *http.Client, baseURL, token string) *HomeAssistantProvider {
	if client == nil {
		client = &http.Client{Timeout: defaultHATimeout}
	}
	return &HomeAssistantProvider{
		client:  client,
		baseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		token:   strings.TrimSpace(token),
		logger:  slog.Default(),
	}
}

// Init initializes the provider with instance domain config and framework options.
func (p *HomeAssistantProvider) Init(ctx context.Context, config map[string]any, opts InitOptions) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	baseURL := resolveConfigField(config, opts, "url", "base_url")
	if baseURL == "" {
		baseURL = strings.TrimSpace(opts.Endpoint)
	}
	if baseURL == "" {
		baseURL = p.baseURL
	}
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		return errors.New("home-assistant provider requires a valid URL (via 'url', 'base_url', 'url_env', or opts.Endpoint)")
	}

	token := resolveToken(config, opts, p.token)
	if token == "" {
		return errors.New("home-assistant provider requires a valid access token (via 'token_env' or opts.Token)")
	}

	p.baseURL = baseURL
	p.token = token

	dashboard := ""
	if d, ok := config["dashboard"].(string); ok {
		dashboard = strings.TrimSpace(d)
	}

	view := ""
	if v, ok := config["view"]; ok && v != nil {
		view = strings.TrimSpace(fmt.Sprint(v))
	}

	title := ""
	if t, ok := config["title"].(string); ok && strings.TrimSpace(t) != "" {
		title = strings.TrimSpace(t)
	} else if n, ok := config["name"].(string); ok && strings.TrimSpace(n) != "" {
		title = strings.TrimSpace(n)
	}

	var domains []string
	if rawDomains, ok := config["domains"]; ok {
		domains = toStringSlice(rawDomains)
	}

	var entities []string
	if rawEntities, ok := config["entities"]; ok {
		entities = toStringSlice(rawEntities)
	}

	p.cfg = HomeAssistantConfig{
		BaseURL:   baseURL,
		Token:     token,
		Dashboard: dashboard,
		View:      view,
		Title:     title,
		Domains:   domains,
		Entities:  entities,
	}

	return nil
}

// Fetch retrieves the current Lovelace layout and hydrated entity states from Home Assistant.
func (p *HomeAssistantProvider) Fetch(ctx context.Context) (any, error) {
	p.mu.RLock()
	cfg := p.cfg
	p.mu.RUnlock()

	// 1. Fetch Lovelace dashboard config
	lovelaceCfg, err := p.FetchLovelaceConfig(ctx, cfg.Dashboard)
	if err != nil && p.logger != nil {
		p.logger.Debug("lovelace config fetch failed, proceeding with fallback layout", "error", err)
	}

	// 2. Discover layout
	var layout *LovelaceLayout
	if lovelaceCfg != nil {
		layout = p.DiscoverLayout(lovelaceCfg, cfg.View)
	} else {
		layout = &LovelaceLayout{
			View: LovelaceView{
				Title: cfg.Title,
			},
		}
	}

	// 3. Hydrate states across supported/configured domains
	states, err := p.HydrateStates(ctx)
	if err != nil {
		return nil, fmt.Errorf("home-assistant hydration failed: %w", err)
	}

	// 4. If no cards were discovered from Lovelace, auto-generate fallback cards from states or configured entities
	if len(layout.Cards) == 0 {
		layout.Cards = p.generateFallbackCards(states, cfg.Entities)
		for _, c := range layout.Cards {
			layout.DiscoveredEntities = append(layout.DiscoveredEntities, ExtractEntitiesFromCard(c)...)
		}
		layout.DiscoveredEntities = deduplicateStrings(layout.DiscoveredEntities)
	}

	// 5. Append any explicitly configured entities into discovered entities
	if len(cfg.Entities) > 0 {
		layout.DiscoveredEntities = deduplicateStrings(append(layout.DiscoveredEntities, cfg.Entities...))
	}

	title := cfg.Title
	if title == "" {
		title = layout.Title
	}
	if title == "" && lovelaceCfg != nil {
		title = lovelaceCfg.Title
	}
	if title == "" {
		title = "Home Assistant"
	}

	snapshot := HomeAssistantSnapshot{
		Title:              title,
		Dashboard:          cfg.Dashboard,
		View:               layout.View,
		Views:              layout.Views,
		Cards:              layout.Cards,
		Entities:           states,
		DiscoveredEntities: layout.DiscoveredEntities,
		LastUpdated:        time.Now().UTC().Format(time.RFC3339),
	}

	return snapshot, nil
}

// Subscribe returns nil as Home Assistant entities are synchronized via coordinator refresh loops.
func (p *HomeAssistantProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown cleanly stops the provider.
func (p *HomeAssistantProvider) Shutdown(ctx context.Context) error {
	return nil
}

// CallService executes a Home Assistant service call via POST /api/services/{domain}/{service}.
func (p *HomeAssistantProvider) CallService(ctx context.Context, domain, service string, serviceData map[string]any) error {
	_, err := p.CallServiceWithResponse(ctx, domain, service, serviceData)
	return err
}

// CallServiceWithResponse executes a Home Assistant service call and returns the resulting entity states.
func (p *HomeAssistantProvider) CallServiceWithResponse(ctx context.Context, domain, service string, serviceData map[string]any) ([]EntityState, error) {
	domain = strings.TrimSpace(domain)
	service = strings.TrimSpace(service)
	if domain == "" || service == "" {
		return nil, fmt.Errorf("%w: domain and service must not be empty", ErrActionValidation)
	}
	if !IsValidIdentifier(domain) {
		return nil, fmt.Errorf("%w: invalid domain %q: must match ^[a-z0-9_]+$", ErrActionValidation, domain)
	}
	if !IsValidIdentifier(service) {
		return nil, fmt.Errorf("%w: invalid service %q: must match ^[a-z0-9_]+$", ErrActionValidation, service)
	}
	if !IsSupportedDomain(domain) {
		return nil, fmt.Errorf("%w: unsupported domain: %q", ErrActionValidation, domain)
	}

	p.mu.RLock()
	baseURL := p.baseURL
	token := p.token
	client := p.client
	p.mu.RUnlock()

	if baseURL == "" {
		return nil, fmt.Errorf("%w: home-assistant provider base URL not configured", ErrUpstreamUnavailable)
	}
	if client == nil {
		client = http.DefaultClient
	}

	cleanBase := strings.TrimRight(baseURL, "/")
	reqURL := fmt.Sprintf("%s/api/services/%s/%s", cleanBase, url.PathEscape(domain), url.PathEscape(service))

	var bodyReader io.Reader
	if serviceData != nil {
		bodyBytes, err := json.Marshal(serviceData)
		if err != nil {
			return nil, fmt.Errorf("%w: failed to marshal service data: %w", ErrActionValidation, err)
		}
		bodyReader = bytes.NewReader(bodyBytes)
	} else {
		bodyReader = bytes.NewReader([]byte("{}"))
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, reqURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("%w: failed to create service request: %w", ErrActionValidation, err)
	}

	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: service call failed: %w", ErrUpstreamUnavailable, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		respBody, readErr := io.ReadAll(resp.Body)
		if readErr != nil {
			return nil, formatUpstreamStatusError("service call", resp.StatusCode, "")
		}
		return nil, formatUpstreamStatusError("service call", resp.StatusCode, string(respBody))
	}

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil || len(bodyBytes) == 0 {
		return nil, nil
	}

	var updatedStates []EntityState
	if err := json.Unmarshal(bodyBytes, &updatedStates); err == nil {
		for i := range updatedStates {
			updatedStates[i] = normalizeEntityState(updatedStates[i], cleanBase)
		}
		return updatedStates, nil
	}

	return nil, nil
}

func formatUpstreamStatusError(prefix string, statusCode int, body string) error {
	body = strings.TrimSpace(body)
	switch statusCode {
	case http.StatusUnauthorized, http.StatusForbidden:
		if body != "" {
			return fmt.Errorf("%w: %w: %s returned status %d: %s", ErrUpstreamStatus, ErrUpstreamUnauthorized, prefix, statusCode, body)
		}
		return fmt.Errorf("%w: %w: %s returned status %d", ErrUpstreamStatus, ErrUpstreamUnauthorized, prefix, statusCode)
	case http.StatusNotFound:
		if body != "" {
			return fmt.Errorf("%w: %w: %s returned status %d: %s", ErrUpstreamStatus, ErrUpstreamNotFound, prefix, statusCode, body)
		}
		return fmt.Errorf("%w: %w: %s returned status %d", ErrUpstreamStatus, ErrUpstreamNotFound, prefix, statusCode)
	default:
		if body != "" {
			return fmt.Errorf("%w: %s returned status %d: %s", ErrUpstreamStatus, prefix, statusCode, body)
		}
		return fmt.Errorf("%w: %s returned status %d", ErrUpstreamStatus, prefix, statusCode)
	}
}

// FetchLovelaceConfig retrieves the Lovelace configuration for the given dashboard (or default).
func (p *HomeAssistantProvider) FetchLovelaceConfig(ctx context.Context, dashboard string) (*LovelaceConfig, error) {
	p.mu.RLock()
	baseURL := p.baseURL
	token := p.token
	client := p.client
	p.mu.RUnlock()

	if baseURL == "" {
		return nil, errors.New("home-assistant provider base URL not configured")
	}
	if client == nil {
		client = http.DefaultClient
	}

	dashboard = strings.TrimSpace(dashboard)
	var endpoint string
	if dashboard == "" || dashboard == "lovelace" || dashboard == "default" {
		endpoint = "/api/lovelace/config"
	} else {
		endpoint = "/api/lovelace/config/" + url.PathEscape(dashboard)
	}

	reqURL := strings.TrimRight(baseURL, "/") + endpoint
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create lovelace config request: %w", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("lovelace config request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound && endpoint != "/api/lovelace/config" {
		// Fallback to default Lovelace config if custom dashboard is not found
		return p.FetchLovelaceConfig(ctx, "")
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, readErr := io.ReadAll(resp.Body)
		if readErr != nil {
			return nil, fmt.Errorf("lovelace config request returned status %d", resp.StatusCode)
		}
		return nil, fmt.Errorf("lovelace config request returned status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var cfg LovelaceConfig
	if err := json.NewDecoder(resp.Body).Decode(&cfg); err != nil {
		return nil, fmt.Errorf("failed to decode lovelace config: %w", err)
	}

	return &cfg, nil
}

// HydrateStates retrieves entity states across supported or configured domains via /api/states.
func (p *HomeAssistantProvider) HydrateStates(ctx context.Context) (map[string]EntityState, error) {
	p.mu.RLock()
	baseURL := p.baseURL
	token := p.token
	client := p.client
	cfg := p.cfg
	p.mu.RUnlock()

	if baseURL == "" {
		return nil, errors.New("home-assistant provider base URL not configured")
	}
	if client == nil {
		client = http.DefaultClient
	}

	cleanBase := strings.TrimRight(baseURL, "/")
	reqURL := cleanBase + "/api/states"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create states request: %w", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("states request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, readErr := io.ReadAll(resp.Body)
		if readErr != nil {
			return nil, fmt.Errorf("states request returned status %d", resp.StatusCode)
		}
		return nil, fmt.Errorf("states request returned status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var rawStates []EntityState
	if err := json.NewDecoder(resp.Body).Decode(&rawStates); err != nil {
		return nil, fmt.Errorf("failed to decode states: %w", err)
	}

	allowedDomains := cfg.Domains
	if len(allowedDomains) == 0 {
		allowedDomains = SupportedDomains
	}

	domainSet := make(map[string]struct{}, len(allowedDomains))
	for _, d := range allowedDomains {
		domainSet[strings.ToLower(strings.TrimSpace(d))] = struct{}{}
	}

	result := make(map[string]EntityState, len(rawStates))
	for _, s := range rawStates {
		dom := strings.ToLower(s.Domain())
		if _, ok := domainSet[dom]; ok {
			result[s.EntityID] = normalizeEntityState(s, cleanBase)
		}
	}

	return result, nil
}

// DiscoverLayout processes LovelaceConfig and returns the structured LovelaceLayout for the requested view.
func (p *HomeAssistantProvider) DiscoverLayout(lovelaceConfig *LovelaceConfig, viewSelector string) *LovelaceLayout {
	if lovelaceConfig == nil {
		return &LovelaceLayout{}
	}

	p.mu.RLock()
	cleanBase := strings.TrimRight(p.baseURL, "/")
	p.mu.RUnlock()

	layout := &LovelaceLayout{
		Title: lovelaceConfig.Title,
		Views: lovelaceConfig.Views,
	}

	if len(lovelaceConfig.Views) == 0 {
		return layout
	}

	selectedView := selectView(lovelaceConfig.Views, viewSelector)
	if cleanBase != "" {
		selectedView = normalizeViewImages(selectedView, cleanBase)
	}
	layout.View = selectedView

	// Extract cards from view
	cards := make([]LovelaceCard, 0)
	if len(selectedView.Cards) > 0 {
		cards = append(cards, selectedView.Cards...)
	}

	// If sections view, collect cards from sections as well
	if len(selectedView.Sections) > 0 {
		for _, sec := range selectedView.Sections {
			cards = append(cards, sec.Cards...)
		}
	}

	layout.Cards = cards

	// Discover all referenced entities
	layout.DiscoveredEntities = ExtractEntitiesFromView(selectedView)
	return layout
}

func normalizeEntityState(s EntityState, cleanBase string) EntityState {
	if cleanBase != "" && s.Attributes != nil {
		if pic, ok := s.Attributes["entity_picture"].(string); ok {
			trimmed := strings.TrimSpace(pic)
			if strings.HasPrefix(trimmed, "/") {
				s.Attributes["entity_picture"] = cleanBase + trimmed
			}
		}
	}
	return s
}

func normalizeViewImages(v LovelaceView, cleanBase string) LovelaceView {
	if len(v.Cards) > 0 {
		cards := make([]LovelaceCard, len(v.Cards))
		for i, c := range v.Cards {
			cards[i] = normalizeCardImage(c, cleanBase)
		}
		v.Cards = cards
	}
	if len(v.Sections) > 0 {
		sections := make([]LovelaceSection, len(v.Sections))
		for i, sec := range v.Sections {
			sections[i] = sec
			if len(sec.Cards) > 0 {
				sCards := make([]LovelaceCard, len(sec.Cards))
				for j, c := range sec.Cards {
					sCards[j] = normalizeCardImage(c, cleanBase)
				}
				sections[i].Cards = sCards
			}
		}
		v.Sections = sections
	}
	return v
}

func normalizeCardImage(card LovelaceCard, cleanBase string) LovelaceCard {
	if cleanBase != "" {
		trimmed := strings.TrimSpace(card.Image)
		if strings.HasPrefix(trimmed, "/") {
			card.Image = cleanBase + trimmed
		}
	}
	if len(card.Cards) > 0 {
		cards := make([]LovelaceCard, len(card.Cards))
		for i, c := range card.Cards {
			cards[i] = normalizeCardImage(c, cleanBase)
		}
		card.Cards = cards
	}
	if len(card.Sections) > 0 {
		sections := make([]LovelaceSection, len(card.Sections))
		for i, sec := range card.Sections {
			sections[i] = sec
			if len(sec.Cards) > 0 {
				sCards := make([]LovelaceCard, len(sec.Cards))
				for j, c := range sec.Cards {
					sCards[j] = normalizeCardImage(c, cleanBase)
				}
				sections[i].Cards = sCards
			}
		}
		card.Sections = sections
	}
	return card
}

// ExtractEntitiesFromCard extracts all referenced entity IDs from a card (including nested cards and sections).
func ExtractEntitiesFromCard(card LovelaceCard) []string {
	var res []string

	if card.Entity != "" {
		res = append(res, strings.TrimSpace(card.Entity))
	}
	if card.CameraImage != "" && strings.Contains(card.CameraImage, ".") {
		res = append(res, strings.TrimSpace(card.CameraImage))
	}

	for _, item := range card.Entities {
		switch v := item.(type) {
		case string:
			if s := strings.TrimSpace(v); s != "" {
				res = append(res, s)
			}
		case map[string]any:
			if ent, ok := v["entity"].(string); ok && strings.TrimSpace(ent) != "" {
				res = append(res, strings.TrimSpace(ent))
			}
		}
	}

	for _, child := range card.Cards {
		res = append(res, ExtractEntitiesFromCard(child)...)
	}

	for _, sec := range card.Sections {
		for _, child := range sec.Cards {
			res = append(res, ExtractEntitiesFromCard(child)...)
		}
	}

	return deduplicateStrings(res)
}

// ExtractEntitiesFromView extracts all referenced entity IDs from a LovelaceView.
func ExtractEntitiesFromView(view LovelaceView) []string {
	var res []string
	for _, card := range view.Cards {
		res = append(res, ExtractEntitiesFromCard(card)...)
	}
	for _, sec := range view.Sections {
		for _, card := range sec.Cards {
			res = append(res, ExtractEntitiesFromCard(card)...)
		}
	}
	return deduplicateStrings(res)
}

func selectView(views []LovelaceView, selector string) LovelaceView {
	if len(views) == 0 {
		return LovelaceView{}
	}
	selector = strings.TrimSpace(selector)
	if selector == "" {
		return views[0]
	}

	// Try index
	if idx, err := strconv.Atoi(selector); err == nil {
		if idx >= 0 && idx < len(views) {
			return views[idx]
		}
	}

	// Try path
	for _, v := range views {
		if strings.EqualFold(v.Path, selector) {
			return v
		}
	}

	// Try title
	for _, v := range views {
		if strings.EqualFold(v.Title, selector) {
			return v
		}
	}

	// Fallback to first view
	return views[0]
}

func (p *HomeAssistantProvider) generateFallbackCards(states map[string]EntityState, explicitEntities []string) []LovelaceCard {
	cards := make([]LovelaceCard, 0)

	if len(explicitEntities) > 0 {
		for _, entityID := range explicitEntities {
			entityID = strings.TrimSpace(entityID)
			if entityID == "" {
				continue
			}
			parts := strings.SplitN(entityID, ".", 2)
			dom := ""
			if len(parts) > 0 {
				dom = parts[0]
			}
			cards = append(cards, fallbackCardForDomain(dom, entityID))
		}
		return cards
	}

	// Generate sorted list of entity cards from hydrated states
	keys := make([]string, 0, len(states))
	for k := range states {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, entityID := range keys {
		s := states[entityID]
		cards = append(cards, fallbackCardForDomain(s.Domain(), entityID))
	}

	return cards
}

func fallbackCardForDomain(dom, entityID string) LovelaceCard {
	switch dom {
	case "climate":
		return LovelaceCard{Type: "thermostat", Entity: entityID}
	case "camera":
		return LovelaceCard{Type: "picture-entity", Entity: entityID}
	case "scene", "script":
		return LovelaceCard{Type: "button", Entity: entityID}
	default:
		return LovelaceCard{Type: "tile", Entity: entityID}
	}
}

func resolveToken(config map[string]any, opts InitOptions, fallback string) string {
	var envKey string
	if v, ok := config["token_env"].(string); ok {
		envKey = v
	}
	if envKey = strings.TrimSpace(envKey); envKey != "" {
		if val := strings.TrimSpace(opts.GetSecret(envKey)); val != "" {
			return val
		}
		if val := strings.TrimSpace(os.Getenv(envKey)); val != "" {
			return val
		}
	}
	if val := strings.TrimSpace(opts.Token); val != "" {
		return val
	}
	if val := strings.TrimSpace(opts.GetSecret("token")); val != "" {
		return val
	}
	if val := strings.TrimSpace(opts.GetSecret("ha.token")); val != "" {
		return val
	}
	return strings.TrimSpace(fallback)
}

func resolveConfigField(config map[string]any, opts InitOptions, fields ...string) string {
	for _, f := range fields {
		if raw, ok := config[f].(string); ok && strings.TrimSpace(raw) != "" {
			return strings.TrimSpace(raw)
		}
		if envKey, ok := config[f+"_env"].(string); ok && strings.TrimSpace(envKey) != "" {
			envKey = strings.TrimSpace(envKey)
			if val := opts.GetSecret(envKey); val != "" {
				return strings.TrimSpace(val)
			}
			if val := os.Getenv(envKey); val != "" {
				return strings.TrimSpace(val)
			}
		}
		if val := opts.GetSecret(f); val != "" {
			return strings.TrimSpace(val)
		}
		if val := opts.GetSecret("ha." + f); val != "" {
			return strings.TrimSpace(val)
		}
	}
	return ""
}

func toStringSlice(val any) []string {
	if val == nil {
		return nil
	}
	switch v := val.(type) {
	case []string:
		return v
	case []any:
		res := make([]string, 0, len(v))
		for _, item := range v {
			if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
				res = append(res, strings.TrimSpace(s))
			}
		}
		return res
	case string:
		parts := strings.Split(v, ",")
		res := make([]string, 0, len(parts))
		for _, p := range parts {
			if s := strings.TrimSpace(p); s != "" {
				res = append(res, s)
			}
		}
		return res
	default:
		return nil
	}
}

func deduplicateStrings(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, s := range in {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		if _, exists := seen[s]; !exists {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	return out
}
