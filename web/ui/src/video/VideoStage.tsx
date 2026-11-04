import { useEffect, useRef, useState } from 'react';
import type { VideoSource, VideoStatePayload } from '../sse/types';
import { useAudioController } from '../audio/useAudio';
import { useDisplayStore } from '../store';
import { Hud, VideoHUDController } from './Hud';
import {
  getNormalizedVideoCoordinates,
} from './coordinates';
import type {
  CoordinateEvent,
  NormalizedCoordinates,
} from './coordinates';
import { VideoSlot } from './VideoSlot';
import type { AudioManagerLike } from './VideoSlot';

export { getNormalizedVideoCoordinates } from './coordinates';
export type { CoordinateEvent, NormalizedCoordinates } from './coordinates';

/**
 * Explicitly forces a layout reflow and multi-frame damage commit on the compositor.
 */
export function flushCompositorPaint(): void {
  if (typeof document === 'undefined') return;

  if (document.body && typeof document.body.offsetHeight === 'number') {
    void document.body.offsetHeight;
  }

  let hb = document.getElementById('mm-compositor-heartbeat');
  if (!hb && document.body && typeof document.body.appendChild === 'function') {
    hb = document.createElement('div');
    hb.id = 'mm-compositor-heartbeat';
    hb.className = 'mm-compositor-heartbeat';
    if (typeof hb.setAttribute === 'function') {
      hb.setAttribute('aria-hidden', 'true');
    }
    document.body.appendChild(hb);
  }

  const rAF =
    typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function'
      ? window.requestAnimationFrame.bind(window)
      : typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame
        : (cb: FrameRequestCallback) => setTimeout(cb, 16);

  let frameCount = 0;
  const step = (): void => {
    frameCount++;
    if (hb) {
      if (hb.dataset) {
        hb.dataset.flush = String(frameCount);
      }
      if (hb.style) {
        hb.style.transform =
          frameCount % 2 === 1 ? 'translateZ(0) scale(1.001)' : 'translateZ(0) scale(1)';
      }
    }
    if (document.body && typeof document.body.offsetHeight === 'number') {
      void document.body.offsetHeight;
    }

    if (frameCount < 2) {
      rAF(step);
    }
  };

  rAF(step);
}

export interface VideoPlayerManagerOptions {
  stageElement?: HTMLElement | null;
  primarySlot?: HTMLElement | null;
  pipSlot?: HTMLElement | null;
  audioManager?: AudioManagerLike | null;
  carousel?: { pause?: () => void; resume?: () => void } | null;
  hud?: VideoHUDController | null;
  window?: Window | null;
  remoteUrl?: string;
  touchForwardingEnabled?: boolean;
  fetch?: typeof fetch;
  RTCPeerConnection?: typeof RTCPeerConnection;
  negotiationTimeout?: number;
  disconnectTimeout?: number;
  maxReconnectAttempts?: number;
  reconnectBaseDelay?: number;
  reconnectBackoffFactor?: number;
  onFallback?: (session: unknown, fallbackTarget: 'widgets' | 'pip_dismissed') => void;
}

/**
 * Lightweight compatibility adapter bridging external/legacy callers
 * to the declarative Zustand display store and VideoStage.
 */
export class VideoPlayerManager {
  public options: VideoPlayerManagerOptions;
  public audioManager: AudioManagerLike | null;
  public hud: VideoHUDController | null = null;
  public isSwapped = false;
  public currentMode: 'widgets' | 'video' = 'widgets';

  constructor(options: VideoPlayerManagerOptions = {}) {
    this.options = options;
    this.audioManager = options.audioManager || null;
    this.hud = options.hud || null;
  }

  get serverPrimary(): VideoSource | null {
    return useDisplayStore.getState().video.primary;
  }

  get serverPip(): VideoSource | null {
    return useDisplayStore.getState().video.pip;
  }

  swapStreams(): void {
    if (typeof window !== 'undefined' && window.videoManager && window.videoManager !== this) {
      window.videoManager.swapStreams();
      this.isSwapped = window.videoManager.isSwapped;
    } else {
      this.isSwapped = !this.isSwapped;
      const stage =
        this.options.stageElement ||
        (typeof document !== 'undefined' ? document.getElementById('video-stage') : null);
      if (stage) {
        stage.dataset.swapped = this.isSwapped ? 'true' : 'false';
      }
    }
  }

  setHUD(hud: VideoHUDController | null): void {
    this.hud = hud;
  }

