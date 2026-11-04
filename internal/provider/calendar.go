package provider

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	ical "github.com/arran4/golang-ical"
	"github.com/teambition/rrule-go"
)

const (
	defaultCalendarTimeout = 10 * time.Second
	maxCalendarFeedBytes   = 2 * 1024 * 1024 // 2MB Slowloris defense ceiling
)

// CalendarEvent represents a single normalized calendar event.
type CalendarEvent struct {
	ID           string `json:"id"`
	CalendarName string `json:"calendar_name"`
	Color        string `json:"color"`
	Title        string `json:"title"`
	Start        string `json:"start"`
	End          string `json:"end"`
	AllDay       bool   `json:"all_day"`
	Location     string `json:"location"`
	Description  string `json:"description"`
}

// CalendarSnapshot represents the normalized calendar provider payload.
type CalendarSnapshot struct {
	LastSync   string          `json:"last_sync"`
	SyncStatus string          `json:"sync_status"`
	Events     []CalendarEvent `json:"events"`
}

// CalendarSource defines configuration for a single upstream calendar feed.
type CalendarSource struct {
	Name           string `json:"name" yaml:"name"`
	Type           string `json:"type,omitempty" yaml:"type,omitempty"` // "ical" (default) or "caldav"
	URL            string `json:"url,omitempty" yaml:"url,omitempty"`
	URLEnv         string `json:"url_env,omitempty" yaml:"url_env,omitempty"`
	Color          string `json:"color,omitempty" yaml:"color,omitempty"`
	Enabled        bool   `json:"enabled" yaml:"enabled"`
	StripRecurring bool   `json:"strip_recurring,omitempty" yaml:"strip_recurring,omitempty"`
	Username       string `json:"username,omitempty" yaml:"username,omitempty"`
	UsernameEnv    string `json:"username_env,omitempty" yaml:"username_env,omitempty"`
	Password       string `json:"password,omitempty" yaml:"password,omitempty"`
	PasswordEnv    string `json:"password_env,omitempty" yaml:"password_env,omitempty"`
	Token          string `json:"token,omitempty" yaml:"token,omitempty"`
	TokenEnv       string `json:"token_env,omitempty" yaml:"token_env,omitempty"`
}

const (
	// DefaultWindowDaysPast is the default number of days in the past to fetch calendar events.
	DefaultWindowDaysPast = 90
	// DefaultWindowDaysFuture is the default number of days into the future to fetch calendar events.
	DefaultWindowDaysFuture = 180
)

// CalendarConfig defines the full instance configuration for calendar providers.
type CalendarConfig struct {
	WindowDaysPast   int              `json:"window_days_past,omitempty" yaml:"window_days_past,omitempty"`
	WindowDaysFuture int              `json:"window_days_future,omitempty" yaml:"window_days_future,omitempty"`
	View             string           `json:"view" yaml:"view"`
	StripRecurring   bool             `json:"strip_recurring,omitempty" yaml:"strip_recurring,omitempty"`
	Calendars        []CalendarSource `json:"calendars" yaml:"calendars"`
}

// CalendarProvider fetches, parses, expands recurrence rules, and normalizes iCal feeds.
type CalendarProvider struct {
	client       *http.Client
	config       *CalendarConfig
	location     *time.Location
	nowFunc      func() time.Time
	logger       *slog.Logger
	caldavClient *CalDAVClient
}

func init() {
	registerBuiltin("calendar-feed", NewCalendarProvider)
}

// NewCalendarProvider constructs a standard CalendarProvider.
func NewCalendarProvider() Provider {
	return NewCalendarProviderWithClient(nil, nil)
}

// NewCalendarProviderWithClient constructs a CalendarProvider with custom HTTP client and clock.
func NewCalendarProviderWithClient(client *http.Client, nowFunc func() time.Time) *CalendarProvider {
	if client == nil {
		client = &http.Client{Timeout: defaultCalendarTimeout}
	}
	if nowFunc == nil {
		nowFunc = time.Now
	}
	logger := slog.Default()
	return &CalendarProvider{
		client:       client,
		location:     time.UTC,
		nowFunc:      nowFunc,
		logger:       logger,
		caldavClient: NewCalDAVClient(client, logger),
	}
}

