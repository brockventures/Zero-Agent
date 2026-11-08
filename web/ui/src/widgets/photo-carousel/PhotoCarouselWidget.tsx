import './PhotoCarouselWidget.css';
import { useEffect, useRef, useState } from 'react';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import { useIsCurrentPane } from '../../carousel/PaneContext';
import { useDocumentVisible } from '../../lifecycle/visibility';
import { useDisplayStore } from '../../store';
import { HudNextIcon, HudPauseIcon, HudPlayIcon, HudPrevIcon } from '../../video/HudIcons';
import {
  EMPTY_PHOTOS, DEFAULT_CYCLE_SECONDS, PHOTO_CONTROLS_HIDE_MS, PHOTO_PAUSE_INACTIVITY_MS,
  photoDeckStateById, getPhotoItemKey, selectPreloadPhotoIndices,
  parsePhotoCarouselData, getBackdropPhotoURL, getSizedPhotoURL, useContainerSize,
  registerEmberSyncListener, usePhotoSlideshowTransition, resolvePhotoCarouselSourceAndUrl,
  formatPhotoMetadataLabel, hasMultiplePhotoAlbums,
  isWideContainer, isVideoPhoto, getVideoStreamURL,
  type ContainerSize, type EmberSyncListener, type PhotoCarouselConfig, type PhotoItem, type SlideTransitionState,
} from './photoCarouselHelpers';

export {
  parsePhotoCarouselData, getBackdropPhotoURL, getSizedPhotoURL, photoDeckStateById,
  getPhotoItemKey, selectNextPhotoIndex, selectPrevPhotoIndex, selectPreloadPhotoIndices,
  formatPhotoOverlayDate, formatPhotoMetadataLabel, hasMultiplePhotoAlbums,
  isPortraitPhoto, isWideContainer, selectCompanionPortraitIndex,
  isVideoPhoto, getVideoStreamURL, VIDEO_STREAM_SUFFIXES,
  PHOTO_CONTROLS_HIDE_MS, PHOTO_PAUSE_INACTIVITY_MS,
} from './photoCarouselHelpers';

declare global {
  interface Window {
    EmberBridge?: {
      syncGooglePhotosAlbum?: (albumUrl: string) => void;
      connectGooglePhotos?: (albumUrl: string) => void;
    };
    onEmberPhotosSynced?: (payload: unknown) => void;
  }
}

function PhotoCarouselLoading() {
  return <div className="photo-loading" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--mm-text-dim, #8b819f)', fontSize: '0.95rem', fontWeight: 700 }}><span>Loading photos...</span></div>;
}

interface PhotoCarouselEmptyProps {
  source: 'share_url' | 'ember_bridge';
  needsAuth: boolean;
  syncError: boolean;
  albumUrl: string;
}

function PhotoCarouselEmpty({ source, needsAuth, syncError, albumUrl }: PhotoCarouselEmptyProps) {
  let emptyTitle = 'No photos in shared album';
  if (source === 'ember_bridge') {
    if (needsAuth) emptyTitle = 'Google Photos sign-in required';
    else if (syncError) emptyTitle = 'Photo sync failed';
    else emptyTitle = 'No photos available';
  }

  const canConnect =
    source === 'ember_bridge' &&
    albumUrl.trim() !== '' &&
    typeof window !== 'undefined' &&
    typeof window.EmberBridge?.connectGooglePhotos === 'function';

  return (
    <div className="photo-empty">
      <span className="empty-icon">
        <svg
          width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
        >
          <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
          <circle cx="9" cy="9" r="2" />
          <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" />
        </svg>
      </span>
      <span className="empty-title">{emptyTitle}</span>
      {canConnect && (
        <button
          type="button"
          className="photo-connect-btn"
          onClick={(e) => {
            e.stopPropagation();
            window.EmberBridge?.connectGooglePhotos?.(albumUrl);
          }}
        >
          Connect Google Photos
        </button>
      )}
    </div>
  );
}

