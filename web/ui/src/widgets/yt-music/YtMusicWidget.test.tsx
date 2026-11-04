import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PaneContext } from '../../carousel/PaneContext';
import { useDisplayStore } from '../../store';
import {
  YtMusicNextIcon,
  YtMusicPauseIcon,
  YtMusicPlayIcon,
  YtMusicPrevIcon,
  YtMusicSpeakerIcon,
  YtMusicStopIcon,
} from './YtMusicIcons';
import { YtMusicNowPlaying } from './YtMusicNowPlaying';
import { YtMusicWidget, type MusicState } from './YtMusicWidget';
import {
  DEFAULT_ACCOUNTS,
  DEFAULT_ART,
  DEFAULT_STATE,
  DEFAULT_SUGGESTIONS,
  LOCAL_SPEAKER,
  postMusicApi,
  resolveAccounts,
  resolveSpeakersWithLocal,
  resolveSuggestions,
  type AccountProfile,
  type Playlist,
  type SearchResultItem,
  type YtMusicTab,
} from './ytMusicTypes';

const initialStore = useDisplayStore.getState();

const mockIdleState: MusicState = {
  active_speaker: 'media_player.living_room_speaker',
  active_account: 'primary',
  accounts: [
    { id: 'primary', name: 'Personal Profile', handle: '@personal', type: 'email' },
    { id: 'brand', name: 'Brand Channel', handle: '@brand', type: 'brand' },
  ],
  speakers: [
    { entity_id: 'this_device', name: 'This Device' },
    { entity_id: 'media_player.living_room_speaker', name: 'Living Room' },
    { entity_id: 'media_player.kitchen_speaker', name: 'Kitchen' },
  ],
  now_playing: {
    title: 'Starboy',
    artist: 'The Weeknd',
    thumb: 'https://example.com/starboy.jpg',
    state: 'paused',
    volume: 50,
    queue_index: 0,
    video_id: 'v_starboy',
  },
  playlists: [
    {
      playlist_id: 'PL_favorites',
      title: 'Daily Drive',
      thumb: 'https://example.com/daily.jpg',
      count: 24,
    },
  ],
  suggestions: [
    {
      id: 'sug-supermix',
      title: 'My Supermix',
      subtitle: 'Endless personalized mix',
      kind: 'album',
      playlist_id: 'PL_supermix',
      thumb: 'https://example.com/supermix.jpg',
    },
    {
      id: 'sug-artist-weeknd',
      title: 'The Weeknd',
      subtitle: 'Artist Radio',
      kind: 'artist',
      video_id: 'v_weeknd',
      thumb: 'https://example.com/weeknd.jpg',
    },
    {
      id: 'sug-generic',
      title: 'Lofi Chill',
      subtitle: 'Beats to study to',
      kind: 'artist',
      thumb: 'https://example.com/lofi.jpg',
    },
  ],
  queue: [
    {
      title: 'Starboy',
      artist: 'The Weeknd',
      thumb: 'https://example.com/starboy.jpg',
      video_id: 'v_starboy',
    },
    {
      title: 'Blinding Lights',
      artist: 'The Weeknd',
      thumb: 'https://example.com/blinding.jpg',
      video_id: 'v_blinding',
    },
  ],
};

const mockPlayingState: MusicState = {
  ...mockIdleState,
  now_playing: {
    ...mockIdleState.now_playing,
    state: 'playing',
  },
};

const mockSearchResults = {
  items: [
    {
      video_id: 'vid_123',
      title: 'Save Your Tears',
      artist: 'The Weeknd',
      thumb: 'https://example.com/tears.jpg',
      duration: '3:35',
    },
  ],
};

