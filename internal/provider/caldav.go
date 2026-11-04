package provider

import (
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"path"
	"strings"
	"sync"
	"time"

	ical "github.com/arran4/golang-ical"
)

// CalDAVClient manages CalDAV protocol communication (RFC 4791, RFC 3744, RFC 4918)
// including PROPFIND discovery, REPORT calendar-queries, and credential authorization.
type CalDAVClient struct {
	client         *http.Client
	logger         *slog.Logger
	discoveryCache map[string]string
	cacheMu        sync.RWMutex
}

// NewCalDAVClient constructs a thread-safe CalDAV client with connection reuse.
func NewCalDAVClient(client *http.Client, logger *slog.Logger) *CalDAVClient {
	if client == nil {
		client = &http.Client{Timeout: defaultCalendarTimeout}
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &CalDAVClient{
		client:         client,
		logger:         logger,
		discoveryCache: make(map[string]string),
	}
}

// FetchCalendarEvents executes a CalDAV REPORT time-range query and parses discovered events.
func (c *CalDAVClient) FetchCalendarEvents(ctx context.Context, source CalendarSource, windowStart, windowEnd time.Time) ([]CalendarEvent, error) {
	cacheKey := source.URL + "|" + source.Name
	targetURL, ok := c.getCachedURL(cacheKey)
	if !ok {
		discovered, err := c.DiscoverCalendarURL(ctx, source)
		if err != nil {
			return nil, fmt.Errorf("caldav discovery failed for %q: %w", source.Name, err)
		}
		targetURL = discovered
		c.setCachedURL(cacheKey, targetURL)
	}

	targetURL = ensureTrailingSlash(targetURL)

	// RFC 4791 §9.9: time-range MUST be UTC in format YYYYMMDD'T'HHMMSS'Z'
	startStr := windowStart.UTC().Format("20060102T150405Z")
	endStr := windowEnd.UTC().Format("20060102T150405Z")

	xmlBody := fmt.Sprintf(`<?xml version="1.0" encoding="utf-8" ?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag/>
    <c:calendar-data/>
  </d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="%s" end="%s"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>`, startStr, endStr)

	req, err := http.NewRequestWithContext(ctx, "REPORT", targetURL, strings.NewReader(xmlBody))
	if err != nil {
		return nil, fmt.Errorf("invalid caldav request URL: %w", err)
	}

	// RFC 4791 §7.8.2: Depth: 1 is mandatory for REPORT on collection resources
	req.Header.Set("Depth", "1")
	req.Header.Set("Content-Type", "application/xml; charset=utf-8")
	req.Header.Set("Accept", "text/calendar, application/xml, text/xml")
	req.Header.Set("User-Agent", "Mirrormere-CalDAV/1.0")

	applyCalDAVAuth(req, source)

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("caldav http request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		return nil, fmt.Errorf("caldav authentication failed (HTTP %d): invalid credentials for %s", resp.StatusCode, redactURL(targetURL))
	}
	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("caldav calendar collection not found (HTTP 404): %s", redactURL(targetURL))
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusMultiStatus {
		return nil, fmt.Errorf("caldav report failed with HTTP %d for %s", resp.StatusCode, redactURL(targetURL))
	}

	// Slowloris defense on response read
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxCalendarFeedBytes+1))
	if err != nil {
		return nil, fmt.Errorf("reading caldav response body: %w", err)
	}
	if len(body) > maxCalendarFeedBytes {
		return nil, fmt.Errorf("caldav payload exceeds size limit of %d bytes", maxCalendarFeedBytes)
	}

	calDataList := extractCalendarData(body)
	if len(calDataList) == 0 {
		return []CalendarEvent{}, nil
	}

	var allEvents []CalendarEvent
	for _, rawData := range calDataList {
		cal, err := ical.ParseCalendar(strings.NewReader(rawData))
		if err != nil {
			c.logger.Warn("skipping unparseable caldav ics payload", "calendar", source.Name, "error", err)
			continue
		}
		events := parseCalendarEvents(cal, source, windowStart, windowEnd)
		allEvents = append(allEvents, events...)
	}

	return allEvents, nil
}

