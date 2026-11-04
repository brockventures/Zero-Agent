package provider_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

const (
	mockPhotosAFHTML            = `<!DOCTYPE html><html><head><meta property="og:title" content="Family Vacation 2026 - Google Photos"><title>Family Vacation 2026 - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["photo_1",["https://lh3.googleusercontent.com/pw/AL9nZEW1=w1024-h768",1200,800],1755268200000],["photo_2",["https://lh3.googleusercontent.com/pw/AL9nZEW2",1920,1080],1755354600000],["photo_3",["https://lh3.googleusercontent.com/pw/AL9nZEW3=s0",1000,1000],1755441000000]]],sideChannel:{}});</script></body></html>`
	mockPhotosWithVideoHTML     = `<!DOCTYPE html><html><head><meta property="og:title" content="Family Vacation 2026 - Google Photos"><title>Family Vacation 2026 - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["photo_1",["https://lh3.googleusercontent.com/pw/AL9nZEW1=w1024-h768",1200,800],1755268200000],["video_1",["https://lh3.googleusercontent.com/pw/AL9nZEVideo",1920,1080],1755354600000,["video/mp4",30000]],["photo_2",["https://lh3.googleusercontent.com/pw/AL9nZEW2",1920,1080],1755441000000]]],sideChannel:{}});</script></body></html>`
	mockPhotosMajorityVideoHTML = `<!DOCTYPE html><html><head><meta property="og:title" content="Family Vacation 2026 - Google Photos"><title>Family Vacation 2026 - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["photo_1",["https://lh3.googleusercontent.com/pw/AL9nZEW1=w1024-h768",1200,800],1755268200000],["video_1",["https://lh3.googleusercontent.com/pw/AL9nZEVideo1",1920,1080],1755354600000,["video/mp4",30000]],["video_2",["https://lh3.googleusercontent.com/pw/AL9nZEVideo2",1920,1080],1755354700000,["video/mp4",30000]],["video_3",["https://lh3.googleusercontent.com/pw/AL9nZEVideo3",1920,1080],1755354800000,["video/mp4",30000]]]],sideChannel:{}});</script></body></html>`
	mockPhotosAllVideoHTML      = `<!DOCTYPE html><html><head><meta property="og:title" content="Video Only Album - Google Photos"><title>Video Only Album - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["video_1",["https://lh3.googleusercontent.com/pw/AL9nZEVideo1",1920,1080],1755354600000,["video/mp4",30000]],["video_2",["https://lh3.googleusercontent.com/pw/AL9nZEVideo2",1920,1080],1755354700000,["video/mp4",30000]]]],sideChannel:{}});</script></body></html>`
	mockPhotosSameDayHTML       = `<!DOCTYPE html><html><head><meta property="og:title" content="Family Vacation 2026 - Google Photos"></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["photo_1",["https://lh3.googleusercontent.com/pw/AL9nZEW1=w1024-h768",1200,800],1755268200000],["photo_2",["https://lh3.googleusercontent.com/pw/AL9nZEW2",1920,1080],1755268300000],["photo_3",["https://lh3.googleusercontent.com/pw/AL9nZEW3=s0",1000,1000],1755268400000]]],sideChannel:{}});</script></body></html>`
	mockPhotosFuncHTML          = `<!DOCTYPE html><html><head><title>Summer Roadtrip - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:function(){return ["key",[["img_a",["https://lh3.googleusercontent.com/pw/ROADA",800,600],1755268200000]]]},sideChannel:{}});</script></body></html>`
	mockPhotosFallbackHTML      = `<!DOCTYPE html><html><head><meta property="og:title" content="Art Showcase"></head><body><div><img src="https://lh3.googleusercontent.com/pw/ART_ITEM_1=w500-h300"/><img src="https://lh4.googleusercontent.com/pw/ART_ITEM_2"/><img src="https://lh3.googleusercontent.com/pw/ART_ITEM_1=w100"/><img src="https://lh3.googleusercontent.com/a/AVATAR_IGNORED"/></div></body></html>`
)

