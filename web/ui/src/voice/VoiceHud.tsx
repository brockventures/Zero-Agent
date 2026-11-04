import { useEffect, useLayoutEffect, useRef } from 'react';
import { AudioController } from '../audio/useAudio';
import type { VoiceState } from '../sse/types';
import { Waveform } from './Waveform';
import { DUCK_STATES, useVoiceHudState } from './useVoiceHudState';

export { DUCK_STATES } from './useVoiceHudState';

/**
 * VoiceHud renders the voice toast dock and Waveform mouth visualizer declaratively,
 * sharing state with HeaderVoice via useVoiceHudState().
 */
export function VoiceHud() {
  const hud = useVoiceHudState();
  const { voiceToast } = hud;
  const { visible, badgeText, bodyText } = voiceToast;

  const stateRef = useRef<{
    activeState: VoiceState;
    statusText: string | null;
    turnDismissed: boolean;
  }>({
    activeState: hud.activeState,
    statusText: hud.statusText,
    turnDismissed: hud.turnDismissed,
  });

  const hudRef = useRef(hud);

  useLayoutEffect(() => {
    stateRef.current.activeState = hud.activeState;
    stateRef.current.statusText = hud.statusText;
    stateRef.current.turnDismissed = hud.turnDismissed;
    hudRef.current = hud;
  });

  const handleDismissToast = () => {
    stateRef.current.turnDismissed = true;
    hudRef.current.dismissToast();
  };

  const handleReset = () => {
    stateRef.current.activeState = 'idle';
    stateRef.current.statusText = null;
    stateRef.current.turnDismissed = false;
    hudRef.current.reset();
  };

  // Window bindings & audio manager fallback
  useEffect(() => {
    let createdFallback = false;
    let fallbackController: AudioController | null = null;
    if (typeof window !== 'undefined' && !window.audioManager) {
      fallbackController = new AudioController();
      window.audioManager = fallbackController;
      createdFallback = true;
    }

    if (typeof window !== 'undefined') {
      window.voiceHUD = {
        get currentState() {
          return stateRef.current.activeState;
        },
        get currentStatus() {
          return stateRef.current.statusText;
        },
        get turnDismissed() {
          return stateRef.current.turnDismissed;
        },
        resetHUD: handleReset,
      };

      window.MirrormereVoice = {
        resetHUD: handleReset,
      };
    }

    return () => {
      handleReset();
      if (typeof window !== 'undefined') {
        delete window.voiceHUD;
        delete window.MirrormereVoice;
        window.audioManager?.setDucked?.(false);
      }
      if (createdFallback && typeof window !== 'undefined' && window.audioManager === fallbackController) {
        delete window.audioManager;
        fallbackController?.destroy();
      }
    };
  }, []);

  // Audio ducking & safety watchdog timer
  useEffect(() => {
    const shouldDuck = DUCK_STATES.has(hud.activeState);
    if (typeof window !== 'undefined') {
      window.audioManager?.setDucked?.(shouldDuck);
    }

    let timer: ReturnType<typeof setTimeout> | null = null;
    if (hud.voice && (shouldDuck || Boolean(hud.statusText))) {
      timer = setTimeout(() => {
        handleReset();
      }, 30000);
      const unrefTimer = timer as unknown as { unref?: () => void };
      if (typeof unrefTimer.unref === 'function') {
        unrefTimer.unref();
      }
    }

    return () => {
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [hud.voice, hud.activeState, hud.statusText]);

  return (
    <>
      <div id="voice-toast-dock" className="voice-toast-dock" aria-live="polite">
        <div
          id="voice-toast"
          className={`voice-toast${visible ? ' visible' : ''}`}
          style={{ display: visible ? 'flex' : 'none' }}
          onClick={handleDismissToast}
        >
          <span
            id="voice-toast-badge"
            className="voice-toast-badge"
            data-engine={badgeText ?? undefined}
            style={{ display: badgeText ? undefined : 'none' }}
          >
            {badgeText || null}
          </span>
          <span id="voice-toast-body" className="voice-toast-body">
            {bodyText || null}
          </span>
        </div>
      </div>
      <Waveform />
    </>
  );
}

declare global {
  interface Window {
    voiceHUD?: {
      currentState: string;
      currentStatus: string | null;
      turnDismissed: boolean;
      resetHUD: () => void;
    };
    MirrormereVoice?: {
      resetHUD: () => void;
    };
  }
}
