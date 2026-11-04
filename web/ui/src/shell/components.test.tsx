import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import { HeaderVideoButton } from './HeaderVideoButton';
import { StatusDot } from './StatusDot';
import { WeatherPill } from './WeatherPill';

describe('WeatherPill component', () => {
  beforeEach(() => {
    useDisplayStore.setState({
      header: { timestamp: null, timezone: 'UTC', weather: null },
    });
  });

  it('renders fallback markup when weather data is null', () => {
    const { container } = render(<WeatherPill />);
    expect(container.querySelector('#weather-temp')?.textContent).toBe('--°');
    expect(container.querySelector('#weather-location')?.textContent).toBe('Oakland');
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Sunny & Mild');
    expect(container.querySelector('#weather-condition svg')).not.toBeNull();
  });

  it('renders weather details from header store', () => {
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 68.4,
          units: '°F',
          icon: 'weather-sunny',
          condition: 'Sunny',
          location: 'Berkeley',
        },
      },
    });

    const { container } = render(<WeatherPill />);
    expect(container.querySelector('#weather-temp')?.textContent).toBe('68°F');
    expect(container.querySelector('#weather-location')?.textContent).toBe('Berkeley');
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Sunny & Mild');
    const svg = container.querySelector('.mm-icon-weather-sunny');
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute('width')).toBe('26');
    expect(svg?.getAttribute('height')).toBe('26');
    expect(svg?.querySelector('circle')).not.toBeNull();
  });

  it('renders night weather condition with declarative SVG icon', () => {
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 55,
          units: '°F',
          icon: 'weather-clear-night',
          condition: 'Clear Night',
          location: 'Oakland',
        },
      },
    });

    const { container } = render(<WeatherPill />);
    expect(container.querySelector('#weather-temp')?.textContent).toBe('55°F');
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Clear');
    const svg = container.querySelector('.mm-icon-weather-clear-night');
    expect(svg).not.toBeNull();
    expect(svg?.querySelector('path')).not.toBeNull();
  });

  it('covers weather descriptions and icon resolution variants', () => {
    // Custom description property takes precedence
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 60,
          description: 'Custom Breezy Fog',
          icon: 'weather-fog',
          condition: 'Foggy',
        } as unknown as { temperature: number; description: string; icon: string; condition: string },
      },
    });
    const { container, rerender } = render(<WeatherPill />);
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Custom Breezy Fog');
    expect(container.querySelector('.mm-icon-weather-fog')).not.toBeNull();

    // Weather condition mapped directly from WEATHER_DESC_MAP
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 40,
          condition: 'weather-snowy-rainy',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Rain & Snow');
    expect(container.querySelector('.mm-icon-weather-snowy-rainy')).not.toBeNull();

    // Unknown icon token with "night" substring falls back to weather-clear-night
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 50,
          icon: 'stormy-night-unknown',
          condition: 'Wild Storm At Night',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('.mm-icon-weather-clear-night')).not.toBeNull();

    // Unknown icon token without night falls back to weather-partly-cloudy and Clear Sky description
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 70,
          icon: 'unknown-alien-weather',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('.mm-icon-weather-partly-cloudy')).not.toBeNull();
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Clear Sky');

    // Weather condition mapped via WEATHER_DESC_MAP when icon is unknown
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 68,
          units: 'C',
          icon: 'custom-unmapped',
          condition: 'weather-rainy',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Light Rain');
    expect(container.querySelector('#weather-temp')?.textContent).toBe('68°C');

    // Weather condition is custom string different from conditionToken
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 62,
          icon: 'weather-cloudy',
          condition: 'Dense Overcast Pea Soup',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Overcast');

    // Condition token without mapping, but condition text contains "night" and equals conditionToken
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 48,
          condition: 'night-chill-unmapped',
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Clear');

    // Empty condition and icon
    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 65,
        },
      },
    });
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-condition')?.getAttribute('title')).toBeNull();

    // Rerender with unchanged state to hit React compiler memoization cache
    rerender(<WeatherPill />);
    expect(container.querySelector('#weather-temp')?.textContent).toBe('65°');
  });
});

