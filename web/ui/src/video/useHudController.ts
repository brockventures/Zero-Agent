import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { VideoSource, VideoStatePayload } from '../sse/types';
import type { AudioSlice } from '../store/types';
import { useDisplayStore } from '../store';
import {
  formatStreamTitle,
  postRemoteKey,
  postVideoAction,
  postVideoDismiss,
} from './api';
import { useHudAutoFade } from './useHudAutoFade';
import { formatDuration, useStreamProgress } from './useStreamProgress';
import { useVolumeSlider } from './useVolumeSlider';

interface VideoHUDManagerRef {
  isSwapped?: boolean;
  serverPrimary?: VideoSource | null;
  serverPip?: VideoSource | null;
  remoteUrl?: string;
}

type VideoManagerLike = VideoHUDManagerRef;

export interface VideoHUDControllerOptions {
  videoManager?: VideoHUDManagerRef | null;
  remoteUrl?: string;
  stageElement?: HTMLElement | null;
  fetch?: typeof fetch;
  autoFadeTimeout?: number;
  throttleInterval?: number;
  commitCooldown?: number;
  [key: string]: unknown;
}

export interface VideoHUDHandle {
  showHUD: () => void;
  hideHUD: () => void;
  syncStream: () => void;
  handleVideoState: (data?: VideoStatePayload | null) => void;
  handleAudioState: (data?: Partial<AudioSlice> | null) => void;
  activeStream: VideoSource | null;
  videoManager?: VideoHUDControllerOptions['videoManager'] | null;
  lastVideoState?: VideoStatePayload | null;
  isVisible?: boolean;
  currentMuted?: boolean;
  destroy?: () => void;
  titleEl: HTMLElement | null;
  formatStreamTitle?: (stream?: VideoSource | null) => string;
  formatTime?: (seconds: number) => string;
  handlePlayPauseToggle?: () => Promise<void>;
  handleRewind?: () => Promise<void>;
  handleFastForward?: () => Promise<void>;
  handleBack?: () => Promise<void>;
  handleHome?: () => Promise<void>;
  handleCaptions?: () => Promise<void>;
  handlePrevious?: () => Promise<void>;
  handleNext?: () => Promise<void>;
  handleSeek?: (positionSeconds: number) => Promise<void>;
  handleDismiss?: () => void;
  handleMuteToggle?: () => void;
  isScrubbing?: boolean;
}

export class VideoHUDController implements VideoHUDHandle {
  public showHUD: () => void = () => {};
  public hideHUD: () => void = () => {};
  public syncStream: () => void = () => {};
  public handleVideoState: (data?: VideoStatePayload | null) => void = () => {};
  public handleAudioState: (data?: Partial<AudioSlice> | null) => void = () => {};
  public videoManager?: VideoHUDControllerOptions['videoManager'] | null = null;
  public lastVideoState?: VideoStatePayload | null = null;
  public isVisible?: boolean = false;
  public currentMuted?: boolean = false;
  public destroy?: () => void = () => {};
  public formatStreamTitle?: (stream?: VideoSource | null) => string = formatStreamTitle;
  public formatTime?: (seconds: number) => string = formatDuration;
  public handlePlayPauseToggle?: () => Promise<void>;
  public handleRewind?: () => Promise<void>;
  public handleFastForward?: () => Promise<void>;
  public handleBack?: () => Promise<void>;
  public handleHome?: () => Promise<void>;
  public handleCaptions?: () => Promise<void>;
  public handlePrevious?: () => Promise<void>;
  public handleNext?: () => Promise<void>;
  public handleSeek?: (positionSeconds: number) => Promise<void>;
  public handleDismiss?: () => void;
  public handleMuteToggle?: () => void;
  public isScrubbing?: boolean = false;

  private _activeStream: VideoSource | null = null;

