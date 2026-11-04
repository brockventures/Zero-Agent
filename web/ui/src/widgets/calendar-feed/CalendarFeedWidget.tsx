import './CalendarFeedWidget.css';
import {
  useState,
  type CSSProperties,
} from 'react';
import { useDisplayStore } from '../../store';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import {
  calendarFeedActiveCalendars,
  calendarFeedDayGroups,
  formatDate,
  formatTime,
  type CalendarFeedEvent,
} from '../format';
import { CalendarEventModal } from '../calendar-grid/CalendarEventModal';
import { formatModalDateRange } from '../calendar-grid/dateUtils';
import { useInactivityReset } from '../calendar-grid/useInactivityReset';
import type { FamilyEvent } from '../calendar-grid/types';

/**
 * Native React widget component for upcoming calendar feed agendas.
 * Ports widgets/calendar-feed/views/widget.html.
 */
export function CalendarFeedWidget({
  id,
}: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const data = entry?.data;
  const isError = entry?.state === 'error';

  const storeTimezone = useDisplayStore((s) => s.header.timezone);
  const configTimezone =
    typeof config?.timezone === 'string' && config.timezone.trim() !== ''
      ? config.timezone.trim()
      : undefined;
  const tz = configTimezone || storeTimezone || undefined;

  const customTitle =
    typeof config?.title === 'string' && config.title.trim() !== ''
      ? config.title.trim()
      : '';

  const activeCalendars = calendarFeedActiveCalendars(config);
  const isHydrated =
    Array.isArray(data) ||
    (Boolean(data) &&
      typeof data === 'object' &&
      (Array.isArray((data as Record<string, unknown>).events) ||
        Array.isArray((data as Record<string, unknown>).Events)));

  const entryTimestamp = entry?.timestamp;
  const groups = (() => {
    if (!isHydrated) return [];
    let refTime: Date | undefined;
    if (entryTimestamp) {
      const parsed = new Date(entryTimestamp);
      if (!isNaN(parsed.getTime())) {
        refTime = parsed;
      }
    }
    return calendarFeedDayGroups(data, refTime, tz);
  })();

  const [activeModalEvent, setActiveModalEvent] = useState<FamilyEvent | null>(
    null
  );
  const [activeModalDateLabel, setActiveModalDateLabel] = useState<string>('');

  const handleInactivityReset = () => {
    setActiveModalEvent(null);
    setActiveModalDateLabel('');
  };

  const { handleUserActivity } = useInactivityReset({
    isInteractive: Boolean(activeModalEvent),
    onReset: handleInactivityReset,
  });

  const closeModal = () => {
    setActiveModalEvent(null);
    setActiveModalDateLabel('');
    handleUserActivity();
  };

  const handleActivateEvent = (
    ev: CalendarFeedEvent,
    evId: string,
    isAllDay: boolean,
    groupDate: string
  ) => {
    const calName = ev.calendar_name || ev.calendarName;
    const familyEvent: FamilyEvent = {
      id: evId,
      title: ev.title,
      all_day: isAllDay,
      location: ev.location ? String(ev.location) : undefined,
      description:
        typeof ev.description === 'string' ? ev.description : undefined,
      color: ev.color ? String(ev.color) : undefined,
      owners: calName ? [String(calName)] : undefined,
      start: !isAllDay
        ? formatTime('3:04 PM', ev.start, tz) || ev.start
        : undefined,
      end:
        !isAllDay && ev.end
          ? formatTime('3:04 PM', ev.end, tz) || ev.end
          : undefined,
    };

    const startDate = groupDate || formatDate('2006-01-02', ev.start, tz);
    const endDate = ev.end
      ? formatDate('2006-01-02', ev.end, tz)
      : undefined;

    setActiveModalEvent(familyEvent);
    setActiveModalDateLabel(formatModalDateRange(startDate, endDate));
    handleUserActivity();
  };

  return (
    <div className="widget-calendar widget-calendar-feed">
      {isError && (
        <div className="widget-error-banner">
          <span className="error-icon">⚠</span>
          <span>Widget Error</span>
        </div>
      )}

      <div className="calendar-feed-header">
        <div className="calendar-header-title-row">
          <div className="calendar-header-icon-box">
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="calendar-header-icon"
            >
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
              <line x1="16" y1="2" x2="16" y2="6" />
              <line x1="8" y1="2" x2="8" y2="6" />
              <line x1="3" y1="10" x2="21" y2="10" />
            </svg>
          </div>
          <h2 className="calendar-header-title">{customTitle || 'Upcoming'}</h2>
        </div>

        {activeCalendars.length > 0 && (
          <div className="calendar-header-chips">
            <div className="calendar-avatar-stack">
              {activeCalendars.map((cal, idx) => (
                <span
                  key={idx}
                  className="calendar-avatar-dot"
                  title={cal.name}
                  style={{ backgroundColor: cal.color || 'var(--mm-coral)' }}
                />
              ))}
            </div>
            <span className="calendar-count-badge">
              {activeCalendars.length}{' '}
              {activeCalendars.length === 1 ? 'Calendar' : 'Calendars'}
            </span>
          </div>
        )}
      </div>

      {!isHydrated ? (
        <div className="calendar-loading">
          <span>Loading calendar feed...</span>
        </div>
      ) : groups.length > 0 ? (
        <div className="calendar-feed-container calendar-agenda-container">
          {groups.map((group) => (
            <div className="calendar-day-group" key={group.date}>
              <div className="calendar-day-divider">
                <span
                  className={`day-pill-badge ${
                    group.rel === 'Today'
                      ? 'day-pill-today'
                      : group.rel === 'Tomorrow'
                        ? 'day-pill-tomorrow'
                        : 'day-pill-future'
                  }`}
                >
                  {group.rel === 'Today'
                    ? `TODAY • ${formatDate('Jan 2', group.start, tz)}`
                    : group.rel === 'Tomorrow'
                      ? `TOMORROW • ${formatDate('Jan 2', group.start, tz)}`
                      : formatDate('Monday • Jan 2', group.start, tz)}
                </span>
                <div className="day-divider-line" />
              </div>
              <div className="calendar-day-card">
                {group.events.map((ev, evIdx) => {
                  const evId = ev.id || `${ev.title}-${ev.start}-${evIdx}`;
                  const isAllDay = Boolean(ev.all_day || ev.allDay);
                  const calName = ev.calendar_name || ev.calendarName;
                  return (
                    <div
                      key={evId}
                      className="calendar-event-item cf-event-clickable"
                      role="button"
                      tabIndex={0}
                      data-event-id={ev.id ?? ''}
                      data-event-start={ev.start ?? ''}
                      data-event-end={ev.end ?? ''}
                      data-event-allday={isAllDay ? 'true' : 'false'}
                      onClick={() =>
                        handleActivateEvent(ev, evId, isAllDay, group.date)
                      }
                      onKeyDown={(e) => {
                        if (
                          e.key === 'Enter' ||
                          e.key === ' ' ||
                          e.key === 'Spacebar'
                        ) {
                          e.preventDefault();
                          handleActivateEvent(ev, evId, isAllDay, group.date);
                        }
                      }}
                    >
                      <div className="event-time-col">
                        {isAllDay ? (
                          <span className="event-time-chip event-time-allday">
                            All Day
                          </span>
                        ) : (
                          <>
                            <span className="event-time-val">
                              {formatTime('3:04', ev.start, tz)}
                            </span>
                            <span className="event-time-ampm">
                              {formatTime('PM', ev.start, tz)}
                            </span>
                          </>
                        )}
                      </div>
                      <div className="event-details-col">
                        <div className="event-title-row">
                          <span className="event-title" title={ev.title}>
                            {ev.title}
                          </span>
                        </div>
                        {ev.location && (
                          <div
                            className="event-location-row"
                            title={String(ev.location)}
                          >
                            <svg
                              width="12"
                              height="12"
                              viewBox="0 0 24 24"
                              fill="none"
                              className="event-location-icon"
                              aria-hidden="true"
                            >
                              <path
                                d="M12 2C7.8 2 4.5 5.3 4.5 9.5C4.5 14.8 12 22 12 22C12 22 19.5 14.8 19.5 9.5C19.5 5.3 16.2 2 12 2Z"
                                fill="#ff4365"
                              />
                              <circle cx="12" cy="9.5" r="3.2" fill="#ffffff" />
                            </svg>
                            <span className="event-location-text">
                              {String(ev.location)}
                            </span>
                          </div>
                        )}
                      </div>
                      {calName && (
                        <span
                          className="event-calendar-pill"
                          style={
                            ev.color
                              ? ({
                                  '--pill-color': ev.color,
                                  background: `color-mix(in srgb, ${ev.color} 12%, white)`,
                                  color: ev.color,
                                } as CSSProperties)
                              : undefined
                          }
                        >
                          {String(calName)}
                        </span>
                      )}
                      <span
                        className="event-accent-dot"
                        style={
                          ev.color ? { backgroundColor: ev.color } : undefined
                        }
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="calendar-empty">
          <span className="empty-icon">
            <svg
              width="24"
              height="24"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
              <line x1="16" y1="2" x2="16" y2="6" />
              <line x1="8" y1="2" x2="8" y2="6" />
              <line x1="3" y1="10" x2="21" y2="10" />
              <path d="m9 16 2 2 4-4" />
            </svg>
          </span>
          <span className="empty-title">All clear! Enjoy your day ✨</span>
          <span className="empty-subtitle">
            No upcoming events on your agenda
          </span>
        </div>
      )}

      <CalendarEventModal
        event={activeModalEvent}
        dateLabel={activeModalDateLabel}
        onClose={closeModal}
      />
    </div>
  );
}
