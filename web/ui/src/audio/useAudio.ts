import { useEffect, useState } from 'react';
import type { AudioSlice } from '../store/types';
import { useDisplayStore } from '../store';

/**
 * Maximum digital gain ceiling applied to all media elements (80%).
 */
export const MAX_GAIN = 0.80;

/**
 * Ducking attenuation multiplier applied when voice interactions are active (20%).
 */
export const DUCK_MULTIPLIER = 0.20;

/**
 * Voice states that trigger media audio ducking.
 */
export const DUCK_VOICE_STATES: ReadonlySet<string> = new Set([
  'listening',
  'transcribing',
  'thinking',
  'synthesizing',
  'speaking',
]);

/**
 * Pure software volume calculation formula enforcing 80% maximum ceiling:
 * effective = (volume / 100) * 0.80 * (isDucked ? 0.20 : 1.0) * (isMuted ? 0.0 : 1.0)
 *
 * @param volume - Volume percentage between 0 and 100.
 * @param isMuted - Whether master mute is active.
 * @param isDucked - Whether ducking attenuation is active.
 * @returns Effective volume clamped to [0.0, 0.80] and rounded to 4 decimals.
 */
export function computeEffectiveVolume(
  volume: number,
  isMuted: boolean = false,
  isDucked: boolean = false,
): number {
  const num = Number(volume);
  const vol = Number.isNaN(num) ? 0 : Math.max(0, Math.min(100, num));
  const muteMultiplier = isMuted ? 0.0 : 1.0;
  const duckMultiplier = isDucked ? DUCK_MULTIPLIER : 1.0;

  const raw = (vol / 100.0) * MAX_GAIN * duckMultiplier * muteMultiplier;
  const clamped = Math.max(0.0, Math.min(MAX_GAIN, raw));
  return Math.round(clamped * 10000) / 10000;
}

export interface AudioControllerOptions {
  initialVolume?: number;
  initialMuted?: boolean;
  mediaElements?: HTMLMediaElement[];
}

/**
 * AudioController manages digital volume attenuation, mute state synchronization,
 * voice interaction ducking, and the 80% maximum element volume ceiling across HTMLMediaElements.
 */
export class AudioController {
  public volume: number;
  public isMuted: boolean;
  public isDucked: boolean;
  public mediaElements: Set<HTMLMediaElement>;

  constructor(options: AudioControllerOptions = {}) {
    const rawVol = options.initialVolume !== undefined ? Number(options.initialVolume) : 75;
    this.volume = Number.isNaN(rawVol) ? 75 : Math.max(0, Math.min(100, rawVol));
    this.isMuted = Boolean(options.initialMuted);
    this.isDucked = false;
    this.mediaElements = new Set<HTMLMediaElement>();

    if (Array.isArray(options.mediaElements)) {
      for (const el of options.mediaElements) {
        this.registerMediaElement(el);
      }
    }
  }

  get muted(): boolean {
    return this.isMuted;
  }

  set muted(val: boolean) {
    this.isMuted = Boolean(val);
  }

  get ducked(): boolean {
    return this.isDucked;
  }

  set ducked(val: boolean) {
    this.isDucked = Boolean(val);
  }

  /**
   * Updates state from audio slice or audio.state event payload and applies volume to registered elements.
   */
  handleAudioState(data?: Partial<AudioSlice> | null): void {
    if (!data) return;
    if (data.volume !== undefined) {
      const v = Number(data.volume);
      if (!Number.isNaN(v)) {
        this.volume = Math.max(0, Math.min(100, v));
      }
    }
    if (data.muted !== undefined) {
      this.isMuted = Boolean(data.muted);
    }
    this.applyVolumeToAll();
  }

  /**
   * Legacy alias for handleAudioState.
   */
  updateAudioState(state?: Partial<AudioSlice> | null): void {
    this.handleAudioState(state);
  }

  /**
   * Sets ducking attenuation state idempotently and reapplies effective volume.
   */
  setDucked(ducked: boolean): void {
    const nextDucked = Boolean(ducked);
    if (this.isDucked === nextDucked) return;
    this.isDucked = nextDucked;
    this.applyVolumeToAll();
  }

  /**
   * Calculates current effective volume based on volume, mute, and ducking.
   */
  getEffectiveVolume(): number {
    return computeEffectiveVolume(this.volume, this.isMuted, this.isDucked);
  }

  /**
   * Registers an HTMLMediaElement and immediately applies current effective volume.
   */
  registerMediaElement(element: HTMLMediaElement): void {
    if (!element) return;
    this.mediaElements.add(element);
    this.applyVolumeToElement(element);
  }

  /**
   * Unregisters an HTMLMediaElement from volume management.
   */
  unregisterMediaElement(element: HTMLMediaElement): void {
    if (!element) return;
    this.mediaElements.delete(element);
  }

  /**
   * Applies the current effective volume to an individual HTMLMediaElement.
   */
  applyVolumeToElement(element: HTMLMediaElement): void {
    if (!element) return;
    try {
      const effective = this.getEffectiveVolume();
      element.volume = effective;
      element.muted = this.isMuted || effective === 0;
    } catch {
      // ignore
    }
  }

  /**
   * Applies current effective volume across all registered media elements,
   * pruning disconnected elements from the set.
   */
  applyVolumeToAll(): void {
    for (const element of this.mediaElements) {
      if (!element || element.isConnected === false) {
        this.mediaElements.delete(element);
        continue;
      }
      this.applyVolumeToElement(element);
    }
  }

  /**
   * Teardown all registered elements and state.
   */
  destroy(): void {
    this.mediaElements.clear();
  }
}



/**
 * React hook subscribing to display store audio and voice slices,
 * coordinating media elements and setting window.audioManager while mounted.
 */
export function useAudioController(): AudioController {
  const audio = useDisplayStore((s) => s.audio);
  const voiceState = useDisplayStore((s) => s.voice.state);
  const [controller] = useState(
    () =>
      new AudioController({
        initialVolume: audio?.volume ?? 75,
        initialMuted: audio?.muted ?? false,
      }),
  );

  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.audioManager = controller;
    }
    return () => {
      if (typeof window !== 'undefined' && window.audioManager === controller) {
        delete window.audioManager;
      }
      controller.destroy();
    };
  }, [controller]);

  useEffect(() => {
    controller.handleAudioState(audio);
  }, [controller, audio]);

  useEffect(() => {
    const shouldDuck = DUCK_VOICE_STATES.has(voiceState);
    controller.setDucked(shouldDuck);
  }, [controller, voiceState]);

  return controller;
}



declare global {
  interface Window {
    audioManager?: AudioController;
  }
}
