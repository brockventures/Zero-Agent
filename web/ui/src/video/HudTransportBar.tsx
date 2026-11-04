import { HudFastForwardIcon, HudNextIcon, HudPrevIcon, HudRewindIcon } from './HudIcons';

export interface HudTransportBarProps {
  isPlaybackActive: boolean;
  hasProgress: boolean;
  isScrubbing: boolean;
  currentTimeStr: string;
  totalTimeStr: string;
  progressPercent: number;
  onCaptions: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onPrevious: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onRewind: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onFastForward: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onNext: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onTrackPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerUp: (e: React.PointerEvent<HTMLDivElement>) => void;
  onTrackPointerCancel: (e: React.PointerEvent<HTMLDivElement>) => void;
}

/**
 * Renders the bottom transport controls, seek buttons, and progress scrub bar.
 */
export function HudTransportBar({
  isPlaybackActive,
  hasProgress,
  isScrubbing,
  currentTimeStr,
  totalTimeStr,
  progressPercent,
  onCaptions,
  onPrevious,
  onRewind,
  onFastForward,
  onNext,
  onTrackPointerDown,
  onTrackPointerMove,
  onTrackPointerUp,
  onTrackPointerCancel,
}: HudTransportBarProps) {
  const stopProp = (e: React.PointerEvent | React.TouchEvent): void => {
    e.stopPropagation();
  };

  return (
    <div
      className="video-hud-controls video-hud-bottom-bar"
      id="video-hud-bottom-bar"
      style={{
        display: isPlaybackActive ? '' : 'none',
        pointerEvents: isPlaybackActive ? undefined : 'none',
      }}
      onPointerDown={stopProp}
      onTouchStart={stopProp}
    >
      <button
        type="button"
        id="video-hud-cc-btn"
        className="video-hud-btn video-hud-cc video-hud-media-btn"
        aria-label="Closed Captions"
        title="Closed Captions"
        onClick={onCaptions}
      >
        CC
      </button>

      <button
        type="button"
        id="video-hud-prev-btn"
        className="video-hud-btn video-hud-prev video-hud-media-btn"
        aria-label="Previous Episode"
        title="Previous Episode"
        onClick={onPrevious}
      >
        <HudPrevIcon />
      </button>

      <button
        type="button"
        id="video-hud-rewind-btn"
        className="video-hud-btn video-hud-skip video-hud-media-btn"
        aria-label="Rewind 10 Seconds"
        title="Rewind 10 Seconds"
        onClick={onRewind}
      >
        <HudRewindIcon /> 10s
      </button>

      <div
        className="video-hud-progress-container"
        id="video-hud-progress-container"
        style={{ display: hasProgress ? '' : 'none' }}
      >
        <span className="video-hud-time video-hud-time-current" id="video-hud-time-current">
          {currentTimeStr}
        </span>
        <div
          className={`video-hud-progress-track${isScrubbing ? ' is-scrubbing' : ''}`}
          id="video-hud-progress-track"
          onPointerDown={onTrackPointerDown}
          onPointerMove={onTrackPointerMove}
          onPointerUp={onTrackPointerUp}
          onPointerCancel={onTrackPointerCancel}
        >
          <div
            className="video-hud-progress-fill"
            id="video-hud-progress-fill"
            style={{ width: `${progressPercent.toFixed(1)}%` }}
          >
            <div className="video-hud-progress-thumb" id="video-hud-progress-thumb" />
          </div>
        </div>
        <span className="video-hud-time video-hud-time-total" id="video-hud-time-total">
          {totalTimeStr}
        </span>
      </div>

      <button
        type="button"
        id="video-hud-forward-btn"
        className="video-hud-btn video-hud-skip video-hud-media-btn"
        aria-label="Fast Forward 10 Seconds"
        title="Fast Forward 10 Seconds"
        onClick={onFastForward}
      >
        <HudFastForwardIcon /> 10s
      </button>

      <button
        type="button"
        id="video-hud-next-btn"
        className="video-hud-btn video-hud-next video-hud-media-btn"
        aria-label="Next Episode"
        title="Next Episode"
        onClick={onNext}
      >
        <HudNextIcon />
      </button>
    </div>
  );
}
