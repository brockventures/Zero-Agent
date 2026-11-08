import { useEffect, useRef, useState } from 'react';
import { useIsCurrentPane } from '../../carousel/PaneContext';
import { useDocumentVisible } from '../../lifecycle/visibility';
import { useDisplayStore } from '../../store';
import {
  CalendarAllDayBand,
  CalendarDayEventCard,
  CalendarEdgeEventsBand,
  CalendarTimelineAxis,
  CalendarWeekMealRow,
  detectDaySpecialStatus,
} from './CalendarWeekBands';
import { computeNowIndicatorPct, resolveConfiguredSpecialDayLabel } from './dateUtils';
import type {
  CollapsedHoursGap,
  FamilyDay,
  FamilyEvent,
  FamilyMember,
  FamilySpanEvent,
  FamilyViewModel,
  ViewMode,
} from './types';

function getElementDimensions(
  evEl: HTMLElement,
  timeEl: HTMLElement,
  titleEl: HTMLElement
): { headerH: number; titleH: number; timeW: number; titleW: number } {
  const headerEl = evEl.querySelector<HTMLElement>('.cf-event-header');
  const headerH =
    headerEl && typeof headerEl.offsetHeight === 'number' && headerEl.offsetHeight > 0
      ? headerEl.offsetHeight
      : typeof timeEl.offsetHeight === 'number'
      ? timeEl.offsetHeight
      : 0;
  const titleH = typeof titleEl.offsetHeight === 'number' ? titleEl.offsetHeight : 0;
  const timeW = typeof timeEl.offsetWidth === 'number' ? timeEl.offsetWidth : 0;
  const titleW =
    typeof titleEl.scrollWidth === 'number'
      ? titleEl.scrollWidth
      : typeof titleEl.offsetWidth === 'number'
      ? titleEl.offsetWidth
      : 0;
  return { headerH, titleH, timeW, titleW };
}

function measureDayEventFit(evEl: HTMLElement): boolean {
  const timeEl = evEl.querySelector<HTMLElement>('.cf-event-time');
  const titleEl = evEl.querySelector<HTMLElement>('.cf-event-title');
  if (!timeEl || !titleEl) return true;

  const { headerH, titleH, timeW, titleW } = getElementDimensions(evEl, timeEl, titleEl);

  if (typeof evEl.clientHeight === 'number' && evEl.clientHeight > 0) {
    if (headerH > 0 && titleH > 0) {
      if (headerH + titleH + 2 > evEl.clientHeight) return false;
    } else if (typeof evEl.scrollHeight === 'number' && evEl.scrollHeight > evEl.clientHeight + 1) {
      if (!evEl.querySelector('.cf-event-location')) return false;
    }
  }

  if (typeof evEl.clientWidth === 'number' && evEl.clientWidth > 0) {
    if (timeW > 0 && titleW > 0 && timeEl.offsetTop === titleEl.offsetTop) {
      if (timeW + titleW + 8 > evEl.clientWidth) return false;
    }
  }

  return true;
}

/**
 * Two-phase ResizeObserver helper for fitting day column events without layout thrashing.
 * Phase 1 reads all geometries into a plain array; Phase 2 toggles .cf-event-compact.
 */
export function batchFitDayColumnEvents(columnEl: HTMLElement): void {
  if (!columnEl || typeof columnEl.querySelectorAll !== 'function') return;
  const dayEvents = columnEl.querySelectorAll<HTMLElement>('.cf-day-event');
  if (dayEvents.length === 0) return;

  for (let i = 0; i < dayEvents.length; i++) dayEvents[i].classList.remove('cf-event-compact');

  const measurements: Array<{ el: HTMLElement; fits: boolean }> = [];
  for (let i = 0; i < dayEvents.length; i++) {
    const evEl = dayEvents[i];
    if (!evEl.querySelector('.cf-event-time') || !evEl.querySelector('.cf-event-title')) continue;
    measurements.push({ el: evEl, fits: measureDayEventFit(evEl) });
  }

  for (let i = 0; i < measurements.length; i++) {
    const { el, fits } = measurements[i];
    if (fits) el.classList.remove('cf-event-compact');
    else el.classList.add('cf-event-compact');
  }
}

