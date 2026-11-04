package provider_test

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/provider"
)

func assertCacheStep(t *testing.T, c *provider.SWRCache, got provider.WidgetPayload, trans bool, wantState, wantTS string, wantTrans bool, wantFails int) {
	t.Helper()
	if got.State != wantState || got.Timestamp != wantTS || trans != wantTrans || c.GetConsecutiveFailures(got.WidgetID) != wantFails {
		t.Fatalf("step fail: state=%q (want %q), ts=%q (want %q), trans=%v (want %v), fails=%d (want %d)", got.State, wantState, got.Timestamp, wantTS, trans, wantTrans, c.GetConsecutiveFailures(got.WidgetID), wantFails)
	}
}

func TestSWRCache_LifecycleAndStateTransitions(t *testing.T) {
	t.Parallel()
	cache := provider.NewSWRCache()
	wID := "sensor-weather"
	t0 := time.Date(2026, 9, 25, 10, 0, 0, 0, time.UTC)

	if _, ok := cache.Get(wID); ok || cache.GetConsecutiveFailures(wID) != 0 {
		t.Fatal("expected empty initial cache")
	}

	errCold := errors.New("timeout on cold boot")
	p1, tr1 := cache.RecordFailure(wID, errCold, t0)
	assertCacheStep(t, cache, p1, tr1, provider.StateError, t0.Format(time.RFC3339), true, 1)
	if dm, ok := p1.Data.(map[string]any); !ok || len(dm) != 0 {
		t.Fatal("expected empty fallback map")
	}

	p2, tr2 := cache.RecordFailure(wID, errCold, t0.Add(5*time.Second))
	assertCacheStep(t, cache, p2, tr2, provider.StateError, t0.Add(5*time.Second).Format(time.RFC3339), false, 2)

	t2 := t0.Add(30 * time.Second)
	p3, tr3 := cache.RecordSuccess(wID, map[string]any{"temp": 72.5}, t2)
	assertCacheStep(t, cache, p3, tr3, provider.StateHealthy, t2.Format(time.RFC3339), true, 0)

	t3 := t2.Add(60 * time.Second)
	p4, tr4 := cache.RecordSuccess(wID, map[string]any{"temp": 73.0}, t3)
	assertCacheStep(t, cache, p4, tr4, provider.StateHealthy, t3.Format(time.RFC3339), false, 0)

	errTrans := errors.New("502 bad gateway")
	p5, tr5 := cache.RecordFailure(wID, errTrans, t3.Add(60*time.Second))
	assertCacheStep(t, cache, p5, tr5, provider.StateDegraded, t3.Format(time.RFC3339), true, 1)
	if dm, ok := p5.Data.(map[string]any); !ok || dm["temp"] != 73.0 {
		t.Fatal("expected preserved LKG data")
	}

	p6, tr6 := cache.RecordFailure(wID, errTrans, t3.Add(70*time.Second))
	assertCacheStep(t, cache, p6, tr6, provider.StateDegraded, t3.Format(time.RFC3339), false, 2)

	t6 := t3.Add(100 * time.Second)
	p7, tr7 := cache.RecordSuccess(wID, map[string]any{"temp": 71.8}, t6)
	assertCacheStep(t, cache, p7, tr7, provider.StateHealthy, t6.Format(time.RFC3339), true, 0)

	pPush, _ := cache.RecordPush(wID, map[string]any{"temp": 70.0}, t6.Add(10*time.Second))
	if pPush.State != provider.StateHealthy {
		t.Fatal("expected healthy on push")
	}

	if len(cache.GetStatusMap()) != 1 {
		t.Fatal("unexpected StatusMap length")
	}

	cache.Purge(wID)
	if _, ok := cache.Get(wID); ok || cache.GetStatusMap() != nil {
		t.Fatal("expected cache eviction after Purge")
	}
}

func TestSWRCache_ConcurrentAccessSafety(t *testing.T) {
	t.Parallel()
	cache := provider.NewSWRCache()
	now := time.Now()
	var wg sync.WaitGroup
	for i := range 20 {
		wg.Add(1)
		go func(id string, idx int) {
			defer wg.Done()
			for j := range 100 {
				switch (idx + j) % 3 {
				case 0:
					cache.RecordSuccess(id, map[string]any{"iter": j}, now)
				case 1:
					cache.RecordFailure(id, errors.New("transient"), now)
				default:
					_, _ = cache.Get(id)
					_ = cache.GetStatusMap()
				}
			}
		}(fmt.Sprintf("widget-%d", i%5), i)
	}
	wg.Wait()
}
