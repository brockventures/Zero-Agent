import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { WidgetPlacement } from '../sse/types';
import { WidgetHost } from '../widgets/WidgetHost';
import { PaneContext, type PaneContextValue } from './PaneContext';

export interface ScreenProps {
  /** Screen index this pane shows. */
  index: number;
  /** -1 previous, 0 current, 1 next, or null for parked. Sets the pane's position in the track. */
  offset: -1 | 0 | 1 | null;
  placements: readonly WidgetPlacement[];
  /** The layout has loaded. The current pane does not claim hydration before that. */
  ready: boolean;
}

/**
 * Runs `step` in the next idle slot, with a 300 ms timeout so it cannot starve. Falls back to
 * a 16 ms timer where requestIdleCallback is missing. Returns a cancel function.
 *
 * Not gated on `data-animating`: holding the step until an animation ends lands it exactly
 * when the settle commit runs, which brings the long task back (#803 bench, 4 of 5 runs).
 */
export function scheduleIdleStep(step: () => void): () => void {
  let cancelled = false;
  const run = () => {
    if (!cancelled) step();
  };
  // Both functions are read once, so the cancel matches the scheduler that queued the step.
  const ric = window.requestIdleCallback;
  const cic = window.cancelIdleCallback;
  if (typeof ric === 'function' && typeof cic === 'function') {
    const id = ric.call(window, run, { timeout: 300 });
    return () => {
      cancelled = true;
      cic.call(window, id);
    };
  }
  const id = window.setTimeout(run, 16);
  return () => {
    cancelled = true;
    window.clearTimeout(id);
  };
}

declare global {
  interface Window {
    __einkReady?: boolean;
  }
}

interface Subscriber {
  index: number;
  getOffset: () => -1 | 0 | 1 | null;
  isReady: () => boolean;
  hasWork: () => boolean;
  step: () => void;
}

const subscribers = new Set<Subscriber>();
let cancelPendingStep: (() => void) | null = null;

function runCoordinator(): void {
  if (cancelPendingStep !== null) return;

  // Prioritize immediate neighbors (-1 or 1) before parked panes (null)
  let candidate: Subscriber | null = null;
  for (const sub of subscribers) {
    if (!sub.isReady() || !sub.hasWork()) continue;
    const off = sub.getOffset();
    if (off === -1 || off === 1) {
      candidate = sub;
      break;
    }
  }

  if (!candidate) {
    for (const sub of subscribers) {
      if (!sub.isReady() || !sub.hasWork()) continue;
      const off = sub.getOffset();
      if (off === null) {
        candidate = sub;
        break;
      }
    }
  }

  if (!candidate) return;

  cancelPendingStep = scheduleIdleStep(() => {
    cancelPendingStep = null;
    // Re-evaluate target in case state/offsets changed during the idle wait
    let target: Subscriber | null = null;
    for (const sub of subscribers) {
      if (!sub.isReady() || !sub.hasWork()) continue;
      const off = sub.getOffset();
      if (off === -1 || off === 1) {
        target = sub;
        break;
      }
    }
    if (!target) {
      for (const sub of subscribers) {
        if (!sub.isReady() || !sub.hasWork()) continue;
        const off = sub.getOffset();
        if (off === null) {
          target = sub;
          break;
        }
      }
    }
    if (target) {
      target.step();
    } else {
      runCoordinator();
    }
  });
}

/**
 * One screen as a CSS grid. Only the current pane carries `id="grid-canvas"` and the
 * e-ink hydration attributes. Neighbours and parked panes are siblings outside it.
 */
export function Screen({ index, offset, placements, ready }: ScreenProps) {
  const isCurrent = offset === 0;
  const ref = useRef<HTMLDivElement>(null);
  const value: PaneContextValue = { isCurrentPane: isCurrent, paneIndex: index };

  const [hydratedCount, setHydratedCount] = useState<number>(() => (isCurrent ? placements.length : 0));
  const hydratedRef = useRef(hydratedCount);
  const offsetRef = useRef(offset);
  const readyRef = useRef(ready);
  const placementsRef = useRef(placements);

  if (isCurrent && hydratedCount < placements.length) {
    setHydratedCount(placements.length);
  }

  useEffect(() => {
    const sub: Subscriber = {
      index,
      getOffset: () => offsetRef.current,
      isReady: () => readyRef.current,
      hasWork: () => {
        if (offsetRef.current === 0) return false;
        return hydratedRef.current < placementsRef.current.length;
      },
      step: () => {
        if (hydratedRef.current < placementsRef.current.length) {
          hydratedRef.current += 1;
          setHydratedCount(hydratedRef.current);
        }
      },
    };
    subscribers.add(sub);
    runCoordinator();

    return () => {
      subscribers.delete(sub);
      // If the picked subscriber unmounts mid-wait, the callback re-evaluates targets on fire so canceling only when subscribers.size === 0 is safe.
      if (subscribers.size === 0 && cancelPendingStep !== null) {
        cancelPendingStep();
        cancelPendingStep = null;
      }
    };
  }, [index]);

  useEffect(() => {
    hydratedRef.current = hydratedCount;
    offsetRef.current = offset;
    readyRef.current = ready;
    placementsRef.current = placements;
    runCoordinator();
  }, [offset, ready, placements, hydratedCount]);

  // Layout effects run after the cell children have mounted, so the stamp means every
  // widget of the current pane is in the DOM.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (isCurrent && ready) {
      el.dataset.widgetCount = String(placements.length);
      el.dataset.screenHydrated = 'true';
      if (typeof window !== 'undefined') {
        window.__einkReady = true;
      }
    } else {
      delete el.dataset.widgetCount;
      delete el.dataset.screenHydrated;
    }
  }, [isCurrent, ready, placements]);

  const visibleCount = isCurrent ? placements.length : hydratedCount;
  const visiblePlacements = placements.slice(0, visibleCount);

  return (
    <PaneContext.Provider value={value}>
      <div
        ref={ref}
        className="grid-pane"
        id={isCurrent ? 'grid-canvas' : undefined}
        style={
          offset === null
            ? { left: '200%', visibility: 'hidden', pointerEvents: 'none' }
            : { left: `${offset * 100}%` }
        }
        aria-hidden={offset === null ? 'true' : undefined}
      >
        {visiblePlacements.map((placement) => (
          <WidgetHost
            key={placement.widget_id}
            id={placement.widget_id}
            origin={placement.origin}
            dimensions={placement.dimensions}
          />
        ))}
      </div>
    </PaneContext.Provider>
  );
}
