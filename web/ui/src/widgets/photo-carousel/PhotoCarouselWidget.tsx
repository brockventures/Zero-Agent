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
  type ContainerSize, type EmberSyncListener, type PhotoCarouselConfig, type PhotoItem,
} from './photoCarouselHelpers';

export {
  parsePhotoCarouselData, getBackdropPhotoURL, getSizedPhotoURL, photoDeckStateById,
  getPhotoItemKey, selectNextPhotoIndex, selectPrevPhotoIndex, selectPreloadPhotoIndices,
  formatPhotoOverlayDate, formatPhotoMetadataLabel, hasMultiplePhotoAlbums,
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
  return (
    <div
      className="photo-loading"
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100%',
        color: 'var(--mm-text-dim, #8b819f)',
        fontSize: '0.95rem',
        fontWeight: 700,
      }}
    >
      <span>Loading photos...</span>
    </div>
  );
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
    if (needsAuth) {
      emptyTitle = 'Google Photos sign-in required';
    } else if (syncError) {
      emptyTitle = 'Photo sync failed';
    } else {
      emptyTitle = 'No photos available';
    }
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

function PhotoBackdrop({
  currentUrl,
  nextUrl,
  isTransitioning,
}: {
  currentUrl: string;
  nextUrl: string;
  isTransitioning: boolean;
}) {
  return (
    <div className="photo-backdrop-container" aria-hidden="true">
      <img
        className="photo-backdrop-layer photo-backdrop-current"
        src={currentUrl}
        alt=""
        style={{
          opacity: isTransitioning ? 0 : 1,
          transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none',
        }}
      />
      <img
        className="photo-backdrop-layer photo-backdrop-next"
        src={isTransitioning ? nextUrl : undefined}
        alt=""
        style={{
          opacity: isTransitioning ? 1 : 0,
          transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none',
        }}
      />
    </div>
  );
}

function PhotoForeground({
  currentUrl,
  nextUrl,
  isTransitioning,
}: {
  currentUrl: string;
  nextUrl: string;
  isTransitioning: boolean;
}) {
  return (
    <div className="photo-foreground-container">
      <img
        className="photo-layer photo-current dither"
        src={currentUrl}
        alt="Photo"
        style={{
          opacity: isTransitioning ? 0 : 1,
          transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none',
        }}
      />
      <img
        className="photo-layer photo-next dither"
        src={isTransitioning ? nextUrl : undefined}
        alt="Photo"
        style={{
          opacity: isTransitioning ? 1 : 0,
          transition: isTransitioning ? 'opacity 0.85s ease-in-out' : 'none',
        }}
      />
    </div>
  );
}

function PhotoMetadataOverlay({ photo, showAlbumName }: { photo?: PhotoItem; showAlbumName: boolean }) {
  const meta = formatPhotoMetadataLabel(photo, showAlbumName);
  if (!meta) return null;
  return (
    <div className="photo-metadata-overlay" aria-label={meta.text}>
      {meta.text}
    </div>
  );
}

