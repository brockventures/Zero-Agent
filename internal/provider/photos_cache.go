package provider

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultPhotosCacheDir         = "/data/photos"
	DefaultPhotosPage1Interval    = time.Hour
	DefaultPhotosFullSyncInterval = 24 * time.Hour
	fDcn4bBatchSize               = 300
)

// PhotosOption configures PhotosProvider options.
type PhotosOption func(*PhotosProvider)

// WithPhotosCacheDir sets the default disk cache directory.
func WithPhotosCacheDir(dir string) PhotosOption {
	return func(p *PhotosProvider) {
		p.defaultCacheDir = dir
	}
}

// WithPhotosSyncIntervals sets the background sync intervals for Page 1 and full sync.
func WithPhotosSyncIntervals(page1Interval, fullSyncInterval time.Duration) PhotosOption {
	return func(p *PhotosProvider) {
		if page1Interval > 0 {
			p.page1Interval = page1Interval
		}
		if fullSyncInterval > 0 {
			p.fullSyncInterval = fullSyncInterval
		}
	}
}

// AlbumCacheFilename computes the sha256 hex filename for an album source.
func AlbumCacheFilename(source, resolvedURL string) string {
	h := sha256.Sum256([]byte(source + ":" + resolvedURL))
	return hex.EncodeToString(h[:]) + ".json"
}

func albumCacheFilename(source, resolvedURL string) string {
	return AlbumCacheFilename(source, resolvedURL)
}

// albumDiskCache represents the on-disk JSON cache format for an album.
type albumDiskCache struct {
	AlbumName     string      `json:"album_name"`
	ScrapedTitle  string      `json:"scraped_title"`
	ResolvedURL   string      `json:"resolved_url"`
	LastPage1Sync time.Time   `json:"last_page1_sync"`
	LastFullSync  time.Time   `json:"last_full_sync"`
	Paginated     bool        `json:"paginated,omitempty"`
	Photos        []PhotoItem `json:"photos"`
}

func loadAlbumDiskCache(cacheDir, source, resolvedURL string) (*albumDiskCache, error) {
	if cacheDir == "" {
		return nil, os.ErrNotExist
	}
	filePath := filepath.Join(cacheDir, albumCacheFilename(source, resolvedURL))
	data, err := os.ReadFile(filePath)
	if err != nil {
		return nil, err
	}
	var cached albumDiskCache
	if err := json.Unmarshal(data, &cached); err != nil {
		return nil, err
	}
	return &cached, nil
}

func writeAlbumDiskCache(cacheDir, source, resolvedURL string, cache *albumDiskCache, logger *slog.Logger) {
	if cacheDir == "" || cache == nil {
		return
	}
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		if logger != nil {
			logger.Warn("photo-carousel: failed to create cache directory", "error", err, "dir", cacheDir)
		}
		return
	}
	targetPath := filepath.Join(cacheDir, albumCacheFilename(source, resolvedURL))
	tmpFile, err := os.CreateTemp(cacheDir, "album-*.json.tmp")
	if err != nil {
		if logger != nil {
			logger.Warn("photo-carousel: failed to create temp cache file", "error", err)
		}
		return
	}
	tmpName := tmpFile.Name()
	defer func() {
		if err := os.Remove(tmpName); err != nil && !errors.Is(err, os.ErrNotExist) && logger != nil {
			logger.Debug("photo-carousel: failed to remove temp cache file", "error", err)
		}
	}()

	enc := json.NewEncoder(tmpFile)
	enc.SetIndent("", "  ")
	if err := enc.Encode(cache); err != nil {
		if closeErr := tmpFile.Close(); closeErr != nil && logger != nil {
			logger.Debug("photo-carousel: failed to close temp cache file", "error", closeErr)
		}
		if logger != nil {
			logger.Warn("photo-carousel: failed to encode cache json", "error", err)
		}
		return
	}
	if err := tmpFile.Close(); err != nil {
		if logger != nil {
			logger.Warn("photo-carousel: failed to close temp cache file", "error", err)
		}
		return
	}
	if err := os.Rename(tmpName, targetPath); err != nil {
		if logger != nil {
			logger.Warn("photo-carousel: failed to rename temp cache file", "error", err, "path", targetPath)
		}
		return
	}
}

// photoDedupeKey returns the deduplication key according to the guardrail.
func photoDedupeKey(item PhotoItem) string {
	if clean := cleanGooglePhotoURL(item.URL); clean != "" {
		return clean
	}
	if !strings.HasPrefix(item.ID, "photo_") && item.ID != "" {
		return item.ID
	}
	return item.ID
}

