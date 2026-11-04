package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/rand"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	PhotoSourceShareURL      = "share_url"
	PhotoSourceEmberBridge   = "ember_bridge"
	defaultPhotoPreloadCount = 500
	maxPhotoAlbumBytes       = 5 * 1024 * 1024 // 5MB Slowloris defense ceiling
)

var pwExactURLRe = regexp.MustCompile(`^https:\/\/lh[0-9]*\.googleusercontent\.com\/pw\/[a-zA-Z0-9_\-]+$`)

// Standard error definitions for Google Photos ingestion.
var (
	ErrAlbumPrivate   = errors.New("shared album requires authentication or link is expired")
	ErrAlbumRateLimit = errors.New("google photos request rate-limited or blocked by captcha")
	ErrAlbumNotFound  = errors.New("shared album not found")
)

// AlbumSource describes a single configured album.
type AlbumSource struct {
	Name        string `json:"name" yaml:"name"`
	Source      string `json:"source,omitempty" yaml:"source,omitempty"`
	URL         string `json:"url,omitempty" yaml:"url,omitempty"`
	URLEnv      string `json:"url_env,omitempty" yaml:"url_env,omitempty"`
	ResolvedURL string `json:"-" yaml:"-"`
}

// PhotoSnapshot represents the normalized photo carousel data emitted over SSE.
type PhotoSnapshot struct {
	AlbumName   string      `json:"album_name"`
	AlbumURL    string      `json:"album_url,omitempty"`
	TotalPhotos int         `json:"total_photos"`
	Photos      []PhotoItem `json:"photos"`
}

// PhotoItem represents a single photograph within a shared album.
// Contains clean CDN base URL without static sizing parameters.
type PhotoItem struct {
	ID              string  `json:"id"`
	URL             string  `json:"url"` // Clean base URL e.g. https://lh3.googleusercontent.com/pw/...
	Timestamp       string  `json:"timestamp,omitempty"`
	AspectRatio     float64 `json:"aspect_ratio"`
	AlbumName       string  `json:"album_name,omitempty"`
	Location        string  `json:"location,omitempty"`
	DominantColor   string  `json:"dominant_color,omitempty"`
	LocationChecked bool    `json:"location_checked,omitempty"`
}

// PhotosConfig holds parsed configuration for PhotosProvider.
type PhotosConfig struct {
	Source      string        `json:"source,omitempty" yaml:"source,omitempty"`
	AlbumURL    string        `json:"album_url,omitempty" yaml:"album_url,omitempty"`
	AlbumURLEnv string        `json:"album_url_env,omitempty" yaml:"album_url_env,omitempty"`
	Albums      []AlbumSource `json:"albums,omitempty" yaml:"albums,omitempty"`
	CacheDir    string        `json:"cache_dir,omitempty" yaml:"cache_dir,omitempty"`
	isLegacy    bool
}

type albumState struct {
	cfg           AlbumSource
	scrapedTitle  string
	photos        []PhotoItem
	lastPage1Sync time.Time
	lastFullSync  time.Time
	paginated     bool
	hydrated      bool
}

// PhotosProvider implements Provider for Google Photos unlisted shared album carousels.
type PhotosProvider struct {
	syncMu            sync.Mutex
	mu                sync.RWMutex
	cfg               PhotosConfig
	albums            []*albumState
	effectiveCacheDir string
	wasWarmBoot       bool
	cachedSnapshot    *PhotoSnapshot
	deck              []PhotoItem
	deckCursor        int
	client            *http.Client
	logger            *slog.Logger
	randSource        *rand.Rand
	location          *time.Location
	defaultCacheDir   string
	page1Interval     time.Duration
	fullSyncInterval  time.Duration
}

func init() {
	registerBuiltin("photo-carousel", func() Provider {
		return NewPhotosProvider()
	})
}

// NewPhotosProvider constructs an uninitialized PhotosProvider.
func NewPhotosProvider(opts ...PhotosOption) *PhotosProvider {
	p := &PhotosProvider{
		defaultCacheDir:  "",
		page1Interval:    DefaultPhotosPage1Interval,
		fullSyncInterval: DefaultPhotosFullSyncInterval,
		client: &http.Client{
			Timeout: 15 * time.Second,
		},
		logger:     slog.Default().With("provider", "photo-carousel"),
		randSource: rand.New(rand.NewSource(time.Now().UnixNano())),
		location:   time.UTC,
	}
	for _, opt := range opts {
		opt(p)
	}
	return p
}

// Source returns the configured ingestion source mode.
func (p *PhotosProvider) Source() string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.sourceLocked()
}

func (p *PhotosProvider) sourceLocked() string {
	if len(p.albums) == 0 {
		return ""
	}
	allEmber := true
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceShareURL {
			return PhotoSourceShareURL
		}
		if a.cfg.Source != PhotoSourceEmberBridge {
			allEmber = false
		}
	}
	if allEmber {
		return PhotoSourceEmberBridge
	}
	return ""
}

func (p *PhotosProvider) isAllEmberBridgeLocked() bool {
	if len(p.albums) == 0 {
		return false
	}
	for _, a := range p.albums {
		if a.cfg.Source != PhotoSourceEmberBridge {
			return false
		}
	}
	return true
}

func resolveAlbumDisplayName(a *albumState) string {
	if a.cfg.Name != "Shared Album" && a.cfg.Name != "" {
		return a.cfg.Name
	}
	if a.scrapedTitle != "" {
		return a.scrapedTitle
	}
	return "Shared Album"
}

