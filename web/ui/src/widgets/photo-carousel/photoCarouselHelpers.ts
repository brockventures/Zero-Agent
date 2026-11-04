import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { beginAnimation, endAnimation } from '../../perf/animating';
import { useDisplayStore } from '../../store';

export interface PhotoItem {
  id?: string; url?: string; URL?: string; width?: number; height?: number;
  timestamp?: string; location?: string; album_name?: string; dominant_color?: string;
}

export interface PhotoCarouselData { photos?: PhotoItem[]; album_url?: string; cycle_interval_seconds?: number; }
export interface PhotoAlbumConfig { name?: string; source?: 'share_url' | 'ember_bridge'; url?: string; }
export interface PhotoCarouselConfig {
  source?: 'share_url' | 'ember_bridge'; album_url?: string; album_url_env?: string; albums?: PhotoAlbumConfig[];
}
export interface ContainerSize { width: number; height: number; }

const CROSSFADE_MS = 850;
export const EMPTY_PHOTOS: PhotoItem[] = [];
export const DEFAULT_CYCLE_SECONDS = 30;
export const PHOTO_CONTROLS_HIDE_MS = 3000;
export const PHOTO_PAUSE_INACTIVITY_MS = 60000;
const OVERLAY_DATE_FMT = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

export function formatPhotoOverlayDate(timestamp?: string): string {
  if (!timestamp || !timestamp.trim()) return '';
  const d = new Date(timestamp.trim());
  return Number.isNaN(d.getTime()) ? '' : OVERLAY_DATE_FMT.format(d);
}

export function hasMultiplePhotoAlbums(photos: readonly PhotoItem[]): boolean {
  let firstAlbum = '';
  for (const p of photos) {
    const name = (p.album_name || '').trim();
    if (!name) continue;
    if (!firstAlbum) firstAlbum = name;
    else if (name !== firstAlbum) return true;
  }
  return false;
}

export function formatPhotoMetadataLabel(photo?: PhotoItem, showAlbumName = false): { date: string; location: string; album: string; text: string } | null {
  const date = formatPhotoOverlayDate(photo?.timestamp);
  const location = (photo?.location || '').trim();
  const album = showAlbumName ? (photo?.album_name || '').trim() : '';
  const secondary = location && album ? `${location} • ${album}` : location || album;
  const text = date && secondary ? `${date}\n${secondary}` : date || secondary;
  return text ? { date, location, album, text } : null;
}

export function parsePhotoCarouselData(raw: unknown): PhotoCarouselData {
  if (!raw) return {};
  if (typeof raw === 'object' && !Array.isArray(raw)) return raw as PhotoCarouselData;
  if (typeof raw === 'string') {
    try {
      let parsed = JSON.parse(raw);
      if (typeof parsed === 'string') {
        try { parsed = JSON.parse(parsed); } catch { /* ignore */ }
      }
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed as PhotoCarouselData;
    } catch { return {}; }
  }
  return {};
}

export function resolvePhotoCarouselSourceAndUrl(config?: PhotoCarouselConfig, data?: PhotoCarouselData): { source: 'share_url' | 'ember_bridge'; albumUrl: string } {
  if (Array.isArray(config?.albums) && config.albums.length > 0) {
    const emberEntry = config.albums.find((a) => a.source === 'ember_bridge');
    if (emberEntry) return { source: 'ember_bridge', albumUrl: (emberEntry.url || config?.album_url || data?.album_url || '').trim() };
    return { source: 'share_url', albumUrl: (config.albums[0]?.url || config?.album_url || data?.album_url || '').trim() };
  }
  return { source: config?.source === 'ember_bridge' ? 'ember_bridge' : 'share_url', albumUrl: (config?.album_url || data?.album_url || '').trim() };
}

export function getBackdropPhotoURL(rawUrl?: string): string {
  return rawUrl ? `${rawUrl}=w96-h54-c` : '';
}