func extractAlbumKeyAndTokenFromArray(arr []any) (string, string) {
	var albumKey, token string
	if len(arr) >= 1 {
		if s, ok := arr[0].(string); ok {
			albumKey = s
		}
	}
	if albumKey == "" && len(arr) >= 4 {
		if meta, ok := arr[3].([]any); ok && len(meta) >= 1 {
			if s, ok := meta[0].(string); ok {
				albumKey = s
			}
		}
	}
	if len(arr) >= 3 {
		if s, ok := arr[2].(string); ok && s != "" {
			token = s
		}
	}
	return albumKey, token
}

// extractAlbumKeyAndToken extracts the albumKey and continuation token from HTML if present.
func extractAlbumKeyAndToken(html string) (string, string) {
	indices := afCallbackPrefixRe.FindAllStringIndex(html, -1)
	for _, idxRange := range indices {
		start := idxRange[0]
		end := min(start+500*1024, len(html))
		chunk := html[start:end]

		dataIdx := strings.Index(chunk, "data:")
		if dataIdx == -1 {
			dataIdx = strings.Index(chunk, "data :")
		}
		if dataIdx == -1 {
			continue
		}
		arrayJSON, ok := extractBalancedArray(chunk[dataIdx:])
		if !ok {
			continue
		}
		var arr []any
		if err := json.Unmarshal([]byte(arrayJSON), &arr); err != nil {
			continue
		}
		if albumKey, token := extractAlbumKeyAndTokenFromArray(arr); albumKey != "" || token != "" {
			return albumKey, token
		}
	}
	return "", ""
}

func resolveBatchexecuteBaseURL(albumURL string) string {
	if parsedU, err := url.Parse(albumURL); err == nil && parsedU.Host != "" && !strings.EqualFold(parsedU.Host, "photos.app.goo.gl") {
		return parsedU.Scheme + "://" + parsedU.Host
	}
	return "https://photos.google.com"
}

func postBatchexecute(ctx context.Context, client *http.Client, albumURL, rpcID string, reqEnvelope any) ([]byte, error) {
	endpoint := resolveBatchexecuteBaseURL(albumURL) + "/_/PhotosUi/data/batchexecute?rpcids=" + rpcID

	envelope, err := json.Marshal(reqEnvelope)
	if err != nil {
		return nil, err
	}

	values := url.Values{}
	values.Set("f.req", string(envelope))

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(values.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")
	req.Header.Set("Accept", "*/*")

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("batchexecute returned HTTP %d", resp.StatusCode)
	}

	limitReader := io.LimitReader(resp.Body, maxPhotoAlbumBytes+1)
	bodyBytes, err := io.ReadAll(limitReader)
	if err != nil {
		return nil, err
	}
	if len(bodyBytes) > maxPhotoAlbumBytes {
		return nil, fmt.Errorf("batchexecute payload exceeded %d byte ceiling", maxPhotoAlbumBytes)
	}
	return bodyBytes, nil
}

// fetchBatchexecutePage queries /_/PhotosUi/data/batchexecute?rpcids=snAcKc for the next page.
func fetchBatchexecutePage(ctx context.Context, client *http.Client, albumURL, albumKey, token, authKey string) ([]PhotoItem, string, error) {
	innerJSON, err := json.Marshal([]any{albumKey, token, nil, authKey})
	if err != nil {
		return nil, "", err
	}
	bodyBytes, err := postBatchexecute(ctx, client, albumURL, "snAcKc", [][][]any{{{"snAcKc", string(innerJSON), nil, "generic"}}})
	if err != nil {
		return nil, "", err
	}
	return parseBatchexecuteResponse(bodyBytes, token)
}

func parseBatchexecuteResponse(bodyBytes []byte, currentToken string) ([]PhotoItem, string, error) {
	bodyStr := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(string(bodyBytes)), ")]}'"))

	var root any
	if err := json.Unmarshal([]byte(bodyStr), &root); err == nil {
		if photos, nextToken, found := extractBatchexecutePayload(root, currentToken); found {
			return photos, nextToken, nil
		}
		return extractFromFallbackRegex(bodyStr), "", nil
	}

	for line := range strings.SplitSeq(bodyStr, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "[") || json.Unmarshal([]byte(line), &root) != nil {
			continue
		}
		if photos, nextToken, found := extractBatchexecutePayload(root, currentToken); found {
			return photos, nextToken, nil
		}
	}

	return extractFromFallbackRegex(bodyStr), "", nil
}

