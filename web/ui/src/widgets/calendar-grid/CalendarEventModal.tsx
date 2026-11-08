import { useEffect, ViewTransition, type ReactElement } from 'react';
import {
  detectEventFlair,
  isMealieImageSource,
  resolveMealieEventImage,
  type EventFlair,
} from './CalendarWeekBands';
import { stripMealiePrefix } from './dateUtils';
import type { FamilyEvent } from './types';

export interface CalendarEventModalProps {
  event: FamilyEvent | null;
  dateLabel: string;
  onClose: () => void;
  eventFlair?: boolean;
}

function renderModalFlairBanner(flair: EventFlair): ReactElement | null {
  switch (flair) {
    case 'birthday':
    case 'party':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #ec4899 0%, #a855f7 50%, #6366f1 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <defs>
              <linearGradient id="bgB1" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#f472b6" /><stop offset="100%" stopColor="#db2777" /></linearGradient>
              <linearGradient id="bgB2" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#38bdf8" /><stop offset="100%" stopColor="#0284c7" /></linearGradient>
              <linearGradient id="bgB3" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#facc15" /><stop offset="100%" stopColor="#eab308" /></linearGradient>
            </defs>
            <circle cx="30" cy="30" r="3" fill="#ffffff" opacity="0.8" />
            <circle cx="80" cy="20" r="2.5" fill="#fde047" opacity="0.9" />
            <circle cx="140" cy="40" r="3.5" fill="#67e8f9" opacity="0.75" />
            <circle cx="280" cy="25" r="3" fill="#fde047" opacity="0.85" />
            <circle cx="340" cy="35" r="2.5" fill="#ffffff" opacity="0.8" />
            <circle cx="370" cy="70" r="3" fill="#f472b6" opacity="0.9" />
            <path d="M50 70 L52 76 L58 77 L53 81 L55 87 L50 83 L45 87 L47 81 L42 77 L48 76 Z" fill="#ffffff" opacity="0.75" />
            <path d="M320 80 L322 84 L326 85 L323 88 L324 92 L320 89 L316 92 L317 88 L314 85 L318 84 Z" fill="#fde047" opacity="0.8" />
            <path d="M185 85 Q 180 105 170 120" stroke="rgba(255,255,255,0.6)" strokeWidth="1.5" fill="none" />
            <path d="M200 90 Q 200 110 205 120" stroke="rgba(255,255,255,0.6)" strokeWidth="1.5" fill="none" />
            <path d="M215 85 Q 220 105 230 120" stroke="rgba(255,255,255,0.6)" strokeWidth="1.5" fill="none" />
            <ellipse cx="178" cy="52" rx="20" ry="26" fill="url(#bgB2)" />
            <ellipse cx="173" cy="44" rx="4" ry="7" fill="#ffffff" opacity="0.4" transform="rotate(-15 173 44)" />
            <polygon points="175,77 181,77 178,82" fill="#0284c7" />
            <ellipse cx="222" cy="52" rx="20" ry="26" fill="url(#bgB3)" />
            <ellipse cx="217" cy="44" rx="4" ry="7" fill="#ffffff" opacity="0.4" transform="rotate(-15 217 44)" />
            <polygon points="219,77 225,77 222,82" fill="#ca8a04" />
            <ellipse cx="200" cy="45" rx="22" ry="28" fill="url(#bgB1)" />
            <ellipse cx="194" cy="36" rx="5" ry="8" fill="#ffffff" opacity="0.5" transform="rotate(-15 194 36)" />
            <polygon points="197,72 203,72 200,77" fill="#be185d" />
          </svg>
        </div>
      );
    case 'dentist':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #06b6d4 0%, #3b82f6 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <circle cx="80" cy="40" r="14" fill="#ffffff" opacity="0.15" />
            <circle cx="120" cy="80" r="8" fill="#ffffff" opacity="0.2" />
            <circle cx="280" cy="35" r="10" fill="#ffffff" opacity="0.15" />
            <circle cx="320" cy="75" r="16" fill="#ffffff" opacity="0.2" />
            <path d="M150 40 L152 46 L158 48 L152 50 L150 56 L148 50 L142 48 L148 46 Z" fill="#ffffff" opacity="0.9" />
            <path d="M245 35 L247 40 L252 41 L247 43 L245 48 L243 43 L238 41 L243 40 Z" fill="#ffffff" opacity="0.9" />
            <path d="M235 80 L236 83 L239 84 L236 85 L235 88 L234 85 L231 84 L234 83 Z" fill="#ffffff" opacity="0.8" />
            <path d="M190 28 C 172 28 165 42 165 56 C 165 72 172 88 178 102 C 182 110 188 108 192 98 C 196 88 198 88 200 88 C 202 88 204 88 208 98 C 212 108 218 110 222 102 C 228 88 235 72 235 56 C 235 42 228 28 210 28 C 204 28 202 33 200 33 C 198 33 196 28 190 28 Z" fill="#ffffff" />
            <path d="M182 45 C 178 50 178 60 180 66" stroke="#06b6d4" strokeWidth="2.5" strokeLinecap="round" fill="none" opacity="0.6" />
          </svg>
        </div>
      );
    case 'movie':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #831843 0%, #581c87 50%, #1e1b4b 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <circle cx="60" cy="30" r="2.5" fill="#fde047" opacity="0.8" />
            <circle cx="120" cy="50" r="3" fill="#ffffff" opacity="0.7" />
            <circle cx="280" cy="40" r="2" fill="#ffffff" opacity="0.8" />
            <circle cx="340" cy="25" r="3.5" fill="#fde047" opacity="0.9" />
            <g transform="translate(170, 20)">
              <circle cx="20" cy="22" r="9" fill="#fef08a" />
              <circle cx="34" cy="18" r="10" fill="#facc15" />
              <circle cx="48" cy="24" r="9" fill="#fef08a" />
              <circle cx="12" cy="30" r="8" fill="#fde047" />
              <circle cx="28" cy="28" r="9" fill="#ffffff" />
              <circle cx="44" cy="32" r="8" fill="#facc15" />
              <circle cx="56" cy="30" r="7" fill="#fde047" />
              <polygon points="12,38 56,38 48,88 20,88" fill="#e11d48" />
              <polygon points="19,38 27,38 25,88 21,88" fill="#ffffff" />
              <polygon points="33,38 41,38 39,88 35,88" fill="#ffffff" />
            </g>
          </svg>
        </div>
      );
    case 'flight':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #0284c7 0%, #1e40af 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <path d="M40 90 A 18 18 0 0 1 70 80 A 24 24 0 0 1 110 85 A 14 14 0 0 1 125 95 Z" fill="#ffffff" opacity="0.25" />
            <path d="M260 85 A 16 16 0 0 1 290 75 A 22 22 0 0 1 325 80 A 16 16 0 0 1 345 92 Z" fill="#ffffff" opacity="0.3" />
            <path d="M80 85 Q 140 65 190 55" stroke="#ffffff" strokeWidth="2.5" strokeDasharray="6 4" strokeLinecap="round" fill="none" opacity="0.65" />
            <g transform="translate(185, 25) rotate(-15)">
              <path d="M35 15 L12 2 L8 2 L14 15 L2 15 L0 12 L-4 12 L-2 18 L-4 24 L0 24 L2 21 L14 21 L8 34 L12 34 L35 21 C42 19 42 17 35 15 Z" fill="#ffffff" />
            </g>
          </svg>
        </div>
      );
    case 'sports':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #15803d 0%, #047857 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <path d="M40 100 L360 100" stroke="#ffffff" strokeWidth="1.5" opacity="0.3" />
            <path d="M200 60 L200 100" stroke="#ffffff" strokeWidth="1.5" opacity="0.25" />
            <polygon points="120,30 123,37 130,37 124,42 126,49 120,45 114,49 116,42 110,37 117,37" fill="#fde047" opacity="0.8" />
            <polygon points="280,30 283,37 290,37 284,42 286,49 280,45 274,49 276,42 270,37 277,37" fill="#fde047" opacity="0.8" />
            <g transform="translate(175, 25)">
              <circle cx="25" cy="25" r="24" fill="#ffffff" />
              <polygon points="25,12 34,18 30,28 20,28 16,18" fill="#1e293b" />
              <line x1="25" y1="12" x2="25" y2="2" stroke="#1e293b" strokeWidth="2" />
              <line x1="34" y1="18" x2="44" y2="14" stroke="#1e293b" strokeWidth="2" />
              <line x1="30" y1="28" x2="38" y2="36" stroke="#1e293b" strokeWidth="2" />
              <line x1="20" y1="28" x2="12" y2="36" stroke="#1e293b" strokeWidth="2" />
              <line x1="16" y1="18" x2="6" y2="14" stroke="#1e293b" strokeWidth="2" />
            </g>
          </svg>
        </div>
      );
    case 'holiday':
      return (
        <div
          className={`cf-modal-flair-banner cf-modal-flair-${flair}`}
          data-flair={flair}
          aria-hidden="true"
          style={{
            width: '100%',
            height: '100px',
            borderRadius: '8px',
            overflow: 'hidden',
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            marginBottom: '8px',
            background: 'linear-gradient(135deg, #b45309 0%, #d97706 50%, #f59e0b 100%)',
          }}
        >
          <svg viewBox="0 0 400 120" style={{ width: '100%', height: '100%' }} preserveAspectRatio="xMidYMid slice">
            <circle cx="60" cy="40" r="3" fill="#ffffff" opacity="0.8" />
            <circle cx="100" cy="20" r="2" fill="#ffffff" opacity="0.9" />
            <circle cx="300" cy="30" r="3.5" fill="#ffffff" opacity="0.85" />
            <circle cx="350" cy="50" r="2" fill="#ffffff" opacity="0.75" />
            <path d="M200 20 L203 35 L218 38 L203 41 L200 56 L197 41 L182 38 L197 35 Z" fill="#ffffff" />
            <path d="M150 45 L152 54 L161 56 L152 58 L150 67 L148 58 L139 56 L148 54 Z" fill="#fde047" opacity="0.9" />
            <path d="M250 45 L252 54 L261 56 L252 58 L250 67 L248 58 L239 56 L248 54 Z" fill="#fde047" opacity="0.9" />
          </svg>
        </div>
      );
    default:
      return null;
  }
}

export function CalendarEventModal({
  event,
  dateLabel,
  onClose,
  eventFlair = true,
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
  const heroImg = resolveMealieEventImage(event);
  const flair = !heroImg && eventFlair !== false ? detectEventFlair(event.title) : null;

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
          {flair && renderModalFlairBanner(flair)}
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
          {(() => {
            const heroImg = resolveMealieEventImage(event);
            if (heroImg) {
              return (
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
                    src={heroImg}
                    alt={modalTitle}
                    style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                  />
                </div>
              );
            }
            if (event.location && event.location.trim() && !isMealieImageSource(event.location)) {
              return (
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
              );
            }
            return null;
          })()}

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
              Boolean(resolveMealieEventImage(event)) ||
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