// Init initializes the calendar provider from configuration.
func (p *CalendarProvider) Init(ctx context.Context, rawConfig map[string]any, opts InitOptions) error {
	cfg, err := parseCalendarConfig(rawConfig, opts)
	if err != nil {
		return fmt.Errorf("calendar provider init failed: %w", err)
	}
	p.config = cfg

	tzStr := opts.Timezone
	if tz, ok := rawConfig["timezone"].(string); ok && strings.TrimSpace(tz) != "" {
		tzStr = strings.TrimSpace(tz)
	}
	if tzStr != "" {
		if loc, err := time.LoadLocation(tzStr); err == nil {
			p.location = loc
		} else {
			p.logger.Warn("invalid timezone in calendar provider, falling back to UTC", "timezone", tzStr, "error", err)
			p.location = time.UTC
		}
	}
	if p.location == nil {
		p.location = time.UTC
	}

	return nil
}

func resolveCalendarField(m map[string]any, opts InitOptions, idx int, name, field string) (string, string, error) {
	var rawVal, envKey string
	if v, ok := m[field].(string); ok {
		rawVal = strings.TrimSpace(v)
	}
	if e, ok := m[field+"_env"].(string); ok {
		envKey = strings.TrimSpace(e)
	}
	if rawVal != "" && envKey != "" {
		return "", "", fmt.Errorf("calendar %q cannot specify both '%s' and '%s_env'", name, field, field)
	}
	resolved := rawVal
	if envKey != "" {
		resolved = opts.GetSecret(fmt.Sprintf("calendars[%d].%s", idx, field))
		if resolved == "" {
			resolved = opts.GetSecret(envKey)
		}
		if resolved == "" {
			resolved = os.Getenv(envKey)
		}
		if resolved == "" {
			return "", "", fmt.Errorf("calendar %q: environment variable %q is not set or empty", name, envKey)
		}
	}
	return resolved, envKey, nil
}

func extractRawCalendarSources(raw map[string]any) ([]map[string]any, error) {
	var rawSources []map[string]any
	switch list := raw["calendars"].(type) {
	case []any:
		for idx, item := range list {
			m, ok := item.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("calendar source at index %d is not a map", idx)
			}
			rawSources = append(rawSources, m)
		}
	case []map[string]any:
		rawSources = list
	}

	if len(rawSources) == 0 {
		return nil, errors.New("calendar provider requires at least one calendar in 'calendars'")
	}
	return rawSources, nil
}

func validateCalendarSourceAuth(src CalendarSource) error {
	// Auth exclusivity: Cannot specify both Basic Auth and Bearer Token
	if (src.Username != "" || src.Password != "") && src.Token != "" {
		return fmt.Errorf("calendar %q cannot specify both Basic auth (username/password) and Bearer auth (token)", src.Name)
	}

	// Basic auth completeness
	if src.Username != "" && src.Password == "" {
		return fmt.Errorf("calendar %q: username provided without password", src.Name)
	}
	if src.Password != "" && src.Username == "" {
		return fmt.Errorf("calendar %q: password provided without username", src.Name)
	}
	return nil
}