function PhotoPreloadPool({ photos, baseIndex, widgetId, containerSize }: {
  photos: readonly PhotoItem[]; baseIndex: number; widgetId: string; containerSize: ContainerSize;
}) {
  const indices = selectPreloadPhotoIndices(photos, baseIndex, photoDeckStateById.get(widgetId)?.seenKeys);
  if (indices.length === 0) return null;
  return (
    <div className="photo-preload-pool" aria-hidden="true">
      {indices.map((idx) => {
        const p = photos[idx];
        const rawUrl = p?.url ?? p?.URL;
        return (
          <span key={getPhotoItemKey(p, idx)}>
            <img src={getBackdropPhotoURL(rawUrl)} decoding="async" loading="eager" alt="" />
            <img src={getSizedPhotoURL(rawUrl, containerSize)} decoding="async" loading="eager" alt="" />
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
  visible,
  isPaused,
  onPrev,
  onNext,
  onTogglePause,
}: PhotoTransportControlsProps) {
  return (
    <div
      className={`photo-transport-controls${visible ? ' visible' : ''}`}
      onClick={(e) => {
        e.stopPropagation();
      }}
    >
      <button
        type="button"
        className="photo-nav-btn photo-nav-prev"
        aria-label="Previous photo"
        onClick={(e) => {
          e.stopPropagation();
          onPrev();
        }}
      >
        <HudPrevIcon />
      </button>
      <button
        type="button"
        className="photo-nav-btn photo-pause-pill"
        aria-label={isPaused ? 'Resume slideshow' : 'Pause slideshow'}
        aria-pressed={isPaused}
        data-paused={isPaused ? 'true' : 'false'}
        onClick={(e) => {
          e.stopPropagation();
          onTogglePause();
        }}
      >
        {isPaused ? <HudPlayIcon /> : <HudPauseIcon />}
      </button>
      <button
        type="button"
        className="photo-nav-btn photo-nav-next"
        aria-label="Next photo"
        onClick={(e) => {
          e.stopPropagation();
          onNext();
        }}
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

  useEffect(() => {
    return () => {
      if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
      if (pauseInactivityTimerRef.current) clearTimeout(pauseInactivityTimerRef.current);
    };
  }, []);

  return { controlsVisible, showControls, resetPauseInactivityTimer, togglePause };
}

export function PhotoCarouselWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const isCurrentPane = useIsCurrentPane();
  const isDocumentVisible = useDocumentVisible();
  const isVideoMode = useDisplayStore(
    (s) => (s.video?.mode || (s.video?.primary ? 'video' : 'widgets')) === 'video',
  );

  const config = entry?.meta?.config as PhotoCarouselConfig | undefined;
  const rawData = entry?.data;
  const data = parsePhotoCarouselData(rawData);
  const hasLoaded = entry?.state !== 'loading' && Array.isArray(data.photos);

  const { source, albumUrl } = resolvePhotoCarouselSourceAndUrl(config, data);

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

  const photos = Array.isArray(data.photos) ? data.photos : EMPTY_PHOTOS;
  const cycleIntervalSeconds = Math.max(
    5,
    Number(data?.cycle_interval_seconds) || DEFAULT_CYCLE_SECONDS,
  );

  const [isPaused, setIsPaused] = useState(false);
  const { controlsVisible, showControls, resetPauseInactivityTimer, togglePause } = usePhotoControls(
    isPaused,
    setIsPaused,
  );
  const { rootRef, containerSize, containerSizeRef } = useContainerSize();

  const { safeIndex, transitionState, stepPhoto } = usePhotoSlideshowTransition({
    id,
    photos,
    cycleIntervalSeconds,
    isCurrentPane,
    isDocumentVisible,
    isVideoMode,
    isPaused,
    containerSizeRef,
  });

  const handleStep = (direction: 'prev' | 'next') => {
    showControls();
    if (isPaused) resetPauseInactivityTimer();
    stepPhoto(direction);
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

  const currentPhoto = photos[safeIndex];
  const currentRawUrl = currentPhoto?.url ?? currentPhoto?.URL;
  const currentFgUrl = getSizedPhotoURL(currentRawUrl, containerSize);
  const currentBackdropUrl = getBackdropPhotoURL(currentRawUrl);

  const isTransitioning = Boolean(transitionState?.active);
  const nextPhoto = transitionState ? photos[transitionState.nextIndex] : undefined;
  const activeIndex = isTransitioning && transitionState ? transitionState.nextIndex : safeIndex;
  const activePhoto = isTransitioning && nextPhoto ? nextPhoto : currentPhoto;
  const nextRawUrl = nextPhoto?.url ?? nextPhoto?.URL;
  const nextFgUrl = nextRawUrl ? getSizedPhotoURL(nextRawUrl, containerSize) : '';
  const nextBackdropUrl = nextRawUrl ? getBackdropPhotoURL(nextRawUrl) : '';
  const showAlbumName = (Array.isArray(config?.albums) && config.albums.length > 1) || hasMultiplePhotoAlbums(photos);

  return (
    <div
      ref={rootRef}
      className="widget-photo-carousel dither"
      data-fit="contain"
      onClick={hasLoaded && photos.length > 0 ? handleClick : undefined}
    >
      {!hasLoaded ? (
        <PhotoCarouselLoading />
      ) : photos.length === 0 ? (
        <PhotoCarouselEmpty
          source={source}
          needsAuth={needsAuth}
          syncError={syncError}
          albumUrl={albumUrl}
        />
      ) : (
        <div className="photo-viewport dither">
          <PhotoBackdrop
            currentUrl={currentBackdropUrl}
            nextUrl={nextBackdropUrl}
            isTransitioning={isTransitioning}
          />
          <PhotoForeground
            currentUrl={currentFgUrl}
            nextUrl={nextFgUrl}
            isTransitioning={isTransitioning}
          />
          <PhotoPreloadPool
            photos={photos}
            baseIndex={activeIndex}
            widgetId={id}
            containerSize={containerSize}
          />
          <PhotoMetadataOverlay photo={activePhoto} showAlbumName={showAlbumName} />
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