func findBatchExecuteEnvelopes(node any, rpcID string) [][]any {
	v, ok := node.([]any)
	if !ok {
		return nil
	}
	if len(v) >= 3 {
		if s, ok := v[1].(string); ok && s == rpcID {
			return [][]any{v}
		}
	}
	var envelopes [][]any
	for _, elem := range v {
		envelopes = append(envelopes, findBatchExecuteEnvelopes(elem, rpcID)...)
	}
	return envelopes
}

func extractBatchexecutePayload(node any, currentToken string) ([]PhotoItem, string, bool) {
	envelopes := findBatchExecuteEnvelopes(node, "snAcKc")
	var photos []PhotoItem
	var nextToken string
	found := false

	for _, elemArr := range envelopes {
		innerStr, ok := elemArr[2].(string)
		if !ok || innerStr == "" {
			continue
		}
		var inner any
		if err := json.Unmarshal([]byte(innerStr), &inner); err != nil {
			continue
		}
		found = true
		if innerPhotos, innerFound := extractPhotosFromInner(inner); innerFound {
			photos = append(photos, innerPhotos...)
		}
		if tok := extractContinuationTokenFromInner(inner, currentToken); tok != "" {
			nextToken = tok
		}
	}

	if !found {
		innerPhotos, innerFound := extractPhotosFromInner(node)
		tok := extractContinuationTokenFromInner(node, currentToken)
		if innerFound || tok != "" {
			found = true
			photos = innerPhotos
			nextToken = tok
		}
	}
	return photos, nextToken, found
}

func extractPhotosFromInner(inner any) ([]PhotoItem, bool) {
	items := findMediaItems(inner)
	if items == nil {
		return nil, false
	}
	seen := make(map[string]struct{})
	var results []PhotoItem
	for _, item := range items {
		photo, ok := parseMediaItem(item)
		if ok && photo.URL != "" {
			if _, exists := seen[photo.URL]; !exists {
				seen[photo.URL] = struct{}{}
				results = append(results, photo)
			}
		}
	}
	return results, true
}

func extractContinuationTokenFromInner(inner any, currentToken string) string {
	arr, ok := inner.([]any)
	if !ok {
		return ""
	}
	for _, elem := range arr {
		if s, ok := elem.(string); ok {
			s = strings.TrimSpace(s)
			if s != "" && s != currentToken && !strings.HasPrefix(s, "http") {
				return s
			}
		}
	}
	return ""
}

func paginateSnAcKc(ctx context.Context, client *http.Client, albumURL, albumKey, initialToken, authKey string, logger *slog.Logger) ([]PhotoItem, bool) {
	if initialToken == "" {
		return nil, true
	}
	var allPaged []PhotoItem
	seenTokens := make(map[string]struct{})
	seenTokens[initialToken] = struct{}{}
	token := initialToken

	for page := range 50 {
		select {
		case <-ctx.Done():
			return allPaged, false
		default:
		}

		photos, nextToken, err := fetchBatchexecutePage(ctx, client, albumURL, albumKey, token, authKey)
		if err != nil {
			if logger != nil {
				logger.Warn("photo-carousel: snAcKc pagination error; retaining earlier pages", "error", err, "page", page)
			}
			return allPaged, false
		}
		if len(photos) > 0 {
			allPaged = append(allPaged, photos...)
		}

		nextToken = strings.TrimSpace(nextToken)
		if nextToken == "" || nextToken == token {
			break
		}
		if _, seen := seenTokens[nextToken]; seen {
			break
		}
		seenTokens[nextToken] = struct{}{}
		token = nextToken
	}

	return allPaged, true
}

func carryCachedPhotoMetadata(photos, existing []PhotoItem) {
	byKey := make(map[string]PhotoItem, len(existing))
	for _, ex := range existing {
		byKey[photoDedupeKey(ex)] = ex
	}
	for i := range photos {
		if prev, ok := byKey[photoDedupeKey(photos[i])]; ok {
			if photos[i].Location == "" {
				photos[i].Location = prev.Location
			}
			photos[i].LocationChecked = photos[i].LocationChecked || prev.LocationChecked
			if photos[i].DominantColor == "" {
				photos[i].DominantColor = prev.DominantColor
			}
		}
	}
}

