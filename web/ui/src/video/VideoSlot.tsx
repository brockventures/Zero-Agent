import { useEffect, useRef, useState } from 'react';
import type { VideoSource } from '../sse/types';
import { useTouchForwarding } from './useTouchForwarding';
import { useWebRTCStream } from './useWebRTCStream';
import { useHlsStream } from './useHlsStream';

export interface AudioManagerLike {
  registerMediaElement?: (el: HTMLMediaElement) => void;
  unregisterMediaElement?: (el: HTMLMediaElement) => void;
}

export interface VideoSlotProps {
  id: string;
  className: string;
  stream: VideoSource | null;
  isPipSlot: boolean;
  isPresentationPrimary: boolean;
  isSwapped: boolean;
  remoteUrl?: string;
  touchForwardingEnabled?: boolean;
  audioController?: AudioManagerLike | null;
  onSlotClick?: () => void;
  onSlotFailure?: () => void;
  customFetch?: typeof fetch;
  RTCPeerConnectionClass?: typeof RTCPeerConnection;
  allowProtocolFallback?: boolean;
  style?: React.CSSProperties;
}

/**
 * VideoSlot renders a single video/image presentation slot with declarative media,
 * SVG wake spinner, audio focus registration, and protocol fallback.
 */
export function VideoSlot({
  id,
  className,
  stream,
  isPipSlot,
  isPresentationPrimary,
  remoteUrl,
  touchForwardingEnabled = true,
  audioController,
  onSlotClick,
  onSlotFailure,
  customFetch,
  RTCPeerConnectionClass,
  allowProtocolFallback = true,
  style,
}: VideoSlotProps): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  const [protocol, setProtocol] = useState<'webrtc' | 'hls' | 'mjpeg'>(() => {
    const t = (stream?.type || 'webrtc').toLowerCase();
    return t === 'mjpeg' || t === 'hls' ? t : 'webrtc';
  });

  const [isLoaded, setIsLoaded] = useState(false);
  const [isFadingOut, setIsFadingOut] = useState(false);
  const [showSpinner, setShowSpinner] = useState(() =>
    Boolean(isPresentationPrimary && !isPipSlot && stream),
  );

  const hasStream = Boolean(stream);
  const streamType = stream?.type;

  const fadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearFadeTimer = () => {
    if (fadeTimerRef.current) {
      clearTimeout(fadeTimerRef.current);
      fadeTimerRef.current = null;
    }
  };

  const dismissSpinner = () => {
    clearFadeTimer();
    setIsLoaded(true);
    setIsFadingOut(true);
    fadeTimerRef.current = setTimeout(() => {
      setShowSpinner(false);
      fadeTimerRef.current = null;
    }, 400);
  };

  const dismissSpinnerRef = useRef(dismissSpinner);
  useEffect(() => {
    dismissSpinnerRef.current = dismissSpinner;
  });

  // Clean up fade timer on unmount
  useEffect(() => {
    return () => {
      clearFadeTimer();
    };
  }, []);

  const [prevStreamKey, setPrevStreamKey] = useState<string | null>(
    stream ? `${stream.id}:${stream.stream_url}:${stream.type}` : null,
  );
  const streamKey = stream ? `${stream.id}:${stream.stream_url}:${stream.type}` : null;

  if (streamKey !== prevStreamKey) {
    setPrevStreamKey(streamKey);
    const t = (streamType || 'webrtc').toLowerCase();
    setProtocol(t === 'mjpeg' || t === 'hls' ? t : 'webrtc');
    setIsLoaded(false);
    setIsFadingOut(false);
    setShowSpinner(isPresentationPrimary && !isPipSlot && hasStream);
  }

  // Clear fade timer when stream identity changes
  useEffect(() => {
    clearFadeTimer();
  }, [streamKey]);

  // 12s spinner safety timeout (#777)
  useEffect(() => {
    if (!showSpinner || isLoaded) return;
    const safetyTimer = setTimeout(() => {
      dismissSpinnerRef.current();
    }, 12000);
    return () => clearTimeout(safetyTimer);
  }, [showSpinner, isLoaded]);

  // Audio focus registration on active full-screen video only
  useEffect(() => {
    const el = videoRef.current;
    if (!el || protocol === 'mjpeg') return;

    if (isPresentationPrimary) {
      el.muted = false;
      audioController?.registerMediaElement?.(el);
    } else {
      el.muted = true;
      el.volume = 0;
      audioController?.unregisterMediaElement?.(el);
    }

    return () => {
      if (el) {
        audioController?.unregisterMediaElement?.(el);
      }
    };
  }, [isPresentationPrimary, audioController, protocol, stream?.id]);

  const handleWebRTCFallback = () => {
    if (allowProtocolFallback) {
      console.warn(`[MirrormereVideo] WebRTC failed for stream ${stream?.id}. Falling back to HLS.`);
      setProtocol('hls');
    } else {
      onSlotFailure?.();
    }
  };

  const handleHlsFallback = () => {
    if (allowProtocolFallback) {
      console.warn(`[MirrormereVideo] HLS failed for stream ${stream?.id}. Falling back to MJPEG.`);
      setProtocol('mjpeg');
    } else {
      onSlotFailure?.();
    }
  };

  const handleTerminalFailure = () => {
    console.warn(`[MirrormereVideo] Stream ${stream?.id} failed terminal protocol.`);
    onSlotFailure?.();
  };

  useWebRTCStream({
    videoRef,
    stream,
    enabled: Boolean(stream) && protocol === 'webrtc',
    fetchFn: customFetch,
    RTCPeerConnectionClass,
    onFallback: () => {
      dismissSpinner();
      handleWebRTCFallback();
    },
  });

  useHlsStream({
    videoRef,
    stream,
    enabled: Boolean(stream) && protocol === 'hls',
  });

  const mediaRef = protocol === 'mjpeg' ? imgRef : videoRef;
  const touchHandlers = useTouchForwarding({
    enabled: Boolean(stream?.controllable) && isPresentationPrimary && touchForwardingEnabled,
    remoteUrl,
    mediaRef,
    customFetch,
  });

  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!isPresentationPrimary && onSlotClick) {
      e.stopPropagation();
      onSlotClick();
    }
  };

  return (
    <div id={id} className={className} style={style} onClick={handleClick}>
      {showSpinner && isPresentationPrimary && (
        <div
          className={`video-stream-spinner${isFadingOut ? ' fade-out' : ''}`}
          aria-label="Waking Stream"
        >
          <div className="video-spinner-ring" />
          <span className="video-spinner-text">Waking Stream...</span>
        </div>
      )}

      {stream && protocol === 'mjpeg' && (
        <img
          ref={imgRef}
          className="video-stream-media"
          alt={`Stream ${stream.id}`}
          src={stream.stream_url}
          onLoad={dismissSpinner}
          onError={() => {
            dismissSpinner();
            handleTerminalFailure();
          }}
          {...(isPresentationPrimary ? touchHandlers : {})}
        />
      )}

      {stream && protocol !== 'mjpeg' && (
        <video
          ref={videoRef}
          className="video-stream-media"
          autoPlay
          playsInline
          muted={!isPresentationPrimary}
          style={{ touchAction: 'none' }}
          onPlaying={dismissSpinner}
          onLoadedData={dismissSpinner}
          onError={() => {
            dismissSpinner();
            if (protocol === 'webrtc') {
              handleWebRTCFallback();
            } else if (protocol === 'hls') {
              handleHlsFallback();
            } else {
              handleTerminalFailure();
            }
          }}
          {...(isPresentationPrimary ? touchHandlers : {})}
        />
      )}
    </div>
  );
}
