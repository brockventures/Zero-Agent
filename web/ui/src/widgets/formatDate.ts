/**
 * Date and time formatting helpers for Mirrormere widgets.
 */
const SHORT_MONTHS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];
const FULL_WEEKDAYS = [
  'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
];
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
const timeFormatters = new Map<string, Intl.DateTimeFormat>();
function getTimeFormatter(tz: string): Intl.DateTimeFormat {
  let fmt = timeFormatters.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour: 'numeric',
      minute: 'numeric',
      hourCycle: 'h23',
    });
    timeFormatters.set(tz, fmt);
  }
  return fmt;
}
function extractHourMinute(dtf: Intl.DateTimeFormat, d: Date): { hour: number; minute: number } {
  const parts = dtf.formatToParts(d);
  let hour = -1;
  let minute = 0;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p.type === 'hour') hour = Number(p.value);
    else if (p.type === 'minute') minute = Number(p.value);
  }
  return { hour, minute };
}
const formatTimeCache = new Map<string, string>();
interface ParsedTime {
  hour: number;
  minute: number;
  valid: boolean;
}
function parseTimeString(s: string, hasValidTz: boolean, tz?: string): ParsedTime {
  // Go formatTime returns "" on date-only input (length 10: YYYY-MM-DD)
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    return { hour: -1, minute: 0, valid: false };
  }
  // Match ISO with optional timezone
  const isoMatch = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/.exec(s);
  if (isoMatch) {
    const tzMatch = isoMatch[7];
    if (hasValidTz && (tzMatch === 'Z' || (tzMatch && tzMatch !== ''))) {
      const d = new Date(s);
      if (isNaN(d.getTime())) return { hour: -1, minute: 0, valid: false };
      const hm = extractHourMinute(getTimeFormatter(tz!), d);
      return { hour: hm.hour, minute: hm.minute, valid: true };
    }
    if (tzMatch === 'Z') {
      const d = new Date(s);
      if (isNaN(d.getTime())) return { hour: -1, minute: 0, valid: false };
      return { hour: d.getUTCHours(), minute: d.getUTCMinutes(), valid: true };
    }
    return { hour: Number(isoMatch[4]), minute: Number(isoMatch[5]), valid: true };
  }
  // Check for standalone HH:mm
  const timeMatch = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(s);
  if (timeMatch) {
    return { hour: Number(timeMatch[1]), minute: Number(timeMatch[2]), valid: true };
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return { hour: -1, minute: 0, valid: false };
  if (hasValidTz) {
    const hm = extractHourMinute(getTimeFormatter(tz!), d);
    return { hour: hm.hour, minute: hm.minute, valid: true };
  }
  return { hour: d.getHours(), minute: d.getMinutes(), valid: true };
}
function resolveRawTime(val: unknown, hasValidTz: boolean, tz?: string): ParsedTime {
  if (typeof val === 'string') {
    return parseTimeString(val.trim(), hasValidTz, tz);
  }
  if (val instanceof Date) {
    if (isNaN(val.getTime())) return { hour: -1, minute: 0, valid: false };
    if (hasValidTz) {
      const hm = extractHourMinute(getTimeFormatter(tz!), val);
      return { hour: hm.hour, minute: hm.minute, valid: true };
    }
    return { hour: val.getHours(), minute: val.getMinutes(), valid: true };
  }
  if (typeof val === 'number') {
    const d = new Date(val);
    if (isNaN(d.getTime())) return { hour: -1, minute: 0, valid: false };
    if (hasValidTz) {
      const hm = extractHourMinute(getTimeFormatter(tz!), d);
      return { hour: hm.hour, minute: hm.minute, valid: true };
    }
    return { hour: d.getHours(), minute: d.getMinutes(), valid: true };
  }
  return { hour: -1, minute: 0, valid: false };
}
function formatTimeWithLayout(layout: string, hour: number, minute: number): string {
  const ampm = hour >= 12 ? 'PM' : 'AM';
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  if (layout === '3 PM') return `${h12} ${ampm}`;
  if (layout === '3:04') return `${h12}:${String(minute).padStart(2, '0')}`;
  if (layout === '3:04 PM') return `${h12}:${String(minute).padStart(2, '0')} ${ampm}`;
  if (layout === 'PM') return ampm;
  if (layout === '15:04' || layout === 'HH:mm') {
    return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  }
  return '';
}
/**
 * Formats a time value according to Go layout string ('3 PM', '3:04', 'PM', '15:04', 'HH:mm').
 * Supports 1-argument formatTime(val) defaulting to '15:04', and 2/3 arguments formatTime(layout, val, tz).
 * Returns '' on invalid input or date-only inputs ('YYYY-MM-DD').
 */