func TestPhotosProvider_FetchExtraction(t *testing.T) {
	t.Parallel()
	var tenPhotos strings.Builder
	tenPhotos.WriteString("<html><body>")
	for i := 1; i <= 10; i++ {
		fmt.Fprintf(&tenPhotos, `<img src="https://lh3.googleusercontent.com/pw/PHOTO_%d"/>`, i)
	}
	tenPhotos.WriteString("</body></html>")

	tests := []struct {
		name             string
		html             string
		wantAlbum        string
		wantTotal        int
		wantPhotos       int
		wantFirstID      string
		wantFirstURL     string
		wantFirstAspect  float64
		wantSecondAspect float64
		wantThirdAspect  float64
	}{
		{"af_callback", mockPhotosAFHTML, "Family Vacation 2026", 3, 3, "photo_1", "https://lh3.googleusercontent.com/pw/AL9nZEW1", 1.5, 1.78, 1.0},
		{"filters_video", mockPhotosWithVideoHTML, "Family Vacation 2026", 2, 2, "photo_1", "https://lh3.googleusercontent.com/pw/AL9nZEW1", 1.5, 1.78, 0},
		{"majority_video", mockPhotosMajorityVideoHTML, "Family Vacation 2026", 1, 1, "photo_1", "https://lh3.googleusercontent.com/pw/AL9nZEW1", 1.5, 0, 0},
		{"all_video_no_regex_leak", mockPhotosAllVideoHTML, "Video Only Album", 0, 0, "", "", 0, 0, 0},
		{"same_day_burst", mockPhotosSameDayHTML, "Family Vacation 2026", 3, 1, "", "", 0, 0, 0},
		{"func_wrapper", mockPhotosFuncHTML, "Summer Roadtrip", 1, 1, "img_a", "", 0, 0, 0},
		{"fallback_regex", mockPhotosFallbackHTML, "Art Showcase", 2, 2, "", "", 0, 0, 0},
		{"ten_photos", tenPhotos.String(), "Shared Album", 10, 10, "", "", 0, 0, 0},
		{"empty_album", "<html><body><div>Empty Album</div></body></html>", "Shared Album", 0, 0, "", "", 0, 0, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "text/html; charset=utf-8")
				_, _ = w.Write([]byte(tc.html))
			}))
			defer ts.Close()

			cfg := map[string]any{"source": "share_url", "album_url": ts.URL}
			p := provider.NewPhotosProvider()
			if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
				t.Fatalf("Init failed: %v", err)
			}
			res, err := p.Fetch(context.Background())
			if err != nil {
				t.Fatalf("Fetch failed: %v", err)
			}
			snap := res.(*provider.PhotoSnapshot)
			if snap.AlbumName != tc.wantAlbum || snap.TotalPhotos != tc.wantTotal || len(snap.Photos) != tc.wantPhotos {
				t.Fatalf("got album=%q total=%d photos=%d; want %q, %d, %d", snap.AlbumName, snap.TotalPhotos, len(snap.Photos), tc.wantAlbum, tc.wantTotal, tc.wantPhotos)
			}
			if snap.AlbumURL != ts.URL {
				t.Errorf("album URL = %q, want %q", snap.AlbumURL, ts.URL)
			}
			photoMap := make(map[string]provider.PhotoItem, len(snap.Photos))
			for _, item := range snap.Photos {
				photoMap[item.ID] = item
			}
			if tc.wantFirstID != "" {
				item, ok := photoMap[tc.wantFirstID]
				if !ok {
					t.Fatalf("item %q not found in photos", tc.wantFirstID)
				}
				if tc.wantFirstURL != "" && item.URL != tc.wantFirstURL {
					t.Errorf("item URL = %q, want %q", item.URL, tc.wantFirstURL)
				}
				if tc.wantFirstAspect != 0 && item.AspectRatio != tc.wantFirstAspect {
					t.Errorf("item aspect = %v, want %v", item.AspectRatio, tc.wantFirstAspect)
				}
				if tc.name == "af_callback" && item.Timestamp == "" {
					t.Error("expected non-empty RFC3339 timestamp")
				}
			}
			if tc.wantSecondAspect != 0 {
				item, ok := photoMap["photo_2"]
				if !ok || item.AspectRatio != tc.wantSecondAspect {
					t.Errorf("photo_2 aspect = %v, want %v", item.AspectRatio, tc.wantSecondAspect)
				}
			}
			if tc.wantThirdAspect != 0 {
				item, ok := photoMap["photo_3"]
				if !ok || item.AspectRatio != tc.wantThirdAspect {
					t.Errorf("photo_3 aspect = %v, want %v", item.AspectRatio, tc.wantThirdAspect)
				}
			}
		})
	}
}

