import type {
  FamilyDay,
  FamilyEvent,
  FamilyMember,
  FamilyMonthDay,
  FamilyMonthWeek,
  FamilySpanEvent,
  FamilyViewModel,
  ViewMode,
} from './types';

const SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const SHORT_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const FULL_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const FULL_MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

export function parseISODate(s: string): Date {
  const parts = s.split('T')[0].split('-');
  const y = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  const d = parseInt(parts[2], 10);
  return new Date(y, m - 1, d, 12, 0, 0);
}

export function formatISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

export function addDays(d: Date, n: number): Date {
  const res = new Date(d.getTime());
  res.setDate(res.getDate() + n);
  return res;
}

export function addMonths(d: Date, n: number): Date {
  const res = new Date(d.getTime());
  res.setDate(1);
  res.setMonth(res.getMonth() + n);
  return res;
}

export function startOfWeek(d: Date, weekStartsOn = 0): Date {
  const res = new Date(d.getTime());
  const day = res.getDay();
  const diff = (day < weekStartsOn ? 7 : 0) + day - weekStartsOn;
  res.setDate(res.getDate() - diff);
  res.setHours(12, 0, 0, 0);
  return res;
}

export function formatShortDate(isoDate: string): string {
  if (!isoDate || !/^\d{4}-\d{2}-\d{2}/.test(isoDate)) return '';
  const d = parseISODate(isoDate);
  if (Number.isNaN(d.getTime())) return '';
  return `${SHORT_WEEKDAYS[d.getDay()]}, ${SHORT_MONTHS[d.getMonth()]} ${d.getDate()}`;
}

export function formatDayLabel(d: Date): string {
  return `${FULL_WEEKDAYS[d.getDay()]}, ${SHORT_MONTHS[d.getMonth()]} ${d.getDate()}`;
}

export function formatRangeLabel(start: Date, end: Date): string {
  if (start.getFullYear() === end.getFullYear()) {
    if (start.getMonth() === end.getMonth()) {
      return `${SHORT_MONTHS[start.getMonth()]} ${start.getDate()} – ${end.getDate()}, ${start.getFullYear()}`;
    }
    return `${SHORT_MONTHS[start.getMonth()]} ${start.getDate()} – ${SHORT_MONTHS[end.getMonth()]} ${end.getDate()}, ${start.getFullYear()}`;
  }
  return `${SHORT_MONTHS[start.getMonth()]} ${start.getDate()}, ${start.getFullYear()} – ${SHORT_MONTHS[end.getMonth()]} ${end.getDate()}, ${end.getFullYear()}`;
}

