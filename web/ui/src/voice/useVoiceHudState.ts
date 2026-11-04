import { useLayoutEffect, useSyncExternalStore } from 'react';
import { AudioController } from '../audio/useAudio';
import type { VoiceState } from '../sse/types';
import { useDisplayStore } from '../store';
import type { VoiceSlice } from '../store/types';
import type { VoiceIconKind } from './VoiceIcon';

export const DUCK_STATES: ReadonlySet<VoiceState> = new Set<VoiceState>([
  'listening',
  'transcribing',
  'thinking',
  'synthesizing',
  'speaking',
]);

export interface HeaderVoiceViewProps {
  isActive: boolean;
  stateClass: string;
  iconKind: VoiceIconKind;
  label: string;
  statusText: string | null;
  transcriptText: string | null;
  showDot: boolean;
  showPulse: boolean;
  showProgress: boolean;
}

export interface VoiceToastViewProps {
  visible: boolean;
  stateClass: string;
  badgeText: string | null;
  bodyText: string | null;
}

export interface VoiceHudSnapshot extends HeaderVoiceViewProps {
  headerVoice: HeaderVoiceViewProps;
  voiceToast: VoiceToastViewProps;
  dismissToast: () => void;
  resetHUD: () => void;
  reset: () => void;
  voice: VoiceSlice;
  activeState: VoiceState;
  statusText: string | null;
  turnDismissed: boolean;
}

export function resetHUD(): void {
  const voice = useDisplayStore.getState().voice;
  currentLocal = { turnDismissed: false, statusText: null, prevVoice: voice, resetForVoice: voice };
  for (const listener of [...localListeners]) listener();
  if (typeof window !== 'undefined' && window.audioManager?.setDucked) {
    window.audioManager.setDucked(false);
  }
}

const IDLE_HEADER_VOICE: Readonly<HeaderVoiceViewProps> = Object.freeze({
  isActive: false,
  stateClass: '',
  iconKind: 'mic' as VoiceIconKind,
  label: '',
  statusText: null,
  transcriptText: null,
  showDot: false,
  showPulse: false,
  showProgress: false,
});

const IDLE_VOICE_TOAST: Readonly<VoiceToastViewProps> = Object.freeze({
  visible: false,
  stateClass: '',
  badgeText: null,
  bodyText: null,
});

const IDLE_DERIVED_PROPS = Object.freeze({
  headerVoice: IDLE_HEADER_VOICE as HeaderVoiceViewProps,
  voiceToast: IDLE_VOICE_TOAST as VoiceToastViewProps,
});

export function deriveVoiceHudProps(state: {
  activeState: VoiceState | string;
  transcript?: string | null;
  reply?: string | null;
  ttsEngine?: string | null;
  statusText?: string | null;
  turnDismissed?: boolean;
  isReset?: boolean;
  [key: string]: unknown;
}): {
  headerVoice: HeaderVoiceViewProps;
  voiceToast: VoiceToastViewProps;
} {
  const activeState = (state.activeState || 'idle') as VoiceState;
  const transcript = state.transcript ?? null;
  const reply = state.reply ?? null;
  const ttsEngine = state.ttsEngine ?? null;
  const statusText = state.statusText ?? null;
  const turnDismissed = state.turnDismissed ?? false;
  const isReset = state.isReset ?? false;

  const isActive = !isReset && activeState !== 'idle' && activeState !== 'error';

  if (!isActive) {
    return IDLE_DERIVED_PROPS;
  }

  let stateClass = '';
  let iconKind: VoiceIconKind = 'mic';
  let label = '';
  let showPulse = false;
  let showProgress = false;
  let transcriptText: string | null = null;

  switch (activeState) {
    case 'listening':
      stateClass = 'listening';
      iconKind = 'mic';
      label = 'Listening...';
      showPulse = true;
      break;
    case 'transcribing':
      stateClass = 'transcribing';
      iconKind = 'mic';
      label = 'Transcribing...';
      if (transcript) {
        transcriptText = `"${transcript}"`;
      }
      break;
    case 'thinking':
      stateClass = 'thinking';
      iconKind = 'think';
      label = 'Thinking...';
      if (transcript) {
        transcriptText = `"${transcript}"`;
      }
      break;
    case 'synthesizing':
      stateClass = 'thinking';
      iconKind = 'think';
      label = 'Synthesizing...';
      if (transcript) {
        transcriptText = `"${transcript}"`;
      }
      break;
    case 'speaking':
      stateClass = 'speaking';
      iconKind = 'sparkle';
      label = 'Speaking...';
      showProgress = true;
      if (reply) {
        transcriptText = `"${reply}"`;
      } else if (transcript) {
        transcriptText = `"${transcript}"`;
      }
      break;
  }

  const toastVisible =
    isActive && activeState === 'speaking' && Boolean(reply) && !turnDismissed;
  const toastBadgeText = toastVisible && ttsEngine ? ttsEngine.toLowerCase() : null;
  const toastBodyText = toastVisible && reply ? reply : null;

  const headerVoice: HeaderVoiceViewProps = {
    isActive,
    stateClass,
    iconKind,
    label,
    statusText: isActive ? statusText : null,
    transcriptText,
    showDot: false,
    showPulse,
    showProgress,
  };

  const voiceToast: VoiceToastViewProps = {
    visible: toastVisible,
    stateClass: toastVisible ? 'visible' : '',
    badgeText: toastBadgeText,
    bodyText: toastBodyText,
  };

  return { headerVoice, voiceToast };
}

