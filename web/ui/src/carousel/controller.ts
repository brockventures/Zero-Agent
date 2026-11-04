import { useDisplayStore } from '../store';
import { isExcludedTarget, releaseDirection, SwipeGesture, type Direction, type PointerSample, type SwipeRelease } from './gesture';
import { TrackAnimator } from './track';

export const INACTIVITY_HOLD_MS = 60_000;

export interface CarouselHost {
  /** Shows `index` as the current screen (React state). */
  commit(index: number): void;
  /** Tells the server which screen the user picked. Called only after the settle finishes. */
  select(index: number): void;
}

export interface CarouselControllerOptions {
  viewport: HTMLElement;
  track: HTMLElement;
  host: CarouselHost;
  /** Screen shown at mount. */
  initial: number;
  now?: () => number;
}

/** Index of the neighbour in `direction`, wrapping like the server's advance. */
export function neighbourOf(index: number, total: number, direction: Direction): number {
  return direction === 'next' ? (index + 1) % total : (index - 1 + total) % total;
}

/**
 * Carousel state machine. Owns the swipe gesture and the track animation, and decides
 * when the shown screen changes. Pointer events are wired here, not through React, so
 * a move never re-renders anything.
 *
 * Order of operations for a user swipe: track follows the finger, release decides,
 * the track settles, then the shown screen changes and the POST goes out. No request
 * is made between pointerup and the end of the settle.
 */
export class CarouselController {
  private shown: number;
  private readonly animator: TrackAnimator;
  private readonly gesture = new SwipeGesture();
  private readonly now: () => number;
  private gestureWidth = 0;
  private pointerId: number | null = null;
  /** Index handed to React and not yet rendered. Gestures wait for the render. */
  private awaitingRender: number | null = null;
  /** A server rotate that arrived while the carousel was busy. */
  private queued: number | null = null;
  /** Target of a settle in flight. Cleared on commit and on reset. */
  private settleTarget: number | null = null;
  /** Last index the host rendered (starts at the initial screen). */
  private lastRendered: number;
  private detachStore: (() => void) | null = null;
  private pinnedState = false;
  private holdTimer: ReturnType<typeof setTimeout> | null = null;
  private activityRoot: HTMLElement | null = null;

  constructor(private readonly options: CarouselControllerOptions) {
    this.shown = options.initial;
    this.lastRendered = options.initial;
    this.now = options.now ?? (() => performance.now());
    this.animator = new TrackAnimator(options.track);
  }

  /** Index of the screen the carousel shows (the one the host last committed). */
  get current(): number {
    return this.shown;
  }

  get pinned(): boolean {
    return this.pinnedState;
  }

  get held(): boolean {
    return this.pinnedState || this.holdTimer !== null;
  }

  setPinned(pinned: boolean): void {
    this.clearHoldTimer();
    this.pinnedState = pinned;
    if (!pinned) {
      this.drainQueue();
    }
  }

  recordActivity(): void {
    this.clearHoldTimer();
    this.holdTimer = setTimeout(() => {
      this.holdTimer = null;
      this.drainQueue();
    }, INACTIVITY_HOLD_MS);
  }

  private clearHoldTimer(): void {
    if (this.holdTimer !== null) {
      clearTimeout(this.holdTimer);
      this.holdTimer = null;
    }
  }

  private readonly onActivity = (event: Event): void => {
    if (event.target instanceof Element && event.target.closest('.screen-indicator-pin')) return;
    this.recordActivity();
  };

  attach(): void {
    const { viewport } = this.options;
    viewport.addEventListener('pointerdown', this.onPointerDown);
    viewport.addEventListener('pointermove', this.onPointerMove);
    viewport.addEventListener('pointerup', this.onPointerUp);
    viewport.addEventListener('pointercancel', this.onPointerCancel);
    viewport.addEventListener('wheel', this.onActivity, { passive: true });
    window.addEventListener('message', this.onMessage);
    this.activityRoot = viewport.closest<HTMLElement>('#mirrormere-app') ?? document.getElementById('mirrormere-app');
    if (this.activityRoot) {
      this.activityRoot.addEventListener('pointerdown', this.onActivity);
      this.activityRoot.addEventListener('wheel', this.onActivity, { passive: true });
    }
    this.detachStore = useDisplayStore.subscribe((state, prev) => {
      if (state.screen.totalScreens !== prev.screen.totalScreens) {
        this.layoutChanged(state.screen.totalScreens);
      }
      // Read the live value: layoutChanged may have reset the store, and the `state` snapshot is stale.
      const current = useDisplayStore.getState().screen.currentScreen;
      if (current !== prev.screen.currentScreen) {
        this.requestShow(current);
      }
    });
  }

