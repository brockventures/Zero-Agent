import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, render, fireEvent, act } from '@testing-library/react';
import {
  PhotoCarouselWidget,
  PHOTO_CONTROLS_HIDE_MS,
  PHOTO_PAUSE_INACTIVITY_MS,
  photoDeckStateById,
  formatPhotoOverlayDate,
  formatPhotoMetadataLabel,
  hasMultiplePhotoAlbums,
} from './PhotoCarouselWidget';
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

function setWidgetPhotos(
  photos: Array<{ id?: string; url?: string; URL?: string; timestamp?: string; location?: string; album_name?: string; dominant_color?: string }>,
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

describe('PhotoCarouselWidget transport controls', () => {
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
  });

  it('pauses automatic cycling when photo-pause-pill is clicked and auto-resumes after PHOTO_PAUSE_INACTIVITY_MS', async () => {
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
      ], 10);

      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
        </PaneContext.Provider>,
      );

      const pausePill = container.querySelector('.photo-pause-pill') as HTMLButtonElement;
      expect(pausePill.getAttribute('data-paused')).toBe('false');
      expect(pausePill.getAttribute('aria-pressed')).toBe('false');
      expect(pausePill.getAttribute('aria-label')).toBe('Pause slideshow');

      act(() => { vi.advanceTimersByTime(4000); });
      expect(resolveDecode).toBeNull();

      act(() => { fireEvent.click(pausePill); });
      expect(pausePill.getAttribute('data-paused')).toBe('true');
      expect(pausePill.getAttribute('aria-pressed')).toBe('true');
      expect(pausePill.getAttribute('aria-label')).toBe('Resume slideshow');

      act(() => { vi.advanceTimersByTime(20000); });
      expect(resolveDecode).toBeNull();

      // Advance remaining time up to PHOTO_PAUSE_INACTIVITY_MS (60s total)
      act(() => { vi.advanceTimersByTime(PHOTO_PAUSE_INACTIVITY_MS - 20000); });
      expect(pausePill.getAttribute('data-paused')).toBe('false');
      expect(pausePill.getAttribute('aria-pressed')).toBe('false');
      expect(pausePill.getAttribute('aria-label')).toBe('Pause slideshow');

      // Remaining cycle time (6s) resumes
      act(() => { vi.advanceTimersByTime(5000); });
      expect(resolveDecode).toBeNull();

      act(() => { vi.advanceTimersByTime(1000); });
      expect(resolveDecode).not.toBeNull();
      act(() => {
        resolveDecode!();
      });
      await vi.waitFor(() => {
        expect(container.querySelector('.photo-layer.photo-next')?.getAttribute('src')).toBe('https://example.com/p2.jpg=w1024-h768');
      });
    } finally {
      window.Image = originalImage;
    }
  });

  it('makes transport controls visible on tap and auto-hides after PHOTO_CONTROLS_HIDE_MS when not paused', async () => {
    setWidgetPhotos([
      { id: '1', url: 'https://example.com/p1.jpg' },
      { id: '2', url: 'https://example.com/p2.jpg' },
    ], 60);

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const controls = container.querySelector('.photo-transport-controls') as HTMLDivElement;
    expect(controls.classList.contains('visible')).toBe(false);

    const root = container.querySelector('.widget-photo-carousel') as HTMLDivElement;
    act(() => { fireEvent.click(root); });
    expect(controls.classList.contains('visible')).toBe(true);

    act(() => { vi.advanceTimersByTime(2000); });
    expect(controls.classList.contains('visible')).toBe(true);

    act(() => { vi.advanceTimersByTime(PHOTO_CONTROLS_HIDE_MS - 2000); });
    expect(controls.classList.contains('visible')).toBe(false);

    const pausePill = container.querySelector('.photo-pause-pill') as HTMLButtonElement;
    act(() => { fireEvent.click(pausePill); });
    expect(controls.classList.contains('visible')).toBe(true);

    act(() => { vi.advanceTimersByTime(10000); });
    expect(controls.classList.contains('visible')).toBe(true);

    act(() => { fireEvent.click(pausePill); });
    expect(controls.classList.contains('visible')).toBe(true);

    act(() => { vi.advanceTimersByTime(PHOTO_CONTROLS_HIDE_MS); });
    expect(controls.classList.contains('visible')).toBe(false);
  });

  it('cleans up pause inactivity timer on unmount while paused', () => {
    setWidgetPhotos([
      { id: '1', url: 'https://example.com/p1.jpg' },
      { id: '2', url: 'https://example.com/p2.jpg' },
    ], 10);

    const { container, unmount } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const pausePill = container.querySelector('.photo-pause-pill') as HTMLButtonElement;
    act(() => { fireEvent.click(pausePill); });
    expect(pausePill.getAttribute('data-paused')).toBe('true');

    expect(() => unmount()).not.toThrow();
  });

  it('saves deck.remainingMs when entering video mode while cycling', () => {
    setWidgetPhotos([
      { id: '1', url: 'https://example.com/p1.jpg' },
      { id: '2', url: 'https://example.com/p2.jpg' },
    ], 20);

    render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    act(() => { vi.advanceTimersByTime(5000); });

    act(() => {
      useDisplayStore.setState({
        video: {
          mode: 'video',
          primary: { id: 'v1', stream_url: 'http://stream', type: 'mjpeg' },
          pip: null,
        },
      });
    });

    const deck = photoDeckStateById.get(widgetId);
    expect(deck?.remainingMs).toBeDefined();
    expect(deck?.remainingMs).toBeLessThanOrEqual(15000);
  });

  it('handles transport control button clicks: play/pause, prev, next, single photo guard, and PascalCase URL', () => {
    // 1. Single photo guard: tapping root returns early at photos.length <= 1
    setWidgetPhotos([
      { id: '1', url: 'https://example.com/p1.jpg' },
    ], 10);

    const { container: singleContainer } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id="single-photo-widget" type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const rootSingle = singleContainer.querySelector('.widget-photo-carousel') as HTMLDivElement;
    expect(singleContainer.querySelector('.photo-transport-controls')).toBeNull();
    act(() => { fireEvent.click(rootSingle); });

    // 2. Multiple photos with PascalCase URL and transport controls
    setWidgetPhotos([
      { id: '1', url: 'https://example.com/p1.jpg' },
      { id: '2', URL: 'https://example.com/p2.jpg' },
      { id: '3', url: 'https://example.com/p3.jpg' },
    ], 20);

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const root = container.querySelector('.widget-photo-carousel') as HTMLDivElement;
    act(() => { fireEvent.click(root); });

    const playPauseBtn = container.querySelector('.photo-pause-pill') as HTMLButtonElement;
    const prevBtn = container.querySelector('.photo-nav-prev') as HTMLButtonElement;
    const nextBtn = container.querySelector('.photo-nav-next') as HTMLButtonElement;
    expect(playPauseBtn.classList.contains('photo-nav-btn')).toBe(true);
    expect(playPauseBtn.querySelectorAll('svg rect').length).toBe(2);
    expect(playPauseBtn.textContent).toBe('');
    expect(prevBtn.querySelector('svg polygon')).not.toBeNull();
    expect(nextBtn.querySelector('svg polygon')).not.toBeNull();

    act(() => { fireEvent.click(playPauseBtn); });
    expect(playPauseBtn.getAttribute('aria-label')).toBe('Resume slideshow');
    expect(playPauseBtn.querySelector('svg polygon')).not.toBeNull();
    expect(playPauseBtn.textContent).toBe('');

    act(() => { fireEvent.click(nextBtn); });
    act(() => { fireEvent.click(prevBtn); });

    act(() => {
      useDisplayStore.setState({
        // @ts-expect-error test mode omission
        video: {
          primary: { id: 'v1', stream_url: 'http://stream', type: 'mjpeg' },
          pip: null,
        },
      });
    });

    act(() => {
      useDisplayStore.setState({
        video: { mode: 'widgets', primary: null, pip: null },
      });
    });
  });

  it('formats metadata labels, resolves adaptive overlay theme, and renders Nest Hub photo overlay across single and multi-album configs', () => {
    expect(formatPhotoOverlayDate('')).toBe('');
    expect(formatPhotoOverlayDate('   ')).toBe('');
    expect(formatPhotoOverlayDate('not-a-date')).toBe('');
    expect(formatPhotoOverlayDate('2026-10-30T14:30:00Z')).toBe('Oct 30, 2026');

    expect(formatPhotoMetadataLabel(undefined)).toBeNull();
    expect(formatPhotoMetadataLabel({ id: '1', url: 'https://example.com/1.jpg' })).toBeNull();
    expect(formatPhotoMetadataLabel({ location: '  Emeryville  ', timestamp: '2026-10-30T14:30:00Z', album_name: 'AZAZAZAZ' }, false)).toEqual({
      date: 'Oct 30, 2026',
      location: 'Emeryville',
      album: '',
      text: 'Oct 30, 2026\nEmeryville',
    });
    expect(formatPhotoMetadataLabel({ location: 'Emeryville', timestamp: '2026-10-30T14:30:00Z', album_name: 'AZAZAZAZ' }, true)).toEqual({
      date: 'Oct 30, 2026',
      location: 'Emeryville',
      album: 'AZAZAZAZ',
      text: 'Oct 30, 2026\nEmeryville • AZAZAZAZ',
    });
    expect(formatPhotoMetadataLabel({ location: 'Tokyo, Japan' })).toEqual({
      date: '',
      location: 'Tokyo, Japan',
      album: '',
      text: 'Tokyo, Japan',
    });
    expect(formatPhotoMetadataLabel({ album_name: 'Family Album' }, true)).toEqual({
      date: '',
      location: '',
      album: 'Family Album',
      text: 'Family Album',
    });

    expect(hasMultiplePhotoAlbums([])).toBe(false);
    expect(hasMultiplePhotoAlbums([{ album_name: 'Family' }, { album_name: 'Family' }])).toBe(false);
    expect(hasMultiplePhotoAlbums([{ album_name: 'Family' }, { album_name: 'Vacation' }])).toBe(true);

    // 1. Single album configured -> omits album name ("Oct 30, 2026\nEmeryville")
    photoDeckStateById.clear();
    setWidgetPhotos([
      {
        id: '1',
        url: 'https://example.com/dark.jpg',
        timestamp: '2026-10-30T14:30:00Z',
        location: 'Emeryville',
        album_name: 'Family Album',
        dominant_color: '#221c28',
      },
    ], 10);

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PhotoCarouselWidget id={widgetId} type="photo-carousel" />
      </PaneContext.Provider>,
    );

    const overlay1 = container.querySelector('.photo-metadata-overlay') as HTMLDivElement;
    expect(overlay1).not.toBeNull();
    expect(overlay1.getAttribute('aria-label')).toBe('Oct 30, 2026\nEmeryville');
    expect(overlay1.textContent).toBe('Oct 30, 2026\nEmeryville');

    // 2. Multiple albums configured in config.albums -> includes album name ("Oct 30, 2026\nEmeryville • AZAZAZAZ")
    act(() => {
      photoDeckStateById.clear();
      setWidgetPhotos(
        [
          {
            id: '2',
            url: 'https://example.com/light.jpg',
            timestamp: '2026-10-30T14:30:00Z',
            location: 'Emeryville',
            album_name: 'AZAZAZAZ',
            dominant_color: '#f2f0eb',
          },
        ],
        10,
        {
          albums: [
            { name: 'AZAZAZAZ', source: 'share_url', url: 'https://photos.app.goo.gl/a1' },
            { name: 'Trip 2026', source: 'share_url', url: 'https://photos.app.goo.gl/a2' },
          ],
        },
      );
    });

    const overlay2 = container.querySelector('.photo-metadata-overlay') as HTMLDivElement;
    expect(overlay2).not.toBeNull();
    expect(overlay2.getAttribute('aria-label')).toBe('Oct 30, 2026\nEmeryville • AZAZAZAZ');
    expect(overlay2.textContent).toBe('Oct 30, 2026\nEmeryville • AZAZAZAZ');

    // 3. Render photo with location only
    act(() => {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: 'loc-only', url: 'https://example.com/loc.jpg', location: 'Kyoto, Japan' },
      ], 10);
    });
    expect(container.querySelector('.photo-metadata-overlay')?.getAttribute('aria-label')).toBe('Kyoto, Japan');

    // 4. Render photo with no location or timestamp -> no overlay
    act(() => {
      photoDeckStateById.clear();
      setWidgetPhotos([
        { id: '3', url: 'https://example.com/plain.jpg' },
      ], 10);
    });
    expect(container.querySelector('.photo-metadata-overlay')).toBeNull();
  });
});