export function getSizedPhotoURL(rawUrl?: string, containerSize?: ContainerSize): string {
  if (!rawUrl) return '';
  const dpr = Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
  const screenWidth = Math.round((((typeof window !== 'undefined' && (window.screen?.width || window.innerWidth)) || 1920) * dpr));
  const screenHeight = Math.round((((typeof window !== 'undefined' && (window.screen?.height || window.innerHeight)) || 1080) * dpr));
  const rectW = Math.round((containerSize?.width || 0) * dpr);
  const rectH = Math.round((containerSize?.height || 0) * dpr);
  return `${rawUrl}=w${rectW > 0 ? rectW : screenWidth}-h${rectH > 0 ? rectH : screenHeight}`;
}

export interface WidgetPhotoDeckState { currentKey: string | null; seenKeys: Set<string>; remainingMs: number | null; }
export const photoDeckStateById = new Map<string, WidgetPhotoDeckState>();

useDisplayStore.subscribe((state) => {
  for (const id of Array.from(photoDeckStateById.keys())) {
    if (!state?.widgets?.[id]) photoDeckStateById.delete(id);
  }
});

function getWidgetPhotoDeckState(id: string): WidgetPhotoDeckState {
  let deck = photoDeckStateById.get(id);
  if (!deck) {
    deck = { currentKey: null, seenKeys: new Set<string>(), remainingMs: null };
    photoDeckStateById.set(id, deck);
  }
  return deck;
}

export function getPhotoItemKey(item?: PhotoItem, fallbackIndex?: number): string {
  const rawUrl = item?.url ?? item?.URL;
  if (rawUrl && rawUrl.trim() !== '') return rawUrl;
  if (item?.id && item.id.trim() !== '') return item.id;
  return fallbackIndex !== undefined ? String(fallbackIndex) : '';
}

export function selectNextPhotoIndex(photos: readonly PhotoItem[], baseIndex: number, seenKeys: Set<string>): number {
  if (photos.length <= 1) return 0;
  const safeBase = ((baseIndex % photos.length) + photos.length) % photos.length;
  for (let i = 1; i < photos.length; i++) {
    const candidateIdx = (safeBase + i) % photos.length;
    const candidateKey = getPhotoItemKey(photos[candidateIdx], candidateIdx);
    if (!seenKeys.has(candidateKey)) return candidateIdx;
  }
  seenKeys.clear();
  return (safeBase + 1) % photos.length;
}

export function selectPrevPhotoIndex(photos: readonly PhotoItem[], baseIndex: number): number {
  if (photos.length <= 1) return 0;
  return (((baseIndex % photos.length) + photos.length) % photos.length - 1 + photos.length) % photos.length;
}

export function selectPreloadPhotoIndices(photos: readonly PhotoItem[], baseIndex: number, seenKeys?: ReadonlySet<string>, count = 8): number[] {
  if (photos.length <= 1 || count <= 0) return [];
  const simSeen = new Set<string>(seenKeys);
  const safeBase = ((baseIndex % photos.length) + photos.length) % photos.length;
  simSeen.add(getPhotoItemKey(photos[safeBase], safeBase));
  const result: number[] = [];
  let cursor = safeBase;
  const maxItems = Math.min(count, photos.length - 1);
  for (let step = 0; step < photos.length && result.length < maxItems; step++) {
    const nextIdx = selectNextPhotoIndex(photos, cursor, simSeen);
    const rawUrl = photos[nextIdx]?.url ?? photos[nextIdx]?.URL;
    simSeen.add(getPhotoItemKey(photos[nextIdx], nextIdx));
    cursor = nextIdx;
    if (nextIdx !== safeBase && rawUrl && !result.includes(nextIdx)) result.push(nextIdx);
  }
  const prevIdx = selectPrevPhotoIndex(photos, safeBase);
  const prevUrl = photos[prevIdx]?.url ?? photos[prevIdx]?.URL;
  if (prevIdx !== safeBase && prevUrl && !result.includes(prevIdx)) result.push(prevIdx);
  return result;
}