describe('YtMusicWidget', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    useDisplayStore.setState({
      visibility: { visible: true },
      widgets: {
        'yt-1': {
          meta: {
            type: 'yt-music',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {
              api_base: '/hearth-api',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-11-01T12:00:00Z',
        },
      },
    });

    fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const urlStr = String(url);

      if (urlStr.includes('/music/state')) {
        return new Response(JSON.stringify(mockIdleState), { status: 200 });
      }

      if (urlStr.includes('/music/search')) {
        return new Response(JSON.stringify(mockSearchResults), { status: 200 });
      }

      if (urlStr.includes('/music/control')) {
        let body: Record<string, unknown> = {};
        if (init?.body) {
          body = JSON.parse(String(init.body));
        }

        if (body.action === 'select_speaker') {
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                active_speaker: body.speaker,
              },
            }),
            { status: 200 }
          );
        }

        if (body.action === 'select_account') {
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                active_account: body.account_id,
              },
            }),
            { status: 200 }
          );
        }

        if (body.action === 'volume') {
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                now_playing: {
                  ...mockIdleState.now_playing,
                  volume: body.value,
                },
              },
            }),
            { status: 200 }
          );
        }

        if (body.action === 'play_pause') {
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                now_playing: {
                  ...mockIdleState.now_playing,
                  state: 'playing',
                },
              },
            }),
            { status: 200 }
          );
        }

        if (body.action === 'play_index') {
          const idx = Number(body.index ?? 0);
          const qItem = mockIdleState.queue?.[idx];
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                now_playing: {
                  title: qItem?.title ?? 'Track',
                  artist: qItem?.artist ?? 'Artist',
                  thumb: qItem?.thumb,
                  state: 'playing',
                  queue_index: idx,
                },
              },
            }),
            { status: 200 }
          );
        }

        if (body.action === 'stop') {
          return new Response(
            JSON.stringify({
              state: {
                ...mockIdleState,
                now_playing: {
                  ...mockIdleState.now_playing,
                  state: 'stopped',
                },
              },
            }),
            { status: 200 }
          );
        }

        return new Response(JSON.stringify({ state: mockIdleState }), { status: 200 });
      }

      if (urlStr.includes('/music/play')) {
        let body: Record<string, unknown> = {};
        if (init?.body) {
          body = JSON.parse(String(init.body));
        }
        return new Response(
          JSON.stringify({
            state: {
              ...mockIdleState,
              now_playing: {
                title: body.playlist_title || body.title || 'New Song',
                artist: body.artist || 'Artist',
                thumb: body.thumb || mockIdleState.now_playing?.thumb,
                state: 'playing',
              },
            },
          }),
          { status: 200 }
        );
      }

      return new Response('Not Found', { status: 404 });
    });

    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('hydrates music state on mount and renders account picker, speakers, mini player, and discovery', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    // Initial state fetch
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/hearth-api/music/state');
    });

    // Account picker
    await waitFor(() => {
      const picker = container.querySelector('#account-picker') as HTMLSelectElement;
      expect(picker).not.toBeNull();
      expect(picker.options.length).toBe(2);
      expect(picker.value).toBe('primary');
    });

    // Speakers rendered in speaker-bar
    const spkButtons = container.querySelectorAll('.spk-pill');
    expect(spkButtons.length).toBe(3);
    expect(spkButtons[0].textContent).toContain('This Device');
    expect(spkButtons[1].textContent).toContain('Living Room');
    expect(spkButtons[1].classList.contains('active')).toBe(true);

    // Speaker toggle button
    const spkToggle = container.querySelector('#speaker-toggle-btn');
    expect(spkToggle?.textContent).toContain('Play on This Device');

    // Mini player deck
    const npTitle = container.querySelector('#np-title');
    const npArtist = container.querySelector('#np-artist');
    const playBtn = container.querySelector('#btn-play');
    const volSlider = container.querySelector('#vol-slider') as HTMLInputElement;

    expect(npTitle?.textContent).toBe('Starboy');
    expect(npArtist?.textContent).toBe('The Weeknd');
    expect(playBtn?.getAttribute('aria-label')).toBe('Play');
    expect(volSlider?.value).toBe('50');

    // Discovery sections: playlists and suggestions
    const plSection = container.querySelector('[data-section="playlists"]');
    const sugSection = container.querySelector('[data-section="suggestions"]');
    expect(plSection).not.toBeNull();
    expect(sugSection).not.toBeNull();

    const plTiles = plSection?.querySelectorAll('.media-tile');
    expect(plTiles?.length).toBe(1);
    expect(plTiles?.[0].textContent).toContain('Daily Drive');

    const sugTiles = sugSection?.querySelectorAll('.media-tile');
    expect(sugTiles?.length).toBe(3);
    expect(sugTiles?.[0].textContent).toContain('My Supermix');
    expect(sugTiles?.[1].textContent).toContain('The Weeknd');
  });

  it('selects account and sends select_account control POST', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#account-picker')).not.toBeNull();
    });

    const picker = container.querySelector('#account-picker') as HTMLSelectElement;
    fireEvent.change(picker, { target: { value: 'brand' } });

    await waitFor(() => {
      const calls = fetchMock.mock.calls;
      const controlCall = calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'select_account' && b.account_id === 'brand';
      });
      expect(controlCall).toBeDefined();
    });
  });

  it('loads Ember accounts from EmberBridge and listens to onEmberAccountsUpdated', async () => {
    const mockAccounts = [
      { id: 'ember_1', name: 'Android Account', handle: '@android', type: 'email' },
    ];
    // Attach EmberBridge mock and pre-existing listener
    const prevSpy = vi.fn();
    const win = window as unknown as {
      EmberBridge?: { getGoogleAccounts: () => unknown };
      onEmberAccountsUpdated?: (payload?: unknown) => void;
    };
    win.onEmberAccountsUpdated = prevSpy;
    win.EmberBridge = {
      getGoogleAccounts: vi.fn(() => JSON.stringify(mockAccounts)),
    };

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      const picker = container.querySelector('#account-picker') as HTMLSelectElement;
      expect(picker).not.toBeNull();
      const options = Array.from(picker.options).map((o) => o.value);
      expect(options).toContain('ember_1');
    });

    // Fire window.onEmberAccountsUpdated with a new array
    act(() => {
      win.onEmberAccountsUpdated?.([
        { id: 'ember_2', name: 'Second Account', type: 'email' },
      ]);
    });
    expect(prevSpy).toHaveBeenCalled();

    await waitFor(() => {
      const picker = container.querySelector('#account-picker') as HTMLSelectElement;
      const options = Array.from(picker.options).map((o) => o.value);
      expect(options).toContain('ember_2');
    });

    // Fire window.onEmberAccountsUpdated with JSON string
    act(() => {
      win.onEmberAccountsUpdated?.(
        JSON.stringify([{ id: 'ember_3', name: 'Third Account', type: 'brand' }])
      );
    });

    await waitFor(() => {
      const picker = container.querySelector('#account-picker') as HTMLSelectElement;
      const options = Array.from(picker.options).map((o) => o.value);
      expect(options).toContain('ember_3');
    });

    // Fire with invalid JSON string when getGoogleAccounts returns direct array
    win.EmberBridge = {
      getGoogleAccounts: vi.fn(() => [{ id: 'ember_arr', name: 'Array Account', type: 'email' }]),
    };
    act(() => {
      win.onEmberAccountsUpdated?.('invalid-json');
    });

    // Fire when getGoogleAccounts returns non-array JSON string
    win.EmberBridge = {
      getGoogleAccounts: vi.fn(() => '{}'),
    };
    act(() => {
      win.onEmberAccountsUpdated?.();
    });

    delete win.EmberBridge;
    delete win.onEmberAccountsUpdated;
  });

  it('selects speaker and toggles speaker between This Device and remote speaker', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelectorAll('.spk-pill').length).toBe(3);
    });

    // 1. Click specific speaker pill (Kitchen)
    const kitchenSpk = container.querySelectorAll('.spk-pill')[2];
    fireEvent.click(kitchenSpk);

    await waitFor(() => {
      const controlCall = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'select_speaker' && b.speaker === 'media_player.kitchen_speaker';
      });
      expect(controlCall).toBeDefined();
    });

    // 2. Click toggle button (current is remote -> toggle to this_device)
    const spkToggle = container.querySelector('#speaker-toggle-btn')!;
    fireEvent.click(spkToggle);

    await waitFor(() => {
      const controlCall = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'select_speaker' && b.speaker === 'this_device';
      });
      expect(controlCall).toBeDefined();
    });
  });

  it('toggles speaker from This Device to first available remote speaker', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            active_speaker: 'this_device',
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ state: mockIdleState }), { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      const spkToggle = container.querySelector('#speaker-toggle-btn');
      expect(spkToggle?.textContent).toContain('Cast to Speakers');
    });

    const spkToggle = container.querySelector('#speaker-toggle-btn')!;
    fireEvent.click(spkToggle);

    await waitFor(() => {
      const controlCall = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'select_speaker' && b.speaker === 'media_player.living_room_speaker';
      });
      expect(controlCall).toBeDefined();
    });
  });

  it('handles toggle speaker when only local speaker exists', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            active_speaker: 'this_device',
            speakers: [{ entity_id: 'this_device', name: 'This Device' }],
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify({ state: mockIdleState }), { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#speaker-toggle-btn')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#speaker-toggle-btn')!);
  });

  it('handles transport buttons, volume slider, and stop action', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    // Previous
    fireEvent.click(container.querySelector('#btn-prev')!);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/control') &&
          JSON.parse(String(c[1]?.body)).action === 'prev'
      );
      expect(call).toBeDefined();
    });

    // Next
    fireEvent.click(container.querySelector('#btn-next')!);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/control') &&
          JSON.parse(String(c[1]?.body)).action === 'next'
      );
      expect(call).toBeDefined();
    });

    // Stop
    fireEvent.click(container.querySelector('#btn-stop')!);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/control') &&
          JSON.parse(String(c[1]?.body)).action === 'stop'
      );
      expect(call).toBeDefined();
    });

    // Volume change
    const volSlider = container.querySelector('#vol-slider') as HTMLInputElement;
    fireEvent.change(volSlider, { target: { value: '75' } });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/control') &&
          JSON.parse(String(c[1]?.body)).action === 'volume'
      );
      expect(call).toBeDefined();
      expect(JSON.parse(String(call![1]?.body)).value).toBe(75);
    });
  });

  it('clicking Play when paused starts playback and transitions to full Now Playing view', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    // Initially in Discovery view
    expect(container.querySelector('.ytm-discovery')).not.toBeNull();
    expect(container.querySelector('.ytm-now-playing-view')).toBeNull();

    // Click Play
    fireEvent.click(container.querySelector('#btn-play')!);

    // Should transition to full Now Playing view
    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).not.toBeNull();
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
      expect(container.querySelector('.np-queue-section')).not.toBeNull();
    });
  });

  it('handles idle play fallback to first queue item when no track loaded', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            now_playing: {},
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('/music/control')) {
        return new Response(
          JSON.stringify({
            state: {
              ...mockIdleState,
              now_playing: { title: 'Starboy', state: 'playing' },
            },
          }),
          { status: 200 }
        );
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    fireEvent.click(container.querySelector('#btn-play')!);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'play_index' && b.index === 0;
      });
      expect(call).toBeDefined();
    });
  });

  it('handles idle play fallback to first playlist when queue is empty', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            now_playing: {},
            queue: [],
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('/music/play')) {
        return new Response(
          JSON.stringify({
            state: {
              ...mockIdleState,
              now_playing: { title: 'Daily Drive', state: 'playing' },
            },
          }),
          { status: 200 }
        );
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    fireEvent.click(container.querySelector('#btn-play')!);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.playlist_id === 'PL_favorites';
      });
      expect(call).toBeDefined();
    });
  });

  it('handles idle play fallback to first suggestion when queue and playlists are empty', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            now_playing: {},
            queue: [],
            playlists: [],
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('/music/play')) {
        return new Response(
          JSON.stringify({
            state: {
              ...mockIdleState,
              now_playing: { title: 'My Supermix', state: 'playing' },
            },
          }),
          { status: 200 }
        );
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    fireEvent.click(container.querySelector('#btn-play')!);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.playlist_id === 'PL_supermix';
      });
      expect(call).toBeDefined();
    });
  });

  it('handles idle play fallback to default suggestion when suggestions, queue, and playlists are empty', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            ...mockIdleState,
            now_playing: {},
            queue: [],
            playlists: [],
            suggestions: [],
          }),
          { status: 200 }
        );
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#btn-play')).not.toBeNull();
    });

    fireEvent.click(container.querySelector('#btn-play')!);
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/play') &&
          JSON.parse(String(c[1]?.body)).playlist_id === DEFAULT_SUGGESTIONS[0].playlist_id
      );
      expect(call).toBeDefined();
    });
  });

  it('transitions to Now Playing on mount when music is playing, returns on Browse Library click', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(JSON.stringify(mockPlayingState), { status: 200 });
      }
      return new Response(JSON.stringify({ state: mockPlayingState }), { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    // Mounts directly in Now Playing view
    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).not.toBeNull();
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
      expect(container.querySelector('#np-device-badge')?.textContent).toContain('Living Room');
    });

    // Test transport controls and volume slider in full Now Playing view
    fireEvent.click(container.querySelector('#btn-prev')!);
    fireEvent.click(container.querySelector('#btn-next')!);
    fireEvent.change(container.querySelector('#vol-slider')!, { target: { value: '60' } });

    // Click Browse Library -> transitions to Discovery view
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).toBeNull();
      expect(container.querySelector('.ytm-discovery')).not.toBeNull();
      expect(container.querySelector('.player-deck')).not.toBeNull();
    });

    // Clicking mini player np-meta transitions back to Now Playing
    fireEvent.click(container.querySelector('.np-meta')!);

    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).not.toBeNull();
    });

    // Clicking Stop in Now Playing stops playback
    fireEvent.click(container.querySelector('#btn-stop')!);
  });

  it('plays playlist and suggestion tiles on click from Discovery view', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('.ytm-discovery')).not.toBeNull();
    });

    // 1. Click playlist tile
    const plTile = container.querySelector('[data-pl="PL_favorites"]')!;
    fireEvent.click(plTile);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.playlist_id === 'PL_favorites';
      });
      expect(call).toBeDefined();
    });

    // Return to library
    await waitFor(() => {
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    // 2. Click artist suggestion tile with video_id
    await waitFor(() => {
      expect(container.querySelector('[data-sug="sug-artist-weeknd"]')).not.toBeNull();
    });
    const sugArtistTile = container.querySelector('[data-sug="sug-artist-weeknd"]')!;
    fireEvent.click(sugArtistTile);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.video_id === 'v_weeknd';
      });
      expect(call).toBeDefined();
    });

    // Return to library
    await waitFor(() => {
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    // 3. Click generic suggestion tile without playlist_id or video_id
    await waitFor(() => {
      expect(container.querySelector('[data-sug="sug-generic"]')).not.toBeNull();
    });
    const sugGenericTile = container.querySelector('[data-sug="sug-generic"]')!;
    fireEvent.click(sugGenericTile);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.title === 'Lofi Chill' && b.artist === 'Beats to study to';
      });
      expect(call).toBeDefined();
    });
  });

  it('switches to Queue tab, triggers playback, and switches back to Playlists tab', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('.sub-toolbar')).not.toBeNull();
    });

    const queueTabBtn = container.querySelector('button[data-tab="queue"]')!;
    fireEvent.click(queueTabBtn);

    await waitFor(() => {
      const queueTiles = container.querySelectorAll('.media-tile');
      expect(queueTiles.length).toBe(2);
      expect(queueTiles[0].textContent).toContain('Starboy');
      expect(queueTiles[1].textContent).toContain('Blinding Lights');
    });

    // Click second item in queue
    const secondTile = container.querySelectorAll('.media-tile')[1];
    fireEvent.click(secondTile);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (c) =>
          String(c[0]).includes('/music/control') &&
          JSON.parse(String(c[1]?.body)).action === 'play_index'
      );
      expect(call).toBeDefined();
      const body = JSON.parse(String(call![1]?.body));
      expect(body.index).toBe(1);
    });

    // Return to discovery by clicking playlists tab button
    await waitFor(() => {
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    await waitFor(() => {
      expect(container.querySelector('button[data-tab="playlists"]')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('button[data-tab="playlists"]')!);
    expect(container.querySelector('.ytm-discovery')).not.toBeNull();
  });

  it('performs search and triggers playback on search result click', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#search-input')).not.toBeNull();
    });

    const searchInput = container.querySelector('#search-input') as HTMLInputElement;
    const searchForm = container.querySelector('#search-form') as HTMLFormElement;

    fireEvent.change(searchInput, { target: { value: 'Save Your Tears' } });
    fireEvent.submit(searchForm);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) =>
        String(c[0]).includes('/music/search?q=Save%20Your%20Tears')
      );
      expect(call).toBeDefined();
    });

    // Search results should render in grid
    await waitFor(() => {
      const tiles = container.querySelectorAll('.media-tile');
      expect(tiles.length).toBe(1);
      expect(tiles[0].textContent).toContain('Save Your Tears');
      expect(tiles[0].textContent).toContain('3:35');
    });

    // Click search result to play
    fireEvent.click(container.querySelector('.media-tile')!);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/play')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.video_id === 'vid_123';
      });
      expect(call).toBeDefined();
      const body = JSON.parse(String(call![1]?.body));
      expect(body.video_id).toBe('vid_123');
      expect(body.title).toBe('Save Your Tears');
    });
  });

  it('pauses polling when off-pane or hidden, and resumes when active', async () => {
    vi.useFakeTimers();

    // 1. Off-pane: isCurrentPane = false
    const { rerender } = render(
      <PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 1 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    expect(fetchMock).not.toHaveBeenCalled();

    // Advance 8s while off-pane: no fetch
    act(() => {
      vi.advanceTimersByTime(8000);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // 2. Active pane: fetch fires immediately
    rerender(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Advance 8s while active: interval re-fetches
    await act(async () => {
      vi.advanceTimersByTime(8000);
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);

    // 3. Document hidden: polling pauses
    act(() => {
      useDisplayStore.setState({ visibility: { visible: false } });
    });

    act(() => {
      vi.advanceTimersByTime(8000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // 4. Document visible again: polling resumes
    act(() => {
      useDisplayStore.setState({ visibility: { visible: true } });
    });

    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(3);

    vi.useRealTimers();
  });

  it('marks horizontal controls with data-prevent-screen-swipe so carousel drags ignore them', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#vol-slider')).not.toBeNull();
    });

    expect(container.querySelector('.vol-wrap')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('#vol-slider')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('#speaker-bar')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('#search-input')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
  });

  it('handles search failure and empty search query submission', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(JSON.stringify(mockIdleState), { status: 200 });
      }
      if (urlStr.includes('/music/search')) {
        throw new Error('search failed');
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('#search-input')).not.toBeNull();
    });

    // Empty search query submission does nothing
    fireEvent.submit(container.querySelector('#search-form')!);
    expect(fetchMock).not.toHaveBeenCalledWith(expect.stringContaining('/music/search'));

    // Search query throwing network error is caught safely
    fireEvent.change(container.querySelector('#search-input')!, { target: { value: 'error' } });
    fireEvent.submit(container.querySelector('#search-form')!);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/music/search?q=error'));
    });
  });

  it('activates media tiles on Enter and Space keydown', async () => {
    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('.media-tile')).not.toBeNull();
    });

    const playlistTile = container.querySelector('.media-tile') as HTMLElement;
    expect(playlistTile.getAttribute('role')).toBe('button');
    expect(playlistTile.getAttribute('tabindex')).toBe('0');

    // Trigger Enter
    fireEvent.keyDown(playlistTile, { key: 'Enter' });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('/music/play'));
      expect(call).toBeDefined();
    });

    // Return to library
    await waitFor(() => {
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    // Trigger Space on suggestion tile
    await waitFor(() => {
      expect(container.querySelectorAll('.media-tile').length).toBeGreaterThan(1);
    });
    const sugTile = container.querySelectorAll('.media-tile')[1] as HTMLElement;
    fireEvent.keyDown(sugTile, { key: ' ' });

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('/music/play'));
      expect(call).toBeDefined();
    });

    // Escape does not trigger action
    fireEvent.keyDown(sugTile, { key: 'Escape' });

    // Switch to queue
    await waitFor(() => {
      expect(container.querySelector('#btn-browse-library')).not.toBeNull();
    });
    fireEvent.click(container.querySelector('#btn-browse-library')!);

    fireEvent.click(container.querySelector('[data-tab="queue"]')!);
    await waitFor(() => {
      expect(container.querySelectorAll('.media-tile').length).toBe(2);
    });
    const queueTile = container.querySelectorAll('.media-tile')[0] as HTMLElement;
    expect(queueTile.getAttribute('role')).toBe('button');
    expect(queueTile.getAttribute('tabindex')).toBe('0');
    fireEvent.keyDown(queueTile, { key: 'Enter' });
    fireEvent.keyDown(queueTile, { key: ' ' });

    // Perform search
    if (container.querySelector('#btn-browse-library')) {
      fireEvent.click(container.querySelector('#btn-browse-library')!);
    }

    await waitFor(() => {
      expect(container.querySelector('#search-input')).not.toBeNull();
    });

    const searchInput = container.querySelector('#search-input') as HTMLInputElement;
    const searchForm = container.querySelector('#search-form') as HTMLFormElement;
    fireEvent.change(searchInput, { target: { value: 'Save Your Tears' } });
    fireEvent.submit(searchForm);
    await waitFor(() => {
      expect(container.querySelectorAll('.media-tile').length).toBe(1);
    });
    const searchTile = container.querySelectorAll('.media-tile')[0] as HTMLElement;
    expect(searchTile.getAttribute('role')).toBe('button');
    expect(searchTile.getAttribute('tabindex')).toBe('0');
    fireEvent.keyDown(searchTile, { key: 'Enter' });
    fireEvent.keyDown(searchTile, { key: ' ' });
    fireEvent.keyDown(searchTile, { key: 'Escape' });

    // Return to library and test mini-player meta keydown
    if (container.querySelector('#btn-browse-library')) {
      fireEvent.click(container.querySelector('#btn-browse-library')!);
    }

    await waitFor(() => {
      expect(container.querySelector('.np-meta')).not.toBeNull();
    });
    const npMeta = container.querySelector('.np-meta') as HTMLElement;
    fireEvent.keyDown(npMeta, { key: 'Enter' });

    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).not.toBeNull();
    });
  });

  it('handles queue row click and pause/stop in Now Playing view', async () => {
    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(JSON.stringify(mockPlayingState), { status: 200 });
      }
      if (urlStr.includes('/music/control')) {
        return new Response(JSON.stringify({ state: mockIdleState }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-1" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).not.toBeNull();
    });

    // Click on up next queue row
    const queueRows = container.querySelectorAll('.np-queue-row');
    expect(queueRows.length).toBe(2);
    fireEvent.click(queueRows[1]);

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => {
        if (!String(c[0]).includes('/music/control')) return false;
        const b = JSON.parse(String(c[1]?.body ?? '{}'));
        return b.action === 'play_index' && b.index === 1;
      });
      expect(call).toBeDefined();
    });

    // Clicking Pause in Now Playing -> pauses and transitions back to Discovery
    const playPauseBtn = container.querySelector('#btn-play')!;
    fireEvent.click(playPauseBtn);

    await waitFor(() => {
      expect(container.querySelector('.ytm-now-playing-view')).toBeNull();
      expect(container.querySelector('.ytm-discovery')).not.toBeNull();
    });
  });

  it('handles minimal empty state, missing api_base config, and unauthenticated requests', async () => {
    useDisplayStore.setState({
      visibility: { visible: true },
      widgets: {
        'yt-empty': {
          meta: {
            type: 'yt-music',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-11-01T12:00:00Z',
        },
      },
    });

    fetchMock.mockImplementation(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/music/state')) {
        return new Response(
          JSON.stringify({
            playlists: [{ playlist_id: 'pl_min', title: 'Minimal List' }],
            suggestions: [
              { id: 's_art', title: 'Minimal Artist', kind: 'artist', video_id: 'v_min' },
              { id: 's_alb', title: 'Minimal Album', kind: 'album' },
            ],
            queue: [{ title: 'Minimal Track', artist: 'Artist' }],
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('/music/search')) {
        return new Response(JSON.stringify({ items: [{ video_id: '', title: 'No Dur', artist: 'A' }] }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    const { container } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <YtMusicWidget id="yt-empty" type="yt-music" />
      </PaneContext.Provider>
    );

    await waitFor(() => {
      expect(container.querySelector('[data-sug="s_art"]')).not.toBeNull();
    });

    // Click artist suggestion without subtitle and with video_id
    fireEvent.click(container.querySelector('[data-sug="s_art"]')!);
    await waitFor(() => {
      expect(container.querySelector('[data-sug="s_alb"]')).not.toBeNull();
    });
    // Click album suggestion without subtitle and without video_id/playlist_id
    fireEvent.click(container.querySelector('[data-sug="s_alb"]')!);
    await waitFor(() => {
      expect(container.querySelector('button[data-tab="queue"]')).not.toBeNull();
    });

    // Switch to queue tab with minimal item
    fireEvent.click(container.querySelector('button[data-tab="queue"]')!);

    // Search with no activeAccountId and result without duration
    fireEvent.change(container.querySelector('#search-input')!, { target: { value: 'minimal' } });
    fireEvent.submit(container.querySelector('#search-form')!);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/hearth-api/music/search?q=minimal');
    });
  });
});

describe('YtMusicNowPlaying component coverage', () => {
  const baseNpProps = {
    nowPlaying: {
      title: 'Starboy',
      artist: 'The Weeknd',
      thumb: 'https://example.com/starboy.jpg',
      state: 'playing',
      volume: 50,
      queue_index: 0,
    },
    queue: [
      { title: 'Starboy', artist: 'The Weeknd', thumb: 'https://example.com/starboy.jpg' },
      { title: 'Blinding Lights', artist: 'The Weeknd', thumb: 'https://example.com/blinding.jpg' },
    ],
    activeSpeaker: 'media_player.living_room',
    speakers: [
      { entity_id: 'media_player.living_room', name: 'Living Room' },
      { entity_id: 'media_player.kitchen', name: 'Kitchen' },
    ],
    onPrev: vi.fn(),
    onPlayPause: vi.fn(),
    onNext: vi.fn(),
    onStop: vi.fn(),
    onVolumeChange: vi.fn(),
    onPlayQueueIndex: vi.fn(),
    onBrowseLibrary: vi.fn(),
  };

  it('renders metadata and handles keyboard navigation on queue rows', () => {
    const { container, rerender } = render(<YtMusicNowPlaying {...baseNpProps} />);
    rerender(<YtMusicNowPlaying {...baseNpProps} onPrev={vi.fn()} />);
    rerender(<YtMusicNowPlaying {...baseNpProps} nowPlaying={{ ...baseNpProps.nowPlaying }} />);

    expect(container.querySelector('#np-title')?.textContent).toBe('Starboy');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('Living Room');

    const secondRow = container.querySelectorAll('.np-queue-row')[1];
    fireEvent.click(secondRow);
    expect(baseNpProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: 'Enter' });
    expect(baseNpProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: ' ' });
    expect(baseNpProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: 'Escape' });
    expect(baseNpProps.onPlayQueueIndex).toHaveBeenCalledTimes(3);
  });

  it('renders empty queue, paused state, and fallback speaker name', () => {
    const { container, rerender } = render(
      <YtMusicNowPlaying
        onPrev={baseNpProps.onPrev}
        onPlayPause={baseNpProps.onPlayPause}
        onNext={baseNpProps.onNext}
        onStop={baseNpProps.onStop}
        onVolumeChange={baseNpProps.onVolumeChange}
        onBrowseLibrary={baseNpProps.onBrowseLibrary}
        startingPlayback={true}
        nowPlaying={{ state: 'paused' }}
      />
    );

    expect(container.querySelector('#np-title')?.textContent).toBe('Starting playback...');
    expect(container.querySelector('#btn-play')?.getAttribute('aria-label')).toBe('Play');

    rerender(
      <YtMusicNowPlaying
        {...baseNpProps}
        nowPlaying={undefined}
        queue={undefined}
        activeSpeaker={undefined}
        speakers={undefined}
      />
    );

    expect(container.querySelector('#np-title')?.textContent).toBe('YouTube Music');
    expect(container.querySelector('#np-artist')?.textContent).toBe('Pick a playlist or search a song');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('This Device');
    expect(container.querySelector('.np-queue-empty')?.textContent).toBe('Queue is empty');
  });

  it('renders single track count in queue and handles custom speaker string and missing callback', () => {
    const singleQueueProps = {
      ...baseNpProps,
      activeSpeaker: 'media_player.unknown_group',
      speakers: [],
      queue: [{ title: 'Solo', artist: 'Artist', id: 'solo-id' }],
      onPlayQueueIndex: undefined,
    };
    const { container } = render(<YtMusicNowPlaying {...singleQueueProps} />);

    expect(container.querySelector('.np-queue-count')?.textContent).toBe('1 track');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('media_player.unknown_group');

    const row = container.querySelector('.np-queue-row')!;
    fireEvent.click(row);
    fireEvent.keyDown(row, { key: 'Enter' });
  });
});

