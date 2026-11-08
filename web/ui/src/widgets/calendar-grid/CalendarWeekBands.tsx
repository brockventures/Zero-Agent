import type { ReactElement } from 'react';
import { formatEventTime, stripMealiePrefix } from './dateUtils';
import type { FamilyDay, FamilyDayEvent, FamilyEvent, FamilyMember, FamilySpanEvent, FamilyViewModel, TimelineMarker, ViewMode } from './types';

function handleBandEventKeyDown(e: React.KeyboardEvent, action: () => void) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    action();
  }
}

export type EventFlair = 'birthday' | 'party' | 'dentist' | 'movie' | 'flight' | 'sports' | 'holiday';

function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

const HOLIDAY_RE = /\b(veterans day|thanksgiving|christmas|new year|memorial day|labor day|independence day|4th of july|easter|halloween|valentine|mother's day|father's day|mlk|presidents day|juneteenth|holiday)\b/i;

export function detectEventFlair(title?: string): EventFlair | null {
  if (!title || typeof title !== 'string') return null;
  const t = title.trim();
  if (!t) return null;
  if (/\b(birthday|bday|b-day)\b/i.test(t)) return 'birthday';
  if (/\b(party|celebration)\b/i.test(t)) return 'party';
  if (/\b(dentist|orthodontist|dental|doctor|pediatric)\b/i.test(t)) return 'dentist';
  if (/\b(movie|cinema|film)\b/i.test(t)) return 'movie';
  if (/\b(flight|airline|airport|fly)\b/i.test(t)) return 'flight';
  if (/\b(soccer|practice|game|basketball|baseball)\b/i.test(t)) return 'sports';
  if (HOLIDAY_RE.test(t)) return 'holiday';
  return null;
}

function isWorkExcluded(ev: FamilyEvent): boolean {
  const title = ev.title || '';
  return Boolean(
    /\(work\)|\bcoworker\b|\bwork\b/i.test(title) ||
      ev.owners?.some((o) => /\(work\)|\bcoworker\b|\bwork\b/i.test(o)) ||
      (ev.description && /\bcoworker\b/i.test(ev.description)),
  );
}

function matchesFamilyMember(ev: FamilyEvent, safeMembers: FamilyMember[]): boolean {
  const title = ev.title || '';
  const evMember = (ev as unknown as { member?: string }).member?.toLowerCase();
  for (const m of safeMembers) {
    const name = m?.name?.trim();
    if (!name) continue;
    const lower = name.toLowerCase();
    if (new RegExp(`\\b${escapeRegExp(name)}('s)?\\b`, 'i').test(title)) return true;
    if (ev.owners?.some((o) => o.toLowerCase() === lower || o.toLowerCase().includes(lower))) return true;
    if (m.initial && ev.initials?.includes(m.initial)) return true;
    if (evMember && evMember === lower) return true;
  }
  return false;
}

export function detectDaySpecialStatus(dayEvents?: FamilyEvent[], members?: FamilyMember[]): 'birthday' | 'holiday' | null {
  if (!dayEvents || dayEvents.length === 0) return null;
  const safeMembers = members || [];
  for (const ev of dayEvents) {
    if (ev?.title && /\b(birthday|bday|b-day)\b/i.test(ev.title) && !isWorkExcluded(ev) && matchesFamilyMember(ev, safeMembers)) {
      return 'birthday';
    }
  }
  for (const ev of dayEvents) {
    if (!ev) continue;
    if (HOLIDAY_RE.test(ev.title || '')) return 'holiday';
    const cal = (ev as unknown as { calendar?: string; calendar_name?: string }).calendar ||
      (ev as unknown as { calendar?: string; calendar_name?: string }).calendar_name || '';
    if (cal && /holiday/i.test(cal)) return 'holiday';
  }
  return null;
}

