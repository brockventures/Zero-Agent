import { describe, expect, it } from 'vitest';
import {
  addDays,
  addMonths,
  buildMonthViewFromDays,
  computeDayLayout,
  computeNowIndicatorPct,
  formatDayLabel,
  formatISODate,
  formatModalDateRange,
  formatMonthLabel,
  formatRangeLabel,
  formatShortDate,
  matchesMemberFilter,
  parseISODate,
  parseTimeMinutes,
  sliceWeekWindow,
  startOfWeek,
  stripMealiePrefix,
} from './dateUtils';
import type { FamilyDay, FamilyMonthDay, FamilySpanEvent, FamilyViewModel } from './types';

describe('dateUtils', () => {
  it('parses and formats ISO dates', () => {
    const d = parseISODate('2026-10-27');
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(9);
    expect(d.getDate()).toBe(27);

    expect(formatISODate(d)).toBe('2026-10-27');
  });

  it('adds days and months correctly', () => {
    const d = parseISODate('2026-10-27');
    const plus3 = addDays(d, 3);
    expect(formatISODate(plus3)).toBe('2026-10-30');

    const nextMonth = addMonths(d, 1);
    expect(formatISODate(nextMonth)).toBe('2026-11-01');
  });

  it('computes startOfWeek', () => {
    // 2026-10-27 is a Tuesday
    const d = parseISODate('2026-10-27');
    const sun = startOfWeek(d, 0); // Sunday
    expect(formatISODate(sun)).toBe('2026-10-25');

    const mon = startOfWeek(d, 1); // Monday
    expect(formatISODate(mon)).toBe('2026-10-26');
  });

  it('formats short dates and labels', () => {
    expect(formatShortDate('2026-10-27')).toBe('Tue, Oct 27');
    expect(formatShortDate('invalid')).toBe('');
    expect(formatShortDate('')).toBe('');

    const d = parseISODate('2026-10-27');
    expect(formatDayLabel(d)).toBe('Tuesday, Oct 27');
    expect(formatMonthLabel(d)).toBe('October 2026');

    const start = parseISODate('2026-10-25');
    const end = parseISODate('2026-10-31');
    expect(formatRangeLabel(start, end)).toBe('Oct 25 – 31, 2026');

    const endDiffMonth = parseISODate('2026-11-05');
    expect(formatRangeLabel(start, endDiffMonth)).toBe('Oct 25 – Nov 5, 2026');

    const endDiffYear = parseISODate('2027-01-02');
    expect(formatRangeLabel(start, endDiffYear)).toBe('Oct 25, 2026 – Jan 2, 2027');
  });

  it('formats modal date range', () => {
    expect(formatModalDateRange('2026-10-25', '2026-10-31')).toBe('Sun, Oct 25 – Sat, Oct 31');
    expect(formatModalDateRange('2026-10-25', '2026-10-25')).toBe('Sun, Oct 25');
    expect(formatModalDateRange('2026-10-25', undefined)).toBe('Sun, Oct 25');
    expect(formatModalDateRange(undefined, '2026-10-25')).toBe('Sun, Oct 25');
    expect(formatModalDateRange(undefined, undefined)).toBe('');
  });

  it('strips leading Mealie emojis', () => {
    expect(stripMealiePrefix('🍽️ Carnitas Tacos')).toBe('Carnitas Tacos');
    expect(stripMealiePrefix('🍽️🍽️ Double')).toBe('Double');
    expect(stripMealiePrefix('Regular Title')).toBe('Regular Title');
    expect(stripMealiePrefix('')).toBe('');
    expect(stripMealiePrefix(undefined)).toBe('');
  });

  it('slices week window for day and week views', () => {
    const days: FamilyDay[] = [
      { date: '2026-10-25', day_of_month: 25, is_today: false, is_weekend: true },
      { date: '2026-10-26', day_of_month: 26, is_today: false, is_weekend: false },
      { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false },
    ];

    expect(sliceWeekWindow(days, 'week', '2026-10-27')).toEqual(days);
    expect(sliceWeekWindow(days, 'day', '2026-10-26')).toEqual([days[1]]);
    expect(sliceWeekWindow(days, 'day', '2026-10-99')).toEqual([days[0]]);
    expect(sliceWeekWindow([], 'day', '2026-10-27')).toEqual([]);
    expect(sliceWeekWindow(undefined, 'week', '2026-10-27')).toEqual([]);
  });

  it('computes day layout for span events', () => {
    const days: FamilyDay[] = [
      { date: '2026-10-25', day_of_month: 25, is_today: false, is_weekend: true },
      { date: '2026-10-26', day_of_month: 26, is_today: false, is_weekend: false },
      { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false },
    ];
    const spanEvents: FamilySpanEvent[] = [
      { id: '1', title: 'Ev 1', start_col: 2, col_span: 2 },
    ];

    // Week view returns all span events
    expect(computeDayLayout(spanEvents, days, 'week', '2026-10-25')).toEqual(spanEvents);

    // Day 2 (Oct 26 -> col 2): Ev 1 is on cols 2 and 3
    expect(computeDayLayout(spanEvents, days, 'day', '2026-10-26')).toEqual(spanEvents);

    // Day 1 (Oct 25 -> col 1): Ev 1 is not visible
    expect(computeDayLayout(spanEvents, days, 'day', '2026-10-25')).toEqual([]);

    expect(computeDayLayout([], days, 'day', '2026-10-26')).toEqual([]);
    expect(computeDayLayout(undefined, days, 'day', '2026-10-26')).toEqual([]);
  });

  it('builds month view from days in 7-day chunks', () => {
    const monthDays: FamilyMonthDay[] = Array.from({ length: 14 }).map((_, i) => ({
      date: `2026-10-${String(i + 1).padStart(2, '0')}`,
      day_of_month: i + 1,
      is_current_month: true,
      is_today: false,
      is_weekend: false,
    }));

    const weeks = buildMonthViewFromDays(monthDays);
    expect(weeks.length).toBe(2);
    expect(weeks[0].week_number).toBe(1);
    expect(weeks[0].days.length).toBe(7);
    expect(weeks[1].week_number).toBe(2);
    expect(weeks[1].days.length).toBe(7);
  });

  describe('parseTimeMinutes', () => {
    it('parses 24h format times', () => {
      expect(parseTimeMinutes('00:00')).toBe(0);
      expect(parseTimeMinutes('08:00')).toBe(480);
      expect(parseTimeMinutes('8:00')).toBe(480);
      expect(parseTimeMinutes('14:30')).toBe(870);
      expect(parseTimeMinutes('23:59')).toBe(1439);
      expect(parseTimeMinutes('24:00')).toBe(1440);
    });

    it('parses 12h format times with AM/PM', () => {
      expect(parseTimeMinutes('12:00 AM')).toBe(0);
      expect(parseTimeMinutes('12 AM')).toBe(0);
      expect(parseTimeMinutes('8 AM')).toBe(480);
      expect(parseTimeMinutes('8:30 AM')).toBe(510);
      expect(parseTimeMinutes('12:00 PM')).toBe(720);
      expect(parseTimeMinutes('12 PM')).toBe(720);
      expect(parseTimeMinutes('1:15 PM')).toBe(795);
      expect(parseTimeMinutes('11:59 pm')).toBe(1439);
    });

    it('returns null on invalid or empty labels', () => {
      expect(parseTimeMinutes('')).toBeNull();
      expect(parseTimeMinutes('   ')).toBeNull();
      expect(parseTimeMinutes(undefined)).toBeNull();
      expect(parseTimeMinutes('invalid')).toBeNull();
      expect(parseTimeMinutes('25:00')).toBeNull();
      expect(parseTimeMinutes('12:60')).toBeNull();
      expect(parseTimeMinutes('13:00 AM')).toBeNull();
      expect(parseTimeMinutes('0 AM')).toBeNull();
      expect(parseTimeMinutes('0:00 PM')).toBeNull();
    });
  });

  describe('computeNowIndicatorPct', () => {
    const baseVM: FamilyViewModel = {
      range_start: '2026-10-25',
      range_end: '2026-10-31',
      range_label: 'Oct 25 – 31, 2026',
      today: '2026-10-27',
      hours_start: '08:00',
      hours_end: '20:00',
    };

    it('computes percentage within bounds using default hours', () => {
      // 14:00 is halfway between 08:00 (480) and 20:00 (1200)
      const now = new Date('2026-10-27T14:00:00Z');
      const pct = computeNowIndicatorPct(baseVM, now, 'UTC');
      expect(pct).toBeCloseTo(50, 1);
    });

    it('resolves start and end from timeline_markers when hours_start/end missing', () => {
      const vm: FamilyViewModel = {
        ...baseVM,
        hours_start: undefined,
        hours_end: undefined,
        timeline_markers: [
          { label: '09:00', top_pct: 0, is_start: true },
          { label: '17:00', top_pct: 100, is_end: true },
        ],
      };
      // 09:00 (540) to 17:00 (1020) -> duration 480 min.
      // 13:00 (780) is (780 - 540) / 480 = 50%
      const now = new Date('2026-10-27T13:00:00Z');
      const pct = computeNowIndicatorPct(vm, now, 'UTC');
      expect(pct).toBeCloseTo(50, 1);
    });

    it('resolves start and end from hour_markers when timeline_markers missing', () => {
      const vm: FamilyViewModel = {
        ...baseVM,
        hours_start: undefined,
        hours_end: undefined,
        hour_markers: ['10:00', '14:00', '18:00'],
      };
      // 10:00 (600) to 18:00 (1080) -> duration 480 min.
      // 12:00 (720) is (720 - 600) / 480 = 25%
      const now = new Date('2026-10-27T12:00:00Z');
      const pct = computeNowIndicatorPct(vm, now, 'UTC');
      expect(pct).toBeCloseTo(25, 1);
    });

    it('falls back to default 480 and 1200 when weekVM is undefined', () => {
      // Default 08:00 (480) to 20:00 (1200)
      const now = new Date('2026-10-27T14:00:00Z');
      const pct = computeNowIndicatorPct(undefined, now, 'UTC');
      expect(pct).toBeCloseTo(50, 1);
    });

    it('returns null when now is outside bounds', () => {
      const early = new Date('2026-10-27T07:30:00Z');
      expect(computeNowIndicatorPct(baseVM, early, 'UTC')).toBeNull();

      const late = new Date('2026-10-27T20:30:00Z');
      expect(computeNowIndicatorPct(baseVM, late, 'UTC')).toBeNull();
    });

    it('returns null when endMin <= startMin or now is invalid Date', () => {
      const invalidVM: FamilyViewModel = {
        ...baseVM,
        hours_start: '20:00',
        hours_end: '08:00',
      };
      const now = new Date('2026-10-27T14:00:00Z');
      expect(computeNowIndicatorPct(invalidVM, now, 'UTC')).toBeNull();
      expect(computeNowIndicatorPct(baseVM, new Date('invalid'), 'UTC')).toBeNull();
    });

    it('handles timezone extraction and fallbacks', () => {
      // 18:00 UTC = 11:00 AM PDT (America/Los_Angeles, UTC-7)
      // 11:00 is (660 - 480) / (1200 - 480) = 180 / 720 = 25%
      const now = new Date('2026-10-27T18:00:00Z');
      const pct = computeNowIndicatorPct(baseVM, now, 'America/Los_Angeles');
      expect(pct).toBeCloseTo(25, 1);

      // Fallback for invalid timezone: does not throw, uses local time
      const fallbackPct = computeNowIndicatorPct(baseVM, now, 'Invalid/Timezone');
      expect(typeof fallbackPct === 'number' || fallbackPct === null).toBe(true);
    });
  });

  describe('matchesMemberFilter', () => {
    const members = [
      { name: 'Alex', initial: 'A', color: '#ff6f59', text_color: '#ffffff', pattern: 'solid' },
      { name: 'Sam', initial: 'S', color: '#8b60ed', text_color: '#ffffff', pattern: 'dots' },
    ];

    it('returns true when selectedMember is null or empty string', () => {
      expect(matchesMemberFilter({ id: '1', title: 'Test' }, null, members)).toBe(true);
      expect(matchesMemberFilter({ id: '1', title: 'Test' }, '', members)).toBe(true);
    });

    it('matches by ev.owners when owners array is non-empty', () => {
      const ev = { id: '1', title: 'Team Meeting', owners: ['Alex'] };
      expect(matchesMemberFilter(ev, 'Alex', members)).toBe(true);
      expect(matchesMemberFilter(ev, 'Sam', members)).toBe(false);
    });

    it('matches by member.initial when owners is absent or empty', () => {
      const ev = { id: '2', title: 'Coffee', initials: ['A'] };
      expect(matchesMemberFilter(ev, 'Alex', members)).toBe(true);
      expect(matchesMemberFilter(ev, 'Sam', members)).toBe(false);

      const evEmptyOwners = { id: '3', title: 'Lunch', owners: [], initials: ['S'] };
      expect(matchesMemberFilter(evEmptyOwners, 'Sam', members)).toBe(true);
      expect(matchesMemberFilter(evEmptyOwners, 'Alex', members)).toBe(false);
    });

    it('falls back to slice(0, 1) when member is not found in members list', () => {
      const ev = { id: '4', title: 'External', initials: ['J'] };
      expect(matchesMemberFilter(ev, 'Jordan', members)).toBe(true);
      expect(matchesMemberFilter(ev, 'Taylor', members)).toBe(false);
    });

    it('returns false when neither owners nor initials match', () => {
      const ev = { id: '5', title: 'Dentist' };
      expect(matchesMemberFilter(ev, 'Alex', members)).toBe(false);
    });
  });
});
