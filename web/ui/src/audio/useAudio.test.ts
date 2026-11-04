import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useDisplayStore } from '../store';
import {
  AudioController,
  DUCK_MULTIPLIER,
  DUCK_VOICE_STATES,
  MAX_GAIN,
  computeEffectiveVolume,
  useAudioController,
} from './useAudio';

describe('useAudio & AudioController', () => {
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    delete window.audioManager;
  });

  afterEach(() => {
    useDisplayStore.setState(initialStore, true);
    delete window.audioManager;
  });

  describe('constants & computeEffectiveVolume', () => {
    it('exports expected constants', () => {
      expect(MAX_GAIN).toBe(0.80);
      expect(DUCK_MULTIPLIER).toBe(0.20);
      expect(DUCK_VOICE_STATES.has('listening')).toBe(true);
      expect(DUCK_VOICE_STATES.has('transcribing')).toBe(true);
      expect(DUCK_VOICE_STATES.has('thinking')).toBe(true);
      expect(DUCK_VOICE_STATES.has('synthesizing')).toBe(true);
      expect(DUCK_VOICE_STATES.has('speaking')).toBe(true);
      expect(DUCK_VOICE_STATES.has('idle')).toBe(false);
    });

    it('computes effective volume correctly under normal conditions', () => {
      expect(computeEffectiveVolume(75)).toBe(0.60);
      expect(computeEffectiveVolume(100)).toBe(0.80);
      expect(computeEffectiveVolume(50)).toBe(0.40);
      expect(computeEffectiveVolume(0)).toBe(0);
    });

    it('clamps volume inputs below 0 and above 100', () => {
      expect(computeEffectiveVolume(-20)).toBe(0);
      expect(computeEffectiveVolume(150)).toBe(0.80);
      expect(computeEffectiveVolume(Number.NaN)).toBe(0);
    });

    it('computes 0 volume when muted', () => {
      expect(computeEffectiveVolume(75, true, false)).toBe(0);
      expect(computeEffectiveVolume(100, true, false)).toBe(0);
    });

    it('computes ducked volume (20% attenuation) when ducked', () => {
      // 75% volume -> (75/100) * 0.80 * 0.20 = 0.12
      expect(computeEffectiveVolume(75, false, true)).toBe(0.12);
      // 100% volume -> (100/100) * 0.80 * 0.20 = 0.16
      expect(computeEffectiveVolume(100, false, true)).toBe(0.16);
    });

    it('computes 0 volume when both ducked and muted', () => {
      expect(computeEffectiveVolume(75, true, true)).toBe(0);
    });

    it('rounds results to 4 decimal places', () => {
      // 33% volume -> (33/100) * 0.80 = 0.264
      expect(computeEffectiveVolume(33)).toBe(0.264);
    });
  });

  describe('AudioController class', () => {
    it('initializes with default or custom options', () => {
      const defaultCtrl = new AudioController();
      expect(defaultCtrl.volume).toBe(75);
      expect(defaultCtrl.isMuted).toBe(false);
      expect(defaultCtrl.isDucked).toBe(false);
      expect(defaultCtrl.getEffectiveVolume()).toBe(0.60);

      const customCtrl = new AudioController({ initialVolume: 50, initialMuted: true });
      expect(customCtrl.volume).toBe(50);
      expect(customCtrl.isMuted).toBe(true);
      expect(customCtrl.getEffectiveVolume()).toBe(0);
    });

    it('provides muted and ducked getters and setters', () => {
      const ctrl = new AudioController();
      expect(ctrl.muted).toBe(false);
      ctrl.muted = true;
      expect(ctrl.isMuted).toBe(true);
      expect(ctrl.muted).toBe(true);

      expect(ctrl.ducked).toBe(false);
      ctrl.ducked = true;
      expect(ctrl.isDucked).toBe(true);
      expect(ctrl.ducked).toBe(true);
    });



    it('registers media elements and applies volume immediately', () => {
      const ctrl = new AudioController({ initialVolume: 75 });
      const el = document.createElement('video');
      document.body.appendChild(el);

      ctrl.registerMediaElement(el);
      expect(ctrl.mediaElements.has(el)).toBe(true);
      expect(el.volume).toBe(0.60);
      expect(el.muted).toBe(false);

      ctrl.unregisterMediaElement(el);
      expect(ctrl.mediaElements.has(el)).toBe(false);
      document.body.removeChild(el);
    });

    it('handles initial mediaElements in constructor', () => {
      const el = document.createElement('audio');
      document.body.appendChild(el);

      const ctrl = new AudioController({ initialVolume: 50, mediaElements: [el] });
      expect(ctrl.mediaElements.has(el)).toBe(true);
      expect(el.volume).toBe(0.40);
      document.body.removeChild(el);
    });

    it('updates state via handleAudioState and updateAudioState', () => {
      const ctrl = new AudioController();
      const el = document.createElement('video');
      document.body.appendChild(el);
      ctrl.registerMediaElement(el);

      ctrl.handleAudioState({ volume: 90, muted: false });
      expect(ctrl.volume).toBe(90);
      expect(ctrl.isMuted).toBe(false);
      expect(el.volume).toBe(0.72);

      ctrl.updateAudioState({ muted: true });
      expect(ctrl.isMuted).toBe(true);
      expect(el.muted).toBe(true);

      document.body.removeChild(el);
    });

    it('toggles ducking idempotently via setDucked', () => {
      const ctrl = new AudioController({ initialVolume: 75 });
      const el = document.createElement('video');
      document.body.appendChild(el);
      ctrl.registerMediaElement(el);

      ctrl.setDucked(true);
      expect(ctrl.isDucked).toBe(true);
      expect(el.volume).toBe(0.12);

      // Re-calling with same value is a no-op
      ctrl.setDucked(true);
      expect(ctrl.isDucked).toBe(true);

      ctrl.setDucked(false);
      expect(ctrl.isDucked).toBe(false);
      expect(el.volume).toBe(0.60);

      document.body.removeChild(el);
    });

    it('prunes disconnected elements in applyVolumeToAll', () => {
      const ctrl = new AudioController({ initialVolume: 75 });
      const el = document.createElement('video');
      // Not attached to DOM: el.isConnected === false
      ctrl.mediaElements.add(el);
      expect(ctrl.mediaElements.size).toBe(1);

      ctrl.applyVolumeToAll();
      expect(ctrl.mediaElements.size).toBe(0);
    });

    it('clears registered elements on destroy', () => {
      const ctrl = new AudioController();
      const el = document.createElement('video');
      ctrl.mediaElements.add(el);
      ctrl.destroy();
      expect(ctrl.mediaElements.size).toBe(0);
    });
  });

  describe('useAudioController & useAudio hook', () => {
    it('sets window.audioManager on mount and cleans up on unmount', () => {
      const { result, unmount } = renderHook(() => useAudioController());
      expect(result.current).toBeInstanceOf(AudioController);
      expect(window.audioManager).toBe(result.current);

      unmount();
      expect(window.audioManager).toBeUndefined();
    });



    it('reacts to store audio updates', () => {
      const { result } = renderHook(() => useAudioController());
      const el = document.createElement('video');
      document.body.appendChild(el);
      result.current.registerMediaElement(el);

      act(() => {
        useDisplayStore.setState({ audio: { volume: 50, muted: false } });
      });

      expect(result.current.volume).toBe(50);
      expect(el.volume).toBe(0.40);

      act(() => {
        useDisplayStore.setState({ audio: { volume: 50, muted: true } });
      });

      expect(result.current.isMuted).toBe(true);
      expect(el.muted).toBe(true);

      document.body.removeChild(el);
    });

    it('ducks when voice state enters speaking and unducks when idle', () => {
      const { result } = renderHook(() => useAudioController());
      const el = document.createElement('video');
      document.body.appendChild(el);
      result.current.registerMediaElement(el);

      expect(result.current.isDucked).toBe(false);
      expect(el.volume).toBe(0.60);

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(result.current.isDucked).toBe(true);
      expect(el.volume).toBe(0.12);

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'idle',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(result.current.isDucked).toBe(false);
      expect(el.volume).toBe(0.60);

      document.body.removeChild(el);
    });
  });
});
