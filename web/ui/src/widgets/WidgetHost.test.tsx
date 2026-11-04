import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Carousel } from '../carousel/Carousel';
import { WidgetHost } from './WidgetHost';
import { useDisplayStore } from '../store';
import type { ScreensResponse } from '../sse/types';

const initial = useDisplayStore.getState();

// Widgets on the three panes around screen 1: clock on screens 0, 1 and 2; weather on screen 1.
const screens: ScreensResponse = {
  current_screen: 1,
  interval_seconds: 30,
  screens: [
    {
      index: 0,
      widgets: [
        { widget_id: 'a-0', type: 'clock', origin: [0, 0], dimensions: [2, 1] },
        { widget_id: 'shared', type: 'clock', origin: [2, 0], dimensions: [2, 1] },
      ],
    },
    {
      index: 1,
      widgets: [
        { widget_id: 'b-1', type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2] },
        { widget_id: 'b-2', type: 'clock', origin: [3, 0], dimensions: [3, 1] },
      ],
    },
    {
      index: 2,
      widgets: [{ widget_id: 'c-2', type: 'clock', origin: [0, 1], dimensions: [6, 1] }],
    },
  ],
};

const CLOCK_IDS = ['a-0', 'shared', 'b-2', 'c-2'];

/** Holds the detail fetch for `id` until `until` resolves. */
interface Hold {
  id: string;
  until: Promise<void>;
}

function stubApi(hold?: Hold) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === 'api/screens') {
      return new Response(JSON.stringify(screens), { status: 200 });
    }
    const id = decodeURIComponent(url.replace('api/widgets/', ''));
    if (hold?.id === id) await hold.until;
    const type = screens.screens.flatMap((screen) => screen.widgets).find((w) => w.widget_id === id)?.type ?? 'clock';
    return new Response(
      JSON.stringify({
        widget_id: id,
        type,
        origin: [0, 0],
        dimensions: [1, 1],
        config: {},
        state: 'healthy',
        data: {},
        timestamp: '2026-10-31T12:00:00Z',
        online: true,
        assets_base: '',
      }),
      { status: 200 },
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** Number of widget detail fetches for `id` so far. */
function hydrations(fetchMock: ReturnType<typeof stubApi>, id: string): number {
  return fetchMock.mock.calls.filter(([url]) => url === `api/widgets/${id}`).length;
}

/** Lets the layout fetch, the mount hydrates and any reload hydrates settle. */
async function settle() {
  await act(async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
  });
}

/** Lets neighbour panes finish their idle-slot widget mounts (#803; 16 ms timer fallback in jsdom). */
/**
 * jsdom has no requestIdleCallback, so scheduleIdleStep uses its 16 ms timer there. Tests that
 * wait for neighbour mounts fake only the timer functions and step the clock, with no real sleeps.
 */
function fakeIdleClock(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
}

/**
 * Steps the fake clock until the idle steps are done. act() applies state only when its scope
 * ends, so each pass gives every neighbour pane one step and lets it schedule the next.
 */
async function runIdleSteps(): Promise<void> {
  for (let pass = 0; pass < 20; pass++) {
    await act(async () => {
      vi.advanceTimersByTime(16);
    });
  }
}

async function mountNeighbours() {
  await runIdleSteps();
  await settle();
}

function reload(type: string) {
  act(() => {
    useDisplayStore.getState().applyEvents([{ type: 'widget.reload', data: { type } }]);
  });
}

