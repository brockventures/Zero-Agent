import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../../store';
import { CalendarFeedWidget } from './CalendarFeedWidget';

const initialStore = useDisplayStore.getState();

describe('CalendarFeedWidget', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it('renders loading state when entry.data is undefined, null, or unhydrated empty object', () => {
    // 1. undefined
    useDisplayStore.setState({
      widgets: {
        'cal-loading': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: undefined,
          timestamp: '',
        },
      },
    });

    const { container: c1 } = render(
      <CalendarFeedWidget id="cal-loading" type="calendar-feed" />
    );
    expect(c1.querySelector('.calendar-loading')?.textContent).toContain(
      'Loading calendar feed...'
    );

    // 2. null
    useDisplayStore.setState({
      widgets: {
        'cal-loading': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: null,
          timestamp: '',
        },
      },
    });

    const { container: c2 } = render(
      <CalendarFeedWidget id="cal-loading" type="calendar-feed" />
    );
    expect(c2.querySelector('.calendar-loading')?.textContent).toContain(
      'Loading calendar feed...'
    );

    // 3. unhydrated empty object {} (initial state before first poll)
    useDisplayStore.setState({
      widgets: {
        'cal-loading': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '',
        },
      },
    });

    const { container: c3 } = render(
      <CalendarFeedWidget id="cal-loading" type="calendar-feed" />
    );
    expect(c3.querySelector('.calendar-loading')?.textContent).toContain(
      'Loading calendar feed...'
    );
    expect(c3.querySelector('.calendar-empty')).toBeNull();
  });

  it('renders empty state when data has no upcoming events', () => {
    useDisplayStore.setState({
      widgets: {
        'cal-empty': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: { events: [] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-empty" type="calendar-feed" />
    );

    const empty = container.querySelector('.calendar-empty');
    expect(empty).not.toBeNull();
    expect(empty?.querySelector('.empty-icon svg')).not.toBeNull();
    expect(empty?.querySelector('.empty-title')?.textContent).toBe(
      'All clear! Enjoy your day ✨'
    );
    expect(empty?.querySelector('.empty-subtitle')?.textContent).toBe(
      'No upcoming events on your agenda'
    );
  });

  it('renders error banner when entry state is error', () => {
    useDisplayStore.setState({
      widgets: {
        'cal-error': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'error',
          data: null,
          timestamp: '',
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-error" type="calendar-feed" />
    );

    const banner = container.querySelector('.widget-error-banner');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Widget Error');
  });

  it('renders custom title and active calendar chips in header', () => {
    useDisplayStore.setState({
      widgets: {
        'cal-chips': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {
              title: 'Family Schedule',
              calendars: [
                { name: 'Personal', color: '#ff4365', enabled: true },
                { name: 'Work', color: '#3a86ff', enabled: 'true' },
                { name: 'Archived', color: '#999999', enabled: false },
              ],
            },
            assets_base: '',
          },
          state: 'healthy',
          data: { events: [] },
          timestamp: '',
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-chips" type="calendar-feed" />
    );

    const title = container.querySelector('.calendar-header-title');
    expect(title?.textContent).toBe('Family Schedule');

    const chips = container.querySelector('.calendar-header-chips');
    expect(chips).not.toBeNull();

    const dots = chips?.querySelectorAll('.calendar-avatar-dot');
    expect(dots?.length).toBe(2);
    expect(dots?.[0].getAttribute('title')).toBe('Personal');
    expect(dots?.[1].getAttribute('title')).toBe('Work');

    const badge = chips?.querySelector('.calendar-count-badge');
    expect(badge?.textContent).toBe('2 Calendars');
  });

  it('renders multi-day grouped events with all-day and timed details', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-31T08:00:00Z'));

    const todayStr = '2026-10-31';
    const tomorrowStr = '2026-11-01';

    useDisplayStore.setState({
      header: { timestamp: `${todayStr}T08:00:00Z`, timezone: 'UTC', weather: null },
      widgets: {
        'cal-events': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { title: 'Upcoming' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            events: [
              {
                id: 'ev-allday',
                title: 'All Day Hackathon',
                start: todayStr,
                end: todayStr,
                all_day: true,
                calendar_name: 'Events',
                color: '#ff4365',
              },
              {
                id: 'ev-timed',
                title: 'Team Sync',
                start: `${todayStr}T14:30:00Z`,
                end: `${todayStr}T15:30:00Z`,
                all_day: false,
                location: 'Room A',
                calendar_name: 'Work',
                color: '#3a86ff',
              },
              {
                id: 'ev-tomorrow',
                title: 'Morning Flight',
                start: `${tomorrowStr}T09:00:00Z`,
                end: `${tomorrowStr}T12:00:00Z`,
                all_day: false,
                calendar_name: 'Travel',
              },
            ],
          },
          timestamp: `${todayStr}T08:00:00Z`,
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-events" type="calendar-feed" />
    );

    const groups = container.querySelectorAll('.calendar-day-group');
    expect(groups.length).toBe(2);

    // Group 0: Today
    const todayPill = groups[0].querySelector('.day-pill-badge');
    expect(todayPill?.classList.contains('day-pill-today')).toBe(true);
    expect(todayPill?.textContent).toContain('TODAY •');

    const todayEvents = groups[0].querySelectorAll('.calendar-event-item');
    expect(todayEvents.length).toBe(2);

    // All-day item
    expect(todayEvents[0].getAttribute('data-event-id')).toBe('ev-allday');
    expect(todayEvents[0].getAttribute('data-event-allday')).toBe('true');
    expect(todayEvents[0].querySelector('.event-time-allday')?.textContent).toBe('All Day');
    expect(todayEvents[0].querySelector('.event-title')?.textContent).toBe('All Day Hackathon');
    expect(todayEvents[0].querySelector('.event-calendar-pill')?.textContent).toBe('Events');

    // Timed item with location and color
    expect(todayEvents[1].getAttribute('data-event-id')).toBe('ev-timed');
    expect(todayEvents[1].getAttribute('data-event-allday')).toBe('false');
    expect(todayEvents[1].querySelector('.event-time-val')).not.toBeNull();
    expect(todayEvents[1].querySelector('.event-time-ampm')).not.toBeNull();
    expect(todayEvents[1].querySelector('.event-location-text')?.textContent).toBe('Room A');
    expect(todayEvents[1].querySelector('.event-calendar-pill')?.textContent).toBe('Work');

    // Group 1: Tomorrow
    const tomorrowPill = groups[1].querySelector('.day-pill-badge');
    expect(tomorrowPill?.classList.contains('day-pill-tomorrow')).toBe(true);
    expect(tomorrowPill?.textContent).toContain('TOMORROW •');

    const tomorrowEvents = groups[1].querySelectorAll('.calendar-event-item');
    expect(tomorrowEvents.length).toBe(1);
    expect(tomorrowEvents[0].querySelector('.event-title')?.textContent).toBe('Morning Flight');
  });

  it('handles PascalCase Events payload and invalid timestamp without crashing', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-31T08:00:00Z'));

    useDisplayStore.setState({
      header: { timestamp: '2026-10-31T08:00:00Z', timezone: 'UTC', weather: null },
      widgets: {
        'cal-pascal': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            Events: [
              {
                ID: 'ev-p1',
                Title: 'Pascal Meeting',
                Start: '2026-10-31T10:00:00Z',
                End: '2026-10-31T11:00:00Z',
              },
            ],
          },
          timestamp: 'invalid-timestamp-value',
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-pascal" type="calendar-feed" />
    );

    const title = container.querySelector('.event-title');
    expect(title?.textContent).toBe('Pascal Meeting');
  });

  it('opens timed and all-day event modals on click and keyboard, and closes via close button, backdrop click, or Escape', () => {
    const todayStr = '2026-10-31';
    useDisplayStore.setState({
      header: { timestamp: `${todayStr}T08:00:00Z`, timezone: 'UTC', weather: null },
      widgets: {
        'cal-modal-test': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            events: [
              {
                id: 'ev-allday',
                title: 'All Day Hackathon',
                start: todayStr,
                all_day: true,
                description: 'Full day hackathon building awesome stuff',
                calendar_name: 'Events',
              },
              {
                id: 'ev-timed',
                title: 'Team Sync',
                start: `${todayStr}T14:30:00Z`,
                end: `${todayStr}T15:30:00Z`,
                all_day: false,
                location: 'Room A',
                description: 'Weekly team sprint sync',
                calendar_name: 'Work',
                color: '#3a86ff',
              },
            ],
          },
          timestamp: `${todayStr}T08:00:00Z`,
        },
      },
    });

    const { container } = render(
      <CalendarFeedWidget id="cal-modal-test" type="calendar-feed" />
    );

    const timedEvent = container.querySelector('[data-event-id="ev-timed"]')!;
    expect(timedEvent).not.toBeNull();
    expect(timedEvent.classList.contains('cf-event-clickable')).toBe(true);
    expect(timedEvent.getAttribute('role')).toBe('button');
    expect(timedEvent.getAttribute('tabIndex')).toBe('0');

    // 1. Open timed event modal via click
    fireEvent.click(timedEvent);

    let modal = container.querySelector('.cf-modal-overlay');
    expect(modal).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Team Sync');
    const modalTime = container.querySelector('.cf-modal-time');
    expect(modalTime?.textContent).toBe('Sat, Oct 31 • 2:30 PM – 3:30 PM');
    const modalLocation = container.querySelector('.cf-modal-location');
    expect(modalLocation?.textContent).toBe('Room A');
    expect(container.querySelector('.cf-modal-owner-chip')?.textContent).toContain('Work');
    expect(container.querySelector('.cf-modal-description')?.textContent).toContain('Weekly team sprint sync');

    // 2. Close via close button
    const closeBtn = container.querySelector('.cf-modal-close')!;
    fireEvent.click(closeBtn);
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    // 3. Re-open and close via backdrop click
    fireEvent.click(timedEvent);
    modal = container.querySelector('.cf-modal-overlay');
    expect(modal).not.toBeNull();
    fireEvent.click(modal!);
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    // 4. Re-open and close via Escape
    fireEvent.click(timedEvent);
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    // 5. Open all-day event modal via Enter key
    const allDayEvent = container.querySelector('[data-event-id="ev-allday"]')!;
    expect(allDayEvent).not.toBeNull();
    fireEvent.keyDown(allDayEvent, { key: 'Enter' });

    modal = container.querySelector('.cf-modal-overlay');
    expect(modal).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('All Day Hackathon');
    expect(container.querySelector('.cf-modal-time')?.textContent).toBe('Sat, Oct 31 • All Day');
    expect(container.querySelector('.cf-modal-owner-chip')?.textContent).toContain('Events');
    expect(container.querySelector('.cf-modal-description')?.textContent).toContain('Full day hackathon building awesome stuff');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    // 6. Open all-day event modal via Space key
    fireEvent.keyDown(allDayEvent, { key: ' ' });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    // 7. Open via Spacebar key and ignore non-activation keys
    fireEvent.keyDown(allDayEvent, { key: 'Spacebar' });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    fireEvent.keyDown(allDayEvent, { key: 'ArrowDown' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();
  });

  it('handles event without end timestamp in modal correctly', () => {
    useDisplayStore.setState({
      widgets: {
        'cal-no-end': {
          meta: { type: 'calendar-feed', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            events: [
              {
                id: 'ev-no-end',
                title: 'Quick Call',
                start: '2026-10-31T10:00:00Z',
                all_day: false,
              },
            ],
          },
          timestamp: '2026-10-31T08:00:00Z',
        },
      },
    });

    const { container } = render(<CalendarFeedWidget id="cal-no-end" type="calendar-feed" />);
    const noEndEvent = container.querySelector('[data-event-id="ev-no-end"]')!;
    fireEvent.click(noEndEvent);
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Quick Call');
  });

  it('auto-closes event modal and resumes store after 60s inactivity timer', () => {
    vi.useFakeTimers();
    const todayStr = '2026-10-31';
    useDisplayStore.setState({
      header: { timestamp: `${todayStr}T08:00:00Z`, timezone: 'UTC', weather: null },
      widgets: {
        'cal-inactivity': {
          meta: {
            type: 'calendar-feed',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            events: [
              {
                id: 'ev-timed',
                title: 'Team Sync',
                start: `${todayStr}T14:30:00Z`,
                end: `${todayStr}T15:30:00Z`,
                all_day: false,
              },
            ],
          },
          timestamp: `${todayStr}T08:00:00Z`,
        },
      },
    });

    const pauseSpy = vi.spyOn(useDisplayStore.getState(), 'pause');
    const resumeSpy = vi.spyOn(useDisplayStore.getState(), 'resume');

    const { container } = render(
      <CalendarFeedWidget id="cal-inactivity" type="calendar-feed" />
    );

    const timedEvent = container.querySelector('[data-event-id="ev-timed"]')!;
    fireEvent.click(timedEvent);

    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    expect(pauseSpy).toHaveBeenCalledTimes(1);
    expect(resumeSpy).not.toHaveBeenCalled();

    // Advance 59s: modal still open
    act(() => {
      vi.advanceTimersByTime(59000);
    });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    expect(resumeSpy).not.toHaveBeenCalled();

    // Advance remaining 2s (total 61s): modal closes, store resumes
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();
    expect(resumeSpy).toHaveBeenCalledTimes(1);
  });

  it('covers keyboard navigation (Space, Spacebar), fallback event ID, camelCase fields, and tomorrow/future groups', () => {
    useDisplayStore.setState({
      header: { timestamp: '2026-10-31T08:00:00Z', timezone: 'UTC', weather: null },
      widgets: {
        'cal-coverage': {
          meta: { type: 'calendar-feed', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            events: [
              {
                title: 'Tomorrow Standup',
                start: '2026-11-01T09:00:00Z',
                allDay: true,
                calendarName: 'Team Work',
              },
              {
                id: 'ev-future',
                title: 'Future Workshop',
                start: '2026-11-05T14:00:00Z',
                all_day: false,
                calendar_name: 'Personal',
              },
            ],
          },
          timestamp: '2026-10-31T08:00:00Z',
        },
      },
    });

    const { container } = render(<CalendarFeedWidget id="cal-coverage" type="calendar-feed" />);

    expect(container.querySelector('.day-pill-tomorrow')?.textContent).toContain('TOMORROW');
    expect(container.querySelector('.day-pill-future')?.textContent).toContain('Thursday');

    const items = container.querySelectorAll('.calendar-event-item');
    expect(items.length).toBe(2);

    fireEvent.keyDown(items[0], { key: ' ' });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Tomorrow Standup');

    const closeBtn = container.querySelector('.cf-modal-close')!;
    fireEvent.click(closeBtn);
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    fireEvent.keyDown(items[1], { key: 'Spacebar' });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Future Workshop');

    fireEvent.keyDown(items[1], { key: 'Tab' });
    expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
  });
});
