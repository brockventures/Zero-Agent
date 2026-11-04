import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BufferedEvent } from '../sse/batch';
import type { WidgetDetailResponse } from '../sse/types';
import { needsHydrate, reduceEvents, useDisplayStore } from './index';

const initial = useDisplayStore.getState();

const widgetUpdate = (id: string, data: unknown, state = 'healthy', timestamp = '2026-10-31T12:00:00Z'): BufferedEvent => ({
  type: 'widget.update',
  data: { widget_id: id, timestamp, state, data },
});

const detail = (overrides: Partial<WidgetDetailResponse> = {}): WidgetDetailResponse => ({
  widget_id: 'weather-1',
  type: 'weather-forecast',
  origin: [0, 0],
  dimensions: [4, 2],
  config: { units: 'metric' },
  state: 'healthy',
  data: { temperature: 12 },
  timestamp: '2026-10-31T12:00:00Z',
  online: true,
  assets_base: 'widget-types/weather-forecast/assets/',
  ...overrides,
});

function stubFetch(body: WidgetDetailResponse | ((url: string) => WidgetDetailResponse)) {
  const fetchMock = vi.fn(async (url: string) => {
    const payload = typeof body === 'function' ? body(url) : body;
    return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('display store', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe('widget.update', () => {
    it('creates an entry with state, data and timestamp and no meta', () => {
      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { n: 1 }, 'degraded')]);
      expect(useDisplayStore.getState().widgets.w1).toEqual({
        state: 'degraded',
        data: { n: 1 },
        timestamp: '2026-10-31T12:00:00Z',
      });
    });

    it('updates state, data and timestamp but keeps meta and viewModel', () => {
      useDisplayStore.setState({
        widgets: {
          w1: {
            meta: { type: 'weather-forecast', config: {}, assets_base: 'a/', origin: [0, 0], dimensions: [1, 1] },
            viewModel: { keep: true },
            state: 'healthy',
            data: { n: 1 },
            timestamp: 'old',
          },
        },
      });

      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { n: 2 }, 'healthy', 'new')]);

      const entry = useDisplayStore.getState().widgets.w1;
      expect(entry.data).toEqual({ n: 2 });
      expect(entry.timestamp).toBe('new');
      expect(entry.meta?.type).toBe('weather-forecast');
      expect(entry.viewModel).toEqual({ keep: true });
    });

    it('causes no store change when data and state are deep-equal', () => {
      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { list: [1, { a: 2 }] })]);
      const before = useDisplayStore.getState().widgets;
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);

      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { list: [1, { a: 2 }] })]);

      expect(listener).not.toHaveBeenCalled();
      expect(useDisplayStore.getState().widgets).toBe(before);
      unsubscribe();
    });

    it('keeps the data reference when only the state changes', () => {
      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { list: [1] }, 'healthy')]);
      const data = useDisplayStore.getState().widgets.w1.data;

      useDisplayStore.getState().applyEvents([widgetUpdate('w1', { list: [1] }, 'degraded')]);

      const entry = useDisplayStore.getState().widgets.w1;
      expect(entry.state).toBe('degraded');
      expect(entry.data).toBe(data);
    });

    it('leaves other widget entries referentially unchanged', () => {
      useDisplayStore.getState().applyEvents([widgetUpdate('a', 1), widgetUpdate('b', 2)]);
      const a = useDisplayStore.getState().widgets.a;

      useDisplayStore.getState().applyEvents([widgetUpdate('b', 3)]);

      expect(useDisplayStore.getState().widgets.a).toBe(a);
    });
  });

  describe('batching', () => {
    it('applies a hydration-sized batch with one store notification', () => {
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);

      useDisplayStore.getState().applyEvents([
        { type: 'screen.rotate', data: { current_screen: 0, total_screens: 2, interval_seconds: 30, widgets: [] } },
        widgetUpdate('a', 1),
        widgetUpdate('b', 2),
        { type: 'header.update', data: { timestamp: 't', timezone: 'America/New_York' } },
        { type: 'audio.state', data: { volume: 40, muted: true } },
        { type: 'theme.update', data: { theme: 'night' } },
      ]);

      expect(listener).toHaveBeenCalledTimes(1);
      const state = useDisplayStore.getState();
      expect(state.screen.totalScreens).toBe(2);
      expect(state.widgets.b.data).toBe(2);
      expect(state.header.timezone).toBe('America/New_York');
      expect(state.audio).toEqual({ volume: 40, muted: true });
      expect(state.theme.theme).toBe('night');
      unsubscribe();
    });

    it('does nothing for an empty batch', () => {
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);
      useDisplayStore.getState().applyEvents([]);
      expect(listener).not.toHaveBeenCalled();
      unsubscribe();
    });
  });

  describe('slices', () => {
    it('maps header, video, voice and system events', () => {
      const store = useDisplayStore.getState();
      store.applyEvents([
        { type: 'header.update', data: { timestamp: 't', timezone: 'UTC', weather: { temperature: 3 } } },
        { type: 'video.state', data: { mode: 'video', primary: null, pip: null } },
        { type: 'voice.state', data: { state: 'speaking', transcript: 'hi', reply: null, tts_engine: 'kokoro', status: null } },
        {
          type: 'system.status',
          data: { online: true, time: 'now', config_status: 'error', config_error: 'bad yaml', providers: { w: 'degraded' }, boot_id: 'b1' },
        },
      ]);
      store.setConnection({ status: 'connected', lastEventId: null });

      const next = useDisplayStore.getState();
      expect(next.header.weather).toEqual({ temperature: 3 });
      expect(next.video.mode).toBe('video');
      expect(next.voice.state).toBe('speaking');
      expect(next.system).toMatchObject({
        connection: 'connected',
        configStatus: 'error',
        configError: 'bad yaml',
        providers: { w: 'degraded' },
        bootId: 'b1',
      });
    });

    it('keeps the previous boot id when system.status omits it', () => {
      const store = useDisplayStore.getState();
      store.applyEvents([{ type: 'system.status', data: { online: true, time: 't', config_status: 'ok', config_error: null, boot_id: 'b1' } }]);
      store.applyEvents([{ type: 'system.status', data: { online: true, time: 't2', config_status: 'ok', config_error: null } }]);
      expect(useDisplayStore.getState().system.bootId).toBe('b1');
    });
  });

  describe('hydrateWidget', () => {
    it('fills meta, state, data, viewModel and timestamp from GET api/widgets/{id}', async () => {
      const fetchMock = stubFetch(
        detail({ widget_id: 'cal', type: 'calendar-grid', view_model: { week: [] }, data: { events: 3 } }),
      );

      await useDisplayStore.getState().hydrateWidget('cal');

      expect(fetchMock).toHaveBeenCalledWith('api/widgets/cal', expect.objectContaining({ headers: { Accept: 'application/json' } }));
      expect(useDisplayStore.getState().widgets.cal).toEqual({
        meta: {
          type: 'calendar-grid',
          config: { units: 'metric' },
          assets_base: 'widget-types/weather-forecast/assets/',
          origin: [0, 0],
          dimensions: [4, 2],
        },
        state: 'healthy',
        data: { events: 3 },
        viewModel: { week: [] },
        timestamp: '2026-10-31T12:00:00Z',
      });
    });

    it('rejects on a non-OK response and leaves the store alone', async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 404 })));
      await expect(useDisplayStore.getState().hydrateWidget('missing')).rejects.toThrow('HTTP 404');
      expect(useDisplayStore.getState().widgets.missing).toBeUndefined();
    });

    it('does not change the store when the fetched entry is identical', async () => {
      stubFetch(detail());
      await useDisplayStore.getState().hydrateWidget('weather-1');
      const before = useDisplayStore.getState().widgets['weather-1'];
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);

      await useDisplayStore.getState().hydrateWidget('weather-1');

      expect(listener).not.toHaveBeenCalled();
      expect(useDisplayStore.getState().widgets['weather-1']).toBe(before);
      unsubscribe();
    });

    it('ignores an older response that resolves after a newer request', async () => {
      const resolvers: Array<(r: Response) => void> = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(() => new Promise<Response>((resolve) => resolvers.push(resolve))),
      );

      const first = useDisplayStore.getState().hydrateWidget('w');
      // A reload between the calls makes the second a separate fetch rather than a shared one.
      useDisplayStore.setState({ widgetReloads: { 'weather-forecast': 1 } });
      const second = useDisplayStore.getState().hydrateWidget('w');
      resolvers[1](new Response(JSON.stringify(detail({ widget_id: 'w', data: 'new' })), { status: 200 }));
      await second;
      resolvers[0](new Response(JSON.stringify(detail({ widget_id: 'w', data: 'old' })), { status: 200 }));
      await first;

      expect(useDisplayStore.getState().widgets.w.data).toBe('new');
    });

    it('shares one fetch between concurrent calls while widgetReloads is unchanged', async () => {
      const fetchMock = stubFetch(detail({ widget_id: 'w' }));

      const first = useDisplayStore.getState().hydrateWidget('w');
      const second = useDisplayStore.getState().hydrateWidget('w');
      await Promise.all([first, second]);

      expect(first).toBe(second);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(useDisplayStore.getState().widgets.w.meta?.type).toBe('weather-forecast');

      // The entry is gone once settled: a later call fetches again.
      await useDisplayStore.getState().hydrateWidget('w');
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('makes a second fetch when a widget reload lands between two calls', async () => {
      const fetchMock = stubFetch(detail({ widget_id: 'w' }));

      const first = useDisplayStore.getState().hydrateWidget('w');
      useDisplayStore.getState().applyEvents([{ type: 'widget.reload', data: { type: 'weather-forecast' } }]);
      const second = useDisplayStore.getState().hydrateWidget('w');
      await Promise.all([first, second]);

      expect(first).not.toBe(second);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('does not let a rejected shared fetch stick: the next call fetches again', async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })));
      const first = useDisplayStore.getState().hydrateWidget('w');
      const second = useDisplayStore.getState().hydrateWidget('w');
      await expect(first).rejects.toThrow('HTTP 500');
      await expect(second).rejects.toThrow('HTTP 500');

      const fetchMock = stubFetch(detail({ widget_id: 'w' }));
      await useDisplayStore.getState().hydrateWidget('w');
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('keeps a stream update that lands while the fetch is in flight', async () => {
      let resolveFetch: (r: Response) => void = () => {};
      vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => (resolveFetch = resolve))));

      const pending = useDisplayStore.getState().hydrateWidget('w');
      useDisplayStore.getState().applyEvents([widgetUpdate('w', { live: true }, 'degraded', 'stream-t')]);
      resolveFetch(new Response(JSON.stringify(detail({ widget_id: 'w', data: { stale: true }, state: 'healthy', timestamp: 'fetch-t' })), { status: 200 }));
      await pending;

      const entry = useDisplayStore.getState().widgets.w;
      expect(entry.data).toEqual({ live: true });
      expect(entry.state).toBe('degraded');
      expect(entry.timestamp).toBe('stream-t');
      expect(entry.meta?.type).toBe('weather-forecast');
    });

    it('re-hydrates calendar-grid widgets on widget.update so viewModel stays current', async () => {
      useDisplayStore.setState({
        widgets: {
          cal: {
            meta: { type: 'calendar-grid', config: {}, assets_base: 'a/', origin: [0, 0], dimensions: [1, 1] },
            state: 'healthy',
            data: { events: 1 },
            viewModel: { week: 'old' },
            timestamp: 't0',
          },
        },
      });
      const fetchMock = stubFetch(detail({ widget_id: 'cal', type: 'calendar-grid', data: { events: 2 }, view_model: { week: 'new' } }));

      useDisplayStore.getState().applyEvents([widgetUpdate('cal', { events: 2 })]);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.waitFor(() => expect(useDisplayStore.getState().widgets.cal.viewModel).toEqual({ week: 'new' }));
      expect(useDisplayStore.getState().widgets.cal.data).toEqual({ events: 2 });
    });

    it('starts a fresh fetch when a calendar-grid widget.update lands while a hydrate is in flight', async () => {
      useDisplayStore.setState({
        widgets: {
          cal: {
            meta: { type: 'calendar-grid', config: {}, assets_base: 'a/', origin: [0, 0], dimensions: [1, 1] },
            state: 'healthy',
            data: { events: 1 },
            viewModel: { week: 'old' },
            timestamp: 't0',
          },
        },
      });
      const resolvers: Array<(r: Response) => void> = [];
      const fetchMock = vi.fn(() => new Promise<Response>((resolve) => resolvers.push(resolve)));
      vi.stubGlobal('fetch', fetchMock);
      const calendarDetail = (viewModel: Record<string, unknown>) =>
        new Response(
          JSON.stringify(detail({ widget_id: 'cal', type: 'calendar-grid', data: { events: 2 }, view_model: viewModel })),
          { status: 200 },
        );

      // The first hydrate is held in flight; the stream update must not reuse it.
      const pending = useDisplayStore.getState().hydrateWidget('cal');
      useDisplayStore.getState().applyEvents([widgetUpdate('cal', { events: 2 })]);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock).toHaveBeenLastCalledWith('api/widgets/cal', expect.anything());

      // The newer fetch resolves first, then the older one lands and must be dropped.
      resolvers[1](calendarDetail({ week: 'second' }));
      await vi.waitFor(() => expect(useDisplayStore.getState().widgets.cal.viewModel).toEqual({ week: 'second' }));
      resolvers[0](calendarDetail({ week: 'first' }));
      await pending;

      expect(useDisplayStore.getState().widgets.cal.viewModel).toEqual({ week: 'second' });
    });

    it('does not refetch widget types outside REFETCH_ON_UPDATE', async () => {
      useDisplayStore.setState({
        widgets: {
          w: {
            meta: { type: 'weather-forecast', config: {}, assets_base: 'a/', origin: [0, 0], dimensions: [1, 1] },
            state: 'healthy',
            data: 1,
            timestamp: 't0',
          },
        },
      });
      const fetchMock = stubFetch(detail());
      useDisplayStore.getState().applyEvents([widgetUpdate('w', 2)]);
      await Promise.resolve();
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('widget.reload', () => {
    it('counts reloads per type and leaves other types alone', () => {
      const { patch } = reduceEvents(useDisplayStore.getState(), [
        { type: 'widget.reload', data: { type: 'T' } },
        { type: 'widget.reload', data: { type: 'T' } },
        { type: 'widget.reload', data: { type: 'U' } },
      ]);
      expect(patch.widgetReloads).toEqual({ T: 2, U: 1 });
    });

    it('adds to the previous counts without mutating them', () => {
      const previous = { T: 1 };
      const state = { ...useDisplayStore.getState(), widgetReloads: previous };
      const { patch } = reduceEvents(state, [{ type: 'widget.reload', data: { type: 'T' } }]);
      expect(patch.widgetReloads).toEqual({ T: 2 });
      expect(previous).toEqual({ T: 1 });
    });

    it('bumps layoutVersion, counts nothing for an empty type, and refetches no widget', () => {
      const { patch, refetch } = reduceEvents(useDisplayStore.getState(), [
        { type: 'widget.reload', data: { type: 'T' } },
        { type: 'widget.reload', data: { type: '' } },
      ]);
      expect(refetch).toEqual([]);
      expect(patch.layoutVersion).toBe(2);
      expect(patch.widgetReloads).toEqual({ T: 1 });
    });
  });

  describe('setDocumentVisible', () => {
    it('updates the visibility slice only when the value changes', () => {
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);
      useDisplayStore.getState().setDocumentVisible(true);
      expect(listener).not.toHaveBeenCalled();
      useDisplayStore.getState().setDocumentVisible(false);
      expect(useDisplayStore.getState().visibility.visible).toBe(false);
      expect(listener).toHaveBeenCalledTimes(1);
      unsubscribe();
    });
  });

  describe('layoutEditorOpen and setLayoutEditorOpen', () => {
    it('defaults layoutEditorOpen to false', () => {
      expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
    });

    it('pauses carousel when layoutEditorOpen is set to true and resumes when set to false', () => {
      expect(useDisplayStore.getState().carousel.paused).toBe(false);
      useDisplayStore.getState().setLayoutEditorOpen(true);
      expect(useDisplayStore.getState().layoutEditorOpen).toBe(true);
      expect(useDisplayStore.getState().carousel.paused).toBe(true);

      // No-op if setting true again
      useDisplayStore.getState().setLayoutEditorOpen(true);
      expect(useDisplayStore.getState().layoutEditorOpen).toBe(true);

      useDisplayStore.getState().setLayoutEditorOpen(false);
      expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
      expect(useDisplayStore.getState().carousel.paused).toBe(false);
    });
  });

  describe('needsHydrate with dimensions', () => {
    it('returns true when widget has no meta', () => {
      expect(needsHydrate('unknown-id')).toBe(true);
      expect(needsHydrate('unknown-id', [2, 1])).toBe(true);
    });

    it('returns true when meta dimensions differ from requested dimensions', () => {
      useDisplayStore.setState({
        widgets: {
          w1: {
            meta: { type: 'calendar-grid', config: {}, assets_base: '', origin: [0, 0], dimensions: [2, 1] },
            state: 'healthy',
            data: {},
            timestamp: 't0',
          },
        },
      });
      expect(needsHydrate('w1', [2, 1])).toBe(false);
      expect(needsHydrate('w1', [3, 2])).toBe(true);
      expect(needsHydrate('w1', [2, 2])).toBe(true);
      expect(needsHydrate('w1')).toBe(false);
    });
  });
});