describe('WidgetHost widget.reload', () => {
  beforeEach(() => {
    fakeIdleClock();
    useDisplayStore.setState(initial, true);
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 3 } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('re-hydrates each mounted widget of the reloaded type exactly once, on every pane', async () => {
    const fetchMock = stubApi();
    render(<Carousel />);
    await settle();
    await mountNeighbours();
    const mounted = Object.fromEntries(CLOCK_IDS.map((id) => [id, hydrations(fetchMock, id)]));
    expect(mounted).toEqual({ 'a-0': 1, shared: 1, 'b-2': 1, 'c-2': 1 });
    const weatherBefore = hydrations(fetchMock, 'b-1');

    reload('clock');
    await settle();

    for (const id of CLOCK_IDS) expect(hydrations(fetchMock, id)).toBe(2);
    expect(hydrations(fetchMock, 'b-1')).toBe(weatherBefore);
  });

  it('defers a reload while paused and re-hydrates once on resume', async () => {
    const fetchMock = stubApi();
    render(<Carousel />);
    await settle();
    await mountNeighbours();

    act(() => useDisplayStore.getState().pause());
    reload('clock');
    reload('clock');
    await settle();
    for (const id of CLOCK_IDS) expect(hydrations(fetchMock, id)).toBe(1);

    act(() => useDisplayStore.getState().resume());
    await settle();
    for (const id of CLOCK_IDS) expect(hydrations(fetchMock, id)).toBe(2);
    expect(hydrations(fetchMock, 'b-1')).toBe(1);
  });

  it('does not re-hydrate on mount for a reload that arrived before the widget had its type', async () => {
    useDisplayStore.setState({ widgetReloads: { clock: 3 } });
    const fetchMock = stubApi();
    render(<Carousel />);
    await settle();
    await mountNeighbours();
    for (const id of CLOCK_IDS) expect(hydrations(fetchMock, id)).toBe(1);
  });
});

describe('WidgetHost reload during the first hydrate', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const cell = () => <WidgetHost id="a-0" origin={[0, 0]} dimensions={[1, 1]} />;

  /** A gate that holds the first detail fetch for a-0 until `release` is called. */
  function gate() {
    let release!: () => void;
    const until = new Promise<void>((resolve) => {
      release = resolve;
    });
    return { until, release };
  }

  it('re-hydrates exactly once after the first fetch when a reload of its type arrived in flight', async () => {
    const held = gate();
    const fetchMock = stubApi({ id: 'a-0', until: held.until });
    render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    reload('clock');
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    held.release();
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
  });

  it('defers a reload that arrived in flight while paused, then re-hydrates once on resume', async () => {
    const held = gate();
    const fetchMock = stubApi({ id: 'a-0', until: held.until });
    act(() => useDisplayStore.getState().pause());
    render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    reload('clock');
    await settle();
    held.release();
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    act(() => useDisplayStore.getState().resume());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
  });
});

describe('WidgetHost remount', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const cell = () => <WidgetHost id="a-0" origin={[0, 0]} dimensions={[1, 1]} />;

  it('re-hydrates on remount when a reload of its type arrived while it was unmounted', async () => {
    const fetchMock = stubApi();
    const first = render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    first.unmount();
    reload('clock');
    render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
  });

  it('does not re-hydrate on remount when no reload arrived while it was unmounted', async () => {
    const fetchMock = stubApi();
    const first = render(cell());
    await settle();

    first.unmount();
    render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);
  });
});

describe('WidgetHost hydrate rejection', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const cell = () => <WidgetHost id="a-0" origin={[0, 0]} dimensions={[1, 1]} />;

  it('catches and logs warning on initial hydrate rejection', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(() => Promise.reject(new Error('Network offline'))),
    );

    render(cell());
    await settle();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('[mirrormere] widget a-0 hydrate failed:'),
      expect.any(Error),
    );
  });

  it('catches and logs warning on reload hydrate rejection', async () => {
    const fetchMock = stubApi();
    render(cell());
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    fetchMock.mockImplementation(async (url: string) => {
      if (url === 'api/widgets/a-0') {
        throw new Error('Reload fetch failed');
      }
      return new Response(JSON.stringify({}), { status: 200 });
    });

    reload('clock');
    await settle();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('[mirrormere] widget a-0 reload hydrate failed:'),
      expect.any(Error),
    );
  });
});

describe('WidgetHost dimension change', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('re-hydrates with fresh fetch when dimensions prop changes', async () => {
    const fetchMock = stubApi();
    const { rerender } = render(<WidgetHost id="a-0" origin={[0, 0]} dimensions={[1, 1]} />);
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(1);

    // Update store meta dimensions to [1, 1] so it matches current
    const currentEntry = useDisplayStore.getState().widgets['a-0'];
    expect(currentEntry?.meta?.dimensions).toEqual([1, 1]);

    // Rerender with changed dimensions [2, 1]
    rerender(<WidgetHost id="a-0" origin={[0, 0]} dimensions={[2, 1]} />);
    await settle();
    expect(hydrations(fetchMock, 'a-0')).toBe(2);
  });
});


