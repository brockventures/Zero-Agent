import { act, cleanup, render } from '@testing-library/react';
import { Profiler, useContext, useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import type { ScreenLayout, ScreensResponse } from '../sse/types';
import captureSource from '../../../../sidecars/eink-renderer/src/capture.js?raw';
import stylesSource from '../styles.css?raw';
import { Carousel, panesFor, paneKey } from './Carousel';
import { SETTLE_FALLBACK_MS } from './track';
import { PaneContext } from './PaneContext';
import { scheduleIdleStep } from './Screen';
import { WIDGET_COMPONENTS } from '../widgets/registry';

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

const initial = useDisplayStore.getState();

const layout: ScreenLayout[] = [
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
];

const screens: ScreensResponse = { current_screen: 1, interval_seconds: 30, screens: layout };

/**
 * The settle predicate from the e-ink renderer, read from its source so the test
 * evaluates the real expression. Raw import keeps the Node-only sidecar out of the bundle.
 */
function settlePredicate(): boolean {
  const match = /SETTLE_PREDICATE_EXPRESSION = `([\s\S]*?)`;/.exec(captureSource);
  if (!match) throw new Error('SETTLE_PREDICATE_EXPRESSION not found in capture.js');
  return new Function(`return (${match[1]});`)() as boolean;
}

type SelectHandler = () => Promise<Response>;

function stubApi(screenBody: ScreensResponse = screens, select: SelectHandler = async () => new Response('{}', { status: 200 })) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === 'api/screens') {
      return new Response(JSON.stringify(screenBody), { status: 200 });
    }
    if (url === 'api/screen/select') {
      return select();
    }
    if (url.endsWith('/render')) {
      return new Response('<div class="widget-card"><span class="legacy-label">legacy</span></div>', { status: 200 });
    }
    const id = decodeURIComponent(url.replace('api/widgets/', ''));
    const type = screenBody.screens.flatMap((screen) => screen.widgets).find((w) => w.widget_id === id)?.type ?? 'clock';
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

function gridCanvas(): HTMLElement | null {
  return document.querySelector<HTMLElement>('#grid-canvas');
}

