import { useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react';
import { useVideo } from '../store/hooks';

export interface HeaderVideoButtonProps {
  enabled?: boolean;
  streamId?: string;
  streamUrl?: string;
  controlUrl?: string;
  type?: string;
  priority?: string;
  controllable?: boolean;
  fetchFn?: typeof fetch;
}

export function HeaderVideoButton(props: HeaderVideoButtonProps = {}) {
  const video = useVideo();
  const [isLoading, setIsLoading] = useState(false);
  const [hasError, setHasError] = useState(false);

  const startCoordRef = useRef<{ x: number; y: number } | null>(null);
  const lastTriggerTimeRef = useRef<number>(0);
  const safetyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const errorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dataset = typeof document !== 'undefined' ? document.body?.dataset : undefined;
  const isEnabled = props.enabled ?? (dataset?.headerVideo === 'true');
  const streamId = props.streamId ?? dataset?.streamId ?? 'chromecast';
  const streamUrl = props.streamUrl ?? dataset?.streamUrl ?? '';
  const controlUrl = props.controlUrl ?? dataset?.controlUrl ?? '';
  const streamType = props.type ?? dataset?.type ?? 'webrtc';
  const priority = props.priority ?? 'persistent';
  const controllable = props.controllable ?? true;
  const fetchFn = props.fetchFn ?? (typeof fetch === 'function' ? fetch : undefined);

  const clearLoading = () => {
    if (safetyTimerRef.current !== null) {
      clearTimeout(safetyTimerRef.current);
      safetyTimerRef.current = null;
    }
    setIsLoading(false);
  };

  const mode = video?.mode || (video?.primary ? 'video' : 'widgets');
  if (mode === 'video' && isLoading) {
    setIsLoading(false);
  }

  // Clear safety timer when video switches to active video playback
  useEffect(() => {
    const activeMode = video?.mode || (video?.primary ? 'video' : 'widgets');
    if (activeMode === 'video' && safetyTimerRef.current !== null) {
      clearTimeout(safetyTimerRef.current);
      safetyTimerRef.current = null;
    }
  }, [video]);

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (safetyTimerRef.current !== null) {
        clearTimeout(safetyTimerRef.current);
      }
      if (errorTimerRef.current !== null) {
        clearTimeout(errorTimerRef.current);
      }
    };
  }, []);

  const handlePointerDown = (e: ReactPointerEvent<HTMLButtonElement>) => {
    startCoordRef.current = { x: e.clientX, y: e.clientY };
  };

  const triggerVideo = async (e?: ReactPointerEvent<HTMLButtonElement> | ReactMouseEvent<HTMLButtonElement>) => {
    if (isLoading) return;
    const now = Date.now();

    if (e && 'clientX' in e && startCoordRef.current) {
      const dist = Math.hypot(e.clientX - startCoordRef.current.x, e.clientY - startCoordRef.current.y);
      startCoordRef.current = null;
      if (dist >= 15) {
        lastTriggerTimeRef.current = now;
        return;
      }
    }
    startCoordRef.current = null;

    if (now - lastTriggerTimeRef.current < 350) return;
    lastTriggerTimeRef.current = now;

    if (typeof window !== 'undefined') {
      const win = window as unknown as {
        videoManager?: { currentMode: string };
        videoHUD?: { showHUD: () => void };
      };
      if (win.videoManager && win.videoManager.currentMode === 'video') {
        win.videoHUD?.showHUD?.();
        return;
      }
    }

    setIsLoading(true);
    setHasError(false);

    if (safetyTimerRef.current !== null) {
      clearTimeout(safetyTimerRef.current);
    }
    safetyTimerRef.current = setTimeout(clearLoading, 4000);

    if (controlUrl && fetchFn) {
      fetchFn(controlUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: streamId, action: 'wake' }),
      }).catch((err) => {
        console.warn('[MirrormereDisplay] Header video wake failed:', err);
      });
    }

    const payload: Record<string, unknown> = {
      id: streamId,
      stream_url: streamUrl,
      type: streamType,
      priority,
      controllable,
    };
    if (controlUrl) {
      payload.control_url = controlUrl;
    }

    try {
      if (fetchFn) {
        const resp = await fetchFn('api/video/trigger', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (resp && !resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
      }
    } catch (err) {
      console.warn('[MirrormereDisplay] Header video trigger failed:', err);
      clearLoading();
      setHasError(true);
      if (errorTimerRef.current !== null) {
        clearTimeout(errorTimerRef.current);
      }
      errorTimerRef.current = setTimeout(() => {
        setHasError(false);
      }, 1500);
    }
  };

  const handlePointerUp = (e: ReactPointerEvent<HTMLButtonElement>) => {
    void triggerVideo(e);
  };

  const handleClick = (e: ReactMouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    void triggerVideo(e);
  };

  if (!isEnabled) {
    return null;
  }

  const btnClass = ['header-video-btn', isLoading ? 'loading' : '', hasError ? 'error' : '']
    .filter(Boolean)
    .join(' ');

  return (
    <div className="header-video" id="header-video">
      <button
        id="header-video-btn"
        className={btnClass}
        type="button"
        aria-label="Open Fullscreen Video"
        title="Open Fullscreen Video"
        data-stream-id={streamId || undefined}
        data-stream-url={streamUrl || undefined}
        data-control-url={controlUrl || undefined}
        data-type={streamType || undefined}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onClick={handleClick}
      >
        <svg
          className="header-video-icon"
          width="24"
          height="24"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect width="20" height="15" x="2" y="7" rx="2" ry="2" />
          <polyline points="17 2 12 7 7 2" />
        </svg>
        <span className="header-video-spinner" id="header-video-spinner" aria-hidden="true" />
      </button>
    </div>
  );
}
