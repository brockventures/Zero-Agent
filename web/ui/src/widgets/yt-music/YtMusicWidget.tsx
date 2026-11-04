import './YtMusicWidget.css';
import { memo, useState, type ChangeEvent, type FormEvent } from 'react';
import { useIsCurrentPane } from '../../carousel/PaneContext';
import { useDocumentVisible } from '../../lifecycle/visibility';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import {
  YtMusicNextIcon,
  YtMusicPauseIcon,
  YtMusicPlayIcon,
  YtMusicPrevIcon,
  YtMusicSpeakerIcon,
  YtMusicStopIcon,
} from './YtMusicIcons';
import { YtMusicNowPlaying } from './YtMusicNowPlaying';
import {
  DEFAULT_ART,
  postMusicApi,
  resolveAccounts,
  resolveSpeakersWithLocal,
  resolveSuggestions,
  useEmberAccounts,
  useMusicStatePolling,
  type AccountProfile,
  type MusicState,
  type NowPlaying,
  type Playlist,
  type QueueItem,
  type SearchResultItem,
  type Speaker,
  type SuggestedItem,
  type YtMusicTab,
} from './ytMusicTypes';

export type { MusicState };

function MediaTile({
  title, sub, badge, thumb, isArtist, isPlaying, dataAttrs, onClick,
}: {
  title: string; sub?: string; badge?: string; thumb?: string; isArtist?: boolean;
  isPlaying?: boolean; dataAttrs?: Record<string, string | number | undefined>; onClick: () => void;
}) {
  'use no memo';
  return (
    <div
      className={`media-tile ${isArtist ? 'kind-artist' : ''}`}
      role="button"
      tabIndex={0}
      aria-label={`Play ${title}`}
      {...dataAttrs}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } }}
    >
      <img className="tile-img" src={thumb || DEFAULT_ART} loading="lazy" alt="" />
      <div className="tile-meta">
        <div className="tile-title">
          {isPlaying && (
            <span aria-hidden="true" style={{ display: 'inline-flex', verticalAlign: 'middle', marginRight: '4px' }}>
              <YtMusicPlayIcon />
            </span>
          )}
          {title}
        </div>
        {sub && <div className="tile-sub">{sub}</div>}
        {badge && <span className="tile-badge">{badge}</span>}
      </div>
    </div>
  );
}

function YtMusicHeader({ accounts, activeAccountId, isLocalSpeaker, onSelectAccount, onToggleSpeaker }: {
  accounts: AccountProfile[]; activeAccountId?: string; isLocalSpeaker: boolean;
  onSelectAccount: (id: string) => void; onToggleSpeaker: () => void;
}) {
  'use no memo';
  return (
    <div className="ytm-header">
      <div className="ytm-account-wrap">
        <select id="account-picker" className="ytm-account-select account-pill" aria-label="Select Account" value={activeAccountId ?? accounts[0]?.id ?? ''} onChange={(e) => onSelectAccount(e.target.value)}>
          {accounts.map((acc) => (
            <option key={acc.id} value={acc.id}>{acc.name}{acc.handle ? ` (${acc.handle})` : ''}</option>
          ))}
        </select>
      </div>
      <button type="button" id="speaker-toggle-btn" className="mode-btn speaker-toggle-btn" aria-label="Toggle Speaker" onClick={onToggleSpeaker}>
        <YtMusicSpeakerIcon />
        {isLocalSpeaker ? 'Cast to Speakers' : 'Play on This Device'}
      </button>
    </div>
  );
}

