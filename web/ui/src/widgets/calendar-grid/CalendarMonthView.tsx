import { stripMealiePrefix } from './dateUtils';
import type { FamilyEvent, FamilyMember, FamilyMonthViewModel, ViewMode } from './types';

export interface CalendarMonthViewProps {
  activeView: ViewMode;
  monthVM?: FamilyMonthViewModel;
  members: FamilyMember[];
  onOpenModal: (ev: FamilyEvent, date: string) => void;
  onSelectDay?: (date: string) => void;
}

export function CalendarMonthView({
  activeView,
  monthVM,
  members,
  onOpenModal,
  onSelectDay,
}: CalendarMonthViewProps) {
  return (
    <div
      className="cf-view-panel cf-view-month"
      style={{
        display: activeView === 'month' ? 'flex' : 'none',
        flex: 1,
        minHeight: 0,
        flexDirection: 'column',
      }}
    >
      {monthVM?.days && members.length > 0 ? (
        <>
          <div
            className="cf-month-weekdays"
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(7, 1fr)',
              gap: '4px',
              paddingBottom: '4px',
              borderBottom: '1px solid var(--mm-divider, rgba(255, 255, 255, 0.08))',
              textAlign: 'center',
              fontSize: '0.70rem',
              fontWeight: 700,
              color: 'var(--mm-text-dim, #94a3b8)',
            }}
          >
            {monthVM.weekday_headers?.map((header) => (
              <span key={header} className="cf-weekday-header">
                {header}
              </span>
            ))}
          </div>

          <div
            className="cf-month-grid"
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(7, 1fr)',
              gridTemplateRows: 'repeat(6, 1fr)',
              gap: '4px',
              flex: 1,
              minHeight: 0,
              marginTop: '4px',
            }}
          >
            {monthVM.days.map((day) => (
              <div
                key={day.date}
                className={`cf-month-day${!day.is_current_month ? ' cf-other-month' : ''}${
                  day.is_today ? ' cf-today' : ''
                }${day.is_weekend ? ' cf-weekend' : ''}${
                  day.events_count && day.events_count > 0 ? ' cf-has-events' : ''
                }`}
                data-date={day.date}
                data-event-id={day.events && day.events.length > 0 ? day.events[0].id : undefined}
                onClick={() => {
                  if (day.events && day.events.length > 0) {
                    onOpenModal(day.events[0], day.date);
                  } else if (day.event_title) {
                    onOpenModal({ id: day.date, title: day.event_title }, day.date);
                  }
                }}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  minWidth: 0,
                  minHeight: 0,
                  padding: '3px 4px',
                  borderRadius: '6px',
                  background: day.is_weekend
                    ? 'rgba(255, 255, 255, 0.02)'
                    : 'rgba(255, 255, 255, 0.04)',
                  border: '1px solid rgba(255, 255, 255, 0.05)',
                  overflow: 'hidden',
                  position: 'relative',
                  opacity: !day.is_current_month ? 0.38 : undefined,
                  outline: day.is_today
                    ? '1px solid var(--mm-cal-today-border, var(--mm-lavender, #8b60ed))'
                    : undefined,
                  zIndex: day.is_today ? 2 : undefined,
                  cursor:
                    (day.events && day.events.length > 0) || day.event_title
                      ? 'pointer'
                      : undefined,
                }}
              >
                <div
                  className="cf-month-day-header"
                  role="button"
                  tabIndex={0}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (day.date) onSelectDay?.(day.date);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      e.stopPropagation();
                      if (day.date) onSelectDay?.(day.date);
                    }
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    fontSize: '0.68rem',
                    fontWeight: 700,
                    lineHeight: 1.2,
                    cursor: 'pointer',
                  }}
                >
                  <span
                    className={`cf-day-num${day.is_today ? ' cf-today-num' : ''}`}
                    style={
                      day.is_today
                        ? {
                            background: 'var(--mm-cal-today-badge, var(--mm-lavender, #8b60ed))',
                            color: 'var(--mm-cal-today-badge-text, #ffffff)',
                            borderRadius: '50%',
                            width: '16px',
                            height: '16px',
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            fontSize: '0.60rem',
                            fontWeight: 800,
                          }
                        : { color: 'var(--mm-text-title, #ffffff)' }
                    }
                  >
                    {day.day_of_month}
                  </span>
                </div>

                {day.dots && day.dots.length > 0 && (
                  <div
                    className="cf-month-dots"
                    style={{
                      display: 'flex',
                      flexWrap: 'wrap',
                      gap: '3px',
                      alignItems: 'center',
                      marginTop: '2px',
                    }}
                  >
                    {day.dots.map((dot, dIdx) => (
                      <span
                        key={dIdx}
                        className={`cf-month-dot cf-pattern-${dot.pattern || 'solid'}${
                          dot.is_shared ? ' cf-dot-shared' : ` cf-member-${dot.member_index}`
                        }`}
                        title={dot.name}
                        style={{
                          display: 'inline-block',
                          width: '6px',
                          height: '6px',
                          borderRadius: '50%',
                          background: dot.is_shared
                            ? 'var(--mm-cf-shared, #3D405B)'
                            : `var(--mm-member-${dot.member_index}, ${dot.color})`,
                          flexShrink: 0,
                        }}
                      />
                    ))}
                  </div>
                )}

                {day.event_title ? (
                  day.events && day.events.length > 0 ? (
                    <>
                      {day.events.slice(0, 2).map((ev) => (
                        <div
                          key={ev.id}
                          className="cf-month-event-title cf-event-clickable"
                          data-event-id={ev.id}
                          title={ev.title}
                          onClick={(e) => {
                            e.stopPropagation();
                            onOpenModal(ev, day.date);
                          }}
                          style={{
                            fontSize: '0.60rem',
                            color: 'var(--mm-text-dim, #cbd5e1)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            marginTop: '2px',
                            lineHeight: 1.2,
                            cursor: 'pointer',
                            touchAction: 'manipulation',
                          }}
                        >
                          {stripMealiePrefix(ev.title)}
                        </div>
                      ))}
                      {day.events.length > 2 && (
                        <div
                          className="cf-month-more"
                          role="button"
                          tabIndex={0}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (day.date) onSelectDay?.(day.date);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              e.stopPropagation();
                              if (day.date) onSelectDay?.(day.date);
                            }
                          }}
                          style={{
                            fontSize: '0.55rem',
                            color: 'var(--mm-text-dim, #94a3b8)',
                            lineHeight: 1.1,
                            marginTop: '1px',
                            cursor: 'pointer',
                          }}
                        >
                          +{day.events.length - 2} more
                        </div>
                      )}
                    </>
                  ) : (
                    <div
                      className="cf-month-event-title cf-event-clickable"
                      title={day.event_title}
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenModal({ id: day.date, title: day.event_title! }, day.date);
                      }}
                      style={{
                        fontSize: '0.60rem',
                        color: 'var(--mm-text-dim, #cbd5e1)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        marginTop: '2px',
                        lineHeight: 1.2,
                        cursor: 'pointer',
                        touchAction: 'manipulation',
                      }}
                    >
                      {stripMealiePrefix(day.event_title)}
                    </div>
                  )
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : (
        <div
          className="cf-empty"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flex: 1,
            color: 'var(--mm-text-dim)',
            fontSize: '0.9rem',
          }}
        >
          <span>No calendar data yet</span>
        </div>
      )}
    </div>
  );
}