function inferTimeFormat(weekVM?: FamilyViewModel): string | undefined {
  if (weekVM?.time_format) return weekVM.time_format;
  if (
    weekVM?.timeline_markers?.some((m) => /[ap]m$/i.test(m.label)) ||
    weekVM?.hour_markers?.some((m) => /[ap]m$/i.test(m))
  ) {
    return '12h';
  }
  return undefined;
}

export interface CalendarWeekViewProps {
  activeView: ViewMode;
  weekVM?: FamilyViewModel;
  daysToRender: FamilyDay[];
  visibleAllDayEvents: FamilySpanEvent[];
  members: FamilyMember[];
  currentDateISO: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
  onSelectDay?: (date: string) => void;
  celebrateSpecialDays?: boolean;
  eventFlair?: boolean;
  birthdays?: unknown;
  specialDays?: unknown;
}

export function CalendarWeekView({
  activeView,
  weekVM,
  daysToRender,
  visibleAllDayEvents,
  members,
  currentDateISO,
  onOpenModal,
  onSelectDay,
  celebrateSpecialDays = true,
  eventFlair = true,
  birthdays,
  specialDays,
}: CalendarWeekViewProps) {
  const tz = useDisplayStore((s) => s.header.timezone);
  const isDocVisible = useDocumentVisible();
  const isPaneActive = useIsCurrentPane();
  const [now, setNow] = useState(() => new Date());

  const hasToday = daysToRender.some((d) => d.is_today);
  const isTimerActive = activeView !== 'month' && isDocVisible && isPaneActive && hasToday;
  const [prevTimerActive, setPrevTimerActive] = useState(isTimerActive);
  if (isTimerActive !== prevTimerActive) {
    setPrevTimerActive(isTimerActive);
    if (isTimerActive) {
      setNow(new Date());
    }
  }

  useEffect(() => {
    if (!isTimerActive) return;
    const timer = setInterval(() => {
      setNow(new Date());
    }, 60_000);
    return () => clearInterval(timer);
  }, [isTimerActive]);

  const nowPct = computeNowIndicatorPct(weekVM, now, tz || undefined);

  const canvasRefs = useRef<Map<string, HTMLElement>>(new Map());
  const registerDayCanvas = (date: string, el: HTMLElement | null) => {
    if (el) {
      canvasRefs.current.set(date, el);
      batchFitDayColumnEvents(el);
    } else {
      canvasRefs.current.delete(date);
    }
  };

  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const observers: ResizeObserver[] = [];
    canvasRefs.current.forEach((canvasEl) => {
      const ro = new ResizeObserver(() => batchFitDayColumnEvents(canvasEl));
      ro.observe(canvasEl);
      observers.push(ro);
    });
    return () => observers.forEach((ro) => ro.disconnect());
  }, [weekVM, activeView, daysToRender]);

  const hasTimelineMarkers = Boolean(
    weekVM?.timeline_markers?.length || weekVM?.hour_markers?.length
  );
  const marginLeft = hasTimelineMarkers ? '44px' : undefined;
  const inferredTimeFormat = inferTimeFormat(weekVM);
  const collapsedGaps: CollapsedHoursGap[] = weekVM?.collapsed_gaps ?? [];

  return (
    <div
      className="cf-view-panel cf-view-week"
      style={{ display: activeView !== 'month' ? 'flex' : 'none', flex: 1, minHeight: 0, flexDirection: 'column' }}
    >
      {daysToRender.length > 0 && members.length > 0 ? (
        <>
          <CalendarAllDayBand
            activeView={activeView} weekVM={weekVM} daysToRender={daysToRender}
            visibleAllDayEvents={visibleAllDayEvents} currentDateISO={currentDateISO}
            marginLeft={marginLeft} onOpenModal={onOpenModal} eventFlair={eventFlair}
          />
          <CalendarEdgeEventsBand
            edge="early" daysToRender={daysToRender} inferredTimeFormat={inferredTimeFormat}
            marginLeft={marginLeft} onOpenModal={onOpenModal}
          />
          <div className="cf-week-body" style={{ display: 'flex', flex: 1, minHeight: 0, gap: '6px', position: 'relative' }}>
            {hasTimelineMarkers ? <CalendarTimelineAxis weekVM={weekVM} /> : null}
            <div
              className="cf-week-grid"
              style={{ display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`, gap: '6px', flex: 1, minHeight: 0 }}
            >
              {daysToRender.map((day) => {
                const dayAllEvents: FamilyEvent[] = [
                  ...(day.all_day || []),
                  ...(day.events || []),
                  ...(day.early_events || []),
                  ...(day.late_events || []),
                  ...(day.meals || []),
                  ...(visibleAllDayEvents?.filter((s) => {
                    if (s.start && s.end && day.date) {
                      const sStart = s.start.slice(0, 10);
                      const sEnd = s.end.slice(0, 10);
                      return day.date >= sStart && day.date <= sEnd;
                    }
                    return false;
                  }) || []),
                ];
                const configuredBdayLabel =
                  celebrateSpecialDays !== false
                    ? resolveConfiguredSpecialDayLabel(day.date, birthdays, specialDays)
                    : null;
                const eventSpecialStatus =
                  celebrateSpecialDays !== false
                    ? detectDaySpecialStatus(dayAllEvents, members)
                    : null;
                const specialStatus = configuredBdayLabel ? 'birthday' : eventSpecialStatus;
                const bdayPillText = day.special_label
                  ? `🎂 ${day.special_label}`
                  : configuredBdayLabel || '🎂 Family Bday';
                const specialClass = specialStatus === 'birthday' ? ' cf-special-birthday' : specialStatus === 'holiday' ? ' cf-special-holiday' : '';
                const dayBg = day.is_today ? 'var(--mm-cal-today-bg, var(--mm-lavender-light, #f3effe))'
                  : day.is_weekend ? 'var(--mm-cal-weekend-bg, rgba(90, 60, 30, 0.02))'
                  : 'var(--mm-cal-day-bg, rgba(90, 60, 30, 0.03))';
                const dayBorder = day.is_today
                  ? '1px solid var(--mm-cal-today-border, var(--mm-lavender, #8b60ed))'
                  : '1px solid var(--mm-card-border, rgba(90, 60, 30, 0.08))';
                return (
                  <div
                    key={day.date}
                    className={`cf-day${day.is_today ? ' cf-today' : ''}${day.is_weekend ? ' cf-weekend' : ''}${specialClass}`}
                    data-date={day.date}
                    data-special-day={specialStatus || undefined}
                    style={{
                      display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0,
                      borderRadius: '14px', padding: '6px', background: dayBg, border: dayBorder,
                      overflow: 'hidden', position: 'relative',
                    }}
                  >
                    <div
                      className="cf-day-header"
                      role={activeView === 'week' ? 'button' : undefined}
                      tabIndex={activeView === 'week' ? 0 : undefined}
                      onClick={() => { if (activeView === 'week' && day.date) onSelectDay?.(day.date); }}
                      onKeyDown={(e) => {
                        if (activeView === 'week' && (e.key === 'Enter' || e.key === ' ')) {
                          e.preventDefault();
                          if (day.date) onSelectDay?.(day.date);
                        }
                      }}
                      style={{
                        minHeight: '44px', height: 'auto', boxSizing: 'border-box', textAlign: 'center', paddingBottom: '6px',
                        borderBottom: day.is_today
                          ? '1px solid var(--mm-cal-today-header-border, rgba(139, 96, 237, 0.25))'
                          : '1px solid var(--mm-divider, rgba(90, 60, 30, 0.07))',
                        flexShrink: 0, background: 'transparent', cursor: activeView === 'week' ? 'pointer' : undefined,
                      }}
                    >
                      <div
                        className="cf-day-weekday"
                        style={{
                          fontSize: '0.68rem', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.04em',
                          color: day.is_today ? 'var(--mm-cal-today-text, var(--mm-lavender, #8b60ed))' : 'var(--mm-text-dim, #8b819f)',
                        }}
                      >
                        {day.is_today ? 'Today' : day.weekday_abbr || ''}
                      </div>
                      <div
                        className="cf-day-num"
                        style={{
                          fontSize: '1.15rem', fontWeight: 900, lineHeight: 1.1,
                          color: day.is_today ? 'var(--mm-cal-today-text, var(--mm-lavender, #8b60ed))' : 'var(--mm-text-title, #2a2338)',
                        }}
                      >
                        {day.day_of_month}
                      </div>
                      {specialStatus === 'birthday' && (
                        <div className="cf-special-pill cf-pill-birthday">{bdayPillText}</div>
                      )}
                      {specialStatus === 'holiday' && (
                        <div className="cf-special-pill cf-pill-holiday">✨ Holiday</div>
                      )}
                    </div>

                    <div
                      className="cf-day-canvas"
                      ref={(el) => registerDayCanvas(day.date, el)}
                      style={{ flex: 1, position: 'relative', minHeight: 0, marginTop: '6px' }}
                    >
                      {collapsedGaps.map((gap, gapIdx) => (
                        <div
                          key={`${gap.start_hour}-${gap.end_hour}-${gapIdx}`}
                          className="cf-collapsed-gap" aria-hidden="true"
                          data-gap-start={gap.start_str || `${String(gap.start_hour).padStart(2, '0')}:00`}
                          data-gap-end={gap.end_str || `${String(gap.end_hour).padStart(2, '0')}:00`}
                          style={{ position: 'absolute', left: 0, right: 0, top: `${gap.top_pct}%`, height: `${gap.height_pct}%` }}
                        >
                          <span className="cf-collapsed-gap-label">{gap.label}</span>
                        </div>
                      ))}
                      {day.is_today && nowPct !== null && (
                        <div
                          className="cf-now-indicator" aria-hidden="true" data-now-pct={nowPct.toFixed(1)}
                          style={{
                            position: 'absolute', left: 0, right: 0, top: `${nowPct.toFixed(2)}%`,
                            height: '2px', background: 'var(--mm-coral, #ff6f59)', zIndex: 15, pointerEvents: 'none',
                          }}
                        >
                          <span
                            className="cf-now-dot"
                            style={{
                              position: 'absolute', left: '-3px', top: '-3px',
                              width: '8px', height: '8px', borderRadius: '50%', background: 'var(--mm-coral, #ff6f59)',
                            }}
                          />
                        </div>
                      )}
                      {day.events?.map((ev) => (
                        <CalendarDayEventCard
                          key={ev.id} ev={ev} dayDate={day.date}
                          inferredTimeFormat={inferredTimeFormat} onOpenModal={onOpenModal}
                          eventFlair={eventFlair}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <CalendarEdgeEventsBand
            edge="late" daysToRender={daysToRender} inferredTimeFormat={inferredTimeFormat}
            marginLeft={marginLeft} onOpenModal={onOpenModal}
          />
          <CalendarWeekMealRow
            daysToRender={daysToRender}
            marginLeft={marginLeft}
            onOpenModal={onOpenModal}
          />
        </>
      ) : (
        <div
          className="cf-empty"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1, color: 'var(--mm-text-dim)', fontSize: '0.9rem' }}
        >
          <span>No calendar data yet</span>
        </div>
      )}
    </div>
  );
}
