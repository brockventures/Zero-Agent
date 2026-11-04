package voice

import (
	"math"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// NewMetrics instantiates and registers bounded voice metrics.
func NewMetrics(reg prometheus.Registerer) *Metrics {
	if reg == nil {
		return DefaultMetrics()
	}
	return newMetrics(reg)
}

func lbl(kv ...string) map[string]string {
	m := make(map[string]string, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		m[kv[i]] = kv[i+1]
	}
	return m
}

func metricVal(t *testing.T, reg *prometheus.Registry, name string, labels map[string]string) (float64, bool) {
	t.Helper()
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}
	var total float64
	var found bool
	for _, mf := range mfs {
		if mf.GetName() != name {
			continue
		}
	nextMetric:
		for _, m := range mf.GetMetric() {
			got := map[string]string{}
			for _, lp := range m.GetLabel() {
				got[lp.GetName()] = lp.GetValue()
			}
			for k, want := range labels {
				if got[k] != want {
					continue nextMetric
				}
			}
			found = true
			switch {
			case m.GetCounter() != nil:
				total += m.GetCounter().GetValue()
			case m.GetGauge() != nil:
				total += m.GetGauge().GetValue()
			case m.GetHistogram() != nil:
				total += float64(m.GetHistogram().GetSampleCount())
			}
		}
	}
	return total, found
}

