import type {
  CollapsedHoursGap,
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

const COLLAPSED_GAP_HOUR_EQUIVALENT = 0.65;

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
 * Formats a time string ('09:00', '18:30', '6pm', '6:00 PM') into 12-hour or 24-hour display format.
 */
export function formatEventTime(label?: string, timeFormat?: string): string {
  if (!label || typeof label !== 'string') return '';
  const trimmed = label.trim();
  if (!trimmed) return '';
  const mins = parseTimeMinutes(trimmed);
  if (mins === null) return trimmed;

  const h24 = Math.floor(mins / 60) % 24;
  const m = mins % 60;

  if (timeFormat && timeFormat.toLowerCase() === '12h') {
    const period = h24 >= 12 ? 'pm' : 'am';
    const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
    if (m === 0) {
      return `${h12}${period}`;
    }
    return `${h12}:${String(m).padStart(2, '0')}${period}`;
  }

  // If already in 12h format (e.g. '6pm' or '6:30pm'), preserve it when timeFormat is omitted
  if (!timeFormat && /[ap]m$/i.test(trimmed)) {
    return trimmed;
  }

  return `${String(h24).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/**
 * Maps a minute offset [startMin, endMin] onto canvas percentage [0, 100],
 * compressing any collapsed interior hour gaps to COLLAPSED_GAP_HOUR_EQUIVALENT hours.
 */
export function mapTimeMinutesToPct(
  targetMin: number,
  startMin: number,
  endMin: number,
  collapsedGaps?: CollapsedHoursGap[]
): number {
  if (endMin <= startMin) return 0;
  if (targetMin <= startMin) return 0;
  if (targetMin >= endMin) return 100;

  if (!collapsedGaps || collapsedGaps.length === 0) {
    return ((targetMin - startMin) / (endMin - startMin)) * 100;
  }

  // If gaps already carry precomputed top_pct and height_pct, interpolate piecewise-linearly
  // or compute using the canonical 0.65 hour-equivalent weighting.
  const compressedGapMins = COLLAPSED_GAP_HOUR_EQUIVALENT * 60;
  let totalEff = 0;
  let effAtTarget = 0;
  let cur = startMin;

  const sortedGaps = [...collapsedGaps].sort((a, b) => a.start_hour - b.start_hour);
  for (const g of sortedGaps) {
    const gStart = Math.max(g.start_hour * 60, cur);
    const gEnd = Math.min(g.end_hour * 60, endMin);
    if (gEnd <= gStart) continue;

    if (gStart > cur) {
      const seg = gStart - cur;
      if (targetMin > cur) {
        effAtTarget += Math.min(targetMin, gStart) - cur;
      }
      totalEff += seg;
    }

    const gapActual = gEnd - gStart;
    if (targetMin > gStart) {
      const inGap = Math.min(targetMin, gEnd) - gStart;
      effAtTarget += (inGap / gapActual) * compressedGapMins;
    }
    totalEff += compressedGapMins;
    cur = gEnd;
  }

  if (endMin > cur) {
    const seg = endMin - cur;
    if (targetMin > cur) {
      effAtTarget += Math.min(targetMin, endMin) - cur;
    }
    totalEff += seg;
  }

  if (totalEff <= 0) return 0;
  return (effAtTarget / totalEff) * 100;
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

  const gaps =
    weekVM?.collapsed_gaps && weekVM.collapsed_gaps.length > 0
      ? weekVM.collapsed_gaps
      : undefined;

  return mapTimeMinutesToPct(nowMin, startMin, endMin, gaps);
}

interface CustomBirthdayEntry {
  name?: string;
  key?: string;
  id?: string;
  date?: string;
  month_day?: string;
  month?: number;
  day?: number;
  label?: string;
  banner_label?: string;
}

function normalizeCustomMonthDay(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const trimmed = raw.trim();
  const fullMatch = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(trimmed);
  if (fullMatch) {
    return `${fullMatch[2].padStart(2, '0')}-${fullMatch[3].padStart(2, '0')}`;
  }
  const mdMatch = /^(\d{1,2})[-/](\d{1,2})$/.exec(trimmed);
  if (mdMatch) {
    return `${mdMatch[1].padStart(2, '0')}-${mdMatch[2].padStart(2, '0')}`;
  }
  return undefined;
}

function formatCustomSpecialDayBadge(nameRaw?: string, labelRaw?: string): string | null {
  if (typeof labelRaw === 'string' && labelRaw.trim()) {
    const cleanLabel = labelRaw.trim().replace(/^🎂\s*/, '');
    return `🎂 ${cleanLabel}`;
  }
  if (typeof nameRaw === 'string' && nameRaw.trim()) {
    const cleanName = nameRaw.trim();
    if (/bday|birthday/i.test(cleanName)) {
      return `🎂 ${cleanName.replace(/^🎂\s*/, '')}`;
    }
    return `🎂 ${cleanName}'s Bday`;
  }
  return null;
}

