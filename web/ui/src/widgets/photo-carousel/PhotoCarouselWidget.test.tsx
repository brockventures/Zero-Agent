import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, fireEvent, act } from '@testing-library/react';
import {
  PhotoCarouselWidget,
  getSizedPhotoURL,
  getBackdropPhotoURL,
  parsePhotoCarouselData,
  getPhotoItemKey,
  selectNextPhotoIndex,
  selectPrevPhotoIndex,
  selectPreloadPhotoIndices,
  photoDeckStateById,
  isPortraitPhoto,
  isWideContainer,
  selectCompanionPortraitIndex,
  isVideoPhoto,
  getVideoStreamURL,
} from './PhotoCarouselWidget';
import { useDisplayStore } from '../../store';
import type { WidgetMeta } from '../../store/types';
import { PaneContext } from '../../carousel/PaneContext';
import { endAnimation } from '../../perf/animating';

function makeWidgetMeta(config: Record<string, unknown> = {}): WidgetMeta {
  return { type: 'photo-carousel', config, origin: [0, 0], dimensions: [2, 2], assets_base: '' };
}

function setWidgetPhotos(
  photos: Array<{ id?: string; url?: string; URL?: string; width?: number; height?: number; aspect_ratio?: number; timestamp?: string; location?: string; album_name?: string; media_type?: string }>,
  cycleSeconds?: number, config: Record<string, unknown> = {},
) {
  useDisplayStore.setState({
    widgets: {
      ['w-photo-1']: {
        meta: makeWidgetMeta(config),
        data: { photos, ...(cycleSeconds !== undefined ? { cycle_interval_seconds: cycleSeconds } : {}) },
        state: 'ready',
        timestamp: '2026-10-31T12:00:00Z',
      },
    },
  });
}

