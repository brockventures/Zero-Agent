import { useEffect, useRef } from 'react';
import { beginAnimation, endAnimation } from '../perf/animating';
import { useDisplayStore } from '../store';

export const MOUTH_POINTS = 33;
export const MOUTH_X0 = 20;
export const MOUTH_STEP = 1.875;
export const LEVEL_GAIN = 2.2;
export const LEVEL_SPAN = 22;
export const LEVEL_STALE_MS = 400;
const EASE_TAU_MS = 20;
const EASE_MAX_DT_MS = 100;
const DECAY_TAU_MS = 120;
export const DRAW_MIN_MS = 25;

export function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * Spatial smoothing for a 33-value level frame: one pass of the kernel [1, 2, 1] / 4.
 * Edge values reuse themselves as the missing neighbour. Returns a new array; input is not mutated.
 */
export function smoothValues(values: number[]): number[] {
  const last = values.length - 1;
  const out = new Array<number>(values.length);
  for (let i = 0; i <= last; i += 1) {
    const left = values[i > 0 ? i - 1 : 0]!;
    const right = values[i < last ? i + 1 : last]!;
    out[i] = (left + 2 * values[i]! + right) / 4;
  }
  return out;
}

/**
 * Maps 33 waveform levels (-100..100) to mouth points.
 * Values are smoothed first; gain scales the level before clamping to the mouth span.
 */
export function framePoints(values: number[], gain: number = LEVEL_GAIN): Array<[number, number]> {
  const smoothed = smoothValues(values);
  const points: Array<[number, number]> = [];
  for (let i = 0; i < MOUTH_POINTS; i += 1) {
    const level = clamp((smoothed[i]! / 100) * gain, -1, 1);
    points.push([MOUTH_X0 + i * MOUTH_STEP, 50 - level * LEVEL_SPAN]);
  }
  return points;
}

/**
 * Exponential approach from current toward target, one step per frame.
 * dtMs is clamped to 0..100. A null current, or one of a different length, returns a copy of target.
 */
export function easeToward(
  current: number[] | null,
  target: number[],
  dtMs: number,
  tauMs: number = EASE_TAU_MS,
): number[] {
  if (!Array.isArray(current) || current.length !== target.length) return target.slice();
  const dt = Number.isFinite(dtMs) ? clamp(dtMs, 0, EASE_MAX_DT_MS) : 0;
  const k = 1 - Math.exp(-dt / tauMs);
  return target.map((t, i) => current[i]! + (t - current[i]!) * k);
}

export function isLevelFrame(values: unknown): values is number[] {
  return (
    Array.isArray(values) &&
    values.length === MOUTH_POINTS &&
    values.every((v) => typeof v === 'number' && Number.isFinite(v))
  );
}

export function defaultClock(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now();
}

export function defaultEventSourceFactory(): ((url: string) => EventSource) | null {
  return typeof EventSource === 'function' ? (url: string) => new EventSource(url) : null;
}

export function defaultReducedMotion(): boolean {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false;
  } catch {
    return false;
  }
}

export function formatPoints(points: Array<[number, number]>): string {
  return points.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
}

export interface LevelSourceLike {
  addEventListener: (type: string, cb: (event: { data?: unknown }) => void) => void;
  close?: () => void;
}

export interface WaveformMouthOptions {
  sseClient?: { on?: (eventType: string, callback: (data: unknown) => void) => void } | null;
  levelsUrl?: string;
  gain?: number;
  eventSourceFactory?: ((url: string) => LevelSourceLike | EventSource) | null;
  clock?: () => number;
  raf?: ((cb: FrameRequestCallback) => number) | null;
  caf?: ((id: number) => void) | null;
  reducedMotion?: boolean;
  svgElement: SVGSVGElement | HTMLElement;
  polylineElement: SVGPolylineElement | HTMLElement;
}

/**
 * WaveformMouth draws the speaking waveform from the ear daemon's level stream
 * using imperative rAF mutations on refs provided by React.
 */
export class WaveformMouth {
  public sseClient: { on?: (eventType: string, callback: (data: unknown) => void) => void } | null;
  public levelsUrl: string;
  public gain: number;
  public eventSourceFactory: ((url: string) => LevelSourceLike | EventSource) | null;
  public clock: () => number;
  public raf: ((cb: FrameRequestCallback) => number) | null;
  public caf: ((id: number) => void) | null;
  public reducedMotion: boolean;

  public element: SVGSVGElement | HTMLElement | null;
  public polyline: SVGPolylineElement | HTMLElement | null;
  public voiceState: string;
  public levelFrame: number[] | null;
  public levelAt: number | null;
  public levelSource: LevelSourceLike | EventSource | null;
  public shownY: number[] | null;
  public looping: boolean;
  public rafId: number | null;
  public lastTick: number | null;
  public destroyed: boolean;

