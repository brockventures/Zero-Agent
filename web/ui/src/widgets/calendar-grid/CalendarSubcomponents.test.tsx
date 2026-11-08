import { act, cleanup, fireEvent, render, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../../store';
import { CalendarEventModal } from './CalendarEventModal';
import { CalendarHeader } from './CalendarHeader';
import { CalendarMonthView } from './CalendarMonthView';
import { batchFitDayColumnEvents, CalendarWeekView } from './CalendarWeekView';
import { formatISODate as toISODate } from './dateUtils';
import { useCalendarRangeData } from './useCalendarRangeData';
import { useInactivityReset } from './useInactivityReset';
import type {
  FamilyDay,
  FamilyEvent,
  FamilyMember,
  FamilyMonthDay,
  FamilyMonthViewModel,
  FamilySpanEvent,
  FamilyViewModel,
  ViewMode,
} from './types';

const members: FamilyMember[] = [
  { name: 'Alex', initial: 'A', color: '#ff6f59', text_color: '#ffffff', pattern: 'solid' },
  { name: 'Sam', initial: 'S', color: '#8b60ed', text_color: '#ffffff', pattern: 'dots' },
];

describe('CalendarEventModal', () => {
  afterEach(() => {
    cleanup();
  });

  it('returns null when event is null', () => {
    const { container } = render(
      <CalendarEventModal event={null} dateLabel="Today" onClose={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('closes on Escape key press and cleans up event listener', () => {
    const onClose = vi.fn();
    const event: FamilyEvent = { id: 'ev-1', title: 'Planning Meeting' };
    const { unmount } = render(
      <CalendarEventModal event={event} dateLabel="Oct 27" onClose={onClose} />
    );

    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);

    unmount();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on overlay backdrop click but stops propagation on card click', () => {
    const onClose = vi.fn();
    const event: FamilyEvent = { id: 'ev-1', title: 'Coffee' };
    const props = { event, dateLabel: 'Oct 27', onClose };
    const { container, rerender } = render(<CalendarEventModal {...props} />);

    const overlay = container.querySelector('.cf-modal-overlay')!;
    const card = container.querySelector('.cf-modal-card')!;
    const closeBtn = container.querySelector('.cf-modal-close')!;

    // Exercise compiler memo hit by changing dateLabel while keeping event identical
    rerender(<CalendarEventModal {...props} dateLabel="Oct 28" />);
    expect(container.querySelector('.cf-modal-card')).not.toBeNull();

    fireEvent.click(card);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(closeBtn);
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('renders Mealie hero image when location starts with /hearth-api/mealie/image/', () => {
    const mealieEvent: FamilyEvent = {
      id: 'mealie-1',
      title: '🍽️ Pasta Carbonara',
      location: '/hearth-api/mealie/image/carbonara.jpg',
    };
    const { container } = render(
      <CalendarEventModal event={mealieEvent} dateLabel="Oct 27" onClose={vi.fn()} />
    );

    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img?.getAttribute('src')).toBe('/hearth-api/mealie/image/carbonara.jpg');
    expect(container.querySelector('.cf-modal-location')).toBeNull();
  });

  it('renders standard location icon for normal locations', () => {
    const event: FamilyEvent = {
      id: 'loc-1',
      title: 'Doctor Appointment',
      location: 'Medical Center Suite 400',
    };
    const { container } = render(
      <CalendarEventModal event={event} dateLabel="Oct 27" onClose={vi.fn()} />
    );

    const locEl = container.querySelector('.cf-modal-location');
    expect(locEl).not.toBeNull();
    expect(locEl?.textContent).toContain('Medical Center Suite 400');
    expect(container.querySelector('img')).toBeNull();
  });

  it('renders owner chips with initials, colors, and pattern classes', () => {
    const event: FamilyEvent = {
      id: 'chip-1',
      title: 'Family Dinner',
      owners: ['Alex', 'Sam'],
      initials: ['A', 'S'],
      colors: ['#ff6f59', '#8b60ed'],
      patterns: ['solid', 'striped'],
    };
    const { container } = render(
      <CalendarEventModal event={event} dateLabel="Oct 27" onClose={vi.fn()} />
    );

    const chips = container.querySelectorAll('.cf-modal-owner-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].classList.contains('cf-pattern-solid')).toBe(true);
    expect(chips[1].classList.contains('cf-pattern-striped')).toBe(true);

    const badges = container.querySelectorAll('.cf-modal-owner-badge');
    expect(badges[0].textContent).toBe('A');
    expect(badges[1].textContent).toBe('S');
  });

  it('handles Mealie emoji description stripping and preserves long text without truncation', () => {
    const longDesc = '🍽️ '.repeat(5) + 'A'.repeat(300);
    const mealieEvent: FamilyEvent = {
      id: 'm-desc',
      title: '🍽️ Stew',
      location: '/hearth-api/mealie/image/stew.jpg',
      description: longDesc,
    };
    const { container } = render(
      <CalendarEventModal event={mealieEvent} dateLabel="Oct 27" onClose={vi.fn()} />
    );

    const descEl = container.querySelector('.cf-modal-description');
    expect(descEl).not.toBeNull();
    expect(descEl?.textContent).not.toContain('🍽️');
    expect(descEl?.textContent?.endsWith('...')).toBe(false);
    expect(descEl?.textContent?.length).toBeGreaterThan(280);
  });

  it('truncates standard event description exceeding 280 chars to 277 chars + ellipsis', () => {
    const longDesc = 'B'.repeat(320);
    const event: FamilyEvent = {
      id: 'norm-desc',
      title: 'Conference',
      description: longDesc,
    };
    const { container } = render(
      <CalendarEventModal event={event} dateLabel="Oct 27" onClose={vi.fn()} />
    );

    const descEl = container.querySelector('.cf-modal-description');
    expect(descEl).not.toBeNull();
    expect(descEl?.textContent?.endsWith('...')).toBe(true);
    expect(descEl?.textContent?.length).toBe(280);
  });
});

describe('CalendarHeader', () => {
  afterEach(() => {
    cleanup();
  });

  it('switches views and updates aria-selected attributes', () => {
    const onViewSwitch = vi.fn();
    const props = {
      customTitle: 'Family Calendar',
      activeView: 'week' as const,
      pageOffset: 0,
      rangeLabelWeek: 'Oct 25 – 31, 2026',
      rangeLabelMonth: 'October 2026',
      rangeLabelDay: 'Tuesday, Oct 27',
      syncMessage: null,
      members,
      onPrev: vi.fn(),
      onNext: vi.fn(),
      onResetDate: vi.fn(),
      onViewSwitch,
    };
    const { container, rerender } = render(<CalendarHeader {...props} />);

    const weekBtn = container.querySelector('button[data-view="week"]')!;
    const monthBtn = container.querySelector('button[data-view="month"]')!;
    expect(weekBtn.getAttribute('aria-selected')).toBe('true');
    expect(monthBtn.getAttribute('aria-selected')).toBe('false');

    // Exercise React compiler memoization hit by changing one prop while keeping rest identical
    rerender(<CalendarHeader {...props} customTitle="Updated Family Calendar" />);
    expect(weekBtn.getAttribute('aria-selected')).toBe('true');

    fireEvent.click(monthBtn);
    expect(onViewSwitch).toHaveBeenCalledWith('month');

    fireEvent.click(weekBtn);
    expect(onViewSwitch).toHaveBeenCalledWith('week');
  });

  it('renders member filter chips and handles clicks with active/dimmed styles', () => {
    const onSelectMember = vi.fn();
    const { container } = render(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage={null}
        members={members}
        selectedMember="Alex"
        onSelectMember={onSelectMember}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={vi.fn()}
        onViewSwitch={vi.fn()}
      />
    );

    const alexChip = container.querySelector('button[data-member="Alex"]')!;
    const samChip = container.querySelector('button[data-member="Sam"]')!;

    expect(alexChip.classList.contains('active')).toBe(true);
    expect(samChip.classList.contains('dimmed')).toBe(true);

    fireEvent.click(samChip);
    expect(onSelectMember).toHaveBeenCalledWith('Sam');

    // Default pattern fallback and missing onSelectMember handler
    const anonMember = { initial: 'X', color: '#123456' } as unknown as FamilyMember;
    const { container: anonContainer } = render(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="day"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage={null}
        members={[anonMember]}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={vi.fn()}
        onViewSwitch={vi.fn()}
      />
    );
    const anonChip = anonContainer.querySelector('.cf-member-chip') as HTMLElement;
    expect(anonChip.classList.contains('cf-pattern-solid')).toBe(true);
    // Clicking should not throw when onSelectMember is undefined
    fireEvent.click(anonChip);

    // Verify day view label is visible and week/month hidden
    const dayLabel = anonContainer.querySelector('.cf-label-day') as HTMLElement;
    const weekLabel = anonContainer.querySelector('.cf-label-week') as HTMLElement;
    expect(dayLabel.style.display).toBe('');
    expect(weekLabel.style.display).toBe('none');
  });

  it('toggles TODAY reset badge visibility according to pageOffset', () => {
    const onResetDate = vi.fn();
    const { container, rerender } = render(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage={null}
        members={members}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={onResetDate}
        onViewSwitch={vi.fn()}
      />
    );

    const todayBtn = container.querySelector('.cf-today-badge') as HTMLElement;
    expect(todayBtn.style.display).toBe('none');

    rerender(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={1}
        rangeLabelWeek="Nov 1 – 7, 2026"
        rangeLabelMonth="November 2026"
        rangeLabelDay="Tuesday, Nov 3"
        syncMessage={null}
        members={members}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={onResetDate}
        onViewSwitch={vi.fn()}
      />
    );

    expect(todayBtn.style.display).toBe('');
    fireEvent.click(todayBtn);
    expect(onResetDate).toHaveBeenCalledTimes(1);
  });

  it('displays syncMessage indicator when present with accessible role', () => {
    const { container, rerender } = render(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage={null}
        members={members}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={vi.fn()}
        onViewSwitch={vi.fn()}
      />
    );

    expect(container.querySelector('.cf-sync-indicator')).toBeNull();

    rerender(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage="Sync failed"
        members={members}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onResetDate={vi.fn()}
        onViewSwitch={vi.fn()}
      />
    );

    const syncEl = container.querySelector('.cf-sync-indicator');
    expect(syncEl).not.toBeNull();
    expect(syncEl?.getAttribute('role')).toBe('status');
    expect(syncEl?.getAttribute('aria-live')).toBe('polite');
    expect(syncEl?.textContent).toBe('Sync failed');
  });

  it('triggers onPrev and onNext callbacks on nav button clicks', () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const { container } = render(
      <CalendarHeader
        customTitle="Family Calendar"
        activeView="week"
        pageOffset={0}
        rangeLabelWeek="Oct 25 – 31, 2026"
        rangeLabelMonth="October 2026"
        rangeLabelDay="Tuesday, Oct 27"
        syncMessage={null}
        members={members}
        onPrev={onPrev}
        onNext={onNext}
        onResetDate={vi.fn()}
        onViewSwitch={vi.fn()}
      />
    );

    fireEvent.click(container.querySelector('.cf-prev-btn')!);
    expect(onPrev).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('.cf-next-btn')!);
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  it('renders day view header with rangeLabelDay and handles empty members and undefined onSelectMember', () => {
    const props = {
      customTitle: 'Family Calendar',
      activeView: 'day' as const,
      pageOffset: 0,
      rangeLabelWeek: 'Oct 25 – 31, 2026',
      rangeLabelMonth: 'October 2026',
      rangeLabelDay: 'Tuesday, Oct 27',
      syncMessage: null,
      members: [],
      onPrev: vi.fn(),
      onNext: vi.fn(),
      onResetDate: vi.fn(),
      onViewSwitch: vi.fn(),
    };
    const { container, rerender } = render(<CalendarHeader {...props} />);

    const dayLabel = container.querySelector('.cf-label-day') as HTMLElement;
    expect(dayLabel.style.display).toBe('');
    expect(dayLabel.textContent).toBe('Tuesday, Oct 27');

    const weekLabel = container.querySelector('.cf-label-week') as HTMLElement;
    expect(weekLabel.style.display).toBe('none');

    const monthLabel = container.querySelector('.cf-label-month') as HTMLElement;
    expect(monthLabel.style.display).toBe('none');

    expect(container.querySelector('.cf-legend')).toBeNull();

    // Exercise compiler memo hit
    rerender(<CalendarHeader {...props} />);
    expect(container.querySelector('.cf-label-day')?.textContent).toBe('Tuesday, Oct 27');
  });

  it('renders month view header with rangeLabelMonth visible', () => {
    const props = {
      customTitle: 'Family Calendar',
      activeView: 'month' as const,
      pageOffset: 0,
      rangeLabelWeek: 'Oct 25 – 31, 2026',
      rangeLabelMonth: 'October 2026',
      rangeLabelDay: 'Tuesday, Oct 27',
      syncMessage: null,
      members: [],
      onPrev: vi.fn(),
      onNext: vi.fn(),
      onResetDate: vi.fn(),
      onViewSwitch: vi.fn(),
    };
    const { container, rerender } = render(<CalendarHeader {...props} />);

    const monthLabel = container.querySelector('.cf-label-month') as HTMLElement;
    expect(monthLabel.style.display).toBe('');
    expect(monthLabel.textContent).toBe('October 2026');

    const weekLabel = container.querySelector('.cf-label-week') as HTMLElement;
    expect(weekLabel.style.display).toBe('none');

    const dayLabel = container.querySelector('.cf-label-day') as HTMLElement;
    expect(dayLabel.style.display).toBe('none');

    // Exercise compiler memo hit
    rerender(<CalendarHeader {...props} />);
    expect(container.querySelector('.cf-label-month')?.textContent).toBe('October 2026');
  });
});

describe('CalendarMonthView & CalendarWeekView', () => {
  afterEach(() => {
    cleanup();
  });

  describe('batchFitDayColumnEvents', () => {
    it('returns early when column element is invalid or empty', () => {
      expect(() => batchFitDayColumnEvents(null as unknown as HTMLElement)).not.toThrow();
      const div = document.createElement('div');
      expect(() => batchFitDayColumnEvents(div)).not.toThrow();
    });

    it('toggles cf-event-compact based on height and width overflow', () => {
      const column = document.createElement('div');
      const ev1 = document.createElement('div');
      ev1.className = 'cf-day-event';
      const time1 = document.createElement('div');
      time1.className = 'cf-event-time';
      const title1 = document.createElement('div');
      title1.className = 'cf-event-title';
      ev1.appendChild(time1);
      ev1.appendChild(title1);
      column.appendChild(ev1);

      // Height overflow
      Object.defineProperty(ev1, 'clientHeight', { value: 30, configurable: true });
      Object.defineProperty(time1, 'offsetHeight', { value: 20, configurable: true });
      Object.defineProperty(title1, 'offsetHeight', { value: 20, configurable: true });
      Object.defineProperty(ev1, 'clientWidth', { value: 100, configurable: true });
      Object.defineProperty(time1, 'offsetWidth', { value: 30, configurable: true });
      Object.defineProperty(title1, 'scrollWidth', { value: 30, configurable: true });

      batchFitDayColumnEvents(column);
      expect(ev1.classList.contains('cf-event-compact')).toBe(true);

      // Fits vertically and horizontally
      Object.defineProperty(ev1, 'clientHeight', { value: 80, configurable: true });
      batchFitDayColumnEvents(column);
      expect(ev1.classList.contains('cf-event-compact')).toBe(false);

      // Width overflow
      Object.defineProperty(time1, 'offsetTop', { value: 10, configurable: true });
      Object.defineProperty(title1, 'offsetTop', { value: 10, configurable: true });
      Object.defineProperty(ev1, 'clientWidth', { value: 50, configurable: true });
      Object.defineProperty(time1, 'offsetWidth', { value: 30, configurable: true });
      Object.defineProperty(title1, 'scrollWidth', { value: 30, configurable: true });

      batchFitDayColumnEvents(column);
      expect(ev1.classList.contains('cf-event-compact')).toBe(true);

      // scrollHeight overflow when headerH is 0: without .cf-event-location -> compacts
      Object.defineProperty(ev1, 'clientWidth', { value: 200, configurable: true });
      Object.defineProperty(time1, 'offsetWidth', { value: 10, configurable: true });
      Object.defineProperty(title1, 'scrollWidth', { value: 10, configurable: true });
      Object.defineProperty(time1, 'offsetHeight', { value: 0, configurable: true });
      Object.defineProperty(title1, 'offsetHeight', { value: 0, configurable: true });
      Object.defineProperty(ev1, 'clientHeight', { value: 30, configurable: true });
      Object.defineProperty(ev1, 'scrollHeight', { value: 60, configurable: true });
      batchFitDayColumnEvents(column);
      expect(ev1.classList.contains('cf-event-compact')).toBe(true);

      // With .cf-event-location present -> does not compact
      const loc = document.createElement('div');
      loc.className = 'cf-event-location';
      ev1.appendChild(loc);
      batchFitDayColumnEvents(column);
      expect(ev1.classList.contains('cf-event-compact')).toBe(false);
    });
  });

  describe('CalendarMonthView', () => {
    it('renders empty state when no month data or members', () => {
      const { container } = render(
        <CalendarMonthView
          activeView="month"
          monthVM={undefined}
          members={[]}
          onOpenModal={vi.fn()}
        />
      );
      expect(container.textContent).toContain('No calendar data yet');
    });

    it('handles day header click, enter and space keydown to select day', () => {
      const onSelectDay = vi.fn();
      const monthVM: FamilyMonthViewModel = {
        month_label: 'October 2026',
        month_name: 'October',
        year: 2026,
        month: 10,
        range_start: '2026-10-01',
        range_end: '2026-10-31',
        today: '2026-10-27',
        weekday_headers: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        days: [
          {
            date: '2026-10-27',
            day_of_month: 27,
            is_current_month: true,
            is_today: true,
            is_weekend: false,
            events: [
              { id: '1', title: 'Ev 1' },
              { id: '2', title: 'Ev 2' },
              { id: '3', title: 'Ev 3' },
            ],
            event_title: 'Ev 1',
          },
        ],
      };

      const props = {
        activeView: 'month' as const,
        monthVM,
        members,
        onOpenModal: vi.fn(),
        onSelectDay,
      };
      const { container, rerender } = render(<CalendarMonthView {...props} />);

      const header = container.querySelector('.cf-month-day-header')!;
      fireEvent.click(header);
      expect(onSelectDay).toHaveBeenCalledWith('2026-10-27');

      // Exercise compiler memo hit by changing activeView while keeping monthVM identical
      rerender(<CalendarMonthView {...props} activeView="day" />);
      expect(container.querySelector('.cf-month-day-header')).not.toBeNull();

      fireEvent.keyDown(header, { key: 'Enter' });
      expect(onSelectDay).toHaveBeenCalledTimes(2);

      fireEvent.keyDown(header, { key: ' ' });
      expect(onSelectDay).toHaveBeenCalledTimes(3);

      const moreBtn = container.querySelector('.cf-month-more')!;
      fireEvent.click(moreBtn);
      expect(onSelectDay).toHaveBeenCalledTimes(4);

      fireEvent.keyDown(moreBtn, { key: 'Enter' });
      expect(onSelectDay).toHaveBeenCalledTimes(5);

      fireEvent.keyDown(moreBtn, { key: ' ' });
      expect(onSelectDay).toHaveBeenCalledTimes(6);
    });

    it('triggers onOpenModal on event title click and day card click', () => {
      const onOpenModal = vi.fn();
      const ev = { id: 'm-1', title: 'Dinner Meeting' };
      const monthVM: FamilyMonthViewModel = {
        month_label: 'October 2026',
        month_name: 'October',
        year: 2026,
        month: 10,
        range_start: '2026-10-01',
        range_end: '2026-10-31',
        today: '2026-10-27',
        weekday_headers: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        days: [
          {
            date: '2026-10-27',
            day_of_month: 27,
            is_current_month: true,
            is_today: true,
            is_weekend: false,
            events: [ev],
            event_title: 'Dinner Meeting',
          },
        ],
      };

      const { container } = render(
        <CalendarMonthView
          activeView="month"
          monthVM={monthVM}
          members={members}
          onOpenModal={onOpenModal}
        />
      );

      const eventTitle = container.querySelector('.cf-month-event-title')!;
      fireEvent.click(eventTitle);
      expect(onOpenModal).toHaveBeenCalledWith(ev, '2026-10-27');

      const dayCard = container.querySelector('.cf-month-day')!;
      fireEvent.click(dayCard);
      expect(onOpenModal).toHaveBeenCalledTimes(2);
    });

    it('triggers onOpenModal when day has event_title but no events array', () => {
      const onOpenModal = vi.fn();
      const monthVM: FamilyMonthViewModel = {
        month_label: 'October 2026',
        month_name: 'October',
        year: 2026,
        month: 10,
        range_start: '2026-10-01',
        range_end: '2026-10-31',
        today: '2026-10-27',
        weekday_headers: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        days: [
          {
            date: '2026-10-27',
            day_of_month: 27,
            is_current_month: true,
            is_today: true,
            is_weekend: false,
            event_title: 'Only Title Event',
          },
        ],
      };

      const { container } = render(
        <CalendarMonthView
          activeView="month"
          monthVM={monthVM}
          members={members}
          onOpenModal={onOpenModal}
        />
      );

      const titleEl = container.querySelector('.cf-month-event-title')!;
      fireEvent.click(titleEl);
      expect(onOpenModal).toHaveBeenCalledWith(
        { id: '2026-10-27', title: 'Only Title Event' },
        '2026-10-27'
      );

      const dayCard = container.querySelector('.cf-month-day')!;
      fireEvent.click(dayCard);
      expect(onOpenModal).toHaveBeenCalledTimes(2);
    });

    it('renders month dots with pattern, member color, and shared dot styling', () => {
      const monthVM: FamilyMonthViewModel = {
        month_label: 'October 2026',
        month_name: 'October',
        year: 2026,
        month: 10,
        range_start: '2026-10-01',
        range_end: '2026-10-31',
        today: '2026-10-27',
        weekday_headers: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        days: [
          {
            date: '2026-10-27',
            day_of_month: 27,
            is_current_month: true,
            is_today: false,
            is_weekend: false,
            dots: [
              { name: 'Alex', color: '#ff6f59', member_index: 0, pattern: 'solid' },
              { name: 'Shared Event', is_shared: true },
            ] as unknown as NonNullable<FamilyMonthDay['dots']>,
          },
        ],
      };

      const { container } = render(
        <CalendarMonthView
          activeView="month"
          monthVM={monthVM}
          members={members}
          onOpenModal={vi.fn()}
        />
      );

      const dots = container.querySelectorAll('.cf-month-dot');
      expect(dots.length).toBe(2);
      expect(dots[0].classList.contains('cf-pattern-solid')).toBe(true);
      expect(dots[0].classList.contains('cf-member-0')).toBe(true);
      expect(dots[1].classList.contains('cf-dot-shared')).toBe(true);
    });
  });

  describe('CalendarWeekView', () => {
    it('renders empty state when daysToRender or members is empty', () => {
      const { container } = render(
        <CalendarWeekView
          activeView="week"
          daysToRender={[]}
          visibleAllDayEvents={[]}
          members={[]}
          currentDateISO="2026-10-27"
          onOpenModal={vi.fn()}
        />
      );
      expect(container.textContent).toContain('No calendar data yet');
    });

    it('renders Mealie all-day card with image and icon vs normal all-day span event', () => {
      const onOpenModal = vi.fn();
      const allDayMealie: FamilySpanEvent = {
        id: 'mealie-span-1',
        title: '🍽️ Tacos Dinner',
        start_col: 1,
        col_span: 1,
        location: '/hearth-api/mealie/image/tacos.jpg',
        color: '#ff6f59',
        text_color: '#ffffff',
        pattern: 'stripes',
      };
      const allDayNormal: FamilySpanEvent = {
        id: 'normal-span-1',
        title: 'All Day Workshop',
        start_col: 2,
        col_span: 1,
        color: '#8b60ed',
        text_color: '#ffffff',
        pattern: 'dots',
      };
      const daysToRender: FamilyDay[] = [
        { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false, events: [] },
        { date: '2026-10-28', day_of_month: 28, is_today: false, is_weekend: false, events: [] },
      ];

      const { container } = render(
        <CalendarWeekView
          activeView="week"
          daysToRender={daysToRender}
          visibleAllDayEvents={[allDayMealie, allDayNormal]}
          members={members}
          currentDateISO="2026-10-27"
          onOpenModal={onOpenModal}
        />
      );

      const mealieImg = container.querySelector('img[src="/hearth-api/mealie/image/tacos.jpg"]');
      expect(mealieImg).not.toBeNull();
      expect(container.querySelector('.cf-mealie-icon')).not.toBeNull();
      expect(container.querySelector('.cf-pattern-stripes')).not.toBeNull();
      expect(container.querySelector('.cf-pattern-dots')).not.toBeNull();

      const spanEvents = container.querySelectorAll('.cf-event.cf-allday');
      expect(spanEvents.length).toBe(2);
      fireEvent.click(spanEvents[0]);
      expect(onOpenModal).toHaveBeenCalled();
      fireEvent.keyDown(spanEvents[0], { key: 'Enter' });
      expect(onOpenModal).toHaveBeenCalledTimes(2);
      fireEvent.keyDown(spanEvents[0], { key: ' ' });
      expect(onOpenModal).toHaveBeenCalledTimes(3);
      fireEvent.keyDown(spanEvents[0], { key: 'Tab' });
    });

    it('renders early and late edge events and handles onOpenModal', () => {
      const onOpenModal = vi.fn();
      const onSelectDay = vi.fn();
      const daysToRender: FamilyDay[] = [
        {
          date: '2026-10-27',
          day_of_month: 27,
          is_today: true,
          is_weekend: false,
          early_events: [
            {
              id: 'early-1',
              title: 'Morning Yoga',
              start: '06:00',
              top_pct: 0,
              height_pct: 0,
              left_pct: 0,
              width_pct: 100,
              color: '#8b60ed',
              text_color: '#ffffff',
              pattern: 'dots',
              location: 'Studio A',
            },
          ],
          late_events: [
            {
              id: 'late-1',
              title: 'Night Run',
              start: '22:30',
              top_pct: 0,
              height_pct: 0,
              left_pct: 0,
              width_pct: 100,
              color: '#ff6f59',
              text_color: '#ffffff',
              pattern: 'dots',
              location: 'Track',
            },
          ],
          events: [
            {
              id: 'timed-1',
              title: 'Midday Sync',
              start: '12:00',
              end: '13:00',
              top_pct: 40,
              height_pct: 15,
              left_pct: 0,
              width_pct: 100,
              pattern: 'dots',
            },
          ],
        },
      ];

      const weekProps = {
        activeView: 'week' as const,
        daysToRender,
        visibleAllDayEvents: [],
        members,
        currentDateISO: '2026-10-27',
        onOpenModal,
        onSelectDay,
      };
      const { container, rerender } = render(<CalendarWeekView {...weekProps} />);

      const earlyEv = container.querySelector('.cf-early-band .cf-edge-event')!;
      expect(earlyEv.textContent).toContain('06:00 Morning Yoga');
      fireEvent.click(earlyEv);
      expect(onOpenModal).toHaveBeenCalledWith(expect.objectContaining({ id: 'early-1' }), '2026-10-27');
      fireEvent.keyDown(earlyEv, { key: 'Enter' });
      fireEvent.keyDown(earlyEv, { key: ' ' });

      // Exercise compiler memo hit by changing currentDateISO while keeping daysToRender identical
      rerender(<CalendarWeekView {...weekProps} currentDateISO="2026-10-28" />);
      expect(container.querySelector('.cf-early-band .cf-edge-event')).not.toBeNull();

      const lateEv = container.querySelector('.cf-late-band .cf-edge-event')!;
      expect(lateEv.textContent).toContain('22:30 Night Run');

      // Test day header Enter and click in week view
      const dayHeader = container.querySelector('.cf-day-header') as HTMLElement;
      expect(dayHeader).not.toBeNull();
      fireEvent.click(dayHeader);
      expect(onSelectDay).toHaveBeenCalledWith('2026-10-27');

      fireEvent.keyDown(dayHeader, { key: 'Enter' });
      expect(onSelectDay).toHaveBeenCalledTimes(2);
      fireEvent.click(lateEv);
      expect(onOpenModal).toHaveBeenCalledWith(expect.objectContaining({ id: 'late-1' }), '2026-10-27');
      fireEvent.keyDown(lateEv, { key: 'Enter' });
      fireEvent.keyDown(lateEv, { key: ' ' });
    });

    it('renders timeline_markers when present and falls back to hour_markers', () => {
      const daysToRender: FamilyDay[] = [
        { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false, events: [] },
      ];

      const weekVMTimeline: FamilyViewModel = {
        range_start: '2026-10-25',
        range_end: '2026-10-31',
        range_label: 'Oct 25 – 31',
        today: '2026-10-27',
        timeline_markers: [
          { label: '09:00', top_pct: 10, is_start: true },
          { label: '17:00', top_pct: 90, is_end: true },
        ],
      };

      const { container, rerender } = render(
        <CalendarWeekView
          activeView="week"
          weekVM={weekVMTimeline}
          daysToRender={daysToRender}
          visibleAllDayEvents={[]}
          members={members}
          currentDateISO="2026-10-27"
          onOpenModal={vi.fn()}
        />
      );

      let markers = container.querySelectorAll('.cf-hour-marker');
      expect(markers.length).toBe(2);
      expect(markers[0].textContent).toBe('09:00');

      const weekVMHourMarkers: FamilyViewModel = {
        range_start: '2026-10-25',
        range_end: '2026-10-31',
        range_label: 'Oct 25 – 31',
        today: '2026-10-27',
        hour_markers: ['08:00', '12:00', '16:00'],
      };

      rerender(
        <CalendarWeekView
          activeView="week"
          weekVM={weekVMHourMarkers}
          daysToRender={daysToRender}
          visibleAllDayEvents={[]}
          members={members}
          currentDateISO="2026-10-27"
          onOpenModal={vi.fn()}
        />
      );

      markers = container.querySelectorAll('.cf-hour-marker');
      expect(markers.length).toBe(3);
      expect(markers[0].textContent).toBe('08:00');
    });

    it('triggers onSelectDay on day header click and keyboard navigation in week view', () => {
      const onSelectDay = vi.fn();
      const daysToRender: FamilyDay[] = [
        { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false, events: [] },
      ];

      const { container } = render(
        <CalendarWeekView
          activeView="week"
          daysToRender={daysToRender}
          visibleAllDayEvents={[]}
          members={members}
          currentDateISO="2026-10-27"
          onOpenModal={vi.fn()}
          onSelectDay={onSelectDay}
        />
      );

      const header = container.querySelector('.cf-day-header')!;
      fireEvent.click(header);
      expect(onSelectDay).toHaveBeenCalledWith('2026-10-27');

      fireEvent.keyDown(header, { key: 'Enter' });
      expect(onSelectDay).toHaveBeenCalledTimes(2);

      fireEvent.keyDown(header, { key: ' ' });
      expect(onSelectDay).toHaveBeenCalledTimes(3);
    });

    it('renders column all_day events when visibleAllDayEvents is empty', () => {
      const onOpenModal = vi.fn();
      const daysToRender: FamilyDay[] = [
        {
          date: '2026-10-27',
          day_of_month: 27,
          is_today: true,
          is_weekend: false,
          all_day: [
            {
              id: 'col-allday-1',
              title: '🍽️ Tacos Lunch',
              location: '/hearth-api/mealie/image/tacos.jpg',
              color: '#8b60ed',
              text_color: '#ffffff',
            },
            {
              id: 'col-allday-2',
              title: 'Standard All Day',
              color: '#ff6f59',
              text_color: '#ffffff',
            },
          ],
        },
      ];

      const { container } = render(
        <CalendarWeekView
          activeView="week"
          daysToRender={daysToRender}
          visibleAllDayEvents={[]}
          members={members}
          currentDateISO="2026-10-27"
          onOpenModal={onOpenModal}
        />
      );

      const items = container.querySelectorAll('.cf-day-allday-col .cf-event.cf-allday');
      expect(items.length).toBe(2);
      fireEvent.click(items[0]);
      expect(onOpenModal).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'col-allday-1' }),
        '2026-10-27'
      );
      fireEvent.keyDown(items[0], { key: 'Enter' });
      fireEvent.keyDown(items[0], { key: ' ' });
    });

    it('attaches ResizeObserver to day canvas elements and disconnects on unmount', () => {
      const observe = vi.fn();
      const disconnect = vi.fn();
      const originalRO = window.ResizeObserver;
      window.ResizeObserver = class {
        observe = observe;
        disconnect = disconnect;
        unobserve = vi.fn();
      } as unknown as typeof ResizeObserver;

      try {
        const daysToRender: FamilyDay[] = [
          { date: '2026-10-27', day_of_month: 27, is_today: true, is_weekend: false },
        ];
        const { unmount } = render(
          <CalendarWeekView
            activeView="week"
            daysToRender={daysToRender}
            visibleAllDayEvents={[]}
            members={members}
            currentDateISO="2026-10-27"
            onOpenModal={vi.fn()}
          />
        );

        expect(observe).toHaveBeenCalled();
        unmount();
        expect(disconnect).toHaveBeenCalled();
      } finally {
        window.ResizeObserver = originalRO;
      }
    });
  });
});

describe('useCalendarRangeData & useInactivityReset', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  describe('useCalendarRangeData', () => {
    const baseDate = new Date(2026, 9, 27, 10, 0, 0);
    const baseWeekStart = new Date(2026, 9, 25, 0, 0, 0);

    it('forwards ?node query parameter and respects LRU cache eviction of 20 entries', async () => {
      const fetchMock = vi.fn().mockImplementation((_url: string) => {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              view_model: {
                active_view: 'week',
                week: { range_start: '2026-10-25', days: [] },
              },
            }),
            { status: 200 }
          )
        );
      });
      vi.stubGlobal('fetch', fetchMock);

      // Set window.location.search
      const origLocation = window.location;
      delete (window as unknown as { location: unknown }).location;
      (window as unknown as { location: unknown }).location = { ...origLocation, search: '?node=kiosk-1' };

      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
          cachedRangeStartConfig: '2026-01-01',
          cachedRangeEndConfig: '2026-12-31',
        })
      );

      // Load 21 distinct days to verify LRU eviction
      for (let i = 1; i <= 21; i++) {
        const d = new Date(baseDate.getTime() + i * 86400000);
        await act(async () => {
          await result.current.loadDateView(d, 'day', () => {});
        });
      }

      expect(fetchMock).toHaveBeenCalled();
      const lastCallUrl = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0];
      expect(lastCallUrl).toContain('&node=kiosk-1');

      // Reloading the 21st date should hit LRU cache without a new fetch
      const callCountBefore = fetchMock.mock.calls.length;
      const date21 = new Date(baseDate.getTime() + 21 * 86400000);
      await act(async () => {
        await result.current.loadDateView(date21, 'day', () => {});
      });
      expect(fetchMock.mock.calls.length).toBe(callCountBefore);

      (window as unknown as { location: unknown }).location = origLocation;
    });

    it('rolls back state and displays "Sync failed" on fetch error', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockImplementation(() =>
          Promise.resolve(new Response('Server Error', { status: 500 }))
        )
      );

      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const rollback = vi.fn();
      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
        })
      );

      await act(async () => {
        await result.current.loadDateView(new Date('2026-11-03'), 'week', rollback);
      });

      expect(rollback).toHaveBeenCalled();
      expect(result.current.syncMessage).toBe('Sync failed');

      // Advances timer past 2500ms to clear message
      act(() => {
        vi.advanceTimersByTime(2600);
      });
      expect(result.current.syncMessage).toBeNull();
    });

    it('enforces cached range boundary and displays "Not synced yet"', () => {
      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
          cachedRangeStartConfig: '2026-10-25',
          cachedRangeEndConfig: '2026-10-31',
        })
      );

      act(() => {
        result.current.pageNext();
      });
      expect(result.current.syncMessage).toBe('Not synced yet');
    });

    it('selectDayView switches to day view and selects date within current week without remote fetch', async () => {
      const fetchMock = vi.fn();
      vi.stubGlobal('fetch', fetchMock);

      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const entryViewModel = {
        week: {
          range_start: '2026-10-25',
          days: [{ date: '2026-10-27', day_of_month: 27 }],
        },
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
          entryViewModel,
        })
      );

      act(() => {
        result.current.selectDayView('2026-10-27');
      });

      expect(activeView).toBe('day');
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it('selectDayView falls back to entryViewModel week if not in currentViewModel, and fetches if neither', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          day: { days: [{ date: '2026-11-15' }] },
        }),
      });
      vi.stubGlobal('fetch', fetchMock);

      const baseDate = new Date(2026, 9, 27);
      const baseWeekStart = new Date(2026, 9, 25);
      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const entryViewModel = {
        week: {
          days: [{ date: '2026-10-28', events: [] }],
          range_start: '2026-10-25',
          range_end: '2026-10-31',
        },
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
          entryViewModel,
        })
      );

      // In entryViewModel days -> aborts and restores entryViewModel without fetch
      act(() => {
        result.current.selectDayView('2026-10-28');
      });
      expect(activeView).toBe('day');
      expect(fetchMock).not.toHaveBeenCalled();

      // Outside both -> fetches target day
      await act(async () => {
        result.current.selectDayView('2026-11-15');
      });
      expect(fetchMock).toHaveBeenCalled();
    });

    it('handles resetToBaseline without entryViewModel by calling loadDateView', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          week: { days: [{ date: '2026-10-27' }] },
        }),
      });
      vi.stubGlobal('fetch', fetchMock);

      const baseDate = new Date(2026, 9, 27);
      const baseWeekStart = new Date(2026, 9, 25);
      let activeView: ViewMode = 'month';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
        })
      );

      await act(async () => {
        result.current.resetToBaseline('week');
      });

      expect(activeView).toBe('week');
      expect(fetchMock).toHaveBeenCalled();
    });

    it('handles pagePrev and handleViewSwitch across day, week, and month', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          week: { days: [{ date: '2026-10-18' }] },
          day: { days: [{ date: '2026-10-26' }] },
          month: { days: [{ date: '2026-09-01' }] },
        }),
      });
      vi.stubGlobal('fetch', fetchMock);

      const baseDate = new Date(2026, 9, 27);
      const baseWeekStart = new Date(2026, 9, 25);
      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate,
          baseWeekStart,
          weekStep: 7,
          activeView,
          setActiveView,
        })
      );

      // Page prev in week view
      await act(async () => {
        result.current.pagePrev();
      });
      expect(fetchMock).toHaveBeenCalled();

      // Switch to day view and page prev
      act(() => {
        result.current.handleViewSwitch('day');
      });
      expect(activeView).toBe('day');

      await act(async () => {
        result.current.pagePrev();
      });

      // Switch to month view and page prev
      act(() => {
        result.current.handleViewSwitch('month');
      });
      expect(activeView).toBe('month');

      await act(async () => {
        result.current.pagePrev();
      });

      // Reset date
      act(() => {
        result.current.resetDate();
      });
      expect(result.current.pagedDate).toBeNull();
      expect(result.current.pagedWeekStart).toBeNull();
    });

    it('enforces month and day boundaries with Not synced yet and handles rollback on selectDayView fetch failure', async () => {
      let activeView: ViewMode = 'month';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const { result } = renderHook(() =>
        useCalendarRangeData({
          id: 'test-cal',
          baseDate: new Date('2026-10-27'),
          baseWeekStart: new Date('2026-10-25'),
          weekStep: 7,
          activeView,
          setActiveView,
          cachedRangeStartConfig: '2026-10-01',
          cachedRangeEndConfig: '2026-10-31',
        })
      );

      // In month view, paging next goes to November which is > cached.end (2026-10-31)
      act(() => {
        result.current.pageNext();
      });
      expect(result.current.syncMessage).toBe('Not synced yet');

      // Paging prev goes to September which is < cached.start (2026-10-01)
      act(() => {
        result.current.pagePrev();
      });
      expect(result.current.syncMessage).toBe('Not synced yet');

      // Now switch to day view
      act(() => {
        result.current.handleViewSwitch('day');
      });
      expect(activeView).toBe('day');

      // Failing fetch on selectDayView rolls back
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));
      await act(async () => {
        result.current.selectDayView('2026-11-20');
      });
      expect(result.current.syncMessage).toBe('Sync failed');
    });

    it('handles selectDayView within currentWeek and entryWeek without remote fetch', async () => {
      let activeView: ViewMode = 'week';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const baseDate = new Date(2026, 9, 27);
      const baseWeekStart = new Date(2026, 9, 25);

      const entryVM: Record<string, unknown> = {
        days: [],
        range_start: '2026-10-25',
        range_end: '2026-10-31',
        week: {
          range_start: '2026-10-25',
          range_end: '2026-10-31',
          days: [
            { date: '2026-10-25', is_today: false, is_weekend: true, events: [] },
            { date: '2026-10-26', is_today: false, is_weekend: false, events: [] },
          ],
        },
      };

      const fetchSpy = vi.fn();
      vi.stubGlobal('fetch', fetchSpy);

      const { result, rerender } = renderHook(
        (props: { activeView: ViewMode }) =>
          useCalendarRangeData({
            id: 'test-cal',
            baseDate,
            baseWeekStart,
            weekStep: 7,
            activeView: props.activeView,
            setActiveView,
            entryViewModel: entryVM,
          }),
        { initialProps: { activeView: 'week' } }
      );

      // Select date within entry week: no fetch needed
      act(() => {
        result.current.selectDayView('2026-10-26');
      });
      rerender({ activeView });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(result.current.pagedWeekStart).toBeNull();
      expect(toISODate(result.current.pagedDate!)).toBe('2026-10-26');
      expect(activeView).toBe('day');

      // Now set currentViewModel to a different week with non-base range_start
      act(() => {
        result.current.setCurrentViewModel({
          week: {
            range_start: '2026-11-01',
            range_end: '2026-11-07',
            days: [
              { date: '2026-11-02', is_today: false, is_weekend: false, is_entry_week: false, events: [] },
            ],
          },
        });
      });

      // Select day in currentWeekVM (2026-11-02): syncs pagedWeekStart to 2026-11-01 without fetch
      act(() => {
        result.current.selectDayView('2026-11-02');
      });
      rerender({ activeView });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(toISODate(result.current.pagedWeekStart!)).toBe('2026-11-01');

      // Now select date from entryWeekVM (2026-10-25): restores entryViewModel and sets pagedWeekStart to null
      act(() => {
        result.current.selectDayView('2026-10-25');
      });
      rerender({ activeView });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(result.current.pagedWeekStart).toBeNull();
      expect(result.current.currentViewModel).toBe(entryVM);
    });

    it('steps forward and then backward back to baseline in day, week, and month views', async () => {
      let activeView: ViewMode = 'day';
      const setActiveView: React.Dispatch<React.SetStateAction<ViewMode>> = (action) => {
        activeView = typeof action === 'function' ? action(activeView) : action;
      };

      const baseDate = new Date(2026, 9, 27);
      const baseWeekStart = new Date(2026, 9, 25);

      const entryVM: Record<string, unknown> = {
        days: [],
        range_start: '2026-09-01',
        range_end: '2026-11-30',
        cached_range_start: '2026-09-01',
        cached_range_end: '2026-11-30',
      };

      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          view_model: {
            days: [],
            range_start: '2026-09-01',
            range_end: '2026-11-30',
          },
        }),
      });
      vi.stubGlobal('fetch', mockFetch);

      const { result, rerender } = renderHook(
        (props: { activeView: ViewMode }) =>
          useCalendarRangeData({
            id: 'test-cal',
            baseDate,
            baseWeekStart,
            weekStep: 7,
            activeView: props.activeView,
            setActiveView,
            entryViewModel: entryVM,
            cachedRangeStartConfig: '2026-09-01',
            cachedRangeEndConfig: '2026-11-30',
          }),
        { initialProps: { activeView: 'day' } }
      );

      // 1. Day view: stepNext then stepPrev back to baseline
      await act(async () => {
        result.current.pageNext();
      });
      rerender({ activeView });
      expect(result.current.isAtBaseDate).toBe(false);

      act(() => {
        result.current.pagePrev();
      });
      rerender({ activeView });
      expect(result.current.isAtBaseDate).toBe(true);

      // 2. Week view: stepNext then stepPrev back to baseline
      act(() => {
        result.current.handleViewSwitch('week');
      });
      rerender({ activeView: 'week' });
      expect(activeView).toBe('week');

      await act(async () => {
        result.current.pageNext();
      });
      rerender({ activeView: 'week' });
      expect(result.current.isAtBaseDate).toBe(false);

      act(() => {
        result.current.pagePrev();
      });
      rerender({ activeView: 'week' });
      expect(result.current.isAtBaseDate).toBe(true);

      // 3. Month view: stepNext then stepPrev back to baseline
      act(() => {
        result.current.handleViewSwitch('month');
      });
      rerender({ activeView: 'month' });
      expect(activeView).toBe('month');

      await act(async () => {
        result.current.pageNext();
      });
      rerender({ activeView: 'month' });
      expect(result.current.isAtBaseDate).toBe(false);

      act(() => {
        result.current.pagePrev();
      });
      rerender({ activeView: 'month' });
      expect(result.current.isAtBaseDate).toBe(true);
    });
  });

  describe('useInactivityReset', () => {
    it('manages 60s inactivity lifecycle: pauses store, fires reset on timeout, and resumes store', () => {
      const onReset = vi.fn();
      const pauseSpy = vi.spyOn(useDisplayStore.getState(), 'pause');
      const resumeSpy = vi.spyOn(useDisplayStore.getState(), 'resume');

      const { rerender, unmount } = renderHook(
        ({ isInteractive }: { isInteractive: boolean }) =>
          useInactivityReset({ isInteractive, onReset }),
        { initialProps: { isInteractive: false } }
      );

      expect(pauseSpy).not.toHaveBeenCalled();

      // Becoming interactive pauses store
      rerender({ isInteractive: true });
      expect(pauseSpy).toHaveBeenCalledTimes(1);
      expect(resumeSpy).not.toHaveBeenCalled();

      // 30s elapsed -> no reset yet
      act(() => {
        vi.advanceTimersByTime(30000);
      });
      expect(onReset).not.toHaveBeenCalled();

      // Another 30s elapsed (60s total) -> onReset called and store resumed
      act(() => {
        vi.advanceTimersByTime(30000);
      });
      expect(onReset).toHaveBeenCalledTimes(1);
      expect(resumeSpy).toHaveBeenCalledTimes(1);

      unmount();
      pauseSpy.mockRestore();
      resumeSpy.mockRestore();
    });

    it('resets inactivity timer on user activity', () => {
      const onReset = vi.fn();
      const { result } = renderHook(() =>
        useInactivityReset({ isInteractive: true, onReset })
      );

      act(() => {
        vi.advanceTimersByTime(40000);
      });
      expect(onReset).not.toHaveBeenCalled();

      // User activity resets timer
      act(() => {
        result.current.handleUserActivity();
      });

      act(() => {
        vi.advanceTimersByTime(40000);
      });
      // 80s total elapsed, but activity occurred at 40s, so 40s into new cycle: no reset yet
      expect(onReset).not.toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(20000);
      });
      expect(onReset).toHaveBeenCalledTimes(1);
    });
  });
});
