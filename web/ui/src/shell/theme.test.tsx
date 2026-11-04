import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../store';
import {
  STORAGE_KEY_THEME_OVERRIDE,
  ThemeToggle,
  applyThemeToDOM,
  computeThemeButtonAttrs,
  getStoredThemeOverride,
  nextThemeOnTap,
  normalizeTheme,
  resolveEffectiveTheme,
  setStoredThemeOverride,
} from './theme';

describe('theme pure helpers', () => {
  it('normalizeTheme maps values correctly', () => {
    expect(normalizeTheme('night')).toBe('night');
    expect(normalizeTheme('day')).toBe('day');
    expect(normalizeTheme(null)).toBe('day');
    expect(normalizeTheme(undefined)).toBe('day');
    expect(normalizeTheme('other')).toBe('day');
  });

  it('getStoredThemeOverride and setStoredThemeOverride interact with storage correctly', () => {
    const mockStorage: Record<string, string> = {};
    const storage = {
      getItem: (key: string) => mockStorage[key] ?? null,
      setItem: (key: string, val: string) => {
        mockStorage[key] = val;
      },
      removeItem: (key: string) => {
        delete mockStorage[key];
      },
    };

    expect(getStoredThemeOverride(storage)).toBeNull();

    setStoredThemeOverride('night', storage);
    expect(getStoredThemeOverride(storage)).toBe('night');

    setStoredThemeOverride('day', storage);
    expect(getStoredThemeOverride(storage)).toBe('day');

    setStoredThemeOverride(null, storage);
    expect(getStoredThemeOverride(storage)).toBeNull();

    // Exception handling resilience (SecurityError/QuotaExceededError)
    const throwingStorage = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('SecurityError');
      },
    };
    expect(getStoredThemeOverride(throwingStorage)).toBeNull();
    expect(() => setStoredThemeOverride('day', throwingStorage)).not.toThrow();
    expect(() => setStoredThemeOverride(null, throwingStorage)).not.toThrow();
  });

  it('resolveEffectiveTheme prioritizes override over serverTheme', () => {
    expect(resolveEffectiveTheme('day', 'night')).toBe('night');
    expect(resolveEffectiveTheme('night', 'day')).toBe('day');
    expect(resolveEffectiveTheme('night', null)).toBe('night');
    expect(resolveEffectiveTheme('day', null)).toBe('day');
  });

  it('nextThemeOnTap follows the 3-step cycle', () => {
    // Server theme is 'day'
    // Step 0: auto 'day', override null -> tap sets override to 'night'
    const step1 = nextThemeOnTap('day', 'day', null);
    expect(step1).toEqual({ activeTheme: 'night', nextOverride: 'night' });

    // Step 1: active 'night', override 'night' -> tap sets override to 'day' (same as server)
    const step2 = nextThemeOnTap('night', 'day', 'night');
    expect(step2).toEqual({ activeTheme: 'day', nextOverride: 'day' });

    // Step 2: active 'day', override 'day' -> tap resets override to null (Auto Solar)
    const step3 = nextThemeOnTap('day', 'day', 'day');
    expect(step3).toEqual({ activeTheme: 'day', nextOverride: null });

    // Server theme is 'night'
    const nightStep1 = nextThemeOnTap('night', 'night', null);
    expect(nightStep1).toEqual({ activeTheme: 'day', nextOverride: 'day' });

    const nightStep2 = nextThemeOnTap('day', 'night', 'day');
    expect(nightStep2).toEqual({ activeTheme: 'night', nextOverride: 'night' });

    const nightStep3 = nextThemeOnTap('night', 'night', 'night');
    expect(nightStep3).toEqual({ activeTheme: 'night', nextOverride: null });
  });

  it('computeThemeButtonAttrs creates proper aria-label and title', () => {
    const autoAttrs = computeThemeButtonAttrs('day', 'day', null);
    expect(autoAttrs.mode).toBe('auto');
    expect(autoAttrs.ariaLabel).toBe('Switch to Dark Mode (Currently Light Mode)');
    expect(autoAttrs.title).toContain('Auto Solar');

    const manualAttrs = computeThemeButtonAttrs('night', 'day', 'night');
    expect(manualAttrs.mode).toBe('manual');
    expect(manualAttrs.title).toContain('Manual Override — Tap for Light Mode, Long-press for Auto Solar');

    const sameManualAttrs = computeThemeButtonAttrs('day', 'day', 'day');
    expect(sameManualAttrs.title).toContain('Manual Override — Tap or Long-press for Auto Solar');
  });
});

