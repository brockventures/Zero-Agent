import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../../store';
import { batchFitDayColumnEvents, CalendarGridWidget, type CalendarGridViewModel } from './CalendarGridWidget';

const mockViewModel: CalendarGridViewModel = {
  active_view: 'week',
  cached_range_start: '2026-10-01',
  cached_range_end: '2026-11-30',
  week: {
    range_start: '2026-10-25',
    range_end: '2026-10-31',
    range_label: 'Oct 25 – 31, 2026',
    today: '2026-10-27',
    shared_color: '#3D405B',
    members: [
      { name: 'Alex', initial: 'A', color: '#ff6f59', text_color: '#ffffff', pattern: 'solid' },
      { name: 'Sam', initial: 'S', color: '#8b60ed', text_color: '#ffffff', pattern: 'dots' },
    ],
    hour_markers: ['08:00', '12:00', '18:00'],
    timeline_markers: [
      { label: '08:00', top_pct: 0, is_start: true },
      { label: '12:00', top_pct: 50 },
      { label: '18:00', top_pct: 100, is_end: true },
    ],
    all_day_events: [
      { id: 'ev-allday-1', title: 'Conference Day', start: '2026-10-26', end: '2026-10-27', all_day: true, start_col: 2, col_span: 2, color: '#ff6f59', text_color: '#ffffff' },
      { id: 'ev-mealie-1', title: '🍽️ Tacos Dinner', start: '2026-10-27', all_day: true, start_col: 3, col_span: 1, color: '#8b60ed', text_color: '#ffffff', location: '/hearth-api/mealie/image/tacos.jpg', description: 'Delicious homemade carnitas tacos recipe details...' },
    ],
    days: [
      { date: '2026-10-25', weekday_abbr: 'Sun', day_of_month: 25, is_today: false, is_weekend: true, events: [] },
      {
        date: '2026-10-26', weekday_abbr: 'Mon', day_of_month: 26, is_today: false, is_weekend: false,
        events: [{ id: 'ev-timed-1', title: 'Morning Standup', start: '09:00', end: '09:30', all_day: false, top_pct: 10, height_pct: 15, left_pct: 0, width_pct: 100, color: '#ff6f59', text_color: '#ffffff', owners: ['Alex'], initials: ['A'], location: 'Room 101', description: 'Weekly team sprint planning and standup sync.' }],
      },
      {
        date: '2026-10-27', weekday_abbr: 'Tue', day_of_month: 27, is_today: true, is_weekend: false,
        early_events: [{ id: 'ev-early-1', title: 'Early Workout', start: '06:30', all_day: false, top_pct: 0, height_pct: 0, left_pct: 0, width_pct: 100, color: '#8b60ed', text_color: '#ffffff' }],
        events: [{ id: 'ev-timed-2', title: 'Project Demo', start: '14:00', end: '15:00', all_day: false, top_pct: 60, height_pct: 20, left_pct: 0, width_pct: 100, color: '#8b60ed', text_color: '#ffffff', owners: ['Sam'], initials: ['S'] }],
        late_events: [{ id: 'ev-late-1', title: 'Late Flight', start: '22:00', all_day: false, top_pct: 0, height_pct: 0, left_pct: 0, width_pct: 100, color: '#ff6f59', text_color: '#ffffff' }],
      },
      { date: '2026-10-28', weekday_abbr: 'Wed', day_of_month: 28, is_today: false, is_weekend: false, events: [] },
      { date: '2026-10-29', weekday_abbr: 'Thu', day_of_month: 29, is_today: false, is_weekend: false, events: [] },
      { date: '2026-10-30', weekday_abbr: 'Fri', day_of_month: 30, is_today: false, is_weekend: false, events: [] },
      { date: '2026-10-31', weekday_abbr: 'Sat', day_of_month: 31, is_today: false, is_weekend: true, events: [] },
    ],
  },
  month: {
    month_label: 'October 2026',
    month_name: 'October',
    year: 2026,
    month: 10,
    range_start: '2026-10-01',
    range_end: '2026-10-31',
    today: '2026-10-27',
    shared_color: '#3D405B',
    weekday_headers: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
    members: [
      { name: 'Alex', initial: 'A', color: '#ff6f59', text_color: '#ffffff', pattern: 'solid' },
      { name: 'Sam', initial: 'S', color: '#8b60ed', text_color: '#ffffff', pattern: 'dots' },
    ],
    days: [
      { date: '2026-09-27', day_of_month: 27, weekday_abbr: 'Sun', is_current_month: false, is_today: false, is_weekend: true, dots: [] },
      { date: '2026-10-01', day_of_month: 1, weekday_abbr: 'Thu', is_current_month: true, is_today: false, is_weekend: false, dots: [] },
      {
        date: '2026-10-27', day_of_month: 27, weekday_abbr: 'Tue', is_current_month: true, is_today: true, is_weekend: false,
        dots: [
          { name: 'Alex', initial: 'A', color: '#ff6f59', text_color: '#ffffff', pattern: 'solid', is_shared: false, member_index: 0 },
          { name: 'Sam', initial: 'S', color: '#8b60ed', text_color: '#ffffff', pattern: 'dots', is_shared: false, member_index: 1 },
        ],
        event_title: '🍽️ Tacos Dinner',
        events_count: 2,
        events: [
          { id: 'ev-month-1', title: '🍽️ Tacos Dinner', all_day: true, location: '/hearth-api/mealie/image/tacos.jpg' },
          { id: 'ev-month-2', title: 'Project Demo', start: '14:00', all_day: false },
        ],
      },
    ],
  },
};