func TestPhotosProvider_ErrorsAndSWR(t *testing.T) {
	t.Parallel()

	t.Run("redirects and auth", func(t *testing.T) {
		t.Parallel()
		for _, tc := range []struct {
			name   string
			status int
			target string
			err    error
		}{
			{"accounts", 302, "https://accounts.google.com/ServiceLogin?continue=https://photos.google.com", provider.ErrAlbumPrivate},
			{"sorry", 302, "https://sorry.google.com/sorry/index", provider.ErrAlbumRateLimit},
			{"consent", 302, "https://consent.google.com/m?continue=https://photos.google.com", provider.ErrAlbumPrivate},
			{"404", 404, "", provider.ErrAlbumNotFound},
			{"403", 403, "", provider.ErrAlbumPrivate},
			{"429", 429, "", provider.ErrAlbumRateLimit},
			{"500", 500, "", errors.New("upstream Google Photos returned HTTP 500")},
		} {
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if tc.target != "" {
					http.Redirect(w, r, tc.target, tc.status)
					return
				}
				w.WriteHeader(tc.status)
			}))
			p := provider.NewPhotosProvider()
			_ = p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": ts.URL}, provider.InitOptions{})
			_, err := p.Fetch(context.Background())
			ts.Close()
			if err == nil || !strings.Contains(err.Error(), tc.err.Error()) {
				t.Errorf("%s: want %v, got %v", tc.name, tc.err, err)
			}
		}
	})

	t.Run("slowloris", func(t *testing.T) {
		t.Parallel()
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html")
			chunk := strings.Repeat("A", 1024*1024)
			for range 6 {
				_, _ = io.WriteString(w, chunk)
			}
		}))
		defer ts.Close()
		p := provider.NewPhotosProvider()
		_ = p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": ts.URL}, provider.InitOptions{})
		_, err := p.Fetch(context.Background())
		if err == nil || !strings.Contains(err.Error(), "google photos payload exceeded") {
			t.Fatalf("expected payload exceeded error, got: %v", err)
		}
	})

	t.Run("swr", func(t *testing.T) {
		t.Parallel()
		var count int
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			count++
			if count == 1 {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`<html><head><title>SWR</title></head><body><img src="https://lh3.googleusercontent.com/pw/SWROK"/></body></html>`))
			} else {
				w.WriteHeader(http.StatusInternalServerError)
			}
		}))
		defer ts.Close()
		p := provider.NewPhotosProvider()
		_ = p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": ts.URL}, provider.InitOptions{})
		res1, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("first fetch failed: %v", err)
		}
		_ = p.Sync(context.Background(), true)
		res2, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("second fetch under SWR failed: %v", err)
		}
		s1, s2 := res1.(*provider.PhotoSnapshot), res2.(*provider.PhotoSnapshot)
		if len(s2.Photos) != 1 || s2.Photos[0].URL != s1.Photos[0].URL {
			t.Fatalf("expected cached snapshot under SWR: %+v", s2)
		}
	})
}

func TestPhotosProvider_InitAndLifecycle(t *testing.T) {
	for _, tc := range []struct {
		name, envKey, envVal string
		cfg                  map[string]any
		opts                 provider.InitOptions
		wantErrSub           string
	}{
		{"missing_source", "", "", map[string]any{"album_url": "https://p.app/1"}, provider.InitOptions{}, "'source' is required"},
		{"invalid_source", "", "", map[string]any{"source": "bad_source", "album_url": "https://p.app/1"}, provider.InitOptions{}, "must be either"},
		{"mutex", "", "", map[string]any{"source": "share_url", "album_url": "https://p.app/1", "album_url_env": "E"}, provider.InitOptions{}, "mutually exclusive"},
		{"neither", "", "", map[string]any{"source": "share_url"}, provider.InitOptions{}, "requires either 'album_url' or 'album_url_env'"},
		{"unset_env", "", "", map[string]any{"source": "share_url", "album_url_env": "MISSING"}, provider.InitOptions{}, "is not set or empty"},
		{"opts_secret", "", "", map[string]any{"source": "share_url", "album_url_env": "S"}, provider.InitOptions{Secrets: map[string]string{"S": "https://p.app/s"}}, ""},
		{"env_var", "TEST_ALBUM_ENV", "https://p.app/env", map[string]any{"source": "share_url", "album_url_env": "TEST_ALBUM_ENV"}, provider.InitOptions{}, ""},
		{"opts_override", "", "", map[string]any{"source": "share_url", "album_url": "https://p.app/raw"}, provider.InitOptions{Secrets: map[string]string{"album_url": "https://p.app/over"}}, ""},
		{"bad_scheme", "", "", map[string]any{"source": "share_url", "album_url": "ftp://photos.google.com/album"}, provider.InitOptions{}, "invalid album URL"},
		{"bad_host", "", "", map[string]any{"source": "share_url", "album_url": "http:///no-host"}, provider.InitOptions{}, "invalid album URL"},
		{"ember_bridge_valid", "", "", map[string]any{"source": "ember_bridge", "album_url": "https://photos.app.goo.gl/ember"}, provider.InitOptions{}, ""},
		{"legacy_share_url", "", "", map[string]any{"share_url": "https://p.app/leg"}, provider.InitOptions{}, ""},
		{"legacy_share_url_env", "LEGACY_ENV", "https://p.app/leg_env", map[string]any{"share_url_env": "LEGACY_ENV"}, provider.InitOptions{}, ""},
		{"legacy_share_url_opts", "", "", map[string]any{"share_url_env": "LEG_S"}, provider.InitOptions{Secrets: map[string]string{"share_url": "https://p.app/sec"}}, ""},
		{"legacy_production_config", "", "", map[string]any{"share_url": "https://photos.app.goo.gl/example-album", "cycle_interval_seconds": 30, "shuffle": true}, provider.InitOptions{}, ""},
	} {
		if tc.envKey != "" {
			t.Setenv(tc.envKey, tc.envVal)
		}
		p := provider.NewPhotosProvider()
		err := p.Init(context.Background(), tc.cfg, tc.opts)
		if tc.wantErrSub != "" {
			if err == nil || !strings.Contains(err.Error(), tc.wantErrSub) {
				t.Errorf("%s: want error %q, got %v", tc.name, tc.wantErrSub, err)
			}
		} else if err != nil {
			t.Errorf("%s: unexpected error: %v", tc.name, err)
		}
	}

	pEmber := provider.NewPhotosProvider()
	if err := pEmber.Init(context.Background(), map[string]any{"source": "ember_bridge", "album_url": "https://photos.app.goo.gl/ember"}, provider.InitOptions{}); err != nil {
		t.Fatalf("Init ember_bridge failed: %v", err)
	}
	if pEmber.Source() != provider.PhotoSourceEmberBridge {
		t.Errorf("got Source=%q, want %q", pEmber.Source(), provider.PhotoSourceEmberBridge)
	}
	resEmber, err := pEmber.Fetch(context.Background())
	if err != nil {
		t.Fatalf("Fetch on ember_bridge failed: %v", err)
	}
	snapEmber := resEmber.(*provider.PhotoSnapshot)
	if snapEmber.AlbumName != "Shared Album" || snapEmber.AlbumURL != "https://photos.app.goo.gl/ember" || snapEmber.TotalPhotos != 0 || len(snapEmber.Photos) != 0 {
		t.Fatalf("unexpected ember snapshot: %+v", snapEmber)
	}

	pUninit := provider.NewPhotosProvider()
	if _, err := pUninit.Fetch(context.Background()); err == nil || !strings.Contains(err.Error(), "provider not initialized") {
		t.Fatalf("expected uninitialized error, got: %v", err)
	}
	tsMal := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`<html><body><script>AF_initDataCallback({key:'ds:1',data:[INVALID JSON...});</script></body></html>`))
	}))
	defer tsMal.Close()
	pMal := provider.NewPhotosProvider()
	_ = pMal.Init(context.Background(), map[string]any{"source": "share_url", "album_url": tsMal.URL}, provider.InitOptions{})
	if res, err := pMal.Fetch(context.Background()); err != nil || len(res.(*provider.PhotoSnapshot).Photos) != 0 {
		t.Fatalf("expected empty photos on malformed JSON, got %v, err=%v", res, err)
	}

	reg := provider.NewRegistry()
	p1, err := reg.Create("photo-carousel")
	if err != nil || p1 == nil {
		t.Fatalf("failed create photo-carousel: %v", err)
	}
	if err := p1.Subscribe(context.Background(), nil); err != nil {
		t.Errorf("Subscribe failed: %v", err)
	}
	if err := p1.Shutdown(context.Background()); err != nil {
		t.Errorf("Shutdown failed: %v", err)
	}
}

