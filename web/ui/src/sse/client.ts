import type { ConnectionPayload, ServerEventName, StreamEventMap, StreamEventName } from './types';

/**
 * TypeScript port of web/static/js/sse.js (MirrormereSSE). Same backoff
 * (base * factor^n, capped, plus jitter), lastEventId re-sent as a query
 * parameter, and synthetic `connection` events on open and error.
 */

/** Every server-sent event name. Listeners are attached to all of these on connect. */
export const SERVER_EVENT_NAMES = [
  'screen.rotate',
  'widget.update',
  'widget.reload',
  'style.reload',
  'header.update',
  'system.status',
  'system.reload',
  'video.state',
  'audio.state',
  'voice.state',
  'theme.update',
] as const satisfies readonly ServerEventName[];

export interface EventSourceLike {
  onopen: ((ev: Event) => void) | null;
  onerror: ((ev: Event) => void) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface EventStreamOptions {
  baseDelayMs?: number;
  maxDelayMs?: number;
  backoffFactor?: number;
  jitterMs?: number;
  /** Resume point; sent as `lastEventId` on the first connect. */
  lastEventId?: string | null;
  createEventSource?: EventSourceFactory;
  random?: () => number;
}

type Listener = (data: unknown) => void;

const defaultFactory: EventSourceFactory = (url) => {
  if (typeof EventSource === 'undefined') {
    throw new Error('EventSource is not available in this environment');
  }
  return new EventSource(url);
};

export class EventStreamClient {
  readonly url: string;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly backoffFactor: number;
  private readonly jitterMs: number;
  private readonly createEventSource: EventSourceFactory;
  private readonly random: () => number;

  private source: EventSourceLike | null = null;
  private attachedNames = new Set<string>();
  private readonly listeners = new Map<string, Set<Listener>>();
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private explicitDisconnect = false;
  private lastEventId: string | null;

  constructor(url: string, options: EventStreamOptions = {}) {
    this.url = url;
    this.baseDelayMs = options.baseDelayMs ?? 1000;
    this.maxDelayMs = options.maxDelayMs ?? 30000;
    this.backoffFactor = options.backoffFactor ?? 1.5;
    this.jitterMs = options.jitterMs ?? 500;
    this.createEventSource = options.createEventSource ?? defaultFactory;
    this.random = options.random ?? Math.random;
    this.lastEventId = normalizeId(options.lastEventId ?? null);
  }

  /** Subscribe to one event type. Returns an unsubscribe function. */
  on<K extends StreamEventName>(type: K, callback: (data: StreamEventMap[K]) => void): () => void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    const listener = callback as Listener;
    set.add(listener);
    if (this.source) this.attach(this.source, type);
    return () => {
      this.listeners.get(type)?.delete(listener);
    };
  }

  /** URL used for the next connection, carrying lastEventId when known. */
  getConnectionURL(): string {
    if (!this.lastEventId) return this.url;
    const [pathAndQuery, hash] = this.url.split('#');
    const sep = pathAndQuery.includes('?') ? '&' : '?';
    const suffix = hash === undefined ? '' : `#${hash}`;
    return `${pathAndQuery}${sep}lastEventId=${encodeURIComponent(this.lastEventId)}${suffix}`;
  }

  getLastEventId(): string | null {
    return this.lastEventId;
  }

  setLastEventId(id: string | null): void {
    this.lastEventId = normalizeId(id);
  }

  connect(): void {
    this.explicitDisconnect = false;
    this.closeSource();
    this.clearReconnectTimer();

    let source: EventSourceLike;
    try {
      source = this.createEventSource(this.getConnectionURL());
    } catch (err) {
      console.error('[EventStreamClient] Failed to initialize EventSource:', err);
      this.scheduleReconnect();
      return;
    }
    this.source = source;
    this.attachedNames = new Set();

    source.onopen = () => {
      this.attempts = 0;
      this.emitConnection({ status: 'connected', lastEventId: this.lastEventId });
    };
    source.onerror = (err) => {
      this.emitConnection({ status: 'disconnected', error: err, lastEventId: this.lastEventId });
      if (!this.explicitDisconnect) this.scheduleReconnect();
    };

    for (const name of SERVER_EVENT_NAMES) this.attach(source, name);
    for (const name of this.listeners.keys()) this.attach(source, name);
  }

  disconnect(): void {
    this.explicitDisconnect = true;
    this.clearReconnectTimer();
    this.closeSource();
    this.emitConnection({ status: 'disconnected', lastEventId: this.lastEventId });
  }

  /** Disconnect and forget the resume point and backoff state. */
  reset(): void {
    this.disconnect();
    this.lastEventId = null;
    this.attempts = 0;
  }

  /** Delay before reconnect attempt n (0-based): min(max, base * factor^n) + jitter. */
  nextDelayMs(attempt: number): number {
    const jitter = this.jitterMs > 0 ? this.random() * this.jitterMs : 0;
    return Math.min(this.maxDelayMs, this.baseDelayMs * Math.pow(this.backoffFactor, attempt)) + jitter;
  }

  private scheduleReconnect(): void {
    this.closeSource();
    this.clearReconnectTimer();
    const delay = this.nextDelayMs(this.attempts);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.explicitDisconnect) this.connect();
    }, delay);
  }

  private attach(source: EventSourceLike, name: string): void {
    if (name === 'connection' || this.attachedNames.has(name)) return;
    if (source !== this.source) return;
    this.attachedNames.add(name);
    source.addEventListener(name, (ev) => {
      if (typeof ev.lastEventId === 'string' && ev.lastEventId.trim() !== '') {
        this.lastEventId = ev.lastEventId.trim();
      }
      this.emit(name, parsePayload(ev.data));
    });
  }

  private emit(name: string, data: unknown): void {
    const set = this.listeners.get(name);
    if (!set) return;
    for (const listener of [...set]) {
      try {
        listener(data);
      } catch (err) {
        console.error(`[EventStreamClient] Listener error for '${name}':`, err);
      }
    }
  }

  private emitConnection(payload: ConnectionPayload): void {
    this.emit('connection', payload);
  }

  private closeSource(): void {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

function normalizeId(id: string | null): string | null {
  if (typeof id !== 'string') return null;
  const trimmed = id.trim();
  return trimmed === '' ? null : trimmed;
}

function parsePayload(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}
