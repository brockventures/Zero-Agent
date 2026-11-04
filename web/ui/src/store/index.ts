import { create } from 'zustand';
import type { BufferedEvent } from '../sse/batch';
import type { ConnectionPayload } from '../sse/types';
import { deepEqual } from './equal';
import type { ScreenLayout, ScreenRotatePayload } from '../sse/types';
import { REFETCH_ON_UPDATE, fetchScreenLayouts, fetchWidgetDetail, toWidgetEntry } from './hydrate';
import type { CarouselSlice, DisplayData, ScreenSlice, WidgetEntry } from './types';

export type {
  DisplayData,
  WidgetEntry,
} from './types';

interface DisplayActions {
  /** Apply a batch of server events in one store update. Refetches widgets in REFETCH_ON_UPDATE. */
  applyEvents: (events: BufferedEvent[]) => void;
  /**
   * Fill meta, state, data, viewModel and timestamp from GET api/widgets/{id}.
   * `fresh` skips sharing an in-flight fetch: a stream update that lands mid-fetch needs its own.
   */
  hydrateWidget: (id: string, opts?: { fresh?: boolean }) => Promise<void>;
  setConnection: (payload: ConnectionPayload) => void;
  setDocumentVisible: (visible: boolean) => void;
  /** Fetch GET api/screens into `layout`. Only the latest request is applied. */
  loadScreens: () => Promise<void>;
  /** Queue screen.rotate instead of applying it. Used while a video takes over the carousel. */
  pause: () => void;
  /** Leave the paused state and apply the latest queued screen.rotate, if any. */
  resume: () => void;
  /** Mark `index` as the current screen without a server round trip. No-op when it already is. */
  setCurrentScreen: (index: number) => void;
  /** Open or close the live canvas layout editor overlay, pausing carousel rotation while open. */
  setLayoutEditorOpen: (open: boolean) => void;
}

export type DisplayStore = DisplayData & DisplayActions;

const initialData: DisplayData = {
  screen: { currentScreen: 0, totalScreens: 1, intervalSeconds: 30, widgets: [] },
  widgets: {},
  header: { timestamp: null, timezone: 'UTC', weather: null },
  theme: { theme: 'day' },
  video: { mode: 'widgets', primary: null, pip: null },
  audio: { volume: 75, muted: false },
  voice: { state: 'idle', transcript: null, reply: null, tts_engine: null, status: null },
  system: {
    connection: 'connecting',
    online: true,
    serverTime: null,
    configStatus: 'ok',
    configError: null,
    providers: {},
    bootId: null,
  },
  visibility: { visible: true },
  layout: [],
  layoutVersion: 0,
  widgetReloads: {},
  carousel: { paused: false, pendingRotate: null },
  layoutEditorOpen: false,
};

/**
 * True when a screen.rotate describes a layout other than `layout`: a different screen
 * count, or different widget placements on the current screen. A rotate with the same
 * placements leaves the layout alone. An empty layout is not yet known, so it is never a change.
 */
export function rotateChangesLayout(layout: readonly ScreenLayout[], data: ScreenRotatePayload): boolean {
  if (layout.length === 0) return false;
  if (data.total_screens !== layout.length) return true;
  const screen = layout.find((s) => s.index === data.current_screen);
  if (!screen) return true;
  const current = screen.widgets;
  const incoming = data.widgets;
  if (current.length !== incoming.length) return true;
  return current.some((w, i) => {
    const next = incoming[i];
    return (
      w.widget_id !== next.widget_id ||
      w.origin[0] !== next.origin[0] ||
      w.origin[1] !== next.origin[1] ||
      w.dimensions[0] !== next.dimensions[0] ||
      w.dimensions[1] !== next.dimensions[1]
    );
  });
}

function screenFromRotate(payload: ScreenRotatePayload): ScreenSlice {
  return {
    currentScreen: payload.current_screen,
    totalScreens: payload.total_screens,
    intervalSeconds: payload.interval_seconds,
    widgets: payload.widgets,
  };
}

/** Latest request sequence per widget id, so a slow older fetch cannot overwrite a newer one. */
const hydrateSeq = new Map<string, number>();
/** Per widget id: the widgetReloads count for its type when its meta was last fetched. */
const hydratedAtReload = new Map<string, number>();
/** Hydrates in flight per widget id, keyed to the widgetReloads they started under. */
const inflight = new Map<string, { reloads: Record<string, number>; promise: Promise<void> }>();
let screensSeq = 0;

/** The widgetReloads count for its type when the widget's meta was last fetched; 0 before any fetch. */
export function hydratedReloadCount(id: string): number {
  return hydratedAtReload.get(id) ?? 0;
}

/**
 * True when a widget needs a detail fetch on mount: it has no meta, its dimensions differ
 * from placement dimensions, or a widget.reload for its type arrived after its meta was fetched.
 */