func TestPhotosProvider_DeckWindowAndReconcile(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name                string
		pollBatches         [][]string
		callInitMid         bool
		callShutdownMid     bool
		wantCounts          []int
		wantStable          bool
		wantAppended        bool
		wantRemovedExcluded string
		wantNoDupes         bool
	}{
		{
			name: "stable_deck_order_across_polls_when_album_fits_preload",
			pollBatches: [][]string{
				{"p1", "p2", "p3", "p4", "p5"},
				{"p1", "p2", "p3", "p4", "p5"},
			},
			wantCounts:  []int{5, 5},
			wantStable:  true,
			wantNoDupes: true,
		},
		{
			name: "reconcile_appends_new_photos_without_reshuffling_existing",
			pollBatches: [][]string{
				{"p1", "p2", "p3"},
				{"p1", "p2", "p3", "p4"},
			},
			wantCounts:   []int{3, 4},
			wantAppended: true,
			wantNoDupes:  true,
		},
		{
			name: "reconcile_removes_old_photos_cleanly",
			pollBatches: [][]string{
				{"p1", "p2", "p3", "p4"},
				{"p1", "p3", "p4"},
			},
			wantCounts:          []int{4, 3},
			wantRemovedExcluded: "p2",
			wantNoDupes:         true,
		},
		{
			name: "init_resets_deck_state",
			pollBatches: [][]string{
				{"p1", "p2", "p3", "p4"},
				{"p1", "p2", "p3", "p4"},
			},
			callInitMid: true,
			wantCounts:  []int{4, 4},
			wantNoDupes: true,
		},
		{
			name: "shutdown_resets_deck_state",
			pollBatches: [][]string{
				{"p1", "p2", "p3", "p4"},
				{"p1", "p2", "p3", "p4"},
			},
			callShutdownMid: true,
			wantCounts:      []int{4, 4},
			wantNoDupes:     true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var pollIndex atomic.Int32
			ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				idx := int(pollIndex.Load())
				if idx >= len(tc.pollBatches) {
					idx = len(tc.pollBatches) - 1
				}
				batch := tc.pollBatches[idx]
				var b strings.Builder
				b.WriteString("<html><body>")
				for _, id := range batch {
					fmt.Fprintf(&b, `<img src="https://lh3.googleusercontent.com/pw/%s"/>`, id)
				}
				b.WriteString("</body></html>")
				w.Header().Set("Content-Type", "text/html; charset=utf-8")
				_, _ = w.Write([]byte(b.String()))
			}))
			defer ts.Close()

			cfg := map[string]any{
				"source":    "share_url",
				"album_url": ts.URL,
			}
			p := provider.NewPhotosProvider()
			if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
				t.Fatalf("Init failed: %v", err)
			}

			snapshots := make([]*provider.PhotoSnapshot, len(tc.pollBatches))
			for i := range tc.pollBatches {
				pollIndex.Store(int32(i))
				if i == 1 && tc.callInitMid {
					if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
						t.Fatalf("mid Init failed: %v", err)
					}
				}
				if i == 1 && tc.callShutdownMid {
					if err := p.Shutdown(context.Background()); err != nil {
						t.Fatalf("mid Shutdown failed: %v", err)
					}
					if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
						t.Fatalf("post-shutdown Init failed: %v", err)
					}
				}
				if i > 0 && !tc.callInitMid && !tc.callShutdownMid {
					if err := p.Sync(context.Background(), true); err != nil {
						t.Fatalf("poll %d Sync failed: %v", i, err)
					}
				}

				res, err := p.Fetch(context.Background())
				if err != nil {
					t.Fatalf("poll %d Fetch failed: %v", i, err)
				}
				snapshots[i] = res.(*provider.PhotoSnapshot)
			}

			for i, snap := range snapshots {
				if len(snap.Photos) != tc.wantCounts[i] {
					t.Errorf("poll %d photos count = %d, want %d", i, len(snap.Photos), tc.wantCounts[i])
				}
				if tc.wantNoDupes {
					seen := make(map[string]bool, len(snap.Photos))
					for _, photo := range snap.Photos {
						if seen[photo.URL] {
							t.Errorf("poll %d contains duplicate photo URL %q", i, photo.URL)
						}
						seen[photo.URL] = true
					}
				}
			}

			if tc.wantStable && len(snapshots) >= 2 {
				s1, s2 := snapshots[0].Photos, snapshots[1].Photos
				if len(s1) != len(s2) {
					t.Errorf("stable check photo count mismatch: %d vs %d", len(s1), len(s2))
				} else {
					for i := range s1 {
						if s1[i].URL != s2[i].URL {
							t.Errorf("stable check photo[%d] = %q, want %q", i, s2[i].URL, s1[i].URL)
						}
					}
				}
			}

			if tc.wantAppended && len(snapshots) >= 2 {
				s1, s2 := snapshots[0].Photos, snapshots[1].Photos
				for i := range s1 {
					if s1[i].URL != s2[i].URL {
						t.Errorf("appended check: photo[%d] = %q, want %q", i, s2[i].URL, s1[i].URL)
					}
				}
				if len(s2) <= len(s1) {
					t.Errorf("expected snapshot 2 to have appended photos, got len %d <= %d", len(s2), len(s1))
				}
			}

			if tc.wantRemovedExcluded != "" && len(snapshots) >= 2 {
				for _, ph := range snapshots[1].Photos {
					if strings.Contains(ph.URL, tc.wantRemovedExcluded) {
						t.Errorf("expected removed photo %q to be excluded from poll 2, but found %q", tc.wantRemovedExcluded, ph.URL)
					}
				}
			}
		})
	}
}