export function formatTime(layoutOrValue: unknown, value?: unknown, tz?: string): string {
  let layout = '15:04';
  let val: unknown = layoutOrValue;
  if (value !== undefined) {
    if (typeof layoutOrValue === 'string' && layoutOrValue !== '' && layoutOrValue !== 'default') {
      layout = layoutOrValue;
    }
    val = value;
  }
  if (val === null || val === undefined || val === '') return '';
  const cacheKey =
    typeof val === 'string' || typeof val === 'number'
      ? `${layout}|${val}|${tz ?? ''}`
      : val instanceof Date && !isNaN(val.getTime())
        ? `${layout}|${val.getTime()}|${tz ?? ''}`
        : null;
  if (cacheKey) {
    const cached = formatTimeCache.get(cacheKey);
    if (cached !== undefined) return cached;
  }
  const hasValidTz = Boolean(tz && isValidTimezone(tz));
  const parsed = resolveRawTime(val, hasValidTz, tz);
  if (!parsed.valid || parsed.hour < 0 || parsed.hour > 23 || parsed.minute < 0 || parsed.minute > 59) {
    return '';
  }
  const res = formatTimeWithLayout(layout, parsed.hour, parsed.minute);
  if (cacheKey) {
    if (formatTimeCache.size > 2000) formatTimeCache.clear();
    formatTimeCache.set(cacheKey, res);
  }
  return res;
}
const dateFormatters = new Map<string, Intl.DateTimeFormat>();
function getDateFormatter(tz: string): Intl.DateTimeFormat {
  let fmt = dateFormatters.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      weekday: 'long',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
    });
    dateFormatters.set(tz, fmt);
  }
  return fmt;
}
function extractDateParts(
  dtf: Intl.DateTimeFormat,
  d: Date
): { year: number; month: number; day: number; weekdayIdx: number } {
  const parts = dtf.formatToParts(d);
  let year = -1;
  let month = -1;
  let day = -1;
  let weekdayIdx = -1;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (p.type === 'year') year = Number(p.value);
    else if (p.type === 'month') month = Number(p.value);
    else if (p.type === 'day') day = Number(p.value);
    else if (p.type === 'weekday') weekdayIdx = FULL_WEEKDAYS.indexOf(p.value);
  }
  return { year, month, day, weekdayIdx };
}
const formatDateCache = new Map<string, string>();
interface ParsedDateParts {
  year: number;
  month: number;
  day: number;
  weekdayIdx: number;
  valid: boolean;
  rawFallback: string;
}
function parseDateFromString(s: string, hasValidTz: boolean, tz?: string): ParsedDateParts {
  const dateOnlyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (dateOnlyMatch) {
    const year = Number(dateOnlyMatch[1]);
    const month = Number(dateOnlyMatch[2]);
    const day = Number(dateOnlyMatch[3]);
    const d = new Date(year, month - 1, day, 12, 0, 0);
    return { year, month, day, weekdayIdx: d.getDay(), valid: true, rawFallback: s };
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) {
    return { year: -1, month: -1, day: -1, weekdayIdx: -1, valid: false, rawFallback: s };
  }
  if (hasValidTz) {
    const dp = extractDateParts(getDateFormatter(tz!), d);
    return { ...dp, valid: true, rawFallback: s };
  }
  if (s.endsWith('Z')) {
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth() + 1,
      day: d.getUTCDate(),
      weekdayIdx: d.getUTCDay(),
      valid: true,
      rawFallback: s,
    };
  }
  return {
    year: d.getFullYear(),
    month: d.getMonth() + 1,
    day: d.getDate(),
    weekdayIdx: d.getDay(),
    valid: true,
    rawFallback: s,
  };
}
function resolveRawDateParts(val: unknown, hasValidTz: boolean, tz?: string): ParsedDateParts {
  if (typeof val === 'string') {
    return parseDateFromString(val.trim(), hasValidTz, tz);
  }
  if (val instanceof Date || typeof val === 'number') {
    const d = typeof val === 'number' ? new Date(val) : val;
    if (isNaN(d.getTime())) {
      return { year: -1, month: -1, day: -1, weekdayIdx: -1, valid: false, rawFallback: String(val) };
    }
    if (hasValidTz) {
      const dp = extractDateParts(getDateFormatter(tz!), d);
      return { ...dp, valid: true, rawFallback: String(val) };
    }
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth() + 1,
      day: d.getUTCDate(),
      weekdayIdx: d.getUTCDay(),
      valid: true,
      rawFallback: String(val),
    };
  }
  return { year: -1, month: -1, day: -1, weekdayIdx: -1, valid: false, rawFallback: String(val) };
}
function formatDateWithLayout(
  layout: string,
  year: number,
  month: number,
  day: number,
  weekdayIdx: number
): string {
  const shortMonth = SHORT_MONTHS[month - 1];
  const fullWeekday = weekdayIdx >= 0 ? FULL_WEEKDAYS[weekdayIdx] : '';
  if (layout === '2006-01-02' || layout === 'default') {
    return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }
  if (layout === 'Jan 2') {
    return `${shortMonth} ${day}`;
  }
  if (layout === 'Jan 02, 2006') {
    return `${shortMonth} ${String(day).padStart(2, '0')}, ${year}`;
  }
  if (layout === 'Monday • Jan 2') {
    return `${fullWeekday} • ${shortMonth} ${day}`;
  }
  if (layout === '2006') {
    return `${year}`;
  }
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}
/**
 * Formats a date value according to Go layout string ('2006-01-02', 'Jan 2', 'Monday • Jan 2', etc.).
 * Supports 1-argument formatDate(val) defaulting to '2006-01-02', and 2/3 arguments formatDate(layout, val, tz).
 * Preserves date-only strings ('YYYY-MM-DD') as calendar dates without timezone shifting.
 */
