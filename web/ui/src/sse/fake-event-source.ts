import type { EventSourceLike } from './client';

/** Test double for EventSource: records instances and lets tests drive open, error and messages. */
export class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];

  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, Array<(ev: MessageEvent) => void>>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  static reset(): void {
    FakeEventSource.instances = [];
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.(new Event('open'));
  }

  error(): void {
    this.onerror?.(new Event('error'));
  }

  message(type: string, payload: unknown, lastEventId = ''): void {
    const event = { data: JSON.stringify(payload), lastEventId } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}
