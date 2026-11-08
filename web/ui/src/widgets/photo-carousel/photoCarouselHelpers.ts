import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { beginAnimation, endAnimation } from '../../perf/animating';
import { useDisplayStore } from '../../store';

export interface PhotoItem {
  id?: string; url?: string; URL?: string; width?: number; height?: number; aspect_ratio?: number;
  timestamp?: string; location?: string; album_name?: string; dominant_color?: string;
  media_type?: 'photo' | 'video' | string;
}
export function isVideoPhoto(photo?: PhotoItem): boolean { return photo?.media_type === 'video'; }
export const VIDEO_STREAM_SUFFIXES = ['=m22', '=m18', '=dv'] as const;
export function getVideoStreamURL(rawUrl?: string, suffixIndex = 0): string {
  if (!rawUrl) return '';
  const idx = Math.min(Math.max(0, suffixIndex), VIDEO_STREAM_SUFFIXES.length - 1);
  return `${rawUrl}${VIDEO_STREAM_SUFFIXES[idx]}`;
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
function joinDistinctLabels(a: string, b: string): string { return a && b && a !== b ? `${a} • ${b}` : a || b; }
export function formatPhotoMetadataLabel(photo?: PhotoItem, showAlbumName = false, secondaryPhoto?: PhotoItem) {
  const date = joinDistinctLabels(formatPhotoOverlayDate(photo?.timestamp), formatPhotoOverlayDate(secondaryPhoto?.timestamp));
  const location = joinDistinctLabels((photo?.location || '').trim(), (secondaryPhoto?.location || '').trim());
  const album = showAlbumName ? joinDistinctLabels((photo?.album_name || '').trim(), (secondaryPhoto?.album_name || '').trim()) : '';
  const secondary = location && album ? `${location} • ${album}` : location || album;
  const text = date && secondary ? `${date}\n${secondary}` : date || secondary;
  return text ? { date, location, album, text } : null;
}
export function isPortraitPhoto(photo?: PhotoItem): boolean {
  if (!photo) return false;
  if (typeof photo.aspect_ratio === 'number' && photo.aspect_ratio > 0) return photo.aspect_ratio < 0.95;
  return Boolean(photo.width && photo.height && photo.width < photo.height);
}
export function isWideContainer(size: ContainerSize): boolean {
  return (size.width > 0 && size.height > 0 && size.width / size.height >= 1.15) || (size.width === 0 && size.height === 0);
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
export function getSizedPhotoURL(rawUrl?: string, containerSize?: ContainerSize, isVideo = false): string {
  if (!rawUrl) return '';
  const dpr = Math.min(2, (typeof window !== 'undefined' && window.devicePixelRatio) || 1);
  const screenWidth = Math.round((((typeof window !== 'undefined' && (window.screen?.width || window.innerWidth)) || 1920) * dpr));
  const screenHeight = Math.round((((typeof window !== 'undefined' && (window.screen?.height || window.innerHeight)) || 1080) * dpr));
  const rectW = Math.round((containerSize?.width || 0) * dpr);
  const rectH = Math.round((containerSize?.height || 0) * dpr);
  return `${rawUrl}=w${rectW > 0 ? rectW : screenWidth}-h${rectH > 0 ? rectH : screenHeight}${isVideo ? '-no' : ''}`;
}

export interface WidgetPhotoDeckState { currentKey: string | null; companionKey: string | null; seenKeys: Set<string>; remainingMs: number | null; }
export const photoDeckStateById = new Map<string, WidgetPhotoDeckState>();

useDisplayStore.subscribe((state) => {
  for (const id of Array.from(photoDeckStateById.keys())) {
    if (!state?.widgets?.[id]) photoDeckStateById.delete(id);
  }
});

function getWidgetPhotoDeckState(id: string): WidgetPhotoDeckState {
  let deck = photoDeckStateById.get(id);
  if (!deck) {
    deck = { currentKey: null, companionKey: null, seenKeys: new Set<string>(), remainingMs: null };
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

export function selectCompanionPortraitIndex(
  photos: readonly PhotoItem[], primaryIndex: number, seenKeys?: ReadonlySet<string>,
): number | null {
  if (photos.length <= 1) return null;
  const safePrimary = ((primaryIndex % photos.length) + photos.length) % photos.length;
  if (!isPortraitPhoto(photos[safePrimary])) return null;
  const primaryKey = getPhotoItemKey(photos[safePrimary], safePrimary);
  let firstFallbackPortrait: number | null = null;
  for (let i = 1; i < photos.length; i++) {
    const idx = (safePrimary + i) % photos.length;
    const candidate = photos[idx];
    const rawUrl = candidate?.url ?? candidate?.URL;
    if (!rawUrl || !isPortraitPhoto(candidate)) continue;
    const key = getPhotoItemKey(candidate, idx);
    if (key === primaryKey) continue;
    if (firstFallbackPortrait === null) firstFallbackPortrait = idx;
    if (!seenKeys || !seenKeys.has(key)) return idx;
  }
  return firstFallbackPortrait;
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
  deck: WidgetPhotoDeckState, cycleIntervalSeconds: number, triggerAdvance: () => void, delayMs: number,
) {
  timerRef.current = setTimeout(() => {
    deck.remainingMs = cycleIntervalSeconds * 1000;
    activeDelayMsRef.current = cycleIntervalSeconds * 1000;
    cycleStartedAtRef.current = Date.now();
    triggerAdvance();
    scheduleCycleTick(timerRef, activeDelayMsRef, cycleStartedAtRef, deck, cycleIntervalSeconds, triggerAdvance, cycleIntervalSeconds * 1000);
  }, delayMs);
}

function preloadAndDecode(src: string): Promise<boolean> {
  const preload = new Image();
  preload.src = src;
  return new Promise<boolean>((resolve) => {
    let settled = false;
    const finish = (ok: boolean) => { if (!settled) { settled = true; resolve(ok); } };
    if (typeof preload.decode === 'function') {
      preload.decode().then(() => finish(true), () => finish(false));
      preload.onerror = () => finish(false);
    } else {
      preload.onload = () => finish(true);
      preload.onerror = () => finish(false);
    }
  });
}

function resolveInitialPhotoIndex(photos: readonly PhotoItem[], deck: WidgetPhotoDeckState, wide = true): number {
  if (photos.length === 0) return 0;
  if (deck.currentKey) {
    const idx = photos.findIndex((p, i) => getPhotoItemKey(p, i) === deck.currentKey);
    if (idx !== -1) { deck.seenKeys.add(deck.currentKey); return idx; }
  }
  let unseenIdx = photos.findIndex((p, i) => !deck.seenKeys.has(getPhotoItemKey(p, i)));
  if (unseenIdx === -1) { deck.seenKeys.clear(); unseenIdx = 0; }
  const key = getPhotoItemKey(photos[unseenIdx], unseenIdx);
  deck.currentKey = key;
  deck.seenKeys.add(key);
  const compIdx = wide ? selectCompanionPortraitIndex(photos, unseenIdx, deck.seenKeys) : null;
  const compKey = compIdx !== null ? getPhotoItemKey(photos[compIdx], compIdx) : null;
  deck.companionKey = compKey;
  if (compKey) deck.seenKeys.add(compKey);
  return unseenIdx;
}

export function useContainerSize() {
  const [containerSize, setContainerSize] = useState<ContainerSize>({ width: 0, height: 0 });
  const containerSizeRef = useRef<ContainerSize>({ width: 0, height: 0 });
  const [element, setElement] = useState<HTMLDivElement | null>(null);
  const rootRef = (node: HTMLDivElement | null) => { setElement(node); };

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
    if (emberSyncListeners.size === 0 && typeof window !== 'undefined') delete window.onEmberPhotosSynced;
  };
}

export interface UsePhotoSlideshowTransitionOptions {
  id: string; photos: readonly PhotoItem[]; cycleIntervalSeconds: number;
  isCurrentPane: boolean; isDocumentVisible: boolean; isVideoMode: boolean;
  isPaused: boolean; containerSizeRef: React.MutableRefObject<ContainerSize>;
  wide?: boolean;
}

export interface SlideTransitionState { nextIndex: number; companionIndex: number | null; active: boolean; }

function resolveSavedCompanionIndex(
  photos: readonly PhotoItem[], primaryIndex: number, companionKey: string | null | undefined, seenKeys: ReadonlySet<string>, wide = true,
): number | null {
  if (!wide || photos.length <= 1 || !isPortraitPhoto(photos[primaryIndex])) return null;
  if (companionKey) {
    const idx = photos.findIndex((p, i) => getPhotoItemKey(p, i) === companionKey);
    if (idx !== -1 && idx !== primaryIndex && isPortraitPhoto(photos[idx])) return idx;
  }
  return selectCompanionPortraitIndex(photos, primaryIndex, seenKeys);
}

export function usePhotoSlideshowTransition({
  id, photos, cycleIntervalSeconds, isCurrentPane, isDocumentVisible, isVideoMode, isPaused, containerSizeRef, wide: initialWide = true,
}: UsePhotoSlideshowTransitionOptions) {
  const [currentIndex, setCurrentIndex] = useState(() => resolveInitialPhotoIndex(photos, getWidgetPhotoDeckState(id), initialWide));
  const safeIndex = photos.length > 0 ? currentIndex % photos.length : 0;
  const [rawCompanionIndex, setCurrentCompanionIndex] = useState<number | null>(() => {
    const deck = getWidgetPhotoDeckState(id);
    const compIdx = resolveSavedCompanionIndex(photos, safeIndex, deck.companionKey, deck.seenKeys, initialWide);
    const compKey = compIdx !== null ? getPhotoItemKey(photos[compIdx], compIdx) : null;
    deck.companionKey = compKey;
    if (compKey) deck.seenKeys.add(compKey);
    return compIdx;
  });
  const currentCompanionIndex = initialWide ? rawCompanionIndex : null;
  const currentIndexRef = useRef(safeIndex);
  const currentCompanionIndexRef = useRef(currentCompanionIndex);
  const probeIndexRef = useRef<number | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const photosRef = useRef(photos);
  const isAnimatingRef = useRef(false);
  const [transitionState, setTransitionState] = useState<SlideTransitionState | null>(null);
  const transitionStateRef = useRef<SlideTransitionState | null>(null);
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
    const isTransitionActive = Boolean(transitionStateRef.current?.active);
    const isProbing = !isTransitionActive && probeIndexRef.current !== null;
    const baseIndex = isTransitionActive
      ? transitionStateRef.current!.nextIndex
      : (probeIndexRef.current ?? currentIndexRef.current);

    const deck = getWidgetPhotoDeckState(id);
    const wide = isWideContainer(containerSizeRef.current);
    const baseKey = getPhotoItemKey(currentPhotos[baseIndex], baseIndex);
    const activeCompanionIdx = isTransitionActive
      ? transitionStateRef.current!.companionIndex
      : (isProbing ? null : currentCompanionIndexRef.current);
    const baseSecIdx = wide ? activeCompanionIdx : null;
    const baseSecKey = baseSecIdx !== null ? getPhotoItemKey(currentPhotos[baseSecIdx], baseSecIdx) : null;
    if (!isProbing) {
      deck.currentKey = baseKey;
      deck.companionKey = baseSecKey;
      if (direction === 'prev') {
        deck.seenKeys.delete(baseKey);
        if (baseSecKey) deck.seenKeys.delete(baseSecKey);
      } else {
        deck.seenKeys.add(baseKey);
        if (baseSecKey) deck.seenKeys.add(baseSecKey);
      }
    }

    if (transitionStateRef.current?.active) {
      setCurrentIndex(baseIndex); currentIndexRef.current = baseIndex;
      setCurrentCompanionIndex(baseSecIdx); currentCompanionIndexRef.current = baseSecIdx;
      stopAnimation();
      if (transitionTimeoutRef.current) { clearTimeout(transitionTimeoutRef.current); transitionTimeoutRef.current = null; }
      transitionStateRef.current = null; setTransitionState(null);
    }

    const nextIndex = direction === 'prev' ? selectPrevPhotoIndex(currentPhotos, baseIndex) : selectNextPhotoIndex(currentPhotos, baseIndex, deck.seenKeys);
    const nextPhoto = currentPhotos[nextIndex];
    const nextRawUrl = nextPhoto?.url ?? nextPhoto?.URL;
    const nextKey = getPhotoItemKey(nextPhoto, nextIndex);

    if (!nextRawUrl) {
      deck.seenKeys.add(nextKey); probeIndexRef.current = nextIndex;
      deck.remainingMs = cycleIntervalSeconds * 1000; clearPendingTransition(); setRetryTick((t) => t + 1);
      return;
    }

    const nextSecIdx = wide ? selectCompanionPortraitIndex(currentPhotos, nextIndex, deck.seenKeys) : null;
    const nextSecPhoto = nextSecIdx !== null ? currentPhotos[nextSecIdx] : undefined;
    const nextSecRawUrl = nextSecPhoto?.url ?? nextSecPhoto?.URL;
    const nextSecKey = nextSecIdx !== null ? getPhotoItemKey(nextSecPhoto, nextSecIdx) : null;
    const nextFgUrl = getSizedPhotoURL(nextRawUrl, containerSizeRef.current, isVideoPhoto(nextPhoto));
    const nextSecFgUrl = nextSecRawUrl ? getSizedPhotoURL(nextSecRawUrl, containerSizeRef.current, isVideoPhoto(nextSecPhoto)) : '';
    const seq = ++cycleSeqRef.current;
    if (transitionTimeoutRef.current) { clearTimeout(transitionTimeoutRef.current); transitionTimeoutRef.current = null; }

    const handleDecodeResult = (ok1: boolean, ok2: boolean) => {
      if (cycleSeqRef.current !== seq) return;
      if (!ok1) {
        deck.seenKeys.add(nextKey); probeIndexRef.current = nextIndex;
        deck.remainingMs = cycleIntervalSeconds * 1000; clearPendingTransition(); setRetryTick((t) => t + 1);
        return;
      }
      if (!ok2 && nextSecKey) deck.seenKeys.add(nextSecKey);
      const nextCompanionIndex = ok2 ? nextSecIdx : null;
      const finalSecKey = ok2 ? nextSecKey : null;
      probeIndexRef.current = null; startAnimation();
      const nextState: SlideTransitionState = { nextIndex, companionIndex: nextCompanionIndex, active: true };
      transitionStateRef.current = nextState; setTransitionState(nextState);

      transitionTimeoutRef.current = setTimeout(() => {
        if (cycleSeqRef.current !== seq) return;
        deck.currentKey = nextKey; deck.companionKey = finalSecKey;
        deck.seenKeys.add(nextKey);
        if (finalSecKey) deck.seenKeys.add(finalSecKey);
        setCurrentIndex(nextIndex); currentIndexRef.current = nextIndex;
        setCurrentCompanionIndex(nextCompanionIndex); currentCompanionIndexRef.current = nextCompanionIndex;
        stopAnimation(); transitionStateRef.current = null; setTransitionState(null); transitionTimeoutRef.current = null;
      }, CROSSFADE_MS);
    };

    if (nextSecFgUrl) {
      Promise.all([preloadAndDecode(nextFgUrl), preloadAndDecode(nextSecFgUrl)]).then(([ok1, ok2]) => handleDecodeResult(ok1, ok2));
    } else {
      preloadAndDecode(nextFgUrl).then((ok1) => handleDecodeResult(ok1, true));
    }
  };

  const stepPhoto = (direction: 'prev' | 'next') => {
    const deck = getWidgetPhotoDeckState(id);
    deck.remainingMs = cycleIntervalSeconds * 1000;
    triggerAdvance(direction);
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    if (isCurrentPane && isDocumentVisible && !isVideoMode && !isPaused) {
      activeDelayMsRef.current = cycleIntervalSeconds * 1000;
      cycleStartedAtRef.current = Date.now();
      scheduleCycleTick(timerRef, activeDelayMsRef, cycleStartedAtRef, deck, cycleIntervalSeconds, triggerAdvance, cycleIntervalSeconds * 1000);
    }
  };

  useLayoutEffect(() => {
    const size = containerSizeRef.current;
    if (size.width > 0 && size.height > 0 && size.width / size.height < 1.15) {
      const deck = getWidgetPhotoDeckState(id);
      if (deck.companionKey) { deck.seenKeys.delete(deck.companionKey); deck.companionKey = null; }
      currentCompanionIndexRef.current = null;
    } else {
      currentCompanionIndexRef.current = currentCompanionIndex;
    }
    currentIndexRef.current = safeIndex;
    photosRef.current = photos;
    transitionStateRef.current = transitionState;
  });

  useEffect(() => {
    probeIndexRef.current = null;
    if (photos.length === 0) return;
    const deck = getWidgetPhotoDeckState(id);
    const wide = isWideContainer(containerSizeRef.current);
    const targetIdx = resolveInitialPhotoIndex(photos, deck, wide);
    const compIdx = resolveSavedCompanionIndex(photos, targetIdx, deck.companionKey, deck.seenKeys, wide);
    const compKey = compIdx !== null ? getPhotoItemKey(photos[compIdx], compIdx) : null;
    deck.companionKey = compKey;
    if (compKey) deck.seenKeys.add(compKey);
    if (currentIndexRef.current !== targetIdx) { setCurrentIndex(targetIdx); currentIndexRef.current = targetIdx; }
    if (currentCompanionIndexRef.current !== compIdx) { setCurrentCompanionIndex(compIdx); currentCompanionIndexRef.current = compIdx; }
  }, [photos, id, containerSizeRef]);

  const clearPendingTransitionRef = useRef(clearPendingTransition);
  const triggerAdvanceRef = useRef(triggerAdvance);
  useEffect(() => { clearPendingTransitionRef.current = clearPendingTransition; triggerAdvanceRef.current = triggerAdvance; });

  useEffect(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    const shouldRun = isCurrentPane && isDocumentVisible && !isVideoMode && !isPaused && photos.length > 1;
    const deck = getWidgetPhotoDeckState(id);

    if (!shouldRun) {
      if (cycleStartedAtRef.current !== null) {
        deck.remainingMs = Math.max(1, activeDelayMsRef.current - (Date.now() - cycleStartedAtRef.current));
        cycleStartedAtRef.current = null;
      }
      const pausedOnly = isPaused && isCurrentPane && isDocumentVisible && !isVideoMode && photos.length > 1;
      if (!pausedOnly) clearPendingTransitionRef.current();
      return;
    }

    const initialDelayMs = deck.remainingMs ?? cycleIntervalSeconds * 1000;
    activeDelayMsRef.current = initialDelayMs;
    cycleStartedAtRef.current = Date.now();
    scheduleCycleTick(timerRef, activeDelayMsRef, cycleStartedAtRef, deck, cycleIntervalSeconds, () => triggerAdvanceRef.current(), initialDelayMs);

    return () => {
      if (cycleStartedAtRef.current !== null) {
        deck.remainingMs = Math.max(1, activeDelayMsRef.current - (Date.now() - cycleStartedAtRef.current));
        cycleStartedAtRef.current = null;
      }
      if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; }
    };
  }, [isCurrentPane, isDocumentVisible, isVideoMode, isPaused, photos.length, cycleIntervalSeconds, retryTick, id]);

  useEffect(() => () => { clearPendingTransitionRef.current(); }, []);

  return { safeIndex, currentCompanionIndex, transitionState, stepPhoto };
}