  syncAudioFocus(): void {}

  handleVideoState(data?: VideoStatePayload | null): void {
    if (data !== undefined) {
      useDisplayStore.setState({
        video: data ?? { mode: 'widgets', primary: null, pip: null },
      });
      this.currentMode = data?.mode || (data?.primary ? 'video' : 'widgets');
    }
  }

  exitVideoMode(): void {
    this.currentMode = 'widgets';
    this.isSwapped = false;
    useDisplayStore.setState({ video: { mode: 'widgets', primary: null, pip: null } });
    flushCompositorPaint();
  }

  destroy(): void {
    this.exitVideoMode();
  }

  getNormalizedVideoCoordinates(
    e: CoordinateEvent,
    videoEl: Parameters<typeof getNormalizedVideoCoordinates>[1],
  ): NormalizedCoordinates | null {
    return getNormalizedVideoCoordinates(e, videoEl);
  }
}

/**
 * VideoStage React component rendering #video-stage, slots, and HUD.
 */
export function VideoStage(): React.JSX.Element {
  const [stageNode, setStageNode] = useState<HTMLDivElement | null>(null);
  const hudControllerRef = useRef<VideoHUDController | null>(null);

  const audioController = useAudioController();
  const videoState = useDisplayStore((s) => s.video);

  const mode = videoState?.mode || (videoState?.primary || videoState?.pip ? 'video' : 'widgets');
  const isActive = mode === 'video' && Boolean(videoState?.primary || videoState?.pip);

  const primaryStream = videoState?.primary || null;
  const pipStream = videoState?.pip || null;

  const [isSwapped, setIsSwapped] = useState(false);
  const effectiveSwapped = Boolean(pipStream) && isSwapped;

  // Sync HUD stream state whenever effectiveSwapped or videoState changes
  useEffect(() => {
    hudControllerRef.current?.syncStream?.();
  }, [effectiveSwapped, videoState]);

  // Carousel coordination on video presentation mode entry/exit
  const prevActiveRef = useRef(false);
  useEffect(() => {
    if (isActive && !prevActiveRef.current) {
      prevActiveRef.current = true;
      try {
        useDisplayStore.getState().pause();
      } catch {
        // ignore
      }
      const win =
        typeof window !== 'undefined'
          ? (window as unknown as { carousel?: { pause?: () => void } })
          : null;
      win?.carousel?.pause?.();
    } else if (!isActive && prevActiveRef.current) {
      prevActiveRef.current = false;
      setIsSwapped(false);
      try {
        useDisplayStore.getState().resume();
      } catch {
        // ignore
      }
      const win =
        typeof window !== 'undefined'
          ? (window as unknown as { carousel?: { resume?: () => void } })
          : null;
      win?.carousel?.resume?.();
      flushCompositorPaint();
    }
  }, [isActive]);

  // Clean up carousel state on unmount
  useEffect(() => {
    return () => {
      if (prevActiveRef.current) {
        try {
          useDisplayStore.getState().resume();
        } catch {
          // ignore
        }
        const win =
          typeof window !== 'undefined'
            ? (window as unknown as { carousel?: { resume?: () => void } })
            : null;
        win?.carousel?.resume?.();
        flushCompositorPaint();
      }
    };
  }, []);

  const handlePrimarySlotClick = () => {
    if (effectiveSwapped && primaryStream && pipStream) {
      setIsSwapped(false);
    }
  };

  const handlePipSlotClick = () => {
    if (!effectiveSwapped && primaryStream && pipStream) {
      setIsSwapped(true);
    }
  };

  // Per-slot failure isolation
  const handlePrimarySlotFailure = () => {
    if (effectiveSwapped && pipStream) {
      // Primary was in corner dock; dismiss dock and leave pip full-screen
      useDisplayStore.setState((s) => ({
        video: { ...s.video, primary: null },
      }));
    } else {
      // Full screen presentation primary failed
      useDisplayStore.setState({
        video: { mode: 'widgets', primary: null, pip: null },
      });
      flushCompositorPaint();
    }
  };

  const handlePipSlotFailure = () => {
    if (!primaryStream) {
      // Primary stream was already lost; PiP was sole active stream, exit to widgets mode
      setIsSwapped(false);
      useDisplayStore.setState({
        video: { mode: 'widgets', primary: null, pip: null },
      });
      flushCompositorPaint();
      return;
    }

    if (effectiveSwapped) {
      // Swapped PiP was full-screen; unswap and promote primary back to full-screen
      setIsSwapped(false);
      useDisplayStore.setState((s) => ({
        video: { ...s.video, pip: null },
      }));
    } else {
      // PiP dock failed; dismiss dock while primary remains full-screen
      useDisplayStore.setState((s) => ({
        video: { ...s.video, pip: null },
      }));
    }
  };

  const resolvedRemoteUrl =
    (typeof document !== 'undefined' && document.body?.dataset?.remoteUrl) ||
    (typeof window !== 'undefined' &&
      (window as unknown as { MIRRORMERE_REMOTE_URL?: string }).MIRRORMERE_REMOTE_URL) ||
    '';

  // Compatibility bridge for window.videoManager
  useEffect(() => {
    if (typeof window === 'undefined') return;

    const managerBridge = {
      get currentMode() {
        return mode;
      },
      get isSwapped() {
        return effectiveSwapped;
      },
      swapStreams: () => {
        if (primaryStream && pipStream) {
          setIsSwapped((s) => !s);
        }
      },
      exitVideoMode: () => {
        useDisplayStore.setState({ video: { mode: 'widgets', primary: null, pip: null } });
        flushCompositorPaint();
      },
      handleVideoState: (data?: VideoStatePayload | null) => {
        if (data !== undefined) {
          useDisplayStore.setState({
            video: data ?? { mode: 'widgets', primary: null, pip: null },
          });
        }
      },
      destroy: () => {
        useDisplayStore.setState({ video: { mode: 'widgets', primary: null, pip: null } });
        flushCompositorPaint();
      },
      get serverPrimary() {
        return primaryStream;
      },
      get serverPip() {
        return pipStream;
      },
      get hud() {
        return hudControllerRef.current;
      },
      setHUD: (hud: VideoHUDController | null) => {
        hudControllerRef.current = hud;
      },
      syncAudioFocus: () => {},
    };

    window.videoManager = managerBridge as unknown as VideoPlayerManager;
    return () => {
      if (window.videoManager === (managerBridge as unknown as VideoPlayerManager)) {
        delete window.videoManager;
      }
    };
  }, [mode, effectiveSwapped, primaryStream, pipStream]);

  const handleHudController = (hud: VideoHUDController | null) => {
    hudControllerRef.current = hud;
    if (typeof window !== 'undefined') {
      if (hud) {
        window.videoHUD = hud;
      } else {
        delete window.videoHUD;
      }
    }
  };

  return (
    <div
      id="video-stage"
      ref={setStageNode}
      className={`video-stage${isActive ? ' active' : ''}`}
      data-swapped={effectiveSwapped ? 'true' : undefined}
      style={{ display: isActive ? 'flex' : 'none' }}
    >
      <VideoSlot
        id="video-primary-slot"
        className="video-primary-slot"
        stream={primaryStream}
        isPipSlot={false}
        isPresentationPrimary={!effectiveSwapped}
        isSwapped={effectiveSwapped}
        remoteUrl={resolvedRemoteUrl}
        audioController={audioController}
        onSlotClick={handlePrimarySlotClick}
        onSlotFailure={handlePrimarySlotFailure}
        style={effectiveSwapped && !primaryStream ? { display: 'none' } : undefined}
      />
      <VideoSlot
        id="video-pip-slot"
        className="video-pip-slot"
        stream={pipStream}
        isPipSlot={true}
        isPresentationPrimary={effectiveSwapped}
        isSwapped={effectiveSwapped}
        remoteUrl={resolvedRemoteUrl}
        audioController={audioController}
        onSlotClick={handlePipSlotClick}
        onSlotFailure={handlePipSlotFailure}
        style={!pipStream ? { display: 'none' } : { display: 'flex' }}
      />
      <Hud
        controllerRef={handleHudController}
        videoManager={{
          isSwapped: effectiveSwapped,
          serverPrimary: primaryStream,
          serverPip: pipStream,
          remoteUrl: resolvedRemoteUrl,
        }}
        remoteUrl={resolvedRemoteUrl}
        stageElement={stageNode}
      />
    </div>
  );
}

declare global {
  interface Window {
    videoManager?: VideoPlayerManager;
    MirrormereVideo?: {
      VideoPlayerManager: typeof VideoPlayerManager;
      getNormalizedVideoCoordinates: typeof getNormalizedVideoCoordinates;
    };
  }
}

if (typeof window !== 'undefined') {
  window.MirrormereVideo = {
    VideoPlayerManager,
    getNormalizedVideoCoordinates,
  };
}
