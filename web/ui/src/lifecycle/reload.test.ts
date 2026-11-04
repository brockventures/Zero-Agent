import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyStyleReload,
  createReloadController,
  DEPLOY_RELOAD_COOLDOWN_MS,
  STORAGE_KEY_DAILY_RELOAD,
  STORAGE_KEY_DEPLOY_RELOAD,
  wallClock,
  type ReloadEnvironment,
} from './reload';

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
    data,
  };
}

function makeEnv(overrides: Partial<ReloadEnvironment> = {}) {
  const reload = vi.fn();
  const storage = memoryStorage();
  const env: ReloadEnvironment = {
    now: () => Date.now(),
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (t) => clearTimeout(t),
    reload,
    storage,
    ...overrides,
  };
  return { env, reload, storage };
}

describe('boot_id deployment reload', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: new Date('2026-10-31T12:00:00Z') });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('records the first boot_id without reloading', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemStatus({ boot_id: 'a' });
    expect(reload).not.toHaveBeenCalled();
  });

  it('reloads on a changed boot_id once the cooldown has passed', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemStatus({ boot_id: 'a' });
    controller.handleSystemStatus({ boot_id: 'b' });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('defers a second change inside the 30 s cooldown and reloads once when it expires', () => {
    const { env, reload, storage } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemStatus({ boot_id: 'a' });
    controller.handleSystemStatus({ boot_id: 'b' });
    expect(reload).toHaveBeenCalledTimes(1);
    expect(storage.data.get(STORAGE_KEY_DEPLOY_RELOAD)).toBe(String(Date.now()));

    vi.advanceTimersByTime(5_000);
    controller.handleSystemStatus({ boot_id: 'c' });
    expect(reload).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(DEPLOY_RELOAD_COOLDOWN_MS - 5_000 - 1);
    expect(reload).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it('cancels a deferred reload when the original boot_id comes back', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemStatus({ boot_id: 'a' });
    controller.handleSystemStatus({ boot_id: 'b' });
    controller.handleSystemStatus({ boot_id: 'c' });
    controller.handleSystemStatus({ boot_id: 'b' });

    vi.advanceTimersByTime(DEPLOY_RELOAD_COOLDOWN_MS * 2);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('respects a reload recorded by a previous page load in sessionStorage', () => {
    const storage = memoryStorage();
    storage.setItem(STORAGE_KEY_DEPLOY_RELOAD, String(Date.now() - 1_000));
    const { env, reload } = makeEnv({ storage });
    const controller = createReloadController(env);
    controller.handleSystemStatus({ boot_id: 'a' });
    controller.handleSystemStatus({ boot_id: 'b' });
    expect(reload).not.toHaveBeenCalled();
    vi.advanceTimersByTime(DEPLOY_RELOAD_COOLDOWN_MS);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('ignores status messages without a boot_id', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemStatus({});
    controller.handleSystemStatus({ boot_id: 'a' });
    controller.handleSystemStatus({});
    expect(reload).not.toHaveBeenCalled();
  });
});

describe('system.reload debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(console, 'info').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('reloads once after a burst of events', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    controller.handleSystemReload({ reason: 'shell_change' });
    vi.advanceTimersByTime(500);
    controller.handleSystemReload({ reason: 'shell_change' });
    vi.advanceTimersByTime(999);
    expect(reload).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('reloads immediately when the delay is zero', () => {
    const { env, reload } = makeEnv();
    createReloadController(env).handleSystemReload({ reason: 'manual' }, 0);
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe('4:00 AM maintenance reload', () => {
  it('reloads at 04:00 in the household timezone, once per day', () => {
    const { env, reload, storage } = makeEnv();
    const controller = createReloadController(env);
    // 2026-10-31 04:00 in America/Los_Angeles (PDT, UTC-7).
    const fourAm = new Date('2026-10-31T11:00:00Z');

    expect(controller.checkDailyMaintenance(fourAm, 'America/Los_Angeles')).toBe(true);
    expect(controller.checkDailyMaintenance(new Date('2026-10-31T11:00:30Z'), 'America/Los_Angeles')).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(storage.data.get(STORAGE_KEY_DAILY_RELOAD)).toBe('10/31/2026');
  });

  it('does not reload at other times', () => {
    const { env, reload } = makeEnv();
    const controller = createReloadController(env);
    expect(controller.checkDailyMaintenance(new Date('2026-10-31T11:01:00Z'), 'America/Los_Angeles')).toBe(false);
    expect(controller.checkDailyMaintenance(new Date('2026-10-31T10:00:00Z'), 'America/Los_Angeles')).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it('skips a day already recorded in sessionStorage', () => {
    const storage = memoryStorage();
    storage.setItem(STORAGE_KEY_DAILY_RELOAD, '10/31/2026');
    const { env, reload } = makeEnv({ storage });
    expect(createReloadController(env).checkDailyMaintenance(new Date('2026-10-31T11:00:00Z'), 'America/Los_Angeles')).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });

  it('falls back to local time for an invalid timezone', () => {
    const local = new Date(2026, 9, 31, 4, 0, 0);
    expect(wallClock(local, 'Not/AZone')).toMatchObject({ hour: 4, minute: 0 });
  });
});

describe('applyStyleReload', () => {
  it('swaps the cache-buster on #hud-stylesheet', () => {
    document.body.innerHTML = '<link rel="stylesheet" id="hud-stylesheet" href="style.css">';
    expect(applyStyleReload(document, 123)).toBe(true);
    expect((document.getElementById('hud-stylesheet') as HTMLLinkElement).href).toMatch(/style\.css\?t=123$/);

    applyStyleReload(document, 456);
    expect((document.getElementById('hud-stylesheet') as HTMLLinkElement).href).toMatch(/style\.css\?t=456$/);
    document.body.innerHTML = '';
  });

  it('does nothing when the stylesheet link is absent', () => {
    document.body.innerHTML = '';
    expect(applyStyleReload(document, 1)).toBe(false);
  });
});
