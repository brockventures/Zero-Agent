import { Profiler } from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { endAnimation } from '../perf/animating';
import { useDisplayStore } from '../store';
import {
  DRAW_MIN_MS,
  LEVEL_GAIN,
  LEVEL_SPAN,
  LEVEL_STALE_MS,
  MOUTH_POINTS,
  MOUTH_STEP,
  MOUTH_X0,
  Waveform,
  WaveformMouth,
  type WaveformMouthOptions,
  clamp,
  defaultClock,
  defaultEventSourceFactory,
  defaultReducedMotion,
  easeToward,
  formatPoints,
  framePoints,
  isLevelFrame,
  smoothValues,
} from './Waveform';

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  listeners: Record<string, Array<(e: { data?: unknown }) => void>> = {};
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (e: { data?: unknown }) => void) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type]!.push(cb);
  }

  emit(type: string, data: unknown) {
    const payload = { data: typeof data === 'string' ? data : JSON.stringify(data) };
    this.listeners[type]?.forEach((cb) => cb(payload));
  }

  close() {
    this.closed = true;
  }
}

describe('Waveform and WaveformMouth', () => {
  const initialStore = useDisplayStore.getState();
  let rafCallback: FrameRequestCallback | null = null;
  let rafId = 0;
  let currentTime = 1000;

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    for (let i = 0; i < 5; i++) endAnimation();
    delete window.voiceWaveform;
    MockEventSource.instances = [];
    (window as unknown as { EventSource: unknown }).EventSource = MockEventSource;

    currentTime = 1000;
    rafCallback = null;
    rafId = 0;

    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb: FrameRequestCallback) => {
      rafCallback = cb;
      return ++rafId;
    });

    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {
      rafCallback = null;
    });

    vi.spyOn(performance, 'now').mockImplementation(() => currentTime);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useDisplayStore.setState(initialStore, true);
    for (let i = 0; i < 5; i++) endAnimation();
    delete window.voiceWaveform;
    if (document.body?.dataset?.voiceWaveformUrl) {
      delete document.body.dataset.voiceWaveformUrl;
    }
    if (document.body?.dataset?.voiceWaveformGain) {
      delete document.body.dataset.voiceWaveformGain;
    }
  });

  function triggerRaf(time: number) {
    currentTime = time;
    const cb = rafCallback;
    rafCallback = null;
    if (cb) {
      cb(time);
    }
  }

  const LOUD = new Array(33).fill(100);
  const QUIET = new Array(33).fill(0);

  describe('pure helpers & constants', () => {
    it('exports expected constants', () => {
      expect(MOUTH_POINTS).toBe(33);
      expect(MOUTH_X0).toBe(20);
      expect(MOUTH_STEP).toBe(1.875);
      expect(LEVEL_GAIN).toBe(2.2);
      expect(LEVEL_SPAN).toBe(22);
      expect(LEVEL_STALE_MS).toBe(400);
      expect(DRAW_MIN_MS).toBe(25);
    });

    it('clamp clamps values within range', () => {
      expect(clamp(5, 0, 10)).toBe(5);
      expect(clamp(-5, 0, 10)).toBe(0);
      expect(clamp(15, 0, 10)).toBe(10);
    });

    it('smoothValues performs 3-point spatial smoothing [1, 2, 1] / 4 without mutating input', () => {
      const input = [10, 20, 30, 40];
      const smoothed = smoothValues(input);
      expect(smoothed).not.toBe(input);
      // index 0 uses left neighbor self: (10 + 20 + 20) / 4 = 12.5
      expect(smoothed[0]).toBe(12.5);
      // index 1: (10 + 40 + 30) / 4 = 20
      expect(smoothed[1]).toBe(20);
      // index 2: (20 + 60 + 40) / 4 = 30
      expect(smoothed[2]).toBe(30);
      // index 3 uses right neighbor self: (30 + 80 + 40) / 4 = 37.5
      expect(smoothed[3]).toBe(37.5);
    });

    it('framePoints maps 33 values to mouth points scaled by gain', () => {
      const zeros = new Array(33).fill(0);
      const points = framePoints(zeros, 2.2);
      expect(points.length).toBe(33);
      // With 0 input, y is 50 - 0 = 50
      expect(points[0]).toEqual([20, 50]);
      expect(points[32]).toEqual([20 + 32 * 1.875, 50]);

      // With 100 input, smoothed is 100, clamped level is 1.0, y is 50 - 22 = 28
      const loudPoints = framePoints(LOUD, 2.2);
      expect(loudPoints[0]).toEqual([20, 28]);
    });

    it('easeToward interpolates values exponentially toward target', () => {
      const current = [0, 50];
      const target = [100, 100];
      const result = easeToward(current, target, 20, 20);
      // dt = 20, tau = 20 -> k = 1 - e^(-1) ~= 0.6321
      expect(result[0]).toBeCloseTo(63.21, 1);
      expect(result[1]).toBeCloseTo(81.6, 1);

      // Null or mismatched length returns copy of target
      expect(easeToward(null, target, 20)).toEqual(target);
      expect(easeToward([1], target, 20)).toEqual(target);
    });

    it('isLevelFrame validates 33 finite numeric items', () => {
      expect(isLevelFrame(new Array(33).fill(0))).toBe(true);
      expect(isLevelFrame(new Array(32).fill(0))).toBe(false);
      expect(isLevelFrame(new Array(34).fill(0))).toBe(false);
      expect(isLevelFrame('not an array')).toBe(false);
      expect(isLevelFrame([...new Array(32).fill(0), 'bad'])).toBe(false);
      expect(isLevelFrame([...new Array(32).fill(0), NaN])).toBe(false);
      expect(isLevelFrame([...new Array(32).fill(0), Infinity])).toBe(false);
    });

    it('formatPoints formats points to 2 decimal places separated by spaces', () => {
      const pts: Array<[number, number]> = [
        [20, 50],
        [21.875, 42.123],
      ];
      expect(formatPoints(pts)).toBe('20.00,50.00 21.88,42.12');
    });

    it('defaultClock, defaultEventSourceFactory, and defaultReducedMotion execute safely', () => {
      expect(typeof defaultClock()).toBe('number');
      expect(typeof defaultEventSourceFactory()).toBe('function');
      expect(typeof defaultReducedMotion()).toBe('boolean');
    });

    it('returns false when window.matchMedia throws in defaultReducedMotion', () => {
      const origMatchMedia = window.matchMedia;
      Object.defineProperty(window, 'matchMedia', {
        value: () => {
          throw new Error('matchMedia blocked');
        },
        configurable: true,
        writable: true,
      });
      expect(defaultReducedMotion()).toBe(false);
      if (origMatchMedia) {
        Object.defineProperty(window, 'matchMedia', {
          value: origMatchMedia,
          configurable: true,
          writable: true,
        });
      } else {
        delete (window as unknown as { matchMedia?: unknown }).matchMedia;
      }
    });

    it('drawFrame eases toward flat line when not speaking or levels are stale', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      const mouth = new WaveformMouth({
        levelsUrl: '',
        svgElement: svg,
        polylineElement: polyline,
      });
      mouth.setVoiceState('idle');
      mouth.drawFrame(16);
      expect(polyline.getAttribute('points')).toContain('50.00');
      mouth.destroy();
    });
  });

  describe('WaveformMouth options and lifecycle', () => {
    it('does not connect when levelsUrl is empty', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      const mouth = new WaveformMouth({ levelsUrl: '', svgElement: svg, polylineElement: polyline });
      expect(mouth.element).toBe(svg);
      expect(MockEventSource.instances.length).toBe(0);
      mouth.destroy();
    });

    it('connects to level stream with svgElement and polylineElement provided from React', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      const mouth = new WaveformMouth({
        levelsUrl: 'http://127.0.0.1:8765/levels',
        svgElement: svg,
        polylineElement: polyline,
      });
      expect(mouth.element).toBe(svg);
      expect(mouth.polyline).toBe(polyline);
      expect(MockEventSource.instances.length).toBe(1);

      mouth.destroy();
      expect(mouth.element).toBeNull();
      expect(MockEventSource.instances[0]!.closed).toBe(true);
    });

    it('reuses existing svgElement and polylineElement without removing them on destroy', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      svg.appendChild(polyline);
      document.body.appendChild(svg);

      const mouth = new WaveformMouth({
        levelsUrl: 'http://127.0.0.1:8765/levels',
        svgElement: svg,
        polylineElement: polyline,
      });

      expect(mouth.element).toBe(svg);
      expect(mouth.polyline).toBe(polyline);

      mouth.destroy();
      // Should NOT remove svg from parentNode because React owns it
      expect(svg.parentNode).toBe(document.body);
      document.body.removeChild(svg);
    });
  });

  describe('Waveform React Component', () => {
    it('returns null and does not connect when levelsUrl is unset', () => {
      const { container } = render(<Waveform />);
      expect(container.firstChild).toBeNull();
      expect(MockEventSource.instances.length).toBe(0);
      expect(window.voiceWaveform).toBeUndefined();
    });

    it('resolves levelsUrl and gain from dataset attributes', () => {
      document.body.dataset.voiceWaveformUrl = 'http://127.0.0.1:8765/dataset-levels';
      document.body.dataset.voiceWaveformGain = '3.5';

      const { container } = render(<Waveform />);
      expect(container.querySelector('svg.mm-voice-waveform')).not.toBeNull();
      expect(MockEventSource.instances.length).toBe(1);
      expect(MockEventSource.instances[0]!.url).toBe('http://127.0.0.1:8765/dataset-levels');
      expect(window.voiceWaveform?.gain).toBe(3.5);
    });

    it('starts rAF loop when speaking and stops when non-speaking or hidden', () => {
      const { container } = render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const svg = container.querySelector('svg.mm-voice-waveform');
      const polyline = container.querySelector('polyline.mm-voice-waveform__line');
      expect(svg).not.toBeNull();
      expect(polyline).not.toBeNull();
      expect(window.voiceWaveform).toBeDefined();

      const source = MockEventSource.instances[0]!;

      // Initially idle: rAF loop is not running
      expect(rafCallback).toBeNull();

      // Enter speaking state
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Hello',
            reply: 'Hi there',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });

      // rAF loop has started
      expect(rafCallback).not.toBeNull();

      // Emit a fresh frame
      act(() => {
        source.emit('frame', LOUD);
      });

      // Run rAF frame
      act(() => {
        triggerRaf(1000);
      });

      expect(svg?.classList.contains('is-active')).toBe(true);

      // Document hidden (visibility.visible = false) stops loop and hides waveform
      act(() => {
        useDisplayStore.setState({ visibility: { visible: false } });
      });

      expect(window.voiceWaveform?.looping).toBe(false);
      expect(svg?.classList.contains('is-active')).toBe(false);

      // Restore document visibility restarts loop
      act(() => {
        useDisplayStore.setState({ visibility: { visible: true } });
      });
      expect(window.voiceWaveform?.looping).toBe(true);

      // Transition to idle stops loop
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'idle',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(window.voiceWaveform?.looping).toBe(false);
      expect(svg?.classList.contains('is-active')).toBe(false);
    });

    it('throttles drawing to DRAW_MIN_MS (25ms)', () => {
      render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const mouth = window.voiceWaveform!;
      const source = MockEventSource.instances[0]!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: 'Testing',
            tts_engine: null,
            status: null,
          },
        });
        source.emit('frame', LOUD);
      });

      const drawSpy = vi.spyOn(mouth, 'drawFrame');

      act(() => {
        triggerRaf(1000);
      });
      expect(drawSpy).toHaveBeenCalledTimes(1);

      // Tick 10ms later (< 25ms threshold) -> drawFrame should be skipped
      act(() => {
        triggerRaf(1010);
      });
      expect(drawSpy).toHaveBeenCalledTimes(1);

      // Tick 26ms after first tick (1026 >= 1000 + 25) -> drawFrame runs
      act(() => {
        triggerRaf(1026);
      });
      expect(drawSpy).toHaveBeenCalledTimes(2);
    });

    it('hides waveform after 400ms staleness threshold', () => {
      const { container } = render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const svg = container.querySelector('svg.mm-voice-waveform')!;
      const source = MockEventSource.instances[0]!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: 'Testing staleness',
            tts_engine: null,
            status: null,
          },
        });
        source.emit('frame', LOUD);
      });

      act(() => {
        triggerRaf(1000);
      });
      expect(svg.classList.contains('is-active')).toBe(true);

      // At 1400ms (exactly 400ms since frame at 1000ms), still fresh
      act(() => {
        triggerRaf(1400);
      });
      expect(svg.classList.contains('is-active')).toBe(true);

      // At 1401ms (> 400ms), levels are stale -> is-active removed and animation stopped
      act(() => {
        triggerRaf(1401);
      });
      expect(svg.classList.contains('is-active')).toBe(false);
      expect(window.voiceWaveform?.looping).toBe(false);
      expect(document.body.dataset.animating).toBe('0');

      // Receiving a fresh level frame while still speaking restarts the loop and marks animating
      act(() => {
        source.emit('frame', LOUD);
      });
      expect(svg.classList.contains('is-active')).toBe(true);
      expect(window.voiceWaveform?.looping).toBe(true);
      expect(document.body.dataset.animating).toBe('1');
    });

    it('marks body.dataset.animating = "1" while speaking and returns to "0" across all stop paths', () => {
      const { unmount } = render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const source = MockEventSource.instances[0]!;

      expect(document.body.dataset.animating).toBe('0');

      // 1. Enter speaking -> body.dataset.animating is '1'
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Hello',
            reply: 'Reply 1',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(document.body.dataset.animating).toBe('1');

      // Stop path A: leaving speaking -> returns to '0'
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'idle',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(document.body.dataset.animating).toBe('0');

      // 2. Re-enter speaking with fresh levels -> '1'
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Hello',
            reply: 'Reply 2',
            tts_engine: 'kokoro',
            status: null,
          },
        });
        source.emit('frame', LOUD);
      });
      expect(document.body.dataset.animating).toBe('1');

      // Stop path B: levels going stale (> 400ms) -> returns to '0'
      act(() => {
        triggerRaf(1000 + LEVEL_STALE_MS + 1);
      });
      expect(document.body.dataset.animating).toBe('0');

      // Fresh frame resumes animation while still speaking -> '1'
      act(() => {
        source.emit('frame', LOUD);
      });
      expect(document.body.dataset.animating).toBe('1');

      // Stop path C: document hidden -> returns to '0'
      act(() => {
        useDisplayStore.setState({ visibility: { visible: false } });
      });
      expect(document.body.dataset.animating).toBe('0');

      // Document visible again -> '1'
      act(() => {
        useDisplayStore.setState({ visibility: { visible: true } });
      });
      expect(document.body.dataset.animating).toBe('1');

      // Stop path D: unmount / destroy() -> returns to '0'
      unmount();
      expect(document.body.dataset.animating).toBe('0');
    });

    it('reduced-motion draws latest frame immediately without easing', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      svg.appendChild(polyline);

      const mouth = new WaveformMouth({
        levelsUrl: 'http://127.0.0.1:8765/levels',
        reducedMotion: true,
        clock: () => currentTime,
        raf: window.requestAnimationFrame.bind(window),
        caf: window.cancelAnimationFrame.bind(window),
        svgElement: svg,
        polylineElement: polyline,
      });

      mouth.setVoiceState('speaking');
      mouth.onLevelFrame({ data: JSON.stringify(QUIET) });
      mouth.drawFrame(16);

      mouth.onLevelFrame({ data: JSON.stringify(LOUD) });
      mouth.drawFrame(16);

      const points = mouth.polyline?.getAttribute('points')?.split(' ') ?? [];
      // With reduced motion, points jump immediately to target without easing:
      // index 0 of LOUD has level 1.0, y = 50 - 22 = 28.00
      expect(points[0]).toBe('20.00,28.00');

      mouth.destroy();
    });

    it('proves zero React re-renders occur across incoming level frames via React.Profiler', () => {
      let renderCount = 0;
      const onRender = () => {
        renderCount++;
      };

      const { container } = render(
        <Profiler id="waveform-profiler" onRender={onRender}>
          <Waveform levelsUrl="http://127.0.0.1:8765/levels" />
        </Profiler>,
      );

      // Initial mount render
      expect(renderCount).toBe(1);

      const source = MockEventSource.instances[0]!;

      // Entering speaking state triggers 1 React render (state change boolean)
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Start',
            reply: 'Speaking now',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(renderCount).toBe(2);

      // Now stream 50 consecutive level frames into EventSource and run animation frames
      act(() => {
        for (let i = 0; i < 50; i++) {
          source.emit('frame', i % 2 === 0 ? LOUD : QUIET);
          triggerRaf(1000 + i * 30);
        }
      });

      // Absolute invariant: ZERO additional React re-renders occurred during high-frequency audio frames!
      expect(renderCount).toBe(2);

      // And the DOM polyline points were actually updated
      const polyline = container.querySelector('polyline.mm-voice-waveform__line');
      expect(polyline?.getAttribute('points')).not.toBe('');
    });

    it('exports window.MirrormereWaveform bindings', () => {
      expect(window.MirrormereWaveform).toBeDefined();
      expect(window.MirrormereWaveform?.WaveformMouth).toBe(WaveformMouth);
      expect(window.MirrormereWaveform?.smoothValues).toBe(smoothValues);
      expect(window.MirrormereWaveform?.framePoints).toBe(framePoints);
      expect(window.MirrormereWaveform?.easeToward).toBe(easeToward);
    });

    it('stops loop when document becomes hidden during animation frame', () => {
      render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const mouth = window.voiceWaveform!;
      mouth.setVoiceState('speaking');
      mouth.onLevelFrame({ data: JSON.stringify(new Array(33).fill(0.5)) });
      expect(mouth.looping).toBe(true);

      Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      triggerRaf(2000);
      expect(mouth.looping).toBe(false);
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
    });

    it('stops loop when voiceState becomes idle during active loop', () => {
      render(<Waveform levelsUrl="http://127.0.0.1:8765/levels" />);
      const mouth = window.voiceWaveform!;
      mouth.setVoiceState('speaking');
      mouth.onLevelFrame({ data: JSON.stringify(new Array(33).fill(0.5)) });
      expect(mouth.looping).toBe(true);

      mouth.setVoiceState('idle');
      triggerRaf(2000);
      expect(mouth.looping).toBe(false);
    });

    it('handles eventSourceFactory throwing an error or invalid source', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      const throwingFactory = () => { throw new Error('SSE blocked'); };
      const mouth = new WaveformMouth({
        levelsUrl: 'http://test',
        svgElement: svg,
        polylineElement: polyline,
        eventSourceFactory: throwingFactory,
      });
      expect(mouth.levelSource).toBeNull();
    });

    it('ignores invalid level frames', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      const mouth = new WaveformMouth({
        levelsUrl: 'http://test',
        svgElement: svg,
        polylineElement: polyline,
      });
      expect(() => {
        mouth.onLevelFrame(undefined);
        mouth.onLevelFrame({ data: 123 as unknown as string });
        mouth.onLevelFrame({ data: 'not-json' });
        mouth.onLevelFrame({ data: JSON.stringify([1, 2]) });
      }).not.toThrow();
    });

    it('handles sseClient voice.state with null or primitive payload', () => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      const polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      let handler: ((data: unknown) => void) | null = null;
      const sseClient = {
        on: (ev: string, cb: (data: unknown) => void) => {
          if (ev === 'voice.state') handler = cb;
        },
      };
      const mouth = new WaveformMouth({
        levelsUrl: 'http://test',
        svgElement: svg,
        polylineElement: polyline,
        sseClient: sseClient as unknown as WaveformMouthOptions['sseClient'],
      });
      expect(() => {
        handler?.(null);
        handler?.('invalid-string');
        handler?.({ state: 'speaking' });
      }).not.toThrow();
      expect(mouth.voiceState).toBe('speaking');
    });
  });
});