function YtMusicSpeakerBar({ speakers, activeSpeaker, onSelectSpeaker }: {
  speakers: Speaker[]; activeSpeaker: string; onSelectSpeaker: (id: string) => void;
}) {
  'use no memo';
  return (
    <div className="speaker-bar" data-prevent-screen-swipe="" id="speaker-bar" style={{ touchAction: 'pan-x' }}>
      {speakers.map((s) => (
        <button
          key={s.entity_id}
          type="button"
          className={`spk-pill ${s.entity_id === 'this_device' || s.entity_id === 'local' ? 'spk-local' : ''} ${s.entity_id === activeSpeaker ? 'active' : ''}`}
          data-spk={s.entity_id}
          aria-label={s.name}
          onClick={() => onSelectSpeaker(s.entity_id)}
        >
          <YtMusicSpeakerIcon />
          {s.name}
        </button>
      ))}
    </div>
  );
}

function YtMusicMiniPlayer({
  np, startingPlayback, onPrev, onPlayPause, onNext, onStop, onVolumeChange, onOpenNowPlaying,
}: {
  np: NowPlaying; startingPlayback: boolean; onPrev: () => void; onPlayPause: () => void;
  onNext: () => void; onStop: () => void; onVolumeChange: (e: ChangeEvent<HTMLInputElement>) => void; onOpenNowPlaying: () => void;
}) {
  'use no memo';
  const isPlaying = np.state === 'playing';
  return (
    <div className="player-deck">
      <img className="art-thumb" id="np-art" src={np.thumb || DEFAULT_ART} alt={np.title || 'Now Playing'} />
      <div className="np-meta" role="button" tabIndex={0} aria-label="Open now playing" onClick={onOpenNowPlaying} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpenNowPlaying(); } }}>
        <div className="np-title" id="np-title">{startingPlayback ? 'Starting playback...' : np.title || 'YouTube Music'}</div>
        <div className="np-artist" id="np-artist">{np.artist || (np.title ? 'Ready' : 'Pick a playlist or search a song')}</div>
      </div>
      <div className="transport">
        <button type="button" className="ctrl-btn" id="btn-prev" title="Previous" aria-label="Previous" onClick={onPrev}><YtMusicPrevIcon /></button>
        <button type="button" className="ctrl-btn play-main" id="btn-play" title={isPlaying ? 'Pause' : 'Play'} aria-label={isPlaying ? 'Pause' : 'Play'} onClick={onPlayPause}>
          {isPlaying ? <YtMusicPauseIcon /> : <YtMusicPlayIcon />}
        </button>
        <button type="button" className="ctrl-btn" id="btn-next" title="Next" aria-label="Next" onClick={onNext}><YtMusicNextIcon /></button>
        <button type="button" className="ctrl-btn" id="btn-stop" title="Stop" aria-label="Stop" onClick={onStop}><YtMusicStopIcon /></button>
      </div>
      <div className="vol-wrap" data-prevent-screen-swipe="">
        <span style={{ fontSize: '0.75rem', display: 'inline-flex', alignItems: 'center' }} aria-hidden="true"><YtMusicSpeakerIcon /></span>
        <input type="range" className="vol-slider" id="vol-slider" data-prevent-screen-swipe="" aria-label="Volume" min={0} max={100} value={typeof np.volume === 'number' ? np.volume : 35} style={{ touchAction: 'pan-x' }} onChange={onVolumeChange} />
      </div>
    </div>
  );
}

function YtMusicDiscovery({
  playlists, suggestions, onPlayPlaylist, onPlaySuggestion,
}: {
  playlists: Playlist[]; suggestions: SuggestedItem[]; onPlayPlaylist: (p: Playlist) => void; onPlaySuggestion: (s: SuggestedItem) => void;
}) {
  'use no memo';
  return (
    <div className="ytm-discovery" id="ytm-discovery">
      <div className="discovery-section" data-section="playlists">
        <div className="discovery-section-header"><span className="discovery-section-title">Playlists</span></div>
        <div className="discovery-grid">
          {playlists.map((p) => (
            <MediaTile key={p.playlist_id} title={p.title} sub={`${Number(p.count || 0)} tracks · Shuffle`} thumb={p.thumb} dataAttrs={{ 'data-pl': p.playlist_id, 'data-title': p.title }} onClick={() => onPlayPlaylist(p)} />
          ))}
        </div>
      </div>
      <div className="discovery-section" data-section="suggestions">
        <div className="discovery-section-header"><span className="discovery-section-title">Suggested Artists &amp; Albums</span></div>
        <div className="discovery-grid">
          {suggestions.map((sug) => (
            <MediaTile key={sug.id} title={sug.title} sub={sug.subtitle || (sug.kind === 'artist' ? 'Artist' : 'Album')} badge={sug.kind} thumb={sug.thumb} isArtist={sug.kind === 'artist'} dataAttrs={{ 'data-sug': sug.id }} onClick={() => onPlaySuggestion(sug)} />
          ))}
        </div>
      </div>
    </div>
  );
}

