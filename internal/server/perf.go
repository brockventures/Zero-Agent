package server

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"

	"github.com/azylman/mirrormere/internal/api"
)

const (
	// perfMaxSamples caps a single POST /api/perf batch.
	perfMaxSamples = 200
	// perfMaxBodyBytes bounds the request body; a full batch is well under this.
	perfMaxBodyBytes = 64 << 10
)

// perfMetricNames is the fixed allow-list of display timing metrics accepted by POST /api/perf.
// Each name maps to the histogram mirrormere_display_<name>_ms.
var perfMetricNames = []string{
	"gesture_first_frame",
	"swipe_settle",
	"frame",
	"long_task",
	"interaction",
	"screen_ready",
	"widget_update",
}

// perfBucketsMs are the histogram upper bounds in milliseconds. 16.7 is one 60Hz frame.
var perfBucketsMs = []float64{4, 8, 16.7, 33, 50, 100, 200, 300, 500, 1000, 2000, 5000}

var (
	defaultPerfOnce       sync.Once
	defaultPerfHistograms map[string]*prometheus.HistogramVec
)

// defaultPerfHistogramsOnce registers the display timing histograms on the default
// registry (the one /metrics exposes) exactly once per process.
func defaultPerfHistogramsOnce() map[string]*prometheus.HistogramVec {
	defaultPerfOnce.Do(func() {
		defaultPerfHistograms = newPerfHistograms(prometheus.DefaultRegisterer)
	})
	return defaultPerfHistograms
}

func newPerfHistograms(reg prometheus.Registerer) map[string]*prometheus.HistogramVec {
	factory := promauto.With(reg)
	hists := make(map[string]*prometheus.HistogramVec, len(perfMetricNames))
	for _, name := range perfMetricNames {
		hists[name] = factory.NewHistogramVec(
			prometheus.HistogramOpts{
				Namespace: "mirrormere",
				Subsystem: "display",
				Name:      name + "_ms",
				Help:      fmt.Sprintf("Display client %s timing in milliseconds, by UI.", name),
				Buckets:   perfBucketsMs,
			},
			[]string{"ui"},
		)
	}
	return hists
}

// handlePerf accepts display timing batches on POST /api/perf. The batch is validated in
// full before any sample is observed, so a rejected batch leaves no partial data behind.
func handlePerf(hists map[string]*prometheus.HistogramVec) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			writeAPIError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		var req api.PerfBatchRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, perfMaxBodyBytes)).Decode(&req); err != nil {
			writeAPIError(w, http.StatusBadRequest, "invalid JSON body")
			return
		}
		if err := validatePerfBatch(req, hists); err != nil {
			writeAPIError(w, http.StatusBadRequest, err.Error())
			return
		}
		for _, sample := range req.Samples {
			hists[sample.Metric].WithLabelValues(sample.UI).Observe(sample.ValueMs)
		}
		w.WriteHeader(http.StatusNoContent)
	}
}

// validatePerfBatch checks batch size, metric allow-list, UI label, and value range.
func validatePerfBatch(req api.PerfBatchRequest, hists map[string]*prometheus.HistogramVec) error {
	if n := len(req.Samples); n == 0 || n > perfMaxSamples {
		return fmt.Errorf("samples must contain between 1 and %d entries", perfMaxSamples)
	}
	for i, sample := range req.Samples {
		if _, ok := hists[sample.Metric]; !ok {
			return fmt.Errorf("samples[%d]: unknown metric %q", i, sample.Metric)
		}
		if sample.UI != api.UILegacy && sample.UI != api.UIReact {
			return fmt.Errorf("samples[%d]: ui must be \"legacy\" or \"react\"", i)
		}
		if math.IsNaN(sample.ValueMs) || math.IsInf(sample.ValueMs, 0) || sample.ValueMs < 0 {
			return fmt.Errorf("samples[%d]: value_ms must be a finite number >= 0", i)
		}
	}
	return nil
}