func (c *CalDAVClient) doPropfind(ctx context.Context, targetURL, depth, xmlBody string, source CalendarSource) (*propfindMultiStatus, int, error) {
	req, err := http.NewRequestWithContext(ctx, "PROPFIND", targetURL, strings.NewReader(xmlBody))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Depth", depth)
	req.Header.Set("Content-Type", "application/xml; charset=utf-8")
	req.Header.Set("User-Agent", "Mirrormere-CalDAV/1.0")
	applyCalDAVAuth(req, source)

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusMultiStatus {
		return nil, resp.StatusCode, nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxCalendarFeedBytes+1))
	if err != nil || len(body) > maxCalendarFeedBytes {
		return nil, resp.StatusCode, err
	}

	var ms propfindMultiStatus
	if err := xml.Unmarshal(body, &ms); err != nil {
		return nil, resp.StatusCode, err
	}
	return &ms, resp.StatusCode, nil
}

// DiscoverCalendarURL resolves the specific calendar collection URL from an endpoint, principal, or home-set.
func (c *CalDAVClient) DiscoverCalendarURL(ctx context.Context, source CalendarSource) (string, error) {
	endpoint := strings.TrimSpace(source.URL)
	if after, ok := strings.CutPrefix(endpoint, "webcal://"); ok {
		endpoint = "https://" + after
	}

	propfindBody := `<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:resourcetype/>
    <c:calendar-home-set/>
    <d:current-user-principal/>
  </d:prop>
</d:propfind>`

	ms, status, err := c.doPropfind(ctx, endpoint, "0", propfindBody, source)
	if status == http.StatusUnauthorized || status == http.StatusForbidden {
		return "", fmt.Errorf("caldav authentication failed (HTTP %d): invalid credentials for %s", status, redactURL(endpoint))
	}
	if err != nil || ms == nil {
		// Fallback to configured endpoint directly if discovery request fails
		return endpoint, nil //nolint:nilerr
	}

	// 1. Fast-Path: Check if endpoint itself is already a calendar collection
	if hasDirectCalendarCollection(ms) {
		return endpoint, nil
	}

	// 2 & 3. Discover from calendar-home-set or current-user-principal
	if discovered := c.discoverFromPropstatHrefs(ctx, ms, endpoint, source); discovered != "" {
		return discovered, nil
	}

	return endpoint, nil
}

func hasDirectCalendarCollection(ms *propfindMultiStatus) bool {
	for _, res := range ms.Responses {
		for _, ps := range res.Propstats {
			if strings.Contains(ps.Status, "200") && ps.Prop.ResourceType.Calendar != nil {
				return true
			}
		}
	}
	return false
}

func (c *CalDAVClient) discoverFromPropstatHrefs(ctx context.Context, ms *propfindMultiStatus, endpoint string, source CalendarSource) string {
	// Discover calendar-home-set
	for _, res := range ms.Responses {
		for _, ps := range res.Propstats {
			if strings.Contains(ps.Status, "200") && ps.Prop.CalendarHomeSet.Href != "" {
				homeURL := resolveHRef(endpoint, ps.Prop.CalendarHomeSet.Href)
				if discovered := c.findCalendarInHome(ctx, homeURL, source); discovered != "" {
					return discovered
				}
			}
		}
	}

	// Discover current-user-principal
	for _, res := range ms.Responses {
		for _, ps := range res.Propstats {
			if strings.Contains(ps.Status, "200") && ps.Prop.CurrentUserPrincipal.Href != "" {
				principalURL := resolveHRef(endpoint, ps.Prop.CurrentUserPrincipal.Href)
				if homeURL := c.findHomeSetFromPrincipal(ctx, principalURL, source); homeURL != "" {
					if discovered := c.findCalendarInHome(ctx, homeURL, source); discovered != "" {
						return discovered
					}
				}
			}
		}
	}

	return ""
}

func (c *CalDAVClient) findHomeSetFromPrincipal(ctx context.Context, principalURL string, source CalendarSource) string {
	propfindBody := `<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <c:calendar-home-set/>
  </d:prop>
</d:propfind>`

	ms, _, err := c.doPropfind(ctx, principalURL, "0", propfindBody, source)
	if err != nil || ms == nil {
		return ""
	}

	for _, res := range ms.Responses {
		for _, ps := range res.Propstats {
			if strings.Contains(ps.Status, "200") && ps.Prop.CalendarHomeSet.Href != "" {
				return resolveHRef(principalURL, ps.Prop.CalendarHomeSet.Href)
			}
		}
	}

	return ""
}