describe('applyThemeToDOM', () => {
  let meta: HTMLMetaElement;
  let appEl: HTMLDivElement;

  beforeEach(() => {
    meta = document.createElement('meta');
    meta.setAttribute('name', 'theme-color');
    document.head.appendChild(meta);

    appEl = document.createElement('div');
    appEl.id = 'mirrormere-app';
    document.body.appendChild(appEl);
  });

  afterEach(() => {
    if (meta.parentNode) meta.parentNode.removeChild(meta);
    if (appEl.parentNode) appEl.parentNode.removeChild(appEl);
    delete document.documentElement.dataset.theme;
    delete document.body.dataset.theme;
  });

  it('sets data-theme on html, body, and app, and updates meta theme-color', () => {
    applyThemeToDOM('night', document);
    expect(document.documentElement.dataset.theme).toBe('night');
    expect(document.body.dataset.theme).toBe('night');
    expect(appEl.dataset.theme).toBe('night');
    expect(meta.getAttribute('content')).toBe('#3b364c');

    applyThemeToDOM('day', document);
    expect(document.documentElement.dataset.theme).toBe('day');
    expect(document.body.dataset.theme).toBe('day');
    expect(appEl.dataset.theme).toBe('day');
    expect(meta.getAttribute('content')).toBe('#f8f5ee');

    // Creates meta theme-color tag if none exists
    meta.remove();
    applyThemeToDOM('night', document);
    const createdMeta = document.querySelector('meta[name="theme-color"]');
    expect(createdMeta).not.toBeNull();
    expect(createdMeta?.getAttribute('content')).toBe('#3b364c');
    createdMeta?.remove();
  });
});

describe('ThemeToggle component', () => {
  let appEl: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.removeItem(STORAGE_KEY_THEME_OVERRIDE);
    useDisplayStore.setState({
      theme: { theme: 'day' },
    });
    appEl = document.createElement('div');
    appEl.id = 'mirrormere-app';
    document.body.appendChild(appEl);
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.removeItem(STORAGE_KEY_THEME_OVERRIDE);
    if (appEl.parentNode) appEl.parentNode.removeChild(appEl);
    delete document.documentElement.dataset.theme;
    delete document.body.dataset.theme;
  });

  it('renders button with correct sun/moon icons and initial auto attributes', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.dataset.theme).toBe('day');
    expect(btn.dataset.mode).toBe('auto');

    expect(btn.querySelector('.theme-icon-sun')).not.toBeNull();
    expect(btn.querySelector('.theme-icon-moon')).not.toBeNull();
  });

  it('executes 3-step tap-to-toggle cycle on clicks', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;

    // Click 1: Day -> Night (manual override)
    fireEvent.click(btn);
    expect(btn.dataset.theme).toBe('night');
    expect(btn.dataset.mode).toBe('manual');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBe('night');

    // Click 2: Night -> Day (manual override)
    fireEvent.click(btn);
    expect(btn.dataset.theme).toBe('day');
    expect(btn.dataset.mode).toBe('manual');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBe('day');

    // Click 3: Day -> Auto Solar (resets override)
    fireEvent.click(btn);
    expect(btn.dataset.theme).toBe('day');
    expect(btn.dataset.mode).toBe('auto');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBeNull();
  });

  it('retains manual override across server theme updates', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;

    // Set manual override to night
    fireEvent.click(btn);
    expect(btn.dataset.theme).toBe('night');

    // Server sends theme update with 'day'
    act(() => {
      useDisplayStore.setState({ theme: { theme: 'day' } });
    });
    expect(btn.dataset.theme).toBe('night');

    // Server sends theme update with 'night'
    act(() => {
      useDisplayStore.setState({ theme: { theme: 'night' } });
    });
    expect(btn.dataset.theme).toBe('night');
  });

  it('resets to auto on 500ms long-press and suppresses subsequent click', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;

    // Tap once to enter manual override
    fireEvent.click(btn);
    expect(btn.dataset.mode).toBe('manual');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBe('night');

    // Start long-press
    fireEvent.pointerDown(btn);

    // Advance 500ms to trigger long-press reset
    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(btn.dataset.mode).toBe('auto');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBeNull();

    // Release pointer and trigger click; click should be suppressed
    fireEvent.pointerUp(btn);
    fireEvent.click(btn);

    // Mode should still be auto because click was suppressed
    expect(btn.dataset.mode).toBe('auto');
    expect(window.localStorage.getItem(STORAGE_KEY_THEME_OVERRIDE)).toBeNull();
  });

  it('cancels long-press when pointer leaves before 500ms', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;

    // Tap once to enter manual override
    fireEvent.click(btn);
    expect(btn.dataset.mode).toBe('manual');

    // Pointer down, then leave at 200ms
    fireEvent.pointerDown(btn);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    fireEvent.pointerLeave(btn);

    // Advance past 500ms
    act(() => {
      vi.advanceTimersByTime(400);
    });

    // Still manual, because long-press was cancelled
    expect(btn.dataset.mode).toBe('manual');
  });

  it('cancels long-press when pointer cancels before 500ms', () => {
    const { container } = render(<ThemeToggle />);
    const btn = container.querySelector('#theme-toggle-btn') as HTMLButtonElement;

    fireEvent.click(btn);
    expect(btn.dataset.mode).toBe('manual');

    fireEvent.pointerDown(btn);
    act(() => {
      vi.advanceTimersByTime(200);
    });
    fireEvent.pointerCancel(btn);

    act(() => {
      vi.advanceTimersByTime(400);
    });

    expect(btn.dataset.mode).toBe('manual');
  });
});
