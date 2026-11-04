import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import { Header } from './Header';

describe('Header component', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.className = '';
    delete document.body.dataset.timezone;
    delete document.body.dataset.headerVideo;

    useDisplayStore.setState({
      header: {
        timestamp: '2026-10-31T12:00:00Z',
        timezone: 'America/Los_Angeles',
        weather: {
          temperature: 65,
          units: '°F',
          icon: 'weather-partly-cloudy',
          condition: 'Partly Cloudy',
          location: 'Oakland',
        },
      },
      system: {
        connection: 'connected',
        online: true,
        serverTime: null,
        configStatus: 'ok',
        configError: null,
        providers: {},
        bootId: null,
      },
      widgets: {},
    });

    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(async (url: string) => {
        if (url === 'api/layout/editor') {
          return new Response(JSON.stringify({ allow_layout_edit: false }), { status: 200 });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('renders all canonical header zones and elements', () => {
    const { container } = render(<Header />);

    // Root zone
    const headerEl = container.querySelector('header.header-zone');
    expect(headerEl).not.toBeNull();

    // Time block with clock and date
    expect(container.querySelector('.header-time-block')).not.toBeNull();
    expect(container.querySelector('#header-clock')).not.toBeNull();
    expect(container.querySelector('#header-date-stack')).not.toBeNull();
    expect(container.querySelector('#header-date')).not.toBeNull();

    // NextUp event region
    expect(container.querySelector('#header-next-event')).not.toBeNull();
    expect(container.querySelector('#next-event-pill')).not.toBeNull();

    // Right cluster with weather, theme toggle, and status dot
    const rightCluster = container.querySelector('.header-right-cluster');
    expect(rightCluster).not.toBeNull();
    expect(container.querySelector('#header-weather')).not.toBeNull();
    expect(container.querySelector('#header-theme-toggle')).not.toBeNull();
    expect(container.querySelector('#header-status')).not.toBeNull();

    // Voice HUD region
    expect(container.querySelector('#header-voice')).not.toBeNull();
    expect(container.querySelector('#voice-indicator')).not.toBeNull();
    expect(container.querySelector('#header-voice-dismiss-btn')).not.toBeNull();
  });

  it('reflects status dot transitions from system store', () => {
    const { container } = render(<Header />);
    const statusContainer = container.querySelector('#header-status') as HTMLDivElement;
    expect(statusContainer.style.display).toBe('none');

    // Transition to offline
    act(() => {
      useDisplayStore.setState((s) => ({
        system: { ...s.system, online: false },
      }));
    });
    expect(statusContainer.style.display).toBe('');
    expect(container.querySelector('#system-status-dot')?.classList.contains('status-error')).toBe(
      true
    );

    // Transition to degraded
    act(() => {
      useDisplayStore.setState((s) => ({
        system: { ...s.system, online: true, configStatus: 'error', configError: 'Invalid YAML' },
      }));
    });
    expect(statusContainer.style.display).toBe('');
    expect(
      container.querySelector('#system-status-dot')?.classList.contains('status-degraded')
    ).toBe(true);
  });

  it('updates weather pill details when store weather changes', () => {
    const { container } = render(<Header />);
    expect(container.querySelector('#weather-temp')?.textContent).toBe('65°F');
    expect(container.querySelector('#weather-location')?.textContent).toBe('Oakland');
    expect(container.querySelector('#weather-desc')?.textContent).toBe('Partly Cloudy');

    act(() => {
      useDisplayStore.setState((s) => ({
        header: {
          ...s.header,
          weather: {
            temperature: 72,
            units: '°F',
            icon: 'weather-sunny',
            condition: 'Sunny',
            location: 'Berkeley',
          },
        },
      }));
    });

    expect(container.querySelector('#weather-temp')?.textContent).toBe('72°F');
    expect(container.querySelector('#weather-location')?.textContent).toBe('Berkeley');
  });

  it('applies cursor suppression from search query overrides', () => {
    vi.stubGlobal('location', { search: '?hide_cursor=true' });
    render(<Header />);
    expect(document.body.classList.contains('mm-touch-display')).toBe(true);
    expect(document.body.classList.contains('mm-touch-kiosk')).toBe(true);
  });

  it('syncs data-timezone to document.body and #mirrormere-app', () => {
    const appWrapper = document.createElement('div');
    appWrapper.id = 'mirrormere-app';
    document.body.appendChild(appWrapper);

    render(<Header timezone="America/New_York" />);
    expect(document.body.dataset.timezone).toBe('America/New_York');
    expect(appWrapper.dataset.timezone).toBe('America/New_York');

    document.body.removeChild(appWrapper);
  });

  it('does not overwrite document.body.dataset.timezone before initial SSE event', () => {
    document.body.dataset.timezone = 'America/Chicago';
    useDisplayStore.setState({
      header: {
        timestamp: null, // pre-SSE, timestamp is null
        timezone: 'UTC', // store default fallback
        weather: null,
      },
    });

    render(<Header />);
    // document.body.dataset.timezone must retain America/Chicago and NOT be overwritten with UTC!
    expect(document.body.dataset.timezone).toBe('America/Chicago');
  });

  it('updates HeaderVoice declaratively without affecting other header elements', () => {
    const { container } = render(<Header />);
    const headerVoice = container.querySelector('#header-voice')!;
    expect(headerVoice.classList.contains('visible')).toBe(false);

    act(() => {
      useDisplayStore.setState({
        voice: {
          state: 'listening',
          transcript: null,
          reply: null,
          tts_engine: null,
          status: null,
        },
      });
    });

    expect(headerVoice.classList.contains('visible')).toBe(true);
    expect(container.querySelector('#voice-indicator')?.classList.contains('listening')).toBe(true);
    expect(container.querySelector('#voice-label')?.textContent).toBe('Listening...');

    // Clock and weather elements remain intact
    expect(container.querySelector('#header-clock')).not.toBeNull();
    expect(container.querySelector('#header-weather')).not.toBeNull();
  });

  it('calls window.MirrormereVoice.resetHUD when dismiss button is clicked', () => {
    const resetSpy = vi.fn();
    (window as unknown as { MirrormereVoice?: { resetHUD: () => void } }).MirrormereVoice = {
      resetHUD: resetSpy,
    };

    useDisplayStore.setState({
      voice: {
        state: 'listening',
        transcript: null,
        reply: null,
        tts_engine: null,
        status: null,
      },
    });

    const { container } = render(<Header />);
    const dismissBtn = container.querySelector('#header-voice-dismiss-btn') as HTMLButtonElement;
    expect(dismissBtn).not.toBeNull();

    act(() => {
      fireEvent.click(dismissBtn);
    });

    expect(resetSpy).toHaveBeenCalledOnce();
    delete (window as unknown as { MirrormereVoice?: unknown }).MirrormereVoice;
  });

  it('falls back to store resetHUD when window.MirrormereVoice is absent', () => {
    delete (window as unknown as { MirrormereVoice?: unknown }).MirrormereVoice;
    const duckSpy = vi.fn();
    (window as unknown as { audioManager?: { setDucked: (ducked: boolean) => void } }).audioManager = {
      setDucked: duckSpy,
    };

    useDisplayStore.setState({
      voice: {
        state: 'speaking',
        transcript: 'Hello',
        reply: 'World',
        tts_engine: 'kokoro',
        status: null,
      },
    });

    const { container } = render(<Header />);
    const headerVoice = container.querySelector('#header-voice')!;
    expect(headerVoice.classList.contains('visible')).toBe(true);

    const dismissBtn = container.querySelector('#header-voice-dismiss-btn') as HTMLButtonElement;
    expect(dismissBtn).not.toBeNull();

    act(() => {
      fireEvent.click(dismissBtn);
    });

    // resetHUD marks local resetForVoice so headerVoice becomes inactive
    expect(headerVoice.classList.contains('visible')).toBe(false);
    expect(duckSpy).toHaveBeenCalledWith(false);

    delete (window as unknown as { audioManager?: unknown }).audioManager;
  });

  it('renders header layout button when allow_layout_edit is true and toggles layoutEditorOpen', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ allow_layout_edit: true }), { status: 200 }),
      ),
    );

    const { container } = render(<Header />);
    await act(async () => {
      await Promise.resolve();
    });

    const layoutBtn = container.querySelector('#header-layout-btn') as HTMLButtonElement;
    expect(layoutBtn).not.toBeNull();

    expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
    act(() => {
      fireEvent.click(layoutBtn);
    });
    expect(useDisplayStore.getState().layoutEditorOpen).toBe(true);
  });

  it('hides header layout button when allow_layout_edit is false', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ allow_layout_edit: false }), { status: 200 }),
      ),
    );

    const { container } = render(<Header />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector('#header-layout-btn')).toBeNull();
  });

  it('hides header layout button when fetch layout editor rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));

    const { container } = render(<Header />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector('#header-layout-btn')).toBeNull();
  });
});