function scheduleCycleTick(
  timerRef: React.MutableRefObject<ReturnType<typeof setTimeout> | null>,
  activeDelayMsRef: React.MutableRefObject<number>,
  cycleStartedAtRef: React.MutableRefObject<number | null>,
  deck: WidgetPhotoDeckState,
  cycleIntervalSeconds: number,
  triggerAdvance: () => void,
  delayMs: number,
) {
  timerRef.current = setTimeout(() => {
    deck.remainingMs = cycleIntervalSeconds * 1000;
    activeDelayMsRef.current = cycleIntervalSeconds * 1000;
    cycleStartedAtRef.current = Date.now();
    triggerAdvance();
    scheduleCycleTick(
      timerRef, activeDelayMsRef, cycleStartedAtRef, deck,
      cycleIntervalSeconds, triggerAdvance, cycleIntervalSeconds * 1000,
    );
  }, delayMs);
}

function preloadAndDecode(src: string): Promise<boolean> {
  const preload = new Image();
  preload.src = src;
  return new Promise<boolean>((resolve) => {
    let settled = false;
    const finish = (ok: boolean) => {
      if (!settled) {
        settled = true;
        resolve(ok);
      }
    };
    if (typeof preload.decode === 'function') {
      preload.decode().then(() => finish(true), () => finish(false));
      preload.onerror = () => finish(false);
    } else {
      preload.onload = () => finish(true);
      preload.onerror = () => finish(false);
    }
  });
}

function resolveInitialPhotoIndex(
  photos: readonly PhotoItem[],
  deck: WidgetPhotoDeckState,
): number {
  if (photos.length === 0) return 0;
  if (deck.currentKey) {
    const idx = photos.findIndex((p, i) => getPhotoItemKey(p, i) === deck.currentKey);
    if (idx !== -1) {
      deck.seenKeys.add(deck.currentKey);
      return idx;
    }
  }
  let unseenIdx = photos.findIndex((p, i) => !deck.seenKeys.has(getPhotoItemKey(p, i)));
  if (unseenIdx === -1) {
    deck.seenKeys.clear();
    unseenIdx = 0;
  }
  const key = getPhotoItemKey(photos[unseenIdx], unseenIdx);
  deck.currentKey = key;
  deck.seenKeys.add(key);
  return unseenIdx;
}

export function useContainerSize() {
  const [containerSize, setContainerSize] = useState<ContainerSize>({ width: 0, height: 0 });
  const containerSizeRef = useRef<ContainerSize>({ width: 0, height: 0 });
  const [element, setElement] = useState<HTMLDivElement | null>(null);

  const rootRef = (node: HTMLDivElement | null) => {
    setElement(node);
  };

  useEffect(() => {
    if (!element || typeof ResizeObserver !== 'function') return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0) {
          const cur = containerSizeRef.current;
          if (cur.width !== width || cur.height !== height) {
            const newSize = { width, height };
            containerSizeRef.current = newSize;
            setContainerSize(newSize);
          }
        }
      }
    });
    ro.observe(element);
    return () => ro.disconnect();
  }, [element]);

  return { rootRef, containerSize, containerSizeRef };
}

export type EmberSyncListener = (payload: unknown) => void;
const emberSyncListeners = new Set<EmberSyncListener>();

function handleGlobalEmberPhotosSynced(payload: unknown) {
  for (const listener of Array.from(emberSyncListeners)) {
    try { listener(payload); } catch { /* no-op */ }
  }
}

export function registerEmberSyncListener(listener: EmberSyncListener): () => void {
  emberSyncListeners.add(listener);
  if (typeof window !== 'undefined') window.onEmberPhotosSynced = handleGlobalEmberPhotosSynced;
  return () => {
    emberSyncListeners.delete(listener);
    if (emberSyncListeners.size === 0 && typeof window !== 'undefined') {
      delete window.onEmberPhotosSynced;
    }
  };
}

export interface UsePhotoSlideshowTransitionOptions {
  id: string; photos: readonly PhotoItem[]; cycleIntervalSeconds: number;
  isCurrentPane: boolean; isDocumentVisible: boolean; isVideoMode: boolean;
  isPaused: boolean; containerSizeRef: React.MutableRefObject<ContainerSize>;
}

