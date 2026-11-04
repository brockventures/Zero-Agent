import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { VideoSource } from '../sse/types';
import { useDisplayStore } from '../store';
import {
  flushCompositorPaint,
  getNormalizedVideoCoordinates,
  VideoPlayerManager,
  VideoStage,
} from './VideoStage';
import { postTouchEvent } from './api';

class MockRTCPeerConnection {
  public connectionState: RTCPeerConnectionState = 'new';
  public iceConnectionState: RTCIceConnectionState = 'new';
  public iceGatheringState: RTCIceGatheringState = 'complete';
  public localDescription: RTCSessionDescriptionInit | null = null;
  public remoteDescription: RTCSessionDescriptionInit | null = null;
  public transceivers: Array<{
    kind: string;
    direction: string;
    receiver: { playoutDelayHint: number | null; track: { kind: string } };
  }> = [];
  public ontrack: ((ev: RTCTrackEvent) => void) | null = null;
  public onconnectionstatechange: ((ev: Event) => void) | null = null;
  public oniceconnectionstatechange: ((ev: Event) => void) | null = null;
  public closed = false;

  private listeners = new Map<string, Array<(e: Event) => void>>();

  addEventListener(type: string, listener: (e: Event) => void): void {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)!.push(listener);
  }

  removeEventListener(type: string, listener: (e: Event) => void): void {
    const arr = this.listeners.get(type);
    if (arr) {
      this.listeners.set(
        type,
        arr.filter((l) => l !== listener),
      );
    }
  }

  dispatchEvent(event: Event): boolean {
    const arr = this.listeners.get(event.type) || [];
    for (const l of arr) l(event);
    return true;
  }

  setConnectionState(state: RTCPeerConnectionState): void {
    this.connectionState = state;
    this.onconnectionstatechange?.(new Event('connectionstatechange'));
    this.dispatchEvent(new Event('connectionstatechange'));
  }

  setIceConnectionState(state: RTCIceConnectionState): void {
    this.iceConnectionState = state;
    this.oniceconnectionstatechange?.(new Event('iceconnectionstatechange'));
    this.dispatchEvent(new Event('iceconnectionstatechange'));
  }

  addTransceiver(
    kind: string,
    init?: { direction?: string },
  ): {
    kind: string;
    direction: string;
    receiver: { playoutDelayHint: number | null; track: { kind: string } };
  } {
    const receiver = { playoutDelayHint: null, track: { kind } };
    const t = { kind, direction: init?.direction ?? 'sendrecv', receiver };
    this.transceivers.push(t);
    return t;
  }

  getReceivers(): Array<{ playoutDelayHint: number | null; track: { kind: string } }> {
    return this.transceivers.map((t) => t.receiver);
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0\r\no=- 12345 2 IN IP4 127.0.0.1\r\ns=Mirrormere' };
  }

  async setLocalDescription(desc: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = desc;
  }

  async setRemoteDescription(desc: RTCSessionDescriptionInit): Promise<void> {
    this.remoteDescription = desc;
  }

  close(): void {
    this.closed = true;
    this.connectionState = 'closed';
    this.iceConnectionState = 'closed';
  }
}