export function needsHydrate(id: string, dimensions?: [number, number]): boolean {
  const state = useDisplayStore.getState();
  const entry = state.widgets[id];
  if (!entry?.meta) return true;
  if (
    dimensions !== undefined &&
    entry.meta.dimensions !== undefined &&
    (entry.meta.dimensions[0] !== dimensions[0] || entry.meta.dimensions[1] !== dimensions[1])
  ) {
    return true;
  }
  return (state.widgetReloads[entry.meta.type] ?? 0) > (hydratedAtReload.get(id) ?? 0);
}

interface ReducerContext {
  state: DisplayData;
  patch: Partial<DisplayData>;
  replace: <K extends keyof DisplayData>(key: K, value: DisplayData[K]) => void;
  refetch: Set<string>;
  widgets: Record<string, WidgetEntry> | null;
  carousel: CarouselSlice;
  reloads: Record<string, number> | null;
  layoutBumps: number;
}

function applyWidgetEvent(
  ctx: ReducerContext,
  data: Extract<BufferedEvent, { type: 'widget.update' }>['data']
): void {
  const { widget_id: id, state: health, timestamp, data: widgetData } = data;
  const current = (ctx.widgets ?? ctx.state.widgets)[id];
  if (!current || current.state !== health || !deepEqual(current.data, widgetData)) {
    ctx.widgets ??= { ...ctx.state.widgets };
    // Keep the previous data reference when only the state changed.
    const keepData = current !== undefined && deepEqual(current.data, widgetData);
    ctx.widgets[id] = { ...current, state: health, data: keepData ? current.data : widgetData, timestamp };
  }
  const type = (ctx.widgets ?? ctx.state.widgets)[id]?.meta?.type;
  if (type !== undefined && REFETCH_ON_UPDATE.has(type)) {
    ctx.refetch.add(id);
  }
}

function applyScreenRotateEvent(
  ctx: ReducerContext,
  data: Extract<BufferedEvent, { type: 'screen.rotate' }>['data']
): void {
  if (rotateChangesLayout(ctx.state.layout, data)) ctx.layoutBumps++;
  // While paused the latest rotate is kept aside and applied by resume().
  if (ctx.carousel.paused) {
    ctx.carousel = { ...ctx.carousel, pendingRotate: data };
    return;
  }
  ctx.replace('screen', screenFromRotate(data));
}

function applySystemStatusEvent(
  ctx: ReducerContext,
  data: Extract<BufferedEvent, { type: 'system.status' }>['data']
): void {
  const { online, time, config_status, config_error, providers, boot_id } = data;
  // A config change or a new server boot can change the layout; the carousel refetches it.
  if (
    config_status !== ctx.state.system.configStatus ||
    config_error !== ctx.state.system.configError ||
    (boot_id !== undefined && ctx.state.system.bootId !== null && boot_id !== ctx.state.system.bootId)
  ) {
    ctx.layoutBumps++;
  }
  ctx.replace('system', {
    ...ctx.state.system,
    online,
    serverTime: time,
    configStatus: config_status,
    configError: config_error,
    providers: providers ?? {},
    bootId: boot_id ?? ctx.state.system.bootId,
  });
}

function applyWidgetReloadEvent(
  ctx: ReducerContext,
  data: Extract<BufferedEvent, { type: 'widget.reload' }>['data']
): void {
  // Mounted widgets re-hydrate from their WidgetHost, which defers while the carousel is paused.
  ctx.layoutBumps++;
  const type = data.type;
  if (type) {
    ctx.reloads ??= { ...ctx.state.widgetReloads };
    ctx.reloads[type] = (ctx.reloads[type] ?? 0) + 1;
  }
}

/**
 * Pure reducer for a batch of events. Returns the patch to apply and the widget
 * ids that need a refetch. Unchanged slices and widgets are left out of the
 * patch so their references (and subscribers) stay untouched.
 */
export function reduceEvents(
  state: DisplayData,
  events: readonly BufferedEvent[],
): { patch: Partial<DisplayData>; refetch: string[] } {
  const patch: Partial<DisplayData> = {};
  const refetch = new Set<string>();

  const replace = <K extends keyof DisplayData>(key: K, value: DisplayData[K]): void => {
    if (deepEqual(state[key], value)) {
      delete patch[key];
    } else {
      patch[key] = value;
    }
  };

  const ctx: ReducerContext = {
    state,
    patch,
    replace,
    refetch,
    widgets: null,
    carousel: state.carousel,
    reloads: null,
    layoutBumps: 0,
  };

  for (const event of events) {
    switch (event.type) {
      case 'widget.update':
        applyWidgetEvent(ctx, event.data);
        break;
      case 'screen.rotate':
        applyScreenRotateEvent(ctx, event.data);
        break;
      case 'header.update':
        ctx.replace('header', {
          timestamp: event.data.timestamp,
          timezone: event.data.timezone,
          weather: event.data.weather ?? null,
        });
        break;
      case 'theme.update':
        ctx.replace('theme', { theme: event.data.theme });
        break;
      case 'video.state':
        ctx.replace('video', event.data);
        break;
      case 'audio.state':
        ctx.replace('audio', event.data);
        break;
      case 'voice.state':
        if (event.data.state === 'idle') {
          ctx.replace('voice', { ...event.data });
        } else {
          // Active turns produce a fresh object reference so VoiceHud's safety watchdog
          // re-arms on repeated thinking status events (#874) and resetHUD()'s resetForVoice
          // lock clears when a new active turn begins.
          patch.voice = { ...event.data };
        }
        break;
      case 'system.status':
        applySystemStatusEvent(ctx, event.data);
        break;
      case 'widget.reload':
        applyWidgetReloadEvent(ctx, event.data);
        break;
      default:
        // style.reload and system.reload are handled by the runtime, not the store.
        break;
    }
  }

  if (ctx.widgets !== null) patch.widgets = ctx.widgets;
  if (ctx.carousel !== state.carousel) patch.carousel = ctx.carousel;
  if (ctx.reloads !== null) patch.widgetReloads = ctx.reloads;
  if (ctx.layoutBumps > 0) patch.layoutVersion = state.layoutVersion + ctx.layoutBumps;
  return { patch, refetch: [...refetch] };
}

