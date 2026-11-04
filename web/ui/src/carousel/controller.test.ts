import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import { CarouselController, INACTIVITY_HOLD_MS, neighbourOf, type CarouselHost } from './controller';
import { SETTLE_FALLBACK_MS, SETTLE_MS } from './track';

const initial = useDisplayStore.getState();

interface Harness {
  viewport: HTMLDivElement;
  track: HTMLDivElement;
  host: { commit: ReturnType<typeof vi.fn>; select: ReturnType<typeof vi.fn> } & CarouselHost;
  controller: CarouselController;
  clock: { t: number };
  frames: Array<() => void>;
  flushFrames(): void;
  down(x: number, y: number, target?: EventTarget): void;
  move(x: number, y: number, ms?: number): void;
  up(x: number, y: number, ms?: number): void;
  cancel(): void;
  transitionEnd(): void;
}

let harness: Harness | null = null;

function pointer(target: EventTarget, type: string, x: number, y: number, pointerId = 1): void {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { clientX: x, clientY: y, pointerId, isPrimary: true });
  target.dispatchEvent(event);
}

function build(initialIndex = 1, width = 1000): Harness {
  const viewport = document.createElement('div');
  const track = document.createElement('div');
  viewport.appendChild(track);
  document.body.appendChild(viewport);
  Object.defineProperty(viewport, 'clientWidth', { value: width, configurable: true });
  Object.defineProperty(track, 'clientWidth', { value: width, configurable: true });

  const frames: Array<() => void> = [];
  vi.stubGlobal('requestAnimationFrame', (cb: () => void) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});

  const host = { commit: vi.fn(), select: vi.fn() };
  const clock = { t: 0 };
  const controller = new CarouselController({
    viewport,
    track,
    host,
    initial: initialIndex,
    now: () => clock.t,
  });
  controller.attach();

  const h: Harness = {
    viewport,
    track,
    host,
    controller,
    clock,
    frames,
    flushFrames() {
      while (frames.length > 0) frames.shift()?.();
    },
    down(x, y, target = viewport) {
      pointer(target, 'pointerdown', x, y);
    },
    move(x, y, ms = 16) {
      clock.t += ms;
      pointer(viewport, 'pointermove', x, y);
    },
    up(x, y, ms = 16) {
      clock.t += ms;
      pointer(viewport, 'pointerup', x, y);
    },
    cancel() {
      pointer(viewport, 'pointercancel', 0, 0);
    },
    transitionEnd() {
      const event = new Event('transitionend');
      Object.assign(event, { propertyName: 'transform' });
      track.dispatchEvent(event);
    },
  };
  harness = h;
  return h;
}

/**
 * Horizontal swipe from x0 to x1 in four 200 ms steps. The final velocity is the last
 * step's pace, 0.375 px/ms for a 300 px drag: under the flick threshold, so only
 * distance decides.
 */
function swipe(h: Harness, x0: number, x1: number): void {
  h.down(x0, 100);
  const steps = 4;
  for (let i = 1; i <= steps; i++) {
    h.move(x0 + ((x1 - x0) * i) / steps, 100, 200);
  }
  h.flushFrames();
  h.up(x1, 100, 0);
}

describe('neighbourOf', () => {
  it('wraps at both ends, as the server advance does', () => {
    expect(neighbourOf(0, 3, 'prev')).toBe(2);
    expect(neighbourOf(2, 3, 'next')).toBe(0);
    expect(neighbourOf(1, 3, 'next')).toBe(2);
  });
});

