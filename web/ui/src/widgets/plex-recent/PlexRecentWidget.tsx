import './PlexRecentWidget.css';
import { memo, useEffect, useMemo, useRef, useState, ViewTransition } from 'react';
import { useIsCurrentPane } from '../../carousel/PaneContext';
import { useDocumentVisible } from '../../lifecycle/visibility';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';

export interface PlexItem {
  id: string | number;
  title: string;
  subtitle?: string;
  year?: string | number;
  category?: string; // 'movie' | 'tv' | 'audiobook' | 'upcoming'
  type?: string;
  badge?: string;
  rating?: number | string;
  thumb?: string;
  summary?: string;
}

export interface SeerrResultItem {
  id?: string | number;
  tmdb_id: string | number;
  media_type: string;
  title: string;
  year?: string | number;
  thumb?: string;
  status?: string;
}

type ReqStatus = 'requesting' | 'requested' | 'error';

const CATEGORIES: { key: string; label: string }[] = [
  { key: 'all', label: 'All Recent' },
  { key: 'movie', label: 'Movies' },
  { key: 'tv', label: 'TV Shows' },
  { key: 'audiobook', label: 'Audiobooks' },
  { key: 'upcoming', label: 'Dropping Soon' },
];

function formatDetailSub(item: PlexItem): string {
  const base = item.subtitle || (item.year !== undefined && item.year !== null ? String(item.year) : '');
  const numRating =
    item.rating !== undefined && item.rating !== null && item.rating !== ''
      ? Number(item.rating)
      : null;
  const ratingTxt = numRating !== null && !isNaN(numRating) ? ` · ★ ${numRating.toFixed(1)}` : '';
  return `${base}${ratingTxt}`.trim();
}

function formatBadge(item: PlexItem): string {
  if (item.badge) return String(item.badge).toUpperCase();
  if (item.category === 'audiobook') return 'BOOK';
  if (item.category === 'tv') return 'TV';
  return 'MOVIE';
}

function TvIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ verticalAlign: 'middle', marginRight: '6px' }}>
      <rect x="2" y="7" width="20" height="15" rx="2" ry="2" />
      <polyline points="17 2 12 7 7 2" />
    </svg>
  );
}

function HourglassIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ verticalAlign: 'middle', marginRight: '6px' }}>
      <path d="M5 22h14M5 2h14M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2" />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ verticalAlign: 'middle', marginRight: '6px' }}>
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/**
 * Native React widget for Plex recent additions, upcoming releases, and Seerr media requests.
 * Ports web/static/plex_recent.html.
 */
