import { Profiler } from 'react';
import { act, fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import {
  NextUp,
  formatRelativeCountdown,
  parseEventDates,
  resolveNextUpEvent,
  selectCalendarEvents,
  useCalendarEvents,
} from './NextUp';
import type { RawCalendarEvent } from './NextUp';

describe('formatRelativeCountdown', () => {
  it('formats under 1 minute as <1m', () => {
    expect(formatRelativeCountdown(0)).toBe('in <1m');
    expect(formatRelativeCountdown(-5000)).toBe('in <1m');
    expect(formatRelativeCountdown(20000, true)).toBe('ends in <1m');
  });

  it('formats minutes under an hour', () => {
    expect(formatRelativeCountdown(15 * 60 * 1000)).toBe('in 15m');
    expect(formatRelativeCountdown(45 * 60 * 1000, true)).toBe('ends in 45m');
  });

  it('formats exact hours', () => {
    expect(formatRelativeCountdown(60 * 60 * 1000)).toBe('in 1h');
    expect(formatRelativeCountdown(2 * 60 * 60 * 1000, true)).toBe('ends in 2h');
  });

  it('formats combined hours and minutes', () => {
    expect(formatRelativeCountdown(90 * 60 * 1000)).toBe('in 1h 30m');
    expect(formatRelativeCountdown(125 * 60 * 1000, true)).toBe('ends in 2h 5m');
  });
});

describe('parseEventDates', () => {
  const refNow = new Date('2026-10-31T10:00:00Z');

  it('returns null for invalid inputs', () => {
    expect(parseEventDates(null as unknown as RawCalendarEvent, refNow)).toBeNull();
    expect(parseEventDates({ title: 'Invalid', start: 'invalid-date' }, refNow)).toBeNull();
  });

  it('parses all-day ISO date string', () => {
    const parsed = parseEventDates(
      {
        id: 'ev-all-day',
        title: 'Halloween',
        start: '2026-10-31',
        allDay: true,
      },
      refNow,
      'UTC'
    );
    expect(parsed).not.toBeNull();
    expect(parsed?.isAllDay).toBe(true);
    expect(parsed?.startDate.toISOString()).toContain('2026-10-31T00:00:00');
    expect(parsed?.endDate.toISOString()).toContain('2026-10-31T23:59:59');
  });

  it('parses timed HH:MM format', () => {
    const parsed = parseEventDates(
      {
        id: 'ev-meeting',
        title: 'Team Sync',
        start: '14:30',
        end: '15:30',
        date: '2026-10-31',
      },
      refNow,
      'UTC'
    );
    expect(parsed).not.toBeNull();
    expect(parsed?.isAllDay).toBe(false);
    expect(parsed?.startDate.toISOString()).toContain('2026-10-31T14:30:00');
    expect(parsed?.endDate.toISOString()).toContain('2026-10-31T15:30:00');
  });

  it('defaults end time to 1 hour after start when missing', () => {
    const parsed = parseEventDates(
      {
        title: 'Solo Start',
        start: '2026-10-31T12:00:00Z',
      },
      refNow,
      'UTC'
    );
    expect(parsed).not.toBeNull();
    expect(parsed!.endDate.getTime() - parsed!.startDate.getTime()).toBe(60 * 60 * 1000);
  });
});

describe('resolveNextUpEvent priority rules', () => {
  const baseNow = new Date('2026-10-31T10:00:00Z');
  const tz = 'UTC';

  it('ignores expired events whose end date is in the past', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'expired-1',
        title: 'Breakfast',
        start: '2026-10-31T08:00:00Z',
        end: '2026-10-31T09:00:00Z',
      },
    ];
    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).toBeNull();
  });

  it('prioritizes ongoing timed event over upcoming timed and all-day events', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'future-today',
        title: 'Afternoon Tea',
        start: '2026-10-31T15:00:00Z',
        end: '2026-10-31T16:00:00Z',
      },
      {
        id: 'ongoing-now',
        title: 'Morning Meeting',
        start: '2026-10-31T09:30:00Z',
        end: '2026-10-31T10:30:00Z',
      },
      {
        id: 'all-day-today',
        title: 'Halloween Holiday',
        start: '2026-10-31',
        allDay: true,
      },
    ];

    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('NOW');
    expect(resolved?.title).toBe('Morning Meeting');
    expect(resolved?.isNow).toBe(true);
    expect(resolved?.countdown).toBe('ends in 30m');
  });

  it('prioritizes today upcoming timed event over today all-day event', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'all-day-today',
        title: 'Full Day Festival',
        start: '2026-10-31',
        allDay: true,
      },
      {
        id: 'upcoming-today',
        title: 'Lunch with Alice',
        start: '2026-10-31T12:00:00Z',
        end: '2026-10-31T13:00:00Z',
      },
    ];

    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('NEXT');
    expect(resolved?.title).toBe('Lunch with Alice');
    expect(resolved?.isNow).toBe(false);
    expect(resolved?.countdown).toBe('in 2h');
  });

  it('prioritizes today all-day event when no timed events exist today', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'all-day-today',
        title: 'Costume Day',
        start: '2026-10-31',
        allDay: true,
      },
      {
        id: 'future-day-timed',
        title: 'Monday Standup',
        start: '2026-11-02T09:00:00Z',
        end: '2026-11-02T10:00:00Z',
      },
    ];

    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('TODAY');
    expect(resolved?.title).toBe('Costume Day');
    expect(resolved?.timeStr).toBe('All Day');
    expect(resolved?.countdown).toBe('');
    expect(resolved?.isNow).toBe(false);
  });

  it('resolves future timed event when nothing is scheduled today', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'future-timed',
        title: 'Doctor Appointment',
        start: '2026-11-01T14:00:00Z',
        end: '2026-11-01T15:00:00Z',
      },
    ];

    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('NEXT');
    expect(resolved?.title).toBe('Doctor Appointment');
    expect(resolved?.timeStr).toContain('Sun');
    expect(resolved?.isNow).toBe(false);
  });

  it('resolves future all-day event with uppercase weekday label', () => {
    const events: RawCalendarEvent[] = [
      {
        id: 'future-all-day',
        title: 'All Saints Day',
        start: '2026-11-01',
        allDay: true,
      },
    ];

    const resolved = resolveNextUpEvent(events, baseNow, tz);
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('SUN');
    expect(resolved?.title).toBe('All Saints Day');
    expect(resolved?.timeStr).toBe('All Day');
    expect(resolved?.countdown).toBe('');
  });
});