func TestPhotosProvider_MultiAlbumAndCache(t *testing.T) {
	t.Parallel()

	t.Run("multi_album_parsing_and_merging", func(t *testing.T) {
		t.Parallel()
		tsShare := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<html><head><title>Share Vacation</title></head><body><img src="https://lh3.googleusercontent.com/pw/P_SHARE1"/><img src="https://lh3.googleusercontent.com/pw/P_DUP"/></body></html>`))
		}))
		defer tsShare.Close()

		cfg := map[string]any{
			"albums": []any{
				map[string]any{
					"name":    "Vacation 2026",
					"source":  "share_url",
					"url_env": "ALBUM_SHARE_URL",
				},
				map[string]any{
					"name":   "Ember Feed",
					"source": "ember_bridge",
					"url":    "https://photos.app.goo.gl/ember-url",
				},
			},
		}

		opts := provider.InitOptions{
			Secrets: map[string]string{
				"albums[0].url": tsShare.URL,
			},
		}

		p := provider.NewPhotosProvider()
		if err := p.Init(context.Background(), cfg, opts); err != nil {
			t.Fatalf("Init failed: %v", err)
		}

		if p.Source() != provider.PhotoSourceShareURL {
			t.Errorf("got Source() = %q, want %q", p.Source(), provider.PhotoSourceShareURL)
		}

		// Initial Fetch hydrates the share_url album
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		// First ember_bridge album's ResolvedURL takes precedence for AlbumURL
		if snap.AlbumURL != "https://photos.app.goo.gl/ember-url" {
			t.Errorf("AlbumURL = %q, want %q", snap.AlbumURL, "https://photos.app.goo.gl/ember-url")
		}
		if snap.AlbumName != "Vacation 2026, Ember Feed" {
			t.Errorf("AlbumName = %q, want %q", snap.AlbumName, "Vacation 2026, Ember Feed")
		}
		if snap.TotalPhotos != 2 {
			t.Errorf("TotalPhotos = %d, want 2", snap.TotalPhotos)
		}
		for _, ph := range snap.Photos {
			if ph.AlbumName != "Vacation 2026" {
				t.Errorf("photo AlbumName = %q, want %q", ph.AlbumName, "Vacation 2026")
			}
		}
	})

	t.Run("per_day_multi_album_pairing", func(t *testing.T) {
		t.Parallel()
		// Day 1 (1755268200000 = 2025-08-15T14:30:00Z): Album A has 3 photos, Album B has 2 photos -> 2 photos dealt (1 from A, 1 from B)
		// Day 2 (1755354600000 = 2025-08-16T14:30:00Z): Album A has 2 photos, Album B has 0 photos -> 1 photo dealt (from A)
		tsAlbumA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><body><script>AF_initDataCallback({key:'ds:1',data:["k",[["a_d1_1",["https://lh3.googleusercontent.com/pw/A_D1_1",800,600],1755268200000],["a_d1_2",["https://lh3.googleusercontent.com/pw/A_D1_2",800,600],1755268260000],["a_d1_3",["https://lh3.googleusercontent.com/pw/A_D1_3",800,600],1755268320000],["a_d2_1",["https://lh3.googleusercontent.com/pw/A_D2_1",800,600],1755354600000],["a_d2_2",["https://lh3.googleusercontent.com/pw/A_D2_2",800,600],1755354660000]]]});</script></body></html>`))
		}))
		defer tsAlbumA.Close()

		tsAlbumB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><body><script>AF_initDataCallback({key:'ds:1',data:["k",[["b_d1_1",["https://lh3.googleusercontent.com/pw/B_D1_1",800,600],1755268200000],["b_d1_2",["https://lh3.googleusercontent.com/pw/B_D1_2",800,600],1755268260000]]]});</script></body></html>`))
		}))
		defer tsAlbumB.Close()

		p := provider.NewPhotosProvider()
		cfg := map[string]any{
			"albums": []any{
				map[string]any{"name": "Album A", "source": "share_url", "url": tsAlbumA.URL},
				map[string]any{"name": "Album B", "source": "share_url", "url": tsAlbumB.URL},
			},
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{Timezone: "UTC"}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		if snap.TotalPhotos != 7 {
			t.Fatalf("TotalPhotos = %d, want 7", snap.TotalPhotos)
		}
		if len(snap.Photos) != 3 {
			t.Fatalf("dealt Photos len = %d, want 3 (2 for Day 1 across Album A & B, 1 for Day 2 from Album A)", len(snap.Photos))
		}
		// Verify that the two Day 1 photos are adjacent and ordered Album A then Album B
		day1Indices := []int{}
		for idx, ph := range snap.Photos {
			if strings.HasPrefix(ph.Timestamp, "2025-08-15") {
				day1Indices = append(day1Indices, idx)
			}
		}
		if len(day1Indices) != 2 || day1Indices[1]-day1Indices[0] != 1 {
			t.Fatalf("expected 2 adjacent Day 1 photos, got indices %v in %+v", day1Indices, snap.Photos)
		}
		if snap.Photos[day1Indices[0]].AlbumName != "Album A" || snap.Photos[day1Indices[1]].AlbumName != "Album B" {
			t.Errorf("expected Day 1 pair [Album A, Album B], got [%s, %s]", snap.Photos[day1Indices[0]].AlbumName, snap.Photos[day1Indices[1]].AlbumName)
		}
	})

	t.Run("warm_boot_zero_http", func(t *testing.T) {
		t.Parallel()
		tempDir := t.TempDir()
		albumURL := "https://photos.app.goo.gl/warm-album"

		// Pre-populate disk cache
		cachePath := filepath.Join(tempDir, provider.AlbumCacheFilename("share_url", albumURL))
		diskData := map[string]any{
			"album_name":      "Cached Album",
			"scraped_title":   "Warm Scraped Title",
			"resolved_url":    albumURL,
			"last_page1_sync": time.Now().Add(-10 * time.Minute),
			"last_full_sync":  time.Now().Add(-2 * time.Hour),
			"photos": []provider.PhotoItem{
				{ID: "c1", URL: "https://lh3.googleusercontent.com/pw/WARM1", AspectRatio: 1.33, AlbumName: "Warm Scraped Title"},
				{ID: "c2", URL: "https://lh3.googleusercontent.com/pw/WARM2", AspectRatio: 1.33, AlbumName: "Warm Scraped Title"},
			},
		}
		dataBytes, _ := json.Marshal(diskData)
		if err := os.WriteFile(cachePath, dataBytes, 0o600); err != nil {
			t.Fatalf("failed to write cache file: %v", err)
		}

		p := provider.NewPhotosProvider(provider.WithPhotosCacheDir(tempDir))
		cfg := map[string]any{
			"source":    "share_url",
			"album_url": albumURL,
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}

		// Warm Fetch should perform zero HTTP I/O and return the 2 cached photos immediately
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("warm Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		if snap.AlbumName != "Warm Scraped Title" {
			t.Errorf("AlbumName = %q, want %q", snap.AlbumName, "Warm Scraped Title")
		}
		if len(snap.Photos) != 2 {
			t.Fatalf("photos count = %d, want 2", len(snap.Photos))
		}
		urls := make(map[string]bool)
		for _, ph := range snap.Photos {
			urls[ph.URL] = true
		}
		if !urls["https://lh3.googleusercontent.com/pw/WARM1"] || !urls["https://lh3.googleusercontent.com/pw/WARM2"] {
			t.Errorf("missing cached photo in %v", urls)
		}
	})

	t.Run("snAcKc_pagination_and_delta_merge", func(t *testing.T) {
		t.Parallel()
		var batchexecuteCalls atomic.Int32
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.Contains(r.URL.Path, "batchexecute") {
				call := batchexecuteCalls.Add(1)
				w.Header().Set("Content-Type", "application/json; charset=utf-8")
				if call == 1 {
					// Return Page 2 with continuation token for Page 3
					_, _ = w.Write([]byte(`)]}'
