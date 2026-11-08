import { useEffect, useRef, useState } from 'react';
import { useIsCurrentPane } from '../../carousel/PaneContext';
import { useDocumentVisible } from '../../lifecycle/visibility';
import { useDisplayStore } from '../../store';
import {
  computeNowIndicatorPct,
  formatEventTime,
  parseTimeMinutes,
  resolveCollapsedMiddayGaps,
  stripMealiePrefix,
} from './dateUtils';
import type {
  CollapsedHoursGap,
  FamilyDay,
  FamilyEvent,
  FamilyMember,
  FamilySpanEvent,
  FamilyViewModel,
  ViewMode,
} from './types';

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
    const timeEl = evEl.querySelector<HTMLElement>('.cf-event-time');
    const titleEl = evEl.querySelector<HTMLElement>('.cf-event-title');
    if (!timeEl || !titleEl) continue;

    const headerEl = evEl.querySelector<HTMLElement>('.cf-event-header');
    let fits = true;

    if (typeof evEl.clientHeight === 'number' && evEl.clientHeight > 0) {
      const headerH = headerEl && typeof headerEl.offsetHeight === 'number' && headerEl.offsetHeight > 0
        ? headerEl.offsetHeight : (typeof timeEl.offsetHeight === 'number' ? timeEl.offsetHeight : 0);
      const titleH = typeof titleEl.offsetHeight === 'number' ? titleEl.offsetHeight : 0;
      if (headerH > 0 && titleH > 0) {
        if (headerH + titleH + 2 > evEl.clientHeight) fits = false;
      } else if (typeof evEl.scrollHeight === 'number' && evEl.scrollHeight > evEl.clientHeight + 1) {
        if (!evEl.querySelector('.cf-event-location')) fits = false;
      }
    }

    if (fits && typeof evEl.clientWidth === 'number' && evEl.clientWidth > 0) {
      const timeW = typeof timeEl.offsetWidth === 'number' ? timeEl.offsetWidth : 0;
      const titleW = typeof titleEl.scrollWidth === 'number' ? titleEl.scrollWidth : (typeof titleEl.offsetWidth === 'number' ? titleEl.offsetWidth : 0);
      if (timeW > 0 && titleW > 0 && timeEl.offsetTop === titleEl.offsetTop) {
        if (timeW + titleW + 8 > evEl.clientWidth) fits = false;
      }
    }

    measurements.push({ el: evEl, fits });
  }

  for (let i = 0; i < measurements.length; i++) {
    const { el, fits } = measurements[i];
    if (fits) el.classList.remove('cf-event-compact');
    else el.classList.add('cf-event-compact');
  }
}