func TestMetrics_CollectorsAndSafety(t *testing.T) {
	t.Parallel()
	if DefaultMetrics() == nil || NewMetrics(nil) == nil || SanitizeNodeID("kitchen-display") != "kitchen-display" || SanitizeNodeID("living_room_1") != "living_room_1" {
		t.Fatal("metrics init or valid node sanitize failed")
	}
	for _, bad := range []string{"", "   ", "node/../../evil", "bad node with spaces"} {
		if got := SanitizeNodeID(bad); got != "unknown" {
			t.Errorf("SanitizeNodeID(%q) = %q, want unknown", bad, got)
		}
	}

	reg := prometheus.NewRegistry()
	node := "kitchen-display"
	metrics := NewMetrics(reg)
	for _, m := range []*Metrics{nil, metrics} {
		for _, stg := range []string{"wake_eval", "stt", "brain", "tts"} {
			m.RecordStageDuration(node, stg, "success", 0.35)
		}
		m.RecordStageDuration(node, "deliberation", "success", 120.0)
		m.RecordStageDuration(node, "deliberation", "timeout", 305.0)
		m.RecordStageDuration(node, "tts", "timeout", 350.0)
		m.RecordAmbientRMS(node, -40.5)
		m.RecordAmbientRMS(node, math.NaN())
		m.RecordAmbientRMS(node, math.Inf(1))
		m.RecordEdgeHeartbeat(node)
		for _, n := range []int{0, -5, 5, 1500} {
			m.RecordFalseWakes(node, n)
		}
		for _, d := range []float64{1.25, 0.0, 125.0} {
			m.RecordPlaybackDuration(node, d)
		}
		m.RecordTurn(node, "success")
		m.RecordError(node, "stt", "network_timeout")
		m.RecordSTTRTF(node, "whisper_large_v3", 0.18)
		m.RecordSTTRTF(node, "", 0.22)
		m.RecordTTSCPS(node, "kokoro_am_adam", 28.5)
		m.RecordTTSCPS(node, "", 24.0)
		for _, bad := range []float64{-0.5, math.NaN(), math.Inf(1)} {
			m.RecordStageDuration(node, "stt", "fail", bad)
			m.RecordPlaybackDuration(node, bad)
			m.RecordSTTRTF(node, "whisper_large_v3", bad)
			m.RecordTTSCPS(node, "kokoro_am_adam", bad)
		}
	}

	for _, tc := range []struct {
		name string
		lbl  map[string]string
		want float64
	}{
		{"mirrormere_voice_stage_duration_seconds", lbl("node_id", node, "stage", "wake_eval", "status", "success"), 1},
		{"mirrormere_voice_stage_duration_seconds", lbl("node_id", node, "stage", "deliberation", "status", "success"), 1},
		{"mirrormere_voice_ambient_rms_dbfs", lbl("node_id", node), -40.5},
		{"mirrormere_voice_false_wakes_total", lbl("node_id", node), 1005},
		{"mirrormere_voice_playback_duration_seconds", lbl("node_id", node), 1},
		{"mirrormere_voice_turns_total", lbl("node_id", node, "status", "success"), 1},
		{"mirrormere_voice_errors_total", lbl("node_id", node, "stage", "stt", "error_type", "network_timeout"), 1},
		{"mirrormere_voice_stt_rtf", lbl("node_id", node, "engine", "whisper_large_v3"), 0.18},
		{"mirrormere_voice_stt_rtf", lbl("node_id", node, "engine", "unknown"), 0.22},
		{"mirrormere_voice_tts_chars_per_second", lbl("node_id", node, "voice", "kokoro_am_adam"), 28.5},
		{"mirrormere_voice_tts_chars_per_second", lbl("node_id", node, "voice", "unknown"), 24.0},
		{"mirrormere_voice_turns_total", lbl("node_id", "cockpit", "status", "success"), 0},
		{"mirrormere_voice_turns_total", lbl("node_id", "cockpit", "status", "error"), 0},
		{"mirrormere_voice_false_wakes_total", lbl("node_id", "cockpit"), 0},
		{"mirrormere_voice_errors_total", lbl("node_id", "cockpit", "stage", "wake_eval", "error_type", "none"), 0},
		{"mirrormere_voice_errors_total", lbl("node_id", "cockpit", "stage", "deliberation", "error_type", "none"), 0},
		{"mirrormere_voice_stage_duration_seconds", lbl("node_id", "cockpit", "stage", "wake_eval", "status", "success"), 0},
		{"mirrormere_voice_stage_duration_seconds", lbl("node_id", "cockpit", "stage", "deliberation", "status", "timeout"), 0},
		{"mirrormere_voice_playback_duration_seconds", lbl("node_id", "cockpit"), 0},
		{"mirrormere_voice_stt_rtf", lbl("node_id", "cockpit", "engine", "faster-whisper"), 0},
		{"mirrormere_voice_tts_chars_per_second", lbl("node_id", "cockpit", "voice", "kokoro"), 0},
		{"mirrormere_voice_ambient_rms_dbfs", lbl("node_id", "cockpit"), 0},
	} {
		if val, ok := metricVal(t, reg, tc.name, tc.lbl); !ok || val != tc.want {
			t.Errorf("metric %s %v = (%v, %v), want (%v, true)", tc.name, tc.lbl, val, ok, tc.want)
		}
	}
	_, okFail := metricVal(t, reg, "mirrormere_voice_stage_duration_seconds", lbl("node_id", node, "stage", "stt", "status", "fail"))
	_, okNone := metricVal(t, reg, "nonexistent_metric", nil)
	_, okCockpitBeforeHeartbeat := metricVal(t, reg, "mirrormere_voice_edge_last_seen_timestamp_seconds", lbl("node_id", "cockpit"))
	if val, ok := metricVal(t, reg, "mirrormere_voice_edge_last_seen_timestamp_seconds", lbl("node_id", node)); !ok || val <= 0 || okFail || okNone || okCockpitBeforeHeartbeat {
		t.Fatalf("last_seen=(%v,%v) fail=%v none=%v cockpit_before=%v", val, ok, okFail, okNone, okCockpitBeforeHeartbeat)
	}
	metrics.RecordEdgeHeartbeat("cockpit")
	if val, ok := metricVal(t, reg, "mirrormere_voice_edge_last_seen_timestamp_seconds", lbl("node_id", "cockpit")); !ok || val <= 0 {
		t.Fatalf("cockpit last_seen after heartbeat=(%v,%v), want (>0, true)", val, ok)
	}
}
