import { useEffect, useRef, useState } from 'react';
import type { AudioSlice } from '../store/types';
import { postAudioMute, postAudioVolume } from './api';

export interface UseVolumeSliderOptions {
  volume: number;
  muted: boolean;
  throttleInterval?: number;
  commitCooldown?: number;
  fetch?: typeof fetch;
  onUserActivity?: () => void;
  audioState?: AudioSlice;
}

export interface UseVolumeSliderReturn {
  volume: number;
  isMuted: boolean;
  handleSliderInput: (e: React.FormEvent<HTMLInputElement> | React.ChangeEvent<HTMLInputElement> | { target: { value: string } }) => void;
  handleSliderCommit: (e?: React.SyntheticEvent | Event) => void;
  handleMuteToggle: (e?: React.SyntheticEvent | Event) => void;
}

/**
 * Custom hook managing volume slider state, leading+trailing drag throttling,
 * commit cooldown against stale SSE events, and audio mute toggling.
 */
export function useVolumeSlider({
  volume: storeVolume,
  muted: storeMuted,
  throttleInterval = 200,
  commitCooldown = 350,
  fetch: customFetch,
  onUserActivity,
  audioState,
}: UseVolumeSliderOptions): UseVolumeSliderReturn {
  const [localVolume, setLocalVolume] = useState<number>(storeVolume ?? 75);
  const isDraggingRef = useRef(false);
  const throttleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingVolumeRef = useRef<number | null>(null);
  const lastNetworkVolumeRef = useRef<number | null>(null);
  const commitCooldownUntilRef = useRef<number>(0);
  const isMutePendingRef = useRef(false);

  // Sync incoming store updates when not dragging and not in commit cooldown
  useEffect(() => {
    const now = Date.now();
    if (!isDraggingRef.current && now >= commitCooldownUntilRef.current && storeVolume !== undefined) {
      setLocalVolume(storeVolume);
    }
  }, [audioState, storeVolume]);

  // Teardown timers on unmount
  useEffect(() => {
    return () => {
      if (throttleTimerRef.current) {
        clearTimeout(throttleTimerRef.current);
        throttleTimerRef.current = null;
      }
    };
  }, []);

  const handleSliderInput = (
    e: React.FormEvent<HTMLInputElement> | React.ChangeEvent<HTMLInputElement> | { target: { value: string } },
  ) => {
    onUserActivity?.();
    isDraggingRef.current = true;

    const targetVal = 'value' in e.target ? String(e.target.value) : '';
    const val = parseInt(targetVal, 10);
    if (Number.isNaN(val)) return;

    setLocalVolume(val);
    pendingVolumeRef.current = val;

    if (!throttleTimerRef.current) {
      void postAudioVolume({ volume: val }, customFetch);
      lastNetworkVolumeRef.current = val;

      throttleTimerRef.current = setTimeout(() => {
        throttleTimerRef.current = null;
        if (pendingVolumeRef.current !== null && pendingVolumeRef.current !== lastNetworkVolumeRef.current) {
          void postAudioVolume({ volume: pendingVolumeRef.current }, customFetch);
          lastNetworkVolumeRef.current = pendingVolumeRef.current;
          pendingVolumeRef.current = null;
        }
      }, throttleInterval);
    }
  };

  const handleSliderCommit = (e?: React.SyntheticEvent | Event) => {
    if (e && typeof (e as Event).stopPropagation === 'function') {
      (e as Event).stopPropagation();
    }
    onUserActivity?.();
    isDraggingRef.current = false;

    if (throttleTimerRef.current) {
      clearTimeout(throttleTimerRef.current);
      throttleTimerRef.current = null;
    }

    let val = pendingVolumeRef.current;
    const target = (e && 'target' in e ? e.target : null) as HTMLInputElement | null;
    const currentTarget = (e && 'currentTarget' in e ? e.currentTarget : null) as HTMLInputElement | null;
    const elVal = target?.value ?? currentTarget?.value;
    if (elVal !== undefined && elVal !== '') {
      const parsed = parseInt(String(elVal), 10);
      if (!Number.isNaN(parsed)) {
        val = parsed;
      }
    }

    const finalVal = val ?? localVolume;
    if (!Number.isNaN(finalVal) && finalVal !== lastNetworkVolumeRef.current) {
      void postAudioVolume({ volume: finalVal }, customFetch);
      lastNetworkVolumeRef.current = finalVal;
    }

    pendingVolumeRef.current = null;
    commitCooldownUntilRef.current = Date.now() + commitCooldown;
  };

  const handleMuteToggle = (e?: React.SyntheticEvent | Event) => {
    if (e && typeof (e as Event).stopPropagation === 'function') {
      (e as Event).stopPropagation();
    }
    onUserActivity?.();

    if (isMutePendingRef.current) {
      return;
    }

    isMutePendingRef.current = true;
    setTimeout(() => {
      isMutePendingRef.current = false;
    }, 300);

    const targetMuted = !storeMuted;
    void postAudioMute({ muted: targetMuted }, customFetch);
  };

  return {
    volume: localVolume,
    isMuted: storeMuted,
    handleSliderInput,
    handleSliderCommit,
    handleMuteToggle,
  };
}