describe('ytMusicTypes helpers coverage', () => {
  it('supports Playlist, SearchResultItem, and YtMusicTab types', () => {
    const tab: YtMusicTab = 'discovery';
    const pl: Playlist = { playlist_id: 'p1', title: 'Favorites', count: 10 };
    const sr: SearchResultItem = { video_id: 'v1', title: 'Song', artist: 'Artist', duration: '3:00' };
    expect(tab).toBe('discovery');
    expect(pl.playlist_id).toBe('p1');
    expect(sr.video_id).toBe('v1');
  });

  describe('resolveAccounts', () => {
    it('returns stateData accounts when available', () => {
      const customAccounts: AccountProfile[] = [
        { id: 'user-1', name: 'Work Profile', handle: '@work', type: 'email' },
      ];
      expect(resolveAccounts({ accounts: customAccounts }, null)).toEqual(customAccounts);
    });

    it('resolves accounts from config when stateData accounts are missing', () => {
      const config = {
        accounts: [
          { id: 'cfg-1', name: 'Studio', handle: '@studio', type: 'brand' },
          { id: 'cfg-2', name: 'Personal', type: 'email' },
          { id: 'cfg-3' },
          {},
          'cfg-5-string',
        ],
      };
      const result = resolveAccounts(null, config);
      expect(result).toEqual([
        { id: 'cfg-1', name: 'Studio', handle: '@studio', type: 'brand' },
        { id: 'cfg-2', name: 'Personal', handle: undefined, type: 'email' },
        { id: 'cfg-3', name: 'cfg-3', handle: undefined, type: 'email' },
        { id: 'acc_3', name: 'Account 4', handle: undefined, type: 'email' },
        { id: 'cfg-5-string', name: 'cfg-5-string', type: 'email' },
      ]);
    });

    it('handles extraAccounts items including strings, objects with email, and invalid types', () => {
      const result = resolveAccounts(null, null, [
        'extra@example.com',
        'username_only',
        '   ',
        { id: 'ext-1', name: 'Extra 1', handle: '@ext1', type: 'brand' },
        { email: 'ext2@example.com' },
        {},
        123 as unknown as string,
      ]);
      expect(result.some((a) => a.id === 'extra@example.com')).toBe(true);
      expect(result.some((a) => a.id === 'username_only' && a.handle === undefined)).toBe(true);
      expect(result.some((a) => a.id === 'ext-1' && a.type === 'brand')).toBe(true);
      expect(result.some((a) => a.id === 'ext2@example.com')).toBe(true);
      expect(result.some((a) => a.id === 'extra_5')).toBe(true);
    });

    it('falls back to default profiles when neither stateData nor config provides accounts', () => {
      expect(resolveAccounts(null, null)).toEqual(DEFAULT_ACCOUNTS);
      expect(resolveAccounts({}, {})).toEqual(DEFAULT_ACCOUNTS);
    });
  });

  describe('resolveSpeakersWithLocal', () => {
    it('prepends This Device when not present in stateData.speakers', () => {
      const state: MusicState = {
        speakers: [{ entity_id: 'media_player.kitchen', name: 'Kitchen Speaker' }],
      };
      const result = resolveSpeakersWithLocal(state);
      expect(result).toHaveLength(2);
      expect(result[0]).toEqual(LOCAL_SPEAKER);
    });

    it('does not duplicate This Device if this_device or local is already present', () => {
      expect(resolveSpeakersWithLocal({ speakers: [{ entity_id: 'this_device', name: 'This Device' }] })).toHaveLength(1);
      expect(resolveSpeakersWithLocal({ speakers: [{ entity_id: 'local', name: 'Local Output' }] })).toHaveLength(1);
      expect(resolveSpeakersWithLocal({ speakers: [{ entity_id: 'media_player.custom', name: 'This Device' }] })).toHaveLength(1);
      expect(resolveSpeakersWithLocal(null)).toEqual([LOCAL_SPEAKER]);
    });
  });

  describe('resolveSuggestions', () => {
    it('returns stateData.suggestions when provided', () => {
      const custom = [{ id: 's1', title: 'Top Hits', kind: 'album' as const }];
      expect(resolveSuggestions({ suggestions: custom })).toEqual(custom);
    });

    it('derives suggestions from queue and playlists when suggestions are empty', () => {
      const state: MusicState = {
        queue: [
          { title: 'Song 1', artist: 'Daft Punk', thumb: 'https://example.com/dp.jpg' },
          { title: 'Song 2', artist: 'Daft Punk' },
          { title: 'Song 3', artist: 'Justice' },
          { title: 'Song 4', artist: 'Air' },
          { title: 'Song 5', artist: 'Phoenix' },
        ],
        playlists: [
          { playlist_id: 'pl-1', title: 'Discovery Mix', count: 25, thumb: 'https://example.com/pl.jpg' },
          { playlist_id: 'pl-2', title: 'Chill Mix' },
          { playlist_id: 'pl-3', title: 'Focus Mix' },
          { playlist_id: 'pl-4', title: 'Extra Mix' },
        ],
      };
      const result = resolveSuggestions(state);
      expect(result.length).toBe(6);
      expect(result[0].title).toBe('Daft Punk');
      expect(result[0].kind).toBe('artist');
      expect(result[3].title).toBe('Discovery Mix');
      expect(result[4].subtitle).toBe('Playlist');
    });

    it('returns default suggestions when state has no suggestions, queue, or playlists', () => {
      expect(resolveSuggestions(null)).toEqual(DEFAULT_SUGGESTIONS);
      expect(resolveSuggestions({})).toEqual(DEFAULT_SUGGESTIONS);
      expect(resolveSuggestions({ queue: [], playlists: [] })).toEqual(DEFAULT_SUGGESTIONS);
    });
  });

  describe('postMusicApi and constants', () => {
    it('returns state on successful POST and undefined on error', async () => {
      const originalFetch = globalThis.fetch;
      const okFetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ state: mockIdleState }), { status: 200 }));
      globalThis.fetch = okFetch;

      const res = await postMusicApi('/test-url', { action: 'test' });
      expect(res).toEqual(mockIdleState);

      const nonJsonFetch = vi.fn().mockResolvedValue(new Response('not-json', { status: 200 }));
      globalThis.fetch = nonJsonFetch;

      const nonJsonRes = await postMusicApi('/test-url', { action: 'test' });
      expect(nonJsonRes).toBeUndefined();

      const errFetch = vi.fn().mockRejectedValue(new Error('network error'));
      globalThis.fetch = errFetch;

      const errRes = await postMusicApi('/test-url', { action: 'test' });
      expect(errRes).toBeUndefined();

      globalThis.fetch = originalFetch;
    });

    it('defines constants correctly', () => {
      expect(DEFAULT_ART).toContain('ytube_music_player');
      expect(DEFAULT_STATE.active_account).toBe('primary');
    });
  });

  describe('YtMusicIcons direct rendering coverage', () => {
    it('renders all icons properly', () => {
      const { container, rerender } = render(
        <div>
          <YtMusicSpeakerIcon />
          <YtMusicPrevIcon />
          <YtMusicPlayIcon />
          <YtMusicPauseIcon />
          <YtMusicNextIcon />
          <YtMusicStopIcon />
        </div>
      );
      rerender(
        <div>
          <YtMusicSpeakerIcon />
          <YtMusicPrevIcon />
          <YtMusicPlayIcon />
          <YtMusicPauseIcon />
          <YtMusicNextIcon />
          <YtMusicStopIcon />
        </div>
      );
      expect(container.querySelectorAll('svg').length).toBe(6);
    });

    it('renders mini-player metadata with accessible aria-label', () => {
      const { container } = render(
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <YtMusicWidget id="yt-1" type="yt-music" />
        </PaneContext.Provider>
      );
      const meta = container.querySelector('.np-meta');
      expect(meta?.getAttribute('aria-label')).toBe('Open now playing');
    });
  });
});
