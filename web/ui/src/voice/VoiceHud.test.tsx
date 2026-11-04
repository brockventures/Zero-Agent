import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioController } from '../audio/useAudio';
import { HeaderVoice } from '../shell/Header';
import { useDisplayStore } from '../store';
import { VoiceIcon } from './VoiceIcon';
import { DUCK_STATES, VoiceHud } from './VoiceHud';
import { deriveVoiceHudProps, resetHUD } from './useVoiceHudState';

function renderVoiceSystem() {
  return render(
    <>
      <HeaderVoice />
      <VoiceHud />
    </>
  );
}

describe('VoiceHud and declarative voice system', () => {
  const initialStore = useDisplayStore.getState();

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    delete window.audioManager;
    delete window.voiceHUD;
    delete window.MirrormereVoice;
    resetHUD();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useDisplayStore.setState(initialStore, true);
    delete window.audioManager;
    delete window.voiceHUD;
    delete window.MirrormereVoice;
    resetHUD();
  });

  describe('constants & icons', () => {
    it('exports duck states and VoiceIcon renders correct SVG icons and hits memo cache', () => {
      expect(DUCK_STATES.has('listening')).toBe(true);
      expect(DUCK_STATES.has('transcribing')).toBe(true);
      expect(DUCK_STATES.has('thinking')).toBe(true);
      expect(DUCK_STATES.has('synthesizing')).toBe(true);
      expect(DUCK_STATES.has('speaking')).toBe(true);
      expect(DUCK_STATES.has('idle')).toBe(false);

      function VoiceIconWrapper({ count, kind }: { count: number; kind: import('./VoiceIcon').VoiceIconKind }) {
        return (
          <div>
            <VoiceIcon kind={kind} />
            <span>{count}</span>
          </div>
        );
      }
      const { container, rerender } = render(<VoiceIconWrapper count={1} kind="mic" />);
      expect(container.querySelector('svg.mm-voice-icon-svg')).not.toBeNull();
      expect(container.innerHTML).toContain('M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z');

      // Exercise memo cache hit on parent rerender with same kind
      rerender(<VoiceIconWrapper count={2} kind="mic" />);
      expect(container.querySelector('svg.mm-voice-icon-svg')).not.toBeNull();

      // Direct renders of think and sparkle
      const thinkRes = render(<VoiceIcon kind="think" />);
      expect(thinkRes.container.innerHTML).toContain('M7.9 20A9 9 0 1 0 4 16.1L2 22Z');

      const sparkleRes = render(<VoiceIcon kind="sparkle" />);
      expect(sparkleRes.container.innerHTML).toContain('m12 3-1.912 5.813');
    });
  });

  describe('VoiceHud and HeaderVoice component state transitions and rendering', () => {
    it('initializes with idle state and exposes window bindings', () => {
      expect(window.MirrormereVoice).toBeUndefined();
      const { unmount } = renderVoiceSystem();

      expect(window.voiceHUD).toBeDefined();
      expect(window.MirrormereVoice).toBeDefined();
      expect(typeof window.MirrormereVoice?.resetHUD).toBe('function');
      expect(window.voiceHUD?.currentState).toBe('idle');
      expect(window.voiceHUD?.currentStatus).toBeNull();
      expect(window.voiceHUD?.turnDismissed).toBe(false);

      const headerVoice = document.getElementById('header-voice');
      const toast = document.getElementById('voice-toast');
      expect(headerVoice?.classList.contains('visible')).toBe(false);
      expect(toast?.classList.contains('visible')).toBe(false);
      expect(toast?.style.display).toBe('none');

      unmount();
      expect(window.voiceHUD).toBeUndefined();
      expect(window.MirrormereVoice).toBeUndefined();
    });

    it('creates fallback AudioController on window.audioManager when absent and tears down on unmount', () => {
      expect(window.audioManager).toBeUndefined();
      const { unmount } = render(<VoiceHud />);

      expect(window.audioManager).toBeInstanceOf(AudioController);
      const fallback = window.audioManager;

      unmount();
      expect(window.audioManager).toBeUndefined();
      expect(fallback?.mediaElements.size).toBe(0);
    });

    it('does not overwrite existing window.audioManager if already provided', () => {
      const mockManager = new AudioController({ initialVolume: 80 });
      window.audioManager = mockManager;

      const { unmount } = render(<VoiceHud />);
      expect(window.audioManager).toBe(mockManager);

      unmount();
      expect(window.audioManager).toBe(mockManager);
    });

    it('handles listening state: reveals header, activates pulse ring, ducks audio, hides toast', () => {
      renderVoiceSystem();
      const headerVoice = document.getElementById('header-voice')!;
      const indicator = document.getElementById('voice-indicator')!;
      const label = document.getElementById('voice-label')!;
      const transcript = document.getElementById('voice-transcript')!;
      const toast = document.getElementById('voice-toast')!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'listening',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(headerVoice.classList.contains('visible')).toBe(true);
      expect(indicator.classList.contains('listening')).toBe(true);
      expect(label.textContent).toBe('Listening...');
      expect(transcript.style.display).toBe('none');
      expect(toast.style.display).toBe('none');
      expect(window.audioManager?.isDucked).toBe(true);
    });

    it('handles transcribing state: halts pulse ring, renders label and transcript', () => {
      renderVoiceSystem();
      const indicator = document.getElementById('voice-indicator')!;
      const label = document.getElementById('voice-label')!;
      const transcript = document.getElementById('voice-transcript')!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'transcribing',
            transcript: 'Turn on the patio heater',
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(indicator.classList.contains('listening')).toBe(false);
      expect(indicator.classList.contains('transcribing')).toBe(true);
      expect(label.textContent).toBe('Transcribing...');
      expect(transcript.textContent).toBe('"Turn on the patio heater"');
      expect(transcript.style.display).toBe('');
      expect(window.audioManager?.isDucked).toBe(true);
    });

    it('handles thinking and synthesizing states', () => {
      renderVoiceSystem();
      const indicator = document.getElementById('voice-indicator')!;
      const label = document.getElementById('voice-label')!;
      const transcript = document.getElementById('voice-transcript')!;

      // Thinking
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'thinking',
            transcript: 'What is the temperature?',
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(indicator.classList.contains('transcribing')).toBe(false);
      expect(indicator.classList.contains('thinking')).toBe(true);
      expect(label.textContent).toBe('Thinking...');
      expect(transcript.textContent).toBe('"What is the temperature?"');
      expect(window.audioManager?.isDucked).toBe(true);

      // Synthesizing
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'synthesizing',
            transcript: 'What is the temperature?',
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(label.textContent).toBe('Synthesizing...');
      expect(transcript.textContent).toBe('"What is the temperature?"');
      expect(window.audioManager?.isDucked).toBe(true);
    });

    it('handles speaking state with caption toasts and various TTS engine badges', () => {
      renderVoiceSystem();
      const indicator = document.getElementById('voice-indicator')!;
      const label = document.getElementById('voice-label')!;
      const transcript = document.getElementById('voice-transcript')!;
      const toast = document.getElementById('voice-toast')!;
      const toastBadge = document.getElementById('voice-toast-badge')!;
      const toastBody = document.getElementById('voice-toast-body')!;
      const audioProgress = document.getElementById('voice-audio-progress')!;

      // 1. Kokoro engine
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'What is the temperature?',
            reply: 'The temperature is 68 degrees.',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });

      expect(indicator.classList.contains('speaking')).toBe(true);
      expect(label.textContent).toBe('Speaking...');
      expect(audioProgress.style.display).toBe('block');
      expect(transcript.textContent).toBe('"The temperature is 68 degrees."');
      expect(toast.style.display).toBe('flex');
      expect(toast.classList.contains('visible')).toBe(true);
      expect(toastBody.textContent).toBe('The temperature is 68 degrees.');
      expect(toastBadge.textContent).toBe('kokoro');
      expect(toastBadge.dataset.engine).toBe('kokoro');
      expect(toastBadge.style.display).toBe('');
      expect(window.audioManager?.isDucked).toBe(true);

      // 2. ElevenLabs engine (case-insensitive conversion)
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: 'Hello from ElevenLabs.',
            tts_engine: 'ElevenLabs',
            status: null,
          },
        });
      });
      expect(toastBadge.textContent).toBe('elevenlabs');
      expect(toastBadge.dataset.engine).toBe('elevenlabs');

      // 3. Piper engine
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: 'Fallback audio.',
            tts_engine: 'piper',
            status: null,
          },
        });
      });
      expect(toastBadge.textContent).toBe('piper');
      expect(toastBadge.dataset.engine).toBe('piper');

      // 4. Null engine hides badge
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: null,
            reply: 'No badge reply.',
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(toastBadge.style.display).toBe('none');

      // 5. Speaking without reply hides toast
      act(() => {
        window.MirrormereVoice?.resetHUD();
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
      expect(toast.style.display).toBe('none');
    });

    it('handles idle and error transitions by resetting HUD and restoring volume', () => {
      renderVoiceSystem();
      const headerVoice = document.getElementById('header-voice')!;
      const toast = document.getElementById('voice-toast')!;

      // Active speaking
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Test',
            reply: 'Hello',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(headerVoice.classList.contains('visible')).toBe(true);
      expect(toast.classList.contains('visible')).toBe(true);
      expect(window.audioManager?.isDucked).toBe(true);

      // Transition to idle
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
      expect(headerVoice.classList.contains('visible')).toBe(false);
      expect(toast.classList.contains('visible')).toBe(false);
      expect(toast.style.display).toBe('none');
      expect(window.audioManager?.isDucked).toBe(false);

      // Active listening -> error transition
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'listening',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(window.audioManager?.isDucked).toBe(true);

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'error',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(headerVoice.classList.contains('visible')).toBe(false);
      expect(window.audioManager?.isDucked).toBe(false);
    });

    it('renders live tool status badge during thinking and clears on speaking/idle', () => {
      renderVoiceSystem();
      const statusEl = document.getElementById('voice-status')!;
      expect(statusEl.style.display).toBe('none');

      // Thinking with tool status
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'thinking',
            transcript: 'Lock front door',
            reply: null,
            tts_engine: null,
            status: '⚡ Checking lock state...',
          },
        });
      });

      expect(statusEl.style.display).toBe('');
      expect(statusEl.textContent).toBe('⚡ Checking lock state...');
      expect(window.voiceHUD?.currentStatus).toBe('⚡ Checking lock state...');

      // Subsequent status update
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'thinking',
            transcript: 'Lock front door',
            reply: null,
            tts_engine: null,
            status: '🔒 Locking front door...',
          },
        });
      });
      expect(statusEl.textContent).toBe('🔒 Locking front door...');

      // Transition to speaking clears status
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Lock front door',
            reply: 'Front door is locked.',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(statusEl.style.display).toBe('none');
      expect(statusEl.textContent).toBe('');
    });

    it('supports tap-to-dismiss for caption toast without reopening during the same turn', () => {
      renderVoiceSystem();
      const toast = document.getElementById('voice-toast')!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Play song',
            reply: 'Playing song now.',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });

      expect(toast.classList.contains('visible')).toBe(true);

      // User clicks toast to dismiss
      fireEvent.click(toast);
      expect(window.voiceHUD?.turnDismissed).toBe(true);
      expect(toast.classList.contains('visible')).toBe(false);
      expect(toast.style.display).toBe('none');

      // Same turn speaking update does NOT reopen toast
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Play song',
            reply: 'Playing song now.',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(toast.classList.contains('visible')).toBe(false);

      // Fresh turn in listening state resets turnDismissed
      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'listening',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });
      expect(window.voiceHUD?.turnDismissed).toBe(false);
    });

    it('integrates with header-voice-dismiss-btn via window.MirrormereVoice.resetHUD()', () => {
      renderVoiceSystem();
      const dismissBtn = document.getElementById('header-voice-dismiss-btn')!;
      const headerVoice = document.getElementById('header-voice')!;

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'speaking',
            transcript: 'Stop',
            reply: 'Stopping now.',
            tts_engine: 'kokoro',
            status: null,
          },
        });
      });
      expect(headerVoice.classList.contains('visible')).toBe(true);
      expect(window.audioManager?.isDucked).toBe(true);

      // Click dismiss button
      fireEvent.click(dismissBtn);

      expect(headerVoice.classList.contains('visible')).toBe(false);
      expect(window.audioManager?.isDucked).toBe(false);
      expect(window.voiceHUD?.currentState).toBe('idle');
    });

    it('arms 30s safety watchdog and resets HUD on timeout', () => {
      vi.useFakeTimers();
      renderVoiceSystem();

      act(() => {
        useDisplayStore.setState({
          voice: {
            state: 'listening',
            transcript: null,
            reply: null,
            tts_engine: null,
            status: null,
          },
        });
      });

      expect(window.audioManager?.isDucked).toBe(true);

      // Advance 29 seconds: still ducked
      act(() => {
        vi.advanceTimersByTime(29000);
      });
      expect(window.audioManager?.isDucked).toBe(true);

      // Advance past 30 seconds: watchdog fires and restores local HUD state without overwriting store voice slice
      act(() => {
        vi.advanceTimersByTime(1500);
      });
      expect(window.voiceHUD?.currentState).toBe('idle');
      expect(window.audioManager?.isDucked).toBe(false);
      expect(useDisplayStore.getState().voice.state).toBe('listening');

      vi.useRealTimers();
    });

    it('re-arms 30s safety watchdog on repeated thinking events with identical status (#874)', () => {
      vi.useFakeTimers();
      renderVoiceSystem();
      const headerVoice = document.getElementById('header-voice')!;
      const setDuckedSpy = vi.spyOn(window.audioManager!, 'setDucked');

      for (let step = 0; step < 3; step++) {
        act(() => {
          useDisplayStore.getState().applyEvents([
            {
              type: 'voice.state',
              data: {
                state: 'thinking',
                transcript: 'Check system status',
                reply: null,
                tts_engine: null,
                status: '⚡ Running diagnostics...',
              },
            },
          ]);
        });
        act(() => {
          vi.advanceTimersByTime(20000);
        });
        expect(headerVoice.classList.contains('visible')).toBe(true);
        expect(window.voiceHUD?.currentState).toBe('thinking');
        expect(window.voiceHUD?.currentStatus).toBe('⚡ Running diagnostics...');
        expect(window.audioManager?.isDucked).toBe(true);
      }

      expect(setDuckedSpy).not.toHaveBeenCalledWith(false);
      vi.useRealTimers();
    });
  });

  describe('deriveVoiceHudProps pure helper', () => {
    it('derives idle state props accurately', () => {
      const idle = deriveVoiceHudProps({
        activeState: 'idle',
        transcript: null,
        reply: null,
        ttsEngine: null,
        statusText: null,
        turnDismissed: false,
        isReset: false,
        safetyTimeoutMs: 30000,
        syncVoice: () => {},
        resetHUD: () => {},
        dismissToast: () => {},
        syncDuckState: () => {},
      });

      expect(idle.headerVoice.isActive).toBe(false);
      expect(idle.headerVoice.stateClass).toBe('');
      expect(idle.headerVoice.label).toBe('');
      expect(idle.headerVoice.iconKind).toBe('mic');
      expect(idle.voiceToast.visible).toBe(false);
    });

    it('derives speaking props accurately with badge and body', () => {
      const speaking = deriveVoiceHudProps({
        activeState: 'speaking',
        transcript: 'Hello',
        reply: 'World',
        ttsEngine: 'Piper',
        statusText: null,
        turnDismissed: false,
        isReset: false,
        safetyTimeoutMs: 30000,
        syncVoice: () => {},
        resetHUD: () => {},
        dismissToast: () => {},
        syncDuckState: () => {},
      });

      expect(speaking.headerVoice.isActive).toBe(true);
      expect(speaking.headerVoice.stateClass).toBe('speaking');
      expect(speaking.headerVoice.iconKind).toBe('sparkle');
      expect(speaking.headerVoice.transcriptText).toBe('"World"');
      expect(speaking.voiceToast.visible).toBe(true);
      expect(speaking.voiceToast.badgeText).toBe('piper');
      expect(speaking.voiceToast.bodyText).toBe('World');
    });
  });
});
