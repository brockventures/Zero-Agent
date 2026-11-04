import { beginAnimation, endAnimation } from '../perf/animating';

/** Settle animation: 250 ms, transform only (see the carousel spec). */
export const SETTLE_MS = 250;
const SETTLE_EASING = 'cubic-bezier(.2,.8,.2,1)';
/**
 * Backstop only. The settle ends on transitionend, which fires after the transition
 * actually runs. A timer shorter than the transition's start latency ends the settle
 * early, so this is set well past SETTLE_MS. It covers a transform that did not change
 * and a page that was hidden mid-settle.
 */
export const SETTLE_FALLBACK_MS = 1000;

export type SettleTarget = 'next' | 'prev' | 'rest';

/**
 * Moves the carousel track. The finger offset is written in a rAF, never through React.
 * Settles run as a CSS transition on transform; the caller learns when one finishes
 * through the callback. Balances beginAnimation()/endAnimation() around each settle.
 */
export class TrackAnimator {
  private gestureActive = false;
  private settling = false;
  private frame: number | null = null;
  private dragPx = 0;
  private cancelSettle: (() => void) | null = null;

  constructor(private readonly track: HTMLElement) {}

  /** A gesture or a settle is running. */
  get busy(): boolean {
    return this.gestureActive || this.settling;
  }

  get gesturing(): boolean {
    return this.gestureActive;
  }

  beginGesture(): void {
    this.gestureActive = true;
    this.track.style.willChange = 'transform';
    this.track.style.transition = 'none';
  }

  /** Schedules the finger offset for the next frame. Only the latest value is written. */
  drag(px: number): void {
    this.dragPx = px;
    if (this.frame !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.track.style.transform = `translate3d(${this.dragPx}px,0,0)`;
    });
  }

  endGesture(): void {
    this.gestureActive = false;
  }

  /**
   * Animates the track to one screen width left (next), right (prev) or back to rest.
   * `done` runs once, when the transition ends or the fallback timer fires.
   */
  settle(target: SettleTarget, done: () => void): void {
    this.cancelFrame();
    this.gestureActive = false;
    const width = this.track.clientWidth;
    const px = target === 'next' ? -width : target === 'prev' ? width : 0;

    this.settling = true;
    beginAnimation();
    this.track.style.willChange = 'transform';
    this.track.style.transition = `transform ${SETTLE_MS}ms ${SETTLE_EASING}`;
    // Flush the style so the transition starts from the current transform.
    void this.track.offsetWidth;
    this.track.style.transform = `translate3d(${px}px,0,0)`;

    let timer: ReturnType<typeof setTimeout> | null = null;
    const onEnd = (event: TransitionEvent): void => {
      if (event.target === this.track && event.propertyName === 'transform') complete();
    };
    const complete = (): void => {
      if (this.cancelSettle === null) return;
      this.cancelSettle = null;
      if (timer !== null) clearTimeout(timer);
      this.track.removeEventListener('transitionend', onEnd);
      this.track.style.transition = 'none';
      this.settling = false;
      endAnimation();
      done();
    };
    this.cancelSettle = () => {
      if (timer !== null) clearTimeout(timer);
      this.track.removeEventListener('transitionend', onEnd);
      this.settling = false;
      endAnimation();
    };
    this.track.addEventListener('transitionend', onEnd);
    timer = setTimeout(complete, SETTLE_FALLBACK_MS);
  }

  /** Clears the inline transform so the track sits at rest. Only call when not busy. */
  reset(): void {
    this.cancelFrame();
    this.track.style.transition = 'none';
    this.track.style.transform = '';
    this.track.style.willChange = '';
  }

  dispose(): void {
    this.cancelFrame();
    if (this.cancelSettle !== null) {
      const cancel = this.cancelSettle;
      this.cancelSettle = null;
      cancel();
    }
    this.gestureActive = false;
  }

  private cancelFrame(): void {
    if (this.frame !== null) {
      cancelAnimationFrame(this.frame);
      this.frame = null;
    }
  }
}
