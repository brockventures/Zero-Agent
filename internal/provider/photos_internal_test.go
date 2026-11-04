package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestPhotos_InternalHelpers(t *testing.T) {
	t.Parallel()

	t.Run("toFloat conversions", func(t *testing.T) {
		t.Parallel()
		for _, tc := range []struct {
			val any
			exp float64
			ok  bool
		}{
			{42.5, 42.5, true},
			{float32(10.5), 10.5, true},
			{7, 7.0, true},
			{int64(100), 100.0, true},
			{json.Number("3.14"), 3.14, true},
			{json.Number("invalid"), 0, false},
			{"123.45", 123.45, true},
			{"invalid", 0, false},
			{true, 0, false},
			{nil, 0, false},
		} {
			if got, ok := toFloat(tc.val); ok != tc.ok || (ok && got != tc.exp) {
				t.Errorf("toFloat(%v) = (%v, %v), want (%v, %v)", tc.val, got, ok, tc.exp, tc.ok)
			}
		}
	})

	t.Run("parsePhotosConfig validation", func(t *testing.T) {
		t.Parallel()
		if _, err := parsePhotosConfig(nil, InitOptions{}); err == nil {
			t.Fatal("expected error for nil cfg")
		}
		if _, err := parsePhotosConfig(map[string]any{}, InitOptions{}); err == nil {
			t.Fatal("expected error for missing source")
		}
		if _, err := parsePhotosConfig(map[string]any{"source": "invalid"}, InitOptions{}); err == nil {
			t.Fatal("expected error for invalid source")
		}
		if _, err := parsePhotosConfig(map[string]any{"source": "share_url"}, InitOptions{}); err == nil {
			t.Fatal("expected error for missing album_url")
		}
		if _, err := parsePhotosConfig(map[string]any{"source": "share_url", "album_url": "https://photos.app.goo.gl/TEST", "album_url_env": "ENV_VAR"}, InitOptions{}); err == nil {
			t.Fatal("expected error for both album_url and album_url_env")
		}
		cfg, err := parsePhotosConfig(map[string]any{"source": "share_url", "album_url": "https://photos.app.goo.gl/TEST"}, InitOptions{})
		if err != nil || cfg.Source != "share_url" || cfg.AlbumURL != "https://photos.app.goo.gl/TEST" {
			t.Fatalf("unexpected cfg result: cfg=%+v, err=%v", cfg, err)
		}
		cfgEnv, err := parsePhotosConfig(map[string]any{"source": "ember_bridge", "album_url_env": "ALBUM_ENV"}, InitOptions{})
		if err != nil || cfgEnv.Source != "ember_bridge" || cfgEnv.AlbumURLEnv != "ALBUM_ENV" {
			t.Fatalf("unexpected cfgEnv result: cfg=%+v, err=%v", cfgEnv, err)
		}
	})

	t.Run("parseMediaItem edge cases", func(t *testing.T) {
		t.Parallel()
		for _, bad := range []any{"not-an-array", []any{"id"}, []any{"id", "not-an-array"}, []any{"id", []any{}}, []any{"id", []any{"https://example.com/pic.jpg"}}} {
			if _, ok := parseMediaItem(bad); ok {
				t.Errorf("expected parseMediaItem(%v) ok=false", bad)
			}
		}
		if item, ok := parseMediaItem([]any{"", []any{"https://lh3.googleusercontent.com/pw/TEST", 800, 600, nil, nil, nil, nil, nil, nil, []any{3549993}}}); !ok || item.ID == "" || item.AspectRatio != 1.33 || item.DominantColor != "#362b29" {
			t.Fatalf("unexpected valid item result: item=%+v, ok=%v", item, ok)
		}
		if got := extractDominantColor([]any{"u", 1, 1, nil, nil, nil, nil, nil, nil, []any{}}); got != "" {
			t.Errorf("expected empty dominant color for empty slice, got %q", got)
		}
		if got := extractDominantColor([]any{"u", 1, 1, nil, nil, nil, nil, nil, nil, -1}); got != "" {
			t.Errorf("expected empty dominant color for negative value, got %q", got)
		}
	})

	t.Run("video item detection and filtering", func(t *testing.T) {
		t.Parallel()
		photoElem := []any{"p1", []any{"https://lh3.googleusercontent.com/pw/P1", 800, 600}, 1755268200000}
		videoElem := []any{"v1", []any{"https://lh3.googleusercontent.com/pw/V1", 1920, 1080}, 1755268200000, []any{"video/mp4", 30000}}

		// Structural check returns true for both photos and videos
		if !isPhotoItemArray(photoElem) {
			t.Error("expected isPhotoItemArray=true for photo")
		}
		if !isPhotoItemArray(videoElem) {
			t.Error("expected isPhotoItemArray=true for video (structural check)")
		}

		// isMediaItemList accurately identifies list when videos outnumber photos
		list := []any{photoElem, videoElem, videoElem, videoElem}
		if !isMediaItemList(list) {
			t.Error("expected isMediaItemList=true for majority-video list")
		}

		// parseMediaItem parses photos and filters out videos
		if _, ok := parseMediaItem(photoElem); !ok {
			t.Error("expected parseMediaItem=true for photo")
		}
		if _, ok := parseMediaItem(videoElem); ok {
			t.Error("expected parseMediaItem=false for video item with video/mp4")
		}
		videoWithExt := []any{"v2", []any{"https://lh3.googleusercontent.com/pw/V2", 1920, 1080}, 1755268200000, "clip.mp4"}
		if _, ok := parseMediaItem(videoWithExt); ok {
			t.Error("expected parseMediaItem=false for video item with .mp4")
		}
	})

	t.Run("location enrichment and metadata carryover", func(t *testing.T) {
		t.Parallel()
		existing := []PhotoItem{
			{ID: "AF1Qip_cached", URL: "https://lh3.googleusercontent.com/pw/CACHED", Location: "Oakland, CA", LocationChecked: true, DominantColor: "#112233"},
		}
		fresh := []PhotoItem{
			{ID: "AF1Qip_cached", URL: "https://lh3.googleusercontent.com/pw/CACHED"},
			{ID: "AF1Qip_new1", URL: "https://lh3.googleusercontent.com/pw/NEW1"},
			{ID: "AF1Qip_noloc", URL: "https://lh3.googleusercontent.com/pw/NOLOC"},
			{ID: "AF1Qip_omitted", URL: "https://lh3.googleusercontent.com/pw/OMITTED"},
			{ID: "photo_positional", URL: "https://lh3.googleusercontent.com/pw/POS"},
		}
		carryCachedPhotoMetadata(fresh, existing)
		if fresh[0].Location != "Oakland, CA" || !fresh[0].LocationChecked || fresh[0].DominantColor != "#112233" {
			t.Fatalf("carryCachedPhotoMetadata failed: %+v", fresh[0])
		}
		if !hasUncheckedPhotoLocations(fresh) {
			t.Error("expected hasUncheckedPhotoLocations=true before enrichment")
		}

		srvLoc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.Contains(r.URL.RawQuery, "rpcids=fDcn4b") {
				w.Header().Set("Content-Type", "text/plain; charset=utf-8")
				_, _ = w.Write([]byte(")]}'\n\n120\n[[[\"wrb.fr\",\"fDcn4b\",\"[[\\\"AF1Qip_new1\\\",null,null,null,null,null,null,null,null,null,null,null,null,[null,null,[[null,[[\\\"San Francisco, CA\\\",null,1]],\\\"ChIJ123\\\"]]]]]\",null,null,null,\"1\"],[\"wrb.fr\",\"fDcn4b\",\"[[\\\"AF1Qip_noloc\\\"]]\",null,null,null,\"2\"],[\"wrb.fr\",\"fDcn4b\",\"[null]\",null,null,null,\"3\"]]]\n"))
				return
			}
			w.WriteHeader(http.StatusNotFound)
		}))
		defer srvLoc.Close()

		enrichPhotoLocations(context.Background(), srvLoc.Client(), srvLoc.URL+"?key=auth123", "auth123", fresh, slog.Default())
		if fresh[1].Location != "San Francisco, CA" || !fresh[1].LocationChecked {
			t.Errorf("expected San Francisco, CA for fresh[1], got %+v", fresh[1])
		}
		if fresh[2].Location != "" || !fresh[2].LocationChecked {
			t.Errorf("expected checked empty location for fresh[2], got %+v", fresh[2])
		}
		if fresh[3].Location != "" || !fresh[3].LocationChecked {
			t.Errorf("expected omitted item in valid batch to be marked checked, got %+v", fresh[3])
		}
		if fresh[4].LocationChecked {
			t.Errorf("expected positional ID to be skipped, got %+v", fresh[4])
		}
		if hasUncheckedPhotoLocations(fresh) {
			t.Error("expected hasUncheckedPhotoLocations=false after enrichment")
		}

		// Error fallback logs warning without mutating unchecked items
		unchecked := []PhotoItem{{ID: "AF1Qip_err", URL: "https://lh3.googleusercontent.com/pw/ERR"}}
		enrichPhotoLocations(context.Background(), srvLoc.Client(), "http://127.0.0.1:1", "auth123", unchecked, slog.Default())
		if unchecked[0].LocationChecked {
			t.Error("expected LocationChecked=false on network error")
		}

		// Malformed envelopes in parseSingleFDcn4bEnvelope and extractLocationLabel
		if _, _, ok := parseSingleFDcn4bEnvelope([]any{"wrb.fr", "fDcn4b", ""}); ok {
			t.Error("expected false for empty innerStr")
		}
		if _, _, ok := parseSingleFDcn4bEnvelope([]any{"wrb.fr", "fDcn4b", "invalid"}); ok {
			t.Error("expected false for invalid JSON")
		}
		if _, _, ok := parseSingleFDcn4bEnvelope([]any{"wrb.fr", "fDcn4b", `["not-slice"]`}); ok {
			t.Error("expected false for non-slice info")
		}
		if _, _, ok := parseSingleFDcn4bEnvelope([]any{"wrb.fr", "fDcn4b", `[[123]]`}); ok {
			t.Error("expected false for non-string id")
		}
		locs, checked := parseFDcn4bResponse([]byte(`)]}'
[[["wrb.fr","fDcn4b","[[\"AF1Qip_single\",null,null,null,null,null,null,null,null,null,null,null,null,[\"https://maps\",\"0x123\",\"ChIJabc\",\"Tokyo, Japan\"]]]",null,null,null,"1"]]]`))
		if !checked["AF1Qip_single"] || locs["AF1Qip_single"] != "Tokyo, Japan" {
			t.Errorf("unexpected single-array parse result: locs=%v checked=%v", locs, checked)
		}
		if got := resolveBatchexecuteBaseURL("https://photos.app.goo.gl/abc123"); got != "https://photos.google.com" {
			t.Errorf("expected https://photos.google.com for shortlink, got %q", got)
		}
	})

	t.Run("findMediaItems and extractBalancedArray", func(t *testing.T) {
		t.Parallel()
		nested := map[string]any{"wrapper": []any{map[string]any{"photos": []any{[]any{"p1", []any{"https://lh3.googleusercontent.com/pw/N1", 100, 100}}}}}}
		if len(findMediaItems(nested)) == 0 {
			t.Error("expected items found in nested map, got 0")
		}
		if _, ok := extractBalancedArray("no brackets"); ok {
			t.Error("expected false for no brackets")
		}
		if _, ok := extractBalancedArray("[ unclosed"); ok {
			t.Error("expected false for unclosed bracket")
		}
		if got, ok := extractBalancedArray(`[ "hello \"world\"", 'single \'quote\'', [1, 2, 3] ] tail`); !ok || got != `[ "hello \"world\"", 'single \'quote\'', [1, 2, 3] ]` {
			t.Fatalf("extractBalancedArray failed: got=%q, ok=%v", got, ok)
		}
	})

	t.Run("extractAlbumTitle and cleanURL", func(t *testing.T) {
		t.Parallel()
		if title := extractAlbumTitle("<html><body>no title</body></html>"); title != "Shared Album" {
			t.Errorf("expected 'Shared Album', got %q", title)
		}
		raw := "https://lh3.googleusercontent.com/pw/ABCDEF"
		if got := cleanGooglePhotoURL(raw); got != raw {
			t.Errorf("expected %q, got %q", raw, got)
		}
	})

	t.Run("photoItemKey", func(t *testing.T) {
		t.Parallel()
		tests := []struct {
			name string
			item PhotoItem
			want string
		}{
			{"url_present", PhotoItem{ID: "id1", URL: "https://lh3.googleusercontent.com/pw/1"}, "https://lh3.googleusercontent.com/pw/1"},
			{"url_empty", PhotoItem{ID: "id2", URL: ""}, "id2"},
		}
		for _, tc := range tests {
			if got := photoItemKey(tc.item); got != tc.want {
				t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
			}
		}
	})

	t.Run("reconcilePhotoDeck operations", func(t *testing.T) {
		t.Parallel()
		p := NewPhotosProvider()
		r := rand.New(rand.NewSource(42))

		initial := []PhotoItem{
			{ID: "1", URL: "u1"},
			{ID: "2", URL: "u2"},
			{ID: "3", URL: "u3"},
		}
		p.reconcilePhotoDeck(initial, r)
		if len(p.deck) != 3 || p.deckCursor != 0 {
			t.Fatalf("expected 3 items in deck, got %d (cursor=%d)", len(p.deck), p.deckCursor)
		}
		initialOrder := make([]string, len(p.deck))
		for i, it := range p.deck {
			initialOrder[i] = it.ID
		}

		withAdded := append(slices.Clone(initial), PhotoItem{ID: "4", URL: "u4"})
		p.reconcilePhotoDeck(withAdded, r)
		if len(p.deck) != 4 {
			t.Fatalf("expected 4 items in deck, got %d", len(p.deck))
		}
		for i := range 3 {
			if p.deck[i].ID != initialOrder[i] {
				t.Errorf("deck[%d] = %q, want %q", i, p.deck[i].ID, initialOrder[i])
			}
		}

		withUpdated := slices.Clone(withAdded)
		withUpdated[0].AspectRatio = 2.5
		p.reconcilePhotoDeck(withUpdated, r)
		foundUpdated := false
		for _, it := range p.deck {
			if it.ID == withUpdated[0].ID && it.AspectRatio == 2.5 {
				foundUpdated = true
				break
			}
		}
		if !foundUpdated {
			t.Error("expected updated metadata to be reflected in deck")
		}

		p.deckCursor = 2
		removedBefore := []PhotoItem{withAdded[1], withAdded[2], withAdded[3]}
		p.reconcilePhotoDeck(removedBefore, r)
		if p.deckCursor != 1 {
			t.Errorf("expected cursor to decrement from 2 to 1, got %d", p.deckCursor)
		}

		p.deckCursor = 1
		removedAfter := []PhotoItem{withAdded[1], withAdded[2]}
		p.reconcilePhotoDeck(removedAfter, r)
		if p.deckCursor != 1 {
			t.Errorf("expected cursor to stay 1, got %d", p.deckCursor)
		}

		// Per-day burst de-biasing and multi-album per-day pairing
		laLoc, err := time.LoadLocation("America/Los_Angeles")
		if err != nil {
			t.Fatalf("LoadLocation failed: %v", err)
		}
		pDay := NewPhotosProvider()
		pDay.location = laLoc
		dated := []PhotoItem{
			// 2026-08-01 in America/Los_Angeles (evening PDT = early UTC next day)
			{ID: "a1", URL: "https://lh3.googleusercontent.com/pw/A1", Timestamp: "2026-08-01T18:00:00Z", AlbumName: "Album A"},
			{ID: "a2", URL: "https://lh3.googleusercontent.com/pw/A2", Timestamp: "2026-08-02T02:00:00Z", AlbumName: "Album A"},
			{ID: "b1", URL: "https://lh3.googleusercontent.com/pw/B1", Timestamp: "2026-08-01T20:00:00Z", AlbumName: "Album B"},
			// 2026-08-02 in America/Los_Angeles
			{ID: "a3", URL: "https://lh3.googleusercontent.com/pw/A3", Timestamp: "2026-08-02T19:00:00Z", AlbumName: "Album A"},
		}
		pDay.reconcilePhotoDeck(dated, nil)
		if len(pDay.deck) != 3 {
			t.Fatalf("expected 3 items in per-day deck (2 for 2026-08-01 across 2 albums, 1 for 2026-08-02), got %d: %+v", len(pDay.deck), pDay.deck)
		}
		// Reconcile again with same items preserves chosen items
		firstDeck := slices.Clone(pDay.deck)
		pDay.reconcilePhotoDeck(dated, r)
		for i := range firstDeck {
			if pDay.deck[i].ID != firstDeck[i].ID {
				t.Errorf("expected stable per-day deck at %d: got %s, want %s", i, pDay.deck[i].ID, firstDeck[i].ID)
			}
		}
		// Fallback to UTC when pDay.location is nil
		pDay.location = nil
		if got := photoDayKey(PhotoItem{ID: "utc1", URL: "u", Timestamp: "2026-08-02T02:00:00Z"}, nil); got != "2026-08-02" {
			t.Errorf("expected UTC date 2026-08-02 when loc is nil, got %q", got)
		}
	})

	t.Run("selectAlbumWindow operations", func(t *testing.T) {
		t.Parallel()
		single := []PhotoItem{{ID: "1", URL: "u1"}}
		pSingle := NewPhotosProvider()
		pSingle.deck = slices.Clone(single)
		pSingle.deckCursor = 1
		winSingle := pSingle.selectAlbumWindow(single)
		if len(winSingle) != 1 || pSingle.deck != nil || pSingle.deckCursor != 0 {
			t.Errorf("expected single-item to reset deck, got deck=%v cursor=%d", pSingle.deck, pSingle.deckCursor)
		}

		scraped := []PhotoItem{
			{ID: "1", URL: "u1"},
			{ID: "2", URL: "u2"},
			{ID: "3", URL: "u3"},
			{ID: "4", URL: "u4"},
		}

		pFull := NewPhotosProvider()
		pFull.randSource = rand.New(rand.NewSource(99))
		w1 := pFull.selectAlbumWindow(scraped)
		w2 := pFull.selectAlbumWindow(scraped)
		if len(w1) != 4 || len(w2) != 4 {
			t.Fatalf("expected 4 items in window, got %d, %d", len(w1), len(w2))
		}
		for i := range w1 {
			if w1[i].ID != w2[i].ID {
				t.Fatalf("expected stable deck order: w1[%d]=%s, w2[%d]=%s", i, w1[i].ID, i, w2[i].ID)
			}
		}

		many := make([]PhotoItem, 600)
		for i := range 600 {
			many[i] = PhotoItem{ID: fmt.Sprintf("id-%d", i), URL: fmt.Sprintf("https://lh3.googleusercontent.com/pw/%d", i)}
		}
		pCap := NewPhotosProvider()
		pCap.randSource = rand.New(rand.NewSource(100))
		wCap := pCap.selectAlbumWindow(many)
		if len(wCap) != defaultPhotoPreloadCount {
			t.Fatalf("expected %d items in capped window, got %d", defaultPhotoPreloadCount, len(wCap))
		}
		wWrap := pCap.selectAlbumWindow(many)
		if len(wWrap) != defaultPhotoPreloadCount {
			t.Fatalf("expected wrap-around window length %d, got %d", defaultPhotoPreloadCount, len(wWrap))
		}

		many1000 := make([]PhotoItem, 1000)
		for i := range 1000 {
			many1000[i] = PhotoItem{ID: fmt.Sprintf("id-%d", i), URL: fmt.Sprintf("https://lh3.googleusercontent.com/pw/%d", i)}
		}
		pExact := NewPhotosProvider()
		pExact.randSource = rand.New(rand.NewSource(200))
		_ = pExact.selectAlbumWindow(many1000)
		wExact := pExact.selectAlbumWindow(many1000)
		if len(wExact) != defaultPhotoPreloadCount {
			t.Fatalf("expected exact window length %d, got %d", defaultPhotoPreloadCount, len(wExact))
		}
	})

	t.Run("lifecycle deck reset", func(t *testing.T) {
		t.Parallel()
		p := NewPhotosProvider()
		p.deck = []PhotoItem{{ID: "1", URL: "u1"}}
		p.deckCursor = 1
		p.cachedSnapshot = &PhotoSnapshot{}

		if err := p.Shutdown(context.Background()); err != nil {
			t.Fatalf("Shutdown failed: %v", err)
		}
		if p.deck != nil || p.deckCursor != 0 || p.cachedSnapshot != nil {
			t.Errorf("Shutdown did not reset deck: deck=%v, cursor=%d, cached=%v", p.deck, p.deckCursor, p.cachedSnapshot)
		}

		p.deck = []PhotoItem{{ID: "2", URL: "u2"}}
		p.deckCursor = 1
		p.cachedSnapshot = &PhotoSnapshot{}
		if err := p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": "https://example.com/album"}, InitOptions{}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}
		if p.deck != nil || p.deckCursor != 0 || p.cachedSnapshot != nil {
			t.Errorf("Init did not reset deck: deck=%v, cursor=%d, cached=%v", p.deck, p.deckCursor, p.cachedSnapshot)
		}
	})

	t.Run("IngestPushedPhotos", func(t *testing.T) {
		t.Parallel()
		pShare := NewPhotosProvider()
		_ = pShare.Init(context.Background(), map[string]any{"source": "share_url", "album_url": "https://photos.app.goo.gl/share"}, InitOptions{})
		if _, err := pShare.IngestPushedPhotos([]string{"https://lh3.googleusercontent.com/pw/VALID"}, "Test"); !errors.Is(err, ErrPhotoPushNotSupported) {
			t.Fatalf("expected ErrPhotoPushNotSupported on share_url provider, got %v", err)
		}

		pEmber := NewPhotosProvider()
		_ = pEmber.Init(context.Background(), map[string]any{"source": "ember_bridge", "album_url": "https://photos.app.goo.gl/ember"}, InitOptions{})

		if _, err := pEmber.IngestPushedPhotos(nil, "Empty"); !errors.Is(err, ErrInvalidPhotoPushPayload) {
			t.Fatalf("expected ErrInvalidPhotoPushPayload on empty photos, got %v", err)
		}
		if _, err := pEmber.IngestPushedPhotos([]string{"https://notgoogle.com/pic.jpg"}, "BadURL"); !errors.Is(err, ErrInvalidPhotoPushPayload) {
			t.Fatalf("expected ErrInvalidPhotoPushPayload on bad url, got %v", err)
		}

		validURLs := []string{
			"https://lh3.googleusercontent.com/pw/VALID1=w1024-h768",
			"https://lh3.googleusercontent.com/pw/VALID1",
			"https://lh4.googleusercontent.com/pw/VALID2",
		}
		snap, err := pEmber.IngestPushedPhotos(validURLs, "Custom Album")
		if err != nil {
			t.Fatalf("IngestPushedPhotos failed: %v", err)
		}
		if snap.AlbumName != "Custom Album" || snap.TotalPhotos != 2 || len(snap.Photos) != 2 {
			t.Fatalf("unexpected snapshot: %+v", snap)
		}
		if snap.AlbumURL != "https://photos.app.goo.gl/ember" {
			t.Fatalf("unexpected AlbumURL: %s", snap.AlbumURL)
		}

		// Empty album name preserves cached album name
		snap2, err := pEmber.IngestPushedPhotos([]string{"https://lh3.googleusercontent.com/pw/VALID3"}, "  ")
		if err != nil {
			t.Fatalf("second push failed: %v", err)
		}
		if snap2.AlbumName != "Custom Album" || snap2.TotalPhotos != 1 {
			t.Fatalf("expected preserved album name 'Custom Album', got %q", snap2.AlbumName)
		}

		// Mixed provider with share_url and ember_bridge
		pMixed := NewPhotosProvider()
		errMixed := pMixed.Init(context.Background(), map[string]any{
			"albums": []any{
				map[string]any{"name": "Shared 1", "source": "share_url", "url": "https://photos.app.goo.gl/share1"},
				map[string]any{"name": "Pushed 1", "source": "ember_bridge", "url": "https://photos.app.goo.gl/ember1"},
			},
		}, InitOptions{})
		if errMixed != nil {
			t.Fatalf("Init mixed provider failed: %v", errMixed)
		}
		pMixed.albums[0].photos = []PhotoItem{{ID: "share_p1", URL: "https://lh3.googleusercontent.com/pw/SHARE1"}}
		pMixed.albums[0].hydrated = true

		snapMixed, err := pMixed.IngestPushedPhotos([]string{"https://lh3.googleusercontent.com/pw/PUSHED1"}, "")
		if err != nil {
			t.Fatalf("IngestPushedPhotos on mixed provider failed: %v", err)
		}
		if snapMixed.TotalPhotos != 2 {
			t.Fatalf("expected TotalPhotos 2 in mixed provider, got %d", snapMixed.TotalPhotos)
		}
		if snapMixed.AlbumName != "Shared 1, Pushed 1" {
			t.Fatalf("expected AlbumName 'Shared 1, Pushed 1', got %q", snapMixed.AlbumName)
		}
	})

	t.Run("provider methods and url resolution", func(t *testing.T) {
		t.Parallel()
		p := NewPhotosProvider()
		if p.Source() != "" {
			t.Errorf("expected empty initial source, got %q", p.Source())
		}
		if err := p.Subscribe(context.Background(), nil); err != nil {
			t.Errorf("expected nil from Subscribe, got %v", err)
		}
		if _, err := p.Fetch(context.Background()); err == nil {
			t.Error("expected error from uninitialized Fetch")
		}
		pEmber := NewPhotosProvider()
		pEmber.cfg = PhotosConfig{Source: PhotoSourceEmberBridge}
		if _, err := pEmber.Fetch(context.Background()); err == nil {
			t.Error("expected error from uninitialized ember_bridge Fetch")
		}

		if _, err := resolveSingleAlbumURL(0, AlbumSource{URLEnv: "UNSET_VAR_XYZ"}, false, InitOptions{}); err == nil {
			t.Error("expected unset env error")
		}
		if _, err := resolveSingleAlbumURL(0, AlbumSource{URL: "not-a-valid-url"}, false, InitOptions{}); err == nil {
			t.Error("expected invalid url error")
		}
		resolved, err := resolveSingleAlbumURL(0, AlbumSource{URLEnv: "SECRET_VAR"}, false, InitOptions{Secrets: map[string]string{"SECRET_VAR": "https://photos.app.goo.gl/secret"}})
		if err != nil || resolved != "https://photos.app.goo.gl/secret" {
			t.Errorf("unexpected secret resolution: %s, %v", resolved, err)
		}
	})

	t.Run("photoDedupeKey and deduplication", func(t *testing.T) {
		t.Parallel()
		tests := []struct {
			name string
			item PhotoItem
			want string
		}{
			{"clean_url_preferred", PhotoItem{ID: "photo_1", URL: "https://lh3.googleusercontent.com/pw/ABC=w100"}, "https://lh3.googleusercontent.com/pw/ABC"},
			{"empty_url_custom_id", PhotoItem{ID: "custom_uuid", URL: ""}, "custom_uuid"},
			{"empty_url_positional_id", PhotoItem{ID: "photo_99", URL: ""}, "photo_99"},
		}
		for _, tc := range tests {
			if got := photoDedupeKey(tc.item); got != tc.want {
				t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
			}
		}
	})

	t.Run("resolveSnapshotAlbumName and URLs", func(t *testing.T) {
		t.Parallel()
		if got := resolveSnapshotAlbumName(nil); got != "Shared Album" {
			t.Errorf("got %q, want Shared Album", got)
		}
		emptyNames := []*albumState{
			{cfg: AlbumSource{Name: "  "}},
		}
		if got := resolveSnapshotAlbumName(emptyNames); got != "Shared Album" {
			t.Errorf("got %q, want Shared Album", got)
		}
		dupes := []*albumState{
			{cfg: AlbumSource{Name: "Vacation"}},
			{cfg: AlbumSource{Name: "Vacation"}},
			{cfg: AlbumSource{Name: "Family"}},
		}
		if got := resolveSnapshotAlbumName(dupes); got != "Vacation, Family" {
			t.Errorf("got %q, want Vacation, Family", got)
		}
		if got := resolveSnapshotAlbumURL(nil); got != "" {
			t.Errorf("got %q, want empty", got)
		}
	})

	t.Run("peekWindowLocked edge cases", func(t *testing.T) {
		t.Parallel()
		p := NewPhotosProvider()
		p.albums = []*albumState{
			{
				cfg: AlbumSource{Name: "A1"},
				photos: []PhotoItem{
					{ID: "1", URL: "u1"},
					{ID: "2", URL: "u2"},
				},
			},
		}
		// Empty deck
		win := p.peekWindowLocked()
		if len(win) != 2 {
			t.Fatalf("expected 2 photos, got %d", len(win))
		}

		// Populated deck with wrap-around
		p.deck = []PhotoItem{
			{ID: "1", URL: "u1"},
			{ID: "2", URL: "u2"},
			{ID: "3", URL: "u3"},
		}
		p.deckCursor = 2
		win2 := p.peekWindowLocked()
		if len(win2) != 3 {
			t.Fatalf("expected 3 photos from peekWindowLocked, got %d", len(win2))
		}
	})

	t.Run("diskCache error handling", func(t *testing.T) {
		t.Parallel()
		if _, err := loadAlbumDiskCache("", "share_url", "http://example.com"); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("expected ErrNotExist, got %v", err)
		}
		tmp := t.TempDir()
		badJSONFile := filepath.Join(tmp, AlbumCacheFilename("share_url", "http://example.com"))
		_ = os.WriteFile(badJSONFile, []byte("invalid json"), 0o600)
		if _, err := loadAlbumDiskCache(tmp, "share_url", "http://example.com"); err == nil {
			t.Error("expected unmarshal error on corrupt cache")
		}

		writeAlbumDiskCache("", "share_url", "http://example.com", nil, nil)
		writeAlbumDiskCache("/dev/null/cannot/create", "share_url", "http://example.com", &albumDiskCache{}, nil)
	})

	t.Run("albumsList parsing errors", func(t *testing.T) {
		t.Parallel()
		if _, err := parseAlbumsList([]any{"not-a-map"}); err == nil {
			t.Error("expected error for non-map album")
		}
		if _, err := parseAlbumsList([]any{map[string]any{"name": "   "}}); err == nil {
			t.Error("expected error for empty name")
		}
		if _, err := parseAlbumsList([]any{map[string]any{"name": "Alb", "source": "invalid"}}); err == nil {
			t.Error("expected error for invalid source")
		}
		if _, err := parseAlbumsList([]any{map[string]any{"name": "Alb", "url": "https://p.app/1", "url_env": "E"}}); err == nil {
			t.Error("expected error for both url and url_env")
		}
		if _, err := parseAlbumsList([]any{map[string]any{"name": "Alb", "source": "share_url"}}); err == nil {
			t.Error("expected error for neither url nor url_env")
		}
		if _, err := parseAlbumsList([]any{map[string]any{"name": "Alb"}}); err == nil {
			t.Error("expected error for album without source and url")
		}
		if _, err := parseAlbumsList(nil); err == nil {
			t.Error("expected error for nil raw albums")
		}
	})

	t.Run("resolveAlbumSources errors", func(t *testing.T) {
		t.Parallel()
		if _, err := resolveAlbumSources(PhotosConfig{}, InitOptions{}); err == nil {
			t.Error("expected error for empty albums")
		}
		badEnvCfg := PhotosConfig{
			Albums: []AlbumSource{
				{Name: "Alb", Source: "share_url", URLEnv: "UNSET_ALBUM_ENV"},
			},
		}
		if _, err := resolveAlbumSources(badEnvCfg, InitOptions{}); err == nil {
			t.Error("expected error for unset album url_env")
		}
	})

	t.Run("photos provider additional edge cases", func(t *testing.T) {
		t.Parallel()
		p := NewPhotosProvider()
		if err := p.Sync(context.Background(), true); err == nil {
			t.Error("expected error from uninitialized Sync")
		}
		pBad := NewPhotosProvider()
		_ = pBad.Init(context.Background(), map[string]any{"source": "share_url", "album_url": "http://127.0.0.1:1"}, InitOptions{})
		if err := pBad.Sync(context.Background(), true); err == nil {
			t.Error("expected error from failing Sync on unhydrated album")
		}

		pDeck := NewPhotosProvider()
		items := make([]PhotoItem, 5)
		for i := range 5 {
			items[i] = PhotoItem{ID: fmt.Sprintf("id_%d", i), URL: fmt.Sprintf("https://lh3.googleusercontent.com/pw/%d", i)}
		}
		pDeck.deck = slices.Clone(items)
		pDeck.deckCursor = -5
		_ = pDeck.selectAlbumWindowLocked(items)
		pDeck.deckCursor = len(pDeck.deck) - 1
		_ = pDeck.selectAlbumWindowLocked(items)

		if _, err := resolveSingleAlbumURL(0, AlbumSource{URL: "http://[::1]:namedport"}, false, InitOptions{}); err == nil {
			t.Error("expected invalid url error for malformed url")
		}
		if _, err := resolveSingleAlbumURL(0, AlbumSource{}, false, InitOptions{}); err == nil {
			t.Error("expected empty url error")
		}

		// resolveSingleAlbumURL legacy secret checks
		u, err := resolveSingleAlbumURL(0, AlbumSource{URL: "https://example.com/a"}, true, InitOptions{Secrets: map[string]string{"album_url": "https://example.com/secret_album"}})
		if err != nil || u != "https://example.com/secret_album" {
			t.Errorf("expected album_url override, got %v, %v", u, err)
		}
		u, err = resolveSingleAlbumURL(0, AlbumSource{URL: "https://example.com/a"}, true, InitOptions{Secrets: map[string]string{"share_url": "https://example.com/secret_share"}})
		if err != nil || u != "https://example.com/secret_share" {
			t.Errorf("expected share_url override, got %v, %v", u, err)
		}
		u, err = resolveSingleAlbumURL(0, AlbumSource{URLEnv: "TEST_KEY"}, false, InitOptions{Secrets: map[string]string{"TEST_KEY": "https://example.com/env"}})
		if err != nil || u != "https://example.com/env" {
			t.Errorf("expected env secret url, got %v, %v", u, err)
		}
		if _, err := resolveSingleAlbumURL(0, AlbumSource{URLEnv: "NON_EXISTENT_VAR"}, false, InitOptions{}); err == nil {
			t.Error("expected error for non-existent env var")
		}

		// resolveSingleAlbumURL secret indexed
		u, err = resolveSingleAlbumURL(2, AlbumSource{Name: "Alb", Source: "share_url", URLEnv: "SOME_ENV"}, false, InitOptions{Secrets: map[string]string{"albums[2].url": "https://example.com/indexed"}})
		if err != nil || u != "https://example.com/indexed" {
			t.Errorf("expected indexed secret, got %v, %v", u, err)
		}
		u, err = resolveSingleAlbumURL(0, AlbumSource{Name: "Shared Album", Source: "share_url", URL: "https://example.com/a"}, true, InitOptions{Secrets: map[string]string{"share_url": "https://example.com/legacy_share"}})
		if err != nil || u != "https://example.com/legacy_share" {
			t.Errorf("expected legacy share_url secret, got %v, %v", u, err)
		}

		// peekWindowLocked branches
		pEmpty := NewPhotosProvider()
		pEmpty.albums = []*albumState{{
			photos: make([]PhotoItem, 600),
		}}
		for i := range 600 {
			pEmpty.albums[0].photos[i] = PhotoItem{ID: fmt.Sprintf("p_%d", i), URL: fmt.Sprintf("https://lh3.googleusercontent.com/pw/%d", i)}
		}
		peeked := pEmpty.peekWindowLocked()
		if len(peeked) != 500 {
			t.Errorf("expected 500 items from peekWindowLocked, got %d", len(peeked))
		}

		pWrap := NewPhotosProvider()
		pWrap.deck = make([]PhotoItem, 10)
		for i := range 10 {
			pWrap.deck[i] = PhotoItem{ID: fmt.Sprintf("wrap_%d", i), URL: fmt.Sprintf("https://lh3.googleusercontent.com/pw/wrap/%d", i)}
		}
		pWrap.deckCursor = 20
		peekedWrap := pWrap.peekWindowLocked()
		if len(peekedWrap) != 10 {
			t.Errorf("expected 10 items from wrapped peekWindowLocked, got %d", len(peekedWrap))
		}
		pWrap.deckCursor = 7
		peekedWrap = pWrap.peekWindowLocked()
		if len(peekedWrap) != 10 {
			t.Errorf("expected 10 items from sliced wrapped peekWindowLocked, got %d", len(peekedWrap))
		}

		snapEmpty := pWrap.buildSnapshotLocked(nil)
		if len(snapEmpty.Photos) != 0 {
			t.Errorf("expected 0 photos since pWrap has empty albums, got %d", len(snapEmpty.Photos))
		}

		writeDir := t.TempDir()
		roDir := filepath.Join(writeDir, "readonly")
		_ = os.Mkdir(roDir, 0o500)
		writeAlbumDiskCache(roDir, "share_url", "https://example.com/ro", &albumDiskCache{AlbumName: "test"}, nil)

		// Source and isAllEmberBridge locked branches
		pMixed := NewPhotosProvider()
		pMixed.albums = []*albumState{
			{cfg: AlbumSource{Source: "other_source"}},
		}
		if s := pMixed.Source(); s != "" {
			t.Errorf("expected empty string for unknown source, got %q", s)
		}
		pNoAlbums := NewPhotosProvider()
		if pNoAlbums.isAllEmberBridgeLocked() {
			t.Error("expected false for empty albums")
		}

		// buildSnapshotLocked with empty window and non-empty total
		pSnapFallback := NewPhotosProvider()
		pSnapFallback.albums = []*albumState{
			{
				cfg: AlbumSource{Name: "Shared Album"},
				photos: []PhotoItem{
					{ID: "p1", URL: "https://lh3.googleusercontent.com/pw/1"},
				},
			},
		}
		snapFallback := pSnapFallback.buildSnapshotLocked(nil)
		if len(snapFallback.Photos) != 1 || snapFallback.AlbumName != "Shared Album" {
			t.Errorf("expected 1 photo and Shared Album name, got %+v", snapFallback)
		}

		// extractAlbumKeyAndToken branches
		k, tok := extractAlbumKeyAndToken("AF_initDataCallback({key: '1', some_other_field: 'val'});")
		if k != "" || tok != "" {
			t.Errorf("expected empty, got %s, %s", k, tok)
		}
		k, tok = extractAlbumKeyAndToken("AF_initDataCallback({data: [invalid json, foo]});")
		if k != "" || tok != "" {
			t.Errorf("expected empty, got %s, %s", k, tok)
		}
		k, tok = extractAlbumKeyAndToken(`AF_initDataCallback({key: 'ds:1', data: [null, [], "tok_next", ["AF1Qip_album_meta_key", "Vacation"]]});`)
		if k != "AF1Qip_album_meta_key" || tok != "tok_next" {
			t.Errorf("expected AF1Qip_album_meta_key and tok_next, got %q, %q", k, tok)
		}

		// parseBatchexecuteResponse multi-line chunk framing
		multiLineSnAcKc := []byte(")]}'\n\n120\n[[[\"wrb.fr\",\"snAcKc\",\"[null,[[\\\"p_ml\\\",[\\\"https://lh3.googleusercontent.com/pw/ML1\\\",1000,800],1755268100000]],\\\"tok_ml2\\\"]\",null,null,null,\"generic\"]]]\n")
		mlPhotos, mlNext, err := parseBatchexecuteResponse(multiLineSnAcKc, "tok_ml1")
		if err != nil || len(mlPhotos) != 1 || mlNext != "tok_ml2" {
			t.Errorf("unexpected multi-line snAcKc parse: photos=%v, next=%q, err=%v", mlPhotos, mlNext, err)
		}

		// parseBatchexecuteResponse with only videos should return 0 photos and next token without falling back to regex
		videoOnlySnAcKc := []byte(")]}'\n\n120\n[[[\"wrb.fr\",\"snAcKc\",\"[null,[[\\\"v_ml\\\",[\\\"https://lh3.googleusercontent.com/pw/VID1\\\",1000,800],1755268100000,[\\\"video/mp4\\\",30000]]],\\\"tok_next_page\\\"]\",null,null,null,\"generic\"]]]\n")
		vPhotos, vNext, err := parseBatchexecuteResponse(videoOnlySnAcKc, "tok_ml1")
		if err != nil || len(vPhotos) != 0 || vNext != "tok_next_page" {
			t.Errorf("expected 0 photos and next token from video-only snAcKc, got photos=%v, next=%q, err=%v", vPhotos, vNext, err)
		}

		// fetchBatchexecutePage network and status branches
		_, _, err = fetchBatchexecutePage(context.Background(), http.DefaultClient, "http://127.0.0.1:1/photos", "key", "token", "auth")
		if err == nil {
			t.Error("expected dial error from fetchBatchexecutePage")
		}
		srv500 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "server error", http.StatusInternalServerError)
		}))
		defer srv500.Close()
		_, _, err = fetchBatchexecutePage(context.Background(), srv500.Client(), srv500.URL, "key", "token", "auth")
		if err == nil {
			t.Error("expected error for HTTP 500 from batchexecute")
		}

		// Init with explicit cache_dir
		pCustomCache := NewPhotosProvider()
		tmpDir := t.TempDir()
		err = pCustomCache.Init(context.Background(), map[string]any{
			"source":    "ember_bridge",
			"album_url": "https://photos.app.goo.gl/custom",
			"cache_dir": tmpDir,
		}, InitOptions{})
		if err != nil || pCustomCache.effectiveCacheDir != tmpDir {
			t.Errorf("expected effectiveCacheDir %s, got %s", tmpDir, pCustomCache.effectiveCacheDir)
		}

		if c := cleanGooglePhotoURL("https://example.com/not-lh"); c != "https://example.com/not-lh" {
			t.Errorf("expected untouched URL, got %s", c)
		}

		// checkWarmBootStaleLocked page1 stale only vs unpaginated / unchecked location backfill
		pStale := NewPhotosProvider()
		pStale.albums = []*albumState{
			{
				cfg:           AlbumSource{Source: PhotoSourceShareURL},
				lastFullSync:  time.Now(),
				lastPage1Sync: time.Now().Add(-2 * time.Hour),
				paginated:     true,
				photos:        []PhotoItem{{ID: "AF1Qip_ok", LocationChecked: true}},
			},
		}
		isStale, needsFull := pStale.checkWarmBootStaleLocked(time.Now())
		if !isStale || needsFull {
			t.Errorf("expected isStale=true, needsFull=false, got %v, %v", isStale, needsFull)
		}
		pStale.albums[0].lastPage1Sync = time.Now()
		pStale.albums[0].paginated = false
		if isStale, needsFull = pStale.checkWarmBootStaleLocked(time.Now()); !isStale || !needsFull {
			t.Errorf("expected isStale=true, needsFull=true for unpaginated cache backfill, got %v, %v", isStale, needsFull)
		}
		pStale.albums[0].paginated = true
		pStale.albums[0].photos = []PhotoItem{{ID: "AF1Qip_unchecked", LocationChecked: false}}
		if isStale, needsFull = pStale.checkWarmBootStaleLocked(time.Now()); !isStale || !needsFull {
			t.Errorf("expected isStale=true, needsFull=true for unchecked photo backfill, got %v, %v", isStale, needsFull)
		}
		pStale.albums[0].lastFullSync = time.Now()
		pStale.albums[0].paginated = false
		if !pStale.checkPeriodicFullLocked(time.Now()) {
			t.Error("expected checkPeriodicFullLocked=true for unpaginated album")
		}
		pStale.albums[0].paginated = true
		if pStale.checkPeriodicFullLocked(time.Now()) {
			t.Error("expected checkPeriodicFullLocked=false when fresh and paginated")
		}
		if !pStale.checkPeriodicFullLocked(time.Now().Add(25 * time.Hour)) {
			t.Error("expected checkPeriodicFullLocked=true after fullSyncInterval")
		}

		pEmberNilSnap := NewPhotosProvider()
		pEmberNilSnap.albums = []*albumState{{cfg: AlbumSource{Source: PhotoSourceEmberBridge}}}
		if _, err := pEmberNilSnap.Fetch(context.Background()); err == nil {
			t.Error("expected error for ember_bridge with nil cachedSnapshot")
		}

		if _, err := resolveSingleAlbumURL(0, AlbumSource{URL: "http://[::1]:namedport"}, false, InitOptions{}); err == nil {
			t.Error("expected error for invalid URL")
		}

		// Additional provider coverage
		err404 := &HTTPStatusError{Code: 404}
		if s := err404.Error(); s != "HTTP 404" {
			t.Errorf("got %q, want 'HTTP 404'", s)
		}
		if code := err404.StatusCode(); code != 404 {
			t.Errorf("got code %d, want 404", code)
		}

		ws := WeatherSnapshot{Current: WeatherCurrent{PrecipProbMax: 55}}
		if prob := ws.TodayPrecipProbMax(); prob != 55 {
			t.Errorf("got %d, want 55", prob)
		}

		calEvents := []CalendarEvent{
			{Title: "Timed B", Start: "2026-11-02T12:00:00Z", End: "2026-11-02T13:00:00Z"},
			{Title: "AllDay A", Start: "20261102", End: "20261103", AllDay: true},
			{Title: "Timed A", Start: "20260102T150405", End: "20260102T160405"},
			{Title: "Invalid", Start: "not-a-date", End: "not-a-date"},
			{Title: "Empty", Start: "", End: ""},
		}
		SortCalendarEvents(calEvents, nil)

		// writeAlbumDiskCache and loadAlbumDiskCache full coverage
		logger := slog.Default()
		writeAlbumDiskCache("/dev/null/cannot/create", "share_url", "http://example.com", &albumDiskCache{}, logger)
		writeAlbumDiskCache(roDir, "share_url", "http://example.com", &albumDiskCache{}, logger)
		blockedFile := filepath.Join(writeDir, albumCacheFilename("share_url", "http://example.com/blocked"))
		_ = os.Mkdir(blockedFile, 0o755)
		writeAlbumDiskCache(writeDir, "share_url", "http://example.com/blocked", &albumDiskCache{AlbumName: "blocked"}, logger)

		validDir := filepath.Join(writeDir, "valid")
		validCache := &albumDiskCache{AlbumName: "Saved", Photos: []PhotoItem{{ID: "1", URL: "https://lh3.googleusercontent.com/pw/1"}}}
		writeAlbumDiskCache(validDir, "share_url", "http://example.com/valid", validCache, logger)
		loaded, err := loadAlbumDiskCache(validDir, "share_url", "http://example.com/valid")
		if err != nil || loaded == nil || loaded.AlbumName != "Saved" {
			t.Fatalf("expected loaded cache, got %+v, %v", loaded, err)
		}

		// fetchBatchexecutePage ceiling error
		srvCeiling := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/plain")
			chunk := make([]byte, 1024*1024)
			for range 6 {
				_, _ = w.Write(chunk)
			}
		}))
		defer srvCeiling.Close()
		_, _, err = fetchBatchexecutePage(context.Background(), srvCeiling.Client(), srvCeiling.URL, "key", "token", "auth")
		if err == nil || !strings.Contains(err.Error(), "exceeded") {
			t.Fatalf("expected payload ceiling error from fetchBatchexecutePage, got %v", err)
		}

		// resolveAlbumSources secret resolution for single album named Shared Album
		singleCfg := PhotosConfig{
			Albums: []AlbumSource{
				{Name: "Shared Album", Source: "share_url", URLEnv: "SOME_UNSET_ENV"},
			},
		}
		singleResolved, err := resolveAlbumSources(singleCfg, InitOptions{
			Secrets: map[string]string{
				"albums[0].url": "https://photos.app.goo.gl/indexed_shared_secret",
			},
		})
		if err != nil || len(singleResolved) != 1 || singleResolved[0].ResolvedURL != "https://photos.app.goo.gl/indexed_shared_secret" {
			t.Fatalf("expected albums[0].url secret resolution for single album named Shared Album, got %+v, %v", singleResolved, err)
		}

		// concurrent Fetch and Sync race-free test
		srvRace := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/html")
			_, _ = w.Write([]byte(`<html><head><title>Test Album - Google Photos</title></head><body><script>AF_initDataCallback({data: [null, [["p1", ["https://lh3.googleusercontent.com/pw/CONCURRENT1", 100, 100], 12345]]]})</script></body></html>`))
		}))
		defer srvRace.Close()

		pRace := NewPhotosProvider()
		if err := pRace.Init(context.Background(), map[string]any{
			"albums": []any{
				map[string]any{"name": "Album 1", "source": "share_url", "url": srvRace.URL},
			},
		}, InitOptions{}); err != nil {
			t.Fatalf("Init pRace failed: %v", err)
		}
		// Hydrate once first so Fetch and background Sync run concurrently
		if err := pRace.Sync(context.Background(), true); err != nil {
			t.Fatalf("initial Sync failed: %v", err)
		}

		ctxRace, cancelRace := context.WithTimeout(context.Background(), 500*time.Millisecond)
		defer cancelRace()

		var wg sync.WaitGroup
		for range 3 {
			wg.Add(2)
			go func() {
				defer wg.Done()
				for ctxRace.Err() == nil {
					_, _ = pRace.Fetch(ctxRace)
				}
			}()
			go func() {
				defer wg.Done()
				for ctxRace.Err() == nil {
					_ = pRace.Sync(ctxRace, false)
				}
			}()
		}
		wg.Wait()
	})
}

func (p *PhotosProvider) selectAlbumWindow(scraped []PhotoItem) []PhotoItem {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.selectAlbumWindowLocked(scraped)
}