func resolveSnapshotAlbumName(albums []*albumState) string {
	if len(albums) == 0 {
		return "Shared Album"
	}
	if len(albums) == 1 && albums[0].cfg.Name == "Shared Album" {
		if title := strings.TrimSpace(albums[0].scrapedTitle); title != "" {
			return title
		}
		return "Shared Album"
	}
	seen := make(map[string]struct{})
	var names []string
	for _, a := range albums {
		name := strings.TrimSpace(a.cfg.Name)
		if name != "" {
			if _, ok := seen[name]; !ok {
				seen[name] = struct{}{}
				names = append(names, name)
			}
		}
	}
	if len(names) == 0 {
		return "Shared Album"
	}
	return strings.Join(names, ", ")
}

func resolveSnapshotAlbumURL(albums []*albumState) string {
	for _, a := range albums {
		if a.cfg.Source == PhotoSourceEmberBridge && a.cfg.ResolvedURL != "" {
			return a.cfg.ResolvedURL
		}
	}
	if len(albums) > 0 {
		return albums[0].cfg.ResolvedURL
	}
	return ""
}

func (p *PhotosProvider) collectAllUniquePhotosLocked() []PhotoItem {
	var totalCap int
	for _, a := range p.albums {
		totalCap += len(a.photos)
	}
	combined := make([]PhotoItem, 0, totalCap)
	seen := make(map[string]struct{}, totalCap)
	for _, a := range p.albums {
		displayName := resolveAlbumDisplayName(a)
		for _, it := range a.photos {
			if it.AlbumName == "" {
				it.AlbumName = displayName
			}
			key := photoDedupeKey(it)
			if key != "" {
				if _, ok := seen[key]; ok {
					continue
				}
				seen[key] = struct{}{}
			}
			combined = append(combined, it)
		}
	}
	return combined
}

func (p *PhotosProvider) buildSnapshotLocked(window []PhotoItem) *PhotoSnapshot {
	allUnique := p.collectAllUniquePhotosLocked()
	total := len(allUnique)
	if len(window) == 0 && total > 0 {
		window = slices.Clone(allUnique)
	}
	return &PhotoSnapshot{
		AlbumName:   resolveSnapshotAlbumName(p.albums),
		AlbumURL:    resolveSnapshotAlbumURL(p.albums),
		TotalPhotos: total,
		Photos:      window,
	}
}

func (p *PhotosProvider) dealWindowLocked() []PhotoItem {
	allUnique := p.collectAllUniquePhotosLocked()
	return p.selectAlbumWindowLocked(allUnique)
}

func (p *PhotosProvider) peekWindowLocked() []PhotoItem {
	if len(p.deck) == 0 {
		allUnique := p.collectAllUniquePhotosLocked()
		if len(allUnique) <= defaultPhotoPreloadCount {
			return slices.Clone(allUnique)
		}
		return slices.Clone(allUnique[:defaultPhotoPreloadCount])
	}
	limit := min(len(p.deck), defaultPhotoPreloadCount)
	start := p.deckCursor
	if start >= len(p.deck) {
		start = 0
	}
	if start+limit <= len(p.deck) {
		return slices.Clone(p.deck[start : start+limit])
	}
	tail := slices.Clone(p.deck[start:])
	rem := limit - len(tail)
	return slices.Concat(tail, p.deck[:rem])
}

// Init parses configuration, resolves 3-tier secrets, and validates parameters.
func (p *PhotosProvider) Init(_ context.Context, config map[string]any, opts InitOptions) error {
	p.syncMu.Lock()
	defer p.syncMu.Unlock()
	p.mu.Lock()
	defer p.mu.Unlock()

	p.cachedSnapshot = nil
	p.deck = nil
	p.deckCursor = 0
	p.albums = nil
	p.wasWarmBoot = false
	p.location = time.UTC
	if tzStr := strings.TrimSpace(opts.Timezone); tzStr != "" {
		if loc, err := time.LoadLocation(tzStr); err == nil {
			p.location = loc
		}
	}

	parsed, err := parsePhotosConfig(config, opts)
	if err != nil {
		return err
	}
	p.cfg = parsed

	resolvedAlbums, err := resolveAlbumSources(parsed, opts)
	if err != nil {
		return err
	}

	effectiveCacheDir := p.defaultCacheDir
	if parsed.CacheDir != "" {
		effectiveCacheDir = parsed.CacheDir
	}
	p.effectiveCacheDir = effectiveCacheDir

	allWarm := true
	shareCount := 0

	for _, as := range resolvedAlbums {
		state := &albumState{
			cfg: as,
		}
		switch as.Source {
		case PhotoSourceShareURL:
			shareCount++
			diskCache, err := loadAlbumDiskCache(effectiveCacheDir, as.Source, as.ResolvedURL)
			if err == nil && diskCache != nil && len(diskCache.Photos) > 0 {
				state.scrapedTitle = diskCache.ScrapedTitle
				state.lastPage1Sync = diskCache.LastPage1Sync
				state.lastFullSync = diskCache.LastFullSync
				state.paginated = diskCache.Paginated
				state.photos = diskCache.Photos
				state.hydrated = true
			} else {
				allWarm = false
			}
		case PhotoSourceEmberBridge:
			state.hydrated = true
		}
		p.albums = append(p.albums, state)
	}

	if shareCount > 0 && allWarm {
		p.wasWarmBoot = true
	}

	combined := p.collectAllUniquePhotosLocked()
	if len(combined) > 0 {
		p.reconcilePhotoDeck(combined, p.randSource)
	}

	if p.isAllEmberBridgeLocked() {
		resolvedURL := ""
		if len(resolvedAlbums) > 0 {
			resolvedURL = resolvedAlbums[0].ResolvedURL
		}
		p.cachedSnapshot = &PhotoSnapshot{
			AlbumName:   resolveSnapshotAlbumName(p.albums),
			AlbumURL:    resolvedURL,
			TotalPhotos: 0,
			Photos:      []PhotoItem{},
		}
	}

	return nil
}