describe('panesFor and paneKey', () => {
  it('mounts previous, current and next in stable 0..total-1 order, wrapping', () => {
    expect(panesFor(0, 3)).toEqual([
      { index: 0, offset: 0 },
      { index: 1, offset: 1 },
      { index: 2, offset: -1 },
    ]);
  });

  it('mounts only the current pane with one screen', () => {
    expect(panesFor(0, 1)).toEqual([{ index: 0, offset: 0 }]);
  });

  it('gives the previous pane its own key when the two neighbours are the same screen', () => {
    const panes = panesFor(0, 2);
    const keys = panes.map((pane) => paneKey(pane, 2));
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('keys by screen index otherwise, so a neighbour keeps its DOM when it becomes current', () => {
    expect(panesFor(1, 3).map((pane) => paneKey(pane, 3))).toEqual(['0', '1', '2']);
  });

  it('mounts all screens with non-neighbors parked (offset: null) in stable 0..total-1 order when total >= 4', () => {
    expect(panesFor(0, 4)).toEqual([
      { index: 0, offset: 0 },
      { index: 1, offset: 1 },
      { index: 2, offset: null },
      { index: 3, offset: -1 },
    ]);
    expect(panesFor(2, 5)).toEqual([
      { index: 0, offset: null },
      { index: 1, offset: -1 },
      { index: 2, offset: 0 },
      { index: 3, offset: 1 },
      { index: 4, offset: null },
    ]);
  });
});

describe('Carousel', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 3 } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    useDisplayStore.setState(initial, true);
    document.body.innerHTML = '';
    document.body.removeAttribute('data-animating');
  });

  it('loads the layout on mount and mounts every widget of the three panes', async () => {
    fakeIdleClock();
    const fetchMock = stubApi();
    render(<Carousel />);
    await act(async () => {});
    await runIdleSteps();

    expect(fetchMock).toHaveBeenCalledWith('api/screens', expect.anything());
    const hydrated = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.startsWith('api/widgets/') && !url.endsWith('/render'))
      .sort();
    expect(hydrated).toEqual(['api/widgets/a-0', 'api/widgets/b-1', 'api/widgets/b-2', 'api/widgets/c-2', 'api/widgets/shared']);
    expect(document.querySelectorAll('[data-widget-id]')).toHaveLength(5);
  });

  it('puts #grid-canvas and the hydration attributes on the current pane only', async () => {
    fakeIdleClock();
    stubApi();
    render(<Carousel />);
    await act(async () => {});
    await runIdleSteps();

    expect(document.querySelectorAll('#grid-canvas')).toHaveLength(1);
    const canvas = gridCanvas();
    expect(canvas?.dataset.widgetCount).toBe('2');
    expect(canvas?.dataset.screenHydrated).toBe('true');
    expect(Array.from(canvas?.querySelectorAll('[data-widget-id]') ?? []).map((el) => (el as HTMLElement).dataset.widgetId)).toEqual(['b-1', 'b-2']);

    const panes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane'));
    expect(panes).toHaveLength(3);
    const neighbours = panes.filter((pane) => pane.id !== 'grid-canvas');
    expect(neighbours.every((pane) => !pane.dataset.screenHydrated && !pane.dataset.widgetCount)).toBe(true);
    // Neighbour widgets exist, but outside #grid-canvas.
    expect(neighbours.flatMap((pane) => Array.from(pane.querySelectorAll('[data-widget-id]')))).toHaveLength(3);
  });

  it('passes the e-ink settle predicate with neighbour widgets mounted', async () => {
    fakeIdleClock();
    stubApi();
    render(<Carousel />);
    await act(async () => {});
    await runIdleSteps();

    expect(document.readyState).toBe('complete');
    expect(settlePredicate()).toBe(true);
  });

  it('does not pass the settle predicate before the layout has loaded', () => {
    stubApi();
    render(<Carousel />);
    // No flush: the layout fetch has not resolved.
    expect(gridCanvas()?.dataset.screenHydrated).toBeUndefined();
    expect(settlePredicate()).toBe(false);
  });

  it('keeps the shown screen and renders nothing new when the echo names the screen already shown', async () => {
    stubApi();
    const onRender = vi.fn();
    render(
      <Profiler id="carousel" onRender={onRender}>
        <Carousel />
      </Profiler>,
    );
    await act(async () => {});
    const before = onRender.mock.calls.length;

    act(() => {
      useDisplayStore.getState().applyEvents([
        {
          type: 'screen.rotate',
          data: {
            current_screen: 1,
            total_screens: 3,
            interval_seconds: 30,
            widgets: layout[1].widgets.map((w) => ({ widget_id: w.widget_id, origin: w.origin, dimensions: w.dimensions })),
          },
        },
      ]);
    });

    expect(onRender.mock.calls.length).toBe(before);
    expect(gridCanvas()?.querySelectorAll('[data-widget-id]')).toHaveLength(2);
  });

  it('follows a store change that lands between the first render and the controller mount', async () => {
    vi.useFakeTimers();
    stubApi();
    useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 0 } });
    // Layout effects run after Carousel renders and before its passive effects build the
    // controller: the same gap a reconnect hydration batch can land in.
    function MoveStore() {
      useLayoutEffect(() => {
        useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 1 } });
      }, []);
      return null;
    }
    render(
      <>
        <Carousel />
        <MoveStore />
      </>,
    );
    await act(async () => {
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
    });
    expect(gridCanvas()?.querySelector('[data-widget-id="b-1"]')).not.toBeNull();

    // A swipe back to screen 0 still renders it.
    act(() => {
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 0 } });
    });
    await act(async () => {
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
    });
    expect(gridCanvas()?.querySelector('[data-widget-id="a-0"]')).not.toBeNull();
  });

  it('slides to a server-rotated neighbour and re-centres with the new current pane', async () => {
    vi.useFakeTimers();
    const clientWidth = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1000);
    stubApi();
    render(<Carousel />);
    await act(async () => {});

    const track = document.querySelector<HTMLElement>('.carousel-track');
    const oldNext = document.querySelector<HTMLElement>('.grid-pane[style*="left: 100%"]');
    expect(track).not.toBeNull();

    act(() => {
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
    });
    expect(track?.style.transform).toBe('translate3d(-1000px,0,0)');
    expect(document.body.dataset.animating).toBe('1');

    await act(async () => {
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
    });

    // The pane that was next is now the current one: same DOM node, same mounted widgets.
    expect(gridCanvas()).toBe(oldNext);
    expect(track?.style.transform).toBe('');
    expect(gridCanvas()?.querySelectorAll('[data-widget-id]')).toHaveLength(1);
    expect(document.body.dataset.animating).toBe('0');
    clientWidth.mockRestore();
  });

  it('renders widget types with no registered component through the legacy fragment', async () => {
    stubApi();
    render(<Carousel />);
    await act(async () => {});
    const card = document.querySelector<HTMLElement>('[data-widget-id="b-2"]');
    expect(card?.className).toContain('widget-card');
    expect(card?.dataset.widgetType).toBe('clock');
    expect(card?.querySelector('.widget-legacy .legacy-label')?.textContent).toBe('legacy');
  });

  describe('selecting a screen with a dot', () => {
    function clickDot(index: number): void {
      act(() => {
        document.querySelector<HTMLButtonElement>(`[data-screen-index="${index}"]`)?.click();
      });
    }

    function activeDot(): number | null {
      const active = document.querySelector<HTMLButtonElement>('.screen-indicator-dot.active');
      return active ? Number(active.dataset.screenIndex) : null;
    }

    /** Clicks a dot, lets the slide settle, and returns once the select request has been made. */
    async function selectAndSettle(): Promise<void> {
      clickDot(2);
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });
    }

    it('sets currentScreen before the select request is made', async () => {
      vi.useFakeTimers();
      let seenAtRequest: number | undefined;
      stubApi(screens, async () => {
        seenAtRequest = useDisplayStore.getState().screen.currentScreen;
        return new Response('{}', { status: 200 });
      });
      render(<Carousel />);
      await act(async () => {});

      await selectAndSettle();

      expect(seenAtRequest).toBe(2);
      expect(useDisplayStore.getState().screen.currentScreen).toBe(2);
      expect(activeDot()).toBe(2);
    });

    it('keeps the new screen when the select request succeeds', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      await selectAndSettle();
      await act(async () => {});

      expect(useDisplayStore.getState().screen.currentScreen).toBe(2);
      expect(activeDot()).toBe(2);
    });

    it('restores the previous screen and slides back when the request rejects', async () => {
      vi.useFakeTimers();
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
      stubApi(screens, async () => {
        throw new TypeError('network down');
      });
      render(<Carousel />);
      await act(async () => {});

      await selectAndSettle();
      await act(async () => {});

      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
      expect(consoleError).toHaveBeenCalled();
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });
      expect(activeDot()).toBe(1);
    });

    it('does not revert a failed select when a newer rotate has moved the screen on', async () => {
      vi.useFakeTimers();
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
      let rejectSelect: (err: Error) => void = () => {};
      stubApi(screens, () => new Promise<Response>((_, reject) => (rejectSelect = reject)));
      render(<Carousel />);
      await act(async () => {});

      await selectAndSettle();
      expect(useDisplayStore.getState().screen.currentScreen).toBe(2);

      act(() => {
        useDisplayStore.getState().applyEvents([
          {
            type: 'screen.rotate',
            data: {
              current_screen: 0,
              total_screens: 3,
              interval_seconds: 30,
              widgets: layout[0].widgets.map((w) => ({ widget_id: w.widget_id, origin: w.origin, dimensions: w.dimensions })),
            },
          },
        ]);
      });
      expect(useDisplayStore.getState().screen.currentScreen).toBe(0);

      await act(async () => {
        rejectSelect(new TypeError('network down'));
      });

      expect(consoleError).toHaveBeenCalled();
      expect(useDisplayStore.getState().screen.currentScreen).toBe(0);
    });

    it('restores the previous screen and slides back when the request returns 500', async () => {
      vi.useFakeTimers();
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
      stubApi(screens, async () => new Response('', { status: 500 }));
      render(<Carousel />);
      await act(async () => {});

      await selectAndSettle();
      await act(async () => {});

      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
      expect(consoleError).toHaveBeenCalled();
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });
      expect(activeDot()).toBe(1);
    });
  });

  describe('swiping the screen indicator pill', () => {
    function pill(): HTMLElement {
      const el = document.querySelector<HTMLElement>('#screen-indicator');
      if (!el) throw new Error('#screen-indicator not rendered');
      return el;
    }

    function firePill(type: string, x: number, target: EventTarget = pill()): void {
      act(() => {
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.assign(event, { clientX: x, clientY: 100, pointerId: 1, isPrimary: true });
        target.dispatchEvent(event);
      });
    }

    function activeDot(): number | null {
      const active = document.querySelector<HTMLButtonElement>('.screen-indicator-dot.active');
      return active ? Number(active.dataset.screenIndex) : null;
    }

    it('selects the next screen on a 60 px left swipe and captures the pointer on press', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});
      const capture = vi.fn();
      Object.assign(pill(), { setPointerCapture: capture });

      firePill('pointerdown', 500);
      expect(capture).toHaveBeenCalledWith(1);
      firePill('pointerup', 440);
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(2);
      expect(activeDot()).toBe(2);
    });

    it('does not capture a press that starts on a dot, so the dot keeps its click', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});
      const capture = vi.fn();
      Object.assign(pill(), { setPointerCapture: capture });
      const dot = document.querySelector<HTMLButtonElement>('[data-screen-index="0"]');
      if (!dot) throw new Error('dot 0 not rendered');

      firePill('pointerdown', 500, dot);
      expect(capture).not.toHaveBeenCalled();
    });

    it('drops the gesture on pointercancel, so a lone pointerup does nothing', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      firePill('pointerdown', 500);
      firePill('pointercancel', 500);
      firePill('pointerup', 440);
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
      expect(activeDot()).toBe(1);
    });

    it('declares touch-action: none on .screen-indicator so touch drags do not fire pointercancel', () => {
      expect(stylesSource).toMatch(/\.screen-indicator\s*\{[^}]*touch-action:\s*none\s*;/);
    });
  });

  describe('handling hearth-swipe postMessage events', () => {
    it('advances to the next screen on hearth-swipe next message', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'hearth-swipe', direction: 'next' },
          }),
        );
      });
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(2);
    });

    it('advances to the previous screen on hearth-swipe prev message', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'hearth-swipe', direction: 'prev' },
          }),
        );
      });
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(0);
    });

    it('ignores unknown message types or missing direction', async () => {
      vi.useFakeTimers();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            data: { type: 'other-event', direction: 'next' },
          }),
        );
        window.dispatchEvent(
          new MessageEvent('message', {
            data: { type: 'hearth-swipe', direction: 'invalid' },
          }),
        );
        window.dispatchEvent(
          new MessageEvent('message', {
            data: 'string message',
          }),
        );
      });
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
    });

    it('does not advance when totalScreens is 1', async () => {
      vi.useFakeTimers();
      const oneScreen: ScreensResponse = {
        current_screen: 0,
        interval_seconds: 30,
        screens: [{ index: 0, widgets: [{ widget_id: 'a-0', type: 'clock', origin: [0, 0], dimensions: [1, 1] }] }],
      };
      stubApi(oneScreen);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 1 } });
      render(<Carousel />);
      await act(async () => {});

      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'hearth-swipe', direction: 'next' },
          }),
        );
      });
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(0);
    });
  });

  it('shows screen 0 when the layout shrinks under the shown screen', async () => {
    const screenOf = (index: number) => ({
      index,
      widgets: [{ widget_id: `s${index}`, type: 'clock', origin: [0, 0] as [number, number], dimensions: [1, 1] as [number, number] }],
    });
    const fourScreens: ScreensResponse = { current_screen: 3, interval_seconds: 30, screens: [0, 1, 2, 3].map(screenOf) };
    const twoScreens: ScreensResponse = { current_screen: 3, interval_seconds: 30, screens: [0, 1].map(screenOf) };
    stubApi(fourScreens);
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 3, totalScreens: 4 } });
    render(<Carousel />);
    await act(async () => {});
    expect(gridCanvas()?.querySelector('[data-widget-id="s3"]')).not.toBeNull();

    stubApi(twoScreens);
    await act(async () => {
      await useDisplayStore.getState().loadScreens();
    });
    expect(useDisplayStore.getState().screen.totalScreens).toBe(2);
    expect(gridCanvas()?.querySelector('[data-widget-id="s0"]')).not.toBeNull();
    expect(gridCanvas()?.querySelector('[data-widget-id="s3"]')).toBeNull();
  });

  it('refetches the layout when the layout version changes', async () => {
    const fetchMock = stubApi();
    render(<Carousel />);
    await act(async () => {});
    const screenCalls = () => fetchMock.mock.calls.filter(([url]) => url === 'api/screens').length;
    expect(screenCalls()).toBe(1);

    act(() => {
      useDisplayStore.getState().applyEvents([{ type: 'widget.reload', data: { type: 'clock' } }]);
    });
    await act(async () => {});
    expect(screenCalls()).toBe(2);
  });

  it('hides carousel viewport when entering video mode and restores it when returning to widgets mode', async () => {
    stubApi();
    const { container } = render(<Carousel />);
    await act(async () => {});

    const viewport = container.querySelector<HTMLElement>('.carousel-viewport');
    expect(viewport).not.toBeNull();
    expect(viewport?.classList.contains('is-hidden')).toBe(false);
    expect(viewport?.style.display).toBe('');

    act(() => {
      useDisplayStore.setState({
        video: {
          mode: 'video',
          primary: { id: 'cam', stream_url: 'http://cam/mjpeg', type: 'mjpeg' },
          pip: null,
        },
      });
    });

    expect(viewport?.classList.contains('is-hidden')).toBe(true);
    expect(viewport?.style.display).toBe('none');

    act(() => {
      useDisplayStore.setState({
        video: {
          mode: 'widgets',
          primary: null,
          pip: null,
        },
      });
    });

    expect(viewport?.classList.contains('is-hidden')).toBe(false);
    expect(viewport?.style.display).toBe('');
  });

  describe('scheduleIdleStep', () => {
    it('runs the step in a later task using 16ms fallback timer', () => {
      vi.useFakeTimers();
      const step = vi.fn();
      scheduleIdleStep(step);
      expect(step).not.toHaveBeenCalled();
      vi.advanceTimersByTime(16);
      expect(step).toHaveBeenCalledTimes(1);
    });

    it('does not run a cancelled step', () => {
      vi.useFakeTimers();
      const step = vi.fn();
      const cancel = scheduleIdleStep(step);
      cancel();
      vi.advanceTimersByTime(100);
      expect(step).not.toHaveBeenCalled();
    });

    it('uses requestIdleCallback with 300ms timeout when available', () => {
      const ric = vi.fn().mockReturnValue(42);
      const cic = vi.fn();
      vi.stubGlobal('requestIdleCallback', ric);
      vi.stubGlobal('cancelIdleCallback', cic);
      const step = vi.fn();
      const cancel = scheduleIdleStep(step);
      expect(ric).toHaveBeenCalledWith(expect.any(Function), { timeout: 300 });
      cancel();
      expect(cic).toHaveBeenCalledWith(42);
    });

    it('does not run step if cancelled before requestIdleCallback fires', () => {
      let idleCb: (() => void) | null = null;
      vi.stubGlobal('requestIdleCallback', (cb: () => void) => {
        idleCb = cb;
        return 1;
      });
      vi.stubGlobal('cancelIdleCallback', vi.fn());
      const step = vi.fn();
      const cancel = scheduleIdleStep(step);
      cancel();
      if (idleCb) (idleCb as () => void)();
      expect(step).not.toHaveBeenCalled();
    });
  });

  describe('4-screen layout guardrails', () => {
    const screenOf = (index: number, count = 2) => ({
      index,
      widgets: Array.from({ length: count }, (_, i) => ({
        widget_id: `s${index}-w${i}`,
        type: 'clock',
        origin: [i * 2, 0] as [number, number],
        dimensions: [2, 1] as [number, number],
      })),
    });

    const fourScreens: ScreensResponse = {
      current_screen: 0,
      interval_seconds: 30,
      screens: [screenOf(0, 2), screenOf(1, 2), screenOf(2, 2), screenOf(3, 2)],
    };

    it('renders exactly one #grid-canvas in the DOM', async () => {
      fakeIdleClock();
      stubApi(fourScreens);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      render(<Carousel />);
      await act(async () => {});
      await runIdleSteps();

      const canvases = document.querySelectorAll('#grid-canvas');
      expect(canvases).toHaveLength(1);
      const canvas = gridCanvas();
      expect(canvas).not.toBeNull();
      expect(canvas?.querySelectorAll('[data-widget-id^="s0-"]')).toHaveLength(2);
    });

    it('renders parked panes with aria-hidden="true", visibility: hidden, and pointer-events: none', async () => {
      fakeIdleClock();
      stubApi(fourScreens);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      render(<Carousel />);
      await act(async () => {});
      await runIdleSteps();

      const panes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane'));
      expect(panes).toHaveLength(4);

      // In panesFor(0, 4), screen 2 has offset === null (parked)
      const parkedPanes = panes.filter((pane) => pane.getAttribute('aria-hidden') === 'true');
      expect(parkedPanes).toHaveLength(1);

      const parked = parkedPanes[0];
      expect(parked.getAttribute('aria-hidden')).toBe('true');
      expect(parked.style.visibility).toBe('hidden');
      expect(parked.style.pointerEvents).toBe('none');
      expect(parked.style.left).toBe('200%');

      // Active panes (current and neighbors) do not have aria-hidden="true" or hidden visibility
      const activePanes = panes.filter((pane) => pane !== parked);
      expect(activePanes).toHaveLength(3);
      for (const active of activePanes) {
        expect(active.getAttribute('aria-hidden')).toBeNull();
        expect(active.style.visibility).toBe('');
        expect(active.style.pointerEvents).toBe('');
      }
    });

    it('provides isCurrentPane === false via PaneContext to a widget rendered inside a parked pane', async () => {
      fakeIdleClock();
      let capturedIsCurrent: boolean | null = null;
      let capturedPaneIndex: number | null = null;

      const ProbeWidget = () => {
        const { isCurrentPane, paneIndex } = useContext(PaneContext);
        capturedIsCurrent = isCurrentPane;
        capturedPaneIndex = paneIndex;
        return <div data-testid="parked-probe" data-is-current={String(isCurrentPane)} />;
      };

      (WIDGET_COMPONENTS as Record<string, unknown>)['pane-probe'] = ProbeWidget;

      const screensWithProbe: ScreensResponse = {
        current_screen: 0,
        interval_seconds: 30,
        screens: [
          screenOf(0, 1),
          screenOf(1, 1),
          {
            index: 2, // Parked pane
            widgets: [{ widget_id: 'probe-w', type: 'pane-probe', origin: [0, 0], dimensions: [1, 1] }],
          },
          screenOf(3, 1),
        ],
      };

      try {
        stubApi(screensWithProbe);
        useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
        render(<Carousel />);
        await act(async () => {});
        await runIdleSteps();

        const probeEl = document.querySelector('[data-testid="parked-probe"]');
        expect(probeEl).not.toBeNull();
        expect(probeEl?.getAttribute('data-is-current')).toBe('false');
        expect(capturedIsCurrent).toBe(false);
        expect(capturedPaneIndex).toBe(2);
      } finally {
        delete (WIDGET_COMPONENTS as Record<string, unknown>)['pane-probe'];
      }
    });

    it('hydrates all neighbor pane widgets before any parked pane widget when stepping a fake clock', async () => {
      fakeIdleClock();
      stubApi(fourScreens);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      render(<Carousel />);
      await act(async () => {});

      const getNeighborWidgetsCount = () => {
        const neighborPanes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane')).filter(
          (pane) => pane.id !== 'grid-canvas' && pane.getAttribute('aria-hidden') !== 'true',
        );
        return neighborPanes.flatMap((p) => Array.from(p.querySelectorAll('[data-widget-id]'))).length;
      };

      const getParkedWidgetsCount = () => {
        const parkedPanes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane[aria-hidden="true"]'));
        return parkedPanes.flatMap((p) => Array.from(p.querySelectorAll('[data-widget-id]'))).length;
      };

      // Initially, current pane has its 2 widgets, but neighbor and parked panes have 0 widgets hydrated
      expect(getNeighborWidgetsCount()).toBe(0);
      expect(getParkedWidgetsCount()).toBe(0);

      // Step fake clock one idle tick (16ms) at a time:
      // The 4 neighbor widgets (2 in screen 1, 2 in screen 3) must hydrate one by one,
      // while parked pane widgets remain at 0 throughout.
      for (let i = 1; i <= 4; i++) {
        await act(async () => {
          vi.advanceTimersByTime(16);
        });
        expect(getNeighborWidgetsCount()).toBe(i);
        expect(getParkedWidgetsCount()).toBe(0);
      }

      // All 4 neighbor widgets are hydrated; parked pane still has 0 widgets
      expect(getNeighborWidgetsCount()).toBe(4);
      expect(getParkedWidgetsCount()).toBe(0);

      // The next idle step now hydrates the first parked widget
      await act(async () => {
        vi.advanceTimersByTime(16);
      });
      expect(getNeighborWidgetsCount()).toBe(4);
      expect(getParkedWidgetsCount()).toBe(1);

      // And the following step hydrates the second parked widget
      await act(async () => {
        vi.advanceTimersByTime(16);
      });
      expect(getNeighborWidgetsCount()).toBe(4);
      expect(getParkedWidgetsCount()).toBe(2);
    });

    it('preserves the exact same .grid-pane DOM element reference when a parked pane becomes a neighbor on swipe', async () => {
      fakeIdleClock();
      stubApi(fourScreens);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      render(<Carousel />);
      await act(async () => {});
      await runIdleSteps();

      // At screen 0, screen 2 is parked (aria-hidden="true", left: 200%)
      const initialParkedPane = document.querySelector<HTMLElement>('.grid-pane[aria-hidden="true"]');
      expect(initialParkedPane).not.toBeNull();
      expect(initialParkedPane?.style.left).toBe('200%');

      // Swipe to next screen (screen 1). Now screen 2 becomes the next neighbor (offset 1, left: 100%)
      act(() => {
        window.dispatchEvent(
          new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'hearth-swipe', direction: 'next' },
          }),
        );
      });
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });

      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);

      // Screen 2 is now a neighbor at left: 100%
      const newNeighborPane = document.querySelector<HTMLElement>('.grid-pane[style*="left: 100%"]');
      expect(newNeighborPane).not.toBeNull();
      expect(newNeighborPane?.getAttribute('aria-hidden')).toBeNull();

      // Crucial assertion: the DOM element reference must be identical
      expect(newNeighborPane).toBe(initialParkedPane);
    });

    it('preserves the exact same DOM node references in the exact same child order across swipes with zero DOM reordering', async () => {
      fakeIdleClock();
      stubApi(fourScreens);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      render(<Carousel />);
      await act(async () => {});
      await runIdleSteps();

      const initialPanes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane'));
      expect(initialPanes).toHaveLength(4);

      // Swipe sequentially across all screens
      for (let nextScreen = 1; nextScreen <= 3; nextScreen++) {
        act(() => {
          window.dispatchEvent(
            new MessageEvent('message', {
              origin: window.location.origin,
              data: { type: 'hearth-swipe', direction: 'next' },
            }),
          );
        });
        await act(async () => {
          vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
        });
        expect(useDisplayStore.getState().screen.currentScreen).toBe(nextScreen);

        const currentPanes = Array.from(document.querySelectorAll<HTMLElement>('.grid-pane'));
        expect(currentPanes).toHaveLength(4);
        currentPanes.forEach((pane, idx) => {
          expect(pane).toBe(initialPanes[idx]);
        });
      }
    });
  });

  describe('ScreenIndicator pin toggle and rotation hold', () => {
    it('renders pin button and toggles pinned state on click, deferring server rotation until unpinned', async () => {
      fakeIdleClock();
      stubApi();
      render(<Carousel />);
      await act(async () => {});

      const pinBtn = document.querySelector<HTMLButtonElement>('.screen-indicator-pin');
      expect(pinBtn).not.toBeNull();
      expect(pinBtn?.getAttribute('aria-label')).toBe('Pin current screen');
      expect(pinBtn?.getAttribute('aria-pressed')).toBe('false');
      expect(pinBtn?.getAttribute('data-pinned')).toBe('false');
      expect(pinBtn?.classList.contains('active')).toBe(false);

      // Pin the carousel
      act(() => {
        pinBtn?.click();
      });

      expect(pinBtn?.getAttribute('aria-label')).toBe('Unpin screen rotation');
      expect(pinBtn?.getAttribute('aria-pressed')).toBe('true');
      expect(pinBtn?.getAttribute('data-pinned')).toBe('true');
      expect(pinBtn?.classList.contains('active')).toBe(true);

      // Server rotates to screen 2 while pinned
      act(() => {
        useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
      });

      // Still on screen 1 after advance
      await act(async () => {
        vi.advanceTimersByTime(120_000);
      });
      const activeDot1 = document.querySelector<HTMLButtonElement>('.screen-indicator-dot.active');
      expect(Number(activeDot1?.dataset.screenIndex)).toBe(1);

      // Unpin the carousel
      act(() => {
        pinBtn?.click();
      });

      expect(pinBtn?.getAttribute('aria-label')).toBe('Pin current screen');
      expect(pinBtn?.getAttribute('aria-pressed')).toBe('false');
      expect(pinBtn?.getAttribute('data-pinned')).toBe('false');
      expect(pinBtn?.classList.contains('active')).toBe(false);

      // Settle fallback timer advances to queued screen 2
      await act(async () => {
        vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      });
      const activeDot2 = document.querySelector<HTMLButtonElement>('.screen-indicator-dot.active');
      expect(Number(activeDot2?.dataset.screenIndex)).toBe(2);
    });

    it('enforces 800x480 zero-overflow bounds and touch target CSS for .screen-indicator-pin', () => {
      expect(stylesSource).toMatch(/\.screen-indicator-pin\s*\{[^}]*min-width:\s*36px\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin\s*\{[^}]*min-height:\s*0\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin\s*\{[^}]*height:\s*100%\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin\s*\{[^}]*padding:\s*0\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin\s*\{[^}]*margin:\s*0\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin::before\s*\{[^}]*top:\s*-8px\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin::before\s*\{[^}]*bottom:\s*-8px\s*;/);
      expect(stylesSource).toMatch(/\.screen-indicator-pin\.active\s*\{[^}]*color:\s*var\(--mm-lavender/);
    });

    it('settles two-screen ScreenIndicator prev pill swipe with translate3d(1000px,0,0)', async () => {
      vi.useFakeTimers();
      const clientWidth = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1000);
      try {
        const twoScreens: ScreensResponse = {
          current_screen: 0,
          interval_seconds: 30,
          screens: [layout[0], layout[1]],
        };
        stubApi(twoScreens);
        useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 2 } });
        render(<Carousel />);
        await act(async () => {});

        const indicator = document.querySelector<HTMLElement>('#screen-indicator');
        expect(indicator).not.toBeNull();

        // Swipe pill from x=440 to x=500 (60px right drag => 'prev' swipe)
        act(() => {
          const down = new Event('pointerdown', { bubbles: true, cancelable: true });
          Object.assign(down, { clientX: 440, clientY: 100, pointerId: 1, isPrimary: true });
          indicator?.dispatchEvent(down);

          const up = new Event('pointerup', { bubbles: true, cancelable: true });
          Object.assign(up, { clientX: 500, clientY: 100, pointerId: 1, isPrimary: true });
          indicator?.dispatchEvent(up);
        });

        const track = document.querySelector<HTMLElement>('.carousel-track');
        expect(track?.style.transform).toBe('translate3d(1000px,0,0)');

        act(() => {
          const event = new Event('transitionend');
          Object.assign(event, { propertyName: 'transform' });
          track?.dispatchEvent(event);
        });
        await act(async () => {
          vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
        });
        expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
      } finally {
        clientWidth.mockRestore();
      }
    });
  });
});