export const PlexRecentWidget = memo(function PlexRecentWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const rawApiBase = config?.api_base ?? config?.apiBase;
  const apiBase =
    typeof rawApiBase === 'string' && rawApiBase.trim() !== ''
      ? rawApiBase.trim().replace(/\/+$/, '')
      : '/hearth-api';

  const isCurrentPane = useIsCurrentPane();
  const isDocumentVisible = useDocumentVisible();
  const isActive = isCurrentPane && isDocumentVisible;

  const [recentItems, setRecentItems] = useState<PlexItem[]>([]);
  const [upcomingItems, setUpcomingItems] = useState<PlexItem[]>([]);
  const [activeCat, setActiveCat] = useState<string>('all');
  const [selectedItem, setSelectedItem] = useState<PlexItem | null>(null);
  const [tvPlayState, setTvPlayState] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');

  const [isSeerrOpen, setIsSeerrOpen] = useState(false);
  const [seerrQuery, setSeerrQuery] = useState('');
  const [seerrResults, setSeerrResults] = useState<SeerrResultItem[]>([]);
  const [seerrSearching, setSeerrSearching] = useState(false);
  const [seerrSearchError, setSeerrSearchError] = useState(false);
  const [reqStatusMap, setReqStatusMap] = useState<Record<string | number, ReqStatus>>({});

  const seerrInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!isActive) return;

    let cancelled = false;

    const loadData = async () => {
      try {
        const [recentRes, upcomingRes] = await Promise.all([
          fetch(`${apiBase}/plex/recent`)
            .then((r) => (r.ok ? r.json() : { items: [] }))
            .catch(() => ({ items: [] })),
          fetch(`${apiBase}/plex/upcoming`)
            .then((r) => (r.ok ? r.json() : { items: [] }))
            .catch(() => ({ items: [] })),
        ]);
        if (!cancelled) {
          const nextRecent = Array.isArray(recentRes?.items) ? recentRes.items : [];
          const nextUpcoming = Array.isArray(upcomingRes?.items) ? upcomingRes.items : [];
          setRecentItems((prev) => (JSON.stringify(prev) === JSON.stringify(nextRecent) ? prev : nextRecent));
          setUpcomingItems((prev) => (JSON.stringify(prev) === JSON.stringify(nextUpcoming) ? prev : nextUpcoming));
        }
      } catch {
        // preserve current list on network error
      }
    };

    void loadData();

    const intervalId = window.setInterval(() => {
      void loadData();
    }, 180000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [isActive, apiBase]);

  useEffect(() => {
    if (isSeerrOpen) {
      const timer = window.setTimeout(() => {
        seerrInputRef.current?.focus();
      }, 50);
      return () => window.clearTimeout(timer);
    }
  }, [isSeerrOpen]);

  const displayedItems = useMemo(() => {
    if (activeCat === 'upcoming') {
      return upcomingItems;
    }
    if (activeCat === 'all') {
      return recentItems;
    }
    return recentItems.filter((i) => i.category === activeCat);
  }, [activeCat, recentItems, upcomingItems]);

  const handleTvPlay = async () => {
    if (!selectedItem) return;
    setTvPlayState('loading');
    try {
      const res = await fetch(`${apiBase}/plex/play`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rating_key: selectedItem.id,
          title: selectedItem.title,
          type: selectedItem.type || selectedItem.category,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data?.ok !== false) {
        setTvPlayState('success');
      } else {
        setTvPlayState('error');
      }
    } catch {
      setTvPlayState('error');
    }
  };

  const handleSeerrSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = seerrQuery.trim();
    if (!q) return;
    setSeerrSearching(true);
    setSeerrSearchError(false);
    try {
      const res = await fetch(`${apiBase}/seerr/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      setSeerrResults(Array.isArray(data?.items) ? data.items : []);
      setSeerrSearching(false);
    } catch {
      setSeerrSearching(false);
      setSeerrSearchError(true);
      setSeerrResults([]);
    }
  };

  const handleSeerrRequest = async (item: SeerrResultItem) => {
    const key = item.tmdb_id;
    setReqStatusMap((prev) => ({ ...prev, [key]: 'requesting' as const }));
    try {
      const res = await fetch(`${apiBase}/seerr/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          media_type: item.media_type,
          tmdb_id: Number(item.tmdb_id),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data?.ok !== false) {
        setReqStatusMap((prev) => ({ ...prev, [key]: 'requested' as const }));
      } else {
        setReqStatusMap((prev) => ({ ...prev, [key]: 'error' as const }));
      }
    } catch {
      setReqStatusMap((prev) => ({ ...prev, [key]: 'error' as const }));
    }
  };

  let playBtnContent: React.ReactNode = (
    <>
      <TvIcon />
      Play on Living Room TV
    </>
  );
  let playBtnAriaLabel = 'Play on Living Room TV';
  if (tvPlayState === 'loading') {
    playBtnContent = (
      <>
        <HourglassIcon />
        Waking Living Room TV...
      </>
    );
    playBtnAriaLabel = 'Waking Living Room TV';
  } else if (tvPlayState === 'success') {
    playBtnContent = '✓ Launching on Living Room TV';
    playBtnAriaLabel = 'Launching on Living Room TV';
  } else if (tvPlayState === 'error') {
    playBtnContent = (
      <>
        <WarningIcon />
        Launch Error
      </>
    );
    playBtnAriaLabel = 'Launch Error';
  }

  return (
    <div className="widget-plex-recent">
      <div className="toolbar">
        <div className="tabs" data-prevent-screen-swipe="">
          {CATEGORIES.map((cat) => (
            <button
              key={cat.key}
              type="button"
              className={`tab-btn ${activeCat === cat.key ? 'active' : ''}`}
              data-cat={cat.key}
              aria-label={cat.label}
              onClick={() => setActiveCat(cat.key)}
            >
              {cat.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="req-open-btn"
          id="open-seerr-btn"
          aria-label="Request media"
          onClick={() => setIsSeerrOpen(true)}
        >
          + Request
        </button>
      </div>

      <div className="plex-poster-grid poster-grid" id="poster-grid">
        {displayedItems.map((item, idx) => {
          const badge = formatBadge(item);
          const numRating =
            item.rating !== undefined && item.rating !== null && item.rating !== ''
              ? Number(item.rating)
              : null;
          const ratingStr = numRating !== null && !isNaN(numRating) ? numRating.toFixed(1) : null;

          return (
            <div
              key={`${item.id}-${idx}`}
              className="item-card"
              role="button"
              tabIndex={0}
              aria-label={item.title}
              data-idx={idx}
              onClick={() => {
                setSelectedItem(item);
                setTvPlayState('idle');
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  setSelectedItem(item);
                  setTvPlayState('idle');
                }
              }}
            >
              <div className="poster-wrap">
                {item.thumb ? <img src={item.thumb} loading="lazy" alt="" /> : null}
                <span className="type-pill">{badge}</span>
                {ratingStr ? <span className="rating-pill">★ {ratingStr}</span> : null}
              </div>
              <div className="meta">
                <div className="title">{item.title}</div>
                <div className="sub">{item.subtitle || ''}</div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Detail Popover + Play on Living Room TV */}
      <ViewTransition default="none">
        <div className={`overlay-modal ${selectedItem ? 'open' : ''}`} id="detail-modal" data-prevent-screen-swipe="">
          {selectedItem && (
            <div className="detail-content">
              <img
                className="detail-poster"
                id="detail-poster"
                src={selectedItem.thumb || ''}
                alt=""
              />
              <div className="detail-info">
                <div className="detail-header">
                  <div>
                    <div className="detail-title" id="detail-title">
                      {selectedItem.title || 'Untitled'}
                    </div>
                    <div className="detail-sub" id="detail-sub">
                      {formatDetailSub(selectedItem)}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="seerr-btn seerr-close"
                    id="close-detail-btn"
                    aria-label="Close details"
                    onClick={() => setSelectedItem(null)}
                  >
                    ✕
                  </button>
                </div>
                <div className="detail-summary" id="detail-summary">
                  {selectedItem.summary || 'No summary available.'}
                </div>
                {selectedItem.category !== 'upcoming' && selectedItem.category !== 'audiobook' && (
                  <div className="detail-actions">
                    <button
                      type="button"
                      className="tv-play-btn"
                      id="tv-play-btn"
                      aria-label={playBtnAriaLabel}
                      disabled={tvPlayState === 'loading' || tvPlayState === 'success'}
                      onClick={handleTvPlay}
                    >
                      {playBtnContent}
                    </button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </ViewTransition>

      {/* Seerr Request Modal */}
      <ViewTransition default="none">
        <div className={`overlay-modal ${isSeerrOpen ? 'open' : ''}`} id="seerr-modal" data-prevent-screen-swipe="">
          <form className="seerr-bar" id="seerr-form" onSubmit={handleSeerrSearch}>
            <input
              ref={seerrInputRef}
              type="text"
              className="seerr-input" data-prevent-screen-swipe=""
              id="seerr-input"
              value={seerrQuery}
              onChange={(e) => setSeerrQuery(e.target.value)}
              placeholder="Search Seerr for a movie or TV show..."
              autoComplete="off"
            />
            <button type="submit" className="seerr-btn" aria-label="Search Seerr">
              Search
            </button>
            <button
              type="button"
              className="seerr-btn seerr-close"
              id="close-seerr-btn"
              aria-label="Close search"
              onClick={() => setIsSeerrOpen(false)}
            >
              Close
            </button>
          </form>
          <div className="plex-poster-grid poster-grid" id="seerr-results">
            {seerrSearching && (
              <div style={{ fontSize: '0.8rem', opacity: 0.7 }}>Searching Seerr...</div>
            )}
            {seerrSearchError && (
              <div style={{ fontSize: '0.8rem', color: '#ef4444' }}>Search failed</div>
            )}
            {!seerrSearching &&
              !seerrSearchError &&
              seerrResults.map((item, idx) => {
                const reqStatus = reqStatusMap[item.tmdb_id];
                const isDone =
                  item.status === 'available' ||
                  item.status === 'requested' ||
                  reqStatus === 'requested';
                const isDisabled = isDone || reqStatus === 'requesting';

                let btnLabel = '+ Request';
                if (item.status === 'available') {
                  btnLabel = '✓ In Plex';
                } else if (item.status === 'requested' || reqStatus === 'requested') {
                  btnLabel = '✓ Requested';
                } else if (reqStatus === 'requesting') {
                  btnLabel = 'Requesting...';
                } else if (reqStatus === 'error') {
                  btnLabel = 'Error';
                }

                return (
                  <div key={`${item.tmdb_id}-${idx}`} className="item-card">
                    <div className="poster-wrap">
                      {item.thumb ? (
                        <img src={item.thumb} loading="lazy" alt="" />
                      ) : null}
                      <span className="type-pill">
                        {(item.media_type || '').toUpperCase()}
                      </span>
                    </div>
                    <div className="meta">
                      <div className="title">{item.title}</div>
                      <div className="sub">{item.year || ''}</div>
                      <button
                        type="button"
                        className="req-action-btn"
                        data-type={item.media_type}
                        data-id={String(item.tmdb_id)}
                        aria-label={btnLabel}
                        disabled={isDisabled}
                        onClick={() => handleSeerrRequest(item)}
                      >
                        {btnLabel}
                      </button>
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      </ViewTransition>
    </div>
  );
});
