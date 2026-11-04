import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, fireEvent, act } from '@testing-library/react';
import { PhotoCarouselWidget } from './PhotoCarouselWidget';
import type { PhotoAlbumConfig } from './photoCarouselHelpers';
import { useDisplayStore } from '../../store';
import type { WidgetMeta } from '../../store/types';
import { PaneContext } from '../../carousel/PaneContext';

function makeWidgetMeta(config: Record<string, unknown> = {}): WidgetMeta {
  return {
    type: 'photo-carousel',
    config,
    origin: [0, 0],
    dimensions: [2, 2],
    assets_base: '',
  };
}

describe('PhotoCarouselWidget ember_bridge sync and empty states', () => {
  const widgetId = 'w-photo-1';
  const initialStore = useDisplayStore.getState();
  const originalEmberBridge = window.EmberBridge;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    useDisplayStore.setState(initialStore, true);
    fetchMock = vi.fn().mockResolvedValue({ ok: true });
    window.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    cleanup();
    vi.clearAllTimers();
    vi.useRealTimers();
    window.EmberBridge = originalEmberBridge;
    delete window.onEmberPhotosSynced;
  });

  it('renders "No photos in shared album" for share_url source and no connect button', () => {
    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'share_url' }),
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
    expect(empty?.querySelector('.empty-title')?.textContent).toBe('No photos in shared album');
    expect(container.querySelector('.photo-connect-btn')).toBeNull();
  });

  it('renders "No photos available" for ember_bridge source when no errors or auth required', () => {
    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/test' }),
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
    expect(empty?.querySelector('.empty-title')?.textContent).toBe('No photos available');
  });

  it('renders connect button and triggers connectGooglePhotos when clicked', () => {
    const connectMock = vi.fn();
    window.EmberBridge = {
      connectGooglePhotos: connectMock,
    };

    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/album1' }),
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

    const btn = container.querySelector('.photo-connect-btn') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.textContent).toBe('Connect Google Photos');

    fireEvent.click(btn);
    expect(connectMock).toHaveBeenCalledWith('https://photos.app.goo.gl/album1');
  });

  it('triggers syncGooglePhotosAlbum on mount and every 5 minutes (300_000ms)', () => {
    const syncMock = vi.fn();
    window.EmberBridge = {
      syncGooglePhotosAlbum: syncMock,
    };

    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/album1' }),
          data: { photos: [{ url: 'https://example.com/1.jpg' }] },
          state: 'ready',
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { unmount } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    expect(syncMock).toHaveBeenCalledTimes(1);
    expect(syncMock).toHaveBeenCalledWith('https://photos.app.goo.gl/album1');

    act(() => {
      vi.advanceTimersByTime(300_000);
    });
    expect(syncMock).toHaveBeenCalledTimes(2);

    act(() => {
      vi.advanceTimersByTime(300_000);
    });
    expect(syncMock).toHaveBeenCalledTimes(3);

    unmount();
    act(() => {
      vi.advanceTimersByTime(300_000);
    });
    expect(syncMock).toHaveBeenCalledTimes(3);
  });

  it('handles window.onEmberPhotosSynced for needs_auth, sync error, and photo push POST', async () => {
    const syncMock = vi.fn();
    window.EmberBridge = {
      syncGooglePhotosAlbum: syncMock,
    };

    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/album1' }),
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

    expect(typeof window.onEmberPhotosSynced).toBe('function');

    // 1. needs_auth payload
    act(() => {
      window.onEmberPhotosSynced?.(JSON.stringify({ needs_auth: true }));
    });
    expect(container.querySelector('.empty-title')?.textContent).toBe('Google Photos sign-in required');

    // 2. ok: false payload
    act(() => {
      window.onEmberPhotosSynced?.({ ok: false, error: 'Network timeout' });
    });
    expect(container.querySelector('.empty-title')?.textContent).toBe('Photo sync failed');

    // 3. success payload with photos
    act(() => {
      window.onEmberPhotosSynced?.({
        ok: true,
        photos: ['https://lh3.googleusercontent.com/photo1', 'https://lh3.googleusercontent.com/photo2'],
      });
    });

    expect(fetchMock).toHaveBeenCalledWith(
      'api/widgets/w-photo-1/photos',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          photos: ['https://lh3.googleusercontent.com/photo1', 'https://lh3.googleusercontent.com/photo2'],
        }),
      }),
    );

    // Clears needsAuth and syncError
    expect(container.querySelector('.empty-title')?.textContent).toBe('No photos available');
  });

  it('unregisters window.onEmberPhotosSynced when the last listener unmounts', () => {
    window.EmberBridge = {
      syncGooglePhotosAlbum: vi.fn(),
    };

    useDisplayStore.setState({
      widgets: {
        [widgetId]: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/album1' }),
          data: { photos: [] },
          state: 'ready',
          timestamp: '2026-10-31T12:00:00Z',
        },
        ['w-photo-2']: {
          meta: makeWidgetMeta({ source: 'ember_bridge', album_url: 'https://photos.app.goo.gl/album2' }),
          data: { photos: [] },
          state: 'ready',
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { unmount: unmount1 } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const { unmount: unmount2 } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id="w-photo-2" type="photo-carousel" />
      </PaneContext.Provider>,
    );

    expect(typeof window.onEmberPhotosSynced).toBe('function');

    unmount1();
    // Still present because w-photo-2 is mounted
    expect(typeof window.onEmberPhotosSynced).toBe('function');

    unmount2();
    // Deleted because all listeners unmounted
    expect(window.onEmberPhotosSynced).toBeUndefined();
  });

  describe('multi-album source resolution', () => {
    it('resolves ember_bridge source and album url when configured via albums array with literal url', () => {
      const syncMock = vi.fn();
      window.EmberBridge = {
        syncGooglePhotosAlbum: syncMock,
      };

      const albums: PhotoAlbumConfig[] = [
        { name: 'Ember Album', source: 'ember_bridge', url: 'https://photos.app.goo.gl/literal-ember' },
      ];

      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({
              albums,
            }),
            data: { photos: [] },
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

      expect(syncMock).toHaveBeenCalledWith('https://photos.app.goo.gl/literal-ember');
    });

    it('resolves ember_bridge and falls back to data.album_url when url_env was used and url is empty in config', () => {
      const syncMock = vi.fn();
      window.EmberBridge = {
        syncGooglePhotosAlbum: syncMock,
      };

      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({
              albums: [
                { name: 'Env Album', source: 'ember_bridge' },
              ],
            }),
            data: {
              photos: [],
              album_url: 'https://photos.app.goo.gl/from-backend-data',
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

      expect(syncMock).toHaveBeenCalledWith('https://photos.app.goo.gl/from-backend-data');
    });

    it('resolves share_url source and renders empty title accordingly when albums array has share_url', () => {
      useDisplayStore.setState({
        widgets: {
          [widgetId]: {
            meta: makeWidgetMeta({
              albums: [
                { name: 'Shared 1', source: 'share_url', url: 'https://photos.app.goo.gl/shared1' },
              ],
            }),
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
      expect(empty?.querySelector('.empty-title')?.textContent).toBe('No photos in shared album');
      expect(container.querySelector('.photo-connect-btn')).toBeNull();
    });
  });
});