describe('selectCalendarEvents store extraction and deduplication', () => {
  it('extracts and deduplicates events across feed, grid, and viewModel formats', () => {
    const widgets = {
      'feed-widget': {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: {
          events: [
            { id: 'shared-ev-1', title: 'Dentist', start: '2026-10-31T11:00:00Z' },
            { id: 'shared-ev-1', title: 'Dentist Duplicate', start: '2026-10-31T11:00:00Z' },
            { id: 'feed-ev-2', title: 'Gym Workout', start: '2026-10-31T18:00:00Z' },
          ],
        },
      },
      'grid-widget': {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: {
          days: [
            {
              date: '2026-10-31',
              events: [
                { id: 'grid-ev-1', title: 'Coffee Chat', start: '11:00' },
                { id: 'grid-ev-3', title: 'Dinner', start: '19:00' },
              ],
              all_day: [{ id: 'holiday-1', title: 'Halloween' }],
            },
          ],
        },
      },
      'vm-widget': {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: null,
        viewModel: {
          week: {
            days: [
              {
                date: '2026-11-01',
                events: [{ id: 'vm-ev-4', title: 'Grocery Run', start: '10:00' }],
              },
            ],
          },
        },
      },
    };

    const extracted = selectCalendarEvents(widgets);
    expect(extracted).toHaveLength(6);
    const titles = extracted.map((e) => e.title);
    expect(titles).toContain('Dentist');
    expect(titles).toContain('Gym Workout');
    expect(titles).toContain('Coffee Chat');
    expect(titles).toContain('Dinner');
    expect(titles).toContain('Halloween');
    expect(titles).toContain('Grocery Run');
  });

  it('preserves multi-day event slices across grid day columns and parses empty start/end', () => {
    const widgets = {
      'grid-widget': {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: {
          days: [
            {
              date: '2026-10-30',
              events: [
                { id: 'multiday-conf', title: 'Tech Conference', start: '', end: '' },
              ],
            },
            {
              date: '2026-10-31',
              events: [
                { id: 'multiday-conf', title: 'Tech Conference', start: '', end: '' },
              ],
            },
          ],
        },
      },
    };

    const extracted = selectCalendarEvents(widgets);
    expect(extracted).toHaveLength(2);
    expect(extracted[0].id).toBe('multiday-conf-2026-10-30');
    expect(extracted[0].start).toBe('2026-10-30');
    expect(extracted[0].allDay).toBe(true);
    expect(extracted[1].id).toBe('multiday-conf-2026-10-31');
    expect(extracted[1].start).toBe('2026-10-31');
    expect(extracted[1].allDay).toBe(true);

    // On 2026-10-31 at 10:00 UTC, the 2026-10-30 slice is expired, but the 2026-10-31 slice resolves as TODAY!
    const resolved = resolveNextUpEvent(extracted, new Date('2026-10-31T10:00:00Z'), 'UTC');
    expect(resolved).not.toBeNull();
    expect(resolved?.label).toBe('TODAY');
    expect(resolved?.title).toBe('Tech Conference');
  });

  it('supports raw array in entry.data', () => {
    const widgets = {
      arrayFeed: {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: [{ id: 'arr-1', title: 'Array Event', start: '2026-10-31T14:00:00Z' }],
      },
    };
    const extracted = selectCalendarEvents(widgets);
    expect(extracted).toHaveLength(1);
    expect(extracted[0].title).toBe('Array Event');
  });

  it('extracts day.timed events, vm.week.all_day_events, and vm.month.days', () => {
    const widgets = {
      timedWidget: {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: {
          days: [
            {
              date: '2026-10-31',
              timed: [{ id: 'timed-ev-1', title: 'Timed Standup', start: '09:00', end: '09:30' }],
            },
          ],
        },
      },
      vmWidget: {
        state: 'ok',
        timestamp: '2026-10-31T10:00:00Z',
        data: null,
        viewModel: {
          week: {
            days: [
              { date: '2026-11-01', events: [] },
              { date: '2026-11-02', events: [] },
              { date: '2026-11-03', events: [] },
            ],
            all_day_events: [
              {
                id: 'all-day-banner',
                title: 'Design Sprint',
                start_col: 2,
              },
            ],
          },
          month: {
            days: [
              {
                date: '2026-11-15',
                events: [{ id: 'month-ev-1', title: 'Board Meeting', start: '14:00' }],
              },
            ],
          },
        },
      },
    };

    const extracted = selectCalendarEvents(widgets);
    const titles = extracted.map((e) => e.title);
    expect(titles).toContain('Timed Standup');
    expect(titles).toContain('Design Sprint');
    expect(titles).toContain('Board Meeting');

    const sprint = extracted.find((e) => e.title === 'Design Sprint');
    expect(sprint?.date).toBe('2026-11-02');
    expect(sprint?.allDay).toBe(true);
  });

  it('useCalendarEvents hook extracts events reactively from store', () => {
    useDisplayStore.setState({
      widgets: {
        w1: {
          state: 'ok',
          timestamp: '2026-10-31T10:00:00Z',
          data: [{ id: 'hook-ev', title: 'Hook Event', start: '2026-10-31T12:00:00Z' }],
        },
      },
    });
    function TestHookConsumer() {
      const events = useCalendarEvents();
      return <div data-testid="count">{events.length}</div>;
    }
    const { getByTestId } = render(<TestHookConsumer />);
    expect(getByTestId('count').textContent).toBe('1');
  });
});