export function renderEventFlairWatermark(flair: EventFlair): ReactElement | null {
  let inner: ReactElement;
  switch (flair) {
    case 'birthday':
    case 'party':
      inner = (
        <>
          <path d="M9 14c-2.76 0-5-2.69-5-6a5 5 0 0 1 10 0c0 3.31-2.24 6-5 6Z" fill="currentColor" fillOpacity="0.25" />
          <path d="m8 14-1 2h2l-1-2" />
          <path d="M8 16c-.5 2-2 3-1 5" />
          <path d="M15 12c2.21 0 4-2.24 4-5a4 4 0 0 0-8 0" strokeDasharray="2 2" />
        </>
      );
      break;
    case 'dentist':
      inner = <path d="M7 3C4.5 3 3 5 3 7.5c0 3 1.5 6 2 9.5.5 3 2 4.5 3.5 4.5 1 0 1.5-1 2-3 .5-2 1.5-2 2-2s1.5 0 2 2c.5 2 1 3 2 3 1.5 0 3-1.5 3.5-4.5.5-3.5 2-6.5 2-9.5C22 5 20.5 3 18 3c-2 0-3.5 1.5-5.5 1.5S9 3 7 3Z" fill="currentColor" fillOpacity="0.25" />;
      break;
    case 'movie':
      inner = (
        <>
          <path d="M7 10h10l-1.5 11h-7L7 10Z" fill="currentColor" fillOpacity="0.25" />
          <path d="M10 10v11M14 10v11" />
          <path d="M6 10c-.8-1.5.5-3.5 2-3.5 1 0 1.5.5 2 1 .5-.5 1-1 2-1s1.5.5 2 1c.5-.5 1-1 2-1 1.5 0 2.8 2 2 3.5" />
        </>
      );
      break;
    case 'flight':
      inner = <path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 20.5 3c-1-1-3-.5-4.5 1L12.5 7.5 4.3 5.7c-.7-.1-1.3.3-1.4 1-.1.5.2 1 .6 1.3l6 4.5-3 3-2.5-.5c-.4-.1-.8.1-1 .4-.3.4-.2.9.2 1.2l2.5 1.8 1.8 2.5c.3.4.8.5 1.2.2.3-.2.5-.6.4-1l-.5-2.5 3-3 4.5 6c.3.4.8.7 1.3.6.7-.1 1.1-.7 1-1.4l-1.7-8.2Z" fill="currentColor" fillOpacity="0.25" />;
      break;
    case 'sports':
      inner = (
        <>
          <circle cx="12" cy="12" r="9" fill="currentColor" fillOpacity="0.25" />
          <path d="m12 7 3.5 2.5v4L12 16l-3.5-2.5v-4L12 7Z" />
          <path d="M12 7V3M15.5 9.5l3.5-2M15.5 13.5l3.5 2M12 16v4M8.5 13.5l-3.5 2M8.5 9.5l-3.5-2" />
        </>
      );
      break;
    case 'holiday':
      inner = <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" fill="currentColor" fillOpacity="0.25" />;
      break;
    default:
      return null;
  }
  return (
    <span className="cf-event-flair-watermark" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{inner}</svg>
    </span>
  );
}

function MealiePillContent({ location, title }: { location?: string; title?: string }) {
  return (
    <>
      <img src={location} alt="" style={{ width: '26px', height: '26px', borderRadius: '5px', objectFit: 'cover', flexShrink: 0, boxShadow: '0 1px 3px rgba(0,0,0,0.3)' }} />
      <svg className="cf-mealie-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flexShrink: 0 }}>
        <path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2" />
        <path d="M7 2v20" />
        <path d="M21 15V2v0a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7" />
      </svg>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 800 }}>{stripMealiePrefix(title)}</span>
    </>
  );
}

export interface CalendarAllDayBandProps {
  activeView: ViewMode;
  weekVM?: FamilyViewModel;
  daysToRender: FamilyDay[];
  visibleAllDayEvents: FamilySpanEvent[];
  currentDateISO: string;
  marginLeft?: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
  eventFlair?: boolean;
}