export function formatDate(layoutOrValue: unknown, value?: unknown, tz?: string): string {
  let layout = '2006-01-02';
  let val: unknown = layoutOrValue;
  if (value !== undefined) {
    if (typeof layoutOrValue === 'string' && layoutOrValue !== '' && layoutOrValue !== 'default') {
      layout = layoutOrValue;
    }
    val = value;
  }
  if (val === null || val === undefined || val === '') return '';
  const cacheKey =
    typeof val === 'string' || typeof val === 'number'
      ? `${layout}|${val}|${tz ?? ''}`
      : val instanceof Date && !isNaN(val.getTime())
        ? `${layout}|${val.getTime()}|${tz ?? ''}`
        : null;
  if (cacheKey) {
    const cached = formatDateCache.get(cacheKey);
    if (cached !== undefined) return cached;
  }
  const hasValidTz = Boolean(tz && isValidTimezone(tz));
  const dp = resolveRawDateParts(val, hasValidTz, tz);
  if (!dp.valid) {
    return dp.rawFallback;
  }
  let res = dp.rawFallback;
  if (dp.year >= 0 && dp.month >= 1 && dp.month <= 12 && dp.day >= 1 && dp.day <= 31) {
    res = formatDateWithLayout(layout, dp.year, dp.month, dp.day, dp.weekdayIdx);
  }
  if (cacheKey) {
    if (formatDateCache.size > 2000) formatDateCache.clear();
    formatDateCache.set(cacheKey, res);
  }
  return res;
}
/**
 * Returns human-readable relative date expressions ("Today", "Tomorrow", "Yesterday", "in N days", "N days ago").
 */
export function relDate(refTime: Date, value: unknown, tz?: string): string {
  if (value === null || value === undefined || value === '') return '';
  if (!refTime || isNaN(refTime.getTime())) return String(value);
  const refDateStr = formatDate('2006-01-02', refTime, tz);
  const valDateStr = formatDate('2006-01-02', value, tz);
  if (
    !refDateStr ||
    !valDateStr ||
    !/^\d{4}-\d{2}-\d{2}$/.test(refDateStr) ||
    !/^\d{4}-\d{2}-\d{2}$/.test(valDateStr)
  ) {
    return String(value);
  }
  const [ry, rm, rd] = refDateStr.split('-').map(Number);
  const [vy, vm, vd] = valDateStr.split('-').map(Number);
  const refUtc = Date.UTC(ry, rm - 1, rd);
  const valUtc = Date.UTC(vy, vm - 1, vd);
  const diffDays = Math.round((valUtc - refUtc) / (24 * 60 * 60 * 1000));
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Tomorrow';
  if (diffDays === -1) return 'Yesterday';
  if (diffDays > 1) return `in ${diffDays} days`;
  return `${-diffDays} days ago`;
}
