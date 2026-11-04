import { useEffect, useState } from 'react';

export interface Speaker {
  entity_id: string;
  name: string;
}

export interface AccountProfile {
  id: string;
  name: string;
  handle?: string;
  type: 'email' | 'brand';
}

export interface SuggestedItem {
  id: string;
  title: string;
  subtitle?: string;
  kind: 'artist' | 'album';
  thumb?: string;
  video_id?: string;
  playlist_id?: string;
}

export interface NowPlaying {
  title?: string;
  artist?: string;
  thumb?: string;
  state?: string;
  volume?: number;
  queue_index?: number;
  video_id?: string;
}

export interface Playlist {
  playlist_id: string;
  title: string;
  thumb?: string;
  count?: number;
}

export interface QueueItem {
  title: string;
  artist: string;
  thumb?: string;
  id?: string;
  video_id?: string;
}

export interface SearchResultItem {
  video_id: string;
  title: string;
  artist: string;
  thumb?: string;
  duration?: string;
}

export interface MusicState {
  active_speaker?: string;
  speakers?: Speaker[];
  now_playing?: NowPlaying;
  playlists?: Playlist[];
  queue?: QueueItem[];
  accounts?: AccountProfile[];
  active_account?: string;
  suggestions?: SuggestedItem[];
}

export type YtMusicTab = 'discovery' | 'playlists' | 'queue' | 'search';

export const DEFAULT_ART = 'https://brands.home-assistant.io/_/ytube_music_player/icon.png';

export const LOCAL_SPEAKER: Speaker = {
  entity_id: 'this_device',
  name: 'This Device',
};

export const DEFAULT_ACCOUNTS: AccountProfile[] = [
  { id: 'primary', name: 'Personal Profile', handle: '@personal', type: 'email' },
  { id: 'brand', name: 'Brand Channel', handle: '@brand', type: 'brand' },
];

export const DEFAULT_SUGGESTIONS: SuggestedItem[] = [
  {
    id: 'sug-supermix',
    title: 'My Supermix',
    subtitle: 'Endless personalized mix',
    kind: 'album',
    playlist_id: 'RDTMAK5uy_ksetItRFlDrQI5PqO6aoUZgleGC9iAYjk',
    thumb: DEFAULT_ART,
  },
  {
    id: 'sug-discover',
    title: 'Discover Mix',
    subtitle: 'Fresh recommendations',
    kind: 'album',
    playlist_id: 'RDTMAK5uy_m-d_fG7e3v8P3i4c7Z4Y0_5c7n-t1x1u0',
    thumb: DEFAULT_ART,
  },
  {
    id: 'sug-chill',
    title: 'Chillout Lounge',
    subtitle: 'Downtempo & lofi beats',
    kind: 'artist',
    thumb: DEFAULT_ART,
  },
  {
    id: 'sug-focus',
    title: 'Deep Focus',
    subtitle: 'Instrumental & ambient',
    kind: 'artist',
    thumb: DEFAULT_ART,
  },
];

export const DEFAULT_STATE: MusicState = {
  active_speaker: 'media_player.home_group',
  speakers: [],
  now_playing: {},
  playlists: [],
  queue: [],
  accounts: DEFAULT_ACCOUNTS,
  active_account: 'primary',
  suggestions: DEFAULT_SUGGESTIONS,
};

declare global {
  interface Window {
    onEmberAccountsUpdated?: (payload?: unknown) => void;
  }
}

/**
 * Safely retrieves Google / Android accounts from EmberBridge if available.
 */
function getEmberAccounts(): unknown[] {
  try {
    if (typeof window !== 'undefined') {
      const bridge = (window as unknown as { EmberBridge?: { getGoogleAccounts?: () => unknown } }).EmberBridge;
      if (typeof bridge?.getGoogleAccounts === 'function') {
        const res = bridge.getGoogleAccounts();
        if (typeof res === 'string') {
          const parsed = JSON.parse(res) as unknown;
          return Array.isArray(parsed) ? parsed : [];
        }
        if (Array.isArray(res)) {
          return res;
        }
      }
    }
  } catch {
    // preserve empty accounts on bridge error
  }
  return [];
}

/**
 * React hook that manages Android OS / Google accounts from EmberBridge and listens
 * for updates via window.onEmberAccountsUpdated.
 */
export function useEmberAccounts(): unknown[] {
  'use no memo';
  const [emberAccounts, setEmberAccounts] = useState<unknown[]>([]);

  useEffect(() => {
    const readAccounts = (payload?: unknown) => {
      if (Array.isArray(payload)) {
        setEmberAccounts(payload);
        return;
      }
      if (typeof payload === 'string') {
        try {
          const parsed = JSON.parse(payload) as unknown;
          if (Array.isArray(parsed)) {
            setEmberAccounts(parsed);
            return;
          }
        } catch {
          // ignore parse error
        }
      }
      setEmberAccounts(getEmberAccounts());
    };

    readAccounts();

    const prevListener = window.onEmberAccountsUpdated;
    window.onEmberAccountsUpdated = (payload?: unknown) => {
      readAccounts(payload);
      if (typeof prevListener === 'function') {
        prevListener(payload);
      }
    };

    return () => {
      window.onEmberAccountsUpdated = prevListener;
    };
  }, []);

  return emberAccounts;
}

/**
 * React hook that polls the music state at interval when active.
 */
