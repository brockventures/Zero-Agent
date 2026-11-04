import { formatDate, relDate } from './formatDate';

export interface CalendarFeedActiveCalendar {
  name: string;
  color?: string;
  enabled?: boolean;
  [key: string]: unknown;
}

/**
 * Filters widget configuration calendars down to those that are enabled.
 */
export function calendarFeedActiveCalendars(config: unknown): CalendarFeedActiveCalendar[] {
  if (!config || typeof config !== 'object') return [];

  let rawList: unknown[] | null = null;
  if (Array.isArray(config)) {
    rawList = config;
  } else {
    const cfg = config as Record<string, unknown>;
    if (Array.isArray(cfg.calendars)) {
      rawList = cfg.calendars;
    }
  }

  if (!rawList) return [];

  const result: CalendarFeedActiveCalendar[] = [];
  for (const item of rawList) {
    if (!item || typeof item !== 'object') continue;
    const m = item as Record<string, unknown>;
    const enabledVal = m.enabled;
    if (enabledVal === false || (typeof enabledVal === 'string' && enabledVal.toLowerCase() === 'false')) {
      continue;
    }
    result.push({
      ...m,
      name: String(m.name ?? ''),
      color: m.color ? String(m.color) : undefined,
      enabled: true,
    });
  }

  return result;
}

export interface CalendarFeedEvent {
  id?: string;
  title: string;
  start: string;
  end?: string;
  all_day?: boolean;
  allDay?: boolean;
  location?: string;
  description?: string;
  calendar_name?: string;
  calendarName?: string;
  color?: string;
  [key: string]: unknown;
}

export interface CalendarFeedDayGroup {
  date: string;
  rel: string;
  start: string;
  events: CalendarFeedEvent[];
}

function normalizeFeedEvent(
  raw: Record<string, unknown>,
  title: string,
  startStr: string,
  endStr: string,
  isAllDay: boolean,
): CalendarFeedEvent {
  return {
    ...raw,
    id: raw.id ? String(raw.id) : raw.ID ? String(raw.ID) : undefined,
    title,
    start: startStr,
    end: endStr || undefined,
    allDay: isAllDay,
    all_day: isAllDay,
    location: (raw.location ?? raw.Location) ? String(raw.location ?? raw.Location) : undefined,
    description: (raw.description ?? raw.Description) ? String(raw.description ?? raw.Description) : undefined,
    calendarName: (raw.calendar_name ?? raw.calendarName ?? raw.CalendarName)
      ? String(raw.calendar_name ?? raw.calendarName ?? raw.CalendarName)
      : undefined,
    color: (raw.color ?? raw.Color) ? String(raw.color ?? raw.Color) : undefined,
  };
}

function isTimedEventActive(startStr: string, endStr: string, refTimeMs: number): boolean {
  if (endStr !== '') {
    const endDate = new Date(endStr);
    return isNaN(endDate.getTime()) || endDate.getTime() >= refTimeMs;
  }
  if (startStr !== '') {
    const startDate = new Date(startStr);
    return isNaN(startDate.getTime()) || startDate.getTime() + 60 * 60 * 1000 >= refTimeMs;
  }
  return true;
}

function parseFeedEvents(
  rawEvents: unknown[],
  todayStr: string,
  refTimeMs: number,
): CalendarFeedEvent[] {
  const filtered: CalendarFeedEvent[] = [];

  for (const raw of rawEvents) {
    if (!raw || typeof raw !== 'object') continue;
    const ev = raw as Record<string, unknown>;
    const title = typeof ev.title === 'string' ? ev.title : typeof ev.Title === 'string' ? ev.Title : '';
    const rawStart = ev.start ?? ev.Start;
    const startStr = typeof rawStart === 'string' ? rawStart : '';
    if (!title && !startStr) continue;

    const rawEnd = ev.end ?? ev.End;
    const endStr = typeof rawEnd === 'string' ? rawEnd : '';
    const isAllDay = Boolean(ev.all_day || ev.allDay || ev.AllDay || /^\d{4}-\d{2}-\d{2}$/.test(startStr));

    if (isAllDay) {
      const endDateStr = endStr !== '' ? endStr : startStr;
      if (endDateStr >= todayStr) {
        filtered.push(normalizeFeedEvent(ev, title, startStr, endStr, true));
      }
      continue;
    }

    if (isTimedEventActive(startStr, endStr, refTimeMs)) {
      filtered.push(normalizeFeedEvent(ev, title, startStr, endStr, false));
    }
  }

  return filtered;
}

function groupFeedEvents(
  events: CalendarFeedEvent[],
  effectiveRefTime: Date,
  tz?: string,
): CalendarFeedDayGroup[] {
  const groups: CalendarFeedDayGroup[] = [];
  for (const ev of events) {
    const dateStr = formatDate('2006-01-02', ev.start, tz);
    if (groups.length === 0 || groups[groups.length - 1].date !== dateStr) {
      const rel = relDate(effectiveRefTime, ev.start, tz);
      groups.push({
        date: dateStr,
        rel,
        start: ev.start,
        events: [ev],
      });
    } else {
      groups[groups.length - 1].events.push(ev);
    }
  }
  return groups;
}

/**
 * Groups filtered calendar feed events by local calendar date.
 */
export function calendarFeedDayGroups(
  data: unknown,
  refTime: Date = new Date(),
  tz?: string
): CalendarFeedDayGroup[] {
  if (!data || typeof data !== 'object') return [];

  const effectiveRefTime = refTime && !isNaN(refTime.getTime()) ? refTime : new Date();

  let rawEvents: unknown[] | null = null;
  if (Array.isArray(data)) {
    rawEvents = data;
  } else {
    const d = data as Record<string, unknown>;
    if (Array.isArray(d.events)) {
      rawEvents = d.events;
    } else if (Array.isArray(d.Events)) {
      rawEvents = d.Events;
    }
  }

  if (!rawEvents || rawEvents.length === 0) return [];

  const todayStr = formatDate('2006-01-02', effectiveRefTime, tz);
  const refTimeMs = effectiveRefTime.getTime();
  const filtered = parseFeedEvents(rawEvents, todayStr, refTimeMs);

  if (filtered.length === 0) return [];
  return groupFeedEvents(filtered, effectiveRefTime, tz);
}
