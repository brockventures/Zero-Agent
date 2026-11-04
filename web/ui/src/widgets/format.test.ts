import { describe, expect, it } from 'vitest';
import {
  calendarFeedActiveCalendars,
  calendarFeedDayGroups,
  computeHourlyGraphLayout,
  escapeHtml,
  formatDate,
  formatTime,
  isValidTimezone,
  relDate,
  tasksStats,
  todayPrecipProb,
} from './format';

describe('format helpers', () => {
  describe('escapeHtml', () => {
    it('escapes special characters', () => {
      expect(escapeHtml('<script>"alert" & \'test\'</script>')).toBe(
        '&lt;script&gt;&quot;alert&quot; &amp; &#39;test&#39;&lt;/script&gt;',
      );
    });
  });

  describe('tasksStats', () => {
    const fixedNow = new Date('2026-10-31T15:00:00Z');

    it('handles null, undefined, and empty objects', () => {
      expect(tasksStats(null)).toEqual({ total: 0, completed: 0, remaining: 0, overdue: 0 });
      expect(tasksStats(undefined)).toEqual({ total: 0, completed: 0, remaining: 0, overdue: 0 });
      expect(tasksStats({})).toEqual({ total: 0, completed: 0, remaining: 0, overdue: 0 });
      expect(tasksStats([])).toEqual({ total: 0, completed: 0, remaining: 0, overdue: 0 });
    });

    it('calculates metrics from a flat array with snake_case and PascalCase', () => {
      const items = [
        { done: true, due_date: '2026-10-20' }, // completed, past due (not overdue because done)
        { Done: false, DueDate: '2026-10-30' }, // pending, past due (overdue)
        { done: false, due_date: '2026-10-31' }, // pending, due today UTC (not overdue)
        { done: false, due_date: '2026-11-05' }, // pending, future due (not overdue)
        { done: false }, // pending, no due date (not overdue)
      ];

      const res = tasksStats(items, fixedNow);
      expect(res).toEqual({
        total: 5,
        completed: 1,
        remaining: 4,
        overdue: 1,
      });
    });

    it('supports RFC3339 timestamps for overdue checks', () => {
      const items = [
        { done: false, due_date: '2026-10-31T14:00:00Z' }, // 1 hour ago (overdue)
        { done: false, due_date: '2026-10-31T16:00:00Z' }, // 1 hour in future (not overdue)
      ];

      const res = tasksStats(items, fixedNow);
      expect(res.overdue).toBe(1);
    });

    it('extracts items from wrapper objects with items or list properties', () => {
      expect(tasksStats({ items: [{ done: true }] })).toEqual({
        total: 1,
        completed: 1,
        remaining: 0,
        overdue: 0,
      });
      expect(tasksStats({ Items: [{ Done: false }] })).toEqual({
        total: 1,
        completed: 0,
        remaining: 1,
        overdue: 0,
      });
      expect(tasksStats({ list: [{ done: true }] })).toEqual({
        total: 1,
        completed: 1,
        remaining: 0,
        overdue: 0,
      });
    });
  });

  describe('isValidTimezone', () => {
    it('validates IANA timezones', () => {
      expect(isValidTimezone('America/Los_Angeles')).toBe(true);
      expect(isValidTimezone('UTC')).toBe(true);
      expect(isValidTimezone('Europe/London')).toBe(true);
      expect(isValidTimezone('Invalid/Zone')).toBe(false);
      expect(isValidTimezone(null)).toBe(false);
      expect(isValidTimezone('')).toBe(false);
    });
  });

  describe('formatTime', () => {
    it('supports 1-argument formatTime(val) defaulting layout to "15:04"', () => {
      expect(formatTime('2026-09-26T14:05:00Z')).toBe('14:05');
      expect(formatTime('14:05')).toBe('14:05');
    });

    it('supports "3 PM" layout on ISO and RFC3339 strings', () => {
      expect(formatTime('3 PM', '2026-09-26T14:00')).toBe('2 PM');
      expect(formatTime('3 PM', '2026-09-26T15:00')).toBe('3 PM');
      expect(formatTime('3 PM', '2026-09-26T00:00')).toBe('12 AM');
      expect(formatTime('3 PM', '2026-09-26T12:00')).toBe('12 PM');
      expect(formatTime('3 PM', '2026-09-26 15:30:00')).toBe('3 PM');
      expect(formatTime('3 PM', '2026-01-02T18:39:10Z')).toBe('6 PM');
    });

    it('supports "3:04" layout without AM/PM', () => {
      expect(formatTime('3:04', '2026-09-26T09:05:00Z')).toBe('9:05');
      expect(formatTime('3:04', '2026-09-26T14:30:00Z')).toBe('2:30');
      expect(formatTime('3:04', '2026-09-26T00:15:00Z')).toBe('12:15');
    });

    it('supports "PM" layout returning AM or PM', () => {
      expect(formatTime('PM', '2026-09-26T09:05:00Z')).toBe('AM');
      expect(formatTime('PM', '2026-09-26T14:30:00Z')).toBe('PM');
    });

    it('supports "15:04" layout', () => {
      expect(formatTime('15:04', '2026-01-02T18:39:10Z')).toBe('18:39');
      expect(formatTime('15:04', '14:05')).toBe('14:05');
    });

    it('supports timezone conversion for RFC3339/ISO with Z or offset', () => {
      const eventUTC = '2026-10-12T03:30:00Z';
      expect(formatTime('3:04', eventUTC, 'America/Los_Angeles')).toBe('8:30');
      expect(formatTime('PM', eventUTC, 'America/Los_Angeles')).toBe('PM');
      expect(formatTime('15:04', eventUTC, 'America/Los_Angeles')).toBe('20:30');
      expect(formatTime('3 PM', eventUTC, 'America/Los_Angeles')).toBe('8 PM');
    });

    it('supports "3:04 PM" layout', () => {
      expect(formatTime('3:04 PM', '2026-09-26T14:30:00Z')).toBe('2:30 PM');
      expect(formatTime('3:04 PM', '2026-09-26T00:05:00Z')).toBe('12:05 AM');
      expect(formatTime('3:04 PM', '2026-09-26T12:00:00Z')).toBe('12:00 PM');
    });

    it('supports Date and numeric timestamp inputs with valid and invalid timezones', () => {
      const date = new Date('2026-10-12T03:30:00Z');
      const timestamp = date.getTime();

      // Date input with valid and invalid timezone
      expect(formatTime('3:04 PM', date, 'America/Los_Angeles')).toBe('8:30 PM');
      expect(formatTime('15:04', date, 'Invalid/Timezone')).toMatch(/^\d{2}:\d{2}$/);
      expect(formatTime('15:04', new Date(NaN))).toBe('');

      // Numeric timestamp input with valid and invalid timezone
      expect(formatTime('3:04 PM', timestamp, 'America/Los_Angeles')).toBe('8:30 PM');
      expect(formatTime('15:04', timestamp, 'Invalid/Timezone')).toMatch(/^\d{2}:\d{2}$/);
      expect(formatTime('15:04', NaN)).toBe('');

      // String full date with timezone via Date constructor
      expect(formatTime('3:04 PM', '2026-10-12 15:30:00', 'America/Los_Angeles')).toBe('3:30 PM');
      // Object input
      expect(formatTime('15:04', { foo: 'bar' })).toBe('');
    });

    it('returns empty string for invalid input or date-only string', () => {
      expect(formatTime('3 PM', null)).toBe('');
      expect(formatTime('3 PM', undefined)).toBe('');
      expect(formatTime('3 PM', '')).toBe('');
      expect(formatTime('3 PM', '2026-09-26')).toBe('');
      expect(formatTime('3 PM', 'not-a-date')).toBe('');
      expect(formatTime(null)).toBe('');
    });
  });

  describe('formatDate', () => {
    const target = new Date('2026-09-25T14:30:00Z');
    const eventUTC = '2026-10-12T03:30:00Z';

    it('handles 1-arg defaulting to "2006-01-02"', () => {
      expect(formatDate(target)).toBe('2026-09-25');
      expect(formatDate('2026-09-25T14:30:00Z')).toBe('2026-09-25');
    });

    it('supports "Jan 2", "Jan 02, 2006", "Monday • Jan 2", and "2006" layouts', () => {
      expect(formatDate('Jan 2', target)).toBe('Sep 25');
      expect(formatDate('Jan 02, 2006', target)).toBe('Sep 25, 2026');
      expect(formatDate('Monday • Jan 2', target)).toBe('Friday • Sep 25');
      expect(formatDate('2006', target)).toBe('2026');
    });

    it('supports Date and numeric timestamp inputs with valid and invalid timezones', () => {
      const date = new Date('2026-10-12T03:30:00Z');
      const timestamp = date.getTime();

      // Date input
      expect(formatDate('2006-01-02', date, 'America/Los_Angeles')).toBe('2026-10-11');
      expect(formatDate('2006-01-02', date, 'Invalid/Timezone')).toBe('2026-10-12');
      expect(formatDate('2006-01-02', new Date(NaN))).toBe('Invalid Date');

      // Numeric timestamp input
      expect(formatDate('2006-01-02', timestamp, 'America/Los_Angeles')).toBe('2026-10-11');
      expect(formatDate('2006-01-02', timestamp, 'Invalid/Timezone')).toBe('2026-10-12');
      expect(formatDate('2006-01-02', NaN)).toBe('NaN');

      // String without timezone fallback (local date)
      expect(formatDate('2006-01-02', '2026-10-12T15:30:00')).toBe('2026-10-12');
      // Unknown object type
      expect(formatDate('2006-01-02', { foo: 'bar' })).toBe('[object Object]');
    });

    it('supports empty or "default" layout', () => {
      expect(formatDate('', target)).toBe('2026-09-25');
      expect(formatDate('default', target)).toBe('2026-09-25');
    });

    it('preserves date-only strings without timezone shifting', () => {
      expect(formatDate('2006-01-02', '2026-10-12', 'America/Los_Angeles')).toBe('2026-10-12');
      expect(formatDate('Jan 2', '2026-10-12', 'America/Los_Angeles')).toBe('Oct 12');
      expect(formatDate('Monday • Jan 2', '2026-10-12', 'America/Los_Angeles')).toBe('Monday • Oct 12');
    });

    it('converts full UTC timestamp to destination timezone', () => {
      expect(formatDate('2006-01-02', eventUTC, 'America/Los_Angeles')).toBe('2026-10-11');
      expect(formatDate('Jan 2', eventUTC, 'America/Los_Angeles')).toBe('Oct 11');
      expect(formatDate('Monday • Jan 2', eventUTC, 'America/Los_Angeles')).toBe('Sunday • Oct 11');
    });

    it('returns raw value for unparseable input', () => {
      expect(formatDate('not-a-date')).toBe('not-a-date');
      expect(formatDate('2006-01-02', 'bad-date')).toBe('bad-date');
    });

    it('returns empty string on empty input', () => {
      expect(formatDate(null)).toBe('');
      expect(formatDate(undefined)).toBe('');
      expect(formatDate('')).toBe('');
    });
  });

  describe('relDate', () => {
    const ref = new Date('2026-09-25T12:00:00Z');
    const eventUTC = '2026-10-12T03:30:00Z'; // 2026-10-11 20:30 in LA

    it('returns relative date strings', () => {
      expect(relDate(ref, ref)).toBe('Today');
      expect(relDate(ref, '2026-09-25')).toBe('Today');
      expect(relDate(ref, '2026-09-26T09:00:00Z')).toBe('Tomorrow');
      expect(relDate(ref, '2026-09-24T23:00:00Z')).toBe('Yesterday');
      expect(relDate(ref, '2026-09-28T10:00:00Z')).toBe('in 3 days');
      expect(relDate(ref, '2026-09-21T10:00:00Z')).toBe('4 days ago');
    });

    it('supports timezone conversions', () => {
      // Sat Oct 10 19:00 in LA -> event is Oct 11 in LA -> Tomorrow
      const satNow = new Date('2026-10-11T02:00:00Z');
      expect(relDate(satNow, eventUTC, 'America/Los_Angeles')).toBe('Tomorrow');

      // Sun Oct 11 14:00 in LA -> event is Oct 11 in LA -> Today
      const sunNow = new Date('2026-10-11T21:00:00Z');
      expect(relDate(sunNow, eventUTC, 'America/Los_Angeles')).toBe('Today');
    });

    it('handles unparseable or empty values', () => {
      expect(relDate(ref, 'invalid')).toBe('invalid');
      expect(relDate(ref, null)).toBe('');
      expect(relDate(ref, undefined)).toBe('');
      expect(relDate(ref, '')).toBe('');
    });
  });

  describe('calendarFeedActiveCalendars', () => {
    it('returns empty array on empty or invalid input', () => {
      expect(calendarFeedActiveCalendars(null)).toEqual([]);
      expect(calendarFeedActiveCalendars({})).toEqual([]);
      expect(calendarFeedActiveCalendars('invalid')).toEqual([]);
    });

    it('filters out calendars where enabled is false or "false"', () => {
      const cfg = {
        calendars: [
          { name: 'Personal', color: '#ff6f59' },
          { name: 'Disabled Feed', color: '#cccccc', enabled: false },
          { name: 'Family', color: '#9d8df1', enabled: true },
          { name: 'Explicit False String', color: '#000000', enabled: 'false' },
        ],
      };
      const active = calendarFeedActiveCalendars(cfg);
      expect(active.map((c) => c.name)).toEqual(['Personal', 'Family']);
    });

    it('supports passing array directly and normalizes enabled boolean', () => {
      const list = [
        { name: 'Feed A', enabled: 'true', color: '#123456' },
        { name: 'Feed B', enabled: false },
      ];
      const active = calendarFeedActiveCalendars(list);
      expect(active).toHaveLength(1);
      expect(active[0]).toEqual({
        name: 'Feed A',
        color: '#123456',
        enabled: true,
      });
    });
  });

  describe('calendarFeedDayGroups', () => {
    const refTime = new Date('2026-10-10T15:30:00Z');

    it('returns empty array on nil or empty data', () => {
      expect(calendarFeedDayGroups(null, refTime)).toEqual([]);
      expect(calendarFeedDayGroups({}, refTime)).toEqual([]);
      expect(calendarFeedDayGroups({ events: [] }, refTime)).toEqual([]);
    });

    it('filters concluded events and groups by calendar date', () => {
      const data = {
        events: [
          { id: '1', title: 'Past Timed', start: '2026-10-09T20:00:00Z', end: '2026-10-09T21:00:00Z' },
          { id: '2', title: 'Past All-Day', start: '2026-10-09', end: '2026-10-09', all_day: true },
          { id: '3', title: 'Earlier Today Concluded', start: '2026-10-10T13:00:00Z', end: '2026-10-10T14:00:00Z' },
          { id: '4', title: 'Currently Ongoing', start: '2026-10-10T15:00:00Z', end: '2026-10-10T16:00:00Z' },
          { id: '5', title: 'Later Today', start: '2026-10-10T18:00:00Z', end: '2026-10-10T19:00:00Z' },
          { id: '6', title: 'Today All-Day', start: '2026-10-10', end: '2026-10-10', all_day: true },
          { id: '7', title: 'Ongoing No End', start: '2026-10-10T15:00:00Z' },
          { id: '8', title: 'Past No End', start: '2026-10-10T13:00:00Z' },
          { id: '9', title: 'Tomorrow All-Day', start: '2026-10-11', end: '2026-10-11', all_day: true },
        ],
      };

      const groups = calendarFeedDayGroups(data, refTime, 'UTC');
      expect(groups.length).toBe(2);
      expect(groups[0].date).toBe('2026-10-10');
      expect(groups[0].rel).toBe('Today');
      expect(groups[0].events.map((e) => e.title)).toEqual([
        'Currently Ongoing',
        'Later Today',
        'Today All-Day',
        'Ongoing No End',
      ]);

      expect(groups[1].date).toBe('2026-10-11');
      expect(groups[1].rel).toBe('Tomorrow');
      expect(groups[1].events.map((e) => e.title)).toEqual(['Tomorrow All-Day']);
    });

    it('normalizes description on both all-day and timed events from camelCase or PascalCase', () => {
      const data = {
        events: [
          {
            id: 'ev-1',
            title: 'All Day With Desc',
            start: '2026-10-10',
            all_day: true,
            description: 'Important details',
          },
          {
            id: 'ev-2',
            title: 'Timed With Pascal Desc',
            start: '2026-10-10T16:00:00Z',
            end: '2026-10-10T17:00:00Z',
            Description: 'Meeting agenda and notes',
          },
          {
            id: 'ev-3',
            title: 'Timed Without Desc',
            start: '2026-10-10T18:00:00Z',
            end: '2026-10-10T19:00:00Z',
          },
        ],
      };

      const groups = calendarFeedDayGroups(data, refTime, 'UTC');
      expect(groups).toHaveLength(1);
      expect(groups[0].events).toHaveLength(3);
      expect(groups[0].events[0].description).toBe('Important details');
      expect(groups[0].events[1].description).toBe('Meeting agenda and notes');
      expect(groups[0].events[2].description).toBeUndefined();
    });
  });

  describe('todayPrecipProb', () => {
    it('returns 0 for empty or invalid data', () => {
      expect(todayPrecipProb(null)).toBe(0);
      expect(todayPrecipProb(undefined)).toBe(0);
      expect(todayPrecipProb({})).toBe(0);
      expect(todayPrecipProb('invalid')).toBe(0);
    });

    it('extracts from daily array', () => {
      expect(todayPrecipProb({ daily: [{ precip_prob_max: 75 }] })).toBe(75);
      expect(todayPrecipProb({ Daily: [{ PrecipProbMax: 60 }] })).toBe(60);
    });

    it('extracts max across first 12 hourly entries when daily is missing', () => {
      const data = {
        hourly: [
          { precip_prob: 10 },
          { precip_prob: 45 },
          { precip_prob: 30 },
          // 13th hour should be ignored
          ...Array.from({ length: 9 }, () => ({ precip_prob: 0 })),
          { precip_prob: 99 },
        ],
      };
      expect(todayPrecipProb(data)).toBe(45);
    });

    it('falls back to current snapshot or direct property', () => {
      expect(todayPrecipProb({ current: { precip_prob_max: 35 } })).toBe(35);
      expect(todayPrecipProb({ Current: { PrecipProbMax: 40 } })).toBe(40);
      expect(todayPrecipProb({ precip_prob_max: 55 })).toBe(55);
    });
  });

  describe('computeHourlyGraphLayout', () => {
    it('returns null on empty or missing points', () => {
      expect(computeHourlyGraphLayout(null)).toBeNull();
      expect(computeHourlyGraphLayout([])).toBeNull();
      expect(computeHourlyGraphLayout([null])).toBeNull();
    });

    it('computes layout for a single point without curve or area path', () => {
      const pts = [{ time: '2026-09-26T14:00', temp: 72.0, precip_prob: 0, icon: 'weather-sunny' }];
      const layout = computeHourlyGraphLayout(pts, 'test-widget');
      expect(layout).not.toBeNull();
      expect(layout?.totalWidth).toBe(560.0);
      expect(layout?.svgHeight).toBe(270.0);
      expect(layout?.baseY).toBe(238.0);
      expect(layout?.gradientId).toBe('mm-weather-grad-test-widget');
      expect(layout?.curvePath).toBe('');
      expect(layout?.areaPath).toBe('');
      expect(layout?.points).toHaveLength(1);
      expect(layout?.points[0].tempRound).toBe(72);
      expect(layout?.points[0].timeLabel).toBe('Now');
      expect(layout?.points[0].icon).toBe('weather-sunny');

      const invalidTempLayout = computeHourlyGraphLayout([{ time: '2026-09-26T14:00', temp: NaN }]);
      expect(invalidTempLayout?.points[0].tempRound).toBe(0);
    });

    it('computes layout for multiple points with cubic Bezier paths and 270px default height', () => {
      const pts = [
        { time: '2026-09-26T14:00', temp: 65.0, precip_prob: 10, icon: 'weather-cloudy' },
        { time: '2026-09-26T15:00', temp: 68.0, precip_prob: 20, icon: 'weather-cloudy' },
        { time: '2026-09-26T16:00', temp: 62.0, precip_prob: 0, icon: 'weather-rainy' },
      ];
      const layout = computeHourlyGraphLayout(pts);
      expect(layout).not.toBeNull();
      expect(layout?.totalWidth).toBe(560.0);
      expect(layout?.svgHeight).toBe(270.0);
      expect(layout?.baseY).toBe(238.0);
      expect(layout?.gradientId).toBe('mm-weather-hourly-grad');
      expect(layout?.curvePath).toContain('M 18.0 144.0');
      // Verify cubic Bezier control points: C (x0+dx, y0), (x1-dx, y1), (x1, y1)
      expect(layout?.curvePath).toContain('C 149.0 144.0, 149.0 76.0, 280.0 76.0');
      expect(layout?.areaPath).toContain('M 18.0 238.0 L 18.0 144.0');
      expect(layout?.gridLinesPath).toContain('M 18.0 150.0 L 18.0 238.0');
      expect(layout?.points).toHaveLength(3);
      expect(layout?.points[0].timeLabel).toBe('Now');
      expect(layout?.points[1].timeLabel).toBe('3 PM');
      expect(layout?.points[2].timeLabel).toBe('4 PM');
      expect(layout?.points[0].precipProb).toBe(10);
      expect(layout?.points[1].precipProb).toBe(20);
      expect(layout?.points[2].precipProb).toBe(0);
    });

    it('computes layout for 24 hourly points capped to 10 points by default, and preserves 24 points when maxPoints is 24', () => {
      const pts = Array.from({ length: 24 }, (_, i) => ({
        time: `2026-09-26T${String(i).padStart(2, '0')}:00`,
        temp: 55 + (i % 10),
        icon: 'weather-sunny',
      }));
      const layout = computeHourlyGraphLayout(pts);
      expect(layout).not.toBeNull();
      expect(layout?.totalWidth).toBe(560.0);
      expect(layout?.points).toHaveLength(10);
      expect(layout?.points[0].timeLabel).toBe('Now');
      expect(layout?.points[9].timeLabel).toBe('9 AM');

      const customLayout = computeHourlyGraphLayout(pts, undefined, { maxPoints: 24 });
      expect(customLayout).not.toBeNull();
      expect(customLayout?.totalWidth).toBe(1344.0);
      expect(customLayout?.points).toHaveLength(24);
      expect(customLayout?.points[0].timeLabel).toBe('Now');
      expect(customLayout?.points[23].timeLabel).toBe('11 PM');
    });

    it('supports compact 108px height mode when passed via options', () => {
      const pts = [
        { time: '2026-09-26T12:00', temp: 80.0, precip_prob: 40, icon: 'weather-rainy' },
        { time: '2026-09-26T13:00', temp: 50.0, precip_prob: 0, icon: 'weather-cloudy' },
      ];
      const layout = computeHourlyGraphLayout(pts, undefined, {
        svgHeight: 108.0,
        curveTop: 34.0,
        curveBottom: 78.0,
        baseY: 90.0,
      });
      expect(layout).not.toBeNull();
      expect(layout?.svgHeight).toBe(108.0);
      expect(layout?.baseY).toBe(90.0);
      expect(layout?.isTall).toBe(false);
      expect(layout?.points[0].tempFontSize).toBe(12);
      expect(layout?.points[0].circleR).toBe(3.5);
      expect(layout?.points[0].timeY).toBe(102);
    });
  });

  describe('format helpers additional branch coverage', () => {
    it('covers relDate edge cases', () => {
      const refTime = new Date('2026-10-27T12:00:00Z');
      expect(relDate(refTime, null)).toBe('');
      expect(relDate(refTime, undefined)).toBe('');
      expect(relDate(refTime, '')).toBe('');
      expect(relDate(null as unknown as Date, '2026-10-27')).toBe('2026-10-27');
      expect(relDate(new Date(NaN), '2026-10-27')).toBe('2026-10-27');
      expect(relDate(refTime, 'invalid-date')).toBe('invalid-date');

      // Yesterday and multiple days ago
      expect(relDate(refTime, '2026-10-26T12:00:00Z', 'UTC')).toBe('Yesterday');
      expect(relDate(refTime, '2026-10-23T12:00:00Z', 'UTC')).toBe('4 days ago');
    });

    it('covers formatTime edge cases: offsets, invalid dates, out-of-range hours', () => {
      // Timezone offset without valid TZ
      expect(formatTime('15:04', '2026-10-27T15:30:00+02:00')).toBe('15:30');
      // Timezone offset with valid TZ
      expect(formatTime('15:04', '2026-10-27T15:30:00+02:00', 'UTC')).toBe('13:30');
      // Invalid Date string with Z
      expect(formatTime('15:04', '99999-99-99T99:99:99Z')).toBe('');
      // Non-ISO string with valid timezone
      expect(formatTime('15:04', 'October 27, 2026 15:30:00 GMT', 'America/New_York')).toBe('11:30');
      // Standalone hour out of range
      expect(formatTime('15:04', '25:70')).toBe('');
    });

    it('covers formatDate edge cases: unknown layouts, non-string/non-date types', () => {
      expect(formatDate('unknown-layout', '2026-10-27')).toBe('2026-10-27');
      expect(formatDate('2006-01-02', 1761566400000, 'UTC')).toBe('2025-10-27');
      expect(formatDate('2006-01-02', new Date(NaN))).toBe('Invalid Date');
      expect(formatTime('3:04 PM', new Date(NaN))).toBe('');

      // Test formatTime and formatDate cache clear branches (>2000 entries)
      for (let i = 0; i < 2005; i++) {
        formatTime('15:04', `2026-10-27T10:${String(i % 60).padStart(2, '0')}:${String((i * 7) % 60).padStart(2, '0')}.${String(i).padStart(3, '0')}Z`);
        formatDate('2006-01-02', `2026-${String((i % 12) + 1).padStart(2, '0')}-${String((i % 28) + 1).padStart(2, '0')}T${String(i % 24).padStart(2, '0')}:00:00.${String(i).padStart(3, '0')}Z`);
      }
    });

    it('covers calendarFeedDayGroups and calendarFeedActiveCalendars edge cases', () => {
      // Direct array input
      const arrResult = calendarFeedDayGroups(
        [
          { title: 'Array Event', start: '2026-10-27T10:00:00Z', end: '2026-10-27T11:00:00Z' },
          null,
        ],
        new Date('2026-10-27T08:00:00Z'),
        'UTC',
      );
      expect(arrResult).toHaveLength(1);
      expect(arrResult[0].events[0].title).toBe('Array Event');

      // PascalCase fields
      const pascalResult = calendarFeedDayGroups(
        {
          events: [
            {
              Title: 'Pascal Title',
              ID: 'pascal-id-1',
              Location: 'Meeting Room A',
              Description: 'Sync demo',
              CalendarName: 'Work',
              Color: '#3b82f6',
              Start: '2026-10-27T14:00:00Z',
              End: '2026-10-27T15:00:00Z',
            },
          ],
        },
        new Date('2026-10-27T08:00:00Z'),
        'UTC',
      );
      expect(pascalResult[0].events[0].title).toBe('Pascal Title');
      expect(pascalResult[0].events[0].id).toBe('pascal-id-1');
      expect(pascalResult[0].events[0].location).toBe('Meeting Room A');
      expect(pascalResult[0].events[0].description).toBe('Sync demo');
      expect(pascalResult[0].events[0].calendarName).toBe('Work');
      expect(pascalResult[0].events[0].color).toBe('#3b82f6');

      // Active calendars with nullish fields and non-object items
      const calendars = calendarFeedActiveCalendars([
        null,
        { name: null, color: null, enabled: true },
        { name: 'Alice', color: '#ff0000', enabled: true },
        { name: 'Bob', enabled: true },
      ]);
      expect(calendars).toHaveLength(3);
      expect(calendars[0].name).toBe('');
      expect(calendars[0].color).toBeUndefined();
      expect(calendars[1].name).toBe('Alice');
      expect(calendars[2].color).toBeUndefined();

      // computeHourlyGraphLayout custom baseY option
      const pts = [{ time: '2026-09-26T14:00', temp: 70, icon: 'weather-sunny' }];
      const layout = computeHourlyGraphLayout(pts, undefined, { baseY: 150 });
      expect(layout?.baseY).toBe(150);
      expect(formatDate('2006-01-02', true as unknown as string)).toBe('true');
    });

    it('covers calendarFeedDayGroups edge cases: past events, missing fields', () => {
      const refTime = new Date('2026-10-27T12:00:00Z');
      expect(calendarFeedDayGroups(null, refTime)).toEqual([]);
      expect(calendarFeedDayGroups({ events: null }, refTime)).toEqual([]);
      expect(calendarFeedDayGroups({ events: [{ title: '', start: '' }] }, refTime)).toEqual([]);

      // Past all-day and timed events filtered out
      const pastData = {
        events: [
          { title: 'Past All Day', start: '2026-10-20', all_day: true },
          { title: 'Past Timed', start: '2026-10-20T10:00:00Z', end: '2026-10-20T11:00:00Z' },
          { title: 'Past No End', start: '2026-10-20T10:00:00Z' },
          {
            ID: 'multi-1',
            Title: 'Today Event 1',
            Start: '2026-10-27T14:00:00Z',
            Location: 'Main Room',
            Description: 'Notes',
            CalendarName: 'Shared',
            Color: '#fff',
          },
          {
            ID: 'multi-2',
            Title: 'Today Event 2',
            Start: '2026-10-27T16:00:00Z',
          },
        ],
      };
      const groups = calendarFeedDayGroups(pastData, refTime, 'UTC');
      expect(groups).toHaveLength(1);
      expect(groups[0].events).toHaveLength(2);
      expect(groups[0].events[0].location).toBe('Main Room');
    });

    it('covers todayPrecipProb variations: daily, hourly, and current', () => {
      expect(todayPrecipProb({ daily: [{ precip_prob: 42 }] })).toBe(42);
      expect(todayPrecipProb({ daily: [{ PrecipitationProbability: 55 }] })).toBe(55);
      expect(todayPrecipProb({ hourly: [{ PrecipitationProbability: 65 }] })).toBe(65);
      expect(todayPrecipProb({ Hourly: [{ PrecipProb: 50 }] })).toBe(50);
      expect(todayPrecipProb({ current: { PrecipitationProbability: 75 } })).toBe(75);
    });
  });
});
