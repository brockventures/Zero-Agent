import { useEffect, useRef, useState } from 'react';

export interface UseHudAutoFadeOptions {
  stageElement?: HTMLElement | null;
  autoFadeTimeout?: number;
  isPlaybackActive: boolean;
  onPlayPauseToggle: () => void;
  isSwapped?: boolean;
}

export interface UseHudAutoFadeReturn {
  isVisible: boolean;
  showHUD: () => void;
  hideHUD: () => void;
  toggleHUD: () => void;
  resetAutoFade: () => void;
}

/**
 * Custom hook coordinating HUD overlay auto-fade visibility lifecycle,
 * stage tap-to-wake / tap-to-toggle, and rapid double-tap play/pause gestures.
 */
export function useHudAutoFade({
  stageElement,
  autoFadeTimeout = 5000,
  isPlaybackActive,
  onPlayPauseToggle,
  isSwapped = false,
}: UseHudAutoFadeOptions): UseHudAutoFadeReturn {
  const [isVisible, setIsVisible] = useState(false);
  const isVisibleRef = useRef(false);

  const autoFadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const singleTapTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTapTimeRef = useRef<number>(0);
  const lastTapCoordsRef = useRef<{ x: number; y: number } | null>(null);
  const doubleTapCooldownUntilRef = useRef<number>(0);
  const lastPointerUpTimeRef = useRef<number>(0);

  const isPlaybackActiveRef = useRef(isPlaybackActive);
  const onPlayPauseToggleRef = useRef(onPlayPauseToggle);
  const isSwappedRef = useRef(isSwapped);

  const hideHUD = () => {
    isVisibleRef.current = false;
    setIsVisible(false);
    if (autoFadeTimerRef.current) {
      clearTimeout(autoFadeTimerRef.current);
      autoFadeTimerRef.current = null;
    }
  };

  const resetAutoFade = () => {
    if (!isVisibleRef.current) return;
    if (autoFadeTimerRef.current) {
      clearTimeout(autoFadeTimerRef.current);
    }
    autoFadeTimerRef.current = setTimeout(() => {
      hideHUD();
    }, autoFadeTimeout);
  };

  const showHUD = () => {
    isVisibleRef.current = true;
    setIsVisible(true);
    if (autoFadeTimerRef.current) {
      clearTimeout(autoFadeTimerRef.current);
    }
    autoFadeTimerRef.current = setTimeout(() => {
      hideHUD();
    }, autoFadeTimeout);
  };

  const toggleHUD = () => {
    if (isVisibleRef.current) {
      hideHUD();
    } else {
      showHUD();
    }
  };

  // Teardown timers on unmount
  useEffect(() => {
    return () => {
      if (autoFadeTimerRef.current) {
        clearTimeout(autoFadeTimerRef.current);
        autoFadeTimerRef.current = null;
      }
      if (singleTapTimerRef.current) {
        clearTimeout(singleTapTimerRef.current);
        singleTapTimerRef.current = null;
      }
    };
  }, []);

  // Synchronize dynamic options to refs before stage gesture bindings
  const showHUDRef = useRef(showHUD);
  const hideHUDRef = useRef(hideHUD);

  useEffect(() => {
    isVisibleRef.current = isVisible;
    isPlaybackActiveRef.current = isPlaybackActive;
    onPlayPauseToggleRef.current = onPlayPauseToggle;
    isSwappedRef.current = isSwapped;
  }, [isVisible, isPlaybackActive, onPlayPauseToggle, isSwapped]);

  useEffect(() => {
    showHUDRef.current = showHUD;
    hideHUDRef.current = hideHUD;
  });

  // Stage gesture bindings
  useEffect(() => {
    if (!stageElement) return;

    let startX: number | null = null;
    let startY: number | null = null;

    const getCoords = (e: Event): { x: number; y: number } | null => {
      const touchEvent = e as TouchEvent;
      if (touchEvent.changedTouches && touchEvent.changedTouches.length > 0) {
        return {
          x: touchEvent.changedTouches[0].clientX,
          y: touchEvent.changedTouches[0].clientY,
        };
      }
      const mouseEvent = e as MouseEvent;
      if (typeof mouseEvent.clientX === 'number' && typeof mouseEvent.clientY === 'number') {
        return { x: mouseEvent.clientX, y: mouseEvent.clientY };
      }
      return null;
    };

    const onStagePointerDown = (e: Event): void => {
      const coords = getCoords(e);
      if (coords) {
        startX = coords.x;
        startY = coords.y;
      } else {
        startX = null;
        startY = null;
      }
    };

    const onStageToggle = (e: Event): void => {
      const now = Date.now();

      // Filter out synthetic duplicate click events using 350ms suppression
      if (e.type === 'click') {
        if (now - lastPointerUpTimeRef.current < 350) {
          return;
        }
      } else if (e.type === 'pointerup') {
        lastPointerUpTimeRef.current = now;
      }

      // Ignore events if in 300ms double-tap cooldown to avoid 3rd-tap pairing
      if (now < doubleTapCooldownUntilRef.current) {
        return;
      }

      // Ignore drag/swipe gestures (movement >= 15px)
      const coords = getCoords(e);
      if (startX !== null && startY !== null && coords) {
        const dist = Math.hypot(coords.x - startX, coords.y - startY);
        if (dist >= 15) {
          lastPointerUpTimeRef.current = now;
          return;
        }
      }

      // Identify which slot is currently rendered as the corner PiP dock
      const cornerDockSelector = isSwappedRef.current ? '.video-primary-slot' : '.video-pip-slot';
      const targetEl = e.target as Element | null;
      if (targetEl && typeof targetEl.closest === 'function' && targetEl.closest(cornerDockSelector)) {
        return;
      }

      // Ignore taps directly on HUD controls
      if (
        targetEl &&
        typeof targetEl.closest === 'function' &&
        (targetEl.closest('.video-hud-controls') ||
          targetEl.closest('.video-hud-header') ||
          targetEl.closest('.video-hud-side-rail') ||
          targetEl.closest('.video-hud-bottom-bar') ||
          targetEl.closest('.video-hud-play-center') ||
          targetEl.closest('#video-hud-play-btn'))
      ) {
        return;
      }

      // Check double-tap (<300ms, <30px)
      const timeDiff = now - lastTapTimeRef.current;
      let isDoubleTap = false;
      if (lastTapTimeRef.current > 0 && timeDiff < 300) {
        if (coords && lastTapCoordsRef.current) {
          const moveDist = Math.hypot(coords.x - lastTapCoordsRef.current.x, coords.y - lastTapCoordsRef.current.y);
          if (moveDist < 30) {
            isDoubleTap = true;
          }
        } else {
          isDoubleTap = true;
        }
      }

      if (isDoubleTap) {
        if (singleTapTimerRef.current) {
          clearTimeout(singleTapTimerRef.current);
          singleTapTimerRef.current = null;
        }
        lastTapTimeRef.current = 0;
        lastTapCoordsRef.current = null;
        doubleTapCooldownUntilRef.current = now + 300;

        if (isPlaybackActiveRef.current) {
          onPlayPauseToggleRef.current();
          showHUDRef.current();
        }
        return;
      }

      // Single-tap handling
      lastTapTimeRef.current = now;
      lastTapCoordsRef.current = coords;

      if (!isVisibleRef.current) {
        showHUDRef.current();
      } else if (isPlaybackActiveRef.current) {
        if (singleTapTimerRef.current) {
          clearTimeout(singleTapTimerRef.current);
        }
        singleTapTimerRef.current = setTimeout(() => {
          singleTapTimerRef.current = null;
          hideHUDRef.current();
        }, 250);
      } else {
        lastTapTimeRef.current = 0;
        lastTapCoordsRef.current = null;
        hideHUDRef.current();
      }
    };

    stageElement.addEventListener('pointerdown', onStagePointerDown);
    stageElement.addEventListener('pointerup', onStageToggle);
    stageElement.addEventListener('click', onStageToggle);

    return () => {
      stageElement.removeEventListener('pointerdown', onStagePointerDown);
      stageElement.removeEventListener('pointerup', onStageToggle);
      stageElement.removeEventListener('click', onStageToggle);
    };
  }, [stageElement]);

  return {
    isVisible,
    showHUD,
    hideHUD,
    toggleHUD,
    resetAutoFade,
  };
}
