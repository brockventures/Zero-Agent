import './CalendarGridWidget.css';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import { CalendarEventModal } from './CalendarEventModal';
import { CalendarHeader } from './CalendarHeader';
import { CalendarMonthView } from './CalendarMonthView';
import { CalendarWeekView } from './CalendarWeekView';
import {
  addDays,
  computeDayLayout,
  formatDayLabel,
  formatModalDateRange,
  formatMonthLabel,
  formatRangeLabel as formatWeekLabel,
  matchesMemberFilter,
  parseISODate,
  sliceWeekWindow,
  formatISODate as toISODate,
} from './dateUtils';
import type {
  FamilyDay,
  FamilyEvent,
  FamilyMember,
  FamilyMonthDay,
  FamilyMonthViewModel,
  FamilyViewModel,
  ViewMode,
} from './types';
import { useCalendarRangeData } from './useCalendarRangeData';
import { useInactivityReset } from './useInactivityReset';

export type { CalendarGridViewModel } from './types';
export { batchFitDayColumnEvents } from './CalendarWeekView';

function resolveDefaultView(
  config?: Record<string, unknown>,
  entryViewModel?: Record<string, unknown>,
): ViewMode {
  const rawDefault = (
    typeof config?.default_view === 'string'
      ? config.default_view
      : typeof entryViewModel?.active_view === 'string'
      ? entryViewModel.active_view
      : 'week'
  ).toLowerCase();
  return rawDefault === 'month' ? 'month' : rawDefault === 'day' ? 'day' : 'week';
}

function resolveCalendarBaselines(
  entryViewModel?: Record<string, unknown>,
  data?: Record<string, unknown>,
  config?: Record<string, unknown>,
) {
  const vmToday = entryViewModel?.week as Record<string, unknown> | undefined;
  const vmMonth = entryViewModel?.month as Record<string, unknown> | undefined;
  const todayStr =
    typeof vmToday?.today === 'string'
      ? vmToday.today
      : typeof vmMonth?.today === 'string'
      ? vmMonth.today
      : typeof data?.today === 'string'
      ? data.today
      : toISODate(new Date());

  const baseDate = parseISODate(todayStr);

  const vmWeek = entryViewModel?.week as Record<string, unknown> | undefined;
  const baseWeekStart =
    typeof vmWeek?.range_start === 'string' ? parseISODate(vmWeek.range_start) : baseDate;

  const baseDays = (
    entryViewModel?.week as FamilyViewModel | undefined
  )?.days;
  const cfgDays = Number(config?.days);
  const weekStep =
    Array.isArray(baseDays) && baseDays.length > 0
      ? baseDays.length
      : Number.isFinite(cfgDays) && cfgDays > 0
      ? Math.round(cfgDays)
      : 7;

  return { baseDate, baseWeekStart, weekStep };
}

function computeMemberCssVars(
  weekVM?: FamilyViewModel,
  monthVM?: FamilyMonthViewModel,
): CSSProperties {
  const vars: Record<string, string> = {};
  const sharedColor = weekVM?.shared_color || monthVM?.shared_color || '#3D405B';
  vars['--mm-cf-shared'] = sharedColor;
  const members = weekVM?.members || monthVM?.members || [];
  members.forEach((m, i) => {
    if (m.color) vars[`--mm-member-${i}`] = m.color;
    if (m.text_color) vars[`--mm-member-${i}-text`] = m.text_color;
  });
  return vars as CSSProperties;
}

function resolveRangeLabels(
  weekVM: FamilyViewModel | undefined,
  monthVM: FamilyMonthViewModel | undefined,
  currentWeekStart: Date | null,
  currentDate: Date,
) {
  let rangeLabelWeek = weekVM?.range_label || '';
  if (!rangeLabelWeek && currentWeekStart) {
    const daysLen = weekVM?.days?.length || 7;
    rangeLabelWeek = formatWeekLabel(currentWeekStart, addDays(currentWeekStart, daysLen - 1));
  }

  let rangeLabelMonth = monthVM?.month_label || '';
  if (!rangeLabelMonth && currentDate) {
    rangeLabelMonth = formatMonthLabel(currentDate);
  }

  const rangeLabelDay = formatDayLabel(currentDate);
  return { rangeLabelWeek, rangeLabelMonth, rangeLabelDay };
}

