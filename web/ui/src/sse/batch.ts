import type { ServerEventName, StreamEventMap } from './types';

/** A server event waiting to be applied to the store. */
export type BufferedEvent = {
  [K in ServerEventName]: { type: K; data: StreamEventMap[K] };
}[ServerEventName];

/**
 * Coalesces store-bound events so a burst (a reconnect's hydration batch, or
 * several updates in one network chunk) is applied in a single call.
 *
 * EventSource delivers each message as its own task, so a microtask would not
 * gather them. Events are flushed on the next macrotask instead. `theme.update`
 * is the last event of BuildHydrationBatch, so it flushes immediately and the
 * hydration batch is not held for the timer.
 */
export function createEventBatcher(
  apply: (events: BufferedEvent[]) => void,
  schedule: (flush: () => void) => void = (flush) => {
    setTimeout(flush, 0);
  },
) {
  let buffer: BufferedEvent[] = [];
  let scheduled = false;

  function flush(): void {
    scheduled = false;
    if (buffer.length === 0) return;
    const batch = buffer;
    buffer = [];
    apply(batch);
  }

  return {
    push(event: BufferedEvent): void {
      buffer.push(event);
      if (event.type === 'theme.update') {
        flush();
        return;
      }
      if (!scheduled) {
        scheduled = true;
        schedule(flush);
      }
    },
    flush,
  };
}