func parseCalendarSource(m map[string]any, opts InitOptions, idx int, defaultStripRecurring bool) (CalendarSource, error) {
	name, ok := m["name"].(string)
	if !ok || strings.TrimSpace(name) == "" {
		return CalendarSource{}, fmt.Errorf("calendar at index %d requires non-empty 'name'", idx)
	}
	name = strings.TrimSpace(name)

	var rawType string
	if t, ok := m["type"].(string); ok {
		rawType = t
	}
	sourceType := strings.ToLower(strings.TrimSpace(rawType))
	if sourceType == "" {
		sourceType = "ical"
	}
	if sourceType != "ical" && sourceType != "caldav" {
		return CalendarSource{}, fmt.Errorf("calendar %q: invalid type %q (must be 'ical' or 'caldav')", name, sourceType)
	}

	resolvedURL, urlEnv, err := resolveCalendarField(m, opts, idx, name, "url")
	if err != nil {
		return CalendarSource{}, err
	}
	if resolvedURL == "" && urlEnv == "" {
		return CalendarSource{}, fmt.Errorf("calendar %q requires either 'url' or 'url_env'", name)
	}

	resolvedUsername, usernameEnv, err := resolveCalendarField(m, opts, idx, name, "username")
	if err != nil {
		return CalendarSource{}, err
	}

	resolvedPassword, passwordEnv, err := resolveCalendarField(m, opts, idx, name, "password")
	if err != nil {
		return CalendarSource{}, err
	}

	resolvedToken, tokenEnv, err := resolveCalendarField(m, opts, idx, name, "token")
	if err != nil {
		return CalendarSource{}, err
	}

	color := "#3b82f6"
	if c, ok := m["color"].(string); ok && strings.TrimSpace(c) != "" {
		color = strings.TrimSpace(c)
	}

	enabled := true
	if e, ok := m["enabled"].(bool); ok {
		enabled = e
	}

	stripRecurring := defaultStripRecurring
	if sr, ok := m["strip_recurring"].(bool); ok {
		stripRecurring = sr
	} else if er, ok := m["exclude_recurring"].(bool); ok {
		stripRecurring = er
	}

	src := CalendarSource{
		Name:           name,
		Type:           sourceType,
		URL:            resolvedURL,
		URLEnv:         urlEnv,
		Color:          color,
		Enabled:        enabled,
		StripRecurring: stripRecurring,
		Username:       resolvedUsername,
		UsernameEnv:    usernameEnv,
		Password:       resolvedPassword,
		PasswordEnv:    passwordEnv,
		Token:          resolvedToken,
		TokenEnv:       tokenEnv,
	}

	if err := validateCalendarSourceAuth(src); err != nil {
		return CalendarSource{}, err
	}

	return src, nil
}

func parseCalendarConfig(raw map[string]any, opts InitOptions) (*CalendarConfig, error) {
	cfg := &CalendarConfig{
		WindowDaysPast:   DefaultWindowDaysPast,
		WindowDaysFuture: DefaultWindowDaysFuture,
		View:             "agenda",
	}

	if v, ok := raw["view"].(string); ok && strings.TrimSpace(v) != "" {
		cfg.View = strings.TrimSpace(v)
	}

	if sr, ok := raw["strip_recurring"].(bool); ok {
		cfg.StripRecurring = sr
	} else if er, ok := raw["exclude_recurring"].(bool); ok {
		cfg.StripRecurring = er
	}

	rawSources, err := extractRawCalendarSources(raw)
	if err != nil {
		return nil, err
	}

	for idx, m := range rawSources {
		src, err := parseCalendarSource(m, opts, idx, cfg.StripRecurring)
		if err != nil {
			return nil, err
		}
		cfg.Calendars = append(cfg.Calendars, src)
	}

	return cfg, nil
}

// Fetch retrieves and parses iCal feeds across all configured calendars in parallel.
func (p *CalendarProvider) Fetch(ctx context.Context) (any, error) {
	if p.config == nil {
		return nil, errors.New("calendar provider not initialized")
	}

	now := p.nowFunc()
	pastDays := p.config.WindowDaysPast
	if pastDays <= 0 {
		pastDays = DefaultWindowDaysPast
	}
	futureDays := p.config.WindowDaysFuture
	if futureDays <= 0 {
		futureDays = DefaultWindowDaysFuture
	}
	windowStart := now.AddDate(0, 0, -pastDays)
	windowEnd := now.AddDate(0, 0, futureDays)

	var enabledSources []CalendarSource
	for _, source := range p.config.Calendars {
		if source.Enabled {
			enabledSources = append(enabledSources, source)
		}
	}

	type fetchResult struct {
		source CalendarSource
		events []CalendarEvent
		err    error
	}

	results := make([]fetchResult, len(enabledSources))
	var wg sync.WaitGroup
	for i, src := range enabledSources {
		wg.Add(1)
		go func(idx int, s CalendarSource) {
			defer wg.Done()
			events, err := p.fetchCalendar(ctx, s, windowStart, windowEnd)
			results[idx] = fetchResult{source: s, events: events, err: err}
		}(i, src)
	}
	wg.Wait()

	var allEvents []CalendarEvent
	hasSuccess := false
	var fetchErrors []string

	for _, r := range results {
		if r.err != nil {
			p.logger.Warn("calendar feed fetch failed", "calendar", r.source.Name, "error", r.err)
			fetchErrors = append(fetchErrors, fmt.Sprintf("%s: %v", r.source.Name, r.err))
			continue
		}
		hasSuccess = true
		allEvents = append(allEvents, r.events...)
	}

	if !hasSuccess && len(fetchErrors) > 0 {
		return nil, fmt.Errorf("all calendar feeds failed: %s", strings.Join(fetchErrors, "; "))
	}

	// Sort chronologically ascending in effective timezone
	sortCalendarEvents(allEvents, p.location)

	syncStatus := "ok"
	if len(fetchErrors) > 0 {
		syncStatus = "partial"
	}

	return CalendarSnapshot{
		LastSync:   now.UTC().Format(time.RFC3339),
		SyncStatus: syncStatus,
		Events:     allEvents,
	}, nil
}

