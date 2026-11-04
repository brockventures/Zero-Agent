import { useEffect, useState } from 'react';
import { useDocumentVisible } from '../lifecycle/visibility';
import { useHeader } from '../store/hooks';

const validTimezoneCache = new Map<string, boolean>();

export function isValidTimezone(tz: string | null | undefined): boolean {
  if (!tz || typeof tz !== 'string') return false;
  const cached = validTimezoneCache.get(tz);
  if (cached !== undefined) return cached;
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: tz });
    validTimezoneCache.set(tz, true);
    return true;
  } catch {
    validTimezoneCache.set(tz, false);
    return false;
  }
}

interface ClockParts {
  time: string;
  ampm: string;
  weekday: string;
  fullDate: string;
}

export interface ClockFormatters {
  formatTime: (d: Date) => string;
  formatDate: (d: Date) => string;
  formatParts: (d: Date) => ClockParts;
}

const formatterCache = new Map<string, ClockFormatters>();

export function createClockFormatters(tz?: string, locale?: string): ClockFormatters {
  const normalizedTz = tz && isValidTimezone(tz) ? tz : '';
  const cacheKey = `${normalizedTz}|${locale ?? ''}`;
  const cached = formatterCache.get(cacheKey);
  if (cached) {
    return cached;
  }

  const timeOpts: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' };
  const dateOpts: Intl.DateTimeFormatOptions = { weekday: 'long', month: 'short', day: 'numeric' };
  const weekdayOpts: Intl.DateTimeFormatOptions = { weekday: 'long' };
  const fullDateOpts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };

  if (normalizedTz) {
    timeOpts.timeZone = normalizedTz;
    dateOpts.timeZone = normalizedTz;
    weekdayOpts.timeZone = normalizedTz;
    fullDateOpts.timeZone = normalizedTz;
  }

  const timeFormatter = new Intl.DateTimeFormat(locale, timeOpts);
  const dateFormatter = new Intl.DateTimeFormat(locale, dateOpts);
  const weekdayFormatter = new Intl.DateTimeFormat(locale, weekdayOpts);
  const fullDateFormatter = new Intl.DateTimeFormat(locale, fullDateOpts);

  const formatters: ClockFormatters = {
    formatTime: (d: Date) => timeFormatter.format(d),
    formatDate: (d: Date) => dateFormatter.format(d),
    formatParts: (d: Date) => {
      const parts = timeFormatter.formatToParts(d);
      const ampmPart = parts.find((p) => p.type === 'dayPeriod');
      const ampm = ampmPart ? ampmPart.value : '';
      const time = parts
        .filter((p) => p.type !== 'dayPeriod')
        .map((p) => p.value)
        .join('')
        .trim();
      return {
        time,
        ampm,
        weekday: weekdayFormatter.format(d),
        fullDate: fullDateFormatter.format(d),
      };
    },
  };

  formatterCache.set(cacheKey, formatters);
  return formatters;
}

export function computeClockParts(date: Date, tz?: string, locale?: string): ClockParts {
  const formatters = createClockFormatters(tz, locale);
  return formatters.formatParts(date);
}

export function resolveActiveTimezone(
  propTimezone?: string,
  header?: { timestamp?: string | null; timezone?: string } | null
): string | undefined {
  const bodyTz = typeof document !== 'undefined' ? document.body?.dataset?.timezone : undefined;
  // If an SSE header.update has arrived (indicated by non-null timestamp), prefer header.timezone.
  // Prior to initial SSE event, prefer document.body data-timezone rendered by the server template
  // over the store's default fallback timezone ('UTC').
  const sseTimezone = header?.timestamp ? header.timezone : undefined;
  const candidates = [propTimezone, sseTimezone, bodyTz, header?.timezone];
  return candidates.find(isValidTimezone);
}

export interface ClockProps {
  timezone?: string;
  locale?: string;
}

export function Clock({ timezone: propTimezone, locale: propLocale }: ClockProps = {}) {
  const header = useHeader();
  const visible = useDocumentVisible();

  const activeTz = resolveActiveTimezone(propTimezone, header);
  const [parts, setParts] = useState<ClockParts>(() =>
    computeClockParts(new Date(), activeTz, propLocale),
  );

  // Sync data-timezone attribute across DOM anchors
  useEffect(() => {
    if (typeof document !== 'undefined' && activeTz) {
      if (document.body?.dataset) {
        document.body.dataset.timezone = activeTz;
      }
      const appEl = document.getElementById('mirrormere-app');
      if (appEl?.dataset) {
        appEl.dataset.timezone = activeTz;
      }
    }
  }, [activeTz]);

  // Second-aligned clock tick, paused when hidden
  useEffect(() => {
    if (!visible) return;

    let timerId: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = () => {
      if (cancelled) return;
      const now = new Date();
      const nextParts = computeClockParts(now, activeTz, propLocale);

      setParts((prev) => {
        if (
          prev.time === nextParts.time &&
          prev.ampm === nextParts.ampm &&
          prev.weekday === nextParts.weekday &&
          prev.fullDate === nextParts.fullDate
        ) {
          return prev;
        }
        return nextParts;
      });

      const nowMs = Date.now();
      const delay = 1000 - (nowMs % 1000);
      timerId = setTimeout(tick, delay);
    };

    tick();

    return () => {
      cancelled = true;
      if (timerId !== null) {
        clearTimeout(timerId);
      }
    };
  }, [visible, activeTz, propLocale]);

  return (
    <div className="header-time-block">
      <div className="header-clock" id="header-clock" data-timezone={activeTz}>
        {parts.time}
        <span className="header-clock-ampm" style={{ display: parts.ampm ? '' : 'none' }}>
          {parts.ampm}
        </span>
      </div>
      <div className="header-date-stack" id="header-date-stack">
        <div className="header-date" id="header-date">
          <span className="header-date-weekday">{parts.weekday}</span>
          <span className="header-date-full">{parts.fullDate}</span>
        </div>
      </div>
    </div>
  );
}