describe('PhotoCarouselWidget helpers', () => {
  describe('parsePhotoCarouselData', () => {
    it('returns empty object when data is nullish or invalid', () => {
      expect(parsePhotoCarouselData(null)).toEqual({});
      expect(parsePhotoCarouselData(undefined)).toEqual({});
      expect(parsePhotoCarouselData(123)).toEqual({});
      expect(parsePhotoCarouselData('not-json')).toEqual({});
    });

    it('returns object as-is if already an object', () => {
      const data = { photos: [{ url: 'https://example.com/1.jpg' }], cycle_interval_seconds: 45 };
      expect(parsePhotoCarouselData(data)).toEqual(data);
    });

    it('parses single-encoded JSON string', () => {
      const data = { photos: [{ url: 'https://example.com/1.jpg' }], cycle_interval_seconds: 45 };
      expect(parsePhotoCarouselData(JSON.stringify(data))).toEqual(data);
    });

    it('parses double-encoded JSON string', () => {
      const data = { photos: [{ url: 'https://example.com/1.jpg' }], cycle_interval_seconds: 45 };
      expect(parsePhotoCarouselData(JSON.stringify(JSON.stringify(data)))).toEqual(data);
    });
  });

  describe('getBackdropPhotoURL', () => {
    it('returns empty string when rawUrl is missing', () => {
      expect(getBackdropPhotoURL('')).toBe('');
      expect(getBackdropPhotoURL(undefined)).toBe('');
    });

    it('returns =w96-h54-c url when rawUrl is provided', () => {
      expect(getBackdropPhotoURL('https://example.com/photo.jpg')).toBe(
        'https://example.com/photo.jpg=w96-h54-c',
      );
    });
  });

  describe('getSizedPhotoURL', () => {
    it('returns empty string if rawUrl is empty', () => {
      expect(getSizedPhotoURL('', { width: 800, height: 600 })).toBe('');
      expect(getSizedPhotoURL(undefined)).toBe('');
    });

    it('computes sized URL based on containerSize, DPR, and screen resolution without -c', () => {
      const originalDpr = window.devicePixelRatio;
      window.devicePixelRatio = 2;
      try {
        const url = getSizedPhotoURL('https://example.com/pic.jpg', { width: 400, height: 300 });
        expect(url).toBe('https://example.com/pic.jpg=w800-h600');
        expect(url).not.toContain('-c');
        const fallbackUrl = getSizedPhotoURL('https://example.com/pic.jpg', { width: 0, height: 0 });
        expect(fallbackUrl).toBe('https://example.com/pic.jpg=w2048-h1536');
      } finally {
        window.devicePixelRatio = originalDpr;
      }
    });

    it('computes sized URL when containerSize exceeds screen resolution', () => {
      const originalDpr = window.devicePixelRatio;
      window.devicePixelRatio = 1;
      try {
        const url = getSizedPhotoURL('https://example.com/pic.jpg', { width: 2500, height: 1500 });
        expect(url).toBe('https://example.com/pic.jpg=w2500-h1500');
        expect(url).not.toContain('-c');
      } finally {
        window.devicePixelRatio = originalDpr;
      }
    });

    it('appends -no suffix when isVideo is true', () => {
      expect(getSizedPhotoURL('https://example.com/video.jpg', { width: 800, height: 600 }, true)).toBe('https://example.com/video.jpg=w800-h600-no');
    });
  });

  describe('isVideoPhoto and getVideoStreamURL', () => {
    it('detects video photo and constructs stream URLs with fallback suffixes', () => {
      expect(isVideoPhoto(undefined)).toBe(false);
      expect(isVideoPhoto({})).toBe(false);
      expect(isVideoPhoto({ media_type: 'photo' })).toBe(false);
      expect(isVideoPhoto({ media_type: 'video' })).toBe(true);
      expect(getVideoStreamURL('')).toBe('');
      expect(getVideoStreamURL(undefined)).toBe('');
      expect(getVideoStreamURL('https://example.com/vid', 0)).toBe('https://example.com/vid=m22');
      expect(getVideoStreamURL('https://example.com/vid', 1)).toBe('https://example.com/vid=m18');
      expect(getVideoStreamURL('https://example.com/vid', 2)).toBe('https://example.com/vid=dv');
      expect(getVideoStreamURL('https://example.com/vid', 5)).toBe('https://example.com/vid=dv');
    });
  });

  describe('getPhotoItemKey', () => {
    it('returns raw URL when url or URL is present', () => {
      expect(getPhotoItemKey({ url: 'https://example.com/a.jpg' })).toBe('https://example.com/a.jpg');
      expect(getPhotoItemKey({ URL: 'https://example.com/b.jpg' })).toBe('https://example.com/b.jpg');
      expect(getPhotoItemKey({ url: 'https://example.com/c.jpg', id: 'id-c' })).toBe('https://example.com/c.jpg');
    });

    it('returns id when URL is absent', () => {
      expect(getPhotoItemKey({ id: 'photo-123' })).toBe('photo-123');
      expect(getPhotoItemKey({ id: 'photo-456', url: '' })).toBe('photo-456');
    });

    it('falls back to index string when URL and id are missing', () => {
      expect(getPhotoItemKey({}, 5)).toBe('5');
      expect(getPhotoItemKey(undefined, 2)).toBe('2');
      expect(getPhotoItemKey(undefined)).toBe('');
    });
  });

  describe('selectNextPhotoIndex', () => {
    const photos = [{ id: '1', url: 'https://example.com/1.jpg' }, { id: '2', url: 'https://example.com/2.jpg' }, { id: '3', url: 'https://example.com/3.jpg' }];

    it('returns 0 for empty or single-item photo arrays', () => {
      expect(selectNextPhotoIndex([], 0, new Set())).toBe(0);
      expect(selectNextPhotoIndex([{ id: '1', url: 'https://example.com/1.jpg' }], 0, new Set(['https://example.com/1.jpg']))).toBe(0);
    });

    it('selects next unseen photo in sequence', () => {
      const seen = new Set(['https://example.com/1.jpg']);
      const next = selectNextPhotoIndex(photos, 0, seen);
      expect(next).toBe(1);
    });

    it('skips seen photos to find the next unseen candidate', () => {
      const seen = new Set(['https://example.com/1.jpg', 'https://example.com/2.jpg']);
      const next = selectNextPhotoIndex(photos, 0, seen);
      expect(next).toBe(2);
    });

    it('clears seenKeys and wraps around when all photos are seen', () => {
      const seen = new Set(['https://example.com/1.jpg', 'https://example.com/2.jpg', 'https://example.com/3.jpg']);
      const next = selectNextPhotoIndex(photos, 2, seen);
      expect(next).toBe(0);
      expect(seen.size).toBe(0);
    });
  });

  describe('selectPrevPhotoIndex', () => {
    const photos = [{ id: '1', url: 'https://example.com/1.jpg' }, { id: '2', url: 'https://example.com/2.jpg' }, { id: '3', url: 'https://example.com/3.jpg' }];

    it('returns 0 for empty or single-item photo arrays', () => {
      expect(selectPrevPhotoIndex([], 0)).toBe(0);
      expect(selectPrevPhotoIndex([{ id: '1', url: 'https://example.com/1.jpg' }], 0)).toBe(0);
    });

    it('steps backward in sequence and wraps around from 0 to last photo', () => {
      expect(selectPrevPhotoIndex(photos, 2)).toBe(1);
      expect(selectPrevPhotoIndex(photos, 1)).toBe(0);
      expect(selectPrevPhotoIndex(photos, 0)).toBe(2);
    });

    it('handles negative or out-of-range base indices gracefully', () => {
      expect(selectPrevPhotoIndex(photos, -1)).toBe(1);
      expect(selectPrevPhotoIndex(photos, 5)).toBe(1);
    });
  });

  describe('selectPreloadPhotoIndices', () => {
    const photos = Array.from({ length: 12 }, (_, i) => ({
      id: String(i + 1),
      url: i === 2 ? '' : `https://example.com/${i + 1}.jpg`,
    }));

    it('returns empty array when photos has 0 or 1 item or count <= 0', () => {
      expect(selectPreloadPhotoIndices([], 0)).toEqual([]);
      expect(selectPreloadPhotoIndices([{ id: '1', url: 'https://example.com/1.jpg' }], 0)).toEqual([]);
      expect(selectPreloadPhotoIndices(photos, 0, undefined, 0)).toEqual([]);
    });

    it('selects upcoming unseen photos skipping empty URLs and includes previous photo', () => {
      const indices = selectPreloadPhotoIndices(photos, 0, new Set(), 4);
      expect(indices).toEqual([1, 3, 4, 5, 11]);
    });
  });

  describe('isPortraitPhoto, isWideContainer, and selectCompanionPortraitIndex', () => {
    it('detects portrait photos via aspect_ratio < 0.95 or width < height', () => {
      expect(isPortraitPhoto(undefined)).toBe(false);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg' })).toBe(false);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', aspect_ratio: 0.75 })).toBe(true);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', aspect_ratio: 0.94 })).toBe(true);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', aspect_ratio: 0.95 })).toBe(false);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', aspect_ratio: 1.33 })).toBe(false);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', width: 900, height: 1200 })).toBe(true);
      expect(isPortraitPhoto({ url: 'https://example.com/1.jpg', width: 1200, height: 900 })).toBe(false);
    });

    it('detects wide containers when width/height >= 1.15 or unmeasured 0x0 fallback', () => {
      expect(isWideContainer({ width: 0, height: 0 })).toBe(true);
      expect(isWideContainer({ width: 1150, height: 1000 })).toBe(true);
      expect(isWideContainer({ width: 1600, height: 900 })).toBe(true);
      expect(isWideContainer({ width: 1000, height: 1000 })).toBe(false);
      expect(isWideContainer({ width: 600, height: 800 })).toBe(false);
    });

    it('selects next unseen portrait companion photo from deck', () => {
      const deck = [
        { id: '1', url: 'https://example.com/p1.jpg', aspect_ratio: 0.75 }, { id: '2', url: 'https://example.com/l1.jpg', aspect_ratio: 1.33 },
        { id: '3', url: 'https://example.com/p2.jpg', aspect_ratio: 0.8 }, { id: '4', url: 'https://example.com/p3.jpg', width: 800, height: 1200 },
      ];
      expect(selectCompanionPortraitIndex(deck, 1, new Set())).toBeNull();
      expect(selectCompanionPortraitIndex(deck, 0, new Set())).toBe(2);
      expect(selectCompanionPortraitIndex(deck, 0, new Set(['https://example.com/p2.jpg']))).toBe(3);
      // Falls back to first portrait companion when all other portraits are seen
      expect(
        selectCompanionPortraitIndex(
          deck,
          0,
          new Set(['https://example.com/p2.jpg', 'https://example.com/p3.jpg']),
        ),
      ).toBe(2);
    });
  });
});