function setGridState(override?: {
  widgetId?: string;
  config?: Record<string, unknown>;
  dimensions?: [number, number];
  data?: Record<string, unknown>;
  viewModel?: unknown;
  state?: 'healthy' | 'idle' | 'error';
  timestamp?: string;
}) {
  const id = override?.widgetId ?? 'cal-grid';
  useDisplayStore.setState({
    widgets: {
      [id]: {
        meta: {
          type: 'calendar-grid',
          origin: [0, 0],
          dimensions: override?.dimensions ?? [6, 2],
          config: override?.config ?? {},
          assets_base: '',
        },
        state: override?.state ?? 'healthy',
        data: override?.data ?? {},
        viewModel: (override && 'viewModel' in override ? override.viewModel : mockViewModel) as Record<string, unknown>,
        timestamp: override?.timestamp ?? '2026-10-27T10:00:00Z',
      },
    },
  });
}

describe('CalendarGridWidget', () => {
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    cleanup();
  });

  it('renders week view by default with days, header, members legend, and events', () => {
    setGridState({ config: { title: 'Family Grid' } });
    const { container, rerender } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    expect(container.querySelector('.cf-title')?.textContent).toBe('Family Grid');
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Oct 25 – 31, 2026');

    // Re-render with changed type to bust React.memo and trigger memoization cache hit
    rerender(<CalendarGridWidget id="cal-grid" type="calendar-grid-custom" />);
    expect(container.querySelector('.cf-title')?.textContent).toBe('Family Grid');

    const chips = container.querySelectorAll('.cf-member-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toContain('Alex');
    expect(chips[1].textContent).toContain('Sam');

    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-week')).toBe(true);
    expect(container.querySelector('.cf-view-btn[data-view="week"]')?.classList.contains('active')).toBe(true);
    expect(container.querySelectorAll('.cf-hour-marker').length).toBeGreaterThan(0);

    const days = container.querySelectorAll('.cf-day');
    expect(days.length).toBe(7);

    const todayCol = container.querySelector('.cf-day.cf-today');
    expect(todayCol).not.toBeNull();
    expect(todayCol?.getAttribute('data-date')).toBe('2026-10-27');

    expect(container.querySelector('[data-event-id="ev-allday-1"]')).not.toBeNull();
    expect(container.querySelector('[data-event-id="ev-timed-1"]')?.textContent).toContain('Morning Standup');
    expect(container.querySelector('[data-event-id="ev-early-1"]')?.textContent).toContain('Early Workout');
    expect(container.querySelector('[data-event-id="ev-late-1"]')?.textContent).toContain('Late Flight');
  });

  it('switches between week and month views and renders month grid with weekday headers, day numbers, and event dots', () => {
    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]');
    expect(monthBtn).not.toBeNull();
    fireEvent.click(monthBtn!);

    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-month')).toBe(true);
    expect(monthBtn?.classList.contains('active')).toBe(true);
    expect(container.querySelector('.cf-range-label.cf-label-month')?.textContent).toBe('October 2026');

    const weekdayHeaders = container.querySelectorAll('.cf-weekday-header');
    expect(weekdayHeaders.length).toBe(7);
    expect(weekdayHeaders[0].textContent).toBe('Sun');
    expect(container.querySelectorAll('.cf-month-dot').length).toBeGreaterThan(0);
  });

  it('renders day view as a 1-day slice of week.days and formats range label as Weekday, Mon D', () => {
    setGridState({ dimensions: [3, 2], config: { default_view: 'day' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-day')).toBe(true);
    const days = container.querySelectorAll('.cf-day');
    expect(days.length).toBe(1);
    expect(days[0].getAttribute('data-date')).toBe('2026-10-27');
    expect(container.querySelector('.cf-range-label.cf-label-day')?.textContent).toBe('Tuesday, Oct 27');
  });

  it('handles LRU(20) cache: caches fetched date views and evicts oldest entry when size exceeds 20', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...mockViewModel,
              week: { ...mockViewModel.week, range_label: `Range for ${url}` },
            },
          }),
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2020-01-01', cached_range_end: '2030-01-01' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    for (let i = 0; i < 21; i++) {
      await act(async () => {
        fireEvent.click(nextBtn);
      });
    }
    expect(fetchMock).toHaveBeenCalledTimes(21);

    const prevBtn = container.querySelector('.cf-nav-btn.cf-prev-btn')!;
    const callsBefore = fetchMock.mock.calls.length;
    await act(async () => {
      fireEvent.click(prevBtn);
    });
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  it('aborts prior in-flight fetch via AbortController on rapid paging', async () => {
    let capturedSignal: AbortSignal | null = null;
    const fetchMock = vi.fn().mockImplementation((_url: string, opts?: { signal?: AbortSignal }) => {
      capturedSignal = opts?.signal || null;
      return new Promise(() => {});
    });
    vi.stubGlobal('fetch', fetchMock);

    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    fireEvent.click(nextBtn);
    const signal1 = capturedSignal;
    expect(signal1).not.toBeNull();
    expect((signal1 as unknown as AbortSignal).aborted).toBe(false);

    fireEvent.click(nextBtn);
    expect((signal1 as unknown as AbortSignal).aborted).toBe(true);
  });

  it('rolls back currentDate, currentWeekStart, and pageOffset and displays Sync failed on fetch failure', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.reject(new Error('Network error')));
    vi.stubGlobal('fetch', fetchMock);

    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    fireEvent.click(nextBtn);
    await vi.waitFor(() => {
      expect(container.querySelector('.cf-sync-indicator')?.textContent).toBe('Sync failed');
    });
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');
  });

  it('displays Not synced yet without fetching when paging exceeds cached date bounds', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2026-10-25', cached_range_end: '2026-10-31' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    fireEvent.click(nextBtn);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector('.cf-sync-indicator')?.textContent).toBe('Not synced yet');
  });

  it('opens event modal on event click, displays details/Mealie hero image, and closes on close button, backdrop click, or Escape', () => {
    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const eventEl = container.querySelector('[data-event-id="ev-timed-1"]');
    expect(eventEl).not.toBeNull();
    fireEvent.click(eventEl!);

    let modal = container.querySelector('.cf-modal-overlay');
    expect(modal).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Morning Standup');
    const modalTime = container.querySelector('.cf-modal-time');
    expect(modalTime?.querySelector('svg.cf-modal-icon')).not.toBeNull();
    expect(modalTime?.textContent).toBe('Mon, Oct 26 • 09:00 – 09:30');
    const modalLocation = container.querySelector('.cf-modal-location');
    expect(modalLocation?.querySelector('svg.cf-modal-icon')).not.toBeNull();
    expect(modalLocation?.textContent).toBe('Room 101');
    expect(container.querySelector('.cf-modal-owner-chip')?.textContent).toContain('Alex');
    expect(container.querySelector('.cf-modal-description')?.textContent).toContain('Weekly team sprint planning');

    const closeBtn = container.querySelector('.cf-modal-close')!;
    fireEvent.click(closeBtn);
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    const mealieEl = container.querySelector('[data-event-id="ev-mealie-1"]');
    expect(mealieEl).not.toBeNull();
    fireEvent.click(mealieEl!);

    modal = container.querySelector('.cf-modal-overlay');
    expect(modal).not.toBeNull();
    expect(container.querySelector('.cf-modal-title')?.textContent).toBe('Tacos Dinner');
    expect(container.querySelector('.cf-modal-time')?.textContent).toBe('Tue, Oct 27 • All Day');
    const heroImg = container.querySelector('.cf-modal-card img');
    expect(heroImg?.getAttribute('src')).toBe('/hearth-api/mealie/image/tacos.jpg');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    fireEvent.click(mealieEl!);
    modal = container.querySelector('.cf-modal-overlay')!;
    fireEvent.click(modal);
    expect(container.querySelector('.cf-modal-overlay')).toBeNull();

    const allDaySpanEl = container.querySelector('[data-event-id="ev-allday-1"]');
    expect(allDaySpanEl).not.toBeNull();
    fireEvent.click(allDaySpanEl!);
    expect(container.querySelector('.cf-modal-time')?.textContent).toBe('Mon, Oct 26 – Tue, Oct 27 • All Day');
    fireEvent.keyDown(window, { key: 'Escape' });
  });

  it('decorates Mealie all-day pills declaratively with data-mealie-decorated="true"', () => {
    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const mealiePill = container.querySelector('[data-event-id="ev-mealie-1"]');
    expect(mealiePill?.getAttribute('data-mealie-decorated')).toBe('true');
    const pillImg = mealiePill?.querySelector('img');
    expect(pillImg?.getAttribute('src')).toBe('/hearth-api/mealie/image/tacos.jpg');
    expect(mealiePill?.querySelector('svg.cf-mealie-icon')).not.toBeNull();
    expect(mealiePill?.textContent).toBe('Tacos Dinner');
  });

  it('manages 60s inactivity reset and store pause/resume', () => {
    vi.useFakeTimers();
    setGridState();

    const pauseSpy = vi.spyOn(useDisplayStore.getState(), 'pause');
    const resumeSpy = vi.spyOn(useDisplayStore.getState(), 'resume');

    const { container, unmount } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    fireEvent.click(monthBtn);

    expect(pauseSpy).toHaveBeenCalledTimes(1);

    fireEvent.pointerDown(container.querySelector('.widget-calendar-grid')!);
    act(() => {
      vi.advanceTimersByTime(59000);
    });
    expect(resumeSpy).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(resumeSpy).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-week')).toBe(true);

    fireEvent.click(monthBtn);
    expect(pauseSpy).toHaveBeenCalledTimes(2);
    unmount();
    expect(resumeSpy).toHaveBeenCalledTimes(2);
  });

  it('batchFitDayColumnEvents performs two-phase measurement and toggles cf-event-compact', () => {
    const columnEl = document.createElement('div');
    columnEl.className = 'cf-day-canvas';

    const evEl = document.createElement('div');
    evEl.className = 'cf-day-event';

    const headerEl = document.createElement('div');
    headerEl.className = 'cf-event-header';
    const timeEl = document.createElement('span');
    timeEl.className = 'cf-event-time';
    headerEl.appendChild(timeEl);

    const titleEl = document.createElement('div');
    titleEl.className = 'cf-event-title';

    evEl.appendChild(headerEl);
    evEl.appendChild(titleEl);
    columnEl.appendChild(evEl);

    Object.defineProperty(evEl, 'clientHeight', { value: 20, configurable: true });
    Object.defineProperty(evEl, 'clientWidth', { value: 100, configurable: true });
    Object.defineProperty(headerEl, 'offsetHeight', { value: 15, configurable: true });
    Object.defineProperty(timeEl, 'offsetHeight', { value: 15, configurable: true });
    Object.defineProperty(titleEl, 'offsetHeight', { value: 15, configurable: true });

    batchFitDayColumnEvents(columnEl);
    expect(evEl.classList.contains('cf-event-compact')).toBe(true);

    Object.defineProperty(evEl, 'clientHeight', { value: 80, configurable: true });
    batchFitDayColumnEvents(columnEl);
    expect(evEl.classList.contains('cf-event-compact')).toBe(false);
  });

  it('clears LRU cache when entry.timestamp updates via SSE', () => {
    setGridState();
    const { rerender, container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const updatedViewModel: CalendarGridViewModel = {
      ...mockViewModel,
      week: { ...mockViewModel.week!, range_label: 'Nov 1 – 7, 2026' },
    };
    setGridState({ viewModel: updatedViewModel, timestamp: '2026-10-27T10:05:00Z' });
    rerender(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Nov 1 – 7, 2026');
  });

  it('renders empty message when neither week nor month data is available', () => {
    setGridState({ widgetId: 'cal-grid-empty', viewModel: undefined });
    const { container } = render(<CalendarGridWidget id="cal-grid-empty" type="calendar-grid" />);
    expect(container.querySelector('.cf-empty')?.textContent).toBe('No calendar data yet');
  });

  it('does not trigger network fetch when switching views at pageOffset 0 with baseline week and month payloads', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;

    fireEvent.click(monthBtn);
    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-month')).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(weekBtn);
    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-week')).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not arm inactivity timer on benign tap/pointer events when isInteractive is false and does not block screen swipes', () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    setGridState();
    const pauseSpy = vi.spyOn(useDisplayStore.getState(), 'pause');
    const resumeSpy = vi.spyOn(useDisplayStore.getState(), 'resume');

    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const gridEl = container.querySelector('.widget-calendar-grid')!;

    expect(gridEl.getAttribute('data-prevent-screen-swipe')).toBeNull();

    fireEvent.pointerDown(gridEl);
    fireEvent.touchStart(gridEl, { touches: [{ clientX: 250, clientY: 100 }] });
    fireEvent.touchMove(gridEl, { touches: [{ clientX: 50, clientY: 100 }] });
    fireEvent.touchEnd(gridEl, { changedTouches: [{ clientX: 50, clientY: 100 }] });

    expect(gridEl.getAttribute('data-page-offset')).toBe('0');
    expect(pauseSpy).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(65000);
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(resumeSpy).not.toHaveBeenCalled();
  });

  it('restores entry.viewModel without network fetch when clicking return to today', async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...mockViewModel,
              week: { ...mockViewModel.week, range_label: 'Paged Range' },
            },
          }),
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    setGridState();
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    await act(async () => {
      fireEvent.click(nextBtn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const todayBtn = container.querySelector('.cf-today-badge')!;
    await act(async () => {
      fireEvent.click(todayBtn);
    });

    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Oct 25 – 31, 2026');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('batchFitDayColumnEvents removes .cf-event-compact before reading dimensions to measure uncompacted geometry', () => {
    const columnEl = document.createElement('div');
    columnEl.className = 'cf-day-canvas';

    const evEl = document.createElement('div');
    evEl.className = 'cf-day-event cf-event-compact';

    const headerEl = document.createElement('div');
    headerEl.className = 'cf-event-header';
    const timeEl = document.createElement('span');
    timeEl.className = 'cf-event-time';
    headerEl.appendChild(timeEl);

    const titleEl = document.createElement('div');
    titleEl.className = 'cf-event-title';

    evEl.appendChild(headerEl);
    evEl.appendChild(titleEl);
    columnEl.appendChild(evEl);

    Object.defineProperty(evEl, 'clientHeight', { value: 30, configurable: true });
    Object.defineProperty(evEl, 'clientWidth', { value: 100, configurable: true });
    Object.defineProperty(titleEl, 'offsetHeight', { value: 15, configurable: true });
    Object.defineProperty(timeEl, 'offsetHeight', {
      get: () => (evEl.classList.contains('cf-event-compact') ? 0 : 20),
      configurable: true,
    });
    Object.defineProperty(headerEl, 'offsetHeight', {
      get: () => (evEl.classList.contains('cf-event-compact') ? 0 : 20),
      configurable: true,
    });

    batchFitDayColumnEvents(columnEl);
    expect(evEl.classList.contains('cf-event-compact')).toBe(true);
  });

  it('does not reset inactivity timer on background SSE widget.update when isInteractive is true', () => {
    vi.useFakeTimers();
    setGridState();

    const resumeSpy = vi.spyOn(useDisplayStore.getState(), 'resume');
    const { container, rerender } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    fireEvent.click(monthBtn);

    act(() => {
      vi.advanceTimersByTime(45000);
    });
    expect(resumeSpy).not.toHaveBeenCalled();

    const sseUpdatedViewModel: CalendarGridViewModel = {
      ...mockViewModel,
      active_view: 'week',
    };
    setGridState({ viewModel: sseUpdatedViewModel, timestamp: '2026-10-27T10:01:00Z' });
    rerender(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    act(() => {
      vi.advanceTimersByTime(16000);
    });

    expect(resumeSpy).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.widget-calendar-grid')?.classList.contains('cf-view-week')).toBe(true);
  });

  it('switches from paged month view to week view using currentDate and synchronizing week range', async () => {
    let requestedUrl = '';
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      requestedUrl = url;
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...mockViewModel,
              week: {
                ...mockViewModel.week,
                range_start: '2026-11-01',
                range_end: '2026-11-07',
                range_label: 'Nov 1 – 7, 2026',
              },
            },
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2026-01-01', cached_range_end: '2027-01-01' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    fireEvent.click(monthBtn);
    expect(fetchMock).not.toHaveBeenCalled();

    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;
    await act(async () => {
      fireEvent.click(nextBtn);
    });
    expect(requestedUrl).toContain('date=2026-11-01');

    const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;
    await act(async () => {
      fireEvent.click(weekBtn);
    });
    expect(requestedUrl).toContain('date=2026-11-01');
    expect(requestedUrl).toContain('view=week');
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Nov 1 – 7, 2026');
  });

  it('does not render .cf-allday-band in day view when there are no all-day events on the active day', () => {
    setGridState({
      dimensions: [3, 2],
      config: { default_view: 'day' },
      viewModel: {
        ...mockViewModel,
        week: {
          ...mockViewModel.week!,
          today: '2026-10-25',
        },
      },
    });

    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    expect(container.querySelector('.cf-allday-band')).toBeNull();
  });

  it('does not clobber anchor dates or prematurely deactivate interactivity when paging across views in opposite directions', async () => {
    let requestedUrl = '';
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      requestedUrl = url;
      const isNov = url.includes('2026-11');
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...mockViewModel,
              week: {
                ...mockViewModel.week,
                range_start: isNov ? '2026-11-01' : '2026-10-25',
                range_end: isNov ? '2026-11-07' : '2026-10-31',
                range_label: isNov ? 'Nov 1 – 7, 2026' : 'Oct 25 – 31, 2026',
              },
              month: {
                ...mockViewModel.month,
                range_start: '2026-11-01',
                range_end: '2026-11-30',
                month_label: 'November 2026',
              },
            },
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2026-01-01', cached_range_end: '2027-01-01' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    fireEvent.click(monthBtn);
    expect(fetchMock).not.toHaveBeenCalled();

    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;
    await act(async () => {
      fireEvent.click(nextBtn);
    });
    expect(requestedUrl).toContain('date=2026-11-01');
    const todayBadge = container.querySelector('.cf-today-badge') as HTMLElement;
    expect(todayBadge?.style.display).not.toBe('none');

    const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;
    await act(async () => {
      fireEvent.click(weekBtn);
    });

    const prevBtn = container.querySelector('.cf-nav-btn.cf-prev-btn')!;
    fetchMock.mockClear();
    await act(async () => {
      fireEvent.click(prevBtn);
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Oct 25 – 31, 2026');
  });

  it('resets both currentDate and currentWeekStart when paging back to the baseline month and switching to week view', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const isNov = url.includes('2026-11');
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...mockViewModel,
              week: {
                ...mockViewModel.week,
                range_start: isNov ? '2026-11-01' : '2026-10-25',
                range_end: isNov ? '2026-11-07' : '2026-10-31',
                range_label: isNov ? 'Nov 1 – 7, 2026' : 'Oct 25 – 31, 2026',
              },
              month: {
                ...mockViewModel.month,
                range_start: '2026-11-01',
                range_end: '2026-11-30',
                month_label: 'November 2026',
              },
            },
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2026-01-01', cached_range_end: '2027-01-01' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;
    const prevBtn = container.querySelector('.cf-nav-btn.cf-prev-btn')!;
    const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
    const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;

    await act(async () => {
      fireEvent.click(nextBtn);
    });
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('1');

    await act(async () => {
      fireEvent.click(monthBtn);
    });
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('1');

    fetchMock.mockClear();
    await act(async () => {
      fireEvent.click(prevBtn);
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');

    await act(async () => {
      fireEvent.click(weekBtn);
    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-10-27');
    const todayBadge = container.querySelector('.cf-today-badge') as HTMLElement;
    expect(todayBadge?.style.display).toBe('none');
  });

  it('tracks baseDate and baseWeekStart updates when resting without flipping isInteractive or pausing store', () => {
    setGridState({ timestamp: '2026-10-27T23:59:00Z' });
    const pauseSpy = vi.spyOn(useDisplayStore.getState(), 'pause');
    const { container, rerender } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    const nextDayVM: CalendarGridViewModel = {
      ...mockViewModel,
      week: {
        ...mockViewModel.week!,
        range_start: '2026-11-01',
        range_end: '2026-11-07',
        range_label: 'Nov 1 – 7, 2026',
        today: '2026-11-01',
      },
      month: { ...mockViewModel.month!, today: '2026-11-01' },
    };

    setGridState({ viewModel: nextDayVM, timestamp: '2026-11-01T00:00:05Z' });
    rerender(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

    expect(pauseSpy).not.toHaveBeenCalled();
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-11-01');
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Nov 1 – 7, 2026');
  });

  it('aborts in-flight page fetches when hitting the LRU cache or returning to baseline date', async () => {
    let abortSignalPage1: AbortSignal | undefined;
    let resolvePage1Json: ((val: unknown) => void) | undefined;

    const fetchMock = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      abortSignalPage1 = init?.signal ?? undefined;
      return Promise.resolve({
        ok: true,
        json: () =>
          new Promise((resolve) => {
            resolvePage1Json = resolve;
          }),
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setGridState({ config: { cached_range_start: '2026-01-01', cached_range_end: '2027-01-01' } });
    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;
    const prevBtn = container.querySelector('.cf-nav-btn.cf-prev-btn')!;

    await act(async () => {
      fireEvent.click(nextBtn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(abortSignalPage1?.aborted).toBe(false);

    await act(async () => {
      fireEvent.click(prevBtn);
    });
    expect(abortSignalPage1?.aborted).toBe(true);
    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');

    await act(async () => {
      resolvePage1Json?.({
        view_model: {
          ...mockViewModel,
          week: { ...mockViewModel.week, range_label: 'Stale Week Range' },
        },
      });
    });
    expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toBe('Oct 25 – 31, 2026');
  });

  it('computes week pageOffset and baseline week bounds using weekVM.days.length for narrow 3-day widgets', async () => {
    const narrow3DayVM: CalendarGridViewModel = {
      ...mockViewModel,
      week: {
        ...mockViewModel.week!,
        range_start: '2026-10-27',
        range_end: '2026-10-29',
        range_label: 'Oct 27 – 29, 2026',
        days: mockViewModel.week!.days!.slice(0, 3),
      },
    };

    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            view_model: {
              ...narrow3DayVM,
              week: {
                ...narrow3DayVM.week!,
                range_start: '2026-10-30',
                range_end: '2026-11-01',
                range_label: 'Oct 30 – Nov 1, 2026',
              },
            },
          }),
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    setGridState({
      dimensions: [3, 2],
      config: { days: 3, cached_range_start: '2026-01-01', cached_range_end: '2027-01-01' },
      viewModel: narrow3DayVM,
    });

    const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
    const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;

    await act(async () => {
      fireEvent.click(nextBtn);
    });

    expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('1');
    const todayBadge = container.querySelector('.cf-today-badge') as HTMLElement;
    expect(todayBadge?.style.display).not.toBe('none');
  });

  describe('cf-now-indicator', () => {
    it('renders .cf-now-indicator at expected top percentage on .cf-today when within bounds', () => {
      vi.useFakeTimers();
      // 13:00 UTC with start 08:00 (480) and end 18:00 (1080) -> (780 - 480) / 600 = 50%
      vi.setSystemTime(new Date('2026-10-27T13:00:00Z'));
      useDisplayStore.setState({
        header: { timestamp: '2026-10-27T13:00:00Z', timezone: 'UTC', weather: null },
      });

      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const todayCol = container.querySelector('.cf-day.cf-today');
      expect(todayCol).toBeTruthy();

      const indicator = todayCol?.querySelector('.cf-now-indicator') as HTMLElement;
      expect(indicator).toBeTruthy();
      expect(indicator.getAttribute('data-now-pct')).toBe('50.0');
      expect(indicator.style.top).toMatch(/^50(?:\.0+)?%$/);
      expect(indicator.querySelector('.cf-now-dot')).toBeTruthy();

      // Non-today days must not contain .cf-now-indicator
      const nonTodayCols = container.querySelectorAll('.cf-day:not(.cf-today)');
      expect(nonTodayCols.length).toBeGreaterThan(0);
      nonTodayCols.forEach((col) => {
        expect(col.querySelector('.cf-now-indicator')).toBeNull();
      });
    });

    it('hides .cf-now-indicator when current time is outside bounds', () => {
      vi.useFakeTimers();
      // 06:00 UTC is earlier than timeline start (08:00)
      vi.setSystemTime(new Date('2026-10-27T06:00:00Z'));
      useDisplayStore.setState({
        header: { timestamp: '2026-10-27T06:00:00Z', timezone: 'UTC', weather: null },
      });

      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const todayCol = container.querySelector('.cf-day.cf-today');
      expect(todayCol).toBeTruthy();
      expect(todayCol?.querySelector('.cf-now-indicator')).toBeNull();
    });

    it('updates .cf-now-indicator when 60s timer ticks', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-10-27T13:00:00Z'));
      useDisplayStore.setState({
        header: { timestamp: '2026-10-27T13:00:00Z', timezone: 'UTC', weather: null },
      });

      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const indicator = container.querySelector('.cf-today .cf-now-indicator') as HTMLElement;
      expect(indicator).toBeTruthy();
      expect(indicator.getAttribute('data-now-pct')).toBe('50.0');

      // Advance by 60 seconds: 13:01:00 -> (781 - 480) / 600 = 50.1666... -> 50.2%
      act(() => {
        vi.advanceTimersByTime(60_000);
      });

      const updated = container.querySelector('.cf-today .cf-now-indicator') as HTMLElement;
      expect(updated.getAttribute('data-now-pct')).toBe('50.2');
    });

    it('pauses now timer when document is hidden and updates immediately on visibility restore', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-10-27T13:00:00Z'));
      useDisplayStore.setState({
        header: { timestamp: '2026-10-27T13:00:00Z', timezone: 'UTC', weather: null },
        visibility: { visible: true },
      });

      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      let indicator = container.querySelector('.cf-today .cf-now-indicator') as HTMLElement;
      expect(indicator.getAttribute('data-now-pct')).toBe('50.0');

      // Hide document
      act(() => {
        useDisplayStore.setState({ visibility: { visible: false } });
      });

      // Advance time while hidden: 30 minutes to 13:30:00 -> (810 - 480) / 600 = 55.0%
      act(() => {
        vi.advanceTimersByTime(30 * 60_000);
      });

      // While hidden and timer was disarmed, it should still retain old value (not ticked via interval)
      indicator = container.querySelector('.cf-today .cf-now-indicator') as HTMLElement;
      expect(indicator.getAttribute('data-now-pct')).toBe('50.0');

      // Restore document visibility
      act(() => {
        useDisplayStore.setState({ visibility: { visible: true } });
      });

      // On visibility restore, useEffect immediately calls setNow(new Date())
      indicator = container.querySelector('.cf-today .cf-now-indicator') as HTMLElement;
      expect(indicator.getAttribute('data-now-pct')).toBe('55.0');
    });
  });

  describe('member chip filters and day drill-down', () => {
    it('toggles member filter on chip click, dimming non-selected chips, and filters week events', () => {
      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const alexChip = container.querySelector('.cf-member-chip[data-member="Alex"]') as HTMLButtonElement;
      const samChip = container.querySelector('.cf-member-chip[data-member="Sam"]') as HTMLButtonElement;
      expect(alexChip).toBeTruthy();
      expect(samChip).toBeTruthy();
      expect(alexChip.getAttribute('aria-pressed')).toBe('false');
      expect(samChip.getAttribute('aria-pressed')).toBe('false');

      // Before filtering, both events are visible
      expect(container.querySelector('[data-event-id="ev-timed-1"]')).toBeTruthy();
      expect(container.querySelector('[data-event-id="ev-timed-2"]')).toBeTruthy();

      // Click Alex chip -> filter to Alex
      fireEvent.click(alexChip);
      expect(alexChip.classList.contains('active')).toBe(true);
      expect(alexChip.getAttribute('aria-pressed')).toBe('true');
      expect(samChip.classList.contains('dimmed')).toBe(true);
      expect(samChip.style.opacity).toBe('0.45');

      // Alex event is visible; Sam event is filtered out
      expect(container.querySelector('[data-event-id="ev-timed-1"]')).toBeTruthy();
      expect(container.querySelector('[data-event-id="ev-timed-2"]')).toBeNull();

      // Click Alex chip again -> toggle off
      fireEvent.click(alexChip);
      expect(alexChip.classList.contains('active')).toBe(false);
      expect(alexChip.getAttribute('aria-pressed')).toBe('false');
      expect(samChip.classList.contains('dimmed')).toBe(false);

      expect(container.querySelector('[data-event-id="ev-timed-1"]')).toBeTruthy();
      expect(container.querySelector('[data-event-id="ev-timed-2"]')).toBeTruthy();

      // Click Sam chip -> filter to Sam
      fireEvent.click(samChip);
      expect(samChip.classList.contains('active')).toBe(true);
      expect(samChip.getAttribute('aria-pressed')).toBe('true');
      expect(alexChip.classList.contains('dimmed')).toBe(true);
      expect(alexChip.style.opacity).toBe('0.45');

      expect(container.querySelector('[data-event-id="ev-timed-1"]')).toBeNull();
      expect(container.querySelector('[data-event-id="ev-timed-2"]')).toBeTruthy();
    });

    it('filters month dots and events when member filter is active', () => {
      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      // Baseline has 2 dots on 2026-10-27
      expect(container.querySelectorAll('.cf-month-dot').length).toBe(2);

      const alexChip = container.querySelector('.cf-member-chip[data-member="Alex"]') as HTMLButtonElement;
      fireEvent.click(alexChip);

      // Only Alex's dot remains
      const dotsAfterAlex = container.querySelectorAll('.cf-month-dot');
      expect(dotsAfterAlex.length).toBe(1);
      expect(dotsAfterAlex[0].getAttribute('title')).toBe('Alex');

      // Toggle off -> both dots return
      fireEvent.click(alexChip);
      expect(container.querySelectorAll('.cf-month-dot').length).toBe(2);
    });

    it('resets selected member filter on inactivity timeout', () => {
      vi.useFakeTimers();
      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const alexChip = container.querySelector('.cf-member-chip[data-member="Alex"]') as HTMLButtonElement;
      fireEvent.click(alexChip);
      expect(alexChip.classList.contains('active')).toBe(true);

      // Advance by 60s inactivity threshold
      act(() => {
        vi.advanceTimersByTime(60_000);
      });

      expect(alexChip.classList.contains('active')).toBe(false);
      expect(alexChip.getAttribute('aria-pressed')).toBe('false');
    });

    it('drills down into day view when day header is clicked or pressed with Enter/Space in week view', () => {
      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const monHeader = container.querySelector('.cf-day[data-date="2026-10-26"] .cf-day-header') as HTMLElement;
      expect(monHeader).toBeTruthy();
      expect(monHeader.getAttribute('role')).toBe('button');
      expect(monHeader.getAttribute('tabindex')).toBe('0');

      // Click day header drills down to 2026-10-26 day view
      fireEvent.click(monHeader);

      const widget = container.querySelector('.widget-calendar-grid');
      expect(widget?.getAttribute('data-active-view')).toBe('day');
      expect(widget?.getAttribute('data-current-date')).toBe('2026-10-26');

      // In day view, day header should not have role="button" or tabindex
      const dayViewHeader = container.querySelector('.cf-day-header') as HTMLElement;
      expect(dayViewHeader.getAttribute('role')).toBeNull();
      expect(dayViewHeader.getAttribute('tabindex')).toBeNull();

      // Switch back to week view
      const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;
      fireEvent.click(weekBtn);

      // Keyboard drill-down with Enter
      const tueHeader = container.querySelector('.cf-day[data-date="2026-10-27"] .cf-day-header') as HTMLElement;
      fireEvent.keyDown(tueHeader, { key: 'Enter' });
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-10-27');
    });

    it('drills down into day view when month day header or +N more is clicked or pressed with Enter/Space in month view', () => {
      const monthWithMultipleEvents: CalendarGridViewModel = {
        ...mockViewModel,
        month: {
          ...mockViewModel.month!,
          days: [
            {
              date: '2026-10-27',
              day_of_month: 27,
              is_current_month: true,
              is_today: true,
              is_weekend: false,
              events_count: 3,
              event_title: 'First Event',
              events: [
                { id: 'ev-1', title: 'Event 1' },
                { id: 'ev-2', title: 'Event 2' },
                { id: 'ev-3', title: 'Event 3' },
              ],
            },
          ],
        },
      };

      setGridState({ viewModel: monthWithMultipleEvents });
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      const monthDayHeader = container.querySelector('.cf-month-day-header') as HTMLElement;
      expect(monthDayHeader).toBeTruthy();
      expect(monthDayHeader.getAttribute('role')).toBe('button');
      expect(monthDayHeader.getAttribute('tabindex')).toBe('0');

      // Click month day header drills into day view
      fireEvent.click(monthDayHeader);
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-10-27');

      // Return to month view
      fireEvent.click(monthBtn);

      // Drill down via +N more click
      const moreEl = container.querySelector('.cf-month-more') as HTMLElement;
      expect(moreEl).toBeTruthy();
      expect(moreEl.textContent).toContain('+1 more');
      expect(moreEl.getAttribute('role')).toBe('button');
      expect(moreEl.getAttribute('tabindex')).toBe('0');

      fireEvent.click(moreEl);
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-10-27');

      // Return to month view and test Space key on +N more
      fireEvent.click(monthBtn);
      const moreEl2 = container.querySelector('.cf-month-more') as HTMLElement;
      fireEvent.keyDown(moreEl2, { key: ' ' });
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
    });

    it('triggers loadDateView fetch when drilling into a day outside cached weekVM', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          active_view: 'day',
          week: {
            ...mockViewModel.week,
            range_start: '2026-11-15',
            range_end: '2026-11-15',
            days: [
              { date: '2026-11-15', day_of_month: 15, is_today: false, is_weekend: false, events: [] },
            ],
          },
        }),
      });
      vi.stubGlobal('fetch', fetchMock);

      const monthWithNovDay: CalendarGridViewModel = {
        ...mockViewModel,
        month: {
          ...mockViewModel.month!,
          days: [
            {
              date: '2026-11-15',
              day_of_month: 15,
              is_current_month: false,
              is_today: false,
              is_weekend: false,
            },
          ],
        },
      };

      setGridState({ viewModel: monthWithNovDay });
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      const headerNov = container.querySelector('.cf-month-day[data-date="2026-11-15"] .cf-month-day-header') as HTMLElement;
      expect(headerNov).toBeTruthy();

      await act(async () => {
        fireEvent.click(headerNov);
      });

      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('date=2026-11-15&view=day'),
        expect.any(Object)
      );
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
    });

    it('resets pagedWeekStart and aborts in-flight fetches when drilling into a baseline week day from paged month view', () => {
      let aborted = false;
      const fetchMock = vi.fn().mockImplementation((_url, opts) => {
        if (opts?.signal) {
          opts.signal.addEventListener('abort', () => {
            aborted = true;
          });
        }
        return new Promise(() => {}); // never resolves
      });
      vi.stubGlobal('fetch', fetchMock);

      setGridState();
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      // Switch to month view
      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      // Page forward 1 month (initiates fetch and sets pagedDate)
      const nextBtn = container.querySelector('.cf-nav-btn.cf-next-btn')!;
      fireEvent.click(nextBtn);

      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('1');
      expect(fetchMock).toHaveBeenCalled();

      // Drill down into 2026-10-27 (belongs to baseline entryViewModel.week.days)
      const dayHeader = container.querySelector('.cf-month-day[data-date="2026-10-27"] .cf-month-day-header') as HTMLElement;
      fireEvent.click(dayHeader);

      expect(aborted).toBe(true);
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('day');
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-current-date')).toBe('2026-10-27');

      // Switch back to week view
      const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;
      fireEvent.click(weekBtn);

      // Verify page-offset is 0 (at baseline, not offset from the paged month)
      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-page-offset')).toBe('0');
    });

    it('renders error banner when widget state is error and opens event modal from month view', () => {
      setGridState({ state: 'error' });
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
      expect(container.querySelector('.widget-error-banner')).not.toBeNull();

      // Switch to month view
      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      // Click month day in month view to open modal (exercises line 304)
      const dayCell = container.querySelector('.cf-month-day[data-date="2026-10-27"]') as HTMLElement;
      expect(dayCell).not.toBeNull();
      fireEvent.click(dayCell);

      expect(container.querySelector('.cf-modal-overlay')).not.toBeNull();
      expect(container.querySelector('.cf-modal-card')).not.toBeNull();
    });

    it('filters early_events, late_events, all_day, timed, and month weeks when selectedMember is active', () => {
      const vmWithAllDayAndWeeks = {
        ...mockViewModel,
        week: {
          ...mockViewModel.week!,
          days: [
            ...mockViewModel.week!.days!.slice(0, 2),
            {
              ...mockViewModel.week!.days![2],
              all_day: [{ id: 'ev-ad-1', title: 'Sam All Day', all_day: true, owners: ['Sam'] }],
              timed: [{ id: 'ev-tm-1', title: 'Sam Timed', all_day: false, owners: ['Sam'] }],
            },
            ...mockViewModel.week!.days!.slice(3),
          ],
        },
        month: {
          ...mockViewModel.month!,
          weeks: [
            {
              week_num: 44,
              days: mockViewModel.month!.days,
            },
          ],
        },
      } as unknown as CalendarGridViewModel;

      setGridState({ viewModel: vmWithAllDayAndWeeks });
      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);

      // Filter by Alex (so Sam's items get filtered out)
      const alexChip = container.querySelector('.cf-member-chip[data-member="Alex"]') as HTMLElement;
      expect(alexChip).not.toBeNull();
      fireEvent.click(alexChip);

      // Verify Sam's items are filtered
      expect(container.textContent).not.toContain('Project Demo');

      // Switch to month view and verify weeks and days are filtered
      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);

      expect(container.querySelector('.widget-calendar-grid')?.getAttribute('data-active-view')).toBe('month');
    });

    it('covers member text_color vars, default shared_color, range label formatters, and customTitle fallbacks', () => {
      const strippedVM = {
        week: {
          days: mockViewModel.week!.days!,
          members: [
            { name: 'Alex', color: '#111', text_color: '#fff' },
            { name: 'Sam' },
          ],
        },
        month: {
          days: [
            {
              date: '2026-10-25',
              day_of_month: 25,
              weekday_abbr: 'Sun',
              is_current_month: true,
              is_today: false,
              is_weekend: true,
            },
            {
              date: '2026-10-26',
              day_of_month: 26,
              weekday_abbr: 'Mon',
              is_current_month: true,
              is_today: true,
              is_weekend: false,
              dots: [{ name: 'Alex', color: '#111' }],
              events: [{ id: 'm1', title: 'Alex Event', all_day: true, owners: ['Alex'] }],
            },
          ],
        },
      } as unknown as CalendarGridViewModel;

      setGridState({
        viewModel: strippedVM,
        config: { title: '   ' },
      });

      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
      const grid = container.querySelector('.widget-calendar-grid') as HTMLElement;
      expect(grid.style.getPropertyValue('--mm-cf-shared')).toBe('#3D405B');
      expect(grid.style.getPropertyValue('--mm-member-0')).toBe('#111');
      expect(grid.style.getPropertyValue('--mm-member-0-text')).toBe('#fff');
      expect(container.textContent).toContain('Calendar');

      // Member filter with dots and events in month view
      const alexChip = container.querySelector('.cf-member-chip[data-member="Alex"]') as HTMLElement;
      fireEvent.click(alexChip);

      const monthBtn = container.querySelector('.cf-view-btn[data-view="month"]')!;
      fireEvent.click(monthBtn);
      expect(container.textContent).toContain('Alex Event');
    });

    it('covers default_view month, missing range_label fallbacks, and ISO date modal parsing', () => {
      // 1. default_view month
      setGridState({
        config: { default_view: 'month' },
        viewModel: {
          ...mockViewModel,
          month: {
            ...mockViewModel.month,
            month_label: '',
          },
          week: {
            ...mockViewModel.week,
            range_label: '',
          },
        } as unknown as CalendarGridViewModel,
      });

      const { container } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
      expect(container.querySelector('.cf-view-btn[data-view="month"]')?.classList.contains('active')).toBe(true);
      expect(container.querySelector('.cf-range-label.cf-label-month')?.textContent).toContain('October');

      // Switch to week view to verify week range_label fallback and click event
      const weekBtn = container.querySelector('.cf-view-btn[data-view="week"]')!;
      fireEvent.click(weekBtn);
      expect(container.querySelector('.cf-range-label.cf-label-week')?.textContent).toContain('Oct 25');

      const evEl = container.querySelector('[data-event-id="ev-timed-1"]')!;
      fireEvent.click(evEl);
      expect(container.querySelector('.cf-modal-card')).not.toBeNull();

      // 2. data.today and config.days with missing viewModel
      setGridState({
        viewModel: null,
        data: { today: '2026-10-29' },
        config: { days: 5 },
      });
      const { container: dataC } = render(<CalendarGridWidget id="cal-grid" type="calendar-grid" />);
      expect(dataC.querySelector('.widget-calendar-grid')).not.toBeNull();
    });
  });
});
