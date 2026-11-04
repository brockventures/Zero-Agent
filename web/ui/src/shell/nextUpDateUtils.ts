import { isValidTimezone } from './Clock';
import type { RawCalendarEvent } from './NextUp';

const tzOffsetFormatters = new Map<string, Intl.DateTimeFormat>();

export function getTimezoneOffsetString(date: Date, tz?: string): string {
  if (!tz || !isValidTimezone(tz)) return '';
  try {
    let formatter = tzOffsetFormatters.get(tz);
    if (!formatter) {
      formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: tz,
        timeZoneName: 'longOffset',
      });
      tzOffsetFormatters.set(tz, formatter);
    }
    const parts = formatter.formatToParts(date);
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (p.type === 'timeZoneName' && p.value) {
        const m = p.value.match(/GMT([+-]\d{2}:\d{2})/);
        if (m) return m[1];
        if (p.value === 'GMT' || p.value === 'UTC') return 'Z';
      }
    }
  } catch {
    // Ignore timezone formatting failures
  }
  return '';
}

const datePartsFormatters = new Map<string, Intl.DateTimeFormat>();

export function getDatePartsInTimezone(
  date: Date,
  tz?: string
): { year: number; month: number; day: number } {
  if (!tz || !isValidTimezone(tz)) {
    return {
      year: date.getFullYear(),
      month: date.getMonth() + 1,
      day: date.getDate(),
    };
  }
  try {
    let formatter = datePartsFormatters.get(tz);
    if (!formatter) {
      formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: tz,
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
      });
      datePartsFormatters.set(tz, formatter);
    }
    const parts = formatter.formatToParts(date);
    let year = date.getFullYear();
    let month = date.getMonth() + 1;
    let day = date.getDate();
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (p.type === 'year') year = parseInt(p.value, 10);
      else if (p.type === 'month') month = parseInt(p.value, 10);
      else if (p.type === 'day') day = parseInt(p.value, 10);
    }
    return { year, month, day };
  } catch {
    return {
      year: date.getFullYear(),
      month: date.getMonth() + 1,
      day: date.getDate(),
    };
  }
}

const timeFormatters = new Map<string, Intl.DateTimeFormat>();

export function formatTime(date: Date, tz?: string): string {
  const normTz = tz && isValidTimezone(tz) ? tz : '';
  let fmt = timeFormatters.get(normTz);
  if (!fmt) {
    const opts: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' };
    if (normTz) opts.timeZone = normTz;
    fmt = new Intl.DateTimeFormat(undefined, opts);
    timeFormatters.set(normTz, fmt);
  }
  return fmt.format(date);
}

const weekdayFormatters = new Map<string, Intl.DateTimeFormat>();

export function formatWeekday(date: Date, tz?: string): string {
  const normTz = tz && isValidTimezone(tz) ? tz : '';
  let fmt = weekdayFormatters.get(normTz);
  if (!fmt) {
    const opts: Intl.DateTimeFormatOptions = { weekday: 'short' };
    if (normTz) opts.timeZone = normTz;
    fmt = new Intl.DateTimeFormat(undefined, opts);
    weekdayFormatters.set(normTz, fmt);
  }
  return fmt.format(date);
}

export function parseAllDayDates(
  raw: RawCalendarEvent,
  offsetStr: string
): { startDate: Date | null; endDate: Date | null } {
  let dateStr = typeof raw.start === 'string' ? raw.start.substring(0, 10) : '';
  if (!dateStr && typeof raw.date === 'string') dateStr = raw.date;
  if (!dateStr && typeof raw.dayDate === 'string') dateStr = raw.dayDate;

  if (dateStr && /^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
    const startDate = new Date(`${dateStr}T00:00:00${offsetStr}`);
    let endDateStr = typeof raw.end === 'string' ? raw.end.substring(0, 10) : dateStr;
    if (!endDateStr || !/^\d{4}-\d{2}-\d{2}$/.test(endDateStr)) endDateStr = dateStr;
    const endDate = new Date(`${endDateStr}T23:59:59.999${offsetStr}`);
    return { startDate, endDate };
  }

  if (raw.start instanceof Date) {
    const startDate = new Date(raw.start);
    const endDate =
      raw.end instanceof Date
        ? new Date(raw.end)
        : new Date(startDate.getTime() + 24 * 3600 * 1000 - 1);
    return { startDate, endDate };
  }

  return { startDate: null, endDate: null };
}

function parseTimeOfDay(
  raw: RawCalendarEvent,
  referenceNow: Date,
  tz: string | undefined,
  offsetStr: string
): { startDate: Date | null; endDate: Date | null } {
  let dateStr =
    (typeof raw.date === 'string' ? raw.date : undefined) ||
    (typeof raw.dayDate === 'string' ? raw.dayDate : undefined);
  if (!dateStr || !/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
    const np = getDatePartsInTimezone(referenceNow, tz);
    dateStr = `${np.year}-${String(np.month).padStart(2, '0')}-${String(np.day).padStart(2, '0')}`;
  }
  const [sh, sm] = (raw.start as string).split(':').map((s: string) => s.padStart(2, '0'));
  const startDate = new Date(`${dateStr}T${sh}:${sm}:00${offsetStr}`);

  if (typeof raw.end === 'string' && /^\d{1,2}:\d{2}$/.test(raw.end)) {
    const [eh, em] = raw.end.split(':').map((s: string) => s.padStart(2, '0'));
    return { startDate, endDate: new Date(`${dateStr}T${eh}:${em}:00${offsetStr}`) };
  }
  return { startDate, endDate: new Date(startDate.getTime() + 60 * 60 * 1000) };
}

export function parseTimedDates(
  raw: RawCalendarEvent,
  referenceNow: Date,
  tz: string | undefined,
  offsetStr: string
): { startDate: Date | null; endDate: Date | null } {
  if (typeof raw.start === 'string' && /^\d{1,2}:\d{2}$/.test(raw.start)) {
    return parseTimeOfDay(raw, referenceNow, tz, offsetStr);
  }
  if (typeof raw.start === 'string') {
    const startDate = new Date(raw.start);
    const endDate = typeof raw.end === 'string' ? new Date(raw.end) : new Date(startDate.getTime() + 60 * 60 * 1000);
    return { startDate, endDate };
  }
  if (raw.start instanceof Date) {
    const startDate = new Date(raw.start);
    const endDate =
      raw.end instanceof Date
        ? new Date(raw.end)
        : new Date(startDate.getTime() + 60 * 60 * 1000);
    return { startDate, endDate };
  }
  return { startDate: null, endDate: null };
}
