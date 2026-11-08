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
} from './PhotoCarouselWidget';
import { useDisplayStore } from '../../store';
import type { WidgetMeta } from '../../store/types';
import { PaneContext } from '../../carousel/PaneContext';
import { endAnimation } from '../../perf/animating';

function makeWidgetMeta(config: Record<string, unknown> = {}): WidgetMeta {
  return {
    type: 'photo-carousel',
    config,
    origin: [0, 0],
    dimensions: [2, 2],
    assets_base: '',
  };
}

function setWidgetPhotos(
  photos: Array<{ id?: string; url?: string; URL?: string }>,
  cycleSeconds?: number,
  config: Record<string, unknown> = {},
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
    const photos = [
      { id: '1', url: 'https://example.com/1.jpg' },
      { id: '2', url: 'https://example.com/2.jpg' },
      { id: '3', url: 'https://example.com/3.jpg' },
    ];

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
      const seen = new Set([
        'https://example.com/1.jpg',
        'https://example.com/2.jpg',
        'https://example.com/3.jpg',
      ]);
      const next = selectNextPhotoIndex(photos, 2, seen);
      expect(next).toBe(0);
      expect(seen.size).toBe(0);
    });
  });

  describe('selectPrevPhotoIndex', () => {
    const photos = [
      { id: '1', url: 'https://example.com/1.jpg' },
      { id: '2', url: 'https://example.com/2.jpg' },
      { id: '3', url: 'https://example.com/3.jpg' },
    ];

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
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    let loading = container.querySelector('.photo-loading');
    expect(loading).not.toBeNull();
    expect(loading?.textContent).toContain('Loading photos...');

    // Also renders loading state when state is healthy but photos array is not yet present
    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({}),
          data: {},
          state: 'healthy',
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    rerender(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    loading = container.querySelector('.photo-loading');
    expect(loading).not.toBeNull();
    expect(loading?.textContent).toContain('Loading photos...');
  });

  it('renders empty state when photos array is empty', () => {
    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({}),
          data: { photos: [] },
          state: 'ready',
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const empty = container.querySelector('.photo-empty');
    expect(empty).not.toBeNull();
    expect(empty?.querySelector('.empty-title')?.textContent).toBe('No photos in shared album');
    expect(empty?.querySelector('.empty-icon svg')).not.toBeNull();
  });

  it('renders photo viewport with backdrop and foreground for contain fit', () => {
    setWidgetPhotos(
      [{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }],
      10,
    );

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const root = container.querySelector('.widget-photo-carousel');
    expect(root).not.toBeNull();
    expect(root?.getAttribute('data-fit')).toBe('contain');
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

  it('always renders contain fit and blurred backdrop regardless of legacy config', () => {
    setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }], 10, { fit: 'cover', image_size: [1000, 800] });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const root = container.querySelector('.widget-photo-carousel');
    expect(root?.getAttribute('data-fit')).toBe('contain');

    const backdropContainer = container.querySelector('.photo-backdrop-container');
    expect(backdropContainer).not.toBeNull();

    const currentBackdrop = container.querySelector('.photo-backdrop-current');
    expect(currentBackdrop?.getAttribute('src')).toBe('https://example.com/p1.jpg=w96-h54-c');

    const currentFg = container.querySelector('.photo-layer.photo-current');
    expect(currentFg?.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
  });

  it('cycles photos automatically with decode preloading', async () => {
    let resolveDecode: (() => void) | null = null;
    const originalImage = window.Image;

    class MockImage {
      src = '';
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos(
        [{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }],
        10,
      );

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Fast-forward interval timer to trigger cycle advance callback
      act(() => {
        vi.advanceTimersByTime(10000);
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
      act(() => {
        vi.advanceTimersByTime(850);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 15);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel')!;
      // Click immediately
      act(() => {
        fireEvent.click(root);
      });

      // Resolve decode and let promise microtasks run
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      let nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      });

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;

      // Fast-forward crossfade
      act(() => {
        vi.advanceTimersByTime(850);
      });

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
      decode() {
        return new Promise<void>((resolve) => {
          decodeResolvers.push(resolve);
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const root = container.querySelector('.widget-photo-carousel')!;
      // First click: begins transition from p1 to p2
      act(() => {
        fireEvent.click(root);
      });

      expect(decodeResolvers.length).toBe(1);
      // Resolve decode for p2 so crossfade becomes active
      act(() => {
        decodeResolvers[0]();
      });

      let nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      });

      // While crossfade is active (before 850ms timeout), click again!
      // This should immediately commit p2 and initiate transition to p3.
      act(() => {
        fireEvent.click(root);
      });

      expect(decodeResolvers.length).toBe(2);
      // Resolve decode for p3
      act(() => {
        decodeResolvers[1]();
      });

      await vi.waitFor(() => {
        nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
      });

      // Fast-forward 850ms crossfade for p3
      act(() => {
        vi.advanceTimersByTime(850);
      });

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
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      expect(container.querySelector('.photo-loading')).not.toBeNull();

      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }], 10);
      rerender(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], undefined);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      // Should NOT advance at 15 seconds
      act(() => {
        vi.advanceTimersByTime(15000);
      });
      expect(resolveDecode).toBeNull();

      // Advancing to 30 seconds triggers the cycle timer
      act(() => {
        vi.advanceTimersByTime(15000);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 10);

      render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      expect(document.body.dataset.animating).toBeUndefined();

      act(() => {
        vi.advanceTimersByTime(10000);
      });

      expect(resolveDecode).not.toBeNull();
      expect(document.body.dataset.animating).toBeUndefined();

      act(() => {
        resolveDecode!();
      });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).toBe('1');
      });

      act(() => {
        vi.advanceTimersByTime(850);
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
      decode() {
        return new Promise<void>((resolve) => {
          decodeResolvers.push(resolve);
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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

      act(() => {
        vi.advanceTimersByTime(850);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
    }
    // @ts-expect-error mock window.Image
    window.Image = MockImage;

    try {
      setWidgetPhotos([{ url: 'https://example.com/p1.jpg' }, { url: 'https://example.com/p2.jpg' }], 10);

      const { unmount } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      act(() => {
        vi.advanceTimersByTime(10000);
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
      set src(v: string) {
        this._src = v;
      }
      get src() {
        return this._src;
      }
      decode() {
        decodeCalls.push(this._src);
        if (this._src.includes('p2.jpg')) {
          return Promise.reject(new Error('Corrupt image data'));
        }
        return Promise.resolve();
      }
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

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Trigger cycle 1: attempts to preload p2 (which rejects)
      await act(async () => {
        vi.advanceTimersByTime(10000);
      });

      expect(decodeCalls.some((url) => url.includes('p2.jpg'))).toBe(true);

      // Wait for decodePromise rejection microtask to be handled
      await vi.waitFor(() => {
        expect(document.body.dataset.animating).not.toBe('1');
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBeNull();
        expect(nextFg.style.opacity).toBe('0');
        expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
      });

      // Next interval cycle: timer re-armed via retryTick, now tries p3 (valid)
      await act(async () => {
        vi.advanceTimersByTime(10000);
      });

      expect(decodeCalls.some((url) => url.includes('p3.jpg'))).toBe(true);

      await vi.waitFor(() => {
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
        expect(document.body.dataset.animating).toBe('1');
      });

      act(() => {
        vi.advanceTimersByTime(850);
      });

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
      set src(v: string) {
        this._src = v;
      }
      get src() {
        return this._src;
      }
      constructor() {
        instances.push(this);
      }
    }
    // @ts-expect-error mock window.Image without decode
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

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Trigger cycle 1: preload p2
      act(() => {
        vi.advanceTimersByTime(10000);
      });

      const p2Img = instances.find((img) => img._src.includes('p2.jpg'));
      expect(p2Img).toBeDefined();

      // Trigger onerror for p2
      act(() => {
        p2Img!.onerror?.();
      });

      await vi.waitFor(() => {
        expect(document.body.dataset.animating).not.toBe('1');
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBeNull();
        expect(nextFg.style.opacity).toBe('0');
        expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');
      });

      // Next interval cycle: preload p3
      act(() => {
        vi.advanceTimersByTime(10000);
      });

      const p3Img = instances.find((img) => img._src.includes('p3.jpg'));
      expect(p3Img).toBeDefined();

      // Trigger onload for p3
      act(() => {
        p3Img!.onload?.();
      });

      await vi.waitFor(() => {
        const nextFg = container.querySelector('.photo-layer.photo-next') as HTMLImageElement;
        expect(nextFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');
        expect(document.body.dataset.animating).toBe('1');
      });

      act(() => {
        vi.advanceTimersByTime(850);
      });

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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      // (a) advance cycle timer and resolve Image.decode() so crossfade starts
      act(() => {
        vi.advanceTimersByTime(10000);
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
      act(() => {
        vi.advanceTimersByTime(30000);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Advance to p2
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Cycle 1 -> p2
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
      });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');

      // Cycle 2 -> p3
      resolveDecode = null;
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p3.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
      });
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p3.jpg=w1024-h768');

      // Cycle 3 -> all 3 seen, resets seenKeys and selects p1
      resolveDecode = null;
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p1.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      // Advance 4 seconds into 10s cycle
      act(() => {
        vi.advanceTimersByTime(4000);
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      // Advance 5 seconds - not yet triggered (needs 6s)
      act(() => {
        vi.advanceTimersByTime(5000);
      });
      expect(resolveDecode).toBeNull();

      // Advance 1 more second (completes remaining 6s)
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p2.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
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
      decode() {
        return new Promise<void>((resolve) => {
          resolveDecode = resolve;
        });
      }
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const currentFg = container.querySelector('.photo-layer.photo-current') as HTMLImageElement;
      expect(currentFg.getAttribute('src')).toBe('https://example.com/p1.jpg=w1024-h768');

      // Tick 1: attempts to advance, candidate 2 has empty URL -> marks seen, resets remainingMs, sets retryTick
      act(() => {
        vi.advanceTimersByTime(10000);
      });

      const deck = photoDeckStateById.get(widgetId);
      expect(deck?.seenKeys.has('2')).toBe(true);

      // Tick 2: advances to photo 3 (skipping broken photo 2)
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toContain('p3.jpg');
      });
      act(() => {
        vi.advanceTimersByTime(850);
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
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
});