  dispose(): void {
    this.clearHoldTimer();
    const { viewport } = this.options;
    viewport.removeEventListener('pointerdown', this.onPointerDown);
    viewport.removeEventListener('pointermove', this.onPointerMove);
    viewport.removeEventListener('pointerup', this.onPointerUp);
    viewport.removeEventListener('pointercancel', this.onPointerCancel);
    viewport.removeEventListener('wheel', this.onActivity);
    window.removeEventListener('message', this.onMessage);
    if (this.activityRoot) {
      this.activityRoot.removeEventListener('pointerdown', this.onActivity);
      this.activityRoot.removeEventListener('wheel', this.onActivity);
      this.activityRoot = null;
    }
    this.releaseWindowListeners();
    this.detachStore?.();
    this.detachStore = null;
    this.animator.dispose();
  }

  /**
   * The store's current screen changed: a server rotate, or the echo of our own select.
   * An echo names the screen already shown, so it does nothing.
   */
  requestShow(index: number): void {
    if (index < 0 || index >= this.total()) return;
    if (index === this.shown) {
      // The server is back on the shown screen: a rotate queued before this is stale.
      this.queued = null;
      return;
    }
    if (this.isBusy() || this.held) {
      this.queued = index;
      return;
    }
    this.go(index, false);
  }

  /** User picked a screen with a dot or the pill swipe. */
  goTo(index: number, direction?: Direction): void {
    if (!this.canSwipe() || index === this.shown || this.isBusy()) return;
    this.go(index, true, direction);
  }

  /**
   * The screen count changed. When it left the shown screen, or the target of a settle in
   * flight, out of range, shows a valid screen instead: the store's current screen if it
   * is still in range, else the first.
   */
  layoutChanged(total: number): void {
    if (total < 1) return;
    // A queued rotate to a screen the layout no longer has is dropped.
    if (this.queued !== null && this.queued >= total) this.queued = null;
    if (this.shown >= total || (this.settleTarget !== null && this.settleTarget >= total)) {
      const cur = useDisplayStore.getState().screen.currentScreen;
      const index = cur >= 0 && cur < total ? cur : 0;
      this.reset(index);
    }
  }

  /**
   * Shows `index` without a select, for when the layout shrank under the shown screen.
   * Drops any queued rotate: the layout it was for no longer exists.
   */
  reset(index: number): void {
    this.clearHoldTimer();
    this.pointerId = null;
    this.gesture.cancel();
    // Cancels any settle without running its callback, and any pending frame.
    this.animator.dispose();
    this.animator.reset();
    this.settleTarget = null;
    this.shown = index;
    this.queued = null;
    // React skips the render when the host already shows `index`, so there is nothing to wait for.
    this.awaitingRender = index !== this.lastRendered ? index : null;
    useDisplayStore.getState().setCurrentScreen(index);
    this.options.host.commit(index);
  }

  /** Called by the host after React renders `index`. Resets the track, then runs any queued rotate. */
  rendered(index: number): void {
    this.lastRendered = index;
    if (this.awaitingRender === index) this.awaitingRender = null;
    if (!this.isBusy()) this.animator.reset();
    this.drainQueue();
  }

  private isBusy(): boolean {
    return this.gesture.active || this.animator.busy || this.awaitingRender !== null;
  }

  private canSwipe(): boolean {
    return this.total() > 1 && !useDisplayStore.getState().carousel.paused;
  }

  private total(): number {
    return useDisplayStore.getState().screen.totalScreens;
  }

  private directionTo(index: number): Direction | null {
    const total = this.total();
    if (total <= 1) return null;
    if (index === neighbourOf(this.shown, total, 'next')) return 'next';
    if (index === neighbourOf(this.shown, total, 'prev')) return 'prev';
    return null;
  }

  /** Slides to an adjacent screen; jumps to a non-adjacent one. */
  private go(index: number, select: boolean, explicitDirection?: Direction): void {
    if (select) {
      this.queued = null;
    }
    const direction = explicitDirection ?? this.directionTo(index);
    if (direction === null) {
      this.commit(index, select);
      return;
    }
    this.settleTarget = index;
    this.animator.settle(direction, () => this.commit(index, select));
  }

