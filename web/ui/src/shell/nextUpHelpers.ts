import type { WidgetEntry } from '../store/types';
import {
  formatTime,
  formatWeekday,
  getDatePartsInTimezone,
  getTimezoneOffsetString,
  parseAllDayDates,
  parseTimedDates,
} from './nextUpDateUtils';

export interface RawCalendarEvent {
  id?: string;
  title: string;
  start: string | Date;
  end?: string | Date;
  allDay?: boolean;
  all_day?: boolean;
  date?: string;
  dayDate?: string;
  location?: string;
  [key: string]: unknown;
}

export interface ResolvedNextUpEvent {
  event: RawCalendarEvent;
  title: string;
  label: string;
  timeStr: string;
  countdown: string;
  isNow: boolean;
}

export interface ParsedEventDates {
  raw: RawCalendarEvent;
  id: string;
  title: string;
  startDate: Date;
  endDate: Date;
  isAllDay: boolean;
}

/**
 * Format relative countdown string (e.g. "in 1h 30m", "in 45m", "ends in 15m").
 * Ported from web/static/js/display.js lines 923-932.
 */
export function formatRelativeCountdown(ms: number, isEnding = false): string {
  const totalMinutes = Math.round(ms / 60000);
  const prefix = isEnding ? 'ends in ' : 'in ';
  if (totalMinutes <= 0) return `${prefix}<1m`;
  if (totalMinutes < 60) return `${prefix}${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  if (mins === 0) return `${prefix}${hours}h`;
  return `${prefix}${hours}h ${mins}m`;
}

/**
 * Parse raw calendar event dates into normalized Date objects.
 * Ported from web/static/js/display.js lines 995-1060.
 */
export function parseEventDates(
  raw: RawCalendarEvent,
  referenceNow: Date,
  tz?: string
): ParsedEventDates | null {
  if (!raw || typeof raw !== 'object') return null;
  const offsetStr = getTimezoneOffsetString(referenceNow, tz);
  const isAllDay = Boolean(
    raw.allDay ||
      raw.all_day ||
      (typeof raw.start === 'string' && raw.start.length === 10 && !raw.start.includes(':'))
  );

  const { startDate, endDate } = isAllDay
    ? parseAllDayDates(raw, offsetStr)
    : parseTimedDates(raw, referenceNow, tz, offsetStr);

  if (!startDate || isNaN(startDate.getTime()) || !endDate || isNaN(endDate.getTime())) {
    return null;
  }

  return {
    raw,
    id: raw.id || `${raw.title}-${String(raw.start)}`,
    title: raw.title || 'Event',
    startDate,
    endDate,
    isAllDay,
  };
}

/**
 * Resolve earliest next up or ongoing event across candidates.
 * Ported from web/static/js/display.js lines 1065-1186.
 */
export function resolveNextUpEvent(
  events: readonly RawCalendarEvent[],
  now: Date,
  tz?: string
): ResolvedNextUpEvent | null {
  if (!Array.isArray(events) || events.length === 0) return null;
  const activeTz = tz;

  const ongoingTimed: ParsedEventDates[] = [];
  const upcomingTimed: ParsedEventDates[] = [];
  const allDayToday: ParsedEventDates[] = [];
  const upcomingAllDay: ParsedEventDates[] = [];

  for (const raw of events) {
    const parsed = parseEventDates(raw, now, activeTz);
    if (!parsed || parsed.endDate <= now) continue;

    if (parsed.isAllDay) {
      if (parsed.startDate <= now && parsed.endDate >= now) {
        allDayToday.push(parsed);
      } else if (parsed.startDate > now) {
        upcomingAllDay.push(parsed);
      }
    } else {
      if (parsed.startDate <= now && parsed.endDate > now) {
        ongoingTimed.push(parsed);
      } else if (parsed.startDate > now) {
        upcomingTimed.push(parsed);
      }
    }
  }

  ongoingTimed.sort((a, b) => a.endDate.getTime() - b.endDate.getTime());
  upcomingTimed.sort((a, b) => a.startDate.getTime() - b.startDate.getTime());
  allDayToday.sort((a, b) => a.startDate.getTime() - b.startDate.getTime());
  upcomingAllDay.sort((a, b) => a.startDate.getTime() - b.startDate.getTime());

  // 1. Ongoing timed event takes top priority
  if (ongoingTimed.length > 0) {
    const ev = ongoingTimed[0];
    const remainingMs = ev.endDate.getTime() - now.getTime();
    return {
      event: ev.raw,
      title: ev.title,
      label: 'NOW',
      timeStr: formatTime(ev.startDate, activeTz),
      countdown: formatRelativeCountdown(remainingMs, true),
      isNow: true,
    };
  }

  // 2. Upcoming timed event today takes precedence over all-day event
  const nowParts = getDatePartsInTimezone(now, activeTz);
  const todayUpcoming = upcomingTimed.filter((e) => {
    const ep = getDatePartsInTimezone(e.startDate, activeTz);
    return ep.year === nowParts.year && ep.month === nowParts.month && ep.day === nowParts.day;
  });

  if (todayUpcoming.length > 0) {
    const ev = todayUpcoming[0];
    const untilMs = ev.startDate.getTime() - now.getTime();
    return {
      event: ev.raw,
      title: ev.title,
      label: 'NEXT',
      timeStr: formatTime(ev.startDate, activeTz),
      countdown: formatRelativeCountdown(untilMs, false),
      isNow: false,
    };
  }

  // 3. All-day event today
  if (allDayToday.length > 0) {
    const ev = allDayToday[0];
    return {
      event: ev.raw,
      title: ev.title,
      label: 'TODAY',
      timeStr: 'All Day',
      countdown: '',
      isNow: false,
    };
  }

  // 4. Upcoming timed event on a future day
  if (upcomingTimed.length > 0) {
    const ev = upcomingTimed[0];
    const untilMs = ev.startDate.getTime() - now.getTime();
    const weekday = formatWeekday(ev.startDate, activeTz);
    return {
      event: ev.raw,
      title: ev.title,
      label: 'NEXT',
      timeStr: `${weekday} ${formatTime(ev.startDate, activeTz)}`,
      countdown: formatRelativeCountdown(untilMs, false),
      isNow: false,
    };
  }

  // 5. Upcoming all-day event on a future day
  if (upcomingAllDay.length > 0) {
    const ev = upcomingAllDay[0];
    const weekday = formatWeekday(ev.startDate, activeTz);
    return {
      event: ev.raw,
      title: ev.title,
      label: weekday.toUpperCase(),
      timeStr: 'All Day',
      countdown: '',
      isNow: false,
    };
  }

  return null;
}

function pushGridDayEvent(
  ev: unknown,
  dayDate: string,
  out: RawCalendarEvent[],
  forceAllDay = false
): void {
  if (!ev || typeof ev !== 'object') return;
  const rec = ev as Record<string, unknown>;
  const title = typeof rec.title === 'string' ? rec.title : '';
  if (!title) return;
  const rawStart = typeof rec.start === 'string' ? rec.start.trim() : rec.start;
  const rawEnd = typeof rec.end === 'string' ? rec.end.trim() : rec.end;
  const isAllDay = Boolean(forceAllDay || rec.all_day || rec.allDay || !rawStart);
  const start =
    (typeof rawStart === 'string' && rawStart !== '') || rawStart instanceof Date
      ? rawStart
      : dayDate;
  const end =
    (typeof rawEnd === 'string' && rawEnd !== '') || rawEnd instanceof Date
      ? rawEnd
      : dayDate;
  const rawId = typeof rec.id === 'string' ? rec.id.trim() : '';
  const id = rawId
    ? dayDate && !rawId.endsWith(`-${dayDate}`)
      ? `${rawId}-${dayDate}`
      : rawId
    : `grid-${dayDate}-${start || 'allday'}-${title}`;

  out.push({
    ...rec,
    id,
    title,
    start,
    end,
    allDay: isAllDay,
    date: dayDate || (typeof rec.date === 'string' ? rec.date : undefined),
    dayDate: dayDate || (typeof rec.dayDate === 'string' ? rec.dayDate : undefined),
  });
}

function extractEventsFromDay(day: Record<string, unknown>, out: RawCalendarEvent[]): void {
  const dayDate =
    (typeof day.date === 'string' ? day.date : '') ||
    (typeof day.dayDate === 'string' ? day.dayDate : '');

  for (const groupKey of ['events', 'early_events', 'late_events', 'timed']) {
    const group = day[groupKey];
    if (Array.isArray(group)) {
      for (const ev of group) {
        pushGridDayEvent(ev, dayDate, out, false);
      }
    }
  }
  if (Array.isArray(day.all_day)) {
    for (const ev of day.all_day) {
      pushGridDayEvent(ev, dayDate, out, true);
    }
  }
}

function processArray(arr: unknown[], out: RawCalendarEvent[]): void {
  for (const item of arr) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    if (
      typeof rec.date === 'string' &&
      (Array.isArray(rec.events) ||
        Array.isArray(rec.all_day) ||
        Array.isArray(rec.early_events) ||
        Array.isArray(rec.late_events) ||
        Array.isArray(rec.timed))
    ) {
      extractEventsFromDay(rec, out);
    } else if (
      typeof rec.title === 'string' &&
      (rec.start !== undefined || rec.allDay || rec.all_day || rec.date || rec.dayDate)
    ) {
      out.push(rec as unknown as RawCalendarEvent);
    }
  }
}

function extractFromWeekViewModel(
  week: Record<string, unknown>,
  rawList: RawCalendarEvent[]
): void {
  if (Array.isArray(week.days)) {
    processArray(week.days, rawList);
  }
  if (!Array.isArray(week.all_day_events)) return;

  const days = Array.isArray(week.days) ? (week.days as Record<string, unknown>[]) : [];
  for (const evItem of week.all_day_events) {
    if (!evItem || typeof evItem !== 'object') continue;
    const ev = { ...(evItem as Record<string, unknown>) };
    if (!ev.start && !ev.date) {
      const startCol =
        typeof ev.start_col === 'number'
          ? ev.start_col
          : typeof ev.startCol === 'number'
            ? ev.startCol
            : 1;
      const targetDay = days[startCol - 1];
      if (targetDay && typeof targetDay.date === 'string') {
        ev.date = targetDay.date;
      }
    }
    if (ev.allDay === undefined && ev.all_day === undefined) {
      ev.allDay = true;
    }
    if (typeof ev.title === 'string') {
      processArray([ev], rawList);
    }
  }
}

function extractFromViewModel(
  viewModel: Record<string, unknown>,
  rawList: RawCalendarEvent[]
): void {
  if (viewModel.week && typeof viewModel.week === 'object') {
    extractFromWeekViewModel(viewModel.week as Record<string, unknown>, rawList);
  }
  if (viewModel.month && typeof viewModel.month === 'object') {
    const month = viewModel.month as Record<string, unknown>;
    if (Array.isArray(month.days)) {
      processArray(month.days, rawList);
    }
  }
  if (Array.isArray(viewModel.days)) {
    processArray(viewModel.days, rawList);
  }
  if (Array.isArray(viewModel.events)) {
    processArray(viewModel.events, rawList);
  }
}

function extractWidgetCalendarEvents(
  entry: WidgetEntry,
  rawList: RawCalendarEvent[]
): void {
  if (Array.isArray(entry.data)) {
    processArray(entry.data, rawList);
  } else if (entry.data && typeof entry.data === 'object') {
    const dataObj = entry.data as Record<string, unknown>;
    if (Array.isArray(dataObj.events)) {
      processArray(dataObj.events, rawList);
    }
    if (Array.isArray(dataObj.days)) {
      processArray(dataObj.days, rawList);
    }
  }

  if (entry.viewModel && typeof entry.viewModel === 'object') {
    extractFromViewModel(entry.viewModel as Record<string, unknown>, rawList);
  }
}

/**
 * Extracts and deduplicates calendar events from display widgets store.
 * Supports { events: [...] }, array entry.data, { days: [...] }, and entry.viewModel?.week?.days.
 */
export function selectCalendarEvents(
  widgets: Record<string, WidgetEntry | undefined>
): RawCalendarEvent[] {
  const rawList: RawCalendarEvent[] = [];

  for (const entry of Object.values(widgets)) {
    if (!entry) continue;
    extractWidgetCalendarEvents(entry, rawList);
  }

  const deduped = new Map<string, RawCalendarEvent>();
  for (const ev of rawList) {
    if (!ev || !ev.title) continue;
    const key = ev.id || `${ev.title}-${String(ev.start)}`;
    if (!deduped.has(key)) {
      deduped.set(key, ev);
    }
  }

  return Array.from(deduped.values());
}
