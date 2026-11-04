import { useEffect, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useDocumentVisible } from '../lifecycle/visibility';
import { useDisplayStore } from '../store';
import { deepEqual } from '../store/equal';
import { resolveActiveTimezone } from './Clock';
import {
  formatRelativeCountdown,
  parseEventDates,
  resolveNextUpEvent,
  selectCalendarEvents,
} from './nextUpHelpers';
import type { RawCalendarEvent } from './nextUpHelpers';

export type { RawCalendarEvent };
export {
  formatRelativeCountdown,
  parseEventDates,
  resolveNextUpEvent,
  selectCalendarEvents,
};

let cachedCalendarWidgets: unknown = null;
let cachedCalendarEvents: RawCalendarEvent[] = [];

/**
 * Hook subscribing to calendar events in display store widgets.
 * Stabilizes the returned array reference via deepEqual so unrelated widget updates do not trigger re-renders.
 */
export function useCalendarEvents(): RawCalendarEvent[] {
  return useDisplayStore((s) => {
    if (s.widgets === cachedCalendarWidgets) {
      return cachedCalendarEvents;
    }
    const nextEvents = selectCalendarEvents(s.widgets);
    if (!deepEqual(cachedCalendarEvents, nextEvents)) {
      cachedCalendarEvents = nextEvents;
    }
    cachedCalendarWidgets = s.widgets;
    return cachedCalendarEvents;
  });
}

export interface NextUpProps {
  now?: Date;
  timezone?: string;
  events?: readonly RawCalendarEvent[];
  onPillClick?: () => void;
}

export function NextUp(props: NextUpProps = {}) {
  const { now: propNow, timezone: propTimezone, events: propEvents, onPillClick } = props;
  const header = useDisplayStore((s) => s.header);
  const storeEvents = useCalendarEvents();
  const visible = useDocumentVisible();

  const [currentInstant, setCurrentInstant] = useState(() => new Date());

  useEffect(() => {
    if (!visible || propNow !== undefined) return;

    let timerId: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const scheduleNext = () => {
      if (cancelled) return;
      const nowMs = Date.now();
      const delay = Math.max(1000, 15000 - (nowMs % 15000));
      timerId = setTimeout(() => {
        if (cancelled) return;
        setCurrentInstant(new Date());
        scheduleNext();
      }, delay);
    };

    scheduleNext();

    return () => {
      cancelled = true;
      if (timerId !== null) clearTimeout(timerId);
    };
  }, [visible, propNow]);

  const activeTz = resolveActiveTimezone(propTimezone, header);
  const events = propEvents ?? storeEvents;
  const now = propNow ?? currentInstant;

  const resolved = resolveNextUpEvent(events, now, activeTz);

  const handlePillClick = () => {
    if (onPillClick) {
      onPillClick();
      return;
    }
    if (typeof document !== 'undefined') {
      const target = document.querySelector(
        '.widget-calendar-feed, .widget-calendar-grid, .calendar-event-item, .cf-day'
      );
      if (target && typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  };

  const handleKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handlePillClick();
    }
  };

  const pillClass = resolved?.isNow
    ? 'pill-badge next-event-pill status-now'
    : 'pill-badge next-event-pill';
  const pillTitle = resolved
    ? `${resolved.label}: ${resolved.title} (${resolved.timeStr})`
    : 'Next upcoming event';

  return (
    <div
      className="header-next-event"
      id="header-next-event"
      style={{ display: resolved ? '' : 'none' }}
    >
      <div
        className={pillClass}
        id="next-event-pill"
        role="button"
        tabIndex={0}
        title={pillTitle}
        onClick={handlePillClick}
        onKeyDown={handleKeyDown}
      >
        <span className="next-event-label" id="next-event-label">
          {resolved?.label ?? 'NEXT'}
        </span>
        <span className="next-event-time" id="next-event-time">
          {resolved?.timeStr ?? '--:--'}
        </span>
        <span
          className="next-event-title"
          id="next-event-title"
          title={resolved?.title ?? undefined}
        >
          {resolved?.title ?? '--'}
        </span>
        <span className="next-event-countdown" id="next-event-countdown">
          {resolved?.countdown ?? ''}
        </span>
      </div>
    </div>
  );
}