  constructor(options: WaveformMouthOptions) {
    this.sseClient = options.sseClient || null;
    this.levelsUrl = options.levelsUrl || '';
    this.gain = typeof options.gain === 'number' && options.gain > 0 ? options.gain : LEVEL_GAIN;
    this.eventSourceFactory = options.eventSourceFactory !== undefined
      ? options.eventSourceFactory
      : defaultEventSourceFactory();
    this.clock = options.clock || defaultClock;
    this.raf = options.raf || (typeof window !== 'undefined' && window.requestAnimationFrame
      ? window.requestAnimationFrame.bind(window) : null);
    this.caf = options.caf || (typeof window !== 'undefined' && window.cancelAnimationFrame
      ? window.cancelAnimationFrame.bind(window) : null);
    this.reducedMotion = options.reducedMotion !== undefined
      ? !!options.reducedMotion
      : defaultReducedMotion();

    this.element = options.svgElement || null;
    this.polyline = options.polylineElement || null;
    this.voiceState = 'idle';
    this.levelFrame = null;
    this.levelAt = null;
    this.levelSource = null;
    this.shownY = null;
    this.looping = false;
    this.rafId = null;
    this.lastTick = null;
    this.destroyed = false;

    if (!this.levelsUrl || !this.element || !this.polyline) return;

    this.drawMouth(framePoints(new Array(MOUTH_POINTS).fill(0), this.gain));
    this.openLevels();

    if (this.sseClient && typeof this.sseClient.on === 'function') {
      this.sseClient.on('voice.state', (data: unknown) => {
        const state = typeof data === 'object' && data !== null && 'state' in data
          ? (data as { state: unknown }).state
          : undefined;
        this.setVoiceState(state);
      });
    }
  }

  /**
   * Opens the level stream once. EventSource reconnects by itself. No-op without a factory.
   */
  openLevels(): void {
    if (!this.eventSourceFactory) return;
    try {
      const source = this.eventSourceFactory(this.levelsUrl);
      if (!source || typeof source.addEventListener !== 'function') return;
      source.addEventListener('frame', (e) => this.onLevelFrame(e));
      this.levelSource = source;
    } catch {
      return;
    }
  }

  /** Stores a valid 33-value frame; malformed payloads are ignored. */
  onLevelFrame(e?: { data?: unknown }): void {
    if (!e || typeof e.data !== 'string') return;
    let values: unknown;
    try {
      values = JSON.parse(e.data);
    } catch {
      return;
    }
    if (!isLevelFrame(values)) return;
    this.levelFrame = values;
    this.levelAt = this.clock();
    this.refresh();
    if (this.voiceState === 'speaking') {
      if (this.reducedMotion || !this.looping) {
        this.drawFrame(0);
      }
      this.startLoop();
    }
  }

  /** True when a level frame arrived within the last LEVEL_STALE_MS. */
  hasFreshLevels(): boolean {
    return this.levelAt !== null && this.clock() - this.levelAt <= LEVEL_STALE_MS;
  }

  /** Shows the mouth only while speaking with fresh levels. Returns the visibility. */
  refresh(): boolean {
    const active = this.voiceState === 'speaking' && this.hasFreshLevels();
    if (this.element && this.element.classList) {
      this.element.classList.toggle('is-active', active);
    }
    return active;
  }

  /** Sets the mouth points from a point list. */
  drawMouth(points: Array<[number, number]>): void {
    if (this.polyline) {
      this.polyline.setAttribute('points', formatPoints(points));
    }
  }

  /**
   * Draws one step. With fresh levels the mouth eases toward the latest frame (or jumps to it
   * under reduced motion); otherwise it eases flat.
   */
  drawFrame(dt: number): void {
    if (this.voiceState === 'speaking' && this.hasFreshLevels() && this.levelFrame) {
      const targetY = framePoints(this.levelFrame, this.gain).map(([, y]) => y);
      this.shownY = this.reducedMotion ? targetY : easeToward(this.shownY, targetY, dt);
    } else {
      const flat = new Array(MOUTH_POINTS).fill(50);
      this.shownY = easeToward(this.shownY, flat, dt, DECAY_TAU_MS);
    }
    this.drawMouth(this.shownY.map((y, i) => [MOUTH_X0 + i * MOUTH_STEP, y]));
  }

