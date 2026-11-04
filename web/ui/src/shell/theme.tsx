import { useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import { useDisplayStore } from '../store/index';

export const STORAGE_KEY_THEME_OVERRIDE = 'mirrormere_theme_override';

export type ThemeMode = 'day' | 'night';

export function normalizeTheme(val: string | null | undefined): ThemeMode {
  return val === 'night' ? 'night' : 'day';
}

export function getStoredThemeOverride(storage?: Pick<Storage, 'getItem'> | null): ThemeMode | null {
  try {
    const s = storage !== undefined ? storage : (typeof window !== 'undefined' ? window.localStorage : null);
    if (s) {
      const val = s.getItem(STORAGE_KEY_THEME_OVERRIDE);
      if (val === 'day' || val === 'night') return val;
    }
  } catch {
    // ignore storage access errors
  }
  return null;
}

export function setStoredThemeOverride(
  val: ThemeMode | null,
  storage?: Pick<Storage, 'setItem' | 'removeItem'> | null
): void {
  try {
    const s = storage !== undefined ? storage : (typeof window !== 'undefined' ? window.localStorage : null);
    if (s) {
      if (val === 'day' || val === 'night') {
        s.setItem(STORAGE_KEY_THEME_OVERRIDE, val);
      } else {
        s.removeItem(STORAGE_KEY_THEME_OVERRIDE);
      }
    }
  } catch {
    // ignore storage access errors
  }
}

export function resolveEffectiveTheme(serverTheme: string | null | undefined, override: ThemeMode | null): ThemeMode {
  if (override === 'day' || override === 'night') {
    return override;
  }
  return normalizeTheme(serverTheme);
}

export function nextThemeOnTap(
  currentTheme: ThemeMode,
  serverTheme: ThemeMode,
  override: ThemeMode | null
): { activeTheme: ThemeMode; nextOverride: ThemeMode | null } {
  const next = currentTheme === 'night' ? 'day' : 'night';
  if (!override) {
    return { activeTheme: next, nextOverride: next };
  }
  const autoTheme = serverTheme;
  const oppositeOfAuto = autoTheme === 'night' ? 'day' : 'night';
  if (override === oppositeOfAuto) {
    return { activeTheme: autoTheme, nextOverride: autoTheme };
  }
  return { activeTheme: autoTheme, nextOverride: null };
}

export function computeThemeButtonAttrs(
  activeTheme: ThemeMode,
  serverTheme: ThemeMode,
  override: ThemeMode | null
): { mode: 'manual' | 'auto'; ariaLabel: string; title: string } {
  const mode = override ? 'manual' : 'auto';
  const modeLabel = activeTheme === 'night' ? 'Dark Mode' : 'Light Mode';
  const nextLabel = activeTheme === 'night' ? 'Light Mode' : 'Dark Mode';
  const autoTheme = serverTheme || 'day';
  const oppositeOfAuto = autoTheme === 'night' ? 'day' : 'night';
  const isOppositeOverride = override === oppositeOfAuto;
  const actionLabel = !override
    ? nextLabel
    : (isOppositeOverride ? nextLabel : 'Auto Solar');
  const ariaLabel = `Switch to ${actionLabel} (Currently ${modeLabel})`;
  const title = override
    ? (isOppositeOverride
      ? `${modeLabel} (Manual Override — Tap for ${nextLabel}, Long-press for Auto Solar)`
      : `${modeLabel} (Manual Override — Tap or Long-press for Auto Solar)`)
    : `${modeLabel} (Auto Solar — Tap for ${nextLabel})`;

  return { mode, ariaLabel, title };
}

export function applyThemeToDOM(activeTheme: ThemeMode, doc?: Document): void {
  const d = doc !== undefined ? doc : (typeof document !== 'undefined' ? document : undefined);
  if (!d) return;
  const normalized = normalizeTheme(activeTheme);
  if (d.documentElement?.dataset) {
    d.documentElement.dataset.theme = normalized;
  }
  if (d.body?.dataset) {
    d.body.dataset.theme = normalized;
  }
  const appEl = d.getElementById('mirrormere-app');
  if (appEl?.dataset) {
    appEl.dataset.theme = normalized;
  }
  let metaTheme = d.querySelector('meta[name="theme-color"]');
  if (!metaTheme && d.head) {
    metaTheme = d.createElement('meta');
    metaTheme.setAttribute('name', 'theme-color');
    d.head.appendChild(metaTheme);
  }
  if (metaTheme) {
    metaTheme.setAttribute('content', normalized === 'night' ? '#3b364c' : '#f8f5ee');
  }
}

interface UseThemeResult {
  activeTheme: ThemeMode;
  serverTheme: ThemeMode;
  override: ThemeMode | null;
  toggle: () => void;
  resetToAuto: () => void;
  buttonAttrs: { mode: 'manual' | 'auto'; ariaLabel: string; title: string };
}

function useTheme(): UseThemeResult {
  const serverThemeRaw = useDisplayStore((s) => s.theme.theme);
  const serverTheme = normalizeTheme(serverThemeRaw);
  const [override, setOverride] = useState<ThemeMode | null>(() => getStoredThemeOverride());

  const activeTheme = resolveEffectiveTheme(serverTheme, override);

  useEffect(() => {
    applyThemeToDOM(activeTheme);
  }, [activeTheme]);

  const toggle = () => {
    setOverride((prevOverride) => {
      const result = nextThemeOnTap(activeTheme, serverTheme, prevOverride);
      setStoredThemeOverride(result.nextOverride);
      return result.nextOverride;
    });
  };

  const resetToAuto = () => {
    setStoredThemeOverride(null);
    setOverride(null);
  };

  const buttonAttrs = computeThemeButtonAttrs(activeTheme, serverTheme, override);

  return {
    activeTheme,
    serverTheme,
    override,
    toggle,
    resetToAuto,
    buttonAttrs,
  };
}



export function ThemeToggle() {
  const { activeTheme, buttonAttrs, toggle, resetToAuto } = useTheme();
  const longPressTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressTriggeredRef = useRef(false);

  const clearLongPress = () => {
    if (longPressTimerRef.current !== null) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
  };

  useEffect(() => {
    return () => {
      if (longPressTimerRef.current !== null) {
        clearTimeout(longPressTimerRef.current);
        longPressTimerRef.current = null;
      }
    };
  }, []);

  const handlePointerDown = () => {
    longPressTriggeredRef.current = false;
    clearLongPress();
    longPressTimerRef.current = setTimeout(() => {
      longPressTimerRef.current = null;
      longPressTriggeredRef.current = true;
      resetToAuto();
    }, 500);
  };

  const handlePointerUp = () => {
    clearLongPress();
  };

  const handlePointerCancel = () => {
    clearLongPress();
  };

  const handlePointerLeave = () => {
    clearLongPress();
  };

  const handleClick = (e: ReactMouseEvent) => {
    e.stopPropagation();
    if (longPressTriggeredRef.current) {
      longPressTriggeredRef.current = false;
      return;
    }
    toggle();
  };

  return (
    <div className="header-theme-toggle" id="header-theme-toggle">
      <button
        id="theme-toggle-btn"
        className="theme-toggle-btn"
        type="button"
        data-theme={activeTheme}
        data-mode={buttonAttrs.mode}
        aria-label={buttonAttrs.ariaLabel}
        title={buttonAttrs.title}
        onPointerDown={handlePointerDown}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerCancel}
        onPointerLeave={handlePointerLeave}
        onClick={handleClick}
      >
        <svg
          className="theme-icon theme-icon-sun"
          width={22}
          height={22}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx={12} cy={12} r={4.5} />
          <line x1={12} y1={2} x2={12} y2={4} />
          <line x1={12} y1={20} x2={12} y2={22} />
          <line x1={4.93} y1={4.93} x2={6.34} y2={6.34} />
          <line x1={17.66} y1={17.66} x2={19.07} y2={19.07} />
          <line x1={2} y1={12} x2={4} y2={12} />
          <line x1={20} y1={12} x2={22} y2={12} />
          <line x1={4.93} y1={19.07} x2={6.34} y2={17.66} />
          <line x1={17.66} y1={6.34} x2={19.07} y2={4.93} />
        </svg>
        <svg
          className="theme-icon theme-icon-moon"
          width={22}
          height={22}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      </button>
    </div>
  );
}