export function usePhotoSlideshowTransition({
  id, photos, cycleIntervalSeconds, isCurrentPane, isDocumentVisible, isVideoMode, isPaused, containerSizeRef,
}: UsePhotoSlideshowTransitionOptions) {
  const [currentIndex, setCurrentIndex] = useState(() => resolveInitialPhotoIndex(photos, getWidgetPhotoDeckState(id)));
  const safeIndex = photos.length > 0 ? currentIndex % photos.length : 0;
  const currentIndexRef = useRef(safeIndex);
  const probeIndexRef = useRef<number | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const photosRef = useRef(photos);
  const isAnimatingRef = useRef(false);
  const [transitionState, setTransitionState] = useState<{ nextIndex: number; active: boolean } | null>(null);
  const transitionStateRef = useRef<{ nextIndex: number; active: boolean } | null>(null);
  const cycleSeqRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transitionTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cycleStartedAtRef = useRef<number | null>(null);
  const activeDelayMsRef = useRef<number>(cycleIntervalSeconds * 1000);
  const startAnimation = () => { if (!isAnimatingRef.current) { isAnimatingRef.current = true; beginAnimation(); } };
  const stopAnimation = () => { if (isAnimatingRef.current) { isAnimatingRef.current = false; endAnimation(); } };
  const clearPendingTransition = () => {
    cycleSeqRef.current += 1;
    stopAnimation();
    if (transitionTimeoutRef.current) { clearTimeout(transitionTimeoutRef.current); transitionTimeoutRef.current = null; }
    if (transitionStateRef.current !== null) { transitionStateRef.current = null; setTransitionState(null); }
  };

  const triggerAdvance = (direction: 'next' | 'prev' = 'next') => {
    const currentPhotos = photosRef.current;
    if (currentPhotos.length <= 1) return;

    const baseIndex = transitionStateRef.current?.active
      ? transitionStateRef.current.nextIndex
      : (probeIndexRef.current ?? currentIndexRef.current);

    const deck = getWidgetPhotoDeckState(id);
    const baseKey = getPhotoItemKey(currentPhotos[baseIndex], baseIndex);
    deck.currentKey = baseKey;
    if (direction === 'prev') {
      deck.seenKeys.delete(baseKey);
    } else {
      deck.seenKeys.add(baseKey);
    }

    if (transitionStateRef.current?.active) {
      setCurrentIndex(baseIndex);
      currentIndexRef.current = baseIndex;
      stopAnimation();
      if (transitionTimeoutRef.current) {
        clearTimeout(transitionTimeoutRef.current);
        transitionTimeoutRef.current = null;
      }
      transitionStateRef.current = null;
      setTransitionState(null);
    }

    const nextIndex = direction === 'prev'
      ? selectPrevPhotoIndex(currentPhotos, baseIndex)
      : selectNextPhotoIndex(currentPhotos, baseIndex, deck.seenKeys);
    const nextPhoto = currentPhotos[nextIndex];
    const nextRawUrl = nextPhoto?.url ?? nextPhoto?.URL;
    const nextKey = getPhotoItemKey(nextPhoto, nextIndex);

    if (!nextRawUrl) {
      deck.seenKeys.add(nextKey);
      probeIndexRef.current = nextIndex;
      deck.remainingMs = cycleIntervalSeconds * 1000;
      clearPendingTransition();
      setRetryTick((t) => t + 1);
      return;
    }

    const nextFgUrl = getSizedPhotoURL(nextRawUrl, containerSizeRef.current);
    const seq = ++cycleSeqRef.current;
    if (transitionTimeoutRef.current) {
      clearTimeout(transitionTimeoutRef.current);
      transitionTimeoutRef.current = null;
    }

    preloadAndDecode(nextFgUrl).then((ok) => {
      if (cycleSeqRef.current !== seq) return;
      if (!ok) {
        deck.seenKeys.add(nextKey);
        probeIndexRef.current = nextIndex;
        deck.remainingMs = cycleIntervalSeconds * 1000;
        clearPendingTransition();
        setRetryTick((t) => t + 1);
        return;
      }

      probeIndexRef.current = null;
      startAnimation();
      const nextState = { nextIndex, active: true };
      transitionStateRef.current = nextState;
      setTransitionState(nextState);

      transitionTimeoutRef.current = setTimeout(() => {
        if (cycleSeqRef.current !== seq) return;
        deck.currentKey = nextKey;
        deck.seenKeys.add(nextKey);
        setCurrentIndex(nextIndex);
        currentIndexRef.current = nextIndex;
        stopAnimation();
        transitionStateRef.current = null;
        setTransitionState(null);
        transitionTimeoutRef.current = null;
      }, CROSSFADE_MS);
    });
  };

  const stepPhoto = (direction: 'prev' | 'next') => {
    const deck = getWidgetPhotoDeckState(id);
    deck.remainingMs = cycleIntervalSeconds * 1000;
    triggerAdvance(direction);
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (isCurrentPane && isDocumentVisible && !isVideoMode && !isPaused) {
      activeDelayMsRef.current = cycleIntervalSeconds * 1000;
      cycleStartedAtRef.current = Date.now();
      scheduleCycleTick(
        timerRef, activeDelayMsRef, cycleStartedAtRef, deck,
        cycleIntervalSeconds, triggerAdvance, cycleIntervalSeconds * 1000,
      );
    }
  };

  useLayoutEffect(() => {
    currentIndexRef.current = safeIndex;
    photosRef.current = photos;
    transitionStateRef.current = transitionState;
  });

  useEffect(() => {
    probeIndexRef.current = null;
    if (photos.length === 0) return;
    const targetIdx = resolveInitialPhotoIndex(photos, getWidgetPhotoDeckState(id));
    if (currentIndexRef.current !== targetIdx) {
      setCurrentIndex(targetIdx);
      currentIndexRef.current = targetIdx;
    }
  }, [photos, id]);

  const clearPendingTransitionRef = useRef(clearPendingTransition);
  const triggerAdvanceRef = useRef(triggerAdvance);
  useEffect(() => {
    clearPendingTransitionRef.current = clearPendingTransition;
    triggerAdvanceRef.current = triggerAdvance;
  });

  useEffect(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const shouldRun = isCurrentPane && isDocumentVisible && !isVideoMode && !isPaused && photos.length > 1;
    const deck = getWidgetPhotoDeckState(id);

    if (!shouldRun) {
      if (cycleStartedAtRef.current !== null) {
        const elapsed = Date.now() - cycleStartedAtRef.current;
        deck.remainingMs = Math.max(1, activeDelayMsRef.current - elapsed);
        cycleStartedAtRef.current = null;
      }
      const pausedOnly = isPaused && isCurrentPane && isDocumentVisible && !isVideoMode && photos.length > 1;
      if (!pausedOnly) clearPendingTransitionRef.current();
      return;
    }

    const initialDelayMs = deck.remainingMs ?? cycleIntervalSeconds * 1000;
    activeDelayMsRef.current = initialDelayMs;
    cycleStartedAtRef.current = Date.now();
    scheduleCycleTick(
      timerRef, activeDelayMsRef, cycleStartedAtRef, deck,
      cycleIntervalSeconds, () => triggerAdvanceRef.current(), initialDelayMs,
    );

    return () => {
      if (cycleStartedAtRef.current !== null) {
        const elapsed = Date.now() - cycleStartedAtRef.current;
        deck.remainingMs = Math.max(1, activeDelayMsRef.current - elapsed);
        cycleStartedAtRef.current = null;
      }
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [isCurrentPane, isDocumentVisible, isVideoMode, isPaused, photos.length, cycleIntervalSeconds, retryTick, id]);

  useEffect(() => {
    return () => {
      clearPendingTransitionRef.current();
    };
  }, []);

  return { safeIndex, transitionState, stepPhoto };
}