function matchCustomEntryObject(
  entry: CustomBirthdayEntry & Record<string, unknown>,
  targetMonthDay: string,
  fallbackKey?: string,
): string | null {
  let entryMonthDay = normalizeCustomMonthDay(entry.date ?? entry.month_day);
  if (
    !entryMonthDay &&
    typeof entry.month === 'number' &&
    typeof entry.day === 'number'
  ) {
    entryMonthDay = `${String(entry.month).padStart(2, '0')}-${String(entry.day).padStart(2, '0')}`;
  }
  if (!entryMonthDay && fallbackKey) {
    entryMonthDay = normalizeCustomMonthDay(fallbackKey);
  }
  if (entryMonthDay !== targetMonthDay) return null;

  const rawName =
    typeof entry.name === 'string'
      ? entry.name
      : typeof entry.key === 'string'
        ? entry.key
        : typeof entry.id === 'string'
          ? entry.id
          : fallbackKey && !normalizeCustomMonthDay(fallbackKey)
            ? fallbackKey
            : '';
  const rawLabel =
    typeof entry.label === 'string'
      ? entry.label
      : typeof entry.banner_label === 'string'
        ? entry.banner_label
        : undefined;
  return formatCustomSpecialDayBadge(rawName, rawLabel);
}

function resolveSingleSpecialSource(dateStr: string, source: unknown): string | null {
  if (!source || typeof source === 'boolean') return null;
  const targetMonthDay = normalizeCustomMonthDay(dateStr);
  if (!targetMonthDay) return null;

  if (Array.isArray(source)) {
    for (const item of source) {
      if (item && typeof item === 'object' && !Array.isArray(item)) {
        const match = matchCustomEntryObject(item as Record<string, unknown>, targetMonthDay);
        if (match) return match;
      }
    }
    return null;
  }

  if (typeof source === 'object') {
    const map = source as Record<string, unknown>;
    for (const [k, v] of Object.entries(map)) {
      if (typeof v === 'string') {
        const kAsDate = normalizeCustomMonthDay(k);
        const vAsDate = normalizeCustomMonthDay(v);
        if (vAsDate === targetMonthDay) {
          const match = formatCustomSpecialDayBadge(k);
          if (match) return match;
        } else if (kAsDate === targetMonthDay) {
          const match = formatCustomSpecialDayBadge(v);
          if (match) return match;
        }
      } else if (v && typeof v === 'object' && !Array.isArray(v)) {
        const match = matchCustomEntryObject(v as Record<string, unknown>, targetMonthDay, k);
        if (match) return match;
      }
    }
  }
  return null;
}

/**
 * Resolves a custom birthday or special-day badge text ('🎂 Maya\'s Bday' or '🎂 Custom Label')
 * for a given date ('YYYY-MM-DD' or 'MM-DD') from widget config (`birthdays` or `special_days`).
 */
export function resolveConfiguredSpecialDayLabel(
  dateStr?: string,
  birthdays?: unknown,
  specialDays?: unknown,
): string | null {
  if (!dateStr) return null;
  return (
    resolveSingleSpecialSource(dateStr, birthdays) ||
    resolveSingleSpecialSource(dateStr, specialDays)
  );
}

