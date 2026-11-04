import { useEffect, useRef } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { getNormalizedVideoCoordinates } from './coordinates';
import { postTouchEvent } from './api';

export interface UseTouchForwardingOptions {
  enabled?: boolean;
  remoteUrl?: string;
  mediaRef: React.RefObject<HTMLElement | null>;
  customFetch?: typeof fetch;
  throttleMs?: number;
}

export interface TouchForwardingHandlers {
  onPointerDown: (e: ReactPointerEvent) => void;
  onPointerMove: (e: ReactPointerEvent) => void;
  onPointerUp: (e: ReactPointerEvent) => void;
  onPointerCancel: (e: ReactPointerEvent) => void;
  onPointerLeave?: (e: ReactPointerEvent) => void;
}

/**
 * Hook for forwarding pointer/touch gestures on interactive streams to the remote sidecar.
 * Handles down, throttled move (30ms), up, and tap gestures (<= 10px).
 */
export function useTouchForwarding({
  enabled = true,
  remoteUrl = '',
  mediaRef,
  customFetch,
  throttleMs = 30,
}: UseTouchForwardingOptions): TouchForwardingHandlers {
  const isDownRef = useRef(false);
  const startXRef = useRef(0);
  const startYRef = useRef(0);
  const downTimeRef = useRef(0);
  const lastMoveTimeRef = useRef(0);
  const hasMovedRef = useRef(false);

  const onPointerDown = (e: ReactPointerEvent) => {
    if (!enabled || !remoteUrl) return;
    if (e.button !== undefined && e.button !== 0) return;

    const mediaEl = mediaRef.current;
    const coords = getNormalizedVideoCoordinates(e.nativeEvent, mediaEl);
    if (!coords) return;

    isDownRef.current = true;
    hasMovedRef.current = false;
    startXRef.current = e.clientX;
    startYRef.current = e.clientY;
    downTimeRef.current = Date.now();
    lastMoveTimeRef.current = downTimeRef.current;

    e.preventDefault();
    e.stopPropagation();

    void postTouchEvent(remoteUrl, 'down', coords.x, coords.y, customFetch);
  };

  const onPointerMove = (e: ReactPointerEvent) => {
    if (!isDownRef.current || !enabled || !remoteUrl) return;

    const currentX = e.clientX;
    const currentY = e.clientY;
    const dist = Math.hypot(currentX - startXRef.current, currentY - startYRef.current);
    if (dist > 10) {
      hasMovedRef.current = true;
    }

    const now = Date.now();
    if (now - lastMoveTimeRef.current < throttleMs) return;
    lastMoveTimeRef.current = now;

    const mediaEl = mediaRef.current;
    const coords = getNormalizedVideoCoordinates(e.nativeEvent, mediaEl);
    if (!coords) return;

    e.preventDefault();
    e.stopPropagation();

    void postTouchEvent(remoteUrl, 'move', coords.x, coords.y, customFetch);
  };

  const onPointerUp = (e: ReactPointerEvent) => {
    if (!isDownRef.current || !enabled || !remoteUrl) return;
    isDownRef.current = false;

    e.preventDefault();
    e.stopPropagation();

    const mediaEl = mediaRef.current;
    const coords = getNormalizedVideoCoordinates(e.nativeEvent, mediaEl);
    const dist = Math.hypot(e.clientX - startXRef.current, e.clientY - startYRef.current);
    if (dist > 10) {
      hasMovedRef.current = true;
    }

    if (!hasMovedRef.current && coords) {
      void postTouchEvent(remoteUrl, 'tap', coords.x, coords.y, customFetch);
    } else if (coords) {
      void postTouchEvent(remoteUrl, 'up', coords.x, coords.y, customFetch);
    }
  };

  const onPointerCancel = (e: ReactPointerEvent) => {
    if (!isDownRef.current || !enabled || !remoteUrl) return;
    isDownRef.current = false;

    const mediaEl = mediaRef.current;
    const coords = getNormalizedVideoCoordinates(e.nativeEvent, mediaEl);
    const x = coords ? coords.x : 0.5;
    const y = coords ? coords.y : 0.5;

    void postTouchEvent(remoteUrl, 'up', x, y, customFetch);
  };

  useEffect(() => {
    const onWindowBlur = () => {
      if (!isDownRef.current || !enabled || !remoteUrl) return;
      isDownRef.current = false;
      void postTouchEvent(remoteUrl, 'up', 0.5, 0.5, customFetch);
    };

    if (typeof window !== 'undefined') {
      window.addEventListener('blur', onWindowBlur);
      return () => {
        window.removeEventListener('blur', onWindowBlur);
      };
    }
  }, [enabled, remoteUrl, customFetch]);

  return {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel,
    onPointerLeave: onPointerCancel,
  };
}
