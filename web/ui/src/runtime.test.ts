import { act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { startRuntime, type Runtime } from './runtime';
import { FakeEventSource } from './sse/fake-event-source';
import { useDisplayStore } from './store';

const initial = useDisplayStore.getState();

describe('startRuntime', () => {
  let runtime: Runtime | null = null;

  beforeEach(() => {
    FakeEventSource.reset();
    useDisplayStore.setState(initial, true);
    vi.spyOn(console, 'info').mockImplementation(() => {});
  });

  afterEach(() => {
    runtime?.dispose();
    runtime = null;
    vi.restoreAllMocks();
  });

  it('opens exactly one EventSource, even when called twice', () => {
    runtime = startRuntime({ createEventSource: (url) => new FakeEventSource(url) });
    const again = startRuntime({ createEventSource: (url) => new FakeEventSource(url) });

    expect(again).toBe(runtime);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe('api/events');
  });

  it('marks the connection as connected on open and fills the store from the hydration batch', () => {
    runtime = startRuntime({ createEventSource: (url) => new FakeEventSource(url) });
    const source = FakeEventSource.instances[0];

    act(() => {
      source.open();
      source.message('header.update', { timestamp: 't', timezone: 'America/New_York' }, 'evt_1');
      source.message('widget.update', { widget_id: 'w1', timestamp: 't', state: 'healthy', data: { a: 1 } }, 'evt_2');
      source.message('theme.update', { theme: 'night' }, 'evt_3');
    });

    const state = useDisplayStore.getState();
    expect(state.system.connection).toBe('connected');
    expect(state.header.timezone).toBe('America/New_York');
    expect(state.widgets.w1.data).toEqual({ a: 1 });
    expect(state.theme.theme).toBe('night');
  });

  it('reloads the page when the server boot_id changes', () => {
    const reload = vi.fn();
    runtime = startRuntime({ createEventSource: (url) => new FakeEventSource(url), reload });
    const source = FakeEventSource.instances[0];
    const status = (boot_id: string) =>
      source.message('system.status', { online: true, time: 't', config_status: 'ok', config_error: null, boot_id });

    status('a');
    expect(reload).not.toHaveBeenCalled();
    status('b');
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('dispose closes the stream and stops store updates', () => {
    runtime = startRuntime({ createEventSource: (url) => new FakeEventSource(url) });
    const source = FakeEventSource.instances[0];
    const rt = runtime;
    rt.dispose();
    runtime = null;

    expect(source.closed).toBe(true);
    source.message('theme.update', { theme: 'night' });
    expect(useDisplayStore.getState().theme.theme).toBe('day');
  });
});