export function useMusicStatePolling(
  apiBase: string,
  isActive: boolean,
  activeAccountId: string | undefined,
): [MusicState, React.Dispatch<React.SetStateAction<MusicState>>] {
  'use no memo';
  const [stateData, setStateData] = useState<MusicState>(DEFAULT_STATE);

  useEffect(() => {
    if (!isActive) return;
    let cancelled = false;

    const refreshState = async () => {
      try {
        const param = activeAccountId ? `?account_id=${encodeURIComponent(activeAccountId)}` : '';
        const r = await fetch(`${apiBase}/music/state${param}`);
        if (!r.ok) return;
        const data = (await r.json()) as MusicState;
        if (!cancelled && data && typeof data === 'object') {
          setStateData((prev) => (JSON.stringify(prev) === JSON.stringify(data) ? prev : data));
        }
      } catch {
        // preserve current state on network error
      }
    };

    void refreshState();

    const intervalId = window.setInterval(() => {
      void refreshState();
    }, 8000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [isActive, apiBase, activeAccountId]);

  return [stateData, setStateData];
}

/**
 * Helper to post music control/playback commands to API and parse returned state.
 */
export async function postMusicApi(
  url: string,
  body: Record<string, unknown>,
): Promise<MusicState | undefined> {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const res = (await r.json().catch(() => ({}))) as { state?: MusicState };
    return res?.state;
  } catch {
    return undefined;
  }
}

/**
 * Resolves available YouTube profiles/accounts.
 * Prioritizes stateData.accounts, then config.accounts, then falls back to default profiles.
 * Optionally appends normalized extra accounts (e.g. from Android OS EmberBridge).
 */
export function resolveAccounts(
  stateData?: MusicState | null,
  config?: Record<string, unknown> | null,
  extraAccounts?: unknown[] | null,
): AccountProfile[] {
  let base: AccountProfile[];
  if (Array.isArray(stateData?.accounts) && stateData.accounts.length > 0) {
    base = [...stateData.accounts];
  } else if (Array.isArray(config?.accounts) && config.accounts.length > 0) {
    base = (config.accounts as unknown[]).map((acc, idx) => {
      if (typeof acc === 'object' && acc !== null) {
        const a = acc as Record<string, unknown>;
        return {
          id: String(a.id ?? `acc_${idx}`),
          name: String(a.name ?? a.id ?? `Account ${idx + 1}`),
          handle: a.handle ? String(a.handle) : undefined,
          type: (a.type === 'brand' ? 'brand' : 'email') as 'email' | 'brand',
        };
      }
      return {
        id: String(acc),
        name: String(acc),
        type: 'email' as const,
      };
    });
  } else {
    base = [...DEFAULT_ACCOUNTS];
  }

  if (Array.isArray(extraAccounts) && extraAccounts.length > 0) {
    for (let i = 0; i < extraAccounts.length; i++) {
      const item = extraAccounts[i];
      let profile: AccountProfile;
      if (typeof item === 'string') {
        const trimmed = item.trim();
        if (!trimmed) continue;
        profile = {
          id: trimmed,
          name: trimmed,
          handle: trimmed.includes('@') ? trimmed : undefined,
          type: 'email',
        };
      } else if (typeof item === 'object' && item !== null) {
        const a = item as Record<string, unknown>;
        const id = String(a.id ?? a.email ?? `extra_${i}`);
        profile = {
          id,
          name: String(a.name ?? a.email ?? id),
          handle: a.handle ? String(a.handle) : (a.email ? String(a.email) : undefined),
          type: (a.type === 'brand' ? 'brand' : 'email') as 'email' | 'brand',
        };
      } else {
        continue;
      }
      if (!base.some((a) => a.id === profile.id)) {
        base.push(profile);
      }
    }
  }

  return base;
}

/**
 * Ensures 'This Device' local speaker is always available alongside Google Home speaker groups.
 */
export function resolveSpeakersWithLocal(stateData?: MusicState | null): Speaker[] {
  const speakers = Array.isArray(stateData?.speakers) ? [...stateData.speakers] : [];
  const hasLocal = speakers.some(
    (s) =>
      s.entity_id === 'this_device' ||
      s.entity_id === 'local' ||
      s.name.trim().toLowerCase() === 'this device',
  );
  if (!hasLocal) {
    return [LOCAL_SPEAKER, ...speakers];
  }
  return speakers;
}

/**
 * Resolves artist and album suggestions, deriving them from queue/playlists or defaults
 * when backend suggestions array is empty.
 */
export function resolveSuggestions(stateData?: MusicState | null): SuggestedItem[] {
  if (Array.isArray(stateData?.suggestions) && stateData.suggestions.length > 0) {
    return stateData.suggestions;
  }

  const derived: SuggestedItem[] = [];
  const seenArtists = new Set<string>();

  if (Array.isArray(stateData?.queue)) {
    for (const item of stateData.queue) {
      const artist = item.artist?.trim();
      if (artist && !seenArtists.has(artist.toLowerCase())) {
        seenArtists.add(artist.toLowerCase());
        derived.push({
          id: `sug-artist-${derived.length}`,
          title: artist,
          subtitle: 'Artist Radio',
          kind: 'artist',
          thumb: item.thumb,
          video_id: item.video_id,
        });
        if (derived.length >= 3) break;
      }
    }
  }

  if (Array.isArray(stateData?.playlists)) {
    for (const p of stateData.playlists) {
      derived.push({
        id: `sug-pl-${p.playlist_id}`,
        title: p.title,
        subtitle: typeof p.count === 'number' ? `${p.count} tracks` : 'Playlist',
        kind: 'album',
        thumb: p.thumb,
        playlist_id: p.playlist_id,
      });
      if (derived.length >= 6) break;
    }
  }

  if (derived.length > 0) {
    return derived;
  }

  return DEFAULT_SUGGESTIONS;
}