describe('NextUp component', () => {
  beforeEach(() => {
    useDisplayStore.setState({
      header: { timestamp: null, timezone: 'UTC', weather: null },
      widgets: {},
    });
  });

  it('renders hidden when no calendar events exist', () => {
    const { container } = render(<NextUp />);
    const wrapper = container.querySelector('#header-next-event') as HTMLDivElement;
    expect(wrapper).not.toBeNull();
    expect(wrapper.style.display).toBe('none');
  });

  it('renders ongoing event pill with status-now and formatted countdown', () => {
    const now = new Date('2026-10-31T10:00:00Z');
    const events: RawCalendarEvent[] = [
      {
        id: 'now-1',
        title: 'Project Demo',
        start: '2026-10-31T09:45:00Z',
        end: '2026-10-31T10:45:00Z',
      },
    ];

    const { container } = render(<NextUp now={now} timezone="UTC" events={events} />);
    const wrapper = container.querySelector('#header-next-event') as HTMLDivElement;
    expect(wrapper.style.display).toBe('');

    const pill = container.querySelector('#next-event-pill');
    expect(pill?.classList.contains('status-now')).toBe(true);
    expect(container.querySelector('#next-event-label')?.textContent).toBe('NOW');
    expect(container.querySelector('#next-event-title')?.textContent).toBe('Project Demo');
    expect(container.querySelector('#next-event-countdown')?.textContent).toBe('ends in 45m');
  });

  it('handles click and keyboard Enter smooth scroll', () => {
    const onPillClick = vi.fn();
    const now = new Date('2026-10-31T10:00:00Z');
    const events: RawCalendarEvent[] = [
      {
        id: 'next-1',
        title: 'Upcoming Talk',
        start: '2026-10-31T11:00:00Z',
        end: '2026-10-31T12:00:00Z',
      },
    ];

    const { container } = render(
      <NextUp now={now} timezone="UTC" events={events} onPillClick={onPillClick} />
    );
    const pill = container.querySelector('#next-event-pill') as HTMLDivElement;

    fireEvent.click(pill);
    expect(onPillClick).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(pill, { key: 'Enter' });
    expect(onPillClick).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(pill, { key: ' ' });
    expect(onPillClick).toHaveBeenCalledTimes(3);
  });

  it('prefers document.body.dataset.timezone before initial SSE header event arrives', () => {
    document.body.dataset.timezone = 'America/New_York';
    useDisplayStore.setState({
      header: { timestamp: null, timezone: 'UTC', weather: null },
      widgets: {},
    });

    const events: RawCalendarEvent[] = [
      {
        id: 'ev-tz',
        title: 'Strategy Meeting',
        start: '2026-10-31T15:00:00Z',
        end: '2026-10-31T16:00:00Z',
      },
    ];

    const now = new Date('2026-10-31T14:00:00Z');
    const { container } = render(<NextUp now={now} events={events} />);

    // In America/New_York (EDT, UTC-4), 15:00 UTC is 11:00 AM
    expect(container.querySelector('#next-event-time')?.textContent).toContain('11:00');
    delete document.body.dataset.timezone;
  });

  it('smoothly scrolls to calendar target when onPillClick is omitted', () => {
    const gridTarget = document.createElement('div');
    gridTarget.className = 'widget-calendar-grid';
    const scrollSpy = vi.fn();
    gridTarget.scrollIntoView = scrollSpy;
    document.body.appendChild(gridTarget);

    const now = new Date('2026-10-31T10:00:00Z');
    const events: RawCalendarEvent[] = [
      {
        id: 'next-scroll',
        title: 'Calendar Review',
        start: '2026-10-31T11:00:00Z',
      },
    ];

    const { container } = render(<NextUp now={now} timezone="UTC" events={events} />);
    const pill = container.querySelector('#next-event-pill') as HTMLDivElement;

    fireEvent.click(pill);
    expect(scrollSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'nearest' });

    document.body.removeChild(gridTarget);
  });

  it('exercises periodic interval timer in NextUp component when propNow is omitted', () => {
    vi.useFakeTimers();
    try {
      const events: RawCalendarEvent[] = [
        {
          id: 'timer-ev',
          title: 'Timer Event',
          start: new Date(Date.now() + 60000).toISOString(),
        },
      ];

      const { unmount } = render(<NextUp timezone="UTC" events={events} />);
      // Fast-forward 15s to trigger scheduleNext timer callback
      act(() => {
        vi.advanceTimersByTime(16000);
      });
      unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it('sorts multiple events within ongoingTimed, upcomingTimed, allDayToday, and upcomingAllDay buckets', () => {
    const baseNow = new Date('2026-10-31T10:00:00Z');
    const events: RawCalendarEvent[] = [
      // 2 ongoing timed events
      { id: 'on-2', title: 'Later Ongoing', start: '2026-10-31T09:00:00Z', end: '2026-10-31T12:00:00Z' },
      { id: 'on-1', title: 'Earlier Ongoing', start: '2026-10-31T09:30:00Z', end: '2026-10-31T11:00:00Z' },
    ];
    // Ongoing sorted by end date ascending -> on-1 ends earlier (11:00)
    const res1 = resolveNextUpEvent(events, baseNow, 'UTC');
    expect(res1?.title).toBe('Earlier Ongoing');

    // 2 upcoming timed events today
    const events2: RawCalendarEvent[] = [
      { id: 'up-2', title: 'Later Upcoming', start: '2026-10-31T15:00:00Z', end: '2026-10-31T16:00:00Z' },
      { id: 'up-1', title: 'Earlier Upcoming', start: '2026-10-31T12:00:00Z', end: '2026-10-31T13:00:00Z' },
    ];
    const res2 = resolveNextUpEvent(events2, baseNow, 'UTC');
    expect(res2?.title).toBe('Earlier Upcoming');

    // 2 all day events today
    const events3: RawCalendarEvent[] = [
      { id: 'ad-2', title: 'All Day 2', start: '2026-10-31', allDay: true },
      { id: 'ad-1', title: 'All Day 1', start: '2026-10-31', allDay: true },
    ];
    const res3 = resolveNextUpEvent(events3, baseNow, 'UTC');
    expect(res3?.label).toBe('TODAY');

    // 2 upcoming all day events future
    const events4: RawCalendarEvent[] = [
      { id: 'fad-2', title: 'Future All Day 2', start: '2026-11-03', allDay: true },
      { id: 'fad-1', title: 'Future All Day 1', start: '2026-11-02', allDay: true },
    ];
    const res4 = resolveNextUpEvent(events4, baseNow, 'UTC');
    expect(res4?.title).toBe('Future All Day 1');
  });

  it('covers parseEventDates edge cases: date/dayDate fallbacks, standalone times, Date instances, UTC offset', () => {
    const baseNow = new Date('2026-10-31T12:00:00Z');

    expect(parseEventDates(null as unknown as RawCalendarEvent, baseNow)).toBeNull();

    // All-day with date instead of start
    const p1 = parseEventDates({ title: 'Ad Date', date: '2026-10-31', allDay: true } as unknown as RawCalendarEvent, baseNow, 'UTC');
    expect(p1).not.toBeNull();
    expect(p1?.startDate.toISOString()).toContain('2026-10-31');

    // All-day with dayDate
    const p2 = parseEventDates({ title: 'Ad DayDate', dayDate: '2026-10-31', allDay: true } as unknown as RawCalendarEvent, baseNow, 'UTC');
    expect(p2).not.toBeNull();

    // All-day with Date instances
    const p3 = parseEventDates(
      { title: 'Ad Date Obj', start: new Date('2026-10-31T00:00:00Z'), allDay: true },
      baseNow,
      'UTC'
    );
    expect(p3).not.toBeNull();

    // All-day with invalid end string fallback
    const p4 = parseEventDates(
      { title: 'Ad Invalid End', start: '2026-10-31', end: 'bad-end', allDay: true },
      baseNow,
      'UTC'
    );
    expect(p4).not.toBeNull();
    expect(p4?.endDate.toISOString()).toContain('2026-10-31T23:59:59.999');

    // Standalone times HH:mm with date
    const p5 = parseEventDates(
      { title: 'Timed Standalone', start: '09:30', end: '10:45', date: '2026-10-31' },
      baseNow,
      'UTC'
    );
    expect(p5?.startDate.toISOString()).toContain('09:30');
    expect(p5?.endDate.toISOString()).toContain('10:45');

    // Standalone time without date (falls back to referenceNow day)
    const p6 = parseEventDates({ title: 'Timed No Date', start: '14:15' }, baseNow, 'UTC');
    expect(p6?.startDate.toISOString()).toContain('14:15');

    // Date instances for timed event
    const p7 = parseEventDates(
      {
        title: 'Timed Date Objs',
        start: new Date('2026-10-31T09:00:00Z'),
        end: new Date('2026-10-31T10:00:00Z'),
      },
      baseNow,
      'UTC'
    );
    expect(p7?.startDate.toISOString()).toBe('2026-10-31T09:00:00.000Z');
  });

  it('selectCalendarEvents extracts from data arrays and viewModel week/month/days/events', () => {
    const widgets = {
      'grid-w': {
        meta: { type: 'calendar-grid', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
        state: 'healthy',
        timestamp: '2026-10-31T12:00:00Z',
        data: [
          {
            date: '2026-10-31',
            events: [{ title: 'Grid Day Ev', start: '10:00' }],
            early_events: [{ title: 'Early Ev', start: '06:00' }],
            late_events: [{ title: 'Late Ev', start: '22:00' }],
            timed: [{ title: 'Timed Ev', start: '14:00' }],
            all_day: [{ title: 'All Day Ev' }],
          },
        ],
        viewModel: {
          week: {
            days: [{ date: '2026-10-31' }],
            all_day_events: [
              { title: 'Week All Day', start_col: 1 },
              { title: 'Week All Day 2', start: '2026-10-31' },
            ],
          },
          month: {
            days: [
              {
                date: '2026-11-01',
                events: [{ title: 'Month Day Ev', start: '2026-11-01T10:00:00Z' }],
              },
            ],
          },
          days: [
            {
              date: '2026-11-02',
              events: [{ title: 'Direct VM Day Ev', start: '2026-11-02T10:00:00Z' }],
            },
          ],
          events: [{ title: 'Direct VM Event', start: '2026-11-03T10:00:00Z' }],
        },
      },
    };

    // @ts-expect-error test widgets input
    const extracted = selectCalendarEvents(widgets);
    expect(extracted.length).toBeGreaterThanOrEqual(8);
    const titles = extracted.map((e) => e.title);
    expect(titles).toContain('Grid Day Ev');
    expect(titles).toContain('Early Ev');
    expect(titles).toContain('Late Ev');
    expect(titles).toContain('Timed Ev');
    expect(titles).toContain('All Day Ev');
    expect(titles).toContain('Week All Day');
    expect(titles).toContain('Month Day Ev');
    expect(titles).toContain('Direct VM Day Ev');
    expect(titles).toContain('Direct VM Event');
  });

  it('handles pill click scrollIntoView and keyboard navigation (Enter/Space)', () => {
    const scrollSpy = vi.fn();
    const calEl = document.createElement('div');
    calEl.className = 'widget-calendar-grid';
    calEl.scrollIntoView = scrollSpy;
    document.body.appendChild(calEl);

    const baseNow = new Date('2026-10-31T10:00:00Z');
    const { container } = render(
      <NextUp
        now={baseNow}
        timezone="UTC"
        events={[{ id: 'e1', title: 'Future Event', start: '2026-10-31T14:00:00Z' }]}
      />
    );

    const pill = container.querySelector('#next-event-pill')!;
    expect(pill).not.toBeNull();

    // Click without onPillClick triggers scrollIntoView
    fireEvent.click(pill);
    expect(scrollSpy).toHaveBeenCalled();

    // Enter and Space keydown
    fireEvent.keyDown(pill, { key: 'Enter' });
    expect(scrollSpy).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(pill, { key: ' ' });
    expect(scrollSpy).toHaveBeenCalledTimes(3);

    document.body.removeChild(calEl);
  });

  it('covers timezone offsets, custom onPillClick, missing calendar el, and all_day_events edge branches', () => {
    const baseNow = new Date('2026-10-31T10:00:00Z');

    // UTC timezone parses correctly with 'Z'
    const pUtc = parseEventDates({ title: 'UTC Ev', start: '2026-10-31' }, baseNow, 'UTC');
    expect(pUtc).not.toBeNull();

    // Invalid timezone fallback
    const pInvalidTz = parseEventDates({ title: 'Invalid Tz Ev', start: '2026-10-31' }, baseNow, 'Invalid/Zone');
    expect(pInvalidTz).not.toBeNull();

    // raw.all_day snake_case
    const pAllDaySnake = parseEventDates({ title: 'Snake AllDay', start: '2026-10-31', all_day: true }, baseNow, 'UTC');
    expect(pAllDaySnake?.isAllDay).toBe(true);

    // Custom onPillClick
    const onPillClick = vi.fn();
    const { container: c1 } = render(
      <NextUp
        now={baseNow}
        timezone="UTC"
        events={[{ id: 'e1', title: 'Future Event', start: '2026-10-31T14:00:00Z' }]}
        onPillClick={onPillClick}
      />
    );
    const pill1 = c1.querySelector('#next-event-pill')!;
    fireEvent.click(pill1);
    expect(onPillClick).toHaveBeenCalledOnce();

    // Missing calendar el without onPillClick
    const { container: c2 } = render(
      <NextUp
        now={baseNow}
        timezone="UTC"
        events={[{ id: 'e2', title: 'No Cal El', start: '2026-10-31T14:00:00Z' }]}
      />
    );
    const pill2 = c2.querySelector('#next-event-pill')!;
    expect(() => fireEvent.click(pill2)).not.toThrow();

    // vm.week.all_day_events with camelCase startCol, null items, out-of-bounds startCol
    const weirdWidgets = {
      w: {
        state: 'ok',
        timestamp: '2026-10-31T12:00:00Z',
        viewModel: {
          week: {
            days: null,
            all_day_events: [
              null,
              { title: 'Camel StartCol', startCol: 1 },
              { title: 'OutOfBounds Col', start_col: 99 },
            ],
          },
        },
      },
    };
    // @ts-expect-error test inputs
    const extracted = selectCalendarEvents(weirdWidgets);
    expect(extracted.map((e) => e.title)).toContain('Camel StartCol');
    expect(extracted.map((e) => e.title)).toContain('OutOfBounds Col');

    // Month days and raw non-object events
    const monthWidgets = {
      w2: {
        state: 'ok',
        timestamp: '2026-10-31T12:00:00Z',
        viewModel: {
          month: {
            days: [
              null,
              {
                date: '2026-10-31',
                events: [
                  null,
                  { title: 'Month Ev', start: '2026-10-31T15:00:00Z' },
                ],
              },
            ],
          },
        },
      },
    };
    // @ts-expect-error test inputs
    const monthExtracted = selectCalendarEvents(monthWidgets);
    expect(monthExtracted.map((e) => e.title)).toContain('Month Ev');

    // Parse event with Date instances, missing title, upcoming all-day
    const pDateInstances = parseEventDates(
      {
        title: '',
        start: new Date('2026-10-31T12:00:00Z'),
        end: new Date('2026-10-31T13:00:00Z'),
        allDay: true,
      },
      baseNow,
      'UTC',
    );
    expect(pDateInstances?.title).toBe('Event');
    expect(pDateInstances?.isAllDay).toBe(true);

    // Upcoming all-day event
    const upcomingAllDayEv = parseEventDates(
      {
        title: 'Upcoming All Day',
        start: '2026-11-01',
        all_day: true,
      },
      baseNow,
      'UTC',
    );
    expect(upcomingAllDayEv?.isAllDay).toBe(true);

    // Default prop render (<NextUp /> without arguments)
    const { container: defContainer, unmount: unmountDef } = render(<NextUp />);
    expect(defContainer.querySelector('#header-next-event')).not.toBeNull();
    // Unmount clears active ticker timer
    unmountDef();

    // Invalid / missing timezone in resolveNextUpEvent and NextUp component
    const invalidTzResolved = resolveNextUpEvent(
      [{ id: 'inv-tz', title: 'Invalid TZ Ev', start: '2026-10-31T12:00:00Z' }],
      baseNow,
      'Invalid/NonExistent_Timezone',
    );
    expect(invalidTzResolved).not.toBeNull();

    const { container: invTzContainer } = render(
      <NextUp
        now={baseNow}
        timezone="Invalid/Timezone"
        events={[{ id: 'inv-tz-c', title: 'Invalid TZ Component', start: '2026-10-31T12:00:00Z' }]}
      />
    );
    expect(invTzContainer.querySelector('#next-event-pill')).not.toBeNull();

    // Date instances in parseEventDates: timed start+end Date, timed start-only Date, all-day start-only Date
    const timedDates = parseEventDates(
      {
        title: 'Timed Dates',
        start: new Date('2026-10-31T14:00:00Z'),
        end: new Date('2026-10-31T15:00:00Z'),
      },
      baseNow,
      'UTC',
    );
    expect(timedDates?.isAllDay).toBe(false);

    const timedDateStartOnly = parseEventDates(
      {
        title: 'Timed Start Only',
        start: new Date('2026-10-31T14:00:00Z'),
      },
      baseNow,
      'UTC',
    );
    expect(timedDateStartOnly?.isAllDay).toBe(false);

    const allDayDateStartOnly = parseEventDates(
      {
        title: 'All Day Start Only',
        start: new Date('2026-10-31T00:00:00Z'),
        allDay: true,
      },
      baseNow,
      'UTC',
    );
    expect(allDayDateStartOnly?.isAllDay).toBe(true);

    // Event with dayDate fallback
    const dayDateEv = parseEventDates(
      {
        title: 'Day Date Event',
        start: '10:00',
        end: '11:00',
        dayDate: '2026-10-31',
      },
      baseNow,
      'UTC',
    );
    expect(dayDateEv?.startDate.toISOString()).toContain('2026-10-31T10:00:00');

    // Upcoming all-day event priority in resolveNextUpEvent
    const upcomingAllDayResolved = resolveNextUpEvent(
      [{ id: 'future-ad', title: 'Future All Day', start: '2026-11-05', allDay: true }],
      baseNow,
      'UTC',
    );
    expect(upcomingAllDayResolved?.event.title).toBe('Future All Day');

    // selectCalendarEvents with null widget entry, day.date, and item without title
    const nullEntryWidgets = {
      wNull: null,
      wDayDate: {
        state: 'ok',
        data: {
          days: [
            {
              date: '2026-10-31',
              events: [
                { title: 'DayDate Ev', start: '11:00' },
                { id: 'no-title', start: '12:00' },
              ],
            },
          ],
        },
      },
    };
    // @ts-expect-error test inputs
    const extractedDayDate = selectCalendarEvents(nullEntryWidgets);
    expect(extractedDayDate.map((e) => e.title)).toContain('DayDate Ev');

    // Click handler scrolls target element into view when present
    const targetEl = document.createElement('div');
    targetEl.className = 'widget-calendar-feed';
    targetEl.scrollIntoView = vi.fn();
    document.body.appendChild(targetEl);

    const { container: clickC } = render(
      <NextUp
        now={baseNow}
        timezone="UTC"
        events={[{ id: 'scroll-test', title: 'Scroll Event', start: '2026-10-31T12:00:00Z' }]}
      />
    );
    const scrollPill = clickC.querySelector('#next-event-pill')!;
    fireEvent.click(scrollPill);
    expect(targetEl.scrollIntoView).toHaveBeenCalled();
    document.body.removeChild(targetEl);
  });

  it('does not re-render NextUp or mutate calendar event reference when an unrelated non-calendar widget updates', () => {
    useDisplayStore.setState({
      header: { timestamp: null, timezone: 'UTC', weather: null },
      widgets: {
        'cal-1': {
          meta: { type: 'calendar-grid', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'ok',
          timestamp: '2026-10-31T10:00:00Z',
          data: [
            {
              date: '2026-10-31',
              events: [{ title: 'Team Sync', start: '2026-10-31T11:00:00Z', end: '2026-10-31T12:00:00Z' }],
            },
          ],
        },
      },
    });

    const onRender = vi.fn();
    const fixedNow = new Date('2026-10-31T10:00:00Z');

    const { container } = render(
      <Profiler id="NextUp" onRender={onRender}>
        <NextUp now={fixedNow} timezone="UTC" />
      </Profiler>
    );

    expect(container.querySelector('#next-event-title')?.textContent).toBe('Team Sync');
    expect(onRender).toHaveBeenCalledTimes(1);

    // Apply update to an unrelated widget (e.g. weather-1)
    act(() => {
      useDisplayStore.setState((s) => ({
        widgets: {
          ...s.widgets,
          'weather-1': {
            meta: { type: 'weather', origin: [0, 0], dimensions: [2, 2], config: {}, assets_base: '' },
            state: 'ok',
            timestamp: '2026-10-31T10:05:00Z',
            data: { temp: 72 },
          },
        },
      }));
    });

    // onRender must still have been called only once (no re-render for NextUp)
    expect(onRender).toHaveBeenCalledTimes(1);
    expect(container.querySelector('#next-event-title')?.textContent).toBe('Team Sync');
  });
});