describe('CarouselController', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 3 } });
  });

  afterEach(() => {
    harness?.controller.dispose();
    harness?.viewport.remove();
    harness = null;
    document.getElementById('mirrormere-app')?.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('commits a left drag past 25% after the settle, and sends the select only then', () => {
    vi.useFakeTimers();
    const h = build();
    swipe(h, 500, 200);
    h.flushFrames();

    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
    expect(document.body.dataset.animating).toBe('1');

    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).toHaveBeenCalledWith(2);
    expect(document.body.dataset.animating).toBe('0');
  });

  it('waits for transitionend rather than the nominal 250 ms, then settles on the fallback timer', () => {
    vi.useFakeTimers();
    const h = build();
    swipe(h, 500, 200);
    vi.advanceTimersByTime(SETTLE_MS + 100);
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
    vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).toHaveBeenCalledWith(2);
  });

  it('springs back below 25% without committing or selecting', () => {
    vi.useFakeTimers();
    const h = build();
    swipe(h, 500, 400);
    h.flushFrames();
    expect(h.track.style.transform).toBe('translate3d(0px,0,0)');

    h.transitionEnd();
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');
    expect(document.body.dataset.animating).toBe('0');
  });

  it('commits a short flick above 0.4 px/ms', () => {
    vi.useFakeTimers();
    const h = build();
    h.down(500, 100);
    h.move(460, 100);
    h.flushFrames();
    // 60 px over 16 ms is 3.75 px/ms: well past the velocity threshold.
    h.up(440, 100);
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
  });

  it('commits a right drag to the previous screen, wrapping from the first', () => {
    vi.useFakeTimers();
    const h = build(0);
    swipe(h, 300, 700);
    h.flushFrames();
    expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).toHaveBeenCalledWith(2);
  });

  it('treats a tap as nothing', () => {
    const h = build();
    h.down(500, 100);
    h.up(502, 101);
    expect(h.track.style.transform).toBe('');
    expect(h.host.commit).not.toHaveBeenCalled();
  });

  it('releases a vertical intent to the page without moving the track', () => {
    vi.useFakeTimers();
    const h = build();
    h.down(500, 100);
    h.move(505, 140);
    h.move(300, 150);
    h.flushFrames();
    h.up(300, 150);
    expect(h.frames).toHaveLength(0);
    expect(h.track.style.transform).toBe('');
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('starts a new gesture on a fresh press after a vertical release', () => {
    vi.useFakeTimers();
    const h = build();
    h.down(500, 100);
    h.move(505, 140);
    h.flushFrames();
    expect(h.frames).toHaveLength(0);

    // The first pointer is gone; a second pointer drags left past 25% and commits.
    pointer(h.viewport, 'pointerdown', 500, 100, 2);
    for (let i = 1; i <= 4; i++) {
      h.clock.t += 200;
      pointer(h.viewport, 'pointermove', 500 - 75 * i, 100, 2);
    }
    h.flushFrames();
    pointer(h.viewport, 'pointerup', 200, 100, 2);
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).toHaveBeenCalledWith(2);
  });

  it('ends a gesture released on the window and drains a queued rotate', () => {
    vi.useFakeTimers();
    const h = build(0);
    h.down(500, 100);
    h.move(495, 100);
    // Still pending (5 px): the gesture is active, so the rotate is queued.
    h.controller.requestShow(2);
    expect(h.host.commit).not.toHaveBeenCalled();

    h.clock.t += 16;
    pointer(window, 'pointerup', 495, 100);
    // The queued rotate to 2 is a wrap from 0, so it slides 'prev'.
    expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(2);
    expect(h.host.select).not.toHaveBeenCalled();
    h.controller.rendered(2);

    h.controller.goTo(1);
    expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(1);
    expect(h.host.select).toHaveBeenCalledWith(1);
  });

  it('springs back when the browser cancels a horizontal drag', () => {
    vi.useFakeTimers();
    const h = build();
    h.down(500, 100);
    h.move(450, 100);
    h.flushFrames();
    h.cancel();
    expect(h.track.style.transform).toBe('translate3d(0px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).not.toHaveBeenCalled();
  });

  it('ignores a swipe that starts inside an excluded zone', () => {
    vi.useFakeTimers();
    const h = build();
    const guarded = document.createElement('div');
    guarded.setAttribute('data-prevent-screen-swipe', '');
    const cell = document.createElement('span');
    guarded.appendChild(cell);
    h.viewport.appendChild(guarded);

    h.down(500, 100, cell);
    h.move(300, 100);
    h.flushFrames();
    h.up(300, 100);
    expect(h.frames).toHaveLength(0);
    expect(h.track.style.transform).toBe('');
    expect(h.host.commit).not.toHaveBeenCalled();
  });

  it('ignores a swipe when the carousel is paused', () => {
    vi.useFakeTimers();
    const h = build();
    useDisplayStore.getState().pause();
    swipe(h, 500, 200);
    h.flushFrames();
    expect(h.track.style.transform).toBe('');
    expect(h.host.commit).not.toHaveBeenCalled();
  });

  it('ignores swipes when there is only one screen', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 1 } });
    const h = build(0);
    swipe(h, 500, 100);
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');
  });

  it('does nothing when the echo of our own select names the screen already shown', () => {
    vi.useFakeTimers();
    const h = build();
    h.controller.requestShow(1);
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');
    expect(h.frames).toHaveLength(0);
  });

  it('settles a server rotate to an adjacent screen without a select', () => {
    vi.useFakeTimers();
    const h = build();
    h.controller.requestShow(2);
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('jumps without animation to a screen that is not a neighbour', () => {
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
    const h = build(0);
    h.controller.requestShow(2);
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');
  });

  it('queues a server rotate that arrives mid-drag and runs it after the release', () => {
    vi.useFakeTimers();
    const h = build();
    h.down(500, 100);
    h.move(480, 100);
    h.flushFrames();
    h.controller.requestShow(2);
    expect(h.host.commit).not.toHaveBeenCalled();

    // Short drag: springs back, then the queued rotate slides to screen 2.
    h.move(490, 100);
    h.up(490, 100);
    h.transitionEnd();
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');

    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('queues a server rotate that arrives during a settle until React has rendered the commit', () => {
    vi.useFakeTimers();
    const h = build();
    swipe(h, 500, 200);
    h.controller.requestShow(0);
    expect(h.host.commit).not.toHaveBeenCalled();

    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.commit).toHaveBeenCalledTimes(1);

    // React renders screen 2: the queued rotate to 0 (a wrap, so 'next') starts now.
    h.controller.rendered(2);
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(0);
    expect(h.host.commit).toHaveBeenCalledTimes(2);
    expect(h.host.select).toHaveBeenCalledTimes(1);
  });

  it('applies a dot or pill selection with the same slide and a select after the settle', () => {
    vi.useFakeTimers();
    const h = build();
    h.controller.goTo(0);
    expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
    expect(h.host.select).not.toHaveBeenCalled();
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(0);
    expect(h.host.select).toHaveBeenCalledWith(0);
  });

  it('ignores dot selection while paused or already on that screen', () => {
    const h = build();
    h.controller.goTo(1);
    useDisplayStore.getState().pause();
    h.controller.goTo(2);
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('stops listening on dispose', () => {
    vi.useFakeTimers();
    const h = build();
    h.controller.dispose();
    swipe(h, 500, 200);
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');
  });

  it('advances on hearth-swipe window message event and ignores after dispose', () => {
    vi.useFakeTimers();
    const h = build(1);
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'hearth-swipe', direction: 'next' },
      }),
    );
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
    expect(h.host.select).toHaveBeenCalledWith(2);

    h.controller.rendered(2);
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'hearth-swipe', direction: 'prev' },
      }),
    );
    expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(1);
    expect(h.host.select).toHaveBeenCalledWith(1);

    h.controller.rendered(1);
    h.controller.dispose();
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'hearth-swipe', direction: 'next' },
      }),
    );
    expect(h.host.commit).toHaveBeenCalledTimes(2);
  });

  it('rejects cross-origin, null-origin, and malformed hearth-swipe messages', () => {
    vi.useFakeTimers();
    const h = build(1);

    // Cross-origin message
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'https://evil.example.com',
        data: { type: 'hearth-swipe', direction: 'next' },
      }),
    );
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
    expect(h.track.style.transform).toBe('');

    // Null origin
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'null',
        data: { type: 'hearth-swipe', direction: 'next' },
      }),
    );
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();

    // Empty string origin
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: '',
        data: { type: 'hearth-swipe', direction: 'next' },
      }),
    );
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();

    // Malformed data: non-object
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: 'hearth-swipe',
      }),
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: null,
      }),
    );
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();

    // Malformed data: invalid or missing direction
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'hearth-swipe', direction: 'up' },
      }),
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'hearth-swipe' },
      }),
    );
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: window.location.origin,
        data: { type: 'other-event', direction: 'next' },
      }),
    );
    expect(h.host.commit).not.toHaveBeenCalled();
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('keeps the animation count balanced when disposed mid-settle', () => {
    vi.useFakeTimers();
    const h = build();
    swipe(h, 500, 200);
    expect(document.body.dataset.animating).toBe('1');
    h.controller.dispose();
    expect(document.body.dataset.animating).toBe('0');
  });

  it('follows the store when the screen changes', () => {
    vi.useFakeTimers();
    const h = build();
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 3 } });
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenCalledWith(2);
  });

  it('reset during a settle toward another screen never commits or selects that screen', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 4 } });
    const h = build(2);
    swipe(h, 500, 200);
    expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');

    h.controller.reset(0);
    h.transitionEnd();
    vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
    expect(h.host.commit).not.toHaveBeenCalledWith(3);
    expect(h.host.select).not.toHaveBeenCalled();
    expect(document.body.dataset.animating).toBe('0');
  });

  it('reset sets the store current screen to the index shown', () => {
    const h = build(1);
    h.controller.reset(0);
    expect(useDisplayStore.getState().screen.currentScreen).toBe(0);
    expect(h.controller.current).toBe(0);
    expect(h.host.commit).toHaveBeenCalledWith(0);
    expect(h.host.select).not.toHaveBeenCalled();
  });

  it('accepts a new swipe after reset once React has rendered the reset screen', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 4 } });
    const h = build(2);
    swipe(h, 500, 200);
    h.controller.reset(0);
    h.controller.rendered(0);

    // A left drag from screen 0 goes to screen 1.
    swipe(h, 500, 200);
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(1);
    expect(h.host.select).toHaveBeenCalledTimes(1);
    expect(h.host.select).toHaveBeenCalledWith(1);
  });

  it('finishes a commit to the screen React already shows instead of waiting for a render', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 2 } });
    const h = build(1);
    // React shows screen 0 while the controller holds 1 (the mismatch a hydration batch caused).
    h.controller.rendered(0);
    swipe(h, 500, 200);
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(0);
    const busy = (h.controller as unknown as { isBusy(): boolean }).isBusy();
    expect(busy).toBe(false);
    expect(h.track.style.transform).toBe('');

    // The carousel still takes swipes afterwards.
    swipe(h, 500, 200);
    h.transitionEnd();
    expect(h.host.commit).toHaveBeenLastCalledWith(1);
  });

  describe('layout shrink', () => {
    const busy = (h: Harness): boolean => (h.controller as unknown as { isBusy(): boolean }).isBusy();

    it('resets when the total drops under a settle toward the last screen, without committing or selecting it', () => {
      vi.useFakeTimers();
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 4 } });
      const h = build(2);
      swipe(h, 500, 200);
      expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');

      // The select already moved the store to 3; the layout then loses screen 3.
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 3 } });
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, totalScreens: 3 } });
      h.transitionEnd();
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);

      expect(h.host.commit).not.toHaveBeenCalledWith(3);
      expect(h.host.select).not.toHaveBeenCalledWith(3);
      expect(h.controller.current).toBeLessThan(3);
      expect(useDisplayStore.getState().screen.currentScreen).toBe(h.controller.current);
      expect(document.body.dataset.animating).toBe('0');

      h.controller.rendered(h.controller.current);
      const from = h.controller.current;
      swipe(h, 500, 200);
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenLastCalledWith((from + 1) % 3);
      expect(h.host.select).toHaveBeenCalledWith((from + 1) % 3);
    });

    it('drops a queued rotate to a screen the layout no longer has', () => {
      vi.useFakeTimers();
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      const h = build(0);
      h.down(500, 100);
      h.move(450, 100, 400);
      h.flushFrames();
      // A server rotate to 3 arrives mid-drag and is queued; then the layout shrinks to 3 screens.
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 3 } });
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 0, totalScreens: 3 } });
      h.up(450, 100, 400);
      h.transitionEnd();
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      h.controller.rendered(h.controller.current);
      expect(h.host.commit).not.toHaveBeenCalledWith(3);
      expect(h.controller.current).toBe(0);
    });

    it('drops a queued rotate when the server returns to the shown screen', () => {
      vi.useFakeTimers();
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 4 } });
      const h = build(0);
      h.down(500, 100);
      h.move(450, 100, 400);
      h.flushFrames();
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 0 } });
      h.up(450, 100, 400);
      h.transitionEnd();
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      h.controller.rendered(h.controller.current);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);
      expect(h.controller.current).toBe(0);
    });

    it('ignores a store update that puts the screen index past the new total', () => {
      const h = build(1);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 2 } });
      expect(h.controller.current).toBeLessThan(2);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);
      expect((h.controller as unknown as { queued: number | null }).queued).toBeNull();
    });

    it('lands on a valid screen when the total drops under the shown screen and the store moves to it', () => {
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 3 } });
      const h = build(2);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 2, totalScreens: 2 } });
      expect(h.controller.current).toBe(0);
      expect(useDisplayStore.getState().screen.currentScreen).toBe(0);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);
    });

    it('keeps the store current screen when it is still valid', () => {
      const h = build(2);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 2 } });
      expect(h.controller.current).toBe(1);
      expect(h.host.commit).toHaveBeenCalledWith(1);
      expect(h.host.select).not.toHaveBeenCalled();
    });

    it('springs back on release when the total drops to 1 mid-drag', () => {
      vi.useFakeTimers();
      const h = build(0);
      h.down(500, 100);
      h.move(400, 100, 200);
      h.move(200, 100, 200);
      h.flushFrames();
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, totalScreens: 1 } });
      h.up(200, 100, 0);
      expect(h.track.style.transform).toBe('translate3d(0px,0,0)');

      h.transitionEnd();
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      expect(h.host.commit).not.toHaveBeenCalled();
      expect(h.host.select).not.toHaveBeenCalled();
      expect(busy(h)).toBe(false);
    });

    it('ignores a layout change that leaves the shown screen valid', () => {
      const h = build(1);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 1, totalScreens: 5 } });
      expect(h.host.commit).not.toHaveBeenCalled();
    });

    it('leaves the controller not busy when reset names the screen React already rendered', () => {
      const h = build(1);
      h.controller.rendered(1);
      h.controller.reset(1);
      expect(busy(h)).toBe(false);
      h.controller.reset(0);
      expect(busy(h)).toBe(true);
      h.controller.rendered(0);
      expect(busy(h)).toBe(false);
    });
  });

  describe('inactivity hold and pinning', () => {
    it('manages pinned and held getters and resets hold after INACTIVITY_HOLD_MS', () => {
      vi.useFakeTimers();
      const h = build(1);

      expect(h.controller.pinned).toBe(false);
      expect(h.controller.held).toBe(false);

      h.controller.setPinned(true);
      expect(h.controller.pinned).toBe(true);
      expect(h.controller.held).toBe(true);

      h.controller.setPinned(false);
      expect(h.controller.pinned).toBe(false);
      expect(h.controller.held).toBe(false);

      h.controller.recordActivity();
      expect(h.controller.pinned).toBe(false);
      expect(h.controller.held).toBe(true);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS);
      expect(h.controller.held).toBe(false);
    });

    it('queues server rotation during viewport wheel hold and drains on expiry', () => {
      vi.useFakeTimers();
      const h = build(1);

      h.viewport.dispatchEvent(new WheelEvent('wheel', { bubbles: true }));
      expect(h.controller.held).toBe(true);

      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
      expect(h.controller.current).toBe(1);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS);
      expect(h.controller.held).toBe(false);
      expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(2);
    });

    it('activates hold on excluded target pointerdown even without #mirrormere-app', () => {
      vi.useFakeTimers();
      const h = build(1);

      const excluded = document.createElement('div');
      excluded.setAttribute('data-prevent-screen-swipe', 'true');
      h.viewport.appendChild(excluded);

      h.down(200, 200, excluded);
      expect(h.controller.held).toBe(true);

      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 0 } });
      expect(h.controller.current).toBe(1);
      expect(h.host.commit).not.toHaveBeenCalledWith(0);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS);
      expect(h.controller.held).toBe(false);
      expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(0);
    });

    it('tracks activity on #mirrormere-app and extends the hold timer on subsequent events', () => {
      vi.useFakeTimers();
      const app = document.createElement('div');
      app.id = 'mirrormere-app';
      document.body.appendChild(app);

      const h = build(1);
      app.appendChild(h.viewport);

      // Re-attach with #mirrormere-app present in DOM
      h.controller.dispose();
      h.controller.attach();

      app.dispatchEvent(new Event('pointerdown', { bubbles: true }));
      expect(h.controller.held).toBe(true);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS - 5000);
      expect(h.controller.held).toBe(true);

      app.dispatchEvent(new WheelEvent('wheel', { bubbles: true }));
      vi.advanceTimersByTime(10000);
      // Still held because wheel refreshed the 60s timer
      expect(h.controller.held).toBe(true);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS - 10000);
      expect(h.controller.held).toBe(false);
    });

    it('ignores pointerdown originating inside .screen-indicator-pin', () => {
      vi.useFakeTimers();
      const app = document.createElement('div');
      app.id = 'mirrormere-app';
      document.body.appendChild(app);

      const h = build(1);
      app.appendChild(h.viewport);

      h.controller.dispose();
      h.controller.attach();

      const pinButton = document.createElement('button');
      pinButton.className = 'screen-indicator-pin';
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      svg.appendChild(path);
      pinButton.appendChild(svg);
      app.appendChild(pinButton);

      path.dispatchEvent(new Event('pointerdown', { bubbles: true }));
      expect(h.controller.held).toBe(false);
    });

    it('holds server rotation while pinned and drains immediately when unpinned', () => {
      vi.useFakeTimers();
      const h = build(1);

      h.controller.setPinned(true);
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
      expect(h.host.commit).not.toHaveBeenCalledWith(2);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS * 3);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);

      h.controller.setPinned(false);
      expect(h.track.style.transform).toBe('translate3d(-1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(2);
    });

    it('clears queued server rotation when user explicitly navigates with goTo', () => {
      vi.useFakeTimers();
      const h = build(1);

      h.controller.recordActivity();
      useDisplayStore.setState({ screen: { ...useDisplayStore.getState().screen, currentScreen: 2 } });
      expect(h.host.commit).not.toHaveBeenCalledWith(2);

      h.controller.goTo(0);
      h.transitionEnd();
      vi.advanceTimersByTime(SETTLE_FALLBACK_MS);
      h.controller.rendered(0);
      expect(h.controller.current).toBe(0);

      vi.advanceTimersByTime(INACTIVITY_HOLD_MS);
      expect(h.controller.current).toBe(0);
      expect(h.host.commit).not.toHaveBeenCalledWith(2);
    });

    it('cleans up hold timer on reset and dispose', () => {
      vi.useFakeTimers();
      const h = build(1);

      h.controller.recordActivity();
      expect(h.controller.held).toBe(true);
      h.controller.reset(0);
      expect(h.controller.held).toBe(false);

      h.controller.recordActivity();
      expect(h.controller.held).toBe(true);
      h.controller.dispose();
      expect(h.controller.held).toBe(false);
    });
  });

  describe('two-screen (total === 2) settle direction', () => {
    beforeEach(() => {
      useDisplayStore.setState(initial, true);
      useDisplayStore.setState({ screen: { ...initial.screen, currentScreen: 0, totalScreens: 2 } });
    });

    it('settles a prev swipe (right drag) with translate3d(1000px,0,0)', () => {
      vi.useFakeTimers();
      const h = build(0);
      swipe(h, 200, 500);
      h.flushFrames();

      expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(1);
      expect(h.host.select).toHaveBeenCalledWith(1);
    });

    it('settles goTo(target, "prev") with translate3d(1000px,0,0)', () => {
      vi.useFakeTimers();
      const h = build(0);
      h.controller.goTo(1, 'prev');
      expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(1);
      expect(h.host.select).toHaveBeenCalledWith(1);
    });

    it('settles hearth-swipe { direction: "prev" } with translate3d(1000px,0,0)', () => {
      vi.useFakeTimers();
      const h = build(0);
      window.dispatchEvent(
        new MessageEvent('message', {
          origin: window.location.origin,
          data: { type: 'hearth-swipe', direction: 'prev' },
        }),
      );
      expect(h.track.style.transform).toBe('translate3d(1000px,0,0)');
      h.transitionEnd();
      expect(h.host.commit).toHaveBeenCalledWith(1);
      expect(h.host.select).toHaveBeenCalledWith(1);
    });
  });
});
