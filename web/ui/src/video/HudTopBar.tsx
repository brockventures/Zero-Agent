import { HudBackIcon, HudHomeIcon, HudMuteIcon, HudUnmuteIcon } from './HudIcons';

export interface HudTopBarProps {
  titleText: string;
  isMedia: boolean;
  volume: number;
  isMuted: boolean;
  onBack: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onHome: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onDismiss: (e: React.MouseEvent<HTMLButtonElement>) => void;
  onVolumeInput: (e: React.FormEvent<HTMLInputElement>) => void;
  onVolumeCommit: (e?: React.SyntheticEvent | Event) => void;
  onMuteToggle: (e: React.MouseEvent<HTMLButtonElement>) => void;
}

/**
 * Renders the top navigation capsule, stream title, and top-right side rail controls.
 */
export function HudTopBar({
  titleText,
  isMedia,
  volume,
  isMuted,
  onBack,
  onHome,
  onDismiss,
  onVolumeInput,
  onVolumeCommit,
  onMuteToggle,
}: HudTopBarProps) {
  const stopProp = (e: React.PointerEvent | React.TouchEvent): void => {
    e.stopPropagation();
  };

  return (
    <div className="video-hud-header">
      {/* Unified horizontal capsule in upper left: Back + Home */}
      <div
        className="video-hud-capsule-top-left video-hud-nav-bar"
        id="video-hud-nav-bar"
        style={{ display: isMedia ? '' : 'none' }}
        onPointerDown={stopProp}
        onTouchStart={stopProp}
      >
        <button
          type="button"
          id="video-hud-back-btn"
          className="video-hud-btn video-hud-nav-btn"
          aria-label="Back"
          title="Back"
          onClick={onBack}
        >
          <HudBackIcon />
        </button>
        <button
          type="button"
          id="video-hud-home-btn"
          className="video-hud-btn video-hud-nav-btn"
          aria-label="Home"
          title="Home"
          onClick={onHome}
        >
          <HudHomeIcon />
        </button>
      </div>

      <span id="video-hud-title" className="video-hud-title">
        {titleText}
      </span>

      {/* Unified vertical frosted pill in upper right: Dismiss + Volume + Mute */}
      <div
        className="video-hud-capsule-top-right video-hud-side-rail"
        id="video-hud-side-rail"
        onPointerDown={stopProp}
        onTouchStart={stopProp}
      >
        <button
          type="button"
          id="video-hud-dismiss-btn"
          className="video-hud-btn video-hud-dismiss"
          aria-label="Dismiss Video"
          title="Dismiss Video"
          onClick={onDismiss}
        >
          ✕
        </button>
        <div className="video-hud-volume-container">
          <input
            type="range"
            id="video-hud-volume-slider"
            className={`video-hud-volume-slider${isMuted ? ' muted' : ''}`}
            min="0"
            max="100"
            value={volume}
            onInput={onVolumeInput}
            onChange={onVolumeInput}
            onPointerUp={onVolumeCommit}
            onTouchEnd={onVolumeCommit}
            onPointerCancel={onVolumeCommit}
            aria-label="Master Volume"
          />
        </div>
        <button
          type="button"
          id="video-hud-mute-btn"
          className={`video-hud-btn video-hud-mute${isMuted ? ' muted' : ''}`}
          aria-label={isMuted ? 'Unmute' : 'Mute'}
          title={isMuted ? 'Unmute' : 'Mute'}
          onClick={onMuteToggle}
        >
          <span
            className="video-hud-icon"
            id="video-hud-mute-icon"
            data-icon={isMuted ? 'muted' : 'unmuted'}
          >
            {isMuted ? <HudMuteIcon /> : <HudUnmuteIcon />}
          </span>
        </button>
      </div>
    </div>
  );
}
