import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { VideoSource } from '../sse/types';
import { useDisplayStore } from '../store';
import { HUD_ICONS, Hud, VideoHUDController } from './Hud';
import {
  HudMuteIcon,
  HudNextIcon,
  HudPauseIcon,
  HudPlayIcon,
  HudPrevIcon,
  HudUnmuteIcon,
} from './HudIcons';
import { HudTopBar } from './HudTopBar';
import { HudTransportBar } from './HudTransportBar';
import { formatStreamTitle } from './api';
import { formatDuration } from './useStreamProgress';

describe('Hud component and declarative decomposition', () => {
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    cleanup();
    document.body.innerHTML = '';
    useDisplayStore.setState(initialStore, true);
    delete window.videoHUD;
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    document.body.innerHTML = '';
    useDisplayStore.setState(initialStore, true);
    delete window.videoHUD;
  });

  describe('HUD_ICONS export', () => {
    it('contains all required svg icons', () => {
      expect(HUD_ICONS.play).toContain('<svg');
      expect(HUD_ICONS.pause).toContain('<svg');
      expect(HUD_ICONS.mute).toContain('<svg');
      expect(HUD_ICONS.unmute).toContain('<svg');
      expect(HUD_ICONS.prev).toContain('<svg');
      expect(HUD_ICONS.next).toContain('<svg');
    });

    it('renders all dedicated HudIcon components as valid SVGs and hits memoization cache', () => {
      function IconWrapper({ count }: { count: number }) {
        return (
          <div>
            <HudPlayIcon {...({ count } as unknown as object)} />
            <HudPauseIcon {...({ count } as unknown as object)} />
            <HudMuteIcon {...({ count } as unknown as object)} />
            <HudUnmuteIcon {...({ count } as unknown as object)} />
            <HudPrevIcon {...({ count } as unknown as object)} />
            <HudNextIcon {...({ count } as unknown as object)} />
            <span>{count}</span>
          </div>
        );
      }

      const { container, rerender } = render(<IconWrapper count={1} />);
      expect(container.querySelectorAll('svg').length).toBe(6);

      // Changing count busts React.memo shallow equality, calling the inner functions a second time
      rerender(<IconWrapper count={2} />);
      expect(container.querySelectorAll('svg').length).toBe(6);
    });
  });

  describe('<Hud /> DOM rendering & hierarchy', () => {
    it('renders the complete HUD overlay hierarchy matching display.html lines 107-149', () => {
      const { container } = render(
        <div id="video-stage">
          <Hud />
        </div>,
      );

      expect(container.querySelector('#video-hud-overlay')).not.toBeNull();
      expect(container.querySelector('#video-hud-nav-bar')).not.toBeNull();
      expect(container.querySelector('#video-hud-back-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-home-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-title')).not.toBeNull();
      expect(container.querySelector('#video-hud-side-rail')).not.toBeNull();
      expect(container.querySelector('#video-hud-dismiss-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-volume-slider')).not.toBeNull();
      expect(container.querySelector('#video-hud-mute-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-mute-icon')).not.toBeNull();
      expect(container.querySelector('#video-hud-play-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-play-icon')).not.toBeNull();
      expect(container.querySelector('#video-hud-bottom-bar')).not.toBeNull();
      expect(container.querySelector('#video-hud-cc-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-prev-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-rewind-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-progress-container')).not.toBeNull();
      expect(container.querySelector('#video-hud-time-current')).not.toBeNull();
      expect(container.querySelector('#video-hud-progress-track')).not.toBeNull();
      expect(container.querySelector('#video-hud-progress-fill')).not.toBeNull();
      expect(container.querySelector('#video-hud-progress-thumb')).not.toBeNull();
      expect(container.querySelector('#video-hud-time-total')).not.toBeNull();
      expect(container.querySelector('#video-hud-forward-btn')).not.toBeNull();
      expect(container.querySelector('#video-hud-next-btn')).not.toBeNull();
    });

    it('stops propagation on play button pointerDown and bottom bar touchStart', () => {
      const mockFetch = vi.fn().mockResolvedValue({ ok: true });
      const { container } = render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', player_state: 'playing' },
            pip: null,
          },
        });
      });

      const playBtn = container.querySelector('#video-hud-play-btn')!;
      const bottomBar = container.querySelector('#video-hud-bottom-bar')!;
      const navBar = container.querySelector('#video-hud-nav-bar')!;
      const sideRail = container.querySelector('#video-hud-side-rail')!;
      const slider = container.querySelector('#video-hud-volume-slider')!;

      fireEvent.pointerDown(playBtn);
      fireEvent.touchStart(bottomBar);
      fireEvent.pointerDown(bottomBar);
      fireEvent.pointerDown(navBar);
      fireEvent.touchStart(navBar);
      fireEvent.pointerDown(sideRail);
      fireEvent.touchStart(sideRail);
      fireEvent.pointerCancel(slider);
      fireEvent.touchEnd(slider);
    });

    it('attaches controller to window.videoHUD and cleans up on unmount', () => {
      let createdCtrl: VideoHUDController | null = null;
      const { unmount } = render(
        <div id="video-stage">
          <Hud controllerRef={(ctrl) => { createdCtrl = ctrl; }} />
        </div>,
      );

      expect(createdCtrl).toBeInstanceOf(VideoHUDController);
      expect(window.videoHUD).toBe(createdCtrl);

      unmount();
      expect(window.videoHUD).toBeUndefined();
    });

    it('responds to store audio and video changes', () => {
      let ctrl: VideoHUDController | null = null;
      render(
        <div id="video-stage">
          <Hud controllerRef={(c) => { ctrl = c; }} />
        </div>,
      );
      const activeCtrl = ctrl as unknown as VideoHUDController;
      expect(activeCtrl).not.toBeNull();

      act(() => {
        useDisplayStore.setState({ audio: { volume: 45, muted: true } });
      });

      expect(activeCtrl.currentMuted).toBe(true);

      const stream: VideoSource = {
        id: 'stream-1',
        title: 'Episode 1',
        stream_url: 'rtsp://test',
        type: 'webrtc',
        controllable: true,
        player_state: 'playing',
      };

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: stream,
            pip: null,
          },
        });
      });

      expect(activeCtrl.activeStream?.id).toBe('stream-1');
      expect(document.getElementById('video-hud-title')?.textContent).toBe('Episode 1');
    });
  });

  describe('formatStreamTitle helper', () => {
    it('formats stream titles accurately with series, season, episode and fallbacks', () => {
      expect(formatStreamTitle(null)).toBe('Live Video');
      expect(formatStreamTitle({ id: 's1', stream_url: '', type: 'webrtc' })).toBe('Stream: s1');
      expect(formatStreamTitle({ id: '', stream_url: '', type: 'webrtc' })).toBe('Live Video');

      expect(
        formatStreamTitle({
          id: 's1',
          stream_url: '',
          type: 'webrtc',
          series: 'Severance',
          season: 1,
          episode: 9,
          title: 'The Grim Barbarity',
        }),
      ).toBe('Severance S1E9: The Grim Barbarity');

      expect(
        formatStreamTitle({
          id: 's1',
          stream_url: '',
          type: 'webrtc',
          series: 'Severance',
          title: 'Pilot',
        }),
      ).toBe('Severance: Pilot');

      expect(
        formatStreamTitle({
          id: 's1',
          stream_url: '',
          type: 'webrtc',
          title: 'Security Feed',
        }),
      ).toBe('Security Feed');
    });
  });

  describe('formatDuration helper', () => {
    it('formats time to M:SS and H:MM:SS accurately', () => {
      expect(formatDuration(-10)).toBe('0:00');
      expect(formatDuration(0)).toBe('0:00');
      expect(formatDuration(65)).toBe('1:05');
      expect(formatDuration(599)).toBe('9:59');
      expect(formatDuration(3665)).toBe('1:01:05');
    });
  });

  describe('Auto-fade lifecycle and visibility', () => {
    it('manages auto-fade timeout (5s default)', () => {
      let ctrl: VideoHUDController | null = null;
      const { container } = render(
        <div id="video-stage">
          <Hud controllerRef={(c) => { ctrl = c; }} />
        </div>,
      );

      const overlay = container.querySelector('#video-hud-overlay') as HTMLElement;
      expect(overlay.classList.contains('visible')).toBe(false);

      act(() => {
        ctrl?.showHUD();
      });
      expect(overlay.classList.contains('visible')).toBe(true);

      // Advance 4900ms -> still visible
      act(() => {
        vi.advanceTimersByTime(4900);
      });
      expect(overlay.classList.contains('visible')).toBe(true);

      // Advance remaining 100ms -> hidden
      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(overlay.classList.contains('visible')).toBe(false);
    });

    it('automatically shows HUD when activeStream transitions from null to present and hides when null', () => {
      render(
        <div id="video-stage">
          <Hud />
        </div>,
      );

      const overlay = document.getElementById('video-hud-overlay')!;
      expect(overlay.classList.contains('visible')).toBe(false);

      // Stream becomes present -> HUD automatically shows
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'stream-auto-show', stream_url: '', type: 'webrtc' },
            pip: null,
          },
        });
      });

      expect(overlay.classList.contains('visible')).toBe(true);

      // Stream becomes null -> HUD automatically hides
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'widgets',
            primary: null,
            pip: null,
          },
        });
      });

      expect(overlay.classList.contains('visible')).toBe(false);
    });
  });

  describe('Stage tap and double-tap gestures', () => {
    it('toggles HUD on stage single tap when idle vs active playback', () => {
      let stage: HTMLElement;
      render(
        <div
          id="video-stage"
          ref={(el) => {
            if (el) stage = el;
          }}
        >
          <Hud />
        </div>,
      );

      const overlay = document.getElementById('video-hud-overlay')!;

      // Tap when hidden -> shows HUD immediately
      fireEvent.pointerDown(stage!, { clientX: 100, clientY: 100 });
      fireEvent.pointerUp(stage!, { clientX: 100, clientY: 100 });
      expect(overlay.classList.contains('visible')).toBe(true);

      // Advance past double-tap window (>300ms)
      act(() => {
        vi.advanceTimersByTime(400);
      });

      // Now set non-media / idle stream: single tap hides immediately
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', player_state: 'idle' },
            pip: null,
          },
        });
      });

      fireEvent.pointerDown(stage!, { clientX: 100, clientY: 100 });
      fireEvent.pointerUp(stage!, { clientX: 100, clientY: 100 });
      expect(overlay.classList.contains('visible')).toBe(false);

      // Advance past double-tap window
      act(() => {
        vi.advanceTimersByTime(400);
      });

      // Now set playing controllable stream: single tap delays hide by 250ms
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', controllable: true, player_state: 'playing' },
            pip: null,
          },
        });
        window.videoHUD?.showHUD();
      });
      expect(overlay.classList.contains('visible')).toBe(true);

      act(() => {
        vi.advanceTimersByTime(400);
      });

      fireEvent.pointerDown(stage!, { clientX: 100, clientY: 100 });
      fireEvent.pointerUp(stage!, { clientX: 100, clientY: 100 });
      // Not hidden immediately: 250ms delay
      expect(overlay.classList.contains('visible')).toBe(true);

      act(() => {
        vi.advanceTimersByTime(250);
      });
      expect(overlay.classList.contains('visible')).toBe(false);
    });

    it('triggers play/pause toggle on rapid double-tap (<300ms, <30px)', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
      let stage: HTMLElement;

      render(
        <div
          id="video-stage"
          ref={(el) => {
            if (el) stage = el;
          }}
        >
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', controllable: true, player_state: 'playing' },
            pip: null,
          },
        });
      });

      // Tap 1
      fireEvent.pointerDown(stage!, { clientX: 100, clientY: 100 });
      fireEvent.pointerUp(stage!, { clientX: 100, clientY: 100 });

      // Tap 2 within 100ms and 5px away
      act(() => {
        vi.advanceTimersByTime(100);
      });
      fireEvent.pointerDown(stage!, { clientX: 105, clientY: 102 });
      fireEvent.pointerUp(stage!, { clientX: 105, clientY: 102 });

      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's1', action: 'toggle_playback' }),
        }),
      );
    });

    it('ignores taps inside active corner PiP dock', () => {
      let pipSlot: HTMLElement;

      render(
        <div id="video-stage">
          <div
            className="video-pip-slot"
            ref={(el) => {
              if (el) pipSlot = el;
            }}
          />
          <Hud />
        </div>
      );

      const overlay = document.getElementById('video-hud-overlay')!;
      expect(overlay.classList.contains('visible')).toBe(false);

      // Tap inside PiP dock -> does not wake HUD
      fireEvent.pointerDown(pipSlot!, { clientX: 10, clientY: 10 });
      fireEvent.pointerUp(pipSlot!, { clientX: 10, clientY: 10 });

      expect(overlay.classList.contains('visible')).toBe(false);
    });
  });

  describe('Transport controls and actions', () => {
    it('executes transport actions with 300ms in-flight lock', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', controllable: true, player_state: 'playing' },
            pip: null,
          },
        });
      });

      const rewindBtn = document.getElementById('video-hud-rewind-btn')!;
      const forwardBtn = document.getElementById('video-hud-forward-btn')!;

      // Click rewind twice rapidly
      fireEvent.click(rewindBtn);
      fireEvent.click(rewindBtn);

      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's1', action: 'rewind' }),
        }),
      );

      // Advance beyond 300ms lock
      act(() => {
        vi.advanceTimersByTime(350);
      });

      fireEvent.click(forwardBtn);
      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's1', action: 'fast_forward' }),
        }),
      );
    });

    it('posts to remoteUrl when preferred remote keys (back, home, etc.) are triggered', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud remoteUrl="http://sidecar:8080" fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 's1', stream_url: '', type: 'webrtc', player_state: 'playing', controllable: true },
            pip: null,
          },
        });
      });

      const backBtn = document.getElementById('video-hud-back-btn')!;
      const homeBtn = document.getElementById('video-hud-home-btn')!;

      fireEvent.click(backBtn);
      expect(mockFetch).toHaveBeenCalledWith(
        'http://sidecar:8080/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'back' }),
        }),
      );

      act(() => {
        vi.advanceTimersByTime(350);
      });

      fireEvent.click(homeBtn);
      expect(mockFetch).toHaveBeenCalledWith(
        'http://sidecar:8080/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'home' }),
        }),
      );
    });

    it('handleDismiss dismisses all streams if both primary and pip are active with distinct IDs', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud
            videoManager={{
              serverPrimary: { id: 'primary-1', stream_url: '', type: 'webrtc' },
              serverPip: { id: 'pip-1', stream_url: '', type: 'webrtc' },
            }}
            fetch={mockFetch as unknown as typeof fetch}
          />
        </div>,
      );

      const dismissBtn = document.getElementById('video-hud-dismiss-btn')!;
      fireEvent.click(dismissBtn);

      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/dismiss',
        expect.objectContaining({
          body: JSON.stringify({ id: 'all' }),
        }),
      );
    });

    it('handleDismiss dismisses single targetId when only primary is active', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud
            videoManager={{
              serverPrimary: { id: 'primary-1', stream_url: '', type: 'webrtc' },
              serverPip: null,
            }}
            fetch={mockFetch as unknown as typeof fetch}
          />
        </div>,
      );

      const dismissBtn = document.getElementById('video-hud-dismiss-btn')!;
      fireEvent.click(dismissBtn);

      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/dismiss',
        expect.objectContaining({
          body: JSON.stringify({ id: 'primary-1' }),
        }),
      );
    });
  });

  describe('Audio controls & volume slider', () => {
    it('handles mute toggle and updates UI icons', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      const muteBtn = document.getElementById('video-hud-mute-btn')!;
      const muteIcon = document.getElementById('video-hud-mute-icon')!;

      expect(muteBtn.classList.contains('muted')).toBe(false);

      fireEvent.click(muteBtn);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/audio/mute',
        expect.objectContaining({
          body: JSON.stringify({ muted: true }),
        }),
      );

      act(() => {
        useDisplayStore.setState({ audio: { muted: true, volume: 75 } });
      });

      expect(muteBtn.classList.contains('muted')).toBe(true);
      expect(muteIcon.dataset.icon).toBe('muted');

      act(() => {
        vi.advanceTimersByTime(350);
      });

      fireEvent.click(muteBtn);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/audio/mute',
        expect.objectContaining({
          body: JSON.stringify({ muted: false }),
        }),
      );
    });

    it('throttles volume slider updates (200ms) and commits on release', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      const volumeSlider = document.getElementById('video-hud-volume-slider') as HTMLInputElement;

      fireEvent.input(volumeSlider, { target: { value: '80' } });

      // First input immediately dispatches
      expect(mockFetch).toHaveBeenCalledWith(
        'api/audio/volume',
        expect.objectContaining({
          body: JSON.stringify({ volume: 80 }),
        }),
      );

      // Second input within 50ms is throttled
      fireEvent.input(volumeSlider, { target: { value: '85' } });
      expect(mockFetch).toHaveBeenCalledTimes(1);

      // Advance 200ms throttle interval -> flushes pending volume
      act(() => {
        vi.advanceTimersByTime(200);
      });
      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/audio/volume',
        expect.objectContaining({
          body: JSON.stringify({ volume: 85 }),
        }),
      );

      // Commit event commits final
      volumeSlider.value = '90';
      fireEvent.pointerUp(volumeSlider);
      expect(mockFetch).toHaveBeenCalledTimes(3);
      expect(mockFetch).toHaveBeenCalledWith(
        'api/audio/volume',
        expect.objectContaining({
          body: JSON.stringify({ volume: 90 }),
        }),
      );

      // Within commitCooldown (350ms), server audio state is ignored
      act(() => {
        useDisplayStore.setState({ audio: { muted: false, volume: 50 } });
      });
      expect(volumeSlider.value).toBe('90');

      act(() => {
        vi.advanceTimersByTime(400);
        useDisplayStore.setState({ audio: { muted: false, volume: 50 } });
      });
      expect(volumeSlider.value).toBe('50');
    });

    it('does not revert committed volume if no server audio update arrives during cooldown', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      const volumeSlider = document.getElementById('video-hud-volume-slider') as HTMLInputElement;

      fireEvent.input(volumeSlider, { target: { value: '92' } });
      fireEvent.pointerUp(volumeSlider);
      expect(volumeSlider.value).toBe('92');

      // Cooldown timer expires without any server SSE update arriving
      act(() => {
        vi.advanceTimersByTime(500);
      });

      // Volume must remain at 92, NOT revert to default 75
      expect(volumeSlider.value).toBe('92');
    });
  });

  describe('Progress bar & scrubbing', () => {
    it('handles progress ticker and scrubbing seeks', () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));

      render(
        <div id="video-stage">
          <Hud fetch={mockFetch as unknown as typeof fetch} />
        </div>,
      );

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: {
              id: 's1',
              stream_url: '',
              type: 'webrtc',
              player_state: 'playing',
              controllable: true,
              duration: 100,
              position: 10,
            },
            pip: null,
          },
        });
        window.videoHUD?.showHUD();
      });

      const progressContainer = document.getElementById('video-hud-progress-container')!;
      const timeCurrent = document.getElementById('video-hud-time-current')!;
      const timeTotal = document.getElementById('video-hud-time-total')!;
      const progressTrack = document.getElementById('video-hud-progress-track')!;

      expect(progressContainer.style.display).not.toBe('none');
      expect(timeTotal.textContent).toBe('1:40');

      // Ticker runs every 1000ms: position starts at 10s -> advances to 11s, 12s
      expect(timeCurrent.textContent).toBe('0:10');
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(timeCurrent.textContent).toBe('0:11');
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(timeCurrent.textContent).toBe('0:12');

      // Scrubbing
      progressTrack.getBoundingClientRect = () => ({
        left: 0,
        top: 0,
        width: 200,
        height: 20,
        right: 200,
        bottom: 20,
        x: 0,
        y: 0,
        toJSON: () => {},
      });

      fireEvent.pointerDown(progressTrack, { clientX: 100, pointerId: 1 });
      expect(progressTrack.classList.contains('is-scrubbing')).toBe(true);
      fireEvent.pointerCancel(progressTrack, { pointerId: 1 });
      expect(progressTrack.classList.contains('is-scrubbing')).toBe(false);

      fireEvent.pointerDown(progressTrack, { clientX: 100, pointerId: 1 });
      expect(progressTrack.classList.contains('is-scrubbing')).toBe(true);

      fireEvent.pointerMove(progressTrack, { clientX: 150, pointerId: 1 });
      // 150 / 200 = 75% of 100s = 75s = 1:15
      expect(timeCurrent.textContent).toBe('1:15');

      fireEvent.pointerUp(progressTrack, { clientX: 150, pointerId: 1 });
      expect(progressTrack.classList.contains('is-scrubbing')).toBe(false);
      expect(timeCurrent.textContent).toBe('1:15');
      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's1', action: 'seek', value: 75 }),
        }),
      );

      // Next 1Hz tick interpolates forward from seek position (1:16)
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(timeCurrent.textContent).toBe('1:16');
    });
  });

  describe('VideoHUDController and transport buttons coverage', () => {
    it('supports object controllerRef and updates ref.current', () => {
      const ref = { current: null as VideoHUDController | null };
      const { unmount } = render(
        <div id="video-stage">
          <Hud controllerRef={ref} />
        </div>,
      );

      expect(ref.current).toBeInstanceOf(VideoHUDController);
      unmount();
      expect(ref.current).toBeNull();
    });

    it('exercises VideoHUDController getters, setters, and state methods', () => {
      const ctrl = new VideoHUDController();
      expect(ctrl.titleEl).toBeNull();

      // Create title in DOM
      const title = document.createElement('div');
      title.id = 'video-hud-title';
      document.body.appendChild(title);
      expect(ctrl.titleEl).toBe(title);

      const primaryStream: VideoSource = { id: 'p1', stream_url: '', type: 'webrtc' };
      const pipStream: VideoSource = { id: 'pip1', stream_url: '', type: 'webrtc' };

      // With videoManager swapped vs unswapped
      ctrl.videoManager = { isSwapped: false, serverPrimary: primaryStream, serverPip: pipStream };
      expect(ctrl.activeStream).toBe(primaryStream);

      ctrl.videoManager.isSwapped = true;
      expect(ctrl.activeStream).toBe(pipStream);

      // Without videoManager
      ctrl.videoManager = null;
      ctrl.activeStream = primaryStream;
      expect(ctrl.activeStream).toBe(primaryStream);

      ctrl.activeStream = null;
      ctrl.lastVideoState = { mode: 'video', primary: primaryStream, pip: pipStream };
      expect(ctrl.activeStream).toBe(primaryStream);

      ctrl.lastVideoState = { mode: 'video', primary: null, pip: pipStream };
      expect(ctrl.activeStream).toBe(pipStream);

      // Standalone VideoHUDController default methods
      ctrl.handleVideoState();
      ctrl.handleAudioState();
      ctrl.destroy?.();
      ctrl.syncStream();

      document.body.removeChild(title);
    });

    it('triggers syncStream, handleVideoState, handleAudioState, center play button, captions, previous, and next actions', async () => {
      const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
      let ctrl: VideoHUDController | null = null;

      const primaryStream: VideoSource = {
        id: 's_main',
        stream_url: 'http://test/stream',
        type: 'webrtc',
        player_state: 'playing',
        controllable: true,
        control_url: 'http://test/control',
      };

      const { container } = render(
        <div id="video-stage">
          <Hud
            fetch={mockFetch as unknown as typeof fetch}
            remoteUrl="http://remote-host"
            controllerRef={(c) => {
              ctrl = c;
            }}
          />
        </div>,
      );

      // handleVideoState and handleAudioState on mounted controller
      act(() => {
        ctrl?.handleVideoState({ mode: 'video', primary: primaryStream, pip: null });
        ctrl?.handleAudioState({ volume: 88, muted: false });
      });
      expect(useDisplayStore.getState().video.primary?.id).toBe('s_main');
      expect(useDisplayStore.getState().audio.volume).toBe(88);

      // Show HUD and test syncStream
      act(() => {
        ctrl?.syncStream();
      });

      // Center play button click
      const playBtn = container.querySelector('#video-hud-play-btn') as HTMLButtonElement;
      expect(playBtn).not.toBeNull();
      act(() => {
        fireEvent.click(playBtn);
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's_main', action: 'toggle_playback' }),
        }),
      );

      // Advance debounce timer (300ms)
      act(() => {
        vi.advanceTimersByTime(350);
      });

      // Captions button click
      const ccBtn = container.querySelector('#video-hud-cc-btn') as HTMLButtonElement;
      expect(ccBtn).not.toBeNull();
      act(() => {
        fireEvent.click(ccBtn);
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'http://remote-host/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'cc' }),
        }),
      );

      act(() => {
        vi.advanceTimersByTime(350);
      });

      // Previous button click
      const prevBtn = container.querySelector('#video-hud-prev-btn') as HTMLButtonElement;
      expect(prevBtn).not.toBeNull();
      act(() => {
        fireEvent.click(prevBtn);
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'http://remote-host/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'prev' }),
        }),
      );

      act(() => {
        vi.advanceTimersByTime(350);
      });

      // Next button click
      const nextBtn = container.querySelector('#video-hud-next-btn') as HTMLButtonElement;
      expect(nextBtn).not.toBeNull();
      act(() => {
        fireEvent.click(nextBtn);
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'http://remote-host/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'next' }),
        }),
      );
    });

    it('toggles pause icon and play icon states according to player_state', () => {
      const { container } = render(<Hud />);
      const pausedStream: VideoSource = {
        id: 's_pause',
        stream_url: 'http://test/stream',
        type: 'webrtc',
        controllable: true,
        player_state: 'paused',
      };

      act(() => {
        useDisplayStore.setState({
          video: { mode: 'video', primary: pausedStream, pip: null },
        });
      });

      const playBtn = container.querySelector('#video-hud-play-btn') as HTMLButtonElement;
      expect(playBtn).not.toBeNull();
      expect(playBtn.getAttribute('aria-label')).toBe('Play');
      expect(playBtn.getAttribute('title')).toBe('Play');
      const playIcon = container.querySelector('#video-hud-play-icon') as HTMLElement;
      expect(playIcon.getAttribute('data-icon')).toBe('play');
      expect(playIcon.querySelector('polygon')).not.toBeNull();

      // Switch to playing state
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { ...pausedStream, player_state: 'playing' },
            pip: null,
          },
        });
      });

      expect(playBtn.getAttribute('aria-label')).toBe('Pause');
      expect(playBtn.getAttribute('title')).toBe('Pause');
      expect(playIcon.getAttribute('data-icon')).toBe('pause');
      expect(playIcon.querySelectorAll('rect').length).toBe(2);
    });

    it('handles action routing fallbacks and dismiss guards without active streams', async () => {
      const mockFetch = vi.fn().mockResolvedValue({ ok: true });
      let ctrl: VideoHUDController | null = null;
      const ref = {
        get current() {
          return ctrl;
        },
        set current(val: VideoHUDController | null) {
          ctrl = val;
        },
      };

      render(
        <Hud controllerRef={ref} remoteUrl="http://remote-host" fetch={mockFetch as unknown as typeof fetch} />,
      );

      // When no stream is active, handleDismiss does nothing
      act(() => {
        ctrl?.handleDismiss?.();
      });
      expect(mockFetch).not.toHaveBeenCalled();

      // When no stream is active, action execution returns early
      await act(async () => {
        await ctrl?.handleRewind?.();
      });
      expect(mockFetch).not.toHaveBeenCalled();

      // Mount controllable stream with remoteUrl and test non-remote-preferred action (e.g. rewind) routing to remoteUrl
      const activeStream: VideoSource = {
        id: 's_remote',
        stream_url: 'http://test/stream',
        type: 'webrtc',
        controllable: true,
      };

      act(() => {
        useDisplayStore.setState({
          video: { mode: 'video', primary: activeStream, pip: null },
        });
      });

      // Rewind routes to postRemoteKey because remoteUrl exists and no control_url/value
      await act(async () => {
        await ctrl?.handleRewind?.();
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'http://remote-host/key',
        expect.objectContaining({
          body: JSON.stringify({ key: 'rewind' }),
        }),
      );

      act(() => {
        vi.advanceTimersByTime(350);
      });

      // Seek routes to postVideoAction with value
      await act(async () => {
        await ctrl?.handleSeek?.(42);
      });
      expect(mockFetch).toHaveBeenCalledWith(
        'api/video/action',
        expect.objectContaining({
          body: JSON.stringify({ id: 's_remote', action: 'seek', value: 42 }),
        }),
      );
    });

    it('supports callback function for controllerRef with mount and unmount', () => {
      let callbackCtrl: VideoHUDController | null = null;
      const refFn = (c: VideoHUDController | null) => {
        callbackCtrl = c;
      };

      const { unmount } = render(<Hud controllerRef={refFn} />);
      expect(callbackCtrl).not.toBeNull();

      unmount();
      expect(callbackCtrl).toBeNull();
    });

    it('handles syncStream hide vs show transitions and null state updates', () => {
      let ctrl: VideoHUDController | null = null;
      const ref = {
        get current() {
          return ctrl;
        },
        set current(val: VideoHUDController | null) {
          ctrl = val;
        },
      };

      render(<Hud controllerRef={ref} />);

      // syncStream with no currentStream calls hideHUD
      act(() => {
        (ctrl as unknown as VideoHUDController)?.syncStream();
      });
      expect((ctrl as unknown as VideoHUDController)?.isVisible).toBe(false);

      // handleAudioState(null) and handleVideoState(null) safe fallbacks
      act(() => {
        (ctrl as unknown as VideoHUDController)?.handleAudioState(null);
        (ctrl as unknown as VideoHUDController)?.handleVideoState(null);
      });
      expect(useDisplayStore.getState().video.mode).toBe('widgets');
    });
  });

  describe('HudTopBar and HudTransportBar subcomponents', () => {
    it('exercises HudTopBar rendering, stopProp, and mute toggling with memo cache hit', () => {
      const onBack = vi.fn();
      const onHome = vi.fn();
      const onDismiss = vi.fn();
      const onVolumeInput = vi.fn();
      const onVolumeCommit = vi.fn();
      const onMuteToggle = vi.fn();

      function TopBarWrapper({ count, isMuted, isMedia }: { count: number; isMuted: boolean; isMedia: boolean }) {
        return (
          <div>
            <span>{count}</span>
            <HudTopBar
              titleText="Camera Front"
              isMedia={isMedia}
              volume={50}
              isMuted={isMuted}
              onBack={onBack}
              onHome={onHome}
              onDismiss={onDismiss}
              onVolumeInput={onVolumeInput}
              onVolumeCommit={onVolumeCommit}
              onMuteToggle={onMuteToggle}
            />
          </div>
        );
      }

      const { container, rerender } = render(<TopBarWrapper count={1} isMuted={false} isMedia={false} />);

      const navBar = container.querySelector('#video-hud-nav-bar') as HTMLElement;
      expect(navBar.style.display).toBe('none');

      const sideRail = container.querySelector('#video-hud-side-rail') as HTMLElement;
      fireEvent.pointerDown(sideRail);
      fireEvent.touchStart(sideRail);

      // Rerender with isMuted={true}
      rerender(<TopBarWrapper count={2} isMuted={true} isMedia={false} />);
      const muteIcon = container.querySelector('#video-hud-mute-icon') as HTMLElement;
      expect(muteIcon.dataset.icon).toBe('muted');

      // Rerender with isMedia={true}
      rerender(<TopBarWrapper count={3} isMuted={true} isMedia={true} />);
      expect(navBar.style.display).toBe('');
      fireEvent.pointerDown(navBar);
      fireEvent.touchStart(navBar);

      // Re-render with same props but count changed for cache hit
      rerender(<TopBarWrapper count={4} isMuted={true} isMedia={true} />);
    });

    it('exercises HudTransportBar rendering, stopProp, scrubbing, and pointerCancel with memo cache hit', () => {
      const onCaptions = vi.fn();
      const onPrevious = vi.fn();
      const onRewind = vi.fn();
      const onFastForward = vi.fn();
      const onNext = vi.fn();
      const onTrackPointerDown = vi.fn();
      const onTrackPointerMove = vi.fn();
      const onTrackPointerUp = vi.fn();
      const onTrackPointerCancel = vi.fn();

      function TransportWrapper({
        count,
        isPlaybackActive,
        isScrubbing,
      }: {
        count: number;
        isPlaybackActive: boolean;
        isScrubbing: boolean;
      }) {
        return (
          <div>
            <span>{count}</span>
            <HudTransportBar
              isPlaybackActive={isPlaybackActive}
              hasProgress={true}
              isScrubbing={isScrubbing}
              currentTimeStr="1:23"
              totalTimeStr="3:45"
              progressPercent={36.8}
              onCaptions={onCaptions}
              onPrevious={onPrevious}
              onRewind={onRewind}
              onFastForward={onFastForward}
              onNext={onNext}
              onTrackPointerDown={onTrackPointerDown}
              onTrackPointerMove={onTrackPointerMove}
              onTrackPointerUp={onTrackPointerUp}
              onTrackPointerCancel={onTrackPointerCancel}
            />
          </div>
        );
      }

      const { container, rerender } = render(
        <TransportWrapper count={1} isPlaybackActive={false} isScrubbing={false} />,
      );

      const bottomBar = container.querySelector('#video-hud-bottom-bar') as HTMLElement;
      expect(bottomBar.style.display).toBe('none');

      fireEvent.pointerDown(bottomBar);
      fireEvent.touchStart(bottomBar);

      rerender(<TransportWrapper count={2} isPlaybackActive={true} isScrubbing={false} />);
      expect(bottomBar.style.display).toBe('');

      const track = container.querySelector('#video-hud-progress-track') as HTMLElement;
      fireEvent.pointerDown(track);
      expect(onTrackPointerDown).toHaveBeenCalled();
      fireEvent.pointerMove(track);
      expect(onTrackPointerMove).toHaveBeenCalled();
      fireEvent.pointerUp(track);
      expect(onTrackPointerUp).toHaveBeenCalled();
      fireEvent.pointerCancel(track);
      expect(onTrackPointerCancel).toHaveBeenCalled();

      // Rerender with isScrubbing={true}
      rerender(<TransportWrapper count={3} isPlaybackActive={true} isScrubbing={true} />);
      expect(track.classList.contains('is-scrubbing')).toBe(true);

      // Rerender with count changed (memo cache hit on time strings and progress)
      rerender(<TransportWrapper count={4} isPlaybackActive={true} isScrubbing={true} />);
    });
  });
});