describe('VideoStage Declarative Suite', () => {
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    cleanup();
    document.body.innerHTML = '';
    useDisplayStore.setState(initialStore, true);
    delete window.videoManager;
    delete window.videoHUD;
    delete (window as unknown as { carousel?: unknown }).carousel;
    HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
    HTMLMediaElement.prototype.load = vi.fn();
    HTMLMediaElement.prototype.pause = vi.fn();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    document.body.innerHTML = '';
    useDisplayStore.setState(initialStore, true);
    delete window.videoManager;
    delete window.videoHUD;
    delete (window as unknown as { carousel?: unknown }).carousel;
  });

  describe('getNormalizedVideoCoordinates', () => {
    it('calculates normalized coordinates correctly with standard 16:9 display', () => {
      const mockVideo = {
        videoWidth: 1920,
        videoHeight: 1080,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 }) as DOMRect,
      };

      const coords = getNormalizedVideoCoordinates({ clientX: 960, clientY: 540 }, mockVideo);
      expect(coords).not.toBeNull();
      expect(coords?.x).toBe(0.5);
      expect(coords?.y).toBe(0.5);
    });

    it('compensates for pillarboxing when 16:9 video is rendered inside ultrawide container', () => {
      const mockVideo = {
        videoWidth: 1920,
        videoHeight: 1080,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 2560, height: 1080, right: 2560, bottom: 1080 }) as DOMRect,
      };

      const center = getNormalizedVideoCoordinates({ clientX: 1280, clientY: 540 }, mockVideo);
      expect(center?.x).toBe(0.5);
      expect(center?.y).toBe(0.5);

      const left = getNormalizedVideoCoordinates({ clientX: 320, clientY: 540 }, mockVideo);
      expect(left?.x).toBe(0);

      const right = getNormalizedVideoCoordinates({ clientX: 2240, clientY: 540 }, mockVideo);
      expect(right?.x).toBe(1);
    });

    it('compensates for letterboxing when 16:9 video is rendered inside 4:3 container', () => {
      const mockVideo = {
        videoWidth: 1920,
        videoHeight: 1080,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 1920, height: 1440, right: 1920, bottom: 1440 }) as DOMRect,
      };

      const center = getNormalizedVideoCoordinates({ clientX: 960, clientY: 720 }, mockVideo);
      expect(center?.x).toBe(0.5);
      expect(center?.y).toBe(0.5);

      const top = getNormalizedVideoCoordinates({ clientX: 960, clientY: 180 }, mockVideo);
      expect(top?.y).toBe(0);

      const bottom = getNormalizedVideoCoordinates({ clientX: 960, clientY: 1260 }, mockVideo);
      expect(bottom?.y).toBe(1);
    });

    it('clamps coordinates outside bounds to [0.0, 1.0]', () => {
      const mockVideo = {
        videoWidth: 1920,
        videoHeight: 1080,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 }) as DOMRect,
      };

      const outside = getNormalizedVideoCoordinates({ clientX: -100, clientY: 2000 }, mockVideo);
      expect(outside?.x).toBe(0);
      expect(outside?.y).toBe(1);
    });

    it('handles touch events using touches and changedTouches', () => {
      const mockVideo = {
        videoWidth: 1000,
        videoHeight: 1000,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 1000, height: 1000, right: 1000, bottom: 1000 }) as DOMRect,
      };

      const touchCoord = getNormalizedVideoCoordinates(
        { touches: [{ clientX: 250, clientY: 750 }] },
        mockVideo,
      );
      expect(touchCoord?.x).toBe(0.25);
      expect(touchCoord?.y).toBe(0.75);

      const changedTouchCoord = getNormalizedVideoCoordinates(
        { changedTouches: [{ clientX: 800, clientY: 200 }] },
        mockVideo,
      );
      expect(changedTouchCoord?.x).toBe(0.8);
      expect(changedTouchCoord?.y).toBe(0.2);
    });

    it('returns null on invalid elements or zero bounds', () => {
      expect(
        getNormalizedVideoCoordinates(
          { clientX: 100, clientY: 100 },
          null as unknown as HTMLVideoElement,
        ),
      ).toBeNull();
      const zeroRectVideo = {
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }) as DOMRect,
      };
      expect(getNormalizedVideoCoordinates({ clientX: 100, clientY: 100 }, zeroRectVideo)).toBeNull();
    });

    it('VideoPlayerManager compatibility adapter exports getNormalizedVideoCoordinates', () => {
      const mgr = new VideoPlayerManager({});
      const mockVideo = {
        videoWidth: 1920,
        videoHeight: 1080,
        getBoundingClientRect: () =>
          ({ left: 0, top: 0, width: 1920, height: 1080, right: 1920, bottom: 1080 }) as DOMRect,
      };
      const direct = getNormalizedVideoCoordinates({ clientX: 960, clientY: 540 }, mockVideo);
      const instance = mgr.getNormalizedVideoCoordinates({ clientX: 960, clientY: 540 }, mockVideo);
      expect(direct).toEqual(instance);
    });
  });

  describe('Presentation Mode Switching & Carousel Coordination', () => {
    it('renders stage structure, sets window.videoManager and window.videoHUD, and cleans up on unmount', () => {
      const { container, unmount } = render(<VideoStage />);

      expect(container.querySelector('#video-stage')).not.toBeNull();
      expect(container.querySelector('#video-primary-slot')).not.toBeNull();
      expect(container.querySelector('#video-pip-slot')).not.toBeNull();
      expect(container.querySelector('#video-hud-overlay')).not.toBeNull();

      expect(window.videoManager).toBeDefined();
      expect(window.videoHUD).toBeDefined();

      unmount();

      expect(window.videoManager).toBeUndefined();
    });

    it('responds to displayStore video slice updates to enter and exit video presentation mode', () => {
      const mockCarousel = { pause: vi.fn(), resume: vi.fn() };
      (window as unknown as { carousel?: typeof mockCarousel }).carousel = mockCarousel;

      render(<VideoStage />);
      const stageEl = document.getElementById('video-stage');

      expect(stageEl?.style.display).toBe('none');

      // Trigger video mode via store
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'doorbell', stream_url: 'http://cam/mjpeg', type: 'mjpeg' },
            pip: null,
          },
        });
      });

      expect(stageEl?.style.display).toBe('flex');
      expect(stageEl?.classList.contains('active')).toBe(true);
      expect(mockCarousel.pause).toHaveBeenCalledTimes(1);

      // Return to widgets mode
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'widgets',
            primary: null,
            pip: null,
          },
        });
      });

      expect(stageEl?.style.display).toBe('none');
      expect(stageEl?.classList.contains('active')).toBe(false);
      expect(mockCarousel.resume).toHaveBeenCalledTimes(1);
    });

    it('reconciles primary stream updates without remounting unchanged streams', () => {
      render(<VideoStage />);
      const initialSource: VideoSource = {
        id: 'cam_a',
        stream_url: 'http://cam/mjpeg',
        type: 'mjpeg',
        title: 'Initial Title',
      };

      act(() => {
        useDisplayStore.setState({
          video: { mode: 'video', primary: initialSource, pip: null },
        });
      });

      const primarySlot = document.getElementById('video-primary-slot');
      const img1 = primarySlot?.querySelector('img');
      expect(img1).not.toBeNull();
      expect(img1?.src).toBe('http://cam/mjpeg');

      // Update metadata in place
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { ...initialSource, title: 'Updated Title' },
            pip: null,
          },
        });
      });

      const img2 = primarySlot?.querySelector('img');
      expect(img2).toBe(img1); // Same element preserved
    });
  });

  describe('Multi-Stream Triad & Media Rendering', () => {
    it('mounts MJPEG stream via responsive img element without audio registration', () => {
      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'doorbell', stream_url: 'http://doorbell/stream.mjpg', type: 'mjpeg' },
            pip: null,
          },
        });
      });

      const primarySlot = document.getElementById('video-primary-slot');
      const img = primarySlot?.querySelector('img');
      expect(img).not.toBeNull();
      expect(img?.src).toBe('http://doorbell/stream.mjpg');
      expect(primarySlot?.querySelector('video')).toBeNull();
    });

    it('mounts HLS stream via video element', () => {
      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'hls_stream', stream_url: 'http://host/stream.m3u8', type: 'hls' },
            pip: null,
          },
        });
      });

      const primarySlot = document.getElementById('video-primary-slot');
      const video = primarySlot?.querySelector('video');
      expect(video).not.toBeNull();
      expect(video?.muted).toBe(false);
    });

    it('mounts WebRTC stream, negotiates SDP offer/answer, and applies zero latency playout delay', async () => {
      const originalPC = window.RTCPeerConnection;
      window.RTCPeerConnection = MockRTCPeerConnection as unknown as typeof RTCPeerConnection;

      let sdpPostSent = false;
      const mockFetch = vi.fn().mockImplementation(async (_url: string, opts) => {
        if (opts?.method === 'POST') {
          sdpPostSent = true;
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify({ sdp: 'v=0\r\ns=go2rtc_answer' }),
          };
        }
        return { ok: true, status: 200, text: async () => '' };
      });
      window.fetch = mockFetch as unknown as typeof fetch;

      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'webrtc_cam', stream_url: 'http://go2rtc/stream', type: 'webrtc' },
            pip: null,
          },
        });
      });

      const primarySlot = document.getElementById('video-primary-slot');
      const video = primarySlot?.querySelector('video');
      expect(video).not.toBeNull();

      // Advance timers for ICE gathering and negotiation
      await act(async () => {
        vi.advanceTimersByTime(600);
      });

      expect(sdpPostSent).toBe(true);

      window.RTCPeerConnection = originalPC;
    });
  });

  describe('Zero-Reparenting PiP Swap & Audio Focus', () => {
    it('manages PiP slot visibility, audio separation, and interactive swap-on-tap', () => {
      render(<VideoStage />);
      const stage = document.getElementById('video-stage');
      const primarySlot = document.getElementById('video-primary-slot');
      const pipSlot = document.getElementById('video-pip-slot');

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p_stream', stream_url: 'http://p/stream.m3u8', type: 'hls' },
            pip: { id: 'pip_stream', stream_url: 'http://pip/stream.m3u8', type: 'hls' },
          },
        });
      });

      expect(pipSlot?.style.display).toBe('flex');
      const primaryVideo = primarySlot?.querySelector('video') as HTMLVideoElement;
      const pipVideo = pipSlot?.querySelector('video') as HTMLVideoElement;

      // Audio focus initially on primary
      expect(primaryVideo.muted).toBe(false);
      expect(pipVideo.muted).toBe(true);
      expect(pipVideo.volume).toBe(0);

      // Tap PiP corner dock to trigger zero-reparenting swap
      act(() => {
        fireEvent.click(pipSlot!);
      });

      expect(stage?.getAttribute('data-swapped')).toBe('true');
      expect(pipVideo.muted).toBe(false);
      expect(primaryVideo.muted).toBe(true);
      expect(primaryVideo.volume).toBe(0);

      // Tap Primary while swapped swaps back
      act(() => {
        fireEvent.click(primarySlot!);
      });

      expect(stage?.getAttribute('data-swapped')).toBeNull();
      expect(primaryVideo.muted).toBe(false);
      expect(pipVideo.muted).toBe(true);

      // Dismissing PiP resets swap state without interrupting primary
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p_stream', stream_url: 'http://p/stream.m3u8', type: 'hls' },
            pip: null,
          },
        });
      });

      expect(pipSlot?.style.display).toBe('none');
      expect(stage?.getAttribute('data-swapped')).toBeNull();
    });
  });

  describe('Watchdog, Reconnect & Fallback Isolation', () => {
    it('auto-reconnects on WebRTC failure and flushes hardware decode buffer', async () => {
      const originalPC = window.RTCPeerConnection;
      let activePC = null as MockRTCPeerConnection | null;

      class TestPC extends MockRTCPeerConnection {}

      window.RTCPeerConnection = function () {
        const pc = new TestPC();
        activePC = pc;
        return pc;
      } as unknown as typeof RTCPeerConnection;
      window.fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: async () => 'v=0\r\ns=answer',
      }) as unknown as typeof fetch;

      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'cast', stream_url: 'http://cast/webrtc', type: 'webrtc' },
            pip: null,
          },
        });
      });

      await act(async () => {
        vi.advanceTimersByTime(600);
      });

      expect(activePC).not.toBeNull();
      const firstPC = activePC;

      // Simulate connection failure
      act(() => {
        activePC?.setConnectionState('failed');
      });

      // Run reconnect backoff timer (1000ms base)
      await act(async () => {
        vi.advanceTimersByTime(1100);
      });

      expect(HTMLMediaElement.prototype.load).toHaveBeenCalled();
      expect(firstPC?.closed).toBe(true);
      expect(activePC).not.toBe(firstPC);

      // Simulate incoming media stream on active WebRTC video element
      const videoEl = document.querySelector('#video-primary-slot video') as HTMLVideoElement;
      expect(videoEl).not.toBeNull();
      const mockStream = {} as MediaStream;
      act(() => {
        activePC?.ontrack?.({
          streams: [mockStream],
          track: { kind: 'video' },
          receiver: { playoutDelayHint: null },
        } as unknown as RTCTrackEvent);
      });
      expect(videoEl.srcObject).toBe(mockStream);
      (HTMLMediaElement.prototype.load as ReturnType<typeof vi.fn>).mockClear();

      // Exit stream (sets primary to null and unmounts <video> in the same commit, #868)
      const exitPC = activePC;
      act(() => {
        useDisplayStore.setState({
          video: { mode: 'widgets', primary: null, pip: null },
        });
      });

      expect(videoEl.srcObject).toBeNull();
      expect(HTMLMediaElement.prototype.load).toHaveBeenCalledTimes(1);
      expect(exitPC?.closed).toBe(true);

      window.RTCPeerConnection = originalPC;
    });

    it('falls back to widgets mode when primary reconnect attempts are exhausted', async () => {
      const originalPC = window.RTCPeerConnection;

      class AlwaysFailPC extends MockRTCPeerConnection {
        async createOffer(): Promise<RTCSessionDescriptionInit> {
          throw new Error('Offer creation failed');
        }
      }

      window.RTCPeerConnection = AlwaysFailPC as unknown as typeof RTCPeerConnection;

      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'cam_exhaust', stream_url: 'http://cam/webrtc', type: 'webrtc' },
            pip: null,
          },
        });
      });

      // Exhaust 3 WebRTC retries (1000ms, 1500ms, 2250ms)
      // Initial failure -> schedules retry 1 (1000ms)
      await act(async () => {
        await Promise.resolve();
      });

      // Retry 1 -> schedules retry 2 (1500ms)
      await act(async () => {
        vi.advanceTimersByTime(1100);
        await Promise.resolve();
      });

      // Retry 2 -> schedules retry 3 (2250ms)
      await act(async () => {
        vi.advanceTimersByTime(1600);
        await Promise.resolve();
      });

      // Retry 3 -> exhausts retries and falls back to HLS
      await act(async () => {
        vi.advanceTimersByTime(2400);
        await Promise.resolve();
      });

      // WebRTC exhausted -> fallback to HLS
      const primarySlot = document.getElementById('video-primary-slot');
      const video = primarySlot?.querySelector('video');
      expect(video).not.toBeNull();

      // Trigger HLS error -> fallback to MJPEG
      await act(async () => {
        if (video) fireEvent.error(video);
        await Promise.resolve();
      });

      const img = primarySlot?.querySelector('img');
      expect(img).not.toBeNull();

      // Trigger MJPEG error -> terminal failure -> exit video mode
      await act(async () => {
        if (img) fireEvent.error(img);
        await Promise.resolve();
      });

      expect(useDisplayStore.getState().video.mode).toBe('widgets');
      expect(useDisplayStore.getState().video.primary).toBeNull();

      window.RTCPeerConnection = originalPC;
    });

    it('handles per-slot failure isolation when swapped vs unswapped', () => {
      render(<VideoStage />);
      const stage = document.getElementById('video-stage');
      const primarySlot = document.getElementById('video-primary-slot');
      const pipSlot = document.getElementById('video-pip-slot');

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p1', stream_url: 'http://p/mjpeg', type: 'mjpeg' },
            pip: { id: 'pip1', stream_url: 'http://pip/mjpeg', type: 'mjpeg' },
          },
        });
      });

      // 1. Swap streams: pip is presentation primary; primary is in corner dock
      act(() => {
        fireEvent.click(pipSlot!);
      });
      expect(stage?.getAttribute('data-swapped')).toBe('true');

      // 2. Corner dock (primary) fails: slot failure callback dismisses dock slot, keeping pip active full-screen
      const primaryImg = primarySlot?.querySelector('img');
      expect(primaryImg).not.toBeNull();
      act(() => {
        if (primaryImg) fireEvent.error(primaryImg);
      });

      expect(useDisplayStore.getState().video.primary).toBeNull();
      expect(useDisplayStore.getState().video.pip).not.toBeNull();
      expect(stage?.style.display).toBe('flex');

      // 3. Subsequent failure of remaining pip stream exits video mode
      const pipImg = pipSlot?.querySelector('img');
      expect(pipImg).not.toBeNull();
      act(() => {
        if (pipImg) fireEvent.error(pipImg);
      });

      expect(useDisplayStore.getState().video.mode).toBe('widgets');
      expect(stage?.style.display).toBe('none');
    });

    it('preserves loaded state and protocol across swap and unswap without stuck spinner', () => {
      render(<VideoStage />);
      const primarySlot = document.getElementById('video-primary-slot');
      const pipSlot = document.getElementById('video-pip-slot');

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p_swap', stream_url: 'http://p/mjpeg', type: 'mjpeg' },
            pip: { id: 'pip_swap', stream_url: 'http://pip/mjpeg', type: 'mjpeg' },
          },
        });
      });

      const primaryImg = primarySlot?.querySelector('img');
      expect(primaryImg).not.toBeNull();

      // Dismiss initial spinner on load
      act(() => {
        if (primaryImg) fireEvent.load(primaryImg);
      });
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(primarySlot?.querySelector('.video-stream-spinner')).toBeNull();

      // Swap streams: pip is presentation primary
      act(() => {
        fireEvent.click(pipSlot!);
      });
      expect(primarySlot?.querySelector('.video-stream-spinner')).toBeNull();

      // Unswap back: primary returns to presentation primary without resetting loaded or showing spinner
      act(() => {
        fireEvent.click(primarySlot!);
      });
      expect(primarySlot?.querySelector('.video-stream-spinner')).toBeNull();
    });

    it('auto-dismisses waking spinner after 12s safety timeout (#777)', () => {
      render(<VideoStage />);
      const primarySlot = document.getElementById('video-primary-slot');

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p_timeout', stream_url: 'http://p/mjpeg', type: 'mjpeg' },
            pip: null,
          },
        });
      });

      expect(primarySlot?.querySelector('.video-stream-spinner')).not.toBeNull();

      // Still visible at 10s
      act(() => {
        vi.advanceTimersByTime(10000);
      });
      expect(primarySlot?.querySelector('.video-stream-spinner')).not.toBeNull();

      // Safety timer fires at 12s + 400ms fade-out
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      act(() => {
        vi.advanceTimersByTime(450);
      });
      expect(primarySlot?.querySelector('.video-stream-spinner')).toBeNull();
    });

    it('forces compositor layout reflow and damage commits on flushCompositorPaint', () => {
      const rAFSpy = vi.spyOn(window, 'requestAnimationFrame');
      flushCompositorPaint();

      expect(document.getElementById('mm-compositor-heartbeat')).not.toBeNull();
      expect(rAFSpy).toHaveBeenCalled();
      rAFSpy.mockRestore();
    });
  });

  describe('Touch Forwarding', () => {
    it('dispatches tap, move, and up actions over remoteUrl', async () => {
      const dispatched: Array<{ url: string; body: unknown }> = [];
      const mockFetch = vi.fn().mockImplementation(async (url: string, opts) => {
        if (opts?.body) {
          dispatched.push({ url, body: JSON.parse(opts.body) });
        }
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
      });
      window.fetch = mockFetch as unknown as typeof fetch;
      document.body.dataset.remoteUrl = 'http://sidecar:8092';

      render(<VideoStage />);

      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: {
              id: 'interactive_cam',
              stream_url: 'http://cam/mjpeg',
              type: 'mjpeg',
              controllable: true,
            },
            pip: null,
          },
        });
      });

      const img = document.querySelector('#video-primary-slot img') as HTMLImageElement;
      expect(img).not.toBeNull();

      img.getBoundingClientRect = () =>
        ({ left: 0, top: 0, width: 1000, height: 1000, right: 1000, bottom: 1000 }) as DOMRect;
      Object.defineProperty(img, 'naturalWidth', { value: 1000, configurable: true });
      Object.defineProperty(img, 'naturalHeight', { value: 1000, configurable: true });

      // Stationary long-press tap (>= 500ms) still dispatches tap (#869)
      act(() => {
        fireEvent.pointerDown(img, { clientX: 500, clientY: 500, button: 0 });
      });
      act(() => {
        vi.advanceTimersByTime(500);
        fireEvent.pointerUp(img, { clientX: 505, clientY: 505, button: 0 });
      });

      expect(dispatched.length).toBe(2);
      expect(dispatched[0].url).toBe('http://sidecar:8092/touch');
      expect(dispatched[0].body).toEqual({ action: 'down', x: 0.5, y: 0.5 });
      expect(dispatched[1].body).toEqual({ action: 'tap', x: 0.505, y: 0.505 });

      // 12px slip (> 10px threshold) cancels tap and sends up (#869)
      dispatched.length = 0;
      act(() => {
        vi.advanceTimersByTime(400);
        fireEvent.pointerDown(img, { clientX: 500, clientY: 500, button: 0 });
      });
      act(() => {
        fireEvent.pointerMove(img, { clientX: 512, clientY: 500 });
        fireEvent.pointerUp(img, { clientX: 512, clientY: 500, button: 0 });
      });
      expect(dispatched.map((d) => (d.body as { action: string }).action)).toEqual(['down', 'up']);

      // Drag gesture with 30ms move throttle (#869)
      dispatched.length = 0;
      act(() => {
        fireEvent.pointerDown(img, { clientX: 100, clientY: 100, button: 0 });
      });

      await act(async () => {
        vi.advanceTimersByTime(35);
      });

      act(() => {
        fireEvent.pointerMove(img, { clientX: 300, clientY: 300 });
      });
      act(() => {
        fireEvent.pointerUp(img, { clientX: 300, clientY: 300 });
      });

      expect(dispatched.length).toBe(3);
      expect(dispatched[0].body).toEqual({ action: 'down', x: 0.1, y: 0.1 });
      expect(dispatched[1].body).toEqual({ action: 'move', x: 0.3, y: 0.3 });
      expect(dispatched[2].body).toEqual({ action: 'up', x: 0.3, y: 0.3 });

      delete document.body.dataset.remoteUrl;
    });

    it('falls back to /tap if /touch fails on tap action', async () => {
      const calls: string[] = [];
      const mockFetch = vi.fn().mockImplementation(async (url: string) => {
        calls.push(url);
        if (url.endsWith('/touch')) {
          throw new Error('404 Not Found');
        }
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
      });

      await postTouchEvent(
        'http://sidecar:8092',
        'tap',
        0.5,
        0.5,
        mockFetch as unknown as typeof fetch,
      );

      expect(calls).toEqual(['http://sidecar:8092/touch', 'http://sidecar:8092/tap']);
    });
  });

  describe('HUD Synchronization', () => {
    it('synchronizes HUD stream state on video state updates, PiP dismissal while swapped, and mode exit', () => {
      render(<VideoStage />);
      const hud = window.videoHUD!;
      expect(hud).toBeDefined();

      // Enter video mode with primary and PiP
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: {
              id: 'primary_stream',
              stream_url: 'http://p/mjpeg',
              type: 'mjpeg',
              title: 'Primary Live',
            },
            pip: {
              id: 'pip_stream',
              stream_url: 'http://pip/mjpeg',
              type: 'mjpeg',
              title: 'PiP Live',
            },
          },
        });
      });

      expect(hud.activeStream?.id).toBe('primary_stream');
      expect(hud.titleEl?.textContent).toBe('Primary Live');

      // Swap streams
      act(() => {
        window.videoManager?.swapStreams();
      });
      expect(hud.activeStream?.id).toBe('pip_stream');
      expect(hud.titleEl?.textContent).toBe('PiP Live');

      // Dismiss PiP while swapped: resets swap state and synchronizes HUD back to primary
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: {
              id: 'primary_stream',
              stream_url: 'http://p/mjpeg',
              type: 'mjpeg',
              title: 'Primary Live',
            },
            pip: null,
          },
        });
      });

      expect(window.videoManager?.isSwapped).toBe(false);
      expect(hud.activeStream?.id).toBe('primary_stream');
      expect(hud.titleEl?.textContent).toBe('Primary Live');

      // Update stream title while in video mode
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: {
              id: 'primary_stream',
              stream_url: 'http://p/mjpeg',
              type: 'mjpeg',
              title: 'Updated Movie Title',
            },
            pip: null,
          },
        });
      });

      expect(hud.activeStream?.title).toBe('Updated Movie Title');
      expect(hud.titleEl?.textContent).toBe('Updated Movie Title');

      // Exit video mode clears activeStream on HUD
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'widgets',
            primary: null,
            pip: null,
          },
        });
      });

      expect(hud.activeStream).toBeNull();
    });

    it('exercises VideoPlayerManager class methods and managerBridge getters/actions', () => {
      render(<VideoStage />);
      const mgr = window.videoManager!;
      expect(mgr).toBeDefined();

      // managerBridge properties & methods
      expect(mgr.currentMode).toBe('widgets');
      expect(mgr.isSwapped).toBe(false);
      expect(mgr.serverPrimary).toBeNull();
      expect(mgr.serverPip).toBeNull();
      expect(mgr.hud).toBeDefined();

      mgr.setHUD(null);
      mgr.syncAudioFocus();

      act(() => {
        mgr.handleVideoState({
          mode: 'video',
          primary: { id: 'p', stream_url: 'http://p', type: 'mjpeg' },
          pip: { id: 'pip', stream_url: 'http://pip', type: 'mjpeg' },
        });
      });
      expect(useDisplayStore.getState().video.primary?.id).toBe('p');
      expect(window.videoManager?.serverPrimary?.id).toBe('p');
      expect(window.videoManager?.serverPip?.id).toBe('pip');

      act(() => {
        mgr.handleVideoState(undefined);
      });
      expect(useDisplayStore.getState().video.primary?.id).toBe('p');

      act(() => {
        mgr.handleVideoState(null);
      });
      expect(useDisplayStore.getState().video.mode).toBe('widgets');

      act(() => {
        window.videoManager?.exitVideoMode();
      });
      expect(useDisplayStore.getState().video.mode).toBe('widgets');

      act(() => {
        window.videoManager?.destroy();
      });

      // Direct VideoPlayerManager class instance
      delete window.videoManager;
      const vpm = new VideoPlayerManager();
      expect(vpm.serverPrimary).toBeNull();
      expect(vpm.serverPip).toBeNull();
      vpm.setHUD(null);
      vpm.syncAudioFocus();
      vpm.swapStreams();
      expect(vpm.isSwapped).toBe(true);

      vpm.handleVideoState({
        mode: 'video',
        primary: { id: 'p2', stream_url: 'http://p2', type: 'mjpeg' },
        pip: null,
      });
      expect(vpm.currentMode).toBe('video');

      vpm.exitVideoMode();
      expect(vpm.currentMode).toBe('widgets');
      vpm.destroy();

      // Delegation test when window.videoManager is another instance
      const externalManager = {
        isSwapped: false,
        swapStreams: vi.fn(function (this: { isSwapped: boolean }) {
          this.isSwapped = true;
        }),
      };
      window.videoManager = externalManager as unknown as VideoPlayerManager;
      const delegatingVpm = new VideoPlayerManager();
      delegatingVpm.swapStreams();
      expect(externalManager.swapStreams).toHaveBeenCalled();
      expect(delegatingVpm.isSwapped).toBe(true);
      delete window.videoManager;
    });

    it('handles per-slot failure cascades for primary and PiP streams', () => {
      // 1. PiP slot fails while swapped: unswaps and sets pip to null
      useDisplayStore.setState({
        video: {
          mode: 'video',
          primary: { id: 'p', stream_url: 'http://p', type: 'mjpeg' },
          pip: { id: 'pip', stream_url: 'http://pip-err', type: 'mjpeg' },
        },
      });

      const { container } = render(<VideoStage />);
      const pipImg = container.querySelector('#video-pip-slot img') as HTMLImageElement;
      expect(pipImg).not.toBeNull();

      // Swap streams first
      act(() => {
        window.videoManager?.swapStreams();
      });
      expect(window.videoManager?.isSwapped).toBe(true);

      // Trigger pip image error
      fireEvent.error(pipImg);
      expect(useDisplayStore.getState().video.pip).toBeNull();
      expect(useDisplayStore.getState().video.primary?.id).toBe('p');
      expect(window.videoManager?.isSwapped).toBe(false);

      // 2. PiP slot fails as sole stream: exits to widgets mode
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: null,
            pip: { id: 'pip-only', stream_url: 'http://pip-only', type: 'mjpeg' },
          },
        });
      });
      const pipOnlyImg = container.querySelector('#video-pip-slot img') as HTMLImageElement;
      expect(pipOnlyImg).not.toBeNull();
      fireEvent.error(pipOnlyImg);
      expect(useDisplayStore.getState().video.mode).toBe('widgets');

      // 3. Primary slot fails while swapped (with active pip): primary cleared, pip retained
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p-swapped', stream_url: 'http://p-swapped', type: 'mjpeg' },
            pip: { id: 'pip-active', stream_url: 'http://pip-active', type: 'mjpeg' },
          },
        });
      });
      act(() => {
        window.videoManager?.swapStreams();
      });
      const primaryImg = container.querySelector('#video-primary-slot img') as HTMLImageElement;
      expect(primaryImg).not.toBeNull();
      fireEvent.error(primaryImg);
      expect(useDisplayStore.getState().video.primary).toBeNull();
      expect(useDisplayStore.getState().video.pip?.id).toBe('pip-active');

      // 4. PiP slot fails while NOT swapped (normal corner dock failure)
      act(() => {
        useDisplayStore.setState({
          video: {
            mode: 'video',
            primary: { id: 'p-normal', stream_url: 'http://p-normal', type: 'mjpeg' },
            pip: { id: 'pip-dock', stream_url: 'http://pip-dock', type: 'mjpeg' },
          },
        });
      });
      const pipDockImg = container.querySelector('#video-pip-slot img') as HTMLImageElement;
      expect(pipDockImg).not.toBeNull();
      fireEvent.error(pipDockImg);
      expect(useDisplayStore.getState().video.pip).toBeNull();
      expect(useDisplayStore.getState().video.primary?.id).toBe('p-normal');
    });

    it('covers flushCompositorPaint fallback when requestAnimationFrame is undefined', () => {
      const origRAF = window.requestAnimationFrame;
      // @ts-expect-error test fallback branch
      delete window.requestAnimationFrame;
      flushCompositorPaint();
      act(() => {
        vi.advanceTimersByTime(32);
      });
      window.requestAnimationFrame = origRAF;
    });

    it('handles VideoPlayerManager edge branches: undefined data, inferred mode, and swapped destroy', () => {
      const vpm = new VideoPlayerManager();

      // 1. undefined data
      vpm.handleVideoState(undefined as unknown as import('../sse/types').VideoStatePayload);

      // 2. data without mode but with primary and pip -> infers mode = 'video'
      vpm.handleVideoState({
        primary: { id: 'prim', stream_url: 'http://prim', type: 'mjpeg' },
        pip: { id: 'pip2', stream_url: 'http://pip2', type: 'mjpeg' },
      } as unknown as import('../sse/types').VideoStatePayload);
      expect(vpm.currentMode).toBe('video');

      // 3. swapStreams when primary and pip both exist
      vpm.swapStreams();
      expect(vpm.isSwapped).toBe(true);

      // 4. destroy when isSwapped is true -> resets swap state
      vpm.destroy();
      expect(vpm.isSwapped).toBe(false);
    });

    it('resolves remoteUrl fallback from window.MIRRORMERE_REMOTE_URL', () => {
      (window as unknown as { MIRRORMERE_REMOTE_URL?: string }).MIRRORMERE_REMOTE_URL = 'http://window-remote';
      const { unmount } = render(<VideoStage />);
      unmount();
      delete (window as unknown as { MIRRORMERE_REMOTE_URL?: string }).MIRRORMERE_REMOTE_URL;
    });
  });
});