function YtMusicMediaGrid({
  activeTab, searching, queue, queueIndex, searchResults, onPlayIndex, onPlayResult,
}: {
  activeTab: YtMusicTab; searching: boolean; queue: QueueItem[]; queueIndex?: number;
  searchResults: SearchResultItem[]; onPlayIndex: (idx: number) => void; onPlayResult: (item: SearchResultItem) => void;
}) {
  'use no memo';
  if (activeTab === 'search') {
    if (searching) return <div style={{ fontSize: '0.78rem', opacity: 0.7 }}>Searching YouTube Music...</div>;
    return (
      <div className="media-grid" id="media-grid" style={{ touchAction: 'pan-y' }}>
        {searchResults.map((t, idx) => (
          <MediaTile key={t.video_id || idx} title={t.title} sub={`${t.artist}${t.duration ? ` · ${t.duration}` : ''}`} thumb={t.thumb} dataAttrs={{ 'data-sidx': idx }} onClick={() => onPlayResult(t)} />
        ))}
      </div>
    );
  }
  return (
    <div className="media-grid" id="media-grid" style={{ touchAction: 'pan-y' }}>
      {queue.map((t, idx) => (
        <MediaTile key={t.id ?? t.video_id ?? `${t.title}:${t.artist}:${idx}`} title={t.title} sub={t.artist} thumb={t.thumb} isPlaying={idx === queueIndex} dataAttrs={{ 'data-qidx': idx }} onClick={() => onPlayIndex(idx)} />
      ))}
    </div>
  );
}

