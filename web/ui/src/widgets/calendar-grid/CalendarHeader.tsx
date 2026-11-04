import type { FamilyMember, ViewMode } from './types';

export interface CalendarHeaderProps {
  customTitle: string;
  activeView: ViewMode;
  pageOffset: number;
  rangeLabelWeek: string;
  rangeLabelMonth: string;
  rangeLabelDay: string;
  syncMessage: string | null;
  members: FamilyMember[];
  selectedMember?: string | null;
  onSelectMember?: (name: string) => void;
  onPrev: () => void;
  onNext: () => void;
  onResetDate: () => void;
  onViewSwitch: (view: ViewMode) => void;
}

export function CalendarHeader({
  customTitle,
  activeView,
  pageOffset,
  rangeLabelWeek,
  rangeLabelMonth,
  rangeLabelDay,
  syncMessage,
  members,
  selectedMember,
  onSelectMember,
  onPrev,
  onNext,
  onResetDate,
  onViewSwitch,
}: CalendarHeaderProps) {
  return (
    <div
      className="cf-header"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        width: '100%',
        paddingBottom: '8px',
        marginBottom: '8px',
        borderBottom: '1px solid var(--mm-divider, rgba(90, 60, 30, 0.08))',
      }}
    >
      <div
        className="cf-header-left"
        style={{ display: 'flex', alignItems: 'center', gap: '12px', minWidth: 0 }}
      >
        <div
          className="cf-title-badge"
          style={{
            width: '36px',
            height: '36px',
            borderRadius: '11px',
            background: 'rgba(139, 92, 246, 0.12)',
            color: 'var(--mm-lavender, #8b60ed)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <svg
            viewBox="0 0 24 24"
            width="18"
            height="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
            <line x1="16" y1="2" x2="16" y2="6" />
            <line x1="8" y1="2" x2="8" y2="6" />
            <line x1="3" y1="10" x2="21" y2="10" />
          </svg>
        </div>

        <h2
          className="cf-title"
          style={{
            margin: 0,
            fontSize: '1.1rem',
            fontWeight: 800,
            color: 'var(--mm-text-title, #2a2338)',
            lineHeight: 1,
            flexShrink: 0,
          }}
        >
          {customTitle}
        </h2>

        <div
          className="cf-date-row cf-date-pill"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '2px',
            background: 'rgba(90, 60, 30, 0.04)',
            border: '1px solid var(--mm-card-border, rgba(90, 60, 30, 0.08))',
            borderRadius: '9999px',
            padding: '2px 4px',
            flexShrink: 0,
          }}
        >
          <button
            type="button"
            className="cf-nav-btn cf-prev-btn"
            aria-label="Previous date range"
            title="Previous"
            onClick={onPrev}
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
          <div
            className="cf-subtitle"
            style={{
              display: 'flex',
              alignItems: 'center',
              padding: '0 6px',
              fontSize: '0.80rem',
              fontWeight: 700,
              color: 'var(--mm-text-title, #2a2338)',
              whiteSpace: 'nowrap',
            }}
          >
            <span
              className="cf-range-label cf-label-week"
              style={{ display: activeView === 'week' ? undefined : 'none', fontWeight: 700 }}
            >
              {rangeLabelWeek}
            </span>
            <span
              className="cf-range-label cf-label-month"
              style={{ display: activeView === 'month' ? undefined : 'none', fontWeight: 700 }}
            >
              {rangeLabelMonth}
            </span>
            <span
              className="cf-range-label cf-label-day"
              style={{ display: activeView === 'day' ? undefined : 'none', fontWeight: 700 }}
            >
              {rangeLabelDay}
            </span>
          </div>
          <button
            type="button"
            className="cf-nav-btn cf-next-btn"
            aria-label="Next date range"
            title="Next"
            onClick={onNext}
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
          <button
            type="button"
            className="cf-today-badge"
            title="Return to today"
            style={{ display: pageOffset !== 0 ? undefined : 'none' }}
            onClick={onResetDate}
          >
            TODAY
          </button>
        </div>

        {syncMessage && (
          <span className="cf-sync-indicator" role="status" aria-live="polite">
            {syncMessage}
          </span>
        )}
      </div>

      <div
        className="cf-header-right"
        style={{ display: 'flex', alignItems: 'center', gap: '12px', flexShrink: 0 }}
      >
        {members.length > 0 && (
          <div
            className="cf-legend"
            style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}
          >
            {members.map((m, i) => (
              <button
                type="button"
                key={m.name || i}
                data-member={m.name}
                aria-pressed={selectedMember === m.name}
                className={`cf-member-chip cf-pattern-${m.pattern || 'solid'}${
                  selectedMember === m.name ? ' active' : ''
                }${selectedMember && selectedMember !== m.name ? ' dimmed' : ''}`}
                title={m.name}
                onClick={() => onSelectMember?.(m.name)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '5px',
                  background: 'rgba(90, 60, 30, 0.04)',
                  padding: '3px 9px',
                  borderRadius: '9999px',
                  border: '1px solid var(--mm-card-border, rgba(90, 60, 30, 0.08))',
                  opacity: selectedMember && selectedMember !== m.name ? 0.45 : 1,
                  cursor: 'pointer',
                  touchAction: 'manipulation',
                }}
              >
                <span
                  className="cf-member-dot"
                  style={{
                    display: 'inline-block',
                    width: '7px',
                    height: '7px',
                    borderRadius: '50%',
                    background: `var(--mm-member-${i}, ${m.color})`,
                    flexShrink: 0,
                  }}
                />
                <span
                  className="cf-member-name"
                  style={{
                    fontSize: '0.70rem',
                    fontWeight: 700,
                    color: 'var(--mm-text-primary, #2a2338)',
                    lineHeight: 1,
                  }}
                >
                  {m.name}
                </span>
              </button>
            ))}
          </div>
        )}

        <div className="cf-view-switcher" role="tablist" aria-label="Calendar view">
          <button
            type="button"
            className={`cf-view-btn${activeView === 'week' ? ' active' : ''}`}
            data-view="week"
            role="tab"
            aria-selected={activeView === 'week' ? 'true' : 'false'}
            onClick={() => onViewSwitch('week')}
          >
            Week
          </button>
          <button
            type="button"
            className={`cf-view-btn${activeView === 'month' ? ' active' : ''}`}
            data-view="month"
            role="tab"
            aria-selected={activeView === 'month' ? 'true' : 'false'}
            onClick={() => onViewSwitch('month')}
          >
            Month
          </button>
        </div>
      </div>
    </div>
  );
}