describe('PhotoCarouselWidget component', () => {
  const widgetId = 'w-photo-1';
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    vi.useFakeTimers();
    useDisplayStore.setState(initialStore, true);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    while (document.body.dataset.animating === '1') {
      endAnimation();
    }
    delete document.body.dataset.animating;
  });

  it('renders loading state when widget data has not loaded or has not hydrated photos array', () => {
    useDisplayStore.setState({ widgets: { [widgetId]: { meta: undefined, data: undefined, state: 'loading', timestamp: '' } } });

    const { container, rerender } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
    );

    let loading = container.querySelector('.photo-loading');
    expect(loading).not.toBeNull();
    expect(loading?.textContent).toContain('Loading photos...');

    // Also renders loading state when state is healthy but photos array is not yet present
    useDisplayStore.setState({
      widgets: { [widgetId]: { meta: makeWidgetMeta({}), data: {}, state: 'healthy', timestamp: '2026-10-31T12:00:00Z' } },
    });

    rerender(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
    );

    loading = container.querySelector('.photo-loading');
    expect(loading).not.toBeNull();
    expect(loading?.textContent).toContain('Loading photos...');
  });

  it('renders empty state when photos array is empty', () => {
    useDisplayStore.setState({
      widgets: { [widgetId]: { meta: makeWidgetMeta({}), data: { photos: [] }, state: 'ready', timestamp: '2026-10-31T12:00:00Z' } },
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
    );

    const empty = container.querySelector('.photo-empty');
    expect(empty).not.toBeNull();
    expect(empty?.querySelector('.empty-title')?.textContent).toBe('No photos in shared album');
    expect(empty?.querySelector('.empty-icon svg')).not.toBeNull();
  });

  it('renders photo viewport with backdrop and foreground for cover fit', () => {
    setWidgetPhotos(
      [{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }],
      10,
    );

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
    );

    const root = container.querySelector('.widget-photo-carousel');
    expect(root).not.toBeNull();
    expect(root?.getAttribute('data-fit')).toBe('cover');
    expect(root?.classList.contains('dither')).toBe(true);
    // Invariant: no duplicate data-widget-id on inner element
    expect(root?.getAttribute('data-widget-id')).toBeNull();

    const viewport = container.querySelector('.photo-viewport');
    expect(viewport?.classList.contains('dither')).toBe(true);

    const backdropContainer = container.querySelector('.photo-backdrop-container');
    expect(backdropContainer).not.toBeNull();

    const currentBackdrop = container.querySelector('.photo-backdrop-current');
    expect(currentBackdrop?.getAttribute('src')).toBe('https://example.com/p1.jpg=w96-h54-c');

    const currentFg = container.querySelector('.photo-layer.photo-current');
    expect(currentFg?.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
    expect(currentFg?.classList.contains('dither')).toBe(true);
  });

  it('cycles photos automatically with decode preloading', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos(
        [{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }],
        10,
      );

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Fast-forward interval timer to trigger cycle advance callback
      act(() => { vi.advanceTimersByTime(10000);
 });

      // Decode has not resolved yet -> next image is not yet in DOM with opacity 1
      let nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      expect(nextFg.getAttribute('src')).toBeNull();

      // In JSDOM with fake timers, if setInterval didn't advance, advance 10000 more
      if (!resolveDecode) {
        act(() => {
          vi.advanceTimersByTime(10000);
   });
      }

      // Resolve decode and let React process state update
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      let nextFgTransitioned = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      await vi.waitFor(() => {
        nextFgTransitioned = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFgTransitioned.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
 });

      expect(nextFgTransitioned.style.opacity).toBe('1');
      expect(currentFg.style.opacity).toBe('0');

      // Fast-forward 850ms crossfade
      act(() => { vi.advanceTimersByTime(850);
 });

      // Next image committed to current
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      expect(currentFg.style.opacity).toBe('1');
      nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      expect(nextFg.getAttribute('src')).toBeNull();
      expect(nextFg.style.opacity).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('supports tap/click to advance to next photo immediately and resets interval', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 15);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel')!;
      act(() => { fireEvent.click(root); });
      expect(resolveDecode).not.toBeNull();
      act(() => { resolveDecode!(); });
      let nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('immediately commits in-flight transition and advances to next photo when tapped mid-crossfade', async () => {
    const decodeResolvers: Array<() => void> = [];
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { decodeResolvers.push(resolve); }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }, { url: 'https://example.com/p3.jpg' }], 15);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel')!;
      act(() => { fireEvent.click(root); });
      expect(decodeResolvers.length).toBe(1);
      act(() => { decodeResolvers[0](); });

      let nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      });

      // While crossfade is active (before 850ms timeout), click again!
      act(() => { fireEvent.click(root); });
      expect(decodeResolvers.length).toBe(2);
      act(() => { decodeResolvers[1](); });

      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
      });

      act(() => { vi.advanceTimersByTime(850); });
      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('pauses cycle timer and clears transitions when not current pane', async () => {
    setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 10);

    const { rerender, container } = render(
      <PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    act(() => {
      vi.advanceTimersByTime(30000);
    });

    const currentFg = container.querySelector('.photo-layer.photo-current');
    expect(currentFg?.getAttribute('src')).toContain('p1.jpg');

    // Make it current pane
    rerender(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
    );
  });

  it('sizes photo URLs via ResizeObserver without synchronous getBoundingClientRect', () => {
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) {
        resizeCallback = cb;
      }
      observe = vi.fn();
      disconnect = vi.fn();
      unobserve = vi.fn();
    }
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    const getBoundingClientRectSpy = vi.spyOn(Element.prototype, 'getBoundingClientRect');

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // Invariant: no synchronous getBoundingClientRect on mount!
      expect(getBoundingClientRectSpy).not.toHaveBeenCalled();

      // Initial render uses fallback screen/window size before ResizeObserver delivers box
      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toMatch(/^https:\/\/example\.com\/p1\.jpg=w\d+-h\d+$/);

      // ResizeObserver delivers measured container size
      expect(resizeCallback).not.toBeNull();
      act(() => {
        resizeCallback!([{ contentRect: { width: 1400, height: 900 } }]);
 });

      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1400-h900');
    } finally {
      window.ResizeObserver = originalResizeObserver;
      getBoundingClientRectSpy.mockRestore();
    }
  });

  it('tracks container size via ResizeObserver when transitioning from loading state to ready photos', () => {
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) {
        resizeCallback = cb;
      }
      observe = vi.fn();
      disconnect = vi.fn();
      unobserve = vi.fn();
    }
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: undefined,
            data: undefined,
            state: 'loading',
            timestamp: '',
          },
        },
 });

      const { container, rerender } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      expect(container.querySelector('.photo-loading')).not.toBeNull();

      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }], 10);
      rerender(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      expect(resizeCallback).not.toBeNull();
      act(() => {
        resizeCallback!([{ contentRect: { width: 1400, height: 900 } }]);
 });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1400-h900');
    } finally {
      window.ResizeObserver = originalResizeObserver;
    }
  });

  it('includes dither class on photo-next during active transition and defaults interval to 30s', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], undefined);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // Should NOT advance at 15 seconds
      act(() => { vi.advanceTimersByTime(15000);
 });
      expect(resolveDecode).toBeNull();

      // Advancing to 30 seconds triggers the cycle timer
      act(() => { vi.advanceTimersByTime(15000);
 });
      expect(resolveDecode).not.toBeNull();

      // Resolve decode to enter active transition
      act(() => {
        resolveDecode!();
 });
      let nextFg: HTMLImageElement | null = null;
      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg).not.toBeNull();
        expect(nextFg.getAttribute('src')).toContain('p2.jpg');
 });

      expect(nextFg!.classList.contains('dither')).toBe(true);
    } finally {
      window.Image = originalImage;
    }
  });

  it('sets document.body.dataset.animating to 1 during crossfade and 0 after CROSSFADE_MS', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 10);

      render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      expect(document.body.dataset.animating).toBeUndefined();

      act(() => { vi.advanceTimersByTime(10000);
 });

      expect(resolveDecode).not.toBeNull();
      expect(document.body.dataset.animating).toBeUndefined();

      act(() => {
        resolveDecode!();
 });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
 });

      act(() => { vi.advanceTimersByTime(850);
 });

      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('resets document.body.dataset.animating to 0 on early tap commit mid-crossfade', async () => {
    const decodeResolvers: Array<() => void> = [];
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { decodeResolvers.push(resolve); }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos(
        [
          { url: 'https://example.com/p1.jpg' },
          { url: 'https://example.com/p2.jpg' },
          { url: 'https://example.com/p3.jpg' },
        ],
        15,
      );

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel')!;
      act(() => {
        fireEvent.click(root);
 });

      expect(decodeResolvers.length).toBe(1);
      act(() => {
        decodeResolvers[0]();
 });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
 });

      // Tap mid-crossfade: should commit p2 and reset animating to 0 immediately
      act(() => {
        fireEvent.click(root);
 });

      expect(document.body.dataset.animating).toBe('0');
      expect(decodeResolvers.length).toBe(2);

      act(() => {
        decodeResolvers[1]();
 });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
 });

      act(() => { vi.advanceTimersByTime(850);
 });

      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('resets document.body.dataset.animating to 0 on unmount mid-crossfade', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 10);

      const { unmount } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      act(() => { vi.advanceTimersByTime(10000);
 });

      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
 });

      unmount();

      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('aborts transition when Image.decode() rejects and advances to subsequent valid photo on next cycle', async () => {
    const originalImage = window.Image;
    const decodeCalls: string[] = [];

    class MockImage {
      _src = '';
      set src(v: string) { this._src = v; }
      get src() { return this._src; }
      decode() {
        decodeCalls.push(this._src);
        return this._src.includes('p2.jpg') ? Promise.reject(new Error('Corrupt image data')) : Promise.resolve();
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([
        { url: 'https://example.com/p1.jpg' },
        { url: 'https://example.com/p2.jpg' },
        { url: 'https://example.com/p3.jpg' },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      await act(async () => { vi.advanceTimersByTime(10000); });
      expect(decodeCalls.some((url) => url.includes('p2.jpg'))).toBe(true);

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).not.toBe('1');
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBeNull();
        expect(nextFg.style.opacity).toBe('0');
        expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
      });

      await act(async () => { vi.advanceTimersByTime(10000); });
      expect(decodeCalls.some((url) => url.includes('p3.jpg'))).toBe(true);

      await vi.waitFor(() => {
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
        expect(document.body.dataset.animating).toBe('1');
      });

      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('aborts transition when onerror fires for the next photo URL and advances to subsequent valid photo', async () => {
    const originalImage = window.Image;
    const instances: MockImage[] = [];

    class MockImage {
      _src = '';
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(v: string) { this._src = v; }
      get src() { return this._src; }
      constructor() { instances.push(this); }
    }
    // @ts-expect-error mock window.Image without decode
    window.Image = MockImage;

    try {
      setWidgetPhotos([
        { url: 'https://example.com/p1.jpg' },
        { url: 'https://example.com/p2.jpg' },
        { url: 'https://example.com/p3.jpg' },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      act(() => { vi.advanceTimersByTime(10000); });
      const p2Img = instances.find((img) => img._src.includes('p2.jpg'));
      expect(p2Img).toBeDefined();
      act(() => { p2Img!.onerror?.(); });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).not.toBe('1');
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBeNull();
        expect(nextFg.style.opacity).toBe('0');
        expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
      });

      act(() => { vi.advanceTimersByTime(10000); });
      const p3Img = instances.find((img) => img._src.includes('p3.jpg'));
      expect(p3Img).toBeDefined();
      act(() => { p3Img!.onload?.(); });

      await vi.waitFor(() => {
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
        expect(document.body.dataset.animating).toBe('1');
      });

      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('stops crossfade and cycle timer immediately when entering video mode and stays stopped', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({}),
            data: {
              photos: [
                { url: 'https://example.com/p1.jpg' },
                { url: 'https://example.com/p2.jpg' },
                { url: 'https://example.com/p3.jpg' },
              ],
              cycle_interval_seconds: 10,
            },
            state: 'ready',
            timestamp: '2026-10-31T12:00:00Z',
          },
        },
 });

      render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // (a) advance cycle timer and resolve Image.decode() so crossfade starts
      act(() => { vi.advanceTimersByTime(10000);
 });

      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
 });

      // (b) enter video mode mid-crossfade
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'cam', stream_url: 'http://cam/mjpeg', type: 'mjpeg' },
            pip: null,
          },
   });
 });

      // (c) crossfade and cycle timer immediately stop with animating === '0'
      expect(document.body.dataset.animating).toBe('0');

      // (d) stays stopped across subsequent timer advances
      resolveDecode = null;
      act(() => { vi.advanceTimersByTime(30000);
 });

      expect(resolveDecode).toBeNull();
      expect(document.body.dataset.animating).toBe('0');
    } finally {
      window.Image = originalImage;
    }
  });

  it('preserves active photo anchor across SSE reorder update', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({}),
            data: {
              photos: [
                { id: '1', url: 'https://example.com/p1.jpg' },
                { id: '2', url: 'https://example.com/p2.jpg' },
                { id: '3', url: 'https://example.com/p3.jpg' },
              ],
              cycle_interval_seconds: 10,
            },
            state: 'ready',
            timestamp: '2026-10-31T12:00:00Z',
          },
        },
 });

      const { container, rerender } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Advance to p2
      act(() => { vi.advanceTimersByTime(10000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');

      // Now simulate backend SSE update with re-ordered deck: [p3, p1, p2]
      act(() => {
        useDisplayStore.setState({
          widgets: {
            [widgetId]: {
              meta: makeWidgetMeta({}),
              data: {
                photos: [
                  { id: '3', url: 'https://example.com/p3.jpg' },
                  { id: '1', url: 'https://example.com/p1.jpg' },
                  { id: '2', url: 'https://example.com/p2.jpg' },
                ],
                cycle_interval_seconds: 10,
              },
              state: 'ready',
              timestamp: '2026-10-31T12:01:00Z',
            },
          },
   });
 });

      rerender(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // p2 is still anchored because currentKey matched
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.currentKey).toBe('https://example.com/p2.jpg');
    } finally {
      window.Image = originalImage;
    }
  });

  it('exhausts unseen deck without replacement before repeating', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({}),
            data: {
              photos: [
                { id: '1', url: 'https://example.com/p1.jpg' },
                { id: '2', url: 'https://example.com/p2.jpg' },
                { id: '3', url: 'https://example.com/p3.jpg' },
              ],
              cycle_interval_seconds: 10,
            },
            state: 'ready',
            timestamp: '2026-10-31T12:00:00Z',
          },
        },
 });

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Cycle 1 -> p2
      act(() => { vi.advanceTimersByTime(10000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');

      // Cycle 2 -> p3
      resolveDecode = null;
      act(() => { vi.advanceTimersByTime(10000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p3.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');

      // Cycle 3 -> all 3 seen, resets seenKeys and selects p1
      resolveDecode = null;
      act(() => { vi.advanceTimersByTime(10000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p1.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('preserves remaining cycle time across isCurrentPane toggles without neighbour overwrite', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({}),
            data: {
              photos: [
                { id: '1', url: 'https://example.com/p1.jpg' },
                { id: '2', url: 'https://example.com/p2.jpg' },
              ],
              cycle_interval_seconds: 10,
            },
            state: 'ready',
            timestamp: '2026-10-31T12:00:00Z',
          },
        },
 });

      const { container, rerender } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // Advance 4 seconds into 10s cycle
      act(() => { vi.advanceTimersByTime(4000);
 });

      // Pause by switching pane away (screen rotation)
      rerender(
        <PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 1 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.remainingMs).toBe(6000);

      // Mount an inactive neighbour pane - must not overwrite remainingMs
      const { unmount: unmountNeighbour } = render(
        <PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 2 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );
      unmountNeighbour();
      expect(deck?.remainingMs).toBe(6000);

      // Rotate back to active pane
      rerender(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      // Advance 5 seconds - not yet triggered (needs 6s)
      act(() => { vi.advanceTimersByTime(5000);
 });
      expect(resolveDecode).toBeNull();

      // Advance 1 more second (completes remaining 6s)
      act(() => { vi.advanceTimersByTime(1000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('marks empty URL candidate as seen and advances to subsequent unseen photo', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() { return new Promise<void>((resolve) => { resolveDecode = resolve; }); }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({}),
            data: {
              photos: [
                { id: '1', url: 'https://example.com/p1.jpg' },
                { id: '2', url: '' }, // Broken URL
                { id: '3', url: 'https://example.com/p3.jpg' },
              ],
              cycle_interval_seconds: 10,
            },
            state: 'ready',
            timestamp: '2026-10-31T12:00:00Z',
          },
        },
 });

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Tick 1: attempts to advance, candidate 2 has empty URL -> marks seen, resets remainingMs, sets retryTick
      act(() => { vi.advanceTimersByTime(10000);
 });

      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.seenKeys.has('2')).toBe(true);

      // Tick 2: advances to photo 3 (skipping broken photo 2)
      act(() => { vi.advanceTimersByTime(10000);
 });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p3.jpg');
 });
      act(() => { vi.advanceTimersByTime(850);
 });

      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('steps to previous photo on left-third tap and photo-nav-prev button click, and subsequent next returns to stepped-from photo', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() {
        return new Promise<void>((res) => { resolveDecode = res; });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([
        { id: '1', url: 'https://example.com/p1.jpg' },
        { id: '2', url: 'https://example.com/p2.jpg' },
        { id: '3', url: 'https://example.com/p3.jpg' },
      ], 15);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel') as HTMLDivElement;
      vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200, x: 0, y: 0, toJSON: () => {} });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // 1. Left-third tap (clientX = 50 < 300/3): steps back from p1 (index 0) to p3 (index 2)
      act(() => { fireEvent.click(root, { clientX: 50 }); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
 });
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');

      // 2. Click .photo-nav-prev button: steps back from p3 (index 2) to p2 (index 1)
      const prevBtn = container.querySelector('.photo-nav-btn.photo-nav-prev') as HTMLButtonElement;
      expect(prevBtn.getAttribute('aria-label')).toBe('Previous photo');
      resolveDecode = null;
      act(() => { fireEvent.click(prevBtn); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
 });
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');

      // 3. Subsequent Next tap returns to stepped-from photo (p3)
      const nextBtn = container.querySelector('.photo-nav-btn.photo-nav-next') as HTMLButtonElement;
      resolveDecode = null;
      act(() => { fireEvent.click(nextBtn); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
 });
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('advances to next photo on right two-thirds tap and photo-nav-next button click', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;
    class MockImage {
      src = '';
      decode() {
        return new Promise<void>((res) => { resolveDecode = res; });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([
        { id: '1', url: 'https://example.com/p1.jpg' },
        { id: '2', url: 'https://example.com/p2.jpg' },
        { id: '3', url: 'https://example.com/p3.jpg' },
      ], 15);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel') as HTMLDivElement;
      vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, right: 300, bottom: 200, width: 300, height: 200, x: 0, y: 0, toJSON: () => {} });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Right two-thirds tap (clientX = 200 >= 300/3): advances to p2
      act(() => { fireEvent.click(root, { clientX: 200 }); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
 });
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');

      // photo-nav-next button: advances to p3
      const nextBtn = container.querySelector('.photo-nav-btn.photo-nav-next') as HTMLButtonElement;
      expect(nextBtn.getAttribute('aria-label')).toBe('Next photo');
      resolveDecode = null;
      act(() => { fireEvent.click(nextBtn); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
 });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
 });
      act(() => { vi.advanceTimersByTime(850); });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
    } finally {
      window.Image = originalImage;
    }
  });

  it('pairs two portrait photos side-by-side in a wide container and renders single landscape photo', async () => {
    const decodeResolvers: Array<() => void> = [];
    const originalImage = window.Image;
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((res) => { decodeResolvers.push(res); }); }
    }
    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
      observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    try {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'p1', url: 'https://example.com/portrait1.jpg', aspect_ratio: 0.75, timestamp: '2026-10-10T12:00:00Z', location: 'Chicago' },
        { id: 'l1', url: 'https://example.com/landscape1.jpg', aspect_ratio: 1.33, timestamp: '2026-10-11T12:00:00Z', location: 'Denver' },
        { id: 'p2', url: 'https://example.com/portrait2.jpg', aspect_ratio: 0.75, timestamp: '2026-10-12T12:00:00Z', location: 'Seattle' },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      act(() => { resizeCallback?.([{ contentRect: { width: 1400, height: 900 } }]); });

      const diptych = container.querySelector('.photo-layer.photo-current.photo-foreground-diptych');
      expect(diptych).not.toBeNull();
      const diptychImgs = diptych!.querySelectorAll('img.photo-diptych-item');
      expect(diptychImgs.length).toBe(2);
      expect(diptychImgs[0].getAttribute('src')).toBe('https://example.com/portrait1.jpg=w1400-h900');
      expect(diptychImgs[1].getAttribute('src')).toBe('https://example.com/portrait2.jpg=w1400-h900');
      expect(container.querySelector('.photo-metadata-overlay')?.textContent).toBe('Oct 10, 2026 • Oct 12, 2026\nChicago • Seattle');

      act(() => { vi.advanceTimersByTime(10000); });
      expect(decodeResolvers.length).toBeGreaterThanOrEqual(1);
      act(() => { while (decodeResolvers.length > 0) decodeResolvers.shift()?.(); });

      await vi.waitFor(() => {
        expect(container.querySelector('img.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/landscape1.jpg=w1400-h900');
      });

      act(() => { vi.advanceTimersByTime(850); });
      expect(container.querySelector('.photo-foreground-diptych')).toBeNull();
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toBe('https://example.com/landscape1.jpg=w1400-h900');
      expect(container.querySelector('.photo-metadata-overlay')?.textContent).toBe('Oct 11, 2026\nDenver');

      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.seenKeys.has('https://example.com/portrait1.jpg')).toBe(true);
      expect(deck?.seenKeys.has('https://example.com/portrait2.jpg')).toBe(true);
      expect(deck?.seenKeys.has('https://example.com/landscape1.jpg')).toBe(true);
    } finally {
      window.Image = originalImage;
      window.ResizeObserver = originalResizeObserver;
    }
  });

  it('keeps outgoing and committed companion portrait images stable across transitions in albums with 5+ portraits', async () => {
    const decodeResolvers: Array<() => void> = [];
    const originalImage = window.Image;
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockImage {
      src = '';
      decode() { return new Promise<void>((res) => { decodeResolvers.push(res); }); }
    }
    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
      observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    try {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'p1', url: 'https://example.com/p1.jpg', aspect_ratio: 0.75 },
        { id: 'p2', url: 'https://example.com/p2.jpg', aspect_ratio: 0.75 },
        { id: 'p3', url: 'https://example.com/p3.jpg', aspect_ratio: 0.75 },
        { id: 'p4', url: 'https://example.com/p4.jpg', aspect_ratio: 0.75 },
        { id: 'p5', url: 'https://example.com/p5.jpg', aspect_ratio: 0.75 },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      act(() => { resizeCallback?.([{ contentRect: { width: 1400, height: 900 } }]); });

      let curImgs = container.querySelectorAll('.photo-layer.photo-current img.photo-diptych-item');
      expect(curImgs.length).toBe(2);
      expect(curImgs[0].getAttribute('src')).toBe('https://example.com/p1.jpg=w1400-h900');
      expect(curImgs[1].getAttribute('src')).toBe('https://example.com/p2.jpg=w1400-h900');

      act(() => { vi.advanceTimersByTime(10000); });
      act(() => { while (decodeResolvers.length > 0) decodeResolvers.shift()?.(); });

      await vi.waitFor(() => {
        const nextImgs = container.querySelectorAll('.photo-layer.photo-next img.photo-diptych-item');
        expect(nextImgs.length).toBe(2);
        expect(nextImgs[0].getAttribute('src')).toBe('https://example.com/p3.jpg=w1400-h900');
        expect(nextImgs[1].getAttribute('src')).toBe('https://example.com/p4.jpg=w1400-h900');
      });

      curImgs = container.querySelectorAll('.photo-layer.photo-current img.photo-diptych-item');
      expect(curImgs[0].getAttribute('src')).toBe('https://example.com/p1.jpg=w1400-h900');
      expect(curImgs[1].getAttribute('src')).toBe('https://example.com/p2.jpg=w1400-h900');

      act(() => { vi.advanceTimersByTime(850); });

      curImgs = container.querySelectorAll('.photo-layer.photo-current img.photo-diptych-item');
      expect(curImgs[0].getAttribute('src')).toBe('https://example.com/p3.jpg=w1400-h900');
      expect(curImgs[1].getAttribute('src')).toBe('https://example.com/p4.jpg=w1400-h900');
    } finally {
      window.Image = originalImage;
      window.ResizeObserver = originalResizeObserver;
    }
  });

  it('renders single portrait photo when container is narrow (< 1.15 aspect ratio) and deduplicates same-day metadata when paired', () => {
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
      observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
    }
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    try {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'p1', url: 'https://example.com/portrait1.jpg', width: 800, height: 1200, timestamp: '2026-10-10T09:00:00Z', location: 'Chicago' },
        { id: 'p2', url: 'https://example.com/portrait2.jpg', width: 900, height: 1200, timestamp: '2026-10-10T18:30:00Z', location: 'Chicago' },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      expect(container.querySelector('.photo-foreground-diptych')).not.toBeNull();
      expect(container.querySelector('.photo-metadata-overlay')?.textContent).toBe('Oct 10, 2026\nChicago');

      act(() => { resizeCallback?.([{ contentRect: { width: 600, height: 800 } }]); });
      expect(container.querySelector('.photo-foreground-diptych')).toBeNull();
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toBe('https://example.com/portrait1.jpg=w600-h800');
      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.companionKey).toBeNull();
      expect(deck?.seenKeys.has('https://example.com/portrait2.jpg')).toBe(false);
    } finally {
      window.ResizeObserver = originalResizeObserver;
    }
  });

  it('handles partial decode in triggerAdvance by falling back to single portrait when companion fails or keeping companion unseen when primary fails', async () => {
    const originalImage = window.Image;
    let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
    const originalResizeObserver = window.ResizeObserver;

    class MockImage {
      _src = '';
      set src(v: string) { this._src = v; }
      get src() { return this._src; }
      decode() {
        if (this._src.includes('p3_bad.jpg') || this._src.includes('p6_bad.jpg')) {
          return Promise.reject(new Error('decode failed'));
        }
        return Promise.resolve();
      }
    }
    class MockResizeObserver {
      constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
      observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;
    // @ts-expect-error mock window.ResizeObserver
    window.ResizeObserver = MockResizeObserver;

    try {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'p1', url: 'https://example.com/p1.jpg', aspect_ratio: 0.75 },
        { id: 'p2', url: 'https://example.com/p2.jpg', aspect_ratio: 0.75 },
        { id: 'p3', url: 'https://example.com/p3_bad.jpg', aspect_ratio: 0.75 },
        { id: 'p4', url: 'https://example.com/p4.jpg', aspect_ratio: 0.75 },
        { id: 'p5', url: 'https://example.com/p5.jpg', aspect_ratio: 0.75 },
        { id: 'p6', url: 'https://example.com/p6.jpg', aspect_ratio: 0.75 },
        { id: 'p7', url: 'https://example.com/p6_bad.jpg', aspect_ratio: 0.75 },
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>,
      );

      act(() => { resizeCallback?.([{ contentRect: { width: 1400, height: 900 } }]); });

      // Cycle 1: primary is p3_bad (fails), companion is p4 (succeeds).
      // Because primary failed, p3_bad is marked seen, but p4 must NOT be marked seen.
      await act(async () => { vi.advanceTimersByTime(10000); });
      const deck = photoDeckStateById.get(widgetId)!;
      expect(deck.seenKeys.has('https://example.com/p3_bad.jpg')).toBe(true);
      expect(deck.seenKeys.has('https://example.com/p4.jpg')).toBe(false);

      // Cycle 2 (retry): primary is p4 (unseen, succeeds), companion is p5 (succeeds).
      await act(async () => { vi.advanceTimersByTime(10000); });
      await vi.waitFor(() => {
        const nextImgs = container.querySelectorAll('.photo-layer.photo-next img.photo-diptych-item');
        expect(nextImgs.length).toBe(2);
        expect(nextImgs[0].getAttribute('src')).toBe('https://example.com/p4.jpg=w1400-h900');
        expect(nextImgs[1].getAttribute('src')).toBe('https://example.com/p5.jpg=w1400-h900');
      });
      act(() => { vi.advanceTimersByTime(850); });

      // Cycle 3: primary is p6 (succeeds), companion is p6_bad (fails).
      // Should mark p6_bad seen and transition to p6 as a single portrait (nextCompanionIndex = null).
      await act(async () => { vi.advanceTimersByTime(10000); });
      await vi.waitFor(() => {
        const nextSingle = container.querySelector('img.photo-layer.photo-next');
        expect(nextSingle?.getAttribute('src')).toBe('https://example.com/p6.jpg=w1400-h900');
      });
      expect(deck.seenKeys.has('https://example.com/p6_bad.jpg')).toBe(true);
      act(() => { vi.advanceTimersByTime(850); });
      expect(container.querySelector('.photo-foreground-diptych')).toBeNull();
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toBe('https://example.com/p6.jpg=w1400-h900');
      expect(deck.companionKey).toBeNull();
    } finally {
      window.Image = originalImage;
      window.ResizeObserver = originalResizeObserver;
    }
  });
  describe('inline video playback', () => {
    it('renders -no poster and play button, plays video inline on click, pauses rotation, and restores on ended/stop for replay', () => {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'v1', url: 'https://example.com/video1', media_type: 'video' },
        { id: 'p2', url: 'https://example.com/photo2', media_type: 'photo' },
      ], 10);
      const { container } = render(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toContain('-no');
      const playBtn = container.querySelector('button.photo-video-play-btn');
      expect(playBtn?.getAttribute('aria-label')).toBe('Play video');
      fireEvent.click(playBtn!);
      const videoEl = container.querySelector('video.photo-layer.photo-current.photo-video-player') as HTMLVideoElement;
      expect(videoEl.getAttribute('src')).toBe('https://example.com/video1=m22');
      expect(videoEl.getAttribute('poster')).toContain('-no');
      expect(container.querySelector('button.photo-video-play-btn')).toBeNull();
      act(() => { vi.advanceTimersByTime(15000); });
      expect(container.querySelector('video.photo-video-player')).not.toBeNull();
      fireEvent.ended(videoEl);
      expect(container.querySelector('video.photo-video-player')).toBeNull();
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toContain('-no');
      expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();
      act(() => { vi.advanceTimersByTime(9000); });
      expect(container.querySelector('img.photo-layer.photo-current')?.getAttribute('src')).toContain('video1');
      fireEvent.click(container.querySelector('button.photo-video-play-btn')!);
      const replayVideo = container.querySelector('video.photo-video-player') as HTMLVideoElement;
      expect(replayVideo).not.toBeNull();
      fireEvent.click(replayVideo);
      expect(container.querySelector('video.photo-video-player')).toBeNull();
      expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();
    });

    it('cycles through stream suffixes on video error', () => {
      photoDeckStateById.clear();
      setWidgetPhotos([{ id: 'v1', url: 'https://example.com/stream_test', media_type: 'video' }], 10);
      const { container } = render(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
      fireEvent.click(container.querySelector('button.photo-video-play-btn')!);
      let videoEl = container.querySelector('video.photo-video-player') as HTMLVideoElement;
      expect(videoEl.getAttribute('src')).toBe('https://example.com/stream_test=m22');
      fireEvent.error(videoEl);
      videoEl = container.querySelector('video.photo-video-player') as HTMLVideoElement;
      expect(videoEl.getAttribute('src')).toBe('https://example.com/stream_test=m18');
      fireEvent.error(videoEl);
      videoEl = container.querySelector('video.photo-video-player') as HTMLVideoElement;
      expect(videoEl.getAttribute('src')).toBe('https://example.com/stream_test=dv');
      fireEvent.error(videoEl);
      expect(container.querySelector('video.photo-video-player')).toBeNull();
      expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();
    });

    it('supports video playback in diptych slots', () => {
      let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
      const originalResizeObserver = window.ResizeObserver;
      class MockResizeObserver {
        constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
        observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
      }
      // @ts-expect-error mock window.ResizeObserver
      window.ResizeObserver = MockResizeObserver;
      try {
        photoDeckStateById.clear();
        setWidgetPhotos([
          { id: 'v1', url: 'https://example.com/v_portrait1', aspect_ratio: 0.7, media_type: 'video' },
          { id: 'v2', url: 'https://example.com/v_portrait2', aspect_ratio: 0.7, media_type: 'video' },
        ], 10);
        const { container } = render(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
        act(() => { resizeCallback?.([{ contentRect: { width: 1400, height: 900 } }]); });
        const diptychSlots = container.querySelectorAll('.photo-diptych-slot');
        expect(diptychSlots.length).toBe(2);
        const playBtns = container.querySelectorAll('.photo-diptych-slot .photo-video-play-btn');
        expect(playBtns.length).toBe(2);
        fireEvent.click(playBtns[1]);
        const videoInSlot2 = container.querySelector('.photo-diptych-slot video.photo-video-player');
        expect(videoInSlot2?.getAttribute('src')).toBe('https://example.com/v_portrait2=m22');
        expect(diptychSlots[0].querySelector('img.photo-diptych-item')).not.toBeNull();
        expect(diptychSlots[0].querySelector('.photo-video-play-btn')).not.toBeNull();
        fireEvent.ended(videoInSlot2!);
        expect(container.querySelector('.photo-diptych-slot video.photo-video-player')).toBeNull();
        expect(container.querySelectorAll('.photo-diptych-slot .photo-video-play-btn').length).toBe(2);
      } finally {
        window.ResizeObserver = originalResizeObserver;
      }
    });

    it('unmounts video player and restores play button when switching panes or entering video mode', () => {
      photoDeckStateById.clear();
      setWidgetPhotos([{ id: 'v1', url: 'https://example.com/v1', media_type: 'video' }], 10);
      const { container, rerender } = render(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
      fireEvent.click(container.querySelector('button.photo-video-play-btn')!);
      expect(container.querySelector('video.photo-video-player')).not.toBeNull();
      rerender(<PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
      expect(container.querySelector('video.photo-video-player')).toBeNull();
      expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();

      rerender(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
      fireEvent.click(container.querySelector('button.photo-video-play-btn')!);
      expect(container.querySelector('video.photo-video-player')).not.toBeNull();
      act(() => { useDisplayStore.setState({ video: { mode: 'video', primary: null, pip: null } }); });
      expect(container.querySelector('video.photo-video-player')).toBeNull();
      expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();
    });

    it('clears secondary video playback when container narrows', () => {
      let resizeCallback: ((entries: Array<{ contentRect: { width: number; height: number } }>) => void) | null = null;
      const originalResizeObserver = window.ResizeObserver;
      class MockResizeObserver {
        constructor(cb: (entries: Array<{ contentRect: { width: number; height: number } }>) => void) { resizeCallback = cb; }
        observe = vi.fn(); disconnect = vi.fn(); unobserve = vi.fn();
      }
      // @ts-expect-error mock window.ResizeObserver
      window.ResizeObserver = MockResizeObserver;
      try {
        photoDeckStateById.clear();
        setWidgetPhotos([
          { id: 'v1', url: 'https://example.com/v1', aspect_ratio: 0.7, media_type: 'video' },
          { id: 'v2', url: 'https://example.com/v2', aspect_ratio: 0.7, media_type: 'video' },
        ], 10);
        const { container } = render(<PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}><PhotoCarouselWidget id={widgetId} type="photo-carousel" /></PaneContext.Provider>);
        act(() => { resizeCallback?.([{ contentRect: { width: 1400, height: 900 } }]); });
        const playBtns = container.querySelectorAll('.photo-diptych-slot .photo-video-play-btn');
        fireEvent.click(playBtns[1]);
        expect(container.querySelector('.photo-diptych-slot video.photo-video-player')).not.toBeNull();
        act(() => { resizeCallback?.([{ contentRect: { width: 500, height: 800 } }]); });
        expect(container.querySelector('video.photo-video-player')).toBeNull();
        expect(container.querySelector('button.photo-video-play-btn')).not.toBeNull();
      } finally {
        window.ResizeObserver = originalResizeObserver;
      }
    });
  });
});
