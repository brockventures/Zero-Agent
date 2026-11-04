package voice

import (
	"math"
	"regexp"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var nodeIDRegex = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`)

// SanitizeNodeID enforces bounded alphanumeric label values.
func SanitizeNodeID(nodeID string) string {
	if !nodeIDRegex.MatchString(nodeID) {
		return "unknown"
	}
	return nodeID
}

// EdgeTimings encapsulates client-side edge latency and audio measurements.
type EdgeTimings struct {
	WakeEvalSec         float64
	UtteranceSpeechSec  float64
	UtteranceSilenceSec float64
}

// Metrics encapsulates Prometheus collectors for the voice pipeline with bounded cardinality.
type Metrics struct {
	stageDuration    *prometheus.HistogramVec
	ambientRMS       *prometheus.GaugeVec
	lastSeen         *prometheus.GaugeVec
	falseWakes       *prometheus.CounterVec
	playbackDuration *prometheus.HistogramVec
	turnsTotal       *prometheus.CounterVec
	errorsTotal      *prometheus.CounterVec
	sttRTF           *prometheus.GaugeVec
	ttsCPS           *prometheus.GaugeVec
}

var (
	defaultMetrics     *Metrics
	defaultMetricsOnce sync.Once
)

// DefaultMetrics returns the global default voice metrics collector.
func DefaultMetrics() *Metrics {
	defaultMetricsOnce.Do(func() {
		defaultMetrics = newMetrics(prometheus.DefaultRegisterer)
	})
	return defaultMetrics
}

func newMetrics(reg prometheus.Registerer) *Metrics {
	factory := promauto.With(reg)

	m := &Metrics{
		stageDuration: factory.NewHistogramVec(
			prometheus.HistogramOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "stage_duration_seconds",
				Help:      "Duration of voice pipeline stages in seconds.",
				Buckets:   []float64{0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0},
			},
			[]string{"node_id", "stage", "status"},
		),
		ambientRMS: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "ambient_rms_dbfs",
				Help:      "Ambient background sound level reported by the edge client in dBFS.",
			},
			[]string{"node_id"},
		),
		lastSeen: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "edge_last_seen_timestamp_seconds",
				Help:      "Unix timestamp in seconds of the most recent heartbeat from an edge client.",
			},
			[]string{"node_id"},
		),
		falseWakes: factory.NewCounterVec(
			prometheus.CounterOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "false_wakes_total",
				Help:      "Total number of false wake detections aborted on the edge client.",
			},
			[]string{"node_id"},
		),
		playbackDuration: factory.NewHistogramVec(
			prometheus.HistogramOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "playback_duration_seconds",
				Help:      "Audio playback duration on the edge device in seconds.",
				Buckets:   []float64{0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0},
			},
			[]string{"node_id"},
		),
		turnsTotal: factory.NewCounterVec(
			prometheus.CounterOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "turns_total",
				Help:      "Total number of voice interaction turns processed.",
			},
			[]string{"node_id", "status"},
		),
		errorsTotal: factory.NewCounterVec(
			prometheus.CounterOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "errors_total",
				Help:      "Total number of voice pipeline errors partitioned by stage and error type.",
			},
			[]string{"node_id", "stage", "error_type"},
		),
		sttRTF: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "stt_rtf",
				Help:      "Speech-to-Text Real-Time Factor (inference duration / audio duration).",
			},
			[]string{"node_id", "engine"},
		),
		ttsCPS: factory.NewGaugeVec(
			prometheus.GaugeOpts{
				Namespace: "mirrormere",
				Subsystem: "voice",
				Name:      "tts_chars_per_second",
				Help:      "Text-to-Speech synthesis speed in characters per second.",
			},
			[]string{"node_id", "voice"},
		),
	}

	for _, status := range []string{"success", "error"} {
		m.turnsTotal.WithLabelValues("cockpit", status).Add(0)
	}
	m.falseWakes.WithLabelValues("cockpit").Add(0)
	for _, stage := range []string{"wake_eval", "stt", "brain", "tts", "deliberation"} {
		m.errorsTotal.WithLabelValues("cockpit", stage, "none").Add(0)
		for _, status := range []string{"success", "error", "timeout"} {
			m.stageDuration.WithLabelValues("cockpit", stage, status)
		}
	}
	m.sttRTF.WithLabelValues("cockpit", "faster-whisper").Set(0)
	m.ttsCPS.WithLabelValues("cockpit", "kokoro").Set(0)
	m.playbackDuration.WithLabelValues("cockpit")
	m.ambientRMS.WithLabelValues("cockpit").Set(0)

	return m
}

func validBoundedFloat(v, minVal, maxVal float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0) && v >= minVal && v <= maxVal
}

// RecordStageDuration logs the duration of a voice pipeline stage.
func (m *Metrics) RecordStageDuration(nodeID, stage, status string, durationSec float64) {
	if m == nil || m.stageDuration == nil {
		return
	}
	if !validBoundedFloat(durationSec, 0, 300.0) {
		return
	}
	m.stageDuration.WithLabelValues(SanitizeNodeID(nodeID), stage, status).Observe(durationSec)
}

// RecordAmbientRMS updates the ambient RMS level gauge for a node.
func (m *Metrics) RecordAmbientRMS(nodeID string, rmsDBFS float64) {
	if m == nil || m.ambientRMS == nil {
		return
	}
	if !validBoundedFloat(rmsDBFS, math.Inf(-1), math.Inf(1)) {
		return
	}
	m.ambientRMS.WithLabelValues(SanitizeNodeID(nodeID)).Set(rmsDBFS)
}

// RecordEdgeHeartbeat records client heartbeat liveness timestamp.
func (m *Metrics) RecordEdgeHeartbeat(nodeID string) {
	if m == nil || m.lastSeen == nil {
		return
	}
	m.lastSeen.WithLabelValues(SanitizeNodeID(nodeID)).Set(float64(time.Now().Unix()))
}

// RecordFalseWakes increments the false wake counter by count with bounding.
func (m *Metrics) RecordFalseWakes(nodeID string, count int) {
	if m == nil || m.falseWakes == nil || count <= 0 {
		return
	}
	if count > 1000 {
		count = 1000
	}
	m.falseWakes.WithLabelValues(SanitizeNodeID(nodeID)).Add(float64(count))
}

// RecordPlaybackDuration records audio playback duration on the edge.
func (m *Metrics) RecordPlaybackDuration(nodeID string, durationSec float64) {
	if m == nil || m.playbackDuration == nil {
		return
	}
	if durationSec <= 0 || !validBoundedFloat(durationSec, 0, 120.0) {
		return
	}
	m.playbackDuration.WithLabelValues(SanitizeNodeID(nodeID)).Observe(durationSec)
}

// RecordTurn increments the interaction turn counter.
func (m *Metrics) RecordTurn(nodeID, status string) {
	if m == nil || m.turnsTotal == nil {
		return
	}
	m.turnsTotal.WithLabelValues(SanitizeNodeID(nodeID), status).Inc()
}

// RecordError increments the error counter for a specific stage.
func (m *Metrics) RecordError(nodeID, stage, errType string) {
	if m == nil || m.errorsTotal == nil {
		return
	}
	m.errorsTotal.WithLabelValues(SanitizeNodeID(nodeID), stage, errType).Inc()
}

// RecordSTTRTF records Whisper Real-Time Factor.
func (m *Metrics) RecordSTTRTF(nodeID, engine string, rtf float64) {
	if m == nil || m.sttRTF == nil {
		return
	}
	if !validBoundedFloat(rtf, 0, math.Inf(1)) {
		return
	}
	if engine == "" {
		engine = "unknown"
	}
	m.sttRTF.WithLabelValues(SanitizeNodeID(nodeID), engine).Set(rtf)
}

// RecordTTSCPS records Kokoro characters-per-second synthesis rate.
func (m *Metrics) RecordTTSCPS(nodeID, voice string, cps float64) {
	if m == nil || m.ttsCPS == nil {
		return
	}
	if !validBoundedFloat(cps, 0, math.Inf(1)) {
		return
	}
	if voice == "" {
		voice = "unknown"
	}
	m.ttsCPS.WithLabelValues(SanitizeNodeID(nodeID), voice).Set(cps)
}