function filterDaysByMember(
  rawDays: FamilyDay[],
  selectedMember: string | null,
  membersList: FamilyMember[],
): FamilyDay[] {
  if (!selectedMember) return rawDays;
  return rawDays.map((day) => ({
    ...day,
    events: day.events?.filter((ev) => matchesMemberFilter(ev, selectedMember, membersList)),
    early_events: day.early_events?.filter((ev) =>
      matchesMemberFilter(ev, selectedMember, membersList),
    ),
    late_events: day.late_events?.filter((ev) =>
      matchesMemberFilter(ev, selectedMember, membersList),
    ),
    all_day: day.all_day?.filter((ev) => matchesMemberFilter(ev, selectedMember, membersList)),
    timed: day.timed?.filter((ev) => matchesMemberFilter(ev, selectedMember, membersList)),
  }));
}

function filterMonthByMember(
  monthVM: FamilyMonthViewModel | undefined,
  selectedMember: string | null,
  membersList: FamilyMember[],
): FamilyMonthViewModel | undefined {
  if (!monthVM || !selectedMember) return monthVM;
  const filterDay = (day: FamilyMonthDay): FamilyMonthDay => {
    const filteredEvents = day.events
      ? day.events.filter((ev) => matchesMemberFilter(ev, selectedMember, membersList))
      : [];
    const filteredDots = day.dots
      ? day.dots.filter((dot) => dot.name === selectedMember)
      : undefined;
    return {
      ...day,
      dots: filteredDots,
      events: filteredEvents,
      events_count: filteredEvents.length,
      event_title: filteredEvents[0]?.title,
    };
  };

  return {
    ...monthVM,
    days: monthVM.days ? monthVM.days.map(filterDay) : undefined,
    weeks: monthVM.weeks
      ? monthVM.weeks.map((w) => ({
          ...w,
          days: w.days.map(filterDay),
        }))
      : undefined,
  };
}

function resolveModalDateLabel(ev: FamilyEvent, dateStr?: string, endDateStr?: string): string {
  const resolvedStart =
    dateStr ||
    (ev.start && /^\d{4}-\d{2}-\d{2}/.test(ev.start) ? ev.start.slice(0, 10) : undefined);
  const resolvedEnd =
    endDateStr || (ev.end && /^\d{4}-\d{2}-\d{2}/.test(ev.end) ? ev.end.slice(0, 10) : undefined);
  return formatModalDateRange(resolvedStart, resolvedEnd);
}

/**
 * Native React calendar grid and family planner widget.
 * Ports widgets/calendar-grid/views/widget.html and web/static/js/calendar_family.js.
 */