  /** Starts the speaking loop once. No-op without a raf. */
  startLoop(): void {
    if (this.destroyed || !this.raf || this.looping) return;
    this.looping = true;
    this.lastTick = null;
    beginAnimation();
    const tick = (now: number) => {
      if (!this.looping) return;
      if (typeof document !== 'undefined' && document.hidden) {
        this.stopLoop();
        return;
      }
      const active = this.refresh();
      if (this.levelAt !== null && !active) {
        this.stopLoop();
        return;
      }
      this.rafId = this.raf!(tick);
      if (this.lastTick !== null && now - this.lastTick < DRAW_MIN_MS) return;
      const dt = this.lastTick === null ? 0 : now - this.lastTick;
      this.lastTick = now;
      this.drawFrame(dt);
    };
    this.rafId = this.raf(tick);
  }

  stopLoop(): void {
    if (!this.looping) return;
    if (this.caf && this.rafId !== null) {
      this.caf(this.rafId);
    }
    this.looping = false;
    this.rafId = null;
    this.lastTick = null;
    endAnimation();
  }

  /**
   * Tears the mouth down: stops the animation loop and closes the level stream.
   * Safe to call more than once, and when no levelsUrl was given.
   */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stopLoop();
    if (this.levelSource && typeof this.levelSource.close === 'function') {
      this.levelSource.close();
    }
    this.levelSource = null;
    this.element = null;
    this.polyline = null;
    this.levelFrame = null;
    this.levelAt = null;
    this.shownY = null;
  }

  /**
   * Records the voice state from a voice.state event. Leaving "speaking" hides the mouth
   * and drops stale levels so the next turn starts clean.
   */
  setVoiceState(state: unknown): void {
    this.voiceState = typeof state === 'string' ? state : 'idle';
    if (this.voiceState === 'speaking') {
      this.startLoop();
    } else {
      this.stopLoop();
      this.levelAt = null;
      this.shownY = null;
      this.drawMouth(framePoints(new Array(MOUTH_POINTS).fill(0), this.gain));
    }
    this.refresh();
  }
}

export interface WaveformProps {
  levelsUrl?: string;
  gain?: number;
}

interface WaveformRendererProps {
  levelsUrl: string;
  gain: number;
}

function WaveformRenderer({ levelsUrl, gain }: WaveformRendererProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const polylineRef = useRef<SVGPolylineElement | null>(null);
  const mouthRef = useRef<WaveformMouth | null>(null);

  const isSpeaking = useDisplayStore((s) => s.voice.state === 'speaking');
  const isVisible = useDisplayStore((s) => s.visibility.visible);

  const activeVoiceState = isSpeaking && isVisible ? 'speaking' : 'idle';
  const activeVoiceStateRef = useRef(activeVoiceState);

  useEffect(() => {
    activeVoiceStateRef.current = activeVoiceState;
    mouthRef.current?.setVoiceState(activeVoiceState);
  }, [activeVoiceState]);

  useEffect(() => {
    if (!svgRef.current || !polylineRef.current) return;

    const mouth = new WaveformMouth({
      levelsUrl,
      gain,
      svgElement: svgRef.current,
      polylineElement: polylineRef.current,
    });
    mouthRef.current = mouth;

    mouth.setVoiceState(activeVoiceStateRef.current);

    if (typeof window !== 'undefined') {
      window.voiceWaveform = mouth;
    }

    return () => {
      mouth.destroy();
      if (mouthRef.current === mouth) {
        mouthRef.current = null;
      }
      if (typeof window !== 'undefined' && window.voiceWaveform === mouth) {
        delete window.voiceWaveform;
      }
    };
  }, [levelsUrl, gain]);

  return (
    <svg ref={svgRef} className="mm-voice-waveform" viewBox="0 0 100 100" aria-hidden="true">
      <polyline ref={polylineRef} className="mm-voice-waveform__line" />
    </svg>
  );
}

export function Waveform(props: WaveformProps) {
  const levelsUrl =
    props.levelsUrl ??
    (typeof document !== 'undefined' ? document.body?.dataset?.voiceWaveformUrl ?? '' : '');
  const docGain =
    typeof document !== 'undefined'
      ? parseFloat(document.body?.dataset?.voiceWaveformGain ?? '') || LEVEL_GAIN
      : LEVEL_GAIN;
  const gain = props.gain ?? docGain;

  if (!levelsUrl) {
    return null;
  }

  return <WaveformRenderer levelsUrl={levelsUrl} gain={gain} />;
}

// Window bindings
if (typeof window !== 'undefined') {
  window.MirrormereWaveform = {
    WaveformMouth,
    smoothValues,
    framePoints,
    easeToward,
  };
}

declare global {
  interface Window {
    voiceWaveform?: WaveformMouth;
    MirrormereWaveform?: {
      WaveformMouth: typeof WaveformMouth;
      smoothValues: typeof smoothValues;
      framePoints: typeof framePoints;
      easeToward: typeof easeToward;
    };
  }
}