describe('StatusDot component', () => {
  beforeEach(() => {
    useDisplayStore.setState({
      system: {
        connection: 'connected',
        online: true,
        serverTime: null,
        configStatus: 'ok',
        configError: null,
        providers: {},
        bootId: null,
      },
    });
  });

  it('hides jewel when system is healthy', () => {
    const { container } = render(<StatusDot />);
    const headerStatus = container.querySelector('#header-status') as HTMLDivElement;
    expect(headerStatus.style.display).toBe('none');

    const dot = container.querySelector('#system-status-dot');
    expect(dot?.classList.contains('status-ok')).toBe(true);
    expect(dot?.getAttribute('title')).toBe('System Status: Healthy');
  });

  it('displays degraded status when configStatus is error', () => {
    useDisplayStore.setState({
      system: {
        connection: 'connected',
        online: true,
        serverTime: null,
        configStatus: 'error',
        configError: 'Parse failed',
        providers: {},
        bootId: null,
      },
    });

    const { container } = render(<StatusDot />);
    const headerStatus = container.querySelector('#header-status') as HTMLDivElement;
    expect(headerStatus.style.display).toBe('');

    const dot = container.querySelector('#system-status-dot');
    expect(dot?.classList.contains('status-degraded')).toBe(true);
    expect(dot?.getAttribute('title')).toBe('Configuration Degraded: Parse failed');
  });

  it('displays error status when offline', () => {
    useDisplayStore.setState({
      system: {
        connection: 'disconnected',
        online: false,
        serverTime: null,
        configStatus: 'ok',
        configError: null,
        providers: {},
        bootId: null,
      },
    });

    const { container } = render(<StatusDot />);
    const headerStatus = container.querySelector('#header-status') as HTMLDivElement;
    expect(headerStatus.style.display).toBe('');

    const dot = container.querySelector('#system-status-dot');
    expect(dot?.classList.contains('status-error')).toBe(true);
    expect(dot?.getAttribute('title')).toBe('System Status: Offline');
  });
});

