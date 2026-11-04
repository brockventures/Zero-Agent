import { unstable_batchedUpdates } from 'react-dom';
import { observeDocumentVisibility, reflectVisibleAttribute } from './lifecycle/visibility';
import { applyStyleReload, createReloadController, MAINTENANCE_CHECK_INTERVAL_MS } from './lifecycle/reload';
import { createEventBatcher, type BufferedEvent } from './sse/batch';
import { EventStreamClient, SERVER_EVENT_NAMES, type EventStreamOptions } from './sse/client';
import type { ServerEventName, StreamEventMap } from './sse/types';
import { useDisplayStore } from './store';

/** Events whose payload is applied to the store (batched). */
const STORE_EVENTS: ReadonlySet<ServerEventName> = new Set<ServerEventName>([
  'screen.rotate',
  'widget.update',
  'widget.reload',
  'header.update',
  'video.state',
  'audio.state',
  'voice.state',
  'theme.update',
  'system.status',
]);

export interface RuntimeOptions extends EventStreamOptions {
  url?: string;
  /** Page reload; defaults to window.location.reload. Injected for tests. */
  reload?: () => void;
}

export interface Runtime {
  client: EventStreamClient;
  dispose(): void;
}

let current: Runtime | null = null;

/**
 * Starts the display's event stream and wires it to the store, the reload
 * rules and the lifecycle hooks. Idempotent: a second call returns the running
 * runtime, so there is only ever one EventSource.
 */
export function startRuntime(options: RuntimeOptions = {}): Runtime {
  if (current) return current;

  const { url = 'api/events', reload = () => window.location.reload(), ...clientOptions } = options;
  const store = useDisplayStore;
  const client = new EventStreamClient(url, clientOptions);

  const batcher = createEventBatcher((events: BufferedEvent[]) => {
    unstable_batchedUpdates(() => {
      store.getState().applyEvents(events);
    });
  });

  const reloads = createReloadController({
    now: () => Date.now(),
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (t) => clearTimeout(t),
    reload,
    storage: typeof window !== 'undefined' && window.sessionStorage ? window.sessionStorage : null,
  });

  const unsubscribers: Array<() => void> = [];

  unsubscribers.push(
    client.on('connection', (payload) => {
      store.getState().setConnection(payload);
    }),
  );

  for (const name of SERVER_EVENT_NAMES) {
    if (STORE_EVENTS.has(name)) {
      unsubscribers.push(
        client.on(name, (data: StreamEventMap[typeof name]) => {
          batcher.push({ type: name, data } as BufferedEvent);
        }),
      );
    }
  }

  unsubscribers.push(
    client.on('system.status', (data) => {
      reloads.handleSystemStatus(data);
    }),
    client.on('system.reload', (data) => {
      reloads.handleSystemReload(data);
    }),
    client.on('style.reload', () => {
      applyStyleReload(document);
    }),
  );

  // Maintenance check and visibility. The interval runs only while visible;
  // becoming visible runs a check immediately.
  let maintenanceTimer: ReturnType<typeof setInterval> | null = null;
  const checkMaintenance = () => {
    reloads.checkDailyMaintenance(new Date(), store.getState().header.timezone);
  };
  const syncMaintenance = (visible: boolean) => {
    if (visible && maintenanceTimer === null) {
      checkMaintenance();
      maintenanceTimer = setInterval(checkMaintenance, MAINTENANCE_CHECK_INTERVAL_MS);
    } else if (!visible && maintenanceTimer !== null) {
      clearInterval(maintenanceTimer);
      maintenanceTimer = null;
    }
  };

  const stopVisibility = observeDocumentVisibility(document, window, (visible) => {
    store.getState().setDocumentVisible(visible);
  });
  const unsubscribeVisible = store.subscribe((state, prev) => {
    if (state.visibility.visible !== prev.visibility.visible) {
      reflectVisibleAttribute(document, state.visibility.visible);
      syncMaintenance(state.visibility.visible);
    }
  });
  reflectVisibleAttribute(document, store.getState().visibility.visible);
  syncMaintenance(store.getState().visibility.visible);

  client.connect();

  current = {
    client,
    dispose() {
      client.disconnect();
      for (const off of unsubscribers) off();
      unsubscribeVisible();
      stopVisibility();
      syncMaintenance(false);
      reloads.dispose();
      batcher.flush();
      current = null;
    },
  };
  return current;
}