[[["wrb.fr","snAcKc","[[[\"p2\",[\"https://lh3.googleusercontent.com/pw/PAGE2\",1200,900],1755268200000]],\"token_page3\"]",null,null,null,"generic"]]]`))
				} else {
					// Return Page 3 with empty token (end of pagination)
					_, _ = w.Write([]byte(`)]}'
[[["wrb.fr","snAcKc","[[[\"p3\",[\"https://lh3.googleusercontent.com/pw/PAGE3\",1200,900],1755268300000]],\"\"]",null,null,null,"generic"]]]`))
				}
				return
			}

			// Page 1 HTML
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><head><title>Paged Album - Google Photos</title></head><body><script>AF_initDataCallback({key:'ds:1',hash:'2',data:["k",[["p1",["https://lh3.googleusercontent.com/pw/PAGE1",1000,800],1755268100000]],"token_page2"],sideChannel:{}});</script></body></html>`))
		}))
		defer ts.Close()

		tempDir := t.TempDir()
		p := provider.NewPhotosProvider(provider.WithPhotosCacheDir(tempDir))
		cfg := map[string]any{
			"source":    "share_url",
			"album_url": ts.URL,
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}

		// Initial Fetch performs full sync with snAcKc pagination
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		if snap.TotalPhotos != 3 {
			t.Errorf("TotalPhotos = %d, want 3 (Page 1 + Page 2 + Page 3)", snap.TotalPhotos)
		}
		if batchexecuteCalls.Load() != 2 {
			t.Errorf("batchexecute calls = %d, want 2", batchexecuteCalls.Load())
		}

		// Now test delta merge (full == false):
		// Subsequent Sync(ctx, false) should only scrape Page 1 HTML and merge ahead
		if err := p.Sync(context.Background(), false); err != nil {
			t.Fatalf("delta Sync failed: %v", err)
		}
		// batchexecuteCalls should remain 2 (no pagination on delta merge)
		if batchexecuteCalls.Load() != 2 {
			t.Errorf("batchexecute calls after delta = %d, want 2", batchexecuteCalls.Load())
		}
	})

	t.Run("subscribe_sync_ticks", func(t *testing.T) {
		t.Parallel()
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<html><head><title>Sub Album</title></head><body><img src="https://lh3.googleusercontent.com/pw/SUB1"/></body></html>`))
		}))
		defer ts.Close()

		p := provider.NewPhotosProvider(provider.WithPhotosSyncIntervals(10*time.Millisecond, 50*time.Millisecond))
		cfg := map[string]any{
			"source":    "share_url",
			"album_url": ts.URL,
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}
		// Initial fetch to hydrate
		if _, err := p.Fetch(context.Background()); err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}

		sink := make(chan provider.WidgetPayload, 5)
		ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
		defer cancel()

		errChan := make(chan error, 1)
		go func() {
			errChan <- p.Subscribe(ctx, sink)
		}()

		select {
		case payload := <-sink:
			if payload.State != provider.StateHealthy {
				t.Errorf("got payload state %v, want healthy", payload.State)
			}
			snap, ok := payload.Data.(*provider.PhotoSnapshot)
			if !ok || len(snap.Photos) == 0 {
				t.Fatalf("unexpected payload data: %+v", payload.Data)
			}
		case <-time.After(500 * time.Millisecond):
			t.Fatal("timed out waiting for Subscribe payload tick")
		}

		cancel()
		<-errChan
	})

	t.Run("warm_boot_stale_subscribe", func(t *testing.T) {
		t.Parallel()
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<html><head><title>Stale Sub</title></head><body><img src="https://lh3.googleusercontent.com/pw/STALE1"/></body></html>`))
		}))
		defer ts.Close()

		tempDir := t.TempDir()
		cachePath := filepath.Join(tempDir, provider.AlbumCacheFilename("share_url", ts.URL))
		diskData := map[string]any{
			"album_name":      "Stale Cached",
			"scraped_title":   "Stale Scraped",
			"resolved_url":    ts.URL,
			"last_page1_sync": time.Now().Add(-48 * time.Hour),
			"last_full_sync":  time.Now().Add(-48 * time.Hour),
			"photos": []provider.PhotoItem{
				{ID: "c1", URL: "https://lh3.googleusercontent.com/pw/WARM_OLD", AspectRatio: 1.33},
			},
		}
		dataBytes, _ := json.Marshal(diskData)
		_ = os.WriteFile(cachePath, dataBytes, 0o600)

		p := provider.NewPhotosProvider(
			provider.WithPhotosCacheDir(tempDir),
			provider.WithPhotosSyncIntervals(10*time.Hour, 24*time.Hour),
		)
		cfg := map[string]any{
			"source":    "share_url",
			"album_url": ts.URL,
		}
		if err := p.Init(context.Background(), cfg, provider.InitOptions{}); err != nil {
			t.Fatalf("Init failed: %v", err)
		}

		sink := make(chan provider.WidgetPayload, 5)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()

		errChan := make(chan error, 1)
		go func() {
			errChan <- p.Subscribe(ctx, sink)
		}()

		select {
		case payload := <-sink:
			if payload.State != provider.StateHealthy {
				t.Errorf("got payload state %v, want healthy", payload.State)
			}
		case <-time.After(500 * time.Millisecond):
			t.Fatal("timed out waiting for warm stale Subscribe sync")
		}
		cancel()
		<-errChan
	})

	t.Run("snAcKc_pagination_error_and_edge_cases", func(t *testing.T) {
		t.Parallel()
		var calls atomic.Int32
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.Contains(r.URL.Path, "batchexecute") {
				call := calls.Add(1)
				if call == 1 {
					w.Header().Set("Content-Type", "application/json")
					_, _ = w.Write([]byte(`)]}'
[[["wrb.fr","snAcKc","[[[\"p2\",[\"https://lh3.googleusercontent.com/pw/PAGE2\",1200,900],1755268200000]],\"repeated_tok\"]",null,null,null,"generic"]]]`))
					return
				}
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><body><script>AF_initDataCallback({key:'ds:1',data:["k",[["p1",["https://lh3.googleusercontent.com/pw/PAGE1",1000,800],1755268100000]],"repeated_tok"]});</script></body></html>`))
		}))
		defer ts.Close()

		p := provider.NewPhotosProvider()
		_ = p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": ts.URL}, provider.InitOptions{})
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		if snap.TotalPhotos != 2 {
			t.Errorf("TotalPhotos = %d, want 2", snap.TotalPhotos)
		}
	})

	t.Run("snAcKc_pagination_skips_all_video_pages", func(t *testing.T) {
		t.Parallel()
		var calls atomic.Int32
		ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if strings.Contains(r.URL.Path, "batchexecute") {
				call := calls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if call == 1 {
					// Page 2: all videos, continuation token to Page 3
					_, _ = w.Write([]byte(`)]}'
