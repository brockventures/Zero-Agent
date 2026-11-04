import { memo, type ChangeEvent } from 'react';
import {
  YtMusicNextIcon,
  YtMusicPauseIcon,
  YtMusicPlayIcon,
  YtMusicPrevIcon,
  YtMusicSpeakerIcon,
  YtMusicStopIcon,
} from './YtMusicIcons';
import {
  DEFAULT_ART,
  type NowPlaying,
  type QueueItem,
  type Speaker,
} from './ytMusicTypes';

export interface YtMusicNowPlayingProps {
  nowPlaying?: NowPlaying;
  queue?: QueueItem[];
  activeSpeaker?: string;
  speakers?: Speaker[];
  startingPlayback?: boolean;
  onPrev: () => void;
  onPlayPause: () => void;
  onNext: () => void;
  onStop: () => void;
  onVolumeChange: (e: ChangeEvent<HTMLInputElement>) => void;
  onPlayQueueIndex?: (index: number) => void;
  onBrowseLibrary: () => void;
}

/**
 * Dedicated full-card Now Playing view for YouTube Music widget.
 * Features hero playback deck, active output speaker indicator, volume slider,
 * transport controls, 'Browse Library' return button, and Up Next queue list.
 */
export const YtMusicNowPlaying = memo(function YtMusicNowPlaying({
  nowPlaying,
  queue = [],
  activeSpeaker,
  speakers = [],
  startingPlayback = false,
  onPrev,
  onPlayPause,
  onNext,
  onStop,
  onVolumeChange,
  onPlayQueueIndex,
  onBrowseLibrary,
}: YtMusicNowPlayingProps) {
  const np = nowPlaying || {};
  const currentTitle = startingPlayback ? 'Starting playback...' : np.title || 'YouTube Music';
  const currentArtist = np.artist || (np.title ? 'Ready' : 'Pick a playlist or search a song');
  const isPlaying = np.state === 'playing';
  const volumeValue = typeof np.volume === 'number' ? np.volume : 35;

  const activeSpkObj = speakers.find((s) => s.entity_id === activeSpeaker);
  const activeSpeakerName =
    activeSpkObj?.name ??
    (activeSpeaker === 'this_device' || activeSpeaker === 'local'
      ? 'This Device'
      : (activeSpeaker || 'This Device'));

  return (
    <div className="ytm-now-playing-view">
      {/* 1. Header Toolbar with Browse Library button and Speaker Output Badge */}
      <div className="np-top-bar">
        <button
          type="button"
          className="btn-browse-library"
          id="btn-browse-library"
          aria-label="Browse Library"
          onClick={onBrowseLibrary}
        >
          <svg
            viewBox="0 0 24 24"
            width="14"
            height="14"
            fill="currentColor"
            aria-hidden="true"
            style={{ marginRight: '6px', verticalAlign: 'middle' }}
          >
            <path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z" />
          </svg>
          Browse Library
        </button>

        <div className="np-device-badge" id="np-device-badge" title={`Output: ${activeSpeakerName}`}>
          <span className="np-device-dot" aria-hidden="true" />
          <span className="np-device-name">
            <YtMusicSpeakerIcon />
            {activeSpeakerName}
          </span>
        </div>
      </div>

      {/* 2. Hero Player Deck */}
      <div className="player-deck">
        <img
          className="art-thumb"
          id="np-art"
          src={np.thumb || DEFAULT_ART}
          alt={np.title || 'Now Playing'}
        />
        <div className="np-meta">
          <div className="np-title" id="np-title">
            {currentTitle}
          </div>
          <div className="np-artist" id="np-artist">
            {currentArtist}
          </div>
        </div>
        <div className="transport">
          <button
            type="button"
            className="ctrl-btn"
            id="btn-prev"
            title="Previous"
            aria-label="Previous"
            onClick={onPrev}
          >
            <YtMusicPrevIcon />
          </button>
          <button
            type="button"
            className="ctrl-btn play-main"
            id="btn-play"
            title={isPlaying ? 'Pause' : 'Play'}
            aria-label={isPlaying ? 'Pause' : 'Play'}
            onClick={onPlayPause}
          >
            {isPlaying ? <YtMusicPauseIcon /> : <YtMusicPlayIcon />}
          </button>
          <button
            type="button"
            className="ctrl-btn"
            id="btn-next"
            title="Next"
            aria-label="Next"
            onClick={onNext}
          >
            <YtMusicNextIcon />
          </button>
          <button
            type="button"
            className="ctrl-btn"
            id="btn-stop"
            title="Stop"
            aria-label="Stop"
            onClick={onStop}
          >
            <YtMusicStopIcon />
          </button>
        </div>
        <div className="vol-wrap" data-prevent-screen-swipe="">
          <span
            style={{ fontSize: '0.75rem', display: 'inline-flex', alignItems: 'center' }}
            aria-hidden="true"
          >
            <YtMusicSpeakerIcon />
          </span>
          <input
            type="range"
            className="vol-slider"
            id="vol-slider"
            data-prevent-screen-swipe=""
            aria-label="Volume"
            min={0}
            max={100}
            value={volumeValue}
            style={{ touchAction: 'pan-x' }}
            onChange={onVolumeChange}
          />
        </div>
      </div>

      {/* 3. Up Next Queue Section */}
      <div className="np-queue-section">
        <div className="np-queue-header">
          <span className="np-queue-label">Up Next</span>
          <span className="np-queue-count">
            {queue.length} {queue.length === 1 ? 'track' : 'tracks'}
          </span>
        </div>
        <div className="np-queue-list" id="np-queue-list" style={{ touchAction: 'pan-y' }}>
          {queue.length === 0 ? (
            <div className="np-queue-empty">Queue is empty</div>
          ) : (
            queue.map((t, idx) => {
              const isCurrent = idx === np.queue_index;
              const queueKey = t.id ?? t.video_id ?? `${t.title}:${t.artist}:${idx}`;
              return (
                <div
                  key={queueKey}
                  className={`np-queue-row ${isCurrent ? 'active' : ''}`}
                  role="button"
                  tabIndex={0}
                  aria-label={`Play ${t.title} by ${t.artist}`}
                  data-qidx={idx}
                  onClick={() => onPlayQueueIndex?.(idx)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onPlayQueueIndex?.(idx);
                    }
                  }}
                >
                  <img
                    className="np-queue-thumb"
                    src={t.thumb || DEFAULT_ART}
                    loading="lazy"
                    alt=""
                  />
                  <div className="np-queue-meta">
                    <div className="np-queue-title">
                      {isCurrent && (
                        <span className="np-queue-playing-indicator" aria-hidden="true">
                          <YtMusicPlayIcon />
                        </span>
                      )}
                      {t.title}
                    </div>
                    <div className="np-queue-artist">{t.artist}</div>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
});