func (c *CalDAVClient) findCalendarInHome(ctx context.Context, homeURL string, source CalendarSource) string {
	propfindBody := `<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:resourcetype/>
    <d:displayname/>
  </d:prop>
</d:propfind>`

	ms, _, err := c.doPropfind(ctx, homeURL, "1", propfindBody, source)
	if err != nil || ms == nil {
		return ""
	}

	var firstCalURL string
	for _, res := range ms.Responses {
		for _, ps := range res.Propstats {
			if strings.Contains(ps.Status, "200") && ps.Prop.ResourceType.Calendar != nil {
				calURL := resolveHRef(homeURL, res.Href)
				if strings.EqualFold(strings.TrimSpace(ps.Prop.DisplayName), strings.TrimSpace(source.Name)) {
					return calURL
				}
				if firstCalURL == "" {
					firstCalURL = calURL
				}
			}
		}
	}

	return firstCalURL
}

func (c *CalDAVClient) getCachedURL(key string) (string, bool) {
	c.cacheMu.RLock()
	defer c.cacheMu.RUnlock()
	url, ok := c.discoveryCache[key]
	return url, ok
}

func (c *CalDAVClient) setCachedURL(key, url string) {
	c.cacheMu.Lock()
	defer c.cacheMu.Unlock()
	c.discoveryCache[key] = url
}

func applyCalDAVAuth(req *http.Request, source CalendarSource) {
	if source.Token != "" {
		req.Header.Set("Authorization", "Bearer "+source.Token)
	} else if source.Username != "" || source.Password != "" {
		req.SetBasicAuth(source.Username, source.Password)
	}
}

func extractCalendarData(body []byte) []string {
	var calDataList []string
	var ms propfindMultiStatus
	if err := xml.Unmarshal(body, &ms); err == nil && len(ms.Responses) > 0 {
		for _, resp := range ms.Responses {
			for _, ps := range resp.Propstats {
				if strings.Contains(ps.Status, "200") {
					trimmed := strings.TrimSpace(ps.Prop.CalendarData)
					if trimmed != "" {
						calDataList = append(calDataList, trimmed)
					}
				}
			}
		}
		return calDataList
	}

	// Fallback for servers returning raw iCalendar text
	rawBody := strings.TrimSpace(string(body))
	if strings.Contains(rawBody, "BEGIN:VCALENDAR") {
		calDataList = append(calDataList, rawBody)
	}

	return calDataList
}

func resolveHRef(baseStr, href string) string {
	base, err := url.Parse(baseStr)
	if err != nil {
		return href
	}
	ref, err := url.Parse(href)
	if err != nil {
		return href
	}
	return base.ResolveReference(ref).String()
}

func redactURL(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	return u.Redacted()
}

func ensureTrailingSlash(u string) string {
	parsed, err := url.Parse(u)
	if err != nil {
		return u
	}
	if !strings.HasSuffix(parsed.Path, "/") && !strings.Contains(path.Base(parsed.Path), ".") {
		parsed.Path += "/"
		return parsed.String()
	}
	return u
}

// XML unmarshaling structs for WebDAV/CalDAV responses
type propfindMultiStatus struct {
	XMLName   xml.Name           `xml:"multistatus"`
	Responses []propfindResponse `xml:"response"`
}

type propfindResponse struct {
	Href      string             `xml:"href"`
	Propstats []propfindPropstat `xml:"propstat"`
}

type propfindPropstat struct {
	Prop   propfindProp `xml:"prop"`
	Status string       `xml:"status"`
}

type propfindProp struct {
	ResourceType         caldavResourceType         `xml:"resourcetype"`
	CalendarHomeSet      caldavCalendarHomeSet      `xml:"calendar-home-set"`
	CurrentUserPrincipal caldavCurrentUserPrincipal `xml:"current-user-principal"`
	DisplayName          string                     `xml:"displayname"`
	CalendarData         string                     `xml:"calendar-data"`
}

type caldavResourceType struct {
	Calendar *struct{} `xml:"calendar"`
}

type caldavCalendarHomeSet struct {
	Href string `xml:"href"`
}

type caldavCurrentUserPrincipal struct {
	Href string `xml:"href"`
}
