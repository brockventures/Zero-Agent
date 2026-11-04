import {
  HudPauseIcon,
  HudPlayIcon,
} from './HudIcons';
import { HudTopBar } from './HudTopBar';
import { HudTransportBar } from './HudTransportBar';
import {
  useHudController,
  type HudProps,
  type VideoHUDHandle,
} from './useHudController';

export { HUD_ICONS } from './HudIcons';
export {
  VideoHUDController,
  type HudProps,
} from './useHudController';

declare global {
  interface Window {
    videoHUD?: VideoHUDHandle;
    MirrormereHUD?: {
      VideoHUDController: unknown;
    };
  }
}

/**
 * Hud React component rendering the declarative #video-hud-overlay, navigation,
 * volume rail, central play/pause transport, and scrubbable progress bar.
 */
export function Hud(props: HudProps = {}) {
  const {
    overlayRef,
    isVisible,
    playerState,
    titleText,
    isMedia,
    isPlaybackActive,
    isPaused,
    volume,
    isMuted,
    hasProgress,
    isScrubbing,
    currentTimeStr,
    totalTimeStr,
    progressPercent,
    resetAutoFade,
    handlePlayPauseToggle,
    handleBack,
    handleHome,
    handleDismiss,
    handleSliderInput,
    handleSliderCommit,
    handleMuteToggle,
    handleCaptions,
    handlePrevious,
    handleRewind,
    handleFastForward,
    handleNext,
    onTrackPointerDown,
    onTrackPointerMove,
    onTrackPointerUp,
    onTrackPointerCancel,
  } = useHudController(props);

  return (
    <div
      id="video-hud-overlay"
      className={`video-hud-overlay${isVisible ? ' visible' : ''}`}
      ref={overlayRef}
      data-player-state={playerState}
    >
      <HudTopBar
        titleText={titleText}
        isMedia={isMedia}
        volume={volume}
        isMuted={isMuted}
        onBack={handleBack}
        onHome={handleHome}
        onDismiss={handleDismiss}
        onVolumeInput={handleSliderInput}
        onVolumeCommit={handleSliderCommit}
        onMuteToggle={handleMuteToggle}
      />

      <button
        id="video-hud-play-btn"
        className="video-hud-btn video-hud-play video-hud-play-center"
        aria-label={isPaused ? 'Play' : 'Pause'}
        title={isPaused ? 'Play' : 'Pause'}
        style={{
          display: isPlaybackActive ? '' : 'none',
          pointerEvents: isPlaybackActive ? undefined : 'none',
        }}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          resetAutoFade();
          void handlePlayPauseToggle();
        }}
      >
        <span
          className="video-hud-icon"
          id="video-hud-play-icon"
          data-icon={isPaused ? 'play' : 'pause'}
        >
          {isPaused ? <HudPlayIcon /> : <HudPauseIcon />}
        </span>
      </button>

      <HudTransportBar
        isPlaybackActive={isPlaybackActive}
        hasProgress={hasProgress}
        isScrubbing={isScrubbing}
        currentTimeStr={currentTimeStr}
        totalTimeStr={totalTimeStr}
        progressPercent={progressPercent}
        onCaptions={handleCaptions}
        onPrevious={handlePrevious}
        onRewind={handleRewind}
        onFastForward={handleFastForward}
        onNext={handleNext}
        onTrackPointerDown={onTrackPointerDown}
        onTrackPointerMove={onTrackPointerMove}
        onTrackPointerUp={onTrackPointerUp}
        onTrackPointerCancel={onTrackPointerCancel}
      />
    </div>
  );
}