// Fetch executes an album synchronization cycle with SWR caching.
func (p *PhotosProvider) Fetch(ctx context.Context) (any, error) {
	p.mu.RLock()
	if len(p.albums) == 0 {
		p.mu.RUnlock()
		return nil, fmt.Errorf("photo-carousel: provider not initialized")
	}

	if p.isAllEmberBridgeLocked() {
		defer p.mu.RUnlock()
		if p.cachedSnapshot == nil {
			return nil, fmt.Errorf("photo-carousel: provider not initialized")
		}
		cloned := *p.cachedSnapshot
		cloned.Photos = slices.Clone(p.cachedSnapshot.Photos)
		return &cloned, nil
	}

	needsHydrate := false
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceShareURL && !a.hydrated {
			needsHydrate = true
			break
		}
	}
	p.mu.RUnlock()

	if needsHydrate {
		if err := p.Sync(ctx, true); err != nil {
			return nil, err
		}
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	window := p.dealWindowLocked()
	snap := p.buildSnapshotLocked(window)
	p.cachedSnapshot = snap
	return snap, nil
}

// Sync scrapes share_url albums, writes disk cache, and updates the combined deck.
func (p *PhotosProvider) Sync(ctx context.Context, full bool) error {
	p.syncMu.Lock()
	defer p.syncMu.Unlock()

	p.mu.RLock()
	if len(p.albums) == 0 {
		p.mu.RUnlock()
		return fmt.Errorf("photo-carousel: provider not initialized")
	}
	albums := slices.Clone(p.albums)
	cacheDir := p.effectiveCacheDir
	client := p.client
	logger := p.logger
	p.mu.RUnlock()

	for _, a := range albums {
		if a.cfg.Source != PhotoSourceShareURL {
			continue
		}
		if err := p.syncAlbum(ctx, a, full, cacheDir, client, logger); err != nil {
			return err
		}
	}

	p.mu.Lock()
	combined := p.collectAllUniquePhotosLocked()
	p.reconcilePhotoDeck(combined, p.randSource)
	p.mu.Unlock()

	return nil
}

func (p *PhotosProvider) syncAlbumFull(ctx context.Context, albumURL, albumKey, initialToken, authKey, displayName string, page1Photos []PhotoItem, client *http.Client, logger *slog.Logger) ([]PhotoItem, bool) {
	paged, complete := paginateSnAcKc(ctx, client, albumURL, albumKey, initialToken, authKey, logger)
	allPhotos := make([]PhotoItem, 0, len(page1Photos)+len(paged))
	for _, it := range page1Photos {
		it.AlbumName = displayName
		allPhotos = append(allPhotos, it)
	}
	for _, it := range paged {
		it.AlbumName = displayName
		allPhotos = append(allPhotos, it)
	}
	return deduplicatePhotoSlice(allPhotos), complete
}

func syncAlbumDelta(existing []PhotoItem, page1Photos []PhotoItem, displayName string) []PhotoItem {
	merged := make([]PhotoItem, 0, len(page1Photos)+len(existing))
	seen := make(map[string]struct{}, len(page1Photos)+len(existing))
	for _, it := range page1Photos {
		it.AlbumName = displayName
		key := photoDedupeKey(it)
		if key != "" {
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
		}
		merged = append(merged, it)
	}
	for _, it := range existing {
		key := photoDedupeKey(it)
		if key != "" {
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
		}
		merged = append(merged, it)
	}
	return merged
}

func (p *PhotosProvider) syncAlbum(ctx context.Context, a *albumState, full bool, cacheDir string, client *http.Client, logger *slog.Logger) error {
	p.mu.RLock()
	resolvedURL := a.cfg.ResolvedURL
	albumSourceName := a.cfg.Name
	albumSourceType := a.cfg.Source
	alreadyHydrated := a.hydrated
	lastFullZero := a.lastFullSync.IsZero()
	cachedCount := len(a.photos)
	currentTitle := a.scrapedTitle
	existingPhotos := slices.Clone(a.photos)
	p.mu.RUnlock()

	page1Photos, scrapedTitle, initialToken, albumKey, authKey, err := p.scrapePage1(ctx, client, resolvedURL, logger)
	if err != nil {
		if alreadyHydrated {
			logger.Warn("photo-carousel upstream scrape failed; returning cached SWR snapshot",
				"error", err,
				"url", redactURL(resolvedURL),
				"cached_photos", cachedCount)
			return nil
		}
		return fmt.Errorf("photo-carousel scrape failed: %w", err)
	}

	finalTitle := currentTitle
	if scrapedTitle != "" {
		finalTitle = scrapedTitle
	}

	displayName := albumSourceName
	if displayName == "" || displayName == "Shared Album" {
		if finalTitle != "" {
			displayName = finalTitle
		} else {
			displayName = "Shared Album"
		}
	}

	var newPhotos []PhotoItem
	var paginatedOK bool
	now := time.Now()
	isFullSync := full || !alreadyHydrated || lastFullZero

	if isFullSync {
		newPhotos, paginatedOK = p.syncAlbumFull(ctx, resolvedURL, albumKey, initialToken, authKey, displayName, page1Photos, client, logger)
	} else {
		newPhotos = syncAlbumDelta(existingPhotos, page1Photos, displayName)
	}
	carryCachedPhotoMetadata(newPhotos, existingPhotos)
	enrichPhotoLocations(ctx, client, resolvedURL, authKey, newPhotos, logger)

	var toCache *albumDiskCache

	p.mu.Lock()
	if scrapedTitle != "" {
		a.scrapedTitle = scrapedTitle
	}
	a.photos = newPhotos
	a.lastPage1Sync = now
	if isFullSync {
		if paginatedOK {
			a.lastFullSync = now
		}
		a.paginated = paginatedOK
	}
	a.hydrated = true

	if cacheDir != "" {
		toCache = &albumDiskCache{
			AlbumName:     a.cfg.Name,
			ScrapedTitle:  a.scrapedTitle,
			ResolvedURL:   a.cfg.ResolvedURL,
			LastPage1Sync: a.lastPage1Sync,
			LastFullSync:  a.lastFullSync,
			Paginated:     a.paginated,
			Photos:        a.photos,
		}
	}
	p.mu.Unlock()

	if toCache != nil {
		writeAlbumDiskCache(cacheDir, albumSourceType, resolvedURL, toCache, logger)
	}

	return nil
}