  public get activeStream(): VideoSource | null {
    const vm = this.videoManager;
    if (vm) {
      if (vm.isSwapped) {
        return vm.serverPip || this.lastVideoState?.pip || null;
      }
      return vm.serverPrimary || this.lastVideoState?.primary || null;
    }
    return this._activeStream || this.lastVideoState?.primary || this.lastVideoState?.pip || null;
  }

  public set activeStream(stream: VideoSource | null) {
    this._activeStream = stream;
  }

  public get titleEl(): HTMLElement | null {
    return typeof document !== 'undefined' ? document.getElementById('video-hud-title') : null;
  }
}

export interface HudProps {
  controllerRef?:
    | React.MutableRefObject<VideoHUDController | null>
    | React.RefCallback<VideoHUDController | null>;
  videoManager?: VideoHUDControllerOptions['videoManager'];
  remoteUrl?: string;
  stageElement?: HTMLElement | null;
  fetch?: typeof fetch;
  autoFadeTimeout?: number;
  throttleInterval?: number;
  commitCooldown?: number;
}

function assignControllerRef(
  target:
    | React.MutableRefObject<VideoHUDController | null>
    | React.RefCallback<VideoHUDController | null>
    | undefined,
  value: VideoHUDController | null,
): void {
  if (!target) return;
  if (typeof target === 'function') {
    target(value);
  } else {
    target.current = value;
  }
}

function resolveRemoteUrl(remoteUrl?: string, vm?: VideoHUDManagerRef | null): string {
  if (remoteUrl) return remoteUrl;
  if (vm?.remoteUrl) return vm.remoteUrl;
  if (typeof document !== 'undefined' && document.body?.dataset?.remoteUrl) {
    return document.body.dataset.remoteUrl;
  }
  if (typeof window !== 'undefined' && (window as unknown as { MIRRORMERE_REMOTE_URL?: string }).MIRRORMERE_REMOTE_URL) {
    return (window as unknown as { MIRRORMERE_REMOTE_URL?: string }).MIRRORMERE_REMOTE_URL || '';
  }
  return '';
}

async function dispatchHudAction(
  activeStream: VideoSource | null,
  resolvedRemoteUrl: string,
  customFetch: typeof fetch | undefined,
  actionName: string,
  remoteKeyName?: string,
  value?: unknown,
): Promise<void> {
  if (!activeStream || !activeStream.id) return;

  const payload = {
    id: activeStream.id,
    action: actionName,
    ...(value !== undefined ? { value } : {}),
  };

  const isRemoteKeyPreferred = ['back', 'home', 'captions', 'previous', 'next'].includes(actionName);
  if (isRemoteKeyPreferred && resolvedRemoteUrl) {
    await postRemoteKey(resolvedRemoteUrl, remoteKeyName || actionName, customFetch);
  } else if (value !== undefined || actionName === 'seek' || (activeStream.controllable && activeStream.control_url)) {
    await postVideoAction(payload, customFetch);
  } else if (resolvedRemoteUrl) {
    await postRemoteKey(resolvedRemoteUrl, remoteKeyName || actionName, customFetch);
  } else if (activeStream.controllable) {
    await postVideoAction(payload, customFetch);
  }
}

function dispatchHudDismiss(
  primaryId: string | undefined,
  pipId: string | undefined,
  activeStreamId: string | undefined,
  customFetch: typeof fetch | undefined,
): void {
  if (primaryId && pipId && primaryId !== pipId) {
    void postVideoDismiss({ id: 'all' }, customFetch);
  } else {
    const targetId = primaryId || pipId || activeStreamId;
    if (targetId) {
      void postVideoDismiss({ id: targetId }, customFetch);
    }
  }
}