describe('HeaderVideoButton component', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.dataset.headerVideo = 'true';
    document.body.dataset.streamId = 'test-stream';
    document.body.dataset.streamUrl = 'https://stream.local/live';
    document.body.dataset.controlUrl = 'https://control.local/wake';
    document.body.dataset.type = 'webrtc';
  });

  afterEach(() => {
    vi.useRealTimers();
    delete document.body.dataset.headerVideo;
    delete document.body.dataset.streamId;
    delete document.body.dataset.streamUrl;
    delete document.body.dataset.controlUrl;
    delete document.body.dataset.type;
  });

  it('renders null when headerVideo dataset is not true', () => {
    delete document.body.dataset.headerVideo;
    const { container } = render(<HeaderVideoButton />);
    expect(container.firstChild).toBeNull();
  });

  it('triggers video wake and api/video/trigger on tap', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const { container } = render(<HeaderVideoButton fetchFn={fetchMock as unknown as typeof fetch} />);
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;
    expect(btn).not.toBeNull();

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(fetchMock).toHaveBeenCalledWith('https://control.local/wake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: 'test-stream', action: 'wake' }),
    });

    expect(fetchMock).toHaveBeenCalledWith('api/video/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'test-stream',
        stream_url: 'https://stream.local/live',
        type: 'webrtc',
        priority: 'persistent',
        controllable: true,
        control_url: 'https://control.local/wake',
      }),
    });

    expect(btn.classList.contains('loading')).toBe(true);

    // Switch video state to active video: loading state clears
    act(() => {
      useDisplayStore.setState({
        video: {
          mode: 'video',
          primary: {
            id: 'test-stream',
            stream_url: 'https://stream.local/live',
            type: 'webrtc',
            priority: 'persistent',
            controllable: true,
          },
          pip: null,
        },
      });
    });

    expect(btn.classList.contains('loading')).toBe(false);
  });

  it('applies error class when fetch rejects and clears after 1500ms', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('Network error'));

    const { container } = render(<HeaderVideoButton fetchFn={fetchMock as unknown as typeof fetch} />);
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(btn.classList.contains('error')).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1500);
    });

    expect(btn.classList.contains('error')).toBe(false);
  });

  it('debounces multiple clicks within 350ms and cancels when drag distance exceeds 15px', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const { container } = render(<HeaderVideoButton fetchFn={fetchMock as unknown as typeof fetch} />);
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;

    // First click triggers fetch
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2); // wake + trigger

    // Immediate second click within 350ms ignored
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // Advance 400ms past debounce window and clear loading
    act(() => {
      vi.advanceTimersByTime(4000); // clears loading via safety timer
    });
    expect(btn.classList.contains('loading')).toBe(false);

    // Drag gesture with movement >= 15px is canceled
    fireEvent.pointerDown(btn, { clientX: 10, clientY: 10 });
    await act(async () => {
      fireEvent.pointerUp(btn, { clientX: 30, clientY: 10 });
    });
    expect(fetchMock).toHaveBeenCalledTimes(2); // no new fetch calls
  });

  it('handles non-ok HTTP response by setting error state and clearing loading', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const { container } = render(<HeaderVideoButton fetchFn={fetchMock as unknown as typeof fetch} />);
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(btn.classList.contains('error')).toBe(true);
    expect(btn.classList.contains('loading')).toBe(false);
    expect(warnSpy).toHaveBeenCalledWith(
      '[MirrormereDisplay] Header video trigger failed:',
      expect.any(Error),
    );

    act(() => {
      vi.advanceTimersByTime(1500);
    });
    expect(btn.classList.contains('error')).toBe(false);
  });

  it('shows HUD instead of triggering fetch when video is already active', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    const showHUDSpy = vi.fn();

    (window as unknown as {
      videoManager?: { currentMode: string };
      videoHUD?: { showHUD: () => void };
    }).videoManager = { currentMode: 'video' };
    (window as unknown as {
      videoManager?: { currentMode: string };
      videoHUD?: { showHUD: () => void };
    }).videoHUD = { showHUD: showHUDSpy };

    const { container } = render(<HeaderVideoButton fetchFn={fetchMock as unknown as typeof fetch} />);
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;

    await act(async () => {
      fireEvent.click(btn);
    });

    expect(showHUDSpy).toHaveBeenCalledOnce();
    expect(fetchMock).not.toHaveBeenCalled();

    delete (window as unknown as { videoManager?: unknown }).videoManager;
    delete (window as unknown as { videoHUD?: unknown }).videoHUD;
  });

  it('exercises props fallback, wake rejection, and small drag triggering', async () => {
    delete document.body.dataset.headerVideo;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url === 'https://custom.local/wake') {
        return Promise.reject(new Error('Wake offline'));
      }
      return Promise.resolve({ ok: true, status: 200 });
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    // Direct props override dataset
    const { container, unmount } = render(
      <HeaderVideoButton
        enabled={true}
        streamId="prop-stream"
        streamUrl="https://stream.local/prop"
        controlUrl="https://custom.local/wake"
        type="hls"
        priority="transient"
        controllable={false}
        fetchFn={fetchMock as unknown as typeof fetch}
      />,
    );
    const btn = container.querySelector('#header-video-btn') as HTMLButtonElement;
    expect(btn.getAttribute('data-stream-id')).toBe('prop-stream');
    expect(btn.getAttribute('data-type')).toBe('hls');

    // Pointer gesture with small distance < 15px triggers video
    fireEvent.pointerDown(btn, { clientX: 10, clientY: 10 });
    await act(async () => {
      fireEvent.pointerUp(btn, { clientX: 14, clientY: 12 });
    });

    expect(fetchMock).toHaveBeenCalledWith('https://custom.local/wake', expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith('api/video/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'prop-stream',
        stream_url: 'https://stream.local/prop',
        type: 'hls',
        priority: 'transient',
        controllable: false,
        control_url: 'https://custom.local/wake',
      }),
    });
    expect(warnSpy).toHaveBeenCalledWith(
      '[MirrormereDisplay] Header video wake failed:',
      expect.any(Error),
    );

    // Unmount clears safety timer
    unmount();
  });

  it('handles trigger without controlUrl and respects explicit enabled=false', async () => {
    delete document.body.dataset.controlUrl;
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });

    const { container } = render(
      <HeaderVideoButton
        enabled={false}
        fetchFn={fetchMock as unknown as typeof fetch}
      />,
    );
    expect(container.firstChild).toBeNull();

    const { container: enabledContainer } = render(
      <HeaderVideoButton
        enabled={true}
        controlUrl=""
        fetchFn={fetchMock as unknown as typeof fetch}
      />,
    );
    const btn = enabledContainer.querySelector('#header-video-btn') as HTMLButtonElement;
    expect(btn).not.toBeNull();

    await act(async () => {
      fireEvent.click(btn);
    });

    // Only api/video/trigger called since controlUrl is empty
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('api/video/trigger', expect.any(Object));
  });
});