function PhotoBackdrop({ currentUrl, nextUrl, isTransitioning }: { currentUrl: string; nextUrl: string; isTransitioning: boolean }) {
  return (
    <div className="photo-backdrop-container" aria-hidden="true">
      <img className="photo-backdrop-layer photo-backdrop-current" src={currentUrl} alt="" style={{ opacity: isTransitioning ? 0 : 1, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }} />
      <img className="photo-backdrop-layer photo-backdrop-next" src={isTransitioning ? nextUrl : undefined} alt="" style={{ opacity: isTransitioning ? 1 : 0, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }} />
    </div>
  );
}

function PhotoForeground({
  currentUrl, currentSecondaryUrl, currentRawUrl, currentSecondaryRawUrl, currentIsVideo, currentSecondaryIsVideo,
  nextUrl, nextSecondaryUrl, isTransitioning, playingSlot, streamSuffixIndex,
  onPlaySlot, onStopVideo, onVideoEnded, onVideoError,
}: {
  currentUrl: string; currentSecondaryUrl?: string; currentRawUrl?: string; currentSecondaryRawUrl?: string;
  currentIsVideo?: boolean; currentSecondaryIsVideo?: boolean; nextUrl: string; nextSecondaryUrl?: string;
  isTransitioning: boolean; playingSlot: 'primary' | 'secondary' | null; streamSuffixIndex: number;
  onPlaySlot: (slot: 'primary' | 'secondary') => void; onStopVideo: () => void; onVideoEnded: () => void; onVideoError: () => void;
}) {
  return (
    <div className="photo-foreground-container">
      {currentSecondaryUrl ? (
        <div className="photo-layer photo-current photo-foreground-diptych dither" style={{ opacity: isTransitioning ? 0 : 1, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }}>
          <div className="photo-diptych-slot">
            {playingSlot === 'primary' ? (
              <video className="photo-diptych-item photo-video-player dither" src={getVideoStreamURL(currentRawUrl, streamSuffixIndex)} poster={currentUrl} autoPlay playsInline onClick={(e) => { e.stopPropagation(); onStopVideo(); }} onEnded={onVideoEnded} onError={onVideoError} />
            ) : (
              <img className="photo-diptych-item photo-current dither" src={currentUrl} alt="Photo" />
            )}
            {!isTransitioning && currentIsVideo && playingSlot !== 'primary' && (
              <button type="button" className="photo-video-play-btn" aria-label="Play video" onClick={(e) => { e.stopPropagation(); onPlaySlot('primary'); }}><HudPlayIcon /></button>
            )}
          </div>
          <div className="photo-diptych-slot">
            {playingSlot === 'secondary' ? (
              <video className="photo-diptych-item photo-video-player dither" src={getVideoStreamURL(currentSecondaryRawUrl, streamSuffixIndex)} poster={currentSecondaryUrl} autoPlay playsInline onClick={(e) => { e.stopPropagation(); onStopVideo(); }} onEnded={onVideoEnded} onError={onVideoError} />
            ) : (
              <img className="photo-diptych-item photo-current-secondary dither" src={currentSecondaryUrl} alt="Photo" />
            )}
            {!isTransitioning && currentSecondaryIsVideo && playingSlot !== 'secondary' && (
              <button type="button" className="photo-video-play-btn" aria-label="Play video" onClick={(e) => { e.stopPropagation(); onPlaySlot('secondary'); }}><HudPlayIcon /></button>
            )}
          </div>
        </div>
      ) : (
        <>
          {playingSlot === 'primary' ? (
            <video className="photo-layer photo-current photo-video-player dither" src={getVideoStreamURL(currentRawUrl, streamSuffixIndex)} poster={currentUrl} autoPlay playsInline onClick={(e) => { e.stopPropagation(); onStopVideo(); }} onEnded={onVideoEnded} onError={onVideoError} />
          ) : (
            <img className="photo-layer photo-current dither" src={currentUrl} alt="Photo" style={{ opacity: isTransitioning ? 0 : 1, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }} />
          )}
          {!isTransitioning && currentIsVideo && playingSlot !== 'primary' && (
            <button type="button" className="photo-video-play-btn" aria-label="Play video" onClick={(e) => { e.stopPropagation(); onPlaySlot('primary'); }}><HudPlayIcon /></button>
          )}
        </>
      )}
      {nextSecondaryUrl ? (
        <div className="photo-layer photo-next photo-foreground-diptych dither" style={{ opacity: isTransitioning ? 1 : 0, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }}>
          <img className="photo-diptych-item photo-next dither" src={isTransitioning ? nextUrl : undefined} alt="Photo" />
          <img className="photo-diptych-item photo-next-secondary dither" src={isTransitioning ? nextSecondaryUrl : undefined} alt="Photo" />
        </div>
      ) : (
        <img className="photo-layer photo-next dither" src={isTransitioning ? nextUrl : undefined} alt="Photo" style={{ opacity: isTransitioning ? 1 : 0, transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none' }} />
      )}
    </div>
  );
}

function PhotoMetadataOverlay({ photo, secondaryPhoto, showAlbumName }: { photo?: PhotoItem; secondaryPhoto?: PhotoItem; showAlbumName: boolean }) {
  const meta = formatPhotoMetadataLabel(photo, showAlbumName, secondaryPhoto);
  return meta ? <div className="photo-metadata-overlay" aria-label={meta.text}>{meta.text}</div> : null;
}

function PhotoPreloadPool({ photos, baseIndex, companionIndex, widgetId, containerSize }: {
  photos: readonly PhotoItem[]; baseIndex: number; companionIndex?: number | null; widgetId: string; containerSize: ContainerSize;
}) {
  const indices = selectPreloadPhotoIndices(photos, baseIndex, photoDeckStateById.get(widgetId)?.seenKeys);
  const allIndices = companionIndex !== undefined && companionIndex !== null && !indices.includes(companionIndex)
    ? [companionIndex, ...indices]
    : indices;
  if (allIndices.length === 0) return null;
  return (
    <div className="photo-preload-pool" aria-hidden="true">
      {allIndices.map((idx) => {
        const p = photos[idx];
        const rawUrl = p?.url ?? p?.URL;
        return (
          <span key={getPhotoItemKey(p, idx)}>
            <img src={getBackdropPhotoURL(rawUrl)} decoding="async" loading="eager" alt="" />
            <img src={getSizedPhotoURL(rawUrl, containerSize, isVideoPhoto(p))} decoding="async" loading="eager" alt="" />
          </span>
        );
      })}
    </div>
  );
}

interface PhotoTransportControlsProps {
  visible: boolean;
  isPaused: boolean;
  onPrev: () => void;
  onNext: () => void;
  onTogglePause: () => void;
}

function PhotoTransportControls({
  visible, isPaused, onPrev, onNext, onTogglePause,
}: PhotoTransportControlsProps) {
  return (
    <div
      className={`photo-transport-controls${visible ? ' visible' : ''}`}
      onClick={(e) => { e.stopPropagation(); }}
    >
      <button
        type="button"
        className="photo-nav-btn photo-nav-prev"
        aria-label="Previous photo"
        onClick={(e) => { e.stopPropagation(); onPrev(); }}
      >
        <HudPrevIcon />
      </button>
      <button
        type="button"
        className="photo-nav-btn photo-pause-pill"
        aria-label={isPaused ? 'Resume slideshow' : 'Pause slideshow'}
        aria-pressed={isPaused}
        data-paused={isPaused ? 'true' : 'false'}
        onClick={(e) => { e.stopPropagation(); onTogglePause(); }}
      >
        {isPaused ? <HudPlayIcon /> : <HudPauseIcon />}
      </button>
      <button
        type="button"
        className="photo-nav-btn photo-nav-next"
        aria-label="Next photo"
        onClick={(e) => { e.stopPropagation(); onNext(); }}
      >
        <HudNextIcon />
      </button>
    </div>
  );
}

function usePhotoControls(
  isPaused: boolean,
  setIsPaused: React.Dispatch<React.SetStateAction<boolean>>,
) {
  const [controlsVisible, setControlsVisible] = useState(false);
  const controlsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pauseInactivityTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showControls = () => {
    setControlsVisible(true);
    if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    controlsTimerRef.current = setTimeout(() => {
      setControlsVisible(false);
      controlsTimerRef.current = null;
    }, PHOTO_CONTROLS_HIDE_MS);
  };

  const resetPauseInactivityTimer = () => {
    if (pauseInactivityTimerRef.current) clearTimeout(pauseInactivityTimerRef.current);
    pauseInactivityTimerRef.current = setTimeout(() => {
      setIsPaused(false);
      pauseInactivityTimerRef.current = null;
    }, PHOTO_PAUSE_INACTIVITY_MS);
  };

  const togglePause = () => {
    if (!isPaused) {
      setIsPaused(true);
      resetPauseInactivityTimer();
    } else {
      setIsPaused(false);
      if (pauseInactivityTimerRef.current) {
        clearTimeout(pauseInactivityTimerRef.current);
        pauseInactivityTimerRef.current = null;
      }
      showControls();
    }
  };

  useEffect(() => () => {
    if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    if (pauseInactivityTimerRef.current) clearTimeout(pauseInactivityTimerRef.current);
  }, []);

  return { controlsVisible, showControls, resetPauseInactivityTimer, togglePause };
}

function useEmberAlbumSync(id: string, source: 'share_url' | 'ember_bridge', albumUrl: string) {
  const [needsAuth, setNeedsAuth] = useState(false);
  const [syncError, setSyncError] = useState(false);

  useEffect(() => {
    if (
      source !== 'ember_bridge' ||
      !albumUrl ||
      typeof window === 'undefined' ||
      typeof window.EmberBridge?.syncGooglePhotosAlbum !== 'function'
    ) {
      return;
    }

    const listener: EmberSyncListener = (rawPayload) => {
      let payload = rawPayload as Record<string, unknown> | null;
      if (typeof rawPayload === 'string') {
        try { payload = JSON.parse(rawPayload); } catch { return; }
      }
      if (!payload || typeof payload !== 'object') return;

      if (payload.needs_auth === true) {
        setNeedsAuth(true);
        setSyncError(false);
      } else if (payload.ok === false) {
        setSyncError(true);
        setNeedsAuth(false);
      } else if (Array.isArray(payload.photos) && payload.photos.length > 0) {
        setNeedsAuth(false);
        setSyncError(false);
        fetch('api/widgets/' + encodeURIComponent(id) + '/photos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ photos: payload.photos }),
        }).catch(() => {});
      }
    };

    const unbind = registerEmberSyncListener(listener);
    window.EmberBridge.syncGooglePhotosAlbum(albumUrl);
    const intervalTimer = setInterval(() => {
      window.EmberBridge?.syncGooglePhotosAlbum?.(albumUrl);
    }, 300_000);

    return () => {
      unbind();
      clearInterval(intervalTimer);
    };
  }, [source, albumUrl, id]);

  return { needsAuth, syncError };
}

function resolveSlideLayerState(
  photos: readonly PhotoItem[], safeIndex: number, currentCompanionIndex: number | null,
  transitionState: SlideTransitionState | null, containerSize: ContainerSize,
) {
  const wide = isWideContainer(containerSize);
  const resolveSlide = (idx: number | null, fixedCompanionIdx: number | null) => {
    if (idx === null) return { photo: undefined, fgUrl: "", backdropUrl: "", rawUrl: undefined, isVideo: false, companionIdx: null, companionPhoto: undefined, companionFgUrl: "", companionRawUrl: undefined, companionIsVideo: false };
    const photo = photos[idx];
    const isVideo = isVideoPhoto(photo);
    const rawUrl = photo?.url ?? photo?.URL;
    const fgUrl = rawUrl ? getSizedPhotoURL(rawUrl, containerSize, isVideo) : "";
    const backdropUrl = rawUrl ? getBackdropPhotoURL(rawUrl) : "";
    const companionIdx = wide ? fixedCompanionIdx : null;
    const companionPhoto = companionIdx !== null ? photos[companionIdx] : undefined;
    const companionIsVideo = isVideoPhoto(companionPhoto);
    const companionRawUrl = companionPhoto?.url ?? companionPhoto?.URL;
    const companionFgUrl = companionRawUrl ? getSizedPhotoURL(companionRawUrl, containerSize, companionIsVideo) : "";
    return { photo, fgUrl, backdropUrl, rawUrl, isVideo, companionIdx, companionPhoto, companionFgUrl, companionRawUrl, companionIsVideo };
  };
  const isTransitioning = Boolean(transitionState?.active);
  const current = resolveSlide(safeIndex, currentCompanionIndex);
  const next = resolveSlide(isTransitioning && transitionState ? transitionState.nextIndex : null, isTransitioning && transitionState ? transitionState.companionIndex : null);
  const active = isTransitioning && next.photo ? next : current;
  const activeIndex = isTransitioning && transitionState ? transitionState.nextIndex : safeIndex;
  return { isTransitioning, current, next, active, activeIndex };
}

export function PhotoCarouselWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const isCurrentPane = useIsCurrentPane();
  const isDocumentVisible = useDocumentVisible();
  const isVideoMode = useDisplayStore(
    (s) => (s.video?.mode || (s.video?.primary ? 'video' : 'widgets')) === 'video',
  );

  const config = entry?.meta?.config as PhotoCarouselConfig | undefined;
  const data = parsePhotoCarouselData(entry?.data);
  const hasLoaded = entry?.state !== 'loading' && Array.isArray(data.photos);
  const { source, albumUrl } = resolvePhotoCarouselSourceAndUrl(config, data);
  const { needsAuth, syncError } = useEmberAlbumSync(id, source, albumUrl);

  const photos = Array.isArray(data.photos) ? data.photos : EMPTY_PHOTOS;
  const cycleIntervalSeconds = Math.max(5, Number(data?.cycle_interval_seconds) || DEFAULT_CYCLE_SECONDS);

  const [isPaused, setIsPaused] = useState(false);
  const [playingSlot, setPlayingSlot] = useState<'primary' | 'secondary' | null>(null);
  const [playingSlideKey, setPlayingSlideKey] = useState<string | null>(null);
  const [streamSuffixIndex, setStreamSuffixIndex] = useState(0);
  const { controlsVisible, showControls, resetPauseInactivityTimer, togglePause } = usePhotoControls(isPaused, setIsPaused);
  const { rootRef, containerSize, containerSizeRef } = useContainerSize();
  const { safeIndex, currentCompanionIndex, transitionState, stepPhoto } = usePhotoSlideshowTransition({
    id, photos, cycleIntervalSeconds, isCurrentPane, isDocumentVisible, isVideoMode, isPaused: isPaused || playingSlot !== null, containerSizeRef,
  });
  const resetDeckTimer = () => { const deck = photoDeckStateById.get(id); if (deck) deck.remainingMs = cycleIntervalSeconds * 1000; };
  const { isTransitioning, current, next, active, activeIndex } = resolveSlideLayerState(
    photos, safeIndex, currentCompanionIndex, transitionState, containerSize,
  );
  const slideKey = `${safeIndex}:${current.rawUrl ?? ''}:${current.companionIdx ?? ''}:${current.companionRawUrl ?? ''}`;
  const shouldClearSlot =
    playingSlot !== null &&
    (playingSlideKey !== slideKey ||
      !isCurrentPane ||
      !isDocumentVisible ||
      isVideoMode ||
      (playingSlot === 'primary' && !current.isVideo) ||
      (playingSlot === 'secondary' && !current.companionIsVideo));
  if (shouldClearSlot) {
    setPlayingSlot(null);
    setPlayingSlideKey(null);
    setStreamSuffixIndex(0);
    resetDeckTimer();
  }
  const onPlaySlot = (slot: 'primary' | 'secondary') => {
    resetDeckTimer();
    setStreamSuffixIndex(0);
    setPlayingSlideKey(slideKey);
    setPlayingSlot(slot);
  };
  const onStopVideo = () => { resetDeckTimer(); setPlayingSlot(null); setPlayingSlideKey(null); setStreamSuffixIndex(0); };
  const onVideoEnded = () => { resetDeckTimer(); setPlayingSlot(null); setPlayingSlideKey(null); setStreamSuffixIndex(0); };
  const onVideoError = () => { if (streamSuffixIndex < 2) setStreamSuffixIndex((idx) => idx + 1); else onStopVideo(); };
  const handleStep = (direction: 'prev' | 'next') => {
    setPlayingSlot(null); setPlayingSlideKey(null); setStreamSuffixIndex(0); showControls(); if (isPaused) resetPauseInactivityTimer(); stepPhoto(direction);
  };

  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (photos.length <= 1) return;
    if (e.clientX > 0) {
      const rect = e.currentTarget.getBoundingClientRect();
      if (rect.width > 0 && e.clientX - rect.left < rect.width / 3) {
        handleStep('prev');
        return;
      }
    }
    handleStep('next');
  };

  const showAlbumName = (Array.isArray(config?.albums) && config.albums.length > 1) || hasMultiplePhotoAlbums(photos);

  return (
    <div
      ref={rootRef}
      className="widget-photo-carousel dither"
      data-fit="cover"
      onClick={hasLoaded && photos.length > 0 ? handleClick : undefined}
    >
      {!hasLoaded ? (
        <PhotoCarouselLoading />
      ) : photos.length === 0 ? (
        <PhotoCarouselEmpty source={source} needsAuth={needsAuth} syncError={syncError} albumUrl={albumUrl} />
      ) : (
        <div className="photo-viewport dither">
          <PhotoBackdrop currentUrl={current.backdropUrl} nextUrl={next.backdropUrl} isTransitioning={isTransitioning} />
          <PhotoForeground
            currentUrl={current.fgUrl}
            currentSecondaryUrl={current.companionFgUrl || undefined}
            currentRawUrl={current.rawUrl}
            currentSecondaryRawUrl={current.companionRawUrl}
            currentIsVideo={current.isVideo}
            currentSecondaryIsVideo={current.companionIsVideo}
            nextUrl={next.fgUrl}
            nextSecondaryUrl={next.companionFgUrl || undefined}
            isTransitioning={isTransitioning}
            playingSlot={playingSlot}
            streamSuffixIndex={streamSuffixIndex}
            onPlaySlot={onPlaySlot}
            onStopVideo={onStopVideo}
            onVideoEnded={onVideoEnded}
            onVideoError={onVideoError}
          />
          <PhotoPreloadPool
            photos={photos}
            baseIndex={activeIndex}
            companionIndex={active.companionIdx}
            widgetId={id}
            containerSize={containerSize}
          />
          <PhotoMetadataOverlay photo={active.photo} secondaryPhoto={active.companionPhoto} showAlbumName={showAlbumName} />
          {photos.length > 1 && (
            <PhotoTransportControls
              visible={controlsVisible || isPaused}
              isPaused={isPaused}
              onPrev={() => handleStep('prev')}
              onNext={() => handleStep('next')}
              onTogglePause={togglePause}
            />
          )}
        </div>
      )}
    </div>
  );
}
