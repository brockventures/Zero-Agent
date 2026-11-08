import { useEffect, ViewTransition } from 'react';
import { stripMealiePrefix } from './dateUtils';
import type { FamilyEvent } from './types';

export interface CalendarEventModalProps {
  event: FamilyEvent | null;
  dateLabel: string;
  onClose: () => void;
}

export function CalendarEventModal({
  event,
  dateLabel,
  onClose,
}: CalendarEventModalProps) {
  useEffect(() => {
    if (!event) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [event, onClose]);

  if (!event) return null;

  const modalTimeText = event.all_day
    ? 'All Day'
    : event.start && event.end
    ? `${event.start} – ${event.end}`
    : event.start || '';

  const modalDateTimeDisplay =
    dateLabel && modalTimeText
      ? `${dateLabel} • ${modalTimeText}`
      : dateLabel || modalTimeText;

  const modalTitle = stripMealiePrefix(event.title) || 'Untitled Event';

  return (
    <ViewTransition default="none">
      <div
        className="cf-modal-overlay"
        role="dialog"
        aria-modal="true"
        aria-label={modalTitle}
        onClick={(e) => {
          if (e.target === e.currentTarget) {
            onClose();
          }
        }}
      >
        <div className="cf-modal-card" onClick={(e) => e.stopPropagation()}>
          <div className="cf-modal-header">
            <h3 className="cf-modal-title">{modalTitle}</h3>
            <button
              type="button"
              className="cf-modal-close"
              aria-label="Close"
              onClick={onClose}
            >
              &times;
            </button>
          </div>

          {/* Date & Time */}
          {modalDateTimeDisplay ? (
            <div className="cf-modal-time">
              <svg
                className="cf-modal-icon"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
                style={{ flexShrink: 0 }}
              >
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <span>{modalDateTimeDisplay}</span>
            </div>
          ) : null}

          {/* Location or Mealie Hero Image */}
          {event.location && event.location.trim() ? (
            event.location.startsWith('/hearth-api/mealie/image/') ? (
              <div
                style={{
                  width: '100%',
                  height: '190px',
                  borderRadius: '12px',
                  overflow: 'hidden',
                  margin: '8px 0 10px',
                  background: 'rgba(15,23,42,0.4)',
                }}
              >
                <img
                  src={event.location}
                  alt={modalTitle}
                  style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                />
              </div>
            ) : (
              <div className="cf-modal-location">
                <svg
                  className="cf-modal-icon"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                  style={{ flexShrink: 0 }}
                >
                  <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
                  <circle cx="12" cy="10" r="3" />
                </svg>
                <span>{event.location}</span>
              </div>
            )
          ) : null}

          {/* Owners Chips */}
          {((event.owners && event.owners.length > 0) ||
            (event.initials && event.initials.length > 0)) && (
            <div className="cf-modal-owners">
              {Array.from({
                length: Math.max(event.owners?.length || 0, event.initials?.length || 0),
              }).map((_, i) => {
                const ownerName = event.owners?.[i] || '';
                const initial =
                  event.initials?.[i] || (ownerName ? ownerName.charAt(0).toUpperCase() : '');
                const color = event.colors?.[i] || event.color || 'var(--mm-accent-purple)';
                const pattern = event.patterns?.[i] || event.pattern || 'solid';
                return (
                  <span key={i} className={`cf-modal-owner-chip cf-pattern-${pattern}`}>
                    <span className="cf-modal-owner-badge" style={{ backgroundColor: color }}>
                      {initial}
                    </span>
                    {ownerName && <span className="cf-modal-owner-name">{ownerName}</span>}
                  </span>
                );
              })}
            </div>
          )}

          {/* Description */}
          {event.description && event.description.trim() ? (() => {
            const isMealie =
              event.location?.startsWith('/hearth-api/mealie/image/') ||
              event.title?.includes('\u{1F37D}');
            let desc = stripMealiePrefix(
              event.description
                .trim()
                .replace(/\\n/g, '\n')
                .replace(/\\,/g, ',')
                .replace(/\\;/g, ';')
            );
            if (isMealie) {
              desc = desc.replace(
                /(?:\u{1F37D}|\u{1F3F7}|\u{1F6D2}|\u{1F469}\u200D\u{1F373}|\u{1F9D1}\u200D\u{1F373}|\u{1F468}\u200D\u{1F373})\uFE0F?\s*/gu,
                ''
              );
            } else if (desc.length > 280) {
              desc = desc.slice(0, 277) + '...';
            }
            return (
              <div
                className="cf-modal-description"
                style={
                  isMealie
                    ? {
                        whiteSpace: 'pre-wrap',
                        maxHeight: '320px',
                        overflowY: 'auto',
                        fontSize: '0.86rem',
                        lineHeight: 1.45,
                        paddingRight: '6px',
                      }
                    : undefined
                }
              >
                {desc}
              </div>
            );
          })() : null}
        </div>
      </div>
    </ViewTransition>
  );
}
