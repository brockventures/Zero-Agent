import { useRef, type PointerEvent as ReactPointerEvent } from 'react';

/** Horizontal movement that counts as a pill swipe. Ported from carousel.js (40 px, 1.2:1). */
const PILL_SWIPE_PX = 40;
const PILL_SWIPE_RATIO = 1.2;

export interface ScreenIndicatorProps {
  total: number;
  active: number;
  /** Hidden while the carousel is paused, as the legacy indicator was. */
  hidden: boolean;
  pinned?: boolean;
  onSelect: (index: number) => void;
  onSwipe: (direction: 'next' | 'prev') => void;
  onTogglePin?: () => void;
}

/**
 * Dots plus a pill swipe. Keyed by dot index, so a change of the active screen only
 * toggles classes; the DOM is rebuilt only when the count changes.
 */
export function ScreenIndicator({
  total,
  active,
  hidden,
  pinned = false,
  onSelect,
  onSwipe,
  onTogglePin,
}: ScreenIndicatorProps) {
  const start = useRef<{ x: number; y: number } | null>(null);
  const visible = total > 1 && !hidden;

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    start.current = { x: event.clientX, y: event.clientY };
    // Keeps pointerup on the pill when the finger is released outside it. Not on a dot: capture
    // retargets the click to the pill, so the dot's onClick would never fire (Chromium).
    if (!(event.target instanceof Element && event.target.closest('button'))) {
      event.currentTarget.setPointerCapture?.(event.pointerId);
    }
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const origin = start.current;
    start.current = null;
    if (!origin) return;
    const dx = event.clientX - origin.x;
    const dy = event.clientY - origin.y;
    if (Math.abs(dx) > PILL_SWIPE_PX && Math.abs(dx) > Math.abs(dy) * PILL_SWIPE_RATIO) {
      onSwipe(dx < 0 ? 'next' : 'prev');
    }
  };

  const onPointerCancel = () => {
    start.current = null;
  };

  const dots = Array.from({ length: visible ? total : 0 }, (_, i) => i);
  return (
    <div
      id="screen-indicator"
      className={hidden ? 'screen-indicator paused' : 'screen-indicator'}
      style={{ display: visible ? 'inline-flex' : 'none' }}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      {dots.map((i) => (
        <button
          key={i}
          type="button"
          className={i === active ? 'screen-indicator-dot screen-dot active' : 'screen-indicator-dot screen-dot'}
          aria-label={`Screen ${i + 1} of ${total}`}
          aria-current={i === active ? 'step' : 'false'}
          data-screen-index={i}
          onClick={() => onSelect(i)}
        />
      ))}
      {visible && onTogglePin ? (
        <button
          type="button"
          className={pinned ? 'screen-indicator-pin active' : 'screen-indicator-pin'}
          aria-label={pinned ? 'Unpin screen rotation' : 'Pin current screen'}
          aria-pressed={pinned}
          data-pinned={pinned ? 'true' : 'false'}
          onClick={onTogglePin}
        >
          {pinned ? (
            <svg
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
              <path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
          ) : (
            <svg
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
              <path d="M7 11V7a5 5 0 0 1 9.9-1" />
            </svg>
          )}
        </button>
      ) : null}
    </div>
  );
}