func hasUncheckedPhotoLocations(photos []PhotoItem) bool {
	for _, it := range photos {
		if !it.LocationChecked && it.ID != "" && !strings.HasPrefix(it.ID, "photo_") {
			return true
		}
	}
	return false
}

func enrichPhotoLocations(ctx context.Context, client *http.Client, albumURL, authKey string, photos []PhotoItem, logger *slog.Logger) {
	if authKey == "" {
		return
	}
	var pendingIdx []int
	for i := range photos {
		if !photos[i].LocationChecked && photos[i].Location == "" && photos[i].ID != "" && !strings.HasPrefix(photos[i].ID, "photo_") {
			pendingIdx = append(pendingIdx, i)
		}
	}
	for start := 0; start < len(pendingIdx); start += fDcn4bBatchSize {
		batchIdx := pendingIdx[start:min(start+fDcn4bBatchSize, len(pendingIdx))]
		ids := make([]string, len(batchIdx))
		for i, idx := range batchIdx {
			ids[i] = photos[idx].ID
		}
		locs, checked, err := fetchPhotoLocationsBatch(ctx, client, albumURL, authKey, ids)
		if err != nil {
			if logger != nil {
				logger.Warn("photo-carousel: fDcn4b location enrichment failed", "error", err)
			}
			return
		}
		batchValid := len(checked) > 0
		for _, idx := range batchIdx {
			id := photos[idx].ID
			if checked[id] || batchValid {
				photos[idx].LocationChecked = true
				photos[idx].Location = locs[id]
			}
		}
	}
}

func fetchPhotoLocationsBatch(ctx context.Context, client *http.Client, albumURL, authKey string, photoIDs []string) (map[string]string, map[string]bool, error) {
	calls := make([][]any, 0, len(photoIDs))
	for i, id := range photoIDs {
		if innerJSON, err := json.Marshal([]any{id, 1, authKey}); err == nil {
			calls = append(calls, []any{"fDcn4b", string(innerJSON), nil, strconv.Itoa(i + 1)})
		}
	}
	bodyBytes, err := postBatchexecute(ctx, client, albumURL, "fDcn4b", [][][]any{calls})
	if err != nil {
		return nil, nil, err
	}
	locs, checked := parseFDcn4bResponse(bodyBytes)
	return locs, checked, nil
}

func parseFDcn4bResponse(bodyBytes []byte) (map[string]string, map[string]bool) {
	bodyStr := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(string(bodyBytes)), ")]}'"))
	var envelopes [][]any
	var root any
	if err := json.Unmarshal([]byte(bodyStr), &root); err == nil {
		envelopes = findBatchExecuteEnvelopes(root, "fDcn4b")
	} else {
		for line := range strings.SplitSeq(bodyStr, "\n") {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "[") && json.Unmarshal([]byte(line), &root) == nil {
				envelopes = append(envelopes, findBatchExecuteEnvelopes(root, "fDcn4b")...)
			}
		}
	}
	locs := make(map[string]string, len(envelopes))
	checked := make(map[string]bool, len(envelopes))
	for _, elemArr := range envelopes {
		if id, loc, ok := parseSingleFDcn4bEnvelope(elemArr); ok {
			checked[id] = true
			if loc != "" {
				locs[id] = loc
			}
		}
	}
	return locs, checked
}

func parseSingleFDcn4bEnvelope(elemArr []any) (string, string, bool) {
	innerStr, ok := elemArr[2].(string)
	if !ok || innerStr == "" {
		return "", "", false
	}
	var innerArr []any
	if err := json.Unmarshal([]byte(innerStr), &innerArr); err != nil || len(innerArr) == 0 {
		return "", "", false
	}
	info, ok := innerArr[0].([]any)
	if !ok || len(info) == 0 {
		return "", "", false
	}
	id, ok := info[0].(string)
	if !ok || id == "" {
		return "", "", false
	}
	var loc string
	if len(info) > 13 {
		loc = extractLocationLabel(info[13])
	}
	return id, loc, true
}

func extractLocationLabel(node any) string {
	switch v := node.(type) {
	case string:
		s := strings.TrimSpace(v)
		if s != "" && !strings.HasPrefix(s, "http") && !strings.HasPrefix(s, "ChIJ") && !strings.HasPrefix(s, "0x") {
			return s
		}
	case []any:
		for _, elem := range v {
			if s := extractLocationLabel(elem); s != "" {
				return s
			}
		}
	}
	return ""
}
