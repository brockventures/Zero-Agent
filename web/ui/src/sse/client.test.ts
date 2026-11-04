import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventStreamClient } from './client';
import { FakeEventSource } from './fake-event-source';

function makeClient(options: ConstructorParameters<typeof EventStreamClient>[1] = {}) {
  return new EventStreamClient('api/events', {
    jitterMs: 0,
    createEventSource: (url) => new FakeEventSource(url),
    ...options,
  });
}

describe('EventStreamClient', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.reset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  describe('backoff', () => {
    it('follows 1 s x 1.5^n and caps at 30 s', () => {
      const client = makeClient();
      const expected = [1000, 1500, 2250, 3375, 5062.5, 7593.75, 11390.625, 17085.9375, 25628.90625, 30000, 30000];
      expected.forEach((ms, attempt) => {
        expect(client.nextDelayMs(attempt)).toBeCloseTo(ms, 6);
      });
    });

    it('adds jitter on top of the capped delay', () => {
      const client = makeClient({ jitterMs: 500, random: () => 0.5 });
      expect(client.nextDelayMs(0)).toBe(1250);
      expect(client.nextDelayMs(20)).toBe(30250);
    });

    it('reconnects on the schedule after repeated errors and resets after open', () => {
      const client = makeClient();
      client.connect();
      const first = FakeEventSource.instances[0];

      first.error();
      expect(first.closed).toBe(true);
      vi.advanceTimersByTime(999);
      expect(FakeEventSource.instances).toHaveLength(1);
      vi.advanceTimersByTime(1);
      expect(FakeEventSource.instances).toHaveLength(2);

      FakeEventSource.instances[1].error();
      vi.advanceTimersByTime(1499);
      expect(FakeEventSource.instances).toHaveLength(2);
      vi.advanceTimersByTime(1);
      expect(FakeEventSource.instances).toHaveLength(3);

      FakeEventSource.instances[2].open();
      FakeEventSource.instances[2].error();
      vi.advanceTimersByTime(1000);
      expect(FakeEventSource.instances).toHaveLength(4);
    });
  });

  describe('resume', () => {
    it('re-sends the last event id as a query parameter', () => {
      const client = makeClient();
      client.connect();
      FakeEventSource.instances[0].message('widget.update', { widget_id: 'w' }, 'evt_42');
      FakeEventSource.instances[0].error();

      vi.advanceTimersByTime(1000);
      expect(FakeEventSource.instances[1].url).toBe('api/events?lastEventId=evt_42');
      expect(client.getLastEventId()).toBe('evt_42');
    });

    it('appends to an existing query string', () => {
      const client = makeClient({ lastEventId: 'evt_7', createEventSource: (url) => new FakeEventSource(url) });
      const withQuery = new EventStreamClient('api/events?node=kitchen', { lastEventId: 'evt_7' });
      expect(withQuery.getConnectionURL()).toBe('api/events?node=kitchen&lastEventId=evt_7');
      expect(client.getConnectionURL()).toBe('api/events?lastEventId=evt_7');
    });

    it('preserves hash fragments with query and path URLs', () => {
      const withHash = new EventStreamClient('api/events#section1', { lastEventId: 'evt_99' });
      expect(withHash.getConnectionURL()).toBe('api/events?lastEventId=evt_99#section1');

      const withQueryAndHash = new EventStreamClient('api/events?filter=all#summary', {
        lastEventId: 'evt_99',
      });
      expect(withQueryAndHash.getConnectionURL()).toBe(
        'api/events?filter=all&lastEventId=evt_99#summary',
      );
    });

    it('allows mutating lastEventId via setLastEventId with whitespace normalization', () => {
      const client = makeClient();
      client.setLastEventId('evt_123');
      expect(client.getLastEventId()).toBe('evt_123');

      client.setLastEventId('  evt_trimmed  ');
      expect(client.getLastEventId()).toBe('evt_trimmed');

      client.setLastEventId('   ');
      expect(client.getLastEventId()).toBeNull();

      client.setLastEventId(null);
      expect(client.getLastEventId()).toBeNull();
    });

    it('omits the parameter when no event id is known', () => {
      const client = makeClient();
      client.connect();
      expect(FakeEventSource.instances[0].url).toBe('api/events');
    });

    it('reset forgets the resume point', () => {
      const client = makeClient({ lastEventId: 'evt_1' });
      client.reset();
      expect(client.getLastEventId()).toBeNull();
    });
  });

  describe('connection events', () => {
    it('emits a synthetic connection event on open and on error', () => {
      const client = makeClient();
      const seen: unknown[] = [];
      client.on('connection', (payload) => seen.push(payload));
      client.connect();

      FakeEventSource.instances[0].open();
      FakeEventSource.instances[0].error();

      expect(seen).toEqual([
        { status: 'connected', lastEventId: null },
        expect.objectContaining({ status: 'disconnected', lastEventId: null }),
      ]);
    });

    it('does not reconnect after an explicit disconnect', () => {
      const client = makeClient();
      client.connect();
      client.disconnect();
      expect(FakeEventSource.instances[0].closed).toBe(true);

      vi.advanceTimersByTime(60_000);
      expect(FakeEventSource.instances).toHaveLength(1);
    });

    it('closes the previous source when connect is called again', () => {
      const client = makeClient();
      client.connect();
      client.connect();
      expect(FakeEventSource.instances[0].closed).toBe(true);
      expect(FakeEventSource.instances[1].closed).toBe(false);
    });
  });

  describe('listeners', () => {
    it('parses JSON payloads and supports unsubscribe', () => {
      const client = makeClient();
      const spy = vi.fn();
      const off = client.on('header.update', spy);
      client.connect();

      FakeEventSource.instances[0].message('header.update', { timestamp: 't', timezone: 'UTC' });
      off();
      FakeEventSource.instances[0].message('header.update', { timestamp: 't2', timezone: 'UTC' });

      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith({ timestamp: 't', timezone: 'UTC' });
    });

    it('isolates a throwing listener from the others', () => {
      const client = makeClient();
      vi.spyOn(console, 'error').mockImplementation(() => {});
      const good = vi.fn();
      client.on('theme.update', () => {
        throw new Error('boom');
      });
      client.on('theme.update', good);
      client.connect();

      FakeEventSource.instances[0].message('theme.update', { theme: 'night' });
      expect(good).toHaveBeenCalledWith({ theme: 'night' });
    });

    it('attaches listeners registered after connect', () => {
      const client = makeClient();
      client.connect();
      const spy = vi.fn();
      client.on('voice.state', spy);

      FakeEventSource.instances[0].message('voice.state', { state: 'listening' });
      expect(spy).toHaveBeenCalledWith({ state: 'listening' });
    });

    it('falls back to raw non-JSON text payloads in parsePayload', () => {
      const client = makeClient();
      const spy = vi.fn();
      client.on('system.reload', spy);
      client.connect();

      const source = FakeEventSource.instances[0];
      const listenersMap = (source as unknown as { listeners: Map<string, Array<(ev: MessageEvent) => void>> }).listeners;
      const registered = listenersMap.get('system.reload') ?? [];
      for (const listener of registered) {
        listener({ data: 'not-valid-json-string', lastEventId: '' } as MessageEvent);
      }

      expect(spy).toHaveBeenCalledWith('not-valid-json-string');
    });
  });

  describe('defaultFactory and error handling on connect', () => {
    it('uses defaultFactory which throws when EventSource is not in environment', () => {
      const originalEventSource = globalThis.EventSource;
      // @ts-expect-error test undefined environment
      delete globalThis.EventSource;

      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
      const client = new EventStreamClient('api/events', { jitterMs: 0 });

      client.connect();
      expect(consoleSpy).toHaveBeenCalledWith(
        '[EventStreamClient] Failed to initialize EventSource:',
        expect.any(Error),
      );

      // Reconnect was scheduled
      vi.advanceTimersByTime(1000);
      expect(consoleSpy).toHaveBeenCalledTimes(2);

      client.disconnect();
      globalThis.EventSource = originalEventSource;
    });

    it('uses defaultFactory when globalThis.EventSource is present', () => {
      const originalEventSource = globalThis.EventSource;
      class FakeGlobalEventSource extends FakeEventSource {}
      globalThis.EventSource = FakeGlobalEventSource as unknown as typeof EventSource;

      const client = new EventStreamClient('api/events');
      client.connect();
      expect(FakeEventSource.instances).toHaveLength(1);

      client.disconnect();
      globalThis.EventSource = originalEventSource;
    });
  });
});