func deduplicatePhotoSlice(photos []PhotoItem) []PhotoItem {
	seen := make(map[string]struct{}, len(photos))
	deduped := make([]PhotoItem, 0, len(photos))
	for _, it := range photos {
		key := photoDedupeKey(it)
		if key != "" {
			if _, ok := seen[key]; ok {
				continue
			}
			seen[key] = struct{}{}
		}
		deduped = append(deduped, it)
	}
	return deduped
}

func (p *PhotosProvider) hasShareURLAlbumsLocked() bool {
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceShareURL {
			return true
		}
	}
	return false
}

func (p *PhotosProvider) checkWarmBootStaleLocked(now time.Time) (isStale, needsFull bool) {
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceShareURL {
			if a.lastFullSync.IsZero() || now.Sub(a.lastFullSync) >= p.fullSyncInterval || !a.paginated || hasUncheckedPhotoLocations(a.photos) {
				return true, true
			}
			if a.lastPage1Sync.IsZero() || now.Sub(a.lastPage1Sync) >= p.page1Interval {
				isStale = true
			}
		}
	}
	return isStale, false
}

func (p *PhotosProvider) checkPeriodicFullLocked(now time.Time) bool {
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceShareURL {
			if a.lastFullSync.IsZero() || now.Sub(a.lastFullSync) >= p.fullSyncInterval || !a.paginated {
				return true
			}
		}
	}
	return false
}

func (p *PhotosProvider) emitCurrentSnapshot(ctx context.Context, sink chan<- WidgetPayload) error {
	p.mu.RLock()
	window := p.peekWindowLocked()
	snap := p.buildSnapshotLocked(window)
	p.mu.RUnlock()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case sink <- WidgetPayload{Data: snap, State: StateHealthy}:
		return nil
	}
}

func (p *PhotosProvider) handleWarmBootSubscribe(ctx context.Context, sink chan<- WidgetPayload) {
	p.mu.RLock()
	isStale, needsFull := p.checkWarmBootStaleLocked(time.Now())
	p.mu.RUnlock()

	if isStale {
		if err := p.Sync(ctx, needsFull); err == nil {
			if emitErr := p.emitCurrentSnapshot(ctx, sink); emitErr != nil && p.logger != nil {
				p.logger.Debug("photo-carousel: failed to emit warm boot snapshot", "error", emitErr)
			}
		}
	}
}

// Subscribe is a background synchronizer for photo-carousel share_url providers.
func (p *PhotosProvider) Subscribe(ctx context.Context, sink chan<- WidgetPayload) error {
	if sink == nil {
		return nil
	}

	p.mu.RLock()
	hasShare := p.hasShareURLAlbumsLocked()
	wasWarm := p.wasWarmBoot
	p.mu.RUnlock()

	if !hasShare {
		return nil
	}

	if wasWarm {
		p.handleWarmBootSubscribe(ctx, sink)
	}

	ticker := time.NewTicker(p.page1Interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			p.mu.RLock()
			needsFull := p.checkPeriodicFullLocked(time.Now())
			p.mu.RUnlock()

			if err := p.Sync(ctx, needsFull); err != nil {
				p.logger.Warn("photo-carousel background sync failed", "error", err)
				continue
			}
			if err := p.emitCurrentSnapshot(ctx, sink); err != nil {
				return err
			}
		}
	}
}

// Shutdown cleans up resources.
func (p *PhotosProvider) Shutdown(_ context.Context) error {
	p.syncMu.Lock()
	defer p.syncMu.Unlock()
	p.mu.Lock()
	defer p.mu.Unlock()

	p.cachedSnapshot = nil
	p.deck = nil
	p.deckCursor = 0
	p.albums = nil
	p.wasWarmBoot = false
	p.cfg = PhotosConfig{}
	return nil
}