export const YtMusicWidget = memo(function YtMusicWidget({ id }: WidgetComponentProps) {
  'use no memo';
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const rawApiBase = config?.api_base ?? config?.apiBase;
  const apiBase =
    typeof rawApiBase === 'string' && rawApiBase.trim() !== ''
      ? rawApiBase.trim().replace(/\/+$/, '')
      : '/hearth-api';

  const isCurrentPane = useIsCurrentPane();
  const isDocumentVisible = useDocumentVisible();
  const isActive = isCurrentPane && isDocumentVisible;

  const [selectedAccountId, setSelectedAccountId] = useState<string | undefined>(undefined);
  const [activeTab, setActiveTab] = useState<YtMusicTab>('playlists');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResultItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [startingPlayback, setStartingPlayback] = useState(false);
  const [userBrowsing, setUserBrowsing] = useState(false);

  const emberAccounts = useEmberAccounts();
  const [stateData, setStateData] = useMusicStatePolling(apiBase, isActive, selectedAccountId);

  const activeAccountId = selectedAccountId ?? stateData.active_account;
  const speakers = resolveSpeakersWithLocal(stateData);
  const accounts = resolveAccounts(stateData, config, emberAccounts);
  const currentActiveSpeaker = stateData.active_speaker ?? speakers[0]?.entity_id ?? 'this_device';
  const isLocalSpeaker = currentActiveSpeaker === 'this_device' || currentActiveSpeaker === 'local';

  const postControl = async (action: string, extra: Record<string, unknown> = {}) => {
    const next = await postMusicApi(`${apiBase}/music/control`, {
      action,
      speaker: currentActiveSpeaker,
      ...(activeAccountId ? { account_id: activeAccountId } : {}),
      ...extra,
    });
    if (next) setStateData((prev) => ({ ...prev, ...next }));
  };

  const postPlay = async (payload: Record<string, unknown>) => {
    setUserBrowsing(false);
    setStartingPlayback(true);
    try {
      const next = await postMusicApi(`${apiBase}/music/play`, {
        speaker: currentActiveSpeaker,
        ...(activeAccountId ? { account_id: activeAccountId } : {}),
        ...payload,
      });
      if (next) setStateData((prev) => ({ ...prev, ...next }));
    } finally {
      setStartingPlayback(false);
    }
  };

  const handleSelectSpeaker = async (entityId: string) => {
    setStateData((prev) => ({ ...prev, active_speaker: entityId }));
    await postControl('select_speaker', { speaker: entityId });
  };

  const handleToggleSpeaker = () => {
    if (isLocalSpeaker) {
      const remote = speakers.find((s) => s.entity_id !== 'this_device' && s.entity_id !== 'local');
      if (remote) void handleSelectSpeaker(remote.entity_id);
    } else {
      void handleSelectSpeaker('this_device');
    }
  };

  const handleSelectAccount = async (accountId: string) => {
    setSelectedAccountId(accountId);
    setStateData((prev) => ({ ...prev, active_account: accountId }));
    await postControl('select_account', { account_id: accountId });
  };

  const handleVolumeChange = (e: ChangeEvent<HTMLInputElement>) => {
    const val = Number(e.target.value);
    setStateData((prev) => ({ ...prev, now_playing: { ...prev.now_playing, volume: val } }));
    void postControl('volume', { value: val });
  };

  const handleStop = async () => {
    setStateData((prev) => ({ ...prev, now_playing: { ...prev.now_playing, state: 'stopped' } }));
    await postControl('stop');
  };

  const handleSearchSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const q = searchQuery.trim();
    if (!q) return;
    setActiveTab('search');
    setSearching(true);
    try {
      const param = activeAccountId ? `&account_id=${encodeURIComponent(activeAccountId)}` : '';
      const r = await fetch(`${apiBase}/music/search?q=${encodeURIComponent(q)}${param}`);
      const data = (await r.json()) as { items?: SearchResultItem[] };
      setSearchResults(Array.isArray(data?.items) ? data.items : []);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  };

  const handlePlaySuggestion = async (item: SuggestedItem) => {
    if (item.playlist_id) {
      await postPlay({ playlist_id: item.playlist_id, playlist_title: item.title, shuffle: true });
    } else if (item.video_id) {
      await postPlay({ video_id: item.video_id, title: item.title, artist: item.subtitle ?? 'Artist', thumb: item.thumb });
    } else {
      await postPlay({ title: item.title, artist: item.subtitle ?? 'Artist', thumb: item.thumb });
    }
  };

  const handlePlayPause = async () => {
    const track = stateData.now_playing;
    const hasTrack = Boolean(track?.title || track?.video_id);

    if (hasTrack) {
      const isCurrentlyPlaying = track?.state === 'playing';
      const nextState = isCurrentlyPlaying ? 'paused' : 'playing';
      setStateData((prev) => ({ ...prev, now_playing: { ...prev.now_playing, state: nextState } }));
      if (nextState === 'playing') setUserBrowsing(false);
      await postControl('play_pause');
      return;
    }

    if (stateData.queue && stateData.queue.length > 0) {
      setUserBrowsing(false);
      setStartingPlayback(true);
      try {
        await postControl('play_index', { index: 0 });
      } finally {
        setStartingPlayback(false);
      }
      return;
    }

    if (stateData.playlists && stateData.playlists.length > 0) {
      const pl = stateData.playlists[0];
      await postPlay({ playlist_id: pl.playlist_id, playlist_title: pl.title, shuffle: true });
      return;
    }

    const sugList = resolveSuggestions(stateData);
    await handlePlaySuggestion(sugList[0]);
  };

  const np = stateData.now_playing || {};
  const isPlaying = np.state === 'playing';
  const showNowPlaying = !userBrowsing && (isPlaying || startingPlayback);

  if (showNowPlaying) {
    return (
      <div className="widget-yt-music">
        <YtMusicNowPlaying
          nowPlaying={stateData.now_playing}
          queue={stateData.queue}
          activeSpeaker={currentActiveSpeaker}
          speakers={speakers}
          startingPlayback={startingPlayback}
          onPrev={() => void postControl('prev')}
          onPlayPause={() => void handlePlayPause()}
          onNext={() => void postControl('next')}
          onStop={() => void handleStop()}
          onVolumeChange={handleVolumeChange}
          onPlayQueueIndex={(idx) => { setUserBrowsing(false); void postControl('play_index', { index: idx }); }}
          onBrowseLibrary={() => setUserBrowsing(true)}
        />
      </div>
    );
  }

  return (
    <div className="widget-yt-music">
      <YtMusicHeader
        accounts={accounts}
        activeAccountId={activeAccountId}
        isLocalSpeaker={isLocalSpeaker}
        onSelectAccount={(accId) => void handleSelectAccount(accId)}
        onToggleSpeaker={handleToggleSpeaker}
      />
      <YtMusicSpeakerBar
        speakers={speakers}
        activeSpeaker={currentActiveSpeaker}
        onSelectSpeaker={(spkId) => void handleSelectSpeaker(spkId)}
      />
      <YtMusicMiniPlayer
        np={np}
        startingPlayback={startingPlayback}
        onPrev={() => void postControl('prev')}
        onPlayPause={() => void handlePlayPause()}
        onNext={() => void postControl('next')}
        onStop={() => void handleStop()}
        onVolumeChange={handleVolumeChange}
        onOpenNowPlaying={() => { if (np.title) setUserBrowsing(false); }}
      />
      <div className="sub-toolbar">
        <button
          type="button"
          className={`mode-btn ${activeTab === 'playlists' || activeTab === 'discovery' ? 'active' : ''}`}
          data-tab="playlists"
          aria-label="Playlists"
          onClick={() => setActiveTab('playlists')}
        >
          Playlists
        </button>
        <button
          type="button"
          className={`mode-btn ${activeTab === 'queue' ? 'active' : ''}`}
          data-tab="queue"
          aria-label="Queue"
          onClick={() => setActiveTab('queue')}
        >
          Queue
        </button>
        <form className="search-form" id="search-form" onSubmit={(e) => void handleSearchSubmit(e)}>
          <input
            type="text"
            className="search-input"
            data-prevent-screen-swipe=""
            id="search-input"
            aria-label="Search YouTube Music"
            placeholder="Search YouTube Music songs or artists..."
            autoComplete="off"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          <button type="submit" className={`mode-btn ${activeTab === 'search' ? 'active' : ''}`} aria-label="Search">
            Search
          </button>
        </form>
      </div>
      {activeTab === 'playlists' || activeTab === 'discovery' ? (
        <YtMusicDiscovery
          playlists={stateData.playlists || []}
          suggestions={resolveSuggestions(stateData)}
          onPlayPlaylist={(p) => void postPlay({ playlist_id: p.playlist_id, playlist_title: p.title, shuffle: true })}
          onPlaySuggestion={(s) => void handlePlaySuggestion(s)}
        />
      ) : (
        <YtMusicMediaGrid
          activeTab={activeTab}
          searching={searching}
          queue={stateData.queue || []}
          queueIndex={np.queue_index}
          searchResults={searchResults}
          onPlayIndex={(idx) => { setUserBrowsing(false); void postControl('play_index', { index: idx }); }}
          onPlayResult={(item) => void postPlay({ video_id: item.video_id, title: item.title, artist: item.artist, thumb: item.thumb })}
        />
      )}
    </div>
  );
});