export const useDisplayStore = create<DisplayStore>()((set, get) => ({
  ...initialData,

  applyEvents(events) {
    if (events.length === 0) return;
    const { patch, refetch } = reduceEvents(get(), events);
    if (Object.keys(patch).length > 0) set(patch);
    for (const id of refetch) {
      get().hydrateWidget(id, { fresh: true }).catch((err: unknown) => {
        console.warn(`[mirrormere] widget ${id} refetch failed:`, err);
      });
    }
  },

  hydrateWidget(id, opts) {
    // Two mounts of the same widget (a neighbour pane with two screens) share one fetch,
    // unless a widget reload landed in between or the caller asked for a fresh one.
    const reloadsNow = get().widgetReloads;
    const existing = inflight.get(id);
    if (!opts?.fresh && existing && existing.reloads === reloadsNow) return existing.promise;

    const run = async (): Promise<void> => {
      const seq = (hydrateSeq.get(id) ?? 0) + 1;
      hydrateSeq.set(id, seq);
      const before = get().widgets[id];
      const reloadsAtStart = get().widgetReloads;
      const entry = toWidgetEntry(await fetchWidgetDetail(id));
      if (hydrateSeq.get(id) !== seq) return;
      hydratedAtReload.set(id, reloadsAtStart[entry.meta?.type ?? ''] ?? 0);

      // A widget.update that landed while the fetch was in flight is newer than
      // the fetch for state, data and timestamp. Keep the stream's values and take
      // only meta and viewModel from the fetch.
      const current = get().widgets[id];
      const streamed = current !== before && current !== undefined;
      const incoming: WidgetEntry = streamed
        ? { ...current, meta: entry.meta, viewModel: entry.viewModel }
        : entry;

      if (current && deepEqual(current, incoming)) return;
      const next: WidgetEntry = current && deepEqual(current.data, incoming.data)
        ? { ...incoming, data: current.data }
        : incoming;
      set((s) => ({ widgets: { ...s.widgets, [id]: next } }));
    };
    const promise: Promise<void> = run().finally(() => {
      if (inflight.get(id)?.promise === promise) inflight.delete(id);
    });
    inflight.set(id, { reloads: reloadsNow, promise });
    return promise;
  },

  setConnection(payload) {
    const connection = payload.status === 'connected' ? 'connected' : 'disconnected';
    if (get().system.connection !== connection) {
      set((s) => ({ system: { ...s.system, connection } }));
    }
  },

  setDocumentVisible(visible) {
    if (get().visibility.visible !== visible) {
      set({ visibility: { visible } });
    }
  },

  async loadScreens() {
    const seq = ++screensSeq;
    const body = await fetchScreenLayouts();
    if (seq !== screensSeq) return;
    const layout = body.screens;
    if (deepEqual(get().layout, layout)) return;
    set((s) => ({
      layout,
      screen: s.screen.totalScreens === layout.length ? s.screen : { ...s.screen, totalScreens: layout.length },
    }));
  },

  pause() {
    const { carousel } = get();
    if (!carousel.paused) set({ carousel: { ...carousel, paused: true } });
  },

  resume() {
    const { carousel } = get();
    if (!carousel.paused) return;
    const pending = carousel.pendingRotate;
    const layoutChanged = pending !== null && rotateChangesLayout(get().layout, pending);
    set({
      carousel: { paused: false, pendingRotate: null },
      ...(pending ? { screen: screenFromRotate(pending) } : {}),
      ...(layoutChanged ? { layoutVersion: get().layoutVersion + 1 } : {}),
    });
  },

  setCurrentScreen(index) {
    if (get().screen.currentScreen === index) return;
    set((s) => ({ screen: { ...s.screen, currentScreen: index } }));
  },

  setLayoutEditorOpen(open) {
    if (get().layoutEditorOpen === open) return;
    set({ layoutEditorOpen: open });
    if (open) {
      get().pause();
    } else {
      get().resume();
    }
  },
}));
