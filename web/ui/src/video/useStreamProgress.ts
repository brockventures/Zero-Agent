import { useEffect, useRef, useState } from 'react';
import type { VideoSource } from '../sse/types';
import { useDisplayStore } from '../store';

/**
 * Format duration in seconds into "M:SS" or "H:MM:SS" format with robust fallback.
 */
export function formatDuration(seconds: number): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
    return '0:00';
  }
  const totalSecs = Math.floor(seconds);
  const hrs = Math.floor(totalSecs / 3600);
  const mins = Math.floor((totalSecs % 3600) / 60);
  const secs = totalSecs % 60;
  const padSecs = String(secs).padStart(2, '0');
  if (hrs > 0) {
    const padMins = String(mins).padStart(2, '0');
    return `${hrs}:${padMins}:${padSecs}`;
  }
  return `${mins}:${padSecs}`;
}

export interface UseStreamProgressOptions {
  stream?: VideoSource | null;
  isVisible: boolean;
  onSeek: (positionSeconds: number) => void;
  onUserActivity?: () => void;
}

export interface UseStreamProgressReturn {
  hasProgress: boolean;
  isScrubbing: boolean;
  currentPosition: number;
  currentTimeStr: string;
  totalTimeStr: string;
  progressPercent: number;
  onTrackPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerUp: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => void;
  formatDuration: typeof formatDuration;
}

/**
 * Custom hook coordinating 1Hz progress interpolation, timestamp drift compensation,
 * and pointer drag scrubbing for video progress bars.
 */
export function useStreamProgress({
  stream,
  isVisible,
  onSeek,
  onUserActivity,
}: UseStreamProgressOptions): UseStreamProgressReturn {
  const documentVisible = useDisplayStore((s) => s.visibility.visible);

  const duration =
    stream?.player_state === 'idle'
      ? 0
      : typeof stream?.duration === 'number' && Number.isFinite(stream.duration)
        ? stream.duration
        : 0;

  const basePosition =
    stream?.player_state === 'idle'
      ? 0
      : typeof stream?.position === 'number' && Number.isFinite(stream.position)
        ? stream.position
        : 0;

  const basePositionTime =
    typeof stream?.position_updated_at === 'number' && Number.isFinite(stream.position_updated_at)
      ? stream.position_updated_at
      : undefined;

  const playerState = stream?.player_state;

  const [currentPos, setCurrentPos] = useState<number>(basePosition);
  const [isScrubbing, setIsScrubbing] = useState<boolean>(false);
  const [scrubPosition, setScrubPosition] = useState<number | null>(null);

  const isScrubbingRef = useRef<boolean>(false);
  const basePositionRef = useRef<number>(basePosition);
  const basePositionTimeRef = useRef<number | null>(basePositionTime ?? null);
  const currentSeekTimeRef = useRef<number>(0);
  const trackElementRef = useRef<HTMLDivElement | null>(null);

  // Synchronize base position when incoming stream updates
  useEffect(() => {
    basePositionRef.current = basePosition;
    basePositionTimeRef.current = basePositionTime ?? Date.now();

    if (!isScrubbingRef.current) {
      if (playerState === 'playing') {
        const elapsed = Math.max(0, (Date.now() - basePositionTimeRef.current) / 1000);
        setCurrentPos(Math.min(duration, basePosition + elapsed));
      } else {
        setCurrentPos(Math.min(duration, basePosition));
      }
    }
  }, [basePosition, basePositionTime, duration, playerState, stream?.id]);

  // 1Hz progress interpolation ticker
  useEffect(() => {
    if (duration <= 0 || playerState !== 'playing' || isScrubbing || !isVisible || !documentVisible) {
      return;
    }

    const interval = setInterval(() => {
      const baseTime = basePositionTimeRef.current ?? Date.now();
      const elapsed = Math.max(0, (Date.now() - baseTime) / 1000);
      setCurrentPos(Math.min(duration, basePositionRef.current + elapsed));
    }, 1000);

    return () => {
      clearInterval(interval);
    };
  }, [documentVisible, duration, isScrubbing, isVisible, playerState]);

  const updateScrub = (clientX: number, target: HTMLElement): number => {
    if (duration <= 0) return 0;
    const rect =
      typeof target.getBoundingClientRect === 'function'
        ? target.getBoundingClientRect()
        : { left: 0, width: 100 };
    const width = rect.width > 0 ? rect.width : 1;
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / width));
    const targetTime = pct * duration;
    currentSeekTimeRef.current = targetTime;
    setScrubPosition(targetTime);
    return targetTime;
  };

  const onTrackPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e && typeof e.stopPropagation === 'function') {
      e.stopPropagation();
    }
    onUserActivity?.();

    if (duration <= 0) return;
    if (!stream || (stream.duration !== undefined && stream.duration <= 0)) return;

    const trackEl = e.currentTarget;
    trackElementRef.current = trackEl;
    if (typeof trackEl.setPointerCapture === 'function' && e.pointerId !== undefined) {
      try {
        trackEl.setPointerCapture(e.pointerId);
      } catch {
        // ignore
      }
    }

    isScrubbingRef.current = true;
    setIsScrubbing(true);
    updateScrub(e.clientX, trackEl);
  };

  const onTrackPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!isScrubbing) return;
    if (e && typeof e.stopPropagation === 'function') {
      e.stopPropagation();
    }
    onUserActivity?.();
    updateScrub(e.clientX, e.currentTarget);
  };

  const onTrackPointerEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!isScrubbing) return;
    if (e && typeof e.stopPropagation === 'function') {
      e.stopPropagation();
    }
    onUserActivity?.();

    const trackEl = trackElementRef.current || e.currentTarget;
    if (trackEl && typeof trackEl.releasePointerCapture === 'function' && e.pointerId !== undefined) {
      try {
        trackEl.releasePointerCapture(e.pointerId);
      } catch {
        // ignore
      }
    }

    isScrubbingRef.current = false;
    setIsScrubbing(false);
    const targetTime = currentSeekTimeRef.current;
    basePositionRef.current = targetTime;
    basePositionTimeRef.current = Date.now();
    onSeek(targetTime);
    setCurrentPos(targetTime);
    setScrubPosition(null);
  };

  const onTrackPointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!isScrubbing) return;
    if (e && typeof e.stopPropagation === 'function') {
      e.stopPropagation();
    }
    onUserActivity?.();

    const trackEl = trackElementRef.current || e.currentTarget;
    if (trackEl && typeof trackEl.releasePointerCapture === 'function' && e.pointerId !== undefined) {
      try {
        trackEl.releasePointerCapture(e.pointerId);
      } catch {
        // ignore
      }
    }

    isScrubbingRef.current = false;
    setIsScrubbing(false);
    setScrubPosition(null);
  };

  const displayPosition = isScrubbing && scrubPosition !== null ? scrubPosition : currentPos;
  const progressPercent = duration > 0 ? Math.min(100, Math.max(0, (displayPosition / duration) * 100)) : 0;
  const hasProgress = duration > 0 && playerState !== 'idle';

  return {
    hasProgress,
    isScrubbing,
    currentPosition: displayPosition,
    currentTimeStr: formatDuration(displayPosition),
    totalTimeStr: formatDuration(duration),
    progressPercent,
    onTrackPointerDown,
    onTrackPointerMove,
    onTrackPointerUp: onTrackPointerEnd,
    onTrackPointerCancel,
    formatDuration,
  };
}