export function formatMonthLabel(d: Date): string {
  return `${FULL_MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

export function formatModalDateRange(startDate?: string, endDate?: string): string {
  const startFmt = startDate ? formatShortDate(startDate) : '';
  const endFmt = endDate && endDate !== startDate ? formatShortDate(endDate) : '';
  if (startFmt && endFmt) return `${startFmt} – ${endFmt}`;
  return startFmt || endFmt;
}

export function stripMealiePrefix(title?: string): string {
  if (!title) return '';
  return title.replace(/^(?:\u{1F37D}\uFE0F?\s*)+/u, '');
}

export function matchesMemberFilter(
  ev: FamilyEvent,
  selectedMember: string | null,
  members: FamilyMember[]
): boolean {
  if (!selectedMember) return true;
  if (ev.owners && ev.owners.length > 0) {
    return ev.owners.includes(selectedMember);
  }
  const member = members.find((m) => m.name === selectedMember);
  const initial = member?.initial || selectedMember.slice(0, 1);
  return Boolean(ev.initials?.includes(initial));
}

export function sliceWeekWindow(
  allDays: FamilyDay[] | undefined,
  activeView: ViewMode,
  currentDateISO: string
): FamilyDay[] {
  const days = allDays || [];
  if (days.length === 0) return [];
  if (activeView === 'day') {
    const match = days.find((d) => d.date === currentDateISO);
    return match ? [match] : [days[0]];
  }
  return days;
}

export function computeDayLayout(
  allDayEvents: FamilySpanEvent[] | undefined,
  days: FamilyDay[] | undefined,
  activeView: ViewMode,
  currentDateISO: string
): FamilySpanEvent[] {
  if (!allDayEvents || allDayEvents.length === 0) return [];
  if (activeView !== 'day') return allDayEvents;
  const dayIdx = (days || []).findIndex((d) => d.date === currentDateISO);
  if (dayIdx < 0) return [];
  return allDayEvents.filter(
    (ev) => ev.start_col <= dayIdx + 1 && ev.start_col + ev.col_span > dayIdx + 1
  );
}

export function buildMonthViewFromDays(days: FamilyMonthDay[]): FamilyMonthWeek[] {
  const weeks: FamilyMonthWeek[] = [];
  for (let i = 0; i < days.length; i += 7) {
    weeks.push({
      week_number: Math.floor(i / 7) + 1,
      days: days.slice(i, i + 7),
    });
  }
  return weeks;
}

/**
 * Parses a 24h ('HH:MM' / 'H:MM') or 12h ('H[:MM] AM/PM') time string into minutes
 * from midnight in the range [0, 1440]. Returns null on invalid or empty strings.
 */
export function parseTimeMinutes(label?: string): number | null {
  if (!label || typeof label !== 'string') return null;
  const s = label.trim();
  if (!s) return null;

  const m12 = s.match(/^(\d{1,2})(?::(\d{2}))?\s*([ap]m)$/i);
  if (m12) {
    let hours = parseInt(m12[1], 10);
    const minutes = m12[2] !== undefined ? parseInt(m12[2], 10) : 0;
    const meridiem = m12[3].toLowerCase();

    if (hours < 1 || hours > 12 || minutes < 0 || minutes > 59) return null;
    if (meridiem === 'am') {
      if (hours === 12) hours = 0;
    } else {
      if (hours !== 12) hours += 12;
    }
    const total = hours * 60 + minutes;
    return total >= 0 && total <= 1440 ? total : null;
  }

  const m24 = s.match(/^(\d{1,2}):(\d{2})$/);
  if (m24) {
    const hours = parseInt(m24[1], 10);
    const minutes = parseInt(m24[2], 10);

    if (hours === 24 && minutes === 0) return 1440;
    if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59) return null;
    const total = hours * 60 + minutes;
    return total >= 0 && total <= 1440 ? total : null;
  }

  return null;
}

/**
 * Computes the percentage from the top of the day canvas for the current time.
 * Returns null if the current time is out of the weekVM's time bounds or arguments are invalid.
 */
export function computeNowIndicatorPct(
  weekVM: FamilyViewModel | undefined,
  now: Date,
  tz?: string
): number | null {
  const startMin =
    parseTimeMinutes(weekVM?.hours_start) ??
    parseTimeMinutes(weekVM?.timeline_markers?.[0]?.label) ??
    parseTimeMinutes(weekVM?.hour_markers?.[0]) ??
    480;

  const endMin =
    parseTimeMinutes(weekVM?.hours_end) ??
    parseTimeMinutes(weekVM?.timeline_markers?.[weekVM.timeline_markers.length - 1]?.label) ??
    parseTimeMinutes(weekVM?.hour_markers?.[weekVM.hour_markers.length - 1]) ??
    1200;

  if (endMin <= startMin || Number.isNaN(now.getTime())) {
    return null;
  }

  let nowMin: number;
  if (tz) {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: tz,
        hour: 'numeric',
        minute: 'numeric',
        hourCycle: 'h23',
      }).formatToParts(now);
      let hour = 0;
      let minute = 0;
      for (const part of parts) {
        if (part.type === 'hour') hour = parseInt(part.value, 10);
        else if (part.type === 'minute') minute = parseInt(part.value, 10);
      }
      nowMin = hour * 60 + minute;
    } catch {
      nowMin = now.getHours() * 60 + now.getMinutes();
    }
  } else {
    nowMin = now.getHours() * 60 + now.getMinutes();
  }

  if (nowMin < startMin || nowMin > endMin) {
    return null;
  }

  return ((nowMin - startMin) / (endMin - startMin)) * 100;
}