[[["wrb.fr","snAcKc","[[[\"v1\",[\"https://lh3.googleusercontent.com/pw/VID1\",1920,1080],1755268200000,[\"video/mp4\",30000]],[\"v2\",[\"https://lh3.googleusercontent.com/pw/VID2\",1920,1080],1755268210000,[\"video/mp4\",30000]]],\"token_page3\"]",null,null,null,"generic"]]]`))
					return
				}
				// Page 3: photo, empty token
				_, _ = w.Write([]byte(`)]}'
[[["wrb.fr","snAcKc","[[[\"p3\",[\"https://lh3.googleusercontent.com/pw/PAGE3\",1200,900],1755268300000]],\"\"]",null,null,null,"generic"]]]`))
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><body><script>AF_initDataCallback({key:'ds:1',data:["k",[["p1",["https://lh3.googleusercontent.com/pw/PAGE1",1000,800],1755268100000]],"token_page2"]});</script></body></html>`))
		}))
		defer ts.Close()

		p := provider.NewPhotosProvider()
		_ = p.Init(context.Background(), map[string]any{"source": "share_url", "album_url": ts.URL}, provider.InitOptions{})
		res, err := p.Fetch(context.Background())
		if err != nil {
			t.Fatalf("Fetch failed: %v", err)
		}
		snap := res.(*provider.PhotoSnapshot)
		if snap.TotalPhotos != 2 {
			t.Errorf("TotalPhotos = %d, want 2 (p1 + p3, skipping all-video page 2)", snap.TotalPhotos)
		}
		for _, ph := range snap.Photos {
			if strings.Contains(ph.URL, "VID") {
				t.Errorf("leaked video url into snapshot: %s", ph.URL)
			}
		}
	})
}
