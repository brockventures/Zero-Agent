import { beforeEach, describe, expect, it } from 'vitest';
import { reduceEvents, useDisplayStore } from './index';

describe('store reduceEvents voice.state equality and event isolation', () => {
  beforeEach(() => {
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

  it('preserves state.voice reference identity when receiving identical voice.state payload', () => {
    const currentState = useDisplayStore.getState();
    const identicalEvent = {
      type: 'voice.state' as const,
      data: {
        state: 'idle' as const,
        transcript: null,
        reply: null,
        tts_engine: null,
        status: null,
      },
    };

    const { patch } = reduceEvents(currentState, [identicalEvent]);
    expect(patch.voice).toBeUndefined();
  });

  it('preserves store.voice reference equality across identical applyEvents', () => {
    const beforeVoice = useDisplayStore.getState().voice;
    useDisplayStore.getState().applyEvents([
      {
        type: 'voice.state',
        data: {
          state: 'idle',
          transcript: null,
          reply: null,
          tts_engine: null,
          status: null,
        },
      },
    ]);
    const afterVoice = useDisplayStore.getState().voice;
    expect(afterVoice).toBe(beforeVoice);
  });

  it('updates state.voice when voice.state payload actually changes', () => {
    const currentState = useDisplayStore.getState();
    const changingEvent = {
      type: 'voice.state' as const,
      data: {
        state: 'listening' as const,
        transcript: 'Hello',
        reply: null,
        tts_engine: null,
        status: null,
      },
    };

    const { patch } = reduceEvents(currentState, [changingEvent]);
    expect(patch.voice).toBeDefined();
    expect(patch.voice?.state).toBe('listening');
    expect(patch.voice?.transcript).toBe('Hello');
  });

  it('produces new state.voice reference on active non-idle events to allow watchdog re-arming', () => {
    const currentState = useDisplayStore.getState();
    const activeEvent = {
      type: 'voice.state' as const,
      data: {
        state: 'thinking' as const,
        transcript: 'Status check',
        reply: null,
        tts_engine: null,
        status: '⚡ Running diagnostics...',
      },
    };

    const { patch } = reduceEvents(currentState, [activeEvent]);
    expect(patch.voice).toBeDefined();
    expect(patch.voice?.state).toBe('thinking');
  });

  it('clears patch.voice in a coalesced batch transitioning from idle to active and back to idle', () => {
    const currentState = useDisplayStore.getState();
    const batch = [
      {
        type: 'voice.state' as const,
        data: {
          state: 'listening' as const,
          transcript: null,
          reply: null,
          tts_engine: null,
          status: null,
        },
      },
      {
        type: 'voice.state' as const,
        data: {
          state: 'idle' as const,
          transcript: null,
          reply: null,
          tts_engine: null,
          status: null,
        },
      },
    ];

    const { patch } = reduceEvents(currentState, batch);
    expect(patch.voice).toBeUndefined();
  });
});
