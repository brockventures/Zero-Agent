import { describe, expect, it } from 'vitest';
import {
  DEFAULT_ACCOUNTS,
  DEFAULT_ART,
  DEFAULT_STATE,
  DEFAULT_SUGGESTIONS,
  LOCAL_SPEAKER,
  resolveAccounts,
  resolveSpeakersWithLocal,
  resolveSuggestions,
  type AccountProfile,
  type MusicState,
  type Playlist,
  type SearchResultItem,
  type YtMusicTab,
} from './ytMusicTypes';

describe('ytMusicTypes helpers', () => {
  it('supports Playlist, SearchResultItem, and YtMusicTab types', () => {
    const tab: YtMusicTab = 'discovery';
    const pl: Playlist = { playlist_id: 'p1', title: 'Favorites', count: 10 };
    const sr: SearchResultItem = { video_id: 'v1', title: 'Song', artist: 'Artist', duration: '3:00' };
    expect(tab).toBe('discovery');
    expect(pl.playlist_id).toBe('p1');
    expect(sr.video_id).toBe('v1');
  });
  describe('resolveAccounts', () => {
    it('returns stateData accounts when available', () => {
      const customAccounts: AccountProfile[] = [
        { id: 'user-1', name: 'Work Profile', handle: '@work', type: 'email' },
      ];
      const state: MusicState = { accounts: customAccounts };
      expect(resolveAccounts(state, null)).toEqual(customAccounts);
    });

    it('resolves accounts from config when stateData accounts are missing', () => {
      const config = {
        accounts: [
          { id: 'cfg-1', name: 'Studio', handle: '@studio', type: 'brand' },
          { id: 'cfg-2', name: 'Personal', type: 'email' },
          { id: 'cfg-3' },
          {},
          'cfg-5-string',
        ],
      };
      const result = resolveAccounts(null, config);
      expect(result).toEqual([
        { id: 'cfg-1', name: 'Studio', handle: '@studio', type: 'brand' },
        { id: 'cfg-2', name: 'Personal', handle: undefined, type: 'email' },
        { id: 'cfg-3', name: 'cfg-3', handle: undefined, type: 'email' },
        { id: 'acc_3', name: 'Account 4', handle: undefined, type: 'email' },
        { id: 'cfg-5-string', name: 'cfg-5-string', type: 'email' },
      ]);
    });

    it('falls back to default profiles when neither stateData nor config provides accounts', () => {
      expect(resolveAccounts(null, null)).toEqual(DEFAULT_ACCOUNTS);
      expect(resolveAccounts({}, {})).toEqual(DEFAULT_ACCOUNTS);
    });
  });

  describe('resolveSpeakersWithLocal', () => {
    it('prepends This Device when not present in stateData.speakers', () => {
      const state: MusicState = {
        speakers: [{ entity_id: 'media_player.kitchen', name: 'Kitchen Speaker' }],
      };
      const result = resolveSpeakersWithLocal(state);
      expect(result).toHaveLength(2);
      expect(result[0]).toEqual(LOCAL_SPEAKER);
      expect(result[1]).toEqual({ entity_id: 'media_player.kitchen', name: 'Kitchen Speaker' });
    });

    it('does not duplicate This Device if this_device or local is already present', () => {
      const state1: MusicState = {
        speakers: [{ entity_id: 'this_device', name: 'This Device' }],
      };
      expect(resolveSpeakersWithLocal(state1)).toEqual([
        { entity_id: 'this_device', name: 'This Device' },
      ]);

      const state2: MusicState = {
        speakers: [{ entity_id: 'local', name: 'Local Output' }],
      };
      expect(resolveSpeakersWithLocal(state2)).toEqual([
        { entity_id: 'local', name: 'Local Output' },
      ]);

      const state3: MusicState = {
        speakers: [{ entity_id: 'media_player.custom', name: 'This Device' }],
      };
      expect(resolveSpeakersWithLocal(state3)).toEqual([
        { entity_id: 'media_player.custom', name: 'This Device' },
      ]);
    });

    it('returns a list with only This Device when stateData or speakers is empty', () => {
      expect(resolveSpeakersWithLocal(null)).toEqual([LOCAL_SPEAKER]);
      expect(resolveSpeakersWithLocal({})).toEqual([LOCAL_SPEAKER]);
    });
  });

  describe('resolveSuggestions', () => {
    it('returns stateData.suggestions when provided', () => {
      const customSuggestions = [
        { id: 's1', title: 'Top Hits', kind: 'album' as const },
      ];
      expect(resolveSuggestions({ suggestions: customSuggestions })).toEqual(customSuggestions);
    });

    it('derives suggestions from queue and playlists when suggestions are empty', () => {
      const state: MusicState = {
        queue: [
          { title: 'Song 1', artist: 'Daft Punk', thumb: 'https://example.com/dp.jpg' },
          { title: 'Song 2', artist: 'Daft Punk' },
          { title: 'Song 3', artist: 'Justice' },
        ],
        playlists: [
          { playlist_id: 'pl-1', title: 'Discovery Mix', count: 25, thumb: 'https://example.com/pl.jpg' },
        ],
      };
      const result = resolveSuggestions(state);
      expect(result.length).toBe(3);
      expect(result[0]).toEqual({
        id: 'sug-artist-0',
        title: 'Daft Punk',
        subtitle: 'Artist Radio',
        kind: 'artist',
        thumb: 'https://example.com/dp.jpg',
        video_id: undefined,
      });
      expect(result[1]).toEqual({
        id: 'sug-artist-1',
        title: 'Justice',
        subtitle: 'Artist Radio',
        kind: 'artist',
        thumb: undefined,
        video_id: undefined,
      });
      expect(result[2]).toEqual({
        id: 'sug-pl-pl-1',
        title: 'Discovery Mix',
        subtitle: '25 tracks',
        kind: 'album',
        thumb: 'https://example.com/pl.jpg',
        playlist_id: 'pl-1',
      });
    });

    it('caps derived suggestions when queue has many artists or playlists have no count', () => {
      const state: MusicState = {
        queue: [
          { title: 'S1', artist: 'A1' },
          { title: 'S2', artist: 'A2' },
          { title: 'S3', artist: 'A3' },
          { title: 'S4', artist: 'A4' },
        ],
        playlists: [
          { playlist_id: 'p1', title: 'P1' },
          { playlist_id: 'p2', title: 'P2' },
          { playlist_id: 'p3', title: 'P3' },
          { playlist_id: 'p4', title: 'P4' },
          { playlist_id: 'p5', title: 'P5' },
          { playlist_id: 'p6', title: 'P6' },
          { playlist_id: 'p7', title: 'P7' },
        ],
      };
      const result = resolveSuggestions(state);
      // 3 artists + up to 6 playlists = 7 total (since break happens when derived.length >= 6)
      expect(result.length).toBe(6);
      expect(result[0].title).toBe('A1');
      expect(result[3].subtitle).toBe('Playlist');
    });

    it('returns default suggestions when state has no suggestions, queue, or playlists', () => {
      expect(resolveSuggestions(null)).toEqual(DEFAULT_SUGGESTIONS);
      expect(resolveSuggestions({})).toEqual(DEFAULT_SUGGESTIONS);
      expect(resolveSuggestions({ queue: [], playlists: [] })).toEqual(DEFAULT_SUGGESTIONS);
    });
  });

  describe('constants', () => {
    it('defines DEFAULT_ART and DEFAULT_STATE', () => {
      expect(DEFAULT_ART).toContain('ytube_music_player');
      expect(DEFAULT_STATE.active_speaker).toBe('media_player.home_group');
      expect(DEFAULT_STATE.active_account).toBe('primary');
    });
  });
});