func (p *CalendarProvider) fetchCalendar(ctx context.Context, source CalendarSource, windowStart, windowEnd time.Time) ([]CalendarEvent, error) {
	if source.Type == "caldav" {
		return p.caldavClient.FetchCalendarEvents(ctx, source, windowStart, windowEnd)
	}

	targetURL := source.URL
	if after, ok := strings.CutPrefix(targetURL, "webcal://"); ok {
		targetURL = "https://" + after
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return nil, fmt.Errorf("invalid request URL: %w", err)
	}
	req.Header.Set("User-Agent", "Mirrormere-Calendar/1.0")
	req.Header.Set("Accept", "text/calendar, application/ics, text/plain")

	resp, err := p.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("http status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxCalendarFeedBytes+1))
	if err != nil {
		return nil, fmt.Errorf("reading response body: %w", err)
	}
	if len(body) > maxCalendarFeedBytes {
		return nil, fmt.Errorf("calendar feed exceeds size limit of %d bytes", maxCalendarFeedBytes)
	}

	cal, err := ical.ParseCalendar(bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("parsing ics calendar: %w", err)
	}

	return parseCalendarEvents(cal, source, windowStart, windowEnd), nil
}

func addRecurrencePropertyKeys(dst map[string]bool, prop *ical.IANAProperty) {
	for raw := range strings.SplitSeq(prop.Value, ",") {
		v := strings.TrimSpace(raw)
		if v == "" {
			continue
		}
		dst[v] = true
		itemProp := &ical.IANAProperty{
			IANAToken:      prop.IANAToken,
			Value:          v,
			ICalParameters: prop.ICalParameters,
		}
		if t, err := parseICalDateTime(itemProp, len(v) == 8); err == nil {
			dst[t.UTC().Format("20060102T150405Z")] = true
			if len(v) == 8 {
				dst[t.Format("20060102")] = true
			}
		}
	}
}

func collectRecurrenceOverrides(events []*ical.VEvent) map[string]map[string]bool {
	recurrenceOverrides := make(map[string]map[string]bool)
	for _, e := range events {
		uidProp := e.GetProperty(ical.ComponentPropertyUniqueId)
		recIDProp := e.GetProperty(ical.ComponentPropertyRecurrenceId)
		if uidProp != nil && recIDProp != nil {
			uid := uidProp.Value
			if recurrenceOverrides[uid] == nil {
				recurrenceOverrides[uid] = make(map[string]bool)
			}
			addRecurrencePropertyKeys(recurrenceOverrides[uid], recIDProp)
		}
	}
	return recurrenceOverrides
}

func isAllDayProperty(dtStartProp *ical.IANAProperty) bool {
	if dtStartProp == nil {
		return false
	}
	if valParam, ok := dtStartProp.ICalParameters["VALUE"]; ok {
		for _, v := range valParam {
			if strings.ToUpper(v) == "DATE" {
				return true
			}
		}
	}
	return len(dtStartProp.Value) == 8
}

func parseCalendarEvents(cal *ical.Calendar, source CalendarSource, windowStart, windowEnd time.Time) []CalendarEvent {
	var events []CalendarEvent
	recurrenceOverrides := collectRecurrenceOverrides(cal.Events())

	// Pass 2: Parse events and expand recurrences
	for _, e := range cal.Events() {
		evs := expandAndClipEvent(e, source, windowStart, windowEnd, recurrenceOverrides)
		events = append(events, evs...)
	}

	return events
}

func expandAndClipEvent(e *ical.VEvent, source CalendarSource, windowStart, windowEnd time.Time, recurrenceOverrides map[string]map[string]bool) []CalendarEvent {
	dtStartProp := e.GetProperty(ical.ComponentPropertyDtStart)
	if dtStartProp == nil {
		return nil
	}

	isAllDay := isAllDayProperty(dtStartProp)

	rruleProp := e.GetProperty(ical.ComponentPropertyRrule)
	recIDProp := e.GetProperty(ical.ComponentPropertyRecurrenceId)
	rdateProp := e.GetProperty(ical.ComponentProperty("RDATE"))

	isRecurring := rruleProp != nil || recIDProp != nil || rdateProp != nil
	if source.StripRecurring && isRecurring {
		return nil
	}

	uid := ""
	if p := e.GetProperty(ical.ComponentPropertyUniqueId); p != nil {
		uid = p.Value
	}

	title := "Untitled Event"
	if p := e.GetProperty(ical.ComponentPropertySummary); p != nil && strings.TrimSpace(p.Value) != "" {
		title = strings.TrimSpace(p.Value)
	}

	location := ""
	if p := e.GetProperty(ical.ComponentPropertyLocation); p != nil {
		location = strings.TrimSpace(p.Value)
	}

	description := ""
	if p := e.GetProperty(ical.ComponentPropertyDescription); p != nil {
		description = strings.TrimSpace(p.Value)
	}

	if rruleProp != nil && recIDProp == nil {
		return expandRecurrence(e, source, uid, title, location, description, isAllDay, windowStart, windowEnd, recurrenceOverrides[uid])
	}

	ev, ok := parseSingleEvent(e, source, uid, title, location, description, isAllDay, windowStart, windowEnd)
	if ok {
		return []CalendarEvent{ev}
	}
	return nil
}

func eventDuration(e *ical.VEvent, startTime time.Time, isAllDay bool) time.Duration {
	var duration time.Duration
	if dtEndProp := e.GetProperty(ical.ComponentPropertyDtEnd); dtEndProp != nil {
		endTime, err := parseICalDateTime(dtEndProp, isAllDay)
		if err == nil && endTime.After(startTime) {
			duration = endTime.Sub(startTime)
		}
	} else if p := e.GetProperty(ical.ComponentPropertyDuration); p != nil {
		dur := parseICalDuration(p.Value)
		if dur > 0 {
			duration = dur
		}
	}
	if duration <= 0 {
		if isAllDay {
			duration = 24 * time.Hour
		} else {
			duration = time.Hour
		}
	}
	return duration
}

func parseSingleEvent(e *ical.VEvent, source CalendarSource, uid, title, location, description string, isAllDay bool, windowStart, windowEnd time.Time) (CalendarEvent, bool) {
	dtStartProp := e.GetProperty(ical.ComponentPropertyDtStart)
	startTime, err := parseICalDateTime(dtStartProp, isAllDay)
	if err != nil {
		return CalendarEvent{}, false
	}

	endTime := startTime.Add(eventDuration(e, startTime, isAllDay))

	// Check window intersection: [start, end] intersects [windowStart, windowEnd]
	if endTime.Before(windowStart) || startTime.After(windowEnd) {
		return CalendarEvent{}, false
	}

	startStr, endStr := formatEventRange(startTime, endTime, isAllDay)
	eventID := fmt.Sprintf("evt_%s_%d", sanitizeID(uid), startTime.Unix())

	return CalendarEvent{
		ID:           eventID,
		CalendarName: source.Name,
		Color:        source.Color,
		Title:        title,
		Start:        startStr,
		End:          endStr,
		AllDay:       isAllDay,
		Location:     location,
		Description:  description,
	}, true
}

func expandRecurringEvent(rruleProp *ical.IANAProperty, base CalendarEvent, start time.Time, duration time.Duration, winStart, winEnd time.Time, excluded map[string]bool) []CalendarEvent {
	if rruleProp == nil {
		return nil
	}
	ruleStr := rruleProp.Value
	upperRule := strings.ToUpper(ruleStr)
	if strings.Contains(upperRule, "FREQ=SECONDLY") || strings.Contains(upperRule, "FREQ=MINUTELY") {
		return nil
	}

	rule, err := rrule.StrToRRule(ruleStr)
	if err != nil {
		return nil
	}

	rule.DTStart(start)
	occurrences := rule.Between(winStart.Add(-duration), winEnd, true)
	const maxOccurrences = 500
	if len(occurrences) > maxOccurrences {
		occurrences = occurrences[:maxOccurrences]
	}

	var expanded []CalendarEvent
	for _, occ := range occurrences {
		occStr := occ.UTC().Format("20060102T150405Z")
		occDateStr := occ.Format("20060102")
		if excluded[occStr] || excluded[occDateStr] {
			continue
		}

		occEnd := occ.Add(duration)
		if occEnd.Before(winStart) || occ.After(winEnd) {
			continue
		}

		startStr, endStr := formatEventRange(occ, occEnd, base.AllDay)
		ev := base
		ev.ID = fmt.Sprintf("evt_%s_%d", sanitizeID(base.ID), occ.Unix())
		ev.Start = startStr
		ev.End = endStr
		expanded = append(expanded, ev)
	}

	return expanded
}

func expandRecurrence(e *ical.VEvent, source CalendarSource, uid, title, location, description string, isAllDay bool, windowStart, windowEnd time.Time, overrides map[string]bool) []CalendarEvent {
	dtStartProp := e.GetProperty(ical.ComponentPropertyDtStart)
	rruleProp := e.GetProperty(ical.ComponentPropertyRrule)

	startTime, err := parseICalDateTime(dtStartProp, isAllDay)
	if err != nil {
		return nil
	}

	duration := eventDuration(e, startTime, isAllDay)

	// Collect EXDATE exclusions
	excluded := make(map[string]bool)
	for _, prop := range e.Properties {
		if prop.IANAToken == string(ical.ComponentPropertyExdate) {
			addRecurrencePropertyKeys(excluded, &prop)
		}
	}
	for k, v := range overrides {
		if v {
			excluded[k] = true
		}
	}

	base := CalendarEvent{
		ID:           uid,
		CalendarName: source.Name,
		Color:        source.Color,
		Title:        title,
		AllDay:       isAllDay,
		Location:     location,
		Description:  description,
	}

	return expandRecurringEvent(rruleProp, base, startTime, duration, windowStart, windowEnd, excluded)
}

func parseICalDateTime(prop *ical.IANAProperty, isAllDay bool) (time.Time, error) {
	if prop == nil {
		return time.Time{}, errors.New("nil property")
	}
	val := strings.TrimSpace(prop.Value)

	if isAllDay || len(val) == 8 {
		t, err := time.Parse("20060102", val)
		if err == nil {
			return t, nil
		}
	}

	// Check TZID parameter
	loc := time.UTC
	if tzid, ok := prop.ICalParameters["TZID"]; ok && len(tzid) > 0 {
		if l, err := time.LoadLocation(tzid[0]); err == nil {
			loc = l
		}
	}

	// Try common iCal formats
	layouts := []string{
		"20060102T150405Z",
		"20060102T150405",
		time.RFC3339,
	}

	for _, layout := range layouts {
		if strings.HasSuffix(layout, "Z") && !strings.HasSuffix(val, "Z") {
			continue
		}
		if t, err := time.ParseInLocation(layout, val, loc); err == nil {
			return t, nil
		}
	}

	return time.Time{}, fmt.Errorf("unable to parse ical datetime %q", val)
}

func parseICalDuration(val string) time.Duration {
	// Simple ISO 8601 duration parser for P1D, PT1H, PT30M, etc.
	val = strings.TrimPrefix(val, "P")
	var dur time.Duration
	timePart := false

	var num int
	for i := range len(val) {
		ch := val[i]
		if ch == 'T' {
			timePart = true
			continue
		}
		if ch >= '0' && ch <= '9' {
			num = num*10 + int(ch-'0')
			continue
		}
		switch ch {
		case 'D':
			dur += time.Duration(num) * 24 * time.Hour
		case 'H':
			dur += time.Duration(num) * time.Hour
		case 'M':
			if timePart {
				dur += time.Duration(num) * time.Minute
			} else {
				// Months approximation if any
				dur += time.Duration(num) * 30 * 24 * time.Hour
			}
		case 'S':
			dur += time.Duration(num) * time.Second
		}
		num = 0
	}
	return dur
}

func formatEventRange(start, end time.Time, isAllDay bool) (string, string) {
	if isAllDay {
		startStr := start.Format("2006-01-02")
		// In RFC 5545, DTEND for all-day events is non-inclusive.
		// e.g. DTSTART:20260925, DTEND:20260926 is 1 day.
		// If end is greater than start, normalize display end to inclusive date.
		endInclusive := end
		if end.After(start) && end.Sub(start) >= 24*time.Hour {
			endInclusive = end.Add(-24 * time.Hour)
		}
		endStr := max(endInclusive.Format("2006-01-02"), startStr)
		return startStr, endStr
	}
	return start.Format(time.RFC3339), end.Format(time.RFC3339)
}

// SortCalendarEvents sorts events chronologically ascending taking the target timezone into account.
// Events are grouped by their calendar date in loc. For events occurring on the same local date,
// all-day events are placed first (sorted by title), followed by timed events ordered by their start time.
func SortCalendarEvents(events []CalendarEvent, loc *time.Location) {
	if loc == nil {
		loc = time.UTC
	}
	sort.SliceStable(events, func(i, j int) bool {
		a, b := events[i], events[j]
		dateA := eventLocalDate(a, loc)
		dateB := eventLocalDate(b, loc)
		if dateA != dateB {
			return dateA < dateB
		}
		if a.AllDay != b.AllDay {
			return a.AllDay // all-day events first on the same local date
		}
		if !a.AllDay && !b.AllDay {
			tA, okA := parseCalendarDateTime(a.Start)
			tB, okB := parseCalendarDateTime(b.Start)
			if okA && okB && !tA.Equal(tB) {
				return tA.Before(tB)
			}
		}
		return a.Title < b.Title
	})
}

func sortCalendarEvents(events []CalendarEvent, loc *time.Location) {
	SortCalendarEvents(events, loc)
}

func eventLocalDate(ev CalendarEvent, loc *time.Location) string {
	if ev.AllDay {
		if len(ev.Start) == 10 && ev.Start[4] == '-' && ev.Start[7] == '-' {
			return ev.Start
		}
		if t, err := time.Parse("2006-01-02", ev.Start); err == nil {
			return t.Format("2006-01-02")
		}
	}
	if t, ok := parseCalendarDateTime(ev.Start); ok {
		return t.In(loc).Format("2006-01-02")
	}
	return ev.Start
}

func parseCalendarDateTime(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false
	}
	layouts := []string{
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02T15:04:05Z",
		"2006-01-02T15:04:05",
		"20060102T150405Z",
		"20060102T150405",
		"2006-01-02",
		"20060102",
	}
	for _, layout := range layouts {
		if strings.HasSuffix(layout, "Z") && !strings.HasSuffix(s, "Z") {
			continue
		}
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

func sanitizeID(uid string) string {
	s := strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			return r
		}
		return '_'
	}, uid)
	if len(s) > 32 {
		return s[:32]
	}
	if s == "" {
		return "event"
	}
	return s
}

// Subscribe satisfies Provider interface (no-op for pull-based calendar feeds).
func (p *CalendarProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown gracefully satisfies Provider interface.
func (p *CalendarProvider) Shutdown(ctx context.Context) error {
	return nil
}
