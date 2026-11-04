/**
 * Pure swipe rules for the carousel: exclusion zones, intent, release decision and
 * velocity. The DOM controller feeds samples in; nothing here touches the DOM.
 */

/** Swipes that start inside these elements belong to the widget, not the carousel. */
const SWIPE_EXCLUDE_SELECTOR = '[data-prevent-screen-swipe]';

/** Movement before a gesture is classed as horizontal or vertical. */
export const INTENT_PX = 10;
/** A release farther than this fraction of the width commits. */
export const COMMIT_DISTANCE_RATIO = 0.25;
/** A release faster than this (px/ms, same sign as the drag) commits. */
const COMMIT_VELOCITY_PX_PER_MS = 0.4;
/** Velocity is measured over the last this many milliseconds of samples. */
const VELOCITY_WINDOW_MS = 100;

const MAX_SAMPLES = 32;

export type Direction = 'next' | 'prev';

export interface PointerSample {
  x: number;
  y: number;
  /** Milliseconds, monotonic (performance.now()). */
  t: number;
}

export interface SwipeRelease {
  /** Horizontal displacement from the press point. */
  dx: number;
  /** Signed horizontal velocity in px/ms over the last VELOCITY_WINDOW_MS. */
  velocity: number;
}

export function isExcludedTarget(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(SWIPE_EXCLUDE_SELECTOR) !== null;
}

/** The screen a release commits to, or null when it springs back. */
export function releaseDirection(release: SwipeRelease, width: number): Direction | null {
  const { dx, velocity } = release;
  if (dx === 0 || width <= 0) return null;
  const direction: Direction = dx < 0 ? 'next' : 'prev';
  const far = Math.abs(dx) > width * COMMIT_DISTANCE_RATIO;
  const fast = Math.abs(velocity) > COMMIT_VELOCITY_PX_PER_MS && Math.sign(velocity) === Math.sign(dx);
  return far || fast ? direction : null;
}

/** Signed velocity over the last VELOCITY_WINDOW_MS. A finger that stopped before lifting gives ~0. */
export function velocityOf(samples: readonly PointerSample[]): number {
  if (samples.length < 2) return 0;
  const last = samples[samples.length - 1];
  const cutoff = last.t - VELOCITY_WINDOW_MS;
  // Reference point: the newest sample at or before the window start, so the span
  // is at least the window even when samples are sparse. Falls back to the first sample.
  let first = samples[0];
  for (let i = samples.length - 1; i >= 0; i--) {
    if (samples[i].t <= cutoff) {
      first = samples[i];
      break;
    }
  }
  const dt = last.t - first.t;
  return dt > 0 ? (last.x - first.x) / dt : 0;
}

type Phase = 'idle' | 'pending' | 'horizontal' | 'vertical';

/** One pointer's journey: pending until it is clearly horizontal or vertical. */
export class SwipeGesture {
  private phase: Phase = 'idle';
  private origin: PointerSample | null = null;
  private samples: PointerSample[] = [];

  /** True once the pointer is tracking, before or after intent is decided. */
  get active(): boolean {
    return this.phase === 'pending' || this.phase === 'horizontal';
  }

  get horizontal(): boolean {
    return this.phase === 'horizontal';
  }

  /** Starts tracking when allowed. Returns false (and stays idle) otherwise. */
  begin(sample: PointerSample, allowed: boolean): boolean {
    this.reset();
    if (!allowed) return false;
    this.phase = 'pending';
    this.origin = sample;
    this.samples = [sample];
    return true;
  }

  /** Records a move. Returns the horizontal displacement once the gesture is horizontal. */
  move(sample: PointerSample): number | null {
    if (!this.active || this.origin === null) return null;
    this.record(sample);
    const dx = sample.x - this.origin.x;
    const dy = sample.y - this.origin.y;
    if (this.phase === 'pending') {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < INTENT_PX) return null;
      if (Math.abs(dy) > Math.abs(dx)) {
        this.phase = 'vertical';
        return null;
      }
      this.phase = 'horizontal';
    }
    return dx;
  }

  /** Ends tracking. Returns the release only when the gesture was horizontal. */
  end(sample: PointerSample): SwipeRelease | null {
    if (!this.active || this.origin === null) {
      this.reset();
      return null;
    }
    this.record(sample);
    const release = this.phase === 'horizontal'
      ? { dx: sample.x - this.origin.x, velocity: velocityOf(this.samples) }
      : null;
    this.reset();
    return release;
  }

  /** Abandons tracking. Returns true when a horizontal drag was in progress (it must spring back). */
  cancel(): boolean {
    const wasHorizontal = this.phase === 'horizontal';
    this.reset();
    return wasHorizontal;
  }

  private record(sample: PointerSample): void {
    this.samples.push(sample);
    if (this.samples.length > MAX_SAMPLES) this.samples.shift();
  }

  private reset(): void {
    this.phase = 'idle';
    this.origin = null;
    this.samples = [];
  }
}