function handleEventKeyDown(e: React.KeyboardEvent, action: () => void) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    action();
  }
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
    if (!isTimerActive) {
      return;
    }
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

  const hasTimelineMarkers = Boolean(weekVM?.timeline_markers?.length || weekVM?.hour_markers?.length);
  const marginLeft = hasTimelineMarkers ? '44px' : undefined;

  const inferredTimeFormat =
    weekVM?.time_format ||
    (weekVM?.timeline_markers?.some((m) => /[ap]m$/i.test(m.label)) ||
    weekVM?.hour_markers?.some((m) => /[ap]m$/i.test(m))
      ? '12h'
      : undefined);

  const startMin =
    parseTimeMinutes(weekVM?.hours_start) ??
    parseTimeMinutes(weekVM?.timeline_markers?.[0]?.label) ??
    parseTimeMinutes(weekVM?.hour_markers?.[0]) ??
    420;
  const endMin =
    parseTimeMinutes(weekVM?.hours_end) ??
    parseTimeMinutes(weekVM?.timeline_markers?.[weekVM.timeline_markers.length - 1]?.label) ??
    parseTimeMinutes(weekVM?.hour_markers?.[weekVM.hour_markers.length - 1]) ??
    1260;

  const collapsedGaps: CollapsedHoursGap[] =
    weekVM?.collapsed_gaps && weekVM.collapsed_gaps.length > 0
      ? weekVM.collapsed_gaps
      : resolveCollapsedMiddayGaps(daysToRender, startMin, endMin, inferredTimeFormat);

  return (
    <div
      className="cf-view-panel cf-view-week"
      style={{ display: activeView !== 'month' ? 'flex' : 'none', flex: 1, minHeight: 0, flexDirection: 'column' }}
    >
      {daysToRender.length > 0 && members.length > 0 ? (
        <>
          {/* All Day Events Band */}
          {visibleAllDayEvents.length > 0 ? (
            <div
              className="cf-allday-band cf-week-allday-grid"
              style={{
                marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
                gridAutoFlow: 'dense', gap: '6px', paddingBottom: '6px', marginBottom: '6px',
                borderBottom: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))',
              }}
            >
              {visibleAllDayEvents.map((ev) => {
                const isMealie = ev.location?.startsWith('/hearth-api/mealie/image/');
                const spanStartDay = weekVM?.days?.[ev.start_col - 1]?.date || ev.start || daysToRender[0]?.date || currentDateISO;
                const spanEndDay = ev.col_span > 1
                  ? weekVM?.days?.[Math.min((weekVM?.days?.length ?? 1) - 1, ev.start_col + ev.col_span - 2)]?.date || ev.end
                  : ev.end;
                return (
                  <div
                    key={ev.id}
                    className={`cf-event cf-allday cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                    role="button"
                    tabIndex={0}
                    aria-label={`${ev.title} (All Day)`}
                    data-event-id={ev.id}
                    data-event-title={ev.title}
                    data-event-allday="true"
                    data-event-location={ev.location}
                    data-event-description={ev.description}
                    data-mealie-decorated={isMealie ? 'true' : undefined}
                    onClick={() => onOpenModal(ev, spanStartDay, spanEndDay)}
                    onKeyDown={(e) => handleEventKeyDown(e, () => onOpenModal(ev, spanStartDay, spanEndDay))}
                    style={{
                      gridColumn: activeView === 'day' ? '1 / span 1' : `${ev.start_col} / span ${ev.col_span}`,
                      fontSize: '0.68rem', fontWeight: 700, borderRadius: '6px', padding: '3px 6px',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      color: ev.text_color, backgroundColor: ev.color, cursor: 'pointer', touchAction: 'manipulation',
                      display: isMealie ? 'flex' : undefined, alignItems: isMealie ? 'center' : undefined, gap: isMealie ? '6px' : undefined,
                    }}
                  >
                    {isMealie ? (
                      <>
                        <img src={ev.location} alt="" style={{ width: '26px', height: '26px', borderRadius: '5px', objectFit: 'cover', flexShrink: 0, boxShadow: '0 1px 3px rgba(0,0,0,0.3)' }} />
                        <svg className="cf-mealie-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0 }}>
                          <path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2" /><path d="M7 2v20" /><path d="M21 15V2v0a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7" />
                        </svg>
                        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 800 }}>{stripMealiePrefix(ev.title)}</span>
                      </>
                    ) : stripMealiePrefix(ev.title)}
                  </div>
                );
              })}
            </div>
          ) : daysToRender.some((d) => d.all_day && d.all_day.length > 0) ? (
            <div
              className="cf-allday-band cf-week-allday-grid"
              style={{
                marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
                gap: '6px', paddingBottom: '6px', marginBottom: '6px', borderBottom: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))',
              }}
            >
              {daysToRender.map((day) => (
                <div key={day.date} className="cf-day-allday-col" style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  {day.all_day?.map((ev) => {
                    const isMealie = ev.location?.startsWith('/hearth-api/mealie/image/');
                    return (
                      <div key={ev.id} className="cf-edge-band cf-allday-band-local" style={{ display: 'flex', flexDirection: 'column', gap: '2px', padding: '2px 4px', background: 'rgba(90, 60, 30, 0.05)', borderRadius: '6px', border: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0 }}>
                        <div
                          className={`cf-event cf-allday cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                          role="button" tabIndex={0} title={ev.title} aria-label={`${ev.title} (All Day)`}
                          data-event-id={ev.id} data-event-title={ev.title} data-event-allday="true"
                          data-event-location={ev.location} data-event-description={ev.description}
                          data-mealie-decorated={isMealie ? 'true' : undefined}
                          onClick={() => onOpenModal(ev, day.date)}
                          onKeyDown={(e) => handleEventKeyDown(e, () => onOpenModal(ev, day.date))}
                          style={{
                            fontSize: '0.65rem', fontWeight: 700, borderRadius: '4px', padding: '2px 4px',
                            color: ev.text_color, backgroundColor: ev.color, cursor: 'pointer', touchAction: 'manipulation',
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            display: isMealie ? 'flex' : undefined, alignItems: isMealie ? 'center' : undefined, gap: isMealie ? '6px' : undefined,
                          }}
                        >
                          {isMealie ? (
                            <>
                              <img src={ev.location} alt="" style={{ width: '26px', height: '26px', borderRadius: '5px', objectFit: 'cover', flexShrink: 0, boxShadow: '0 1px 3px rgba(0,0,0,0.3)' }} />
                              <svg className="cf-mealie-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0 }}>
                                <path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2" /><path d="M7 2v20" /><path d="M21 15V2v0a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7" />
                              </svg>
                              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 800 }}>{stripMealiePrefix(ev.title)}</span>
                            </>
                          ) : stripMealiePrefix(ev.title)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          ) : null}

          {/* Early Events Grid */}
          {daysToRender.some((d) => d.early_events && d.early_events.length > 0) && (
            <div
              className="cf-edge-band cf-week-early-grid"
              style={{
                marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
                gap: '6px', paddingBottom: '4px', marginBottom: '4px', borderBottom: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))',
              }}
            >
              {daysToRender.map((day) => (
                <div key={day.date} className="cf-day-early-col" style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  {day.early_events?.map((ev) => {
                    const startDisplay = formatEventTime(ev.start, inferredTimeFormat) || ev.start;
                    return (
                      <div key={ev.id} className="cf-edge-band cf-early-band" style={{ display: 'flex', flexDirection: 'column', gap: '2px', padding: '2px 4px', background: 'rgba(90, 60, 30, 0.05)', borderRadius: '6px', border: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0 }}>
                        <div
                          className={`cf-edge-event cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                          role="button" tabIndex={0} title={`Early event: ${startDisplay} ${ev.title}${ev.location ? ` (${ev.location})` : ''}`}
                          aria-label={`Early event: ${ev.title} at ${startDisplay}`}
                          data-event-id={ev.id} data-event-title={ev.title} data-event-start={ev.start}
                          data-event-end={ev.end} data-event-location={ev.location} data-event-description={ev.description}
                          onClick={() => onOpenModal(ev, day.date)}
                          onKeyDown={(e) => handleEventKeyDown(e, () => onOpenModal(ev, day.date))}
                          style={{
                            fontSize: '0.60rem', fontWeight: 700, padding: '2px 4px', borderRadius: '4px',
                            backgroundColor: ev.color, color: ev.text_color, overflow: 'hidden', textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap', cursor: 'pointer', touchAction: 'manipulation',
                          }}
                        >
                          ▲ {startDisplay} {ev.title}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}

          {/* Week / Day Body */}
          <div className="cf-week-body" style={{ display: 'flex', flex: 1, minHeight: 0, gap: '6px', position: 'relative' }}>
            {hasTimelineMarkers ? (
              <div className="cf-timeline-axis" style={{ width: '38px', display: 'flex', flexDirection: 'column', flexShrink: 0, userSelect: 'none' }}>
                <div className="cf-timeline-spacer" style={{ height: '44px', flexShrink: 0 }} />
                <div className="cf-timeline-markers" style={{ flex: 1, position: 'relative', minHeight: 0, marginBottom: '6px', fontFamily: 'var(--mm-font-mono, monospace)', fontSize: '0.60rem', fontWeight: 700, color: 'var(--mm-text-dim, #8b819f)' }}>
                  {weekVM?.timeline_markers && weekVM.timeline_markers.length > 0 ? (
                    weekVM.timeline_markers.map((marker, idx) => (
                      <span
                        key={idx}
                        className="cf-hour-marker"
                        style={{
                          position: 'absolute', right: '6px', top: `${marker.top_pct}%`,
                          transform: marker.is_start ? 'translateY(0)' : marker.is_end ? 'translateY(-100%)' : 'translateY(-50%)',
                          lineHeight: 1, whiteSpace: 'nowrap', pointerEvents: 'none',
                        }}
                      >
                        {marker.label}
                      </span>
                    ))
                  ) : (
                    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', justifyContent: 'space-between', textAlign: 'right', paddingRight: '6px' }}>
                      {weekVM?.hour_markers?.map((marker, idx) => (
                        <span key={idx} className="cf-hour-marker" style={{ lineHeight: 1 }}>{marker}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : null}

            <div className="cf-week-grid" style={{ display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`, gap: '6px', flex: 1, minHeight: 0 }}>
              {daysToRender.map((day) => {
                const dayBg = day.is_today ? 'var(--mm-cal-today-bg, var(--mm-lavender-light, #f3effe))'
                  : day.is_weekend ? 'var(--mm-cal-weekend-bg, rgba(90, 60, 30, 0.02))'
                  : 'var(--mm-cal-day-bg, rgba(90, 60, 30, 0.03))';
                const dayBorder = day.is_today ? '1px solid var(--mm-cal-today-border, var(--mm-lavender, #8b60ed))'
                  : '1px solid var(--mm-card-border, rgba(90, 60, 30, 0.08))';
                return (
                  <div
                    key={day.date}
                    className={`cf-day${day.is_today ? ' cf-today' : ''}${day.is_weekend ? ' cf-weekend' : ''}`}
                    data-date={day.date}
                    style={{
                      display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0, borderRadius: '14px',
                      padding: '6px', background: dayBg, border: dayBorder, overflow: 'hidden', position: 'relative',
                    }}
                  >
                    <div
                      className="cf-day-header"
                      role={activeView === 'week' ? 'button' : undefined}
                      tabIndex={activeView === 'week' ? 0 : undefined}
                      onClick={() => {
                        if (activeView === 'week' && day.date) onSelectDay?.(day.date);
                      }}
                      onKeyDown={(e) => {
                        if (activeView === 'week' && (e.key === 'Enter' || e.key === ' ')) {
                          e.preventDefault();
                          if (day.date) onSelectDay?.(day.date);
                        }
                      }}
                      style={{
                        height: '44px', boxSizing: 'border-box', textAlign: 'center', paddingBottom: '6px',
                        borderBottom: day.is_today ? '1px solid var(--mm-cal-today-header-border, rgba(139, 96, 237, 0.25))' : '1px solid var(--mm-divider, rgba(90, 60, 30, 0.07))',
                        flexShrink: 0, background: 'transparent',
                        cursor: activeView === 'week' ? 'pointer' : undefined,
                      }}
                    >
                      <div className="cf-day-weekday" style={{ fontSize: '0.68rem', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.04em', color: day.is_today ? 'var(--mm-cal-today-text, var(--mm-lavender, #8b60ed))' : 'var(--mm-text-dim, #8b819f)' }}>
                        {day.is_today ? 'Today' : day.weekday_abbr || ''}
                      </div>
                      <div className="cf-day-num" style={{ fontSize: '1.15rem', fontWeight: 900, lineHeight: 1.1, color: day.is_today ? 'var(--mm-cal-today-text, var(--mm-lavender, #8b60ed))' : 'var(--mm-text-title, #2a2338)' }}>
                        {day.day_of_month}
                      </div>
                    </div>

                    <div className="cf-day-canvas" ref={(el) => registerDayCanvas(day.date, el)} style={{ flex: 1, position: 'relative', minHeight: 0, marginTop: '4px' }}>
                      {collapsedGaps.map((gap, gapIdx) => (
                        <div
                          key={`${gap.start_hour}-${gap.end_hour}-${gapIdx}`}
                          className="cf-collapsed-gap"
                          aria-hidden="true"
                          data-gap-start={gap.start_str || `${String(gap.start_hour).padStart(2, '0')}:00`}
                          data-gap-end={gap.end_str || `${String(gap.end_hour).padStart(2, '0')}:00`}
                          style={{
                            position: 'absolute',
                            left: 0,
                            right: 0,
                            top: `${gap.top_pct}%`,
                            height: `${gap.height_pct}%`,
                          }}
                        >
                          <span className="cf-collapsed-gap-label">{gap.label}</span>
                        </div>
                      ))}
                      {day.is_today && nowPct !== null && (
                        <div
                          className="cf-now-indicator"
                          aria-hidden="true"
                          data-now-pct={nowPct.toFixed(1)}
                          style={{
                            position: 'absolute',
                            left: 0,
                            right: 0,
                            top: `${nowPct.toFixed(2)}%`,
                            height: '2px',
                            background: 'var(--mm-coral, #ff6f59)',
                            zIndex: 15,
                            pointerEvents: 'none',
                          }}
                        >
                          <span
                            className="cf-now-dot"
                            style={{
                              position: 'absolute',
                              left: '-3px',
                              top: '-3px',
                              width: '8px',
                              height: '8px',
                              borderRadius: '50%',
                              background: 'var(--mm-coral, #ff6f59)',
                            }}
                          />
                        </div>
                      )}
                      {day.events?.map((ev) => {
                        const startDisplay = formatEventTime(ev.start, inferredTimeFormat) || ev.start;
                        const endDisplay = formatEventTime(ev.end, inferredTimeFormat) || ev.end;
                        return (
                          <div
                            key={ev.id}
                            className={`cf-day-event cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                            role="button" tabIndex={0}
                            title={`${startDisplay}–${endDisplay}: ${ev.title}${ev.location ? ` (${ev.location})` : ''}`}
                            aria-label={`${ev.title}, ${startDisplay} to ${endDisplay}${ev.location ? `, ${ev.location}` : ''}`}
                            data-event-id={ev.id} data-event-title={ev.title} data-event-start={ev.start}
                            data-event-end={ev.end} data-event-location={ev.location} data-event-description={ev.description}
                            onClick={() => onOpenModal(ev, day.date)}
                            onKeyDown={(e) => handleEventKeyDown(e, () => onOpenModal(ev, day.date))}
                            style={{
                              position: 'absolute', top: `${ev.top_pct}%`, height: `${ev.height_pct}%`,
                              left: `calc(${ev.left_pct}% + 1px)`, width: `calc(${ev.width_pct}% - 2px)`,
                              minHeight: '20px', padding: '2px 4px', borderRadius: '6px',
                              backgroundColor: ev.color, color: ev.text_color, overflow: 'hidden',
                              display: 'flex', flexDirection: 'column', boxSizing: 'border-box',
                              border: '1px solid rgba(0, 0, 0, 0.12)', boxShadow: '0 1px 3px rgba(0, 0, 0, 0.12)',
                              cursor: 'pointer', touchAction: 'manipulation',
                            }}
                          >
                            <div className="cf-event-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '2px', fontSize: '0.60rem', fontWeight: 800, lineHeight: 1.1, flexShrink: 0 }}>
                              <span className="cf-event-time" style={{ fontFamily: 'var(--mm-font-mono, monospace)' }}>{startDisplay}</span>
                              {ev.initials && ev.initials.length > 0 && <span className="cf-event-initials" style={{ opacity: 0.85 }}>{ev.initials.join('')}</span>}
                            </div>
                            <div className="cf-event-title" style={{ fontSize: '0.65rem', fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.2, flexShrink: 0 }}>
                              {ev.title}
                            </div>
                            {ev.location && (
                              <div className="cf-event-location" style={{ fontSize: '0.55rem', opacity: 0.85, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {ev.location}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Late Events Grid */}
          {daysToRender.some((d) => d.late_events && d.late_events.length > 0) && (
            <div
              className="cf-edge-band cf-week-late-grid"
              style={{
                marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
                gap: '6px', paddingTop: '4px', marginTop: '4px', borderTop: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))',
              }}
            >
              {daysToRender.map((day) => (
                <div key={day.date} className="cf-day-late-col" style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  {day.late_events?.map((ev) => {
                    const startDisplay = formatEventTime(ev.start, inferredTimeFormat) || ev.start;
                    return (
                      <div key={ev.id} className="cf-edge-band cf-late-band" style={{ display: 'flex', flexDirection: 'column', gap: '2px', padding: '2px 4px', background: 'rgba(90, 60, 30, 0.05)', borderRadius: '6px', border: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0 }}>
                        <div
                          className={`cf-edge-event cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                          role="button" tabIndex={0} title={`Late event: ${startDisplay} ${ev.title}${ev.location ? ` (${ev.location})` : ''}`}
                          aria-label={`Late event: ${ev.title} at ${startDisplay}`}
                          data-event-id={ev.id} data-event-title={ev.title} data-event-start={ev.start}
                          data-event-end={ev.end} data-event-location={ev.location} data-event-description={ev.description}
                          onClick={() => onOpenModal(ev, day.date)}
                          onKeyDown={(e) => handleEventKeyDown(e, () => onOpenModal(ev, day.date))}
                          style={{
                            fontSize: '0.60rem', fontWeight: 700, padding: '2px 4px', borderRadius: '4px',
                            backgroundColor: ev.color, color: ev.text_color, overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer', touchAction: 'manipulation',
                          }}
                        >
                          ▼ {startDisplay} {ev.title}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="cf-empty" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', flex: 1, color: 'var(--mm-text-dim)', fontSize: '0.9rem' }}>
          <span>No calendar data yet</span>
        </div>
      )}
    </div>
  );
}