export function CalendarAllDayBand({
  activeView, weekVM, daysToRender, visibleAllDayEvents, currentDateISO, marginLeft, onOpenModal, eventFlair = true,
}: CalendarAllDayBandProps) {
  if (visibleAllDayEvents && visibleAllDayEvents.length > 0) {
    return (
      <div
        className="cf-allday-band cf-week-allday-grid"
        style={{
          marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
          gridAutoFlow: 'dense', gap: '6px', paddingBottom: '6px', marginBottom: '6px', borderBottom: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))',
        }}
      >
        {visibleAllDayEvents.map((ev) => {
          const mealieImg = resolveMealieEventImage(ev);
          const isMealie = Boolean(mealieImg);
          const flair = eventFlair !== false && !isMealie ? detectEventFlair(ev.title) : null;
          const spanStartDay = weekVM?.days?.[ev.start_col - 1]?.date || ev.start || daysToRender[0]?.date || currentDateISO;
          const spanEndDay = ev.col_span > 1 ? weekVM?.days?.[Math.min((weekVM?.days?.length ?? 1) - 1, ev.start_col + ev.col_span - 2)]?.date || ev.end : ev.end;
          return (
            <div
              key={ev.id}
              className={`cf-event cf-allday cf-event-clickable${flair ? ` cf-flair-${flair}` : ''}${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
              role="button" tabIndex={0} aria-label={`${ev.title} (All Day)`}
              data-event-id={ev.id} data-event-title={ev.title} data-event-allday="true"
              data-event-location={ev.location} data-event-description={ev.description}
              data-mealie-decorated={isMealie ? 'true' : undefined} data-flair={flair || undefined}
              onClick={() => onOpenModal(ev, spanStartDay, spanEndDay)}
              onKeyDown={(e) => handleBandEventKeyDown(e, () => onOpenModal(ev, spanStartDay, spanEndDay))}
              style={{
                gridColumn: activeView === 'day' ? '1 / span 1' : `${ev.start_col} / span ${ev.col_span}`,
                fontSize: '0.68rem', fontWeight: 700, borderRadius: '6px', padding: '3px 6px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                color: ev.text_color, backgroundColor: ev.color, cursor: 'pointer', touchAction: 'manipulation',
                display: isMealie ? 'flex' : undefined, alignItems: isMealie ? 'center' : undefined, gap: isMealie ? '6px' : undefined, position: 'relative',
              }}
            >
              {isMealie ? <MealiePillContent location={mealieImg} title={ev.title} /> : stripMealiePrefix(ev.title)}
              {flair && renderEventFlairWatermark(flair)}
            </div>
          );
        })}
      </div>
    );
  }

  if (daysToRender.some((d) => d.all_day && d.all_day.length > 0)) {
    return (
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
              const mealieImg = resolveMealieEventImage(ev);
              const isMealie = Boolean(mealieImg);
              const flair = eventFlair !== false && !isMealie ? detectEventFlair(ev.title) : null;
              return (
                <div
                  key={ev.id} className="cf-edge-band cf-allday-band-local"
                  style={{
                    display: 'flex', flexDirection: 'column', gap: '2px', padding: '2px 4px', background: 'rgba(90, 60, 30, 0.05)',
                    borderRadius: '6px', border: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0,
                  }}
                >
                  <div
                    className={`cf-event cf-allday cf-event-clickable${flair ? ` cf-flair-${flair}` : ''}${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                    role="button" tabIndex={0} title={ev.title} aria-label={`${ev.title} (All Day)`}
                    data-event-id={ev.id} data-event-title={ev.title} data-event-allday="true"
                    data-event-location={ev.location} data-event-description={ev.description}
                    data-mealie-decorated={isMealie ? 'true' : undefined} data-flair={flair || undefined}
                    onClick={() => onOpenModal(ev, day.date)}
                    onKeyDown={(e) => handleBandEventKeyDown(e, () => onOpenModal(ev, day.date))}
                    style={{
                      fontSize: '0.65rem', fontWeight: 700, borderRadius: '4px', padding: '2px 4px', color: ev.text_color, backgroundColor: ev.color,
                      cursor: 'pointer', touchAction: 'manipulation', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      display: isMealie ? 'flex' : undefined, alignItems: isMealie ? 'center' : undefined, gap: isMealie ? '6px' : undefined, position: 'relative',
                    }}
                  >
                    {isMealie ? <MealiePillContent location={mealieImg} title={ev.title} /> : stripMealiePrefix(ev.title)}
                    {flair && renderEventFlairWatermark(flair)}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    );
  }

  return null;
}

interface CalendarEdgeEventsBandProps {
  edge: 'early' | 'late';
  daysToRender: FamilyDay[];
  inferredTimeFormat?: string;
  marginLeft?: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
}

export function CalendarEdgeEventsBand({ edge, daysToRender, inferredTimeFormat, marginLeft, onOpenModal }: CalendarEdgeEventsBandProps) {
  const isEarly = edge === 'early';
  const hasEvents = daysToRender.some((d) => {
    const list = isEarly ? d.early_events : d.late_events;
    return list && list.length > 0;
  });

  if (!hasEvents) return null;

  const gridClass = isEarly ? 'cf-week-early-grid' : 'cf-week-late-grid';
  const colClass = isEarly ? 'cf-day-early-col' : 'cf-day-late-col';
  const bandClass = isEarly ? 'cf-early-band' : 'cf-late-band';
  const prefixLabel = isEarly ? 'Early event' : 'Late event';
  const glyph = isEarly ? '▲' : '▼';

  return (
    <div
      className={`cf-edge-band ${gridClass}`}
      style={{
        marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`, gap: '6px',
        paddingBottom: isEarly ? '4px' : undefined, marginBottom: isEarly ? '4px' : undefined, borderBottom: isEarly ? '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))' : undefined,
        paddingTop: !isEarly ? '4px' : undefined, marginTop: !isEarly ? '4px' : undefined, borderTop: !isEarly ? '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.1))' : undefined,
      }}
    >
      {daysToRender.map((day) => {
        const events: FamilyDayEvent[] | undefined = isEarly ? day.early_events : day.late_events;
        return (
          <div key={day.date} className={colClass} style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
            {events?.map((ev) => {
              const startDisplay = formatEventTime(ev.start, inferredTimeFormat) || ev.start;
              return (
                <div
                  key={ev.id} className={`cf-edge-band ${bandClass}`}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: '2px', padding: '2px 4px', background: 'rgba(90, 60, 30, 0.05)',
                    borderRadius: '6px', border: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0,
                  }}
                >
                  <div
                    className={`cf-edge-event cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
                    role="button" tabIndex={0}
                    title={`${prefixLabel}: ${startDisplay} ${ev.title}${ev.location ? ` (${ev.location})` : ''}`}
                    aria-label={`${prefixLabel}: ${ev.title} at ${startDisplay}`}
                    data-event-id={ev.id} data-event-title={ev.title} data-event-start={ev.start}
                    data-event-end={ev.end} data-event-location={ev.location} data-event-description={ev.description}
                    onClick={() => onOpenModal(ev, day.date)}
                    onKeyDown={(e) => handleBandEventKeyDown(e, () => onOpenModal(ev, day.date))}
                    style={{
                      fontSize: '0.60rem', fontWeight: 700, padding: '2px 4px', borderRadius: '4px', backgroundColor: ev.color, color: ev.text_color,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', cursor: 'pointer', touchAction: 'manipulation',
                    }}
                  >{glyph} {startDisplay} {ev.title}</div>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

export function CalendarTimelineAxis({ weekVM }: { weekVM?: FamilyViewModel }) {
  return (
    <div className="cf-timeline-axis" style={{ width: '38px', display: 'flex', flexDirection: 'column', flexShrink: 0, userSelect: 'none' }}>
      <div className="cf-timeline-spacer" style={{ height: '44px', flexShrink: 0 }} />
      <div
        className="cf-timeline-markers"
        style={{
          flex: 1, position: 'relative', minHeight: 0, marginBottom: '6px',
          fontFamily: 'var(--mm-font-mono, monospace)', fontSize: '0.60rem', fontWeight: 700, color: 'var(--mm-text-dim, #8b819f)',
        }}
      >
        {weekVM?.timeline_markers && weekVM.timeline_markers.length > 0 ? (
          weekVM.timeline_markers.map((marker: TimelineMarker, idx: number) => (
            <span
              key={idx} className="cf-hour-marker"
              style={{
                position: 'absolute', right: '6px', top: `${marker.top_pct}%`,
                transform: marker.is_start ? 'translateY(0)' : marker.is_end ? 'translateY(-100%)' : 'translateY(-50%)',
                lineHeight: 1, whiteSpace: 'nowrap', pointerEvents: 'none',
              }}
            >{marker.label}</span>
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
  );
}

export interface CalendarDayEventCardProps {
  ev: FamilyDayEvent;
  dayDate: string;
  inferredTimeFormat?: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
  eventFlair?: boolean;
}

export function CalendarDayEventCard({ ev, dayDate, inferredTimeFormat, onOpenModal, eventFlair = true }: CalendarDayEventCardProps) {
  const startDisplay = formatEventTime(ev.start, inferredTimeFormat) || ev.start;
  const endDisplay = formatEventTime(ev.end, inferredTimeFormat) || ev.end;
  const flair = eventFlair !== false ? detectEventFlair(ev.title) : null;
  return (
    <div
      className={`cf-day-event cf-event-clickable${flair ? ` cf-flair-${flair}` : ''}${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
      role="button" tabIndex={0}
      title={`${startDisplay}–${endDisplay}: ${ev.title}${ev.location ? ` (${ev.location})` : ''}`}
      aria-label={`${ev.title}, ${startDisplay} to ${endDisplay}${ev.location ? `, ${ev.location}` : ''}`}
      data-event-id={ev.id} data-event-title={ev.title} data-event-start={ev.start}
      data-event-end={ev.end} data-event-location={ev.location} data-event-description={ev.description} data-flair={flair || undefined}
      onClick={() => onOpenModal(ev, dayDate)}
      onKeyDown={(e) => handleBandEventKeyDown(e, () => onOpenModal(ev, dayDate))}
      style={{
        position: 'absolute', top: `${ev.top_pct}%`, height: `${ev.height_pct}%`,
        left: `calc(${ev.left_pct}% + 1px)`, width: `calc(${ev.width_pct}% - 2px)`,
        minHeight: '20px', padding: '2px 4px', borderRadius: '6px', backgroundColor: ev.color, color: ev.text_color,
        overflow: 'hidden', display: 'flex', flexDirection: 'column', boxSizing: 'border-box',
        border: '1px solid rgba(0, 0, 0, 0.12)', boxShadow: '0 1px 3px rgba(0, 0, 0, 0.12)', cursor: 'pointer', touchAction: 'manipulation',
      }}
    >
      <div className="cf-event-header" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '2px', fontSize: '0.60rem', fontWeight: 800, lineHeight: 1.1, flexShrink: 0 }}>
        <span className="cf-event-time" style={{ fontFamily: 'var(--mm-font-mono, monospace)' }}>{startDisplay}</span>
        {ev.initials && ev.initials.length > 0 && <span className="cf-event-initials" style={{ opacity: 0.85 }}>{ev.initials.join('')}</span>}
      </div>
      <div className="cf-event-title" style={{ fontSize: '0.65rem', fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1.2, flexShrink: 0 }}>{ev.title}</div>
      {ev.location && <div className="cf-event-location" style={{ fontSize: '0.55rem', opacity: 0.85, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{ev.location}</div>}
      {flair && renderEventFlairWatermark(flair)}
    </div>
  );
}

export function isMealieImageSource(src?: string): boolean {
  if (!src) return false;
  const s = src.trim();
  return s.startsWith('/hearth-api/mealie/image/') || s.includes('/api/media/recipes/') || s.startsWith('data:image/');
}

export function resolveMealieEventImage(ev: FamilyEvent): string | undefined {
  const imgUrl = ev.image_url?.trim();
  if (isMealieImageSource(imgUrl)) return imgUrl;
  const loc = ev.location?.trim();
  if (isMealieImageSource(loc)) return loc;
  return undefined;
}

interface CalendarDayMealBarProps {
  meals?: FamilyEvent[];
  dayDate: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
}

function CalendarDayMealBar({ meals, dayDate, onOpenModal }: CalendarDayMealBarProps) {
  if (!meals || meals.length === 0) return null;
  return (
    <div className="cf-day-meal-bar cf-day-meal-col" data-date={dayDate} style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0, flexShrink: 0 }}>
      {meals.map((ev) => {
        const imgSrc = resolveMealieEventImage(ev);
        const cleanTitle = stripMealiePrefix(ev.title);
        return (
          <div
            key={ev.id}
            className={`cf-event cf-mealie-bar cf-mealie-card cf-event-clickable${ev.pattern ? ` cf-pattern-${ev.pattern}` : ''}`}
            role="button" tabIndex={0} title={ev.title} aria-label={ev.title}
            data-event-id={ev.id} data-event-title={ev.title} data-event-allday="true"
            data-event-location={ev.location} data-event-image-url={imgSrc}
            data-event-description={ev.description} data-mealie-decorated="true"
            onClick={() => onOpenModal(ev, dayDate)}
            onKeyDown={(e) => handleBandEventKeyDown(e, () => onOpenModal(ev, dayDate))}
            style={{
              position: 'relative', minHeight: '128px', height: '128px', borderRadius: '12px',
              overflow: 'hidden', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
              backgroundColor: '#d97757', color: '#ffffff', cursor: 'pointer', touchAction: 'manipulation', boxShadow: '0 2px 6px rgba(0, 0, 0, 0.14)',
            }}
          >
            {imgSrc && <img src={imgSrc} alt="" className="cf-mealie-photo" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', display: 'block', zIndex: 0 }} />}
            <span
              className="cf-mealie-pill"
              style={{
                position: 'absolute', top: '6px', left: '6px', zIndex: 2, padding: '2px 7px', borderRadius: '9999px', fontSize: '0.56rem', fontWeight: 800,
                letterSpacing: '0.02em', color: '#ffffff', background: 'rgba(16,12,10,0.52)', backdropFilter: 'blur(6px)', WebkitBackdropFilter: 'blur(6px)', lineHeight: 1.2, pointerEvents: 'none',
              }}
            >🍽️ Dinner</span>
            <div
              className="cf-mealie-scrim"
              style={{
                position: 'absolute', left: 0, right: 0, bottom: 0, zIndex: 1, padding: '20px 8px 7px',
                background: 'linear-gradient(to top, rgba(16,12,10,0.90) 0%, rgba(16,12,10,0.62) 55%, rgba(16,12,10,0.0) 100%)', display: 'flex', alignItems: 'flex-end', pointerEvents: 'none',
              }}
            >
              <span
                className="cf-mealie-title"
                style={{
                  fontSize: '0.70rem', fontWeight: 800, lineHeight: 1.2, color: '#ffffff', display: '-webkit-box',
                  WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden', wordBreak: 'break-word', textShadow: '0 1px 2px rgba(0,0,0,0.6)',
                }}
              >{cleanTitle}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface CalendarWeekMealRowProps {
  daysToRender: FamilyDay[];
  marginLeft?: string;
  onOpenModal: (ev: FamilyEvent, startDate?: string, endDate?: string) => void;
}

export function CalendarWeekMealRow({ daysToRender, marginLeft, onOpenModal }: CalendarWeekMealRowProps) {
  const hasMeals = daysToRender.some((d) => d.meals && d.meals.length > 0);
  if (!hasMeals) return null;
  return (
    <div
      className="cf-week-meal-row"
      style={{
        marginLeft, display: 'grid', gridTemplateColumns: `repeat(${daysToRender.length}, 1fr)`,
        gap: '6px', paddingTop: '6px', marginTop: '6px', borderTop: '1px dashed var(--mm-divider, rgba(90, 60, 30, 0.12))', flexShrink: 0,
      }}
    >
      {daysToRender.map((day) => (
        <div key={day.date} className="cf-week-meal-cell" data-date={day.date} style={{ display: 'flex', flexDirection: 'column', gap: '4px', minWidth: 0 }}>
          <CalendarDayMealBar meals={day.meals} dayDate={day.date} onOpenModal={onOpenModal} />
        </div>
      ))}
    </div>
  );
}