  private commit(index: number, select: boolean): void {
    this.settleTarget = null;
    this.shown = index;
    this.awaitingRender = index;
    this.options.host.commit(index);
    if (select) this.options.host.select(index);
    // React skips a render for the screen it already shows, so rendered() would never come
    // and the carousel would stay busy. Finish the commit here instead.
    if (index === this.lastRendered) this.rendered(index);
  }

  private drainQueue(): void {
    if (this.queued === null || this.isBusy() || this.held) return;
    const next = this.queued;
    this.queued = null;
    this.requestShow(next);
  }

  private sample(event: PointerEvent): PointerSample {
    return { x: event.clientX, y: event.clientY, t: this.now() };
  }

  private readonly onPointerDown = (event: PointerEvent): void => {
    if (event.isPrimary === false || this.pointerId !== null) return;
    if (isExcludedTarget(event.target)) {
      this.recordActivity();
    }
    const allowed = this.canSwipe() && !this.isBusy() && !isExcludedTarget(event.target);
    if (!this.gesture.begin(this.sample(event), allowed)) return;
    this.pointerId = event.pointerId ?? 0;
    this.gestureWidth = this.options.viewport.clientWidth;
    // The pointer can be released outside the viewport; the window hears that release.
    window.addEventListener('pointerup', this.onPointerUp);
    window.addEventListener('pointercancel', this.onPointerCancel);
  };

  private readonly onPointerMove = (event: PointerEvent): void => {
    if (this.pointerId === null || (event.pointerId ?? 0) !== this.pointerId) return;
    const dx = this.gesture.move(this.sample(event));
    if (dx === null) {
      if (!this.gesture.active) {
        // Vertical intent: the page scrolls and the gesture is over. Free the pointer for the next press.
        this.pointerId = null;
        this.releaseWindowListeners();
        this.drainQueue();
      }
      return;
    }
    if (!this.animator.gesturing) {
      this.animator.beginGesture();
      this.options.viewport.setPointerCapture?.(this.pointerId);
    }
    this.animator.drag(dx);
  };

  private readonly onPointerUp = (event: PointerEvent): void => {
    if (this.pointerId === null || (event.pointerId ?? 0) !== this.pointerId) return;
    this.pointerId = null;
    this.releaseWindowListeners();
    const release = this.gesture.end(this.sample(event));
    this.finishGesture(release);
  };

  private readonly onPointerCancel = (event: PointerEvent): void => {
    if (this.pointerId === null || (event.pointerId ?? 0) !== this.pointerId) return;
    this.pointerId = null;
    this.releaseWindowListeners();
    if (this.gesture.cancel()) {
      this.animator.endGesture();
      this.animator.settle('rest', () => this.afterRest());
    } else {
      this.drainQueue();
    }
  };

  private readonly onMessage = (event: MessageEvent): void => {
    if (!window.location?.origin || window.location.origin === 'null' || event.origin !== window.location.origin) {
      return;
    }
    if (
      event.data &&
      typeof event.data === 'object' &&
      event.data.type === 'hearth-swipe' &&
      (event.data.direction === 'next' || event.data.direction === 'prev')
    ) {
      this.goTo(neighbourOf(this.shown, this.total(), event.data.direction), event.data.direction);
    }
  };

  private finishGesture(release: SwipeRelease | null): void {
    if (release === null) {
      // Tap or vertical scroll: nothing moved, so nothing to settle.
      this.drainQueue();
      return;
    }
    this.animator.endGesture();
    if (!this.canSwipe()) {
      // The layout shrank to one screen, or the carousel paused, mid-drag.
      this.animator.settle('rest', () => this.afterRest());
      return;
    }
    const direction = releaseDirection(release, this.gestureWidth);
    const target = direction === null ? null : neighbourOf(this.shown, this.total(), direction);
    if (target === null || direction === null) {
      this.animator.settle('rest', () => this.afterRest());
      return;
    }
    this.go(target, true, direction);
  }

  private afterRest(): void {
    this.animator.reset();
    this.drainQueue();
  }

  private releaseWindowListeners(): void {
    window.removeEventListener('pointerup', this.onPointerUp);
    window.removeEventListener('pointercancel', this.onPointerCancel);
  }
}