func checkAlbumResponseStatus(resp *http.Response) error {
	// Detect redirect to login wall, cookie consent, or CAPTCHA blocks
	if resp.Request != nil && resp.Request.URL != nil {
		finalHost := resp.Request.URL.Host
		if strings.Contains(finalHost, "accounts.google.com") {
			return ErrAlbumPrivate
		}
		if strings.Contains(finalHost, "sorry.google.com") {
			return ErrAlbumRateLimit
		}
		if strings.Contains(finalHost, "consent.google.com") {
			return ErrAlbumPrivate
		}
	}

	if resp.StatusCode == http.StatusNotFound {
		return ErrAlbumNotFound
	}
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusUnauthorized {
		return ErrAlbumPrivate
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		return ErrAlbumRateLimit
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("upstream Google Photos returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func (p *PhotosProvider) scrapePage1(ctx context.Context, client *http.Client, albumURL string, logger *slog.Logger) ([]PhotoItem, string, string, string, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, albumURL, nil)
	if err != nil {
		return nil, "", "", "", "", fmt.Errorf("failed to create request: %w", err)
	}

	// Browser User-Agent and Accept-Language per Girl Gang recommendations
	req.Header.Set("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")

	resp, err := client.Do(req)
	if err != nil {
		return nil, "", "", "", "", fmt.Errorf("request to %s failed: %w", redactURL(albumURL), err)
	}
	defer func() { _ = resp.Body.Close() }()

	if err := checkAlbumResponseStatus(resp); err != nil {
		return nil, "", "", "", "", err
	}

	// Slowloris defense: enforce 5MB ceiling
	limitReader := io.LimitReader(resp.Body, maxPhotoAlbumBytes+1)
	bodyBytes, err := io.ReadAll(limitReader)
	if err != nil {
		return nil, "", "", "", "", fmt.Errorf("failed to read response body: %w", err)
	}
	if len(bodyBytes) > maxPhotoAlbumBytes {
		return nil, "", "", "", "", fmt.Errorf("google photos payload exceeded %d byte ceiling", maxPhotoAlbumBytes)
	}

	var authKey string
	if resp.Request != nil && resp.Request.URL != nil {
		authKey = strings.TrimSpace(resp.Request.URL.Query().Get("key"))
	}

	html := string(bodyBytes)
	albumName := extractAlbumTitle(html)
	photos := extractPhotosFromHTML(html, logger)
	albumKey, token := extractAlbumKeyAndToken(html)

	return photos, albumName, token, albumKey, authKey, nil
}

func photoItemKey(item PhotoItem) string {
	if item.URL != "" {
		return item.URL
	}
	return item.ID
}

func photoDayKey(item PhotoItem, loc *time.Location) string {
	if ts := strings.TrimSpace(item.Timestamp); ts != "" {
		if t, err := time.Parse(time.RFC3339, ts); err == nil {
			if loc != nil {
				t = t.In(loc)
			} else {
				t = t.UTC()
			}
			return t.Format("2006-01-02")
		}
	}
	return "undated:" + photoItemKey(item)
}

func photoSlotKey(item PhotoItem, loc *time.Location) string {
	return photoDayKey(item, loc) + "\x00" + strings.TrimSpace(item.AlbumName)
}

type dayAlbumBuckets struct {
	dayOrder     []string
	albumOrder   []string
	byDayAlbum   map[string]map[string][]PhotoItem
	scrapedByKey map[string]PhotoItem
}

func groupPhotosByDayAndAlbum(scraped []PhotoItem, loc *time.Location) dayAlbumBuckets {
	res := dayAlbumBuckets{
		byDayAlbum:   make(map[string]map[string][]PhotoItem),
		scrapedByKey: make(map[string]PhotoItem, len(scraped)),
	}
	seenAlbums := make(map[string]struct{})
	for _, item := range scraped {
		res.scrapedByKey[photoItemKey(item)] = item
		dKey := photoDayKey(item, loc)
		aKey := strings.TrimSpace(item.AlbumName)
		if _, ok := seenAlbums[aKey]; !ok {
			seenAlbums[aKey] = struct{}{}
			res.albumOrder = append(res.albumOrder, aKey)
		}
		albumsForDay, ok := res.byDayAlbum[dKey]
		if !ok {
			albumsForDay = make(map[string][]PhotoItem)
			res.byDayAlbum[dKey] = albumsForDay
			res.dayOrder = append(res.dayOrder, dKey)
		}
		albumsForDay[aKey] = append(albumsForDay[aKey], item)
	}
	return res
}

func (p *PhotosProvider) extractExistingDeckState(buckets dayAlbumBuckets) ([]string, map[string]PhotoItem) {
	seenDays := make(map[string]struct{}, len(p.deck))
	var existingDays []string
	chosenBySlot := make(map[string]PhotoItem, len(p.deck))

	for _, item := range p.deck {
		updated, ok := buckets.scrapedByKey[photoItemKey(item)]
		if !ok {
			continue
		}
		dKey := photoDayKey(updated, p.location)
		aKey := strings.TrimSpace(updated.AlbumName)
		slotKey := dKey + "\x00" + aKey
		if _, exists := chosenBySlot[slotKey]; !exists {
			chosenBySlot[slotKey] = updated
		}
		if _, exists := seenDays[dKey]; !exists {
			seenDays[dKey] = struct{}{}
			existingDays = append(existingDays, dKey)
		}
	}
	return existingDays, chosenBySlot
}

func (p *PhotosProvider) reconcilePhotoDeck(scraped []PhotoItem, r *rand.Rand) {
	if r == nil {
		r = rand.New(rand.NewSource(time.Now().UnixNano()))
	}

	buckets := groupPhotosByDayAndAlbum(scraped, p.location)
	existingDays, chosenBySlot := p.extractExistingDeckState(buckets)

	existingDaySet := make(map[string]struct{}, len(existingDays))
	for _, dKey := range existingDays {
		existingDaySet[dKey] = struct{}{}
	}

	var newDays []string
	for _, dKey := range buckets.dayOrder {
		if _, ok := existingDaySet[dKey]; !ok {
			newDays = append(newDays, dKey)
		}
	}
	if len(newDays) > 1 {
		r.Shuffle(len(newDays), func(i, j int) {
			newDays[i], newDays[j] = newDays[j], newDays[i]
		})
	}

	allDays := slices.Concat(existingDays, newDays)
	capHint := len(allDays) * max(1, len(buckets.albumOrder))
	newDeck := make([]PhotoItem, 0, capHint)
	newDeckKeys := make(map[string]struct{}, capHint)

	for _, dKey := range allDays {
		albumsForDay := buckets.byDayAlbum[dKey]
		for _, aKey := range buckets.albumOrder {
			candidates := albumsForDay[aKey]
			if len(candidates) == 0 {
				continue
			}
			chosen, ok := chosenBySlot[dKey+"\x00"+aKey]
			if !ok {
				chosen = candidates[r.Intn(len(candidates))]
			}
			newDeck = append(newDeck, chosen)
			newDeckKeys[photoItemKey(chosen)] = struct{}{}
		}
	}

	newCursor := p.deckCursor
	for i := range min(p.deckCursor, len(p.deck)) {
		if _, ok := newDeckKeys[photoItemKey(p.deck[i])]; !ok {
			newCursor--
		}
	}
	p.deckCursor = max(0, min(newCursor, len(newDeck)))
	p.deck = newDeck
}

func (p *PhotosProvider) selectAlbumWindowLocked(scraped []PhotoItem) []PhotoItem {
	if len(scraped) <= 1 {
		p.deck = nil
		p.deckCursor = 0
		return slices.Clone(scraped)
	}

	r := p.randSource
	if r == nil {
		r = rand.New(rand.NewSource(time.Now().UnixNano()))
		p.randSource = r
	}
	p.reconcilePhotoDeck(scraped, r)

	if p.deckCursor < 0 || p.deckCursor >= len(p.deck) {
		p.deckCursor = 0
	}

	limit := min(len(p.deck), defaultPhotoPreloadCount)

	if limit >= len(p.deck) {
		p.deckCursor = 0
		return slices.Clone(p.deck)
	}

	if p.deckCursor+limit < len(p.deck) {
		window := slices.Clone(p.deck[p.deckCursor : p.deckCursor+limit])
		p.deckCursor += limit
		return window
	}

	if p.deckCursor+limit == len(p.deck) {
		tail := slices.Clone(p.deck[p.deckCursor:])
		p.deck = nil
		p.reconcilePhotoDeck(scraped, r)
		if len(p.deck) > 1 && photoItemKey(p.deck[0]) == photoItemKey(tail[len(tail)-1]) {
			p.deck[0], p.deck[len(p.deck)-1] = p.deck[len(p.deck)-1], p.deck[0]
		}
		p.deckCursor = 0
		return tail
	}

	// Wrap-around: p.deckCursor+limit > len(p.deck)
	tail := slices.Clone(p.deck[p.deckCursor:])
	tailSlots := make(map[string]struct{}, len(tail))
	for _, it := range tail {
		tailSlots[photoSlotKey(it, p.location)] = struct{}{}
	}
	p.deck = nil
	p.reconcilePhotoDeck(scraped, r)
	prefix := make([]PhotoItem, 0, len(p.deck))
	for _, it := range p.deck {
		if _, inTail := tailSlots[photoSlotKey(it, p.location)]; !inTail {
			prefix = append(prefix, it)
		}
	}
	p.deck = slices.Concat(tail, prefix)
	window := slices.Clone(p.deck[:limit])
	p.deckCursor = limit
	return window
}

// IngestPushedPhotos accepts pushed photo URLs for ember_bridge instances, validates URLs, and updates cache.
func (p *PhotosProvider) IngestPushedPhotos(rawURLs []string, albumName string) (*PhotoSnapshot, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	var targetAlbum *albumState
	for _, a := range p.albums {
		if a.cfg.Source == PhotoSourceEmberBridge {
			targetAlbum = a
			break
		}
	}
	if targetAlbum == nil {
		return nil, ErrPhotoPushNotSupported
	}

	if len(rawURLs) == 0 {
		return nil, ErrInvalidPhotoPushPayload
	}

	seen := make(map[string]struct{}, len(rawURLs))
	items := make([]PhotoItem, 0, len(rawURLs))
	for idx, raw := range rawURLs {
		cleanURL := cleanGooglePhotoURL(raw)
		if !pwExactURLRe.MatchString(cleanURL) {
			return nil, fmt.Errorf("%w: URL %q does not match expected Google Photos pattern", ErrInvalidPhotoPushPayload, cleanURL)
		}
		if _, exists := seen[cleanURL]; !exists {
			seen[cleanURL] = struct{}{}
			items = append(items, PhotoItem{
				ID:          fmt.Sprintf("photo_%d", idx+1),
				URL:         cleanURL,
				AspectRatio: 1.33,
			})
		}
	}

	trimmedAlbum := strings.TrimSpace(albumName)
	targetAlbum.photos = items
	if trimmedAlbum != "" {
		targetAlbum.scrapedTitle = trimmedAlbum
	}
	targetAlbum.hydrated = true

	window := p.dealWindowLocked()
	snap := p.buildSnapshotLocked(window)
	if len(p.albums) == 1 {
		if trimmedAlbum != "" {
			snap.AlbumName = trimmedAlbum
		} else if p.cachedSnapshot != nil && p.cachedSnapshot.AlbumName != "" {
			snap.AlbumName = p.cachedSnapshot.AlbumName
		}
	}
	p.cachedSnapshot = snap

	cloned := *snap
	cloned.Photos = slices.Clone(snap.Photos)
	return &cloned, nil
}

// Regex patterns for metadata and photo extraction.
var (
	ogTitleRe  = regexp.MustCompile(`(?i)<meta\s+property=["']og:title["']\s+content=["']([^"']+)["']`)
	titleTagRe = regexp.MustCompile(`(?i)<title>([^<]+)</title>`)

	// AF_initDataCallback invocation prefix
	afCallbackPrefixRe = regexp.MustCompile(`(?i)AF_initDataCallback\s*\(`)

	// Fallback regex matching Google Photos user CDN URLs (/pw/ path)
	pwURLRe = regexp.MustCompile(`https:\/\/lh[0-9]*\.googleusercontent\.com\/pw\/[a-zA-Z0-9_\-]+`)
)

// extractAlbumTitle discovers the album name from og:title or title tag, stripping Google Photos branding.
func extractAlbumTitle(html string) string {
	for _, re := range []*regexp.Regexp{ogTitleRe, titleTagRe} {
		if m := re.FindStringSubmatch(html); len(m) > 1 {
			title := strings.TrimSpace(m[1])
			title = strings.TrimSuffix(title, " - Google Photos")
			title = strings.TrimSuffix(title, " - Google")
			if title != "" {
				return title
			}
		}
	}
	return "Shared Album"
}

// extractPhotosFromHTML implements the multi-strategy photo extraction pipeline.
func extractPhotosFromHTML(html string, logger *slog.Logger) []PhotoItem {
	// Strategy 1: AF_initDataCallback array extraction
	photos, found := extractFromAFInitData(html)
	if found {
		return photos
	}

	// Strategy 2: Fallback regex on /pw/ image CDN URLs
	photos = extractFromFallbackRegex(html)
	if len(photos) > 0 {
		return photos
	}

	if logger != nil {
		logger.Debug("photo-carousel: no photos extracted from album HTML")
	}
	return []PhotoItem{}
}

// extractFromCallback parses a single callback chunk for photo media items.
// It returns extracted photos and reports whether a structured media list was found.
func extractFromCallback(chunk string, seenURLs map[string]struct{}) ([]PhotoItem, bool) {
	dataIdx := strings.Index(chunk, "data:")
	if dataIdx == -1 {
		dataIdx = strings.Index(chunk, "data :")
	}
	if dataIdx == -1 {
		return nil, false
	}

	arrayJSON, ok := extractBalancedArray(chunk[dataIdx:])
	if !ok {
		return nil, false
	}

	payloads := collectCallbackPayloads([][]string{{"", arrayJSON}})
	if len(payloads) == 0 {
		return nil, false
	}

	// In Google Photos shared albums, items typically reside at data[1] or nested within data[0]
	items := findMediaItems(payloads[0])
	if items == nil {
		return nil, false
	}

	var results []PhotoItem
	for _, item := range items {
		photo, ok := parseMediaItem(item)
		if ok && photo.URL != "" {
			if _, seen := seenURLs[photo.URL]; !seen {
				seenURLs[photo.URL] = struct{}{}
				results = append(results, photo)
			}
		}
	}
	return results, true
}

// extractFromAFInitData searches for AF_initDataCallback blocks and extracts [id, [url, w, h], timestamp] items.
// It returns extracted photos and reports whether any structured media item list was found.
func extractFromAFInitData(html string) ([]PhotoItem, bool) {
	callbackIndices := afCallbackPrefixRe.FindAllStringIndex(html, -1)
	if len(callbackIndices) == 0 {
		return nil, false
	}

	seenURLs := make(map[string]struct{})
	var results []PhotoItem
	foundAny := false

	for _, idxRange := range callbackIndices {
		start := idxRange[0]
		end := min(start+500*1024, len(html))
		chunk := html[start:end]
		photos, found := extractFromCallback(chunk, seenURLs)
		if found {
			foundAny = true
			results = append(results, photos...)
		}
	}

	return results, foundAny
}

// extractBalancedArray scans for the first '[' and tracks nested brackets until depth returns to 0.
func extractBalancedArray(s string) (string, bool) {
	start := strings.IndexByte(s, '[')
	if start == -1 {
		return "", false
	}

	depth := 0
	inString := false
	var stringChar byte
	escaped := false

	for i := start; i < len(s); i++ {
		ch := s[i]

		if inString {
			switch {
			case escaped:
				escaped = false
			case ch == '\\':
				escaped = true
			case ch == stringChar:
				inString = false
			}
			continue
		}

		if ch == '"' || ch == '\'' {
			inString = true
			stringChar = ch
			continue
		}

		switch ch {
		case '[':
			depth++
		case ']':
			depth--
			if depth == 0 {
				return s[start : i+1], true
			}
		}
	}

	return "", false
}

func hasVideoMarker(val any) bool {
	switch v := val.(type) {
	case string:
		l := strings.ToLower(v)
		return strings.Contains(l, "video/") || strings.Contains(l, ".mp4") || l == "video"
	case []any:
		return slices.ContainsFunc(v, hasVideoMarker)
	case map[string]any:
		for k, child := range v {
			if (k == "76647426" && child != nil) || hasVideoMarker(child) {
				return true
			}
		}
	}
	return false
}

// isPhotoItemArray performs a structural check on an array element to determine whether
// it matches the [id, [url, ...], ...] media item shape. It intentionally avoids filtering
// by media type so that isMediaItemList can reliably identify media collections even when
// videos outnumber photos. Video filtering is handled when parsing items in parseMediaItem.
func isPhotoItemArray(elem any) bool {
	elemArr, ok := elem.([]any)
	if !ok || len(elemArr) < 2 {
		return false
	}
	mediaArr, ok := elemArr[1].([]any)
	if !ok || len(mediaArr) == 0 {
		return false
	}
	u, ok := mediaArr[0].(string)
	return ok && strings.HasPrefix(u, "https://lh")
}

func isMediaItemList(v []any) bool {
	if len(v) == 0 {
		return false
	}
	matchesCount := 0
	for _, elem := range v {
		if isPhotoItemArray(elem) {
			matchesCount++
		}
	}
	return matchesCount > 0 && matchesCount >= len(v)/2
}

// findMediaItems recursively inspects the parsed JSON array to locate lists of photo media items.
func findMediaItems(node any) []any {
	switch v := node.(type) {
	case []any:
		if isMediaItemList(v) {
			return v
		}
		for _, elem := range v {
			if res := findMediaItems(elem); len(res) > 0 {
				return res
			}
		}
	case map[string]any:
		for _, val := range v {
			if res := findMediaItems(val); len(res) > 0 {
				return res
			}
		}
	}

	return nil
}

func tryParseMediaSlice(arr []any) (PhotoItem, bool) {
	var id string
	if s, ok := arr[0].(string); ok {
		id = s
	}
	mediaInfo, ok := arr[1].([]any)
	if !ok || len(mediaInfo) == 0 {
		return PhotoItem{}, false
	}

	rawURL, ok := mediaInfo[0].(string)
	if !ok || !strings.HasPrefix(rawURL, "https://lh") {
		return PhotoItem{}, false
	}

	cleanURL := cleanGooglePhotoURL(rawURL)

	var width, height float64
	if len(mediaInfo) >= 3 {
		width, _ = toFloat(mediaInfo[1])
		height, _ = toFloat(mediaInfo[2])
	}

	aspectRatio := 1.33 // Default 4:3 aspect ratio per panel review
	if width > 0 && height > 0 {
		aspectRatio = math.Round((width/height)*100) / 100
	}

	var timestampStr string
	if len(arr) >= 3 {
		if tsMs, ok := toFloat(arr[2]); ok && tsMs > 0 {
			t := time.UnixMilli(int64(tsMs)).UTC()
			timestampStr = t.Format(time.RFC3339)
		}
	}

	if id == "" {
		id = fmt.Sprintf("photo_%d", time.Now().UnixNano())
	}

	return PhotoItem{
		ID:            id,
		URL:           cleanURL,
		Timestamp:     timestampStr,
		AspectRatio:   aspectRatio,
		DominantColor: extractDominantColor(mediaInfo),
	}, true
}

func extractDominantColor(mediaInfo []any) string {
	if len(mediaInfo) <= 9 || mediaInfo[9] == nil {
		return ""
	}
	raw := mediaInfo[9]
	if colorSlice, ok := raw.([]any); ok {
		if len(colorSlice) == 0 {
			return ""
		}
		raw = colorSlice[0]
	}
	rgbFloat, ok := toFloat(raw)
	if !ok || rgbFloat < 0 || rgbFloat > 0xFFFFFFFF {
		return ""
	}
	return fmt.Sprintf("#%06x", int64(rgbFloat)&0xFFFFFF)
}

// parseMediaItem parses an individual media item array: [id, [url, width, height], timestamp, ...]
func parseMediaItem(elem any) (PhotoItem, bool) {
	arr, ok := elem.([]any)
	if !ok || len(arr) < 2 || hasVideoMarker(arr) {
		return PhotoItem{}, false
	}
	return tryParseMediaSlice(arr)
}

func collectCallbackPayloads(matches [][]string) [][]any {
	var payloads [][]any
	for _, m := range matches {
		if len(m) > 1 {
			var data []any
			if err := json.Unmarshal([]byte(m[1]), &data); err == nil {
				payloads = append(payloads, data)
			}
		}
	}
	return payloads
}

// extractFromFallbackRegex scans the HTML for user media URLs (/pw/ path) and extracts clean URLs.
func extractFromFallbackRegex(html string) []PhotoItem {
	matches := pwURLRe.FindAllString(html, -1)
	if len(matches) == 0 {
		return nil
	}

	seen := make(map[string]struct{})
	var results []PhotoItem

	for idx, rawURL := range matches {
		cleanURL := cleanGooglePhotoURL(rawURL)
		if _, exists := seen[cleanURL]; !exists {
			seen[cleanURL] = struct{}{}
			results = append(results, PhotoItem{
				ID:          fmt.Sprintf("photo_%d", idx+1),
				URL:         cleanURL,
				AspectRatio: 1.33,
			})
		}
	}

	return results
}

// cleanGooglePhotoURL strips trailing sizing parameters (e.g. =w1024-h768 or =s0) from CDN URLs.
func cleanGooglePhotoURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if idx := strings.Index(raw, "="); idx != -1 {
		return raw[:idx]
	}
	return raw
}

func toFloat(val any) (float64, bool) {
	switch v := val.(type) {
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	case json.Number:
		if f, err := v.Float64(); err == nil {
			return f, true
		}
	case string:
		if f, err := strconv.ParseFloat(strings.TrimSpace(v), 64); err == nil {
			return f, true
		}
	}
	return 0, false
}