export function CalendarGridWidget({
  id,
}: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const data = entry?.data as Record<string, unknown> | undefined;
  const isError = entry?.state === 'error';
  const entryVM = entry?.viewModel as Record<string, unknown> | undefined;

  const defaultView = resolveDefaultView(config, entryVM);

  const [activeView, setActiveView] = useState<ViewMode>(defaultView);
  const [selectedMember, setSelectedMember] = useState<string | null>(null);
  const [activeModalEvent, setActiveModalEvent] = useState<FamilyEvent | null>(null);
  const [activeModalDateLabel, setActiveModalDateLabel] = useState<string>('');

  const { baseDate, baseWeekStart, weekStep } = resolveCalendarBaselines(entryVM, data, config);

  // Ref for user activity hook so range data callbacks can notify inactivity timer
  const activityCallbackRef = useRef<() => void>(() => {});

  const {
    currentViewModel,
    currentDate,
    currentWeekStart,
    pageOffset,
    syncMessage,
    pageNext,
    pagePrev,
    resetDate,
    handleViewSwitch,
    resetToBaseline,
    selectDayView,
  } = useCalendarRangeData({
    id,
    baseDate,
    baseWeekStart,
    weekStep,
    activeView,
    setActiveView,
    entryTimestamp: entry?.timestamp,
    entryViewModel: entry?.viewModel,
    cachedRangeStartConfig: config?.cached_range_start as string | undefined,
    cachedRangeEndConfig: config?.cached_range_end as string | undefined,
    dataCachedRangeStart: data?.cached_range_start as string | undefined,
    dataCachedRangeEnd: data?.cached_range_end as string | undefined,
    onActivity: () => activityCallbackRef.current(),
  });

  const handleInactivityReset = () => {
    setActiveModalEvent(null);
    setActiveModalDateLabel('');
    setSelectedMember(null);
    resetToBaseline(defaultView);
  };

  const isInteractive =
    Boolean(activeModalEvent) ||
    activeView !== defaultView ||
    pageOffset !== 0 ||
    Boolean(selectedMember);

  const { handleUserActivity } = useInactivityReset({
    isInteractive,
    onReset: handleInactivityReset,
  });

  useEffect(() => {
    activityCallbackRef.current = handleUserActivity;
  }, [handleUserActivity]);

  const handleSelectMember = (name: string) => {
    setSelectedMember((prev) => (prev === name ? null : name));
    handleUserActivity();
  };

  const openModal = (ev: FamilyEvent, dateStr?: string, endDateStr?: string) => {
    setActiveModalEvent(ev);
    setActiveModalDateLabel(resolveModalDateLabel(ev, dateStr, endDateStr));
    handleUserActivity();
  };

  const closeModal = () => {
    setActiveModalEvent(null);
    setActiveModalDateLabel('');
    handleUserActivity();
  };

  const weekVM = currentViewModel?.week as FamilyViewModel | undefined;
  const monthVM = currentViewModel?.month as FamilyMonthViewModel | undefined;
  const members = weekVM?.members || monthVM?.members || [];

  const memberCssVars = computeMemberCssVars(weekVM, monthVM);
  const { rangeLabelWeek, rangeLabelMonth, rangeLabelDay } = resolveRangeLabels(
    weekVM,
    monthVM,
    currentWeekStart,
    currentDate,
  );
  const currentDateISO = toISODate(currentDate);

  const rawDaysToRender: FamilyDay[] = sliceWeekWindow(weekVM?.days, activeView, currentDateISO);
  const daysToRender = filterDaysByMember(rawDaysToRender, selectedMember, members);

  const rawVisibleAllDayEvents = computeDayLayout(
    weekVM?.all_day_events,
    weekVM?.days,
    activeView,
    currentDateISO,
  );
  const visibleAllDayEvents = selectedMember
    ? rawVisibleAllDayEvents.filter((ev) => matchesMemberFilter(ev, selectedMember, members))
    : rawVisibleAllDayEvents;

  const filteredMonthVM = filterMonthByMember(monthVM, selectedMember, members);

  const customTitle =
    typeof config?.title === 'string' && config.title.trim() !== ''
      ? config.title.trim()
      : 'Calendar';

  return (
    <div
      className={`widget-calendar-grid widget-calendar-family cf-view-${activeView}`}
      data-widget-id={id}
      data-widget-type="calendar-grid"
      data-default-view={defaultView}
      data-active-view={activeView}
      data-current-date={currentDateISO}
      data-page-offset={String(pageOffset)}
      style={memberCssVars}
      onPointerDown={handleUserActivity}
    >
      {isError && (
        <div className="widget-error-banner">
          <span className="error-icon">⚠</span>
          <span>Widget Error</span>
        </div>
      )}

      <CalendarHeader
        customTitle={customTitle}
        activeView={activeView}
        pageOffset={pageOffset}
        rangeLabelWeek={rangeLabelWeek}
        rangeLabelMonth={rangeLabelMonth}
        rangeLabelDay={rangeLabelDay}
        syncMessage={syncMessage}
        members={members}
        selectedMember={selectedMember}
        onSelectMember={handleSelectMember}
        onPrev={pagePrev}
        onNext={pageNext}
        onResetDate={resetDate}
        onViewSwitch={handleViewSwitch}
      />

      <CalendarWeekView
        activeView={activeView}
        weekVM={weekVM}
        daysToRender={daysToRender}
        visibleAllDayEvents={visibleAllDayEvents}
        members={members}
        currentDateISO={currentDateISO}
        onOpenModal={openModal}
        onSelectDay={selectDayView}
      />

      <CalendarMonthView
        activeView={activeView}
        monthVM={filteredMonthVM}
        members={members}
        onOpenModal={(ev, date) => openModal(ev, date)}
        onSelectDay={selectDayView}
      />

      <CalendarEventModal
        event={activeModalEvent}
        dateLabel={activeModalDateLabel}
        onClose={closeModal}
      />
    </div>
  );
}