interface VoiceLocalState {
  turnDismissed: boolean;
  statusText: string | null;
  prevVoice: VoiceSlice | null;
  resetForVoice: VoiceSlice | null;
}

let currentLocal: VoiceLocalState = {
  turnDismissed: false,
  statusText: null,
  prevVoice: null,
  resetForVoice: null,
};

const localListeners = new Set<() => void>();
const subscribeLocal = (l: () => void) => {
  localListeners.add(l);
  return () => {
    localListeners.delete(l);
  };
};
const getLocalSnapshot = (): VoiceLocalState => currentLocal;

function computeNextLocal(prevLocal: VoiceLocalState, voice: VoiceSlice): VoiceLocalState {
  if (voice === prevLocal.prevVoice) {
    return prevLocal;
  }
  const rawState = voice?.state ?? 'idle';
  const normalizedStatus =
    voice?.status && voice.status.trim().length > 0 ? voice.status : null;
  let nextDismissed = prevLocal.turnDismissed;
  let nextStatus = prevLocal.statusText;

  if (rawState === 'idle' || rawState === 'error') {
    nextDismissed = false;
    nextStatus = null;
  } else if (rawState === 'listening') {
    nextDismissed = false;
    nextStatus = normalizedStatus;
  } else if (rawState === 'thinking' || rawState === 'transcribing') {
    if (normalizedStatus !== null) {
      nextStatus = normalizedStatus;
    }
  } else {
    nextStatus = normalizedStatus;
  }

  return {
    turnDismissed: nextDismissed,
    statusText: nextStatus,
    prevVoice: voice,
    resetForVoice: null,
  };
}

export function useVoiceHudState(): VoiceHudSnapshot {
  const voice = useDisplayStore((s) => s.voice);

  useLayoutEffect(() => {
    if (voice !== currentLocal.prevVoice) {
      currentLocal = computeNextLocal(currentLocal, voice);
      for (const listener of [...localListeners]) listener();
    }
  }, [voice]);

  const localState = useSyncExternalStore(subscribeLocal, getLocalSnapshot, getLocalSnapshot);
  const effectiveLocal = voice !== localState.prevVoice ? computeNextLocal(localState, voice) : localState;

  const dismissToast = () => {
    currentLocal = { ...currentLocal, turnDismissed: true };
    for (const listener of [...localListeners]) listener();
  };

  const reset = () => {
    resetHUD();
  };

  const isReset = effectiveLocal.resetForVoice !== null && effectiveLocal.resetForVoice === voice;
  const rawActiveState = (voice?.state ?? 'idle') as VoiceState;
  const activeState: VoiceState = isReset ? 'idle' : rawActiveState;
  const effectiveStatus = isReset ? null : effectiveLocal.statusText;
  const effectiveDismissed = effectiveLocal.turnDismissed;
  const transcript = voice?.transcript ?? null;
  const reply = voice?.reply ?? null;
  const ttsEngine = voice?.tts_engine ?? null;

  const { headerVoice, voiceToast } = deriveVoiceHudProps({
    activeState,
    transcript,
    reply,
    ttsEngine,
    statusText: effectiveStatus,
    turnDismissed: effectiveDismissed,
    isReset,
  });

  return {
    ...headerVoice,
    headerVoice,
    voiceToast,
    dismissToast,
    resetHUD: reset,
    reset,
    voice,
    activeState,
    statusText: effectiveStatus,
    turnDismissed: effectiveDismissed,
  };
}

declare global {
  interface Window {
    audioManager?: AudioController;
    MirrormereVoice?: {
      resetHUD: () => void;
    };
  }
}
