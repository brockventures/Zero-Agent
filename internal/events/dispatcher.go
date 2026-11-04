package events

import (
	"encoding/json"
	"time"

	"github.com/azylman/mirrormere/internal/config"
)

// StyleReloadEvent matches api/schemas/style.reload.json.
type StyleReloadEvent struct {
	File      string `json:"file"`
	Timestamp string `json:"timestamp"`
}

// WidgetReloadEvent matches api/schemas/widget.reload.json.
type WidgetReloadEvent struct {
	Type string `json:"type"`
}

// SystemReloadEvent matches api/schemas/system.reload.json.
type SystemReloadEvent struct {
	Reason    string `json:"reason"`
	Timestamp string `json:"timestamp"`
}

func (h *Hub) publishJSON(eventType string, payload any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	h.Publish(eventType, data)
	return nil
}

func (h *Hub) dispatchTimezoneChange(snapshot *config.Snapshot, nowStr string) error {
	var weather *HeaderWeather
	if isp, ok := h.stateProvider.(*InMemoryStateProvider); ok {
		if w, ok := isp.GetHeaderWeather(); ok {
			weather = w
		}
	}
	tz := snapshot.Config.Timezone
	if tz == "" {
		tz = "UTC"
	}
	return h.publishJSON(EventHeaderUpdate, HeaderUpdateData{
		Timestamp: nowStr,
		Timezone:  tz,
		Weather:   weather,
	})
}

func (h *Hub) dispatchDiffReloads(snapshot *config.Snapshot, diff *config.ConfigDiff) error {
	if diff == nil {
		return nil
	}
	now := h.cfg.NowFunc()
	nowStr := now.UTC().Format(time.RFC3339)
	if err := h.dispatchDegradedWidgetUpdates(diff, nowStr); err != nil {
		return err
	}
	if diff.TimezoneChanged && snapshot.Config != nil {
		if err := h.dispatchTimezoneChange(snapshot, nowStr); err != nil {
			return err
		}
	}
	if diff.ShellChanged {
		reloadEvent := SystemReloadEvent{
			Reason:    "shell_change",
			Timestamp: nowStr,
		}
		if err := h.DispatchSystemReload(reloadEvent); err != nil {
			h.logger.Error("failed to dispatch system reload on shell change", "error", err)
		}
	}
	return nil
}

// DispatchConfigReload handles configuration updates, layout changes, and domain state resets.
func (h *Hub) DispatchConfigReload(snapshot *config.Snapshot, diff *config.ConfigDiff) error {
	if snapshot == nil {
		return nil
	}

	// 1. Update StateProvider snapshot reference
	if isp, ok := h.stateProvider.(*InMemoryStateProvider); ok {
		isp.SetSnapshot(snapshot)
	}

	// 2. Broadcast screen.rotate via rotation coordinator if registered, or fallback to layout
	if rc := h.RotationCoordinator(); rc != nil {
		rc.UpdateConfig(snapshot)
	} else if snapshot.Layout != nil && len(snapshot.Layout.Screens) > 0 {
		if err := h.publishJSON(EventScreenRotate, buildScreenRotateFromSnapshot(snapshot)); err != nil {
			return err
		}
	}

	// Reconcile active providers if provider coordinator registered
	if pc := h.ProviderCoordinator(); pc != nil {
		if err := pc.UpdateConfig(snapshot); err != nil {
			h.logger.Error("failed to reconcile provider coordinator on config reload", "error", err)
		}
	}

	return h.dispatchDiffReloads(snapshot, diff)
}

func (h *Hub) dispatchDegradedWidgetUpdates(diff *config.ConfigDiff, nowStr string) error {
	if h.ProviderCoordinator() != nil {
		return nil
	}
	for _, mod := range diff.Modified {
		if mod.DomainChange {
			payload := WidgetUpdateData{
				WidgetID:  mod.ID,
				Timestamp: nowStr,
				State:     "degraded",
				Data:      map[string]any{},
			}
			if err := h.publishJSON(EventWidgetUpdate, payload); err != nil {
				return err
			}
		}
	}
	return nil
}

// DispatchStyleReload broadcasts style.reload notifications to connected clients.
func (h *Hub) DispatchStyleReload(event StyleReloadEvent) error {
	return h.publishJSON(EventStyleReload, event)
}

// DispatchWidgetReload broadcasts widget.reload notifications to connected clients.
func (h *Hub) DispatchWidgetReload(event WidgetReloadEvent) error {
	return h.publishJSON(EventWidgetReload, event)
}

// DispatchSystemReload broadcasts system.reload notifications to connected clients.
func (h *Hub) DispatchSystemReload(event SystemReloadEvent) error {
	return h.publishJSON(EventSystemReload, event)
}

// DispatchStatus broadcasts system.status telemetry matching api/schemas/system.status.json.
func (h *Hub) DispatchStatus(status config.Status) error {
	if isp, ok := h.stateProvider.(*InMemoryStateProvider); ok {
		isp.SetStatus(status)
	}
	nowStr := h.cfg.NowFunc().UTC().Format(time.RFC3339)
	return h.publishJSON(EventSystemStatus, buildSystemStatusData(h.stateProvider, status, nowStr))
}
