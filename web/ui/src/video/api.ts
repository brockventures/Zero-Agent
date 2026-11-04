import type { VideoSource } from '../sse/types';

export interface VideoActionPayload {
  id: string;
  action: string;
  value?: unknown;
}

export interface VideoDismissPayload {
  id?: string | null;
}

export interface AudioMutePayload {
  muted: boolean;
}

export interface AudioVolumePayload {
  volume: number;
}

export type TouchAction = 'down' | 'move' | 'up' | 'tap';

function getFetch(customFetch?: typeof fetch): typeof fetch | null {
  if (customFetch) return customFetch;
  if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
    return window.fetch.bind(window);
  }
  if (typeof fetch === 'function') {
    return fetch;
  }
  return null;
}

function normalizePart(val: unknown, prefix: string): string {
  if (val === undefined || val === null) return '';
  const s = String(val).trim();
  if (!s) return '';
  return s.toUpperCase().startsWith(prefix) ? s.toUpperCase() : `${prefix}${s}`;
}

function resolveAnnotation(stream: VideoSource, record: Record<string, unknown>): string {
  const explicit = String(record.annotation || '').trim();
  if (explicit) return explicit;

  const rawSeason = stream.season !== undefined && stream.season !== null ? stream.season : record.season_number;
  const rawEp = stream.episode !== undefined && stream.episode !== null ? stream.episode : record.episode_number;

  const s = normalizePart(rawSeason, 'S');
  const ep = normalizePart(rawEp, 'E');
  return `${s}${ep}`;
}

function buildTitleString(
  series: string,
  annotation: string,
  title: string,
  fallback: string,
): string {
  if (series && annotation && title) return `${series} ${annotation}: ${title}`;
  if (series && title) return `${series}: ${title}`;
  if (series && annotation) return `${series} ${annotation}`;
  if (annotation && title) return `${annotation}: ${title}`;
  if (title) return title;
  if (series) return series;
  return fallback;
}

/**
 * Format stream presentation title: "<Series Name> <season + episode annotation>: <episode name>"
 * with graceful fallbacks.
 */
export function formatStreamTitle(stream?: VideoSource | null): string {
  if (!stream) return 'Live Video';

  const streamRecord = stream as unknown as Record<string, unknown>;
  const series = String(
    stream.series || streamRecord.series_title || streamRecord.show_title || '',
  ).trim();
  const title = String(stream.title || streamRecord.episode_name || '').trim();
  const annotation = resolveAnnotation(stream, streamRecord);
  const fallback = stream.id ? `Stream: ${stream.id}` : 'Live Video';

  return buildTitleString(series, annotation, title, fallback);
}

/**
 * Post transport action to video API.
 */
export async function postVideoAction(
  body: VideoActionPayload,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn) return;
  try {
    return await fetchFn('api/video/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.warn('[MirrormereHUD] Action failed:', err);
  }
}

/**
 * Post remote key action to remote sidecar URL.
 */
export async function postRemoteKey(
  remoteUrl: string,
  key: string,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn || !remoteUrl) return;
  try {
    return await fetchFn(`${remoteUrl}/key`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
  } catch (err) {
    console.warn('[MirrormereHUD] Remote key failed:', err);
  }
}

/**
 * Post video dismiss action to video API.
 */
export async function postVideoDismiss(
  body: VideoDismissPayload,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn) return;
  try {
    return await fetchFn('api/video/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.warn('[MirrormereHUD] Dismiss failed:', err);
  }
}

/**
 * Post audio mute toggle to audio API.
 */
export async function postAudioMute(
  body: AudioMutePayload,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn) return;
  try {
    return await fetchFn('api/audio/mute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.warn('[MirrormereHUD] Mute toggle failed:', err);
  }
}

/**
 * Post audio volume update to audio API.
 */
export async function postAudioVolume(
  body: AudioVolumePayload,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn) return;
  try {
    return await fetchFn('api/audio/volume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.warn('[MirrormereHUD] Volume update failed:', err);
  }
}

/**
 * Post touch action to remote sidecar URL.
 */
export async function postTouchEvent(
  remoteUrl: string,
  action: TouchAction,
  x: number,
  y: number,
  customFetch?: typeof fetch,
): Promise<unknown> {
  const fetchFn = getFetch(customFetch);
  if (!fetchFn || !remoteUrl) return;
  const payload = { action, x, y };
  try {
    return await fetchFn(`${remoteUrl}/touch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true,
    });
  } catch {
    if (action === 'tap') {
      try {
        return await fetchFn(`${remoteUrl}/tap`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ x, y }),
          keepalive: true,
        });
      } catch {
        // ignore
      }
    }
  }
}