export function useHudController({
  controllerRef,
  videoManager,
  remoteUrl,
  stageElement,
  fetch: customFetch,
  autoFadeTimeout = 5000,
  throttleInterval = 200,
  commitCooldown = 350,
}: HudProps = {}) {
  const overlayRef = useRef<HTMLDivElement>(null);
  const [syncTick, setSyncTick] = useState(0);
  void syncTick;

  const controllerInstanceRef = useRef<VideoHUDController>(new VideoHUDController());
  const [syncedVideoManager, setSyncedVideoManager] = useState<VideoManagerLike | null | undefined>(videoManager);

  const videoState = useDisplayStore((s) => s.video);
  const audioState = useDisplayStore((s) => s.audio);

  const effectiveVideoManager = videoManager ?? syncedVideoManager;
  const isSwapped = Boolean(effectiveVideoManager?.isSwapped);

  const activeStream = effectiveVideoManager && effectiveVideoManager.isSwapped
    ? (effectiveVideoManager.serverPip || videoState?.pip || null)
    : (effectiveVideoManager?.serverPrimary || videoState?.primary || null);

  const resolvedRemoteUrl = resolveRemoteUrl(remoteUrl, effectiveVideoManager);

  const isMedia = Boolean(
    activeStream &&
      (activeStream.controllable === true ||
        (activeStream.priority === 'persistent' && Boolean(resolvedRemoteUrl))),
  );
  const isIdle = Boolean(activeStream && activeStream.player_state === 'idle');
  const isPlaybackActive = Boolean(isMedia && !isIdle);
  const isPaused = activeStream?.player_state === 'paused';

  const resetAutoFadeRef = useRef<() => void>(() => {});
  const isActionPendingRef = useRef(false);

  const executeAction = async (actionName: string, remoteKeyName?: string, value?: unknown): Promise<void> => {
    resetAutoFadeRef.current();
    if (isActionPendingRef.current && actionName !== 'seek') return;
    if (!activeStream || !activeStream.id) return;

    isActionPendingRef.current = true;
    let timer: ReturnType<typeof setTimeout> | null = setTimeout(() => {
      timer = null;
      isActionPendingRef.current = false;
    }, 300);

    try {
      await dispatchHudAction(activeStream, resolvedRemoteUrl, customFetch, actionName, remoteKeyName, value);
    } finally {
      if (!timer) isActionPendingRef.current = false;
    }
  };

  const handlePlayPauseToggle = () => executeAction('toggle_playback', 'play_pause');
  const handleRewind = () => executeAction('rewind', 'rewind');
  const handleFastForward = () => executeAction('fast_forward', 'fast_forward');
  const handleBack = () => executeAction('back', 'back');
  const handleHome = () => executeAction('home', 'home');
  const handleCaptions = () => executeAction('captions', 'cc');
  const handlePrevious = () => executeAction('previous', 'prev');
  const handleNext = () => executeAction('next', 'next');
  const handleSeek = (pos: number) => executeAction('seek', 'seek', pos);

  const handleDismiss = (): void => {
    resetAutoFadeRef.current();
    if (isActionPendingRef.current) return;

    const primaryId = effectiveVideoManager?.serverPrimary?.id || videoState?.primary?.id;
    const pipId = effectiveVideoManager?.serverPip?.id || videoState?.pip?.id;
    if (!primaryId && !pipId && (!activeStream || !activeStream.id)) return;

    isActionPendingRef.current = true;
    setTimeout(() => { isActionPendingRef.current = false; }, 300);
    dispatchHudDismiss(primaryId, pipId, activeStream?.id, customFetch);
  };

  const [fallbackStageEl, setFallbackStageEl] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!stageElement) {
      const found =
        overlayRef.current?.closest<HTMLElement>('#video-stage') ||
        (typeof document !== 'undefined' ? document.getElementById('video-stage') : null);
      if (found) setFallbackStageEl(found);
    }
  }, [stageElement]);

  const effectiveStageEl = stageElement ?? fallbackStageEl ?? (typeof document !== 'undefined' ? document.getElementById('video-stage') : null);

  const { isVisible, showHUD, hideHUD, resetAutoFade } = useHudAutoFade({
    stageElement: effectiveStageEl,
    autoFadeTimeout,
    isPlaybackActive,
    onPlayPauseToggle: handlePlayPauseToggle,
    isSwapped,
  });

  const {
    volume,
    isMuted,
    handleSliderInput,
    handleSliderCommit,
    handleMuteToggle,
  } = useVolumeSlider({
    volume: audioState.volume,
    muted: audioState.muted,
    throttleInterval,
    commitCooldown,
    fetch: customFetch,
    onUserActivity: resetAutoFade,
    audioState,
  });

  const {
    hasProgress,
    isScrubbing,
    currentTimeStr,
    totalTimeStr,
    progressPercent,
    onTrackPointerDown,
    onTrackPointerMove,
    onTrackPointerUp,
    onTrackPointerCancel,
  } = useStreamProgress({
    stream: activeStream,
    isVisible,
    onSeek: handleSeek,
    onUserActivity: resetAutoFade,
  });

  const hadStreamRef = useRef<boolean>(false);

  useEffect(() => {
    const hadStream = hadStreamRef.current;
    const hasStream = activeStream !== null;
    hadStreamRef.current = hasStream;

    if (!hadStream && hasStream) {
      showHUD();
    } else if (hadStream && !hasStream) {
      hideHUD();
    }
  }, [activeStream, hideHUD, showHUD]);

  useLayoutEffect(() => {
    resetAutoFadeRef.current = resetAutoFade;
    const ctrl = controllerInstanceRef.current;
    if (videoManager !== undefined) ctrl.videoManager = videoManager;

    Object.assign(ctrl, {
      showHUD, hideHUD, activeStream, lastVideoState: videoState, isVisible, currentMuted: isMuted,
      destroy: hideHUD, formatStreamTitle, formatTime: formatDuration, handlePlayPauseToggle,
      handleRewind, handleFastForward, handleBack, handleHome, handleCaptions, handlePrevious,
      handleNext, handleSeek, handleDismiss, handleMuteToggle, isScrubbing,
    });

    ctrl.syncStream = () => {
      const vm = ctrl.videoManager ?? videoManager ?? null;
      setSyncedVideoManager(vm);
      const currentStream = vm && vm.isSwapped
        ? (vm.serverPip || videoState?.pip || null)
        : (vm?.serverPrimary || videoState?.primary || null);
      if (!currentStream) {
        hideHUD();
      } else if (!hadStreamRef.current) {
        showHUD();
      }
      setSyncTick((t) => t + 1);
    };

    ctrl.handleVideoState = (data) => {
      if (data !== undefined) {
        useDisplayStore.setState({ video: data ?? { mode: 'widgets', primary: null, pip: null } });
      }
    };

    ctrl.handleAudioState = (data) => {
      if (data) {
        useDisplayStore.setState((s) => ({ audio: { ...s.audio, ...data } }));
      }
    };
  });

  useEffect(() => {
    const ctrl = controllerInstanceRef.current;
    if (typeof window !== 'undefined') window.videoHUD = ctrl;
    assignControllerRef(controllerRef, ctrl);

    return () => {
      if (typeof window !== 'undefined' && window.videoHUD === ctrl) delete window.videoHUD;
      assignControllerRef(controllerRef, null);
    };
  }, [controllerRef]);

  const playerState = activeStream
    ? activeStream.player_state || (isIdle ? 'idle' : 'playing')
    : undefined;

  const titleText = formatStreamTitle(activeStream);

  return {
    overlayRef, isVisible, playerState, titleText, isMedia, isPlaybackActive, isPaused, volume, isMuted,
    hasProgress, isScrubbing, currentTimeStr, totalTimeStr, progressPercent, resetAutoFade,
    handlePlayPauseToggle, handleBack, handleHome, handleDismiss, handleSliderInput, handleSliderCommit,
    handleMuteToggle, handleCaptions, handlePrevious, handleRewind, handleFastForward, handleNext,
    onTrackPointerDown, onTrackPointerMove, onTrackPointerUp, onTrackPointerCancel,
  };
}
