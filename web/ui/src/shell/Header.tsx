import { useEffect } from 'react';
import { useHeader } from '../store/hooks';
import { VoiceIcon } from '../voice/VoiceIcon';
import { useVoiceHudState } from '../voice/useVoiceHudState';
import { Clock, resolveActiveTimezone } from './Clock';
import { HeaderVideoButton } from './HeaderVideoButton';
import { HeaderLayoutButton } from './HeaderLayoutButton';
import { NextUp } from './NextUp';
import { StatusDot } from './StatusDot';
import { WeatherPill } from './WeatherPill';
import { applyCursorSuppression } from './cursor';
import { ThemeToggle } from './theme';

export interface HeaderProps {
  timezone?: string;
}

export function HeaderVoice() {
  const { headerVoice, resetHUD } = useVoiceHudState();
  const {
    isActive,
    stateClass,
    iconKind,
    label,
    statusText,
    transcriptText,
    showDot,
    showProgress,
  } = headerVoice;

  return (
    <div className={`header-voice${isActive ? ' visible' : ''}`} id="header-voice">
      <div className={`voice-indicator${stateClass ? ` ${stateClass}` : ''}`} id="voice-indicator">
        <span className="voice-pulse-ring" id="voice-pulse-ring" />
        <span className="voice-dot" id="voice-dot" style={{ display: showDot ? undefined : 'none' }} />
        <span className="voice-icon" id="voice-icon">
          <VoiceIcon kind={iconKind} />
        </span>
        <span className="voice-label" id="voice-label">
          {label || null}
        </span>
      </div>
      <span className="voice-status" id="voice-status" style={{ display: statusText ? undefined : 'none' }}>
        {statusText || null}
      </span>
      <div
        className="voice-transcript"
        id="voice-transcript"
        style={{ display: transcriptText ? undefined : 'none' }}
      >
        {transcriptText || null}
      </div>
      <button
        id="header-voice-dismiss-btn"
        className="header-voice-dismiss"
        type="button"
        aria-label="Dismiss Voice"
        title="Dismiss Voice"
        onClick={() => {
          if (window.MirrormereVoice?.resetHUD) {
            window.MirrormereVoice.resetHUD();
          } else {
            resetHUD();
          }
        }}
      >
        ✕
      </button>
      <div
        className="voice-audio-progress"
        id="voice-audio-progress"
        style={{ display: showProgress ? 'block' : 'none' }}
      />
    </div>
  );
}

export function Header({ timezone: propTimezone }: HeaderProps = {}) {
  const header = useHeader();
  const timezone = resolveActiveTimezone(propTimezone, header);

  useEffect(() => {
    applyCursorSuppression();
  }, []);

  useEffect(() => {
    if (typeof document !== 'undefined' && timezone) {
      if (document.body?.dataset) {
        document.body.dataset.timezone = timezone;
      }
      const appEl = document.getElementById('mirrormere-app');
      if (appEl?.dataset) {
        appEl.dataset.timezone = timezone;
      }
    }
  }, [timezone]);

  return (
    <header className="header-zone">
      <Clock timezone={propTimezone} />
      <NextUp timezone={propTimezone} />
      <div className="header-right-cluster">
        <HeaderLayoutButton />
        <HeaderVideoButton />
        <WeatherPill />
        <ThemeToggle />
        <StatusDot />
      </div>
      <HeaderVoice />
    </header>
  );
}
