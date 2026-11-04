import type { SystemReloadPayload, SystemStatusPayload } from '../sse/types';

/**
 * Reload behaviour ported from web/static/js/display.js: deployment detection
 * via boot_id (30 s cooldown), debounced system.reload, the style.reload
 * stylesheet cache-buster, and the 4:00 AM maintenance reload.
 */

export const DEPLOY_RELOAD_COOLDOWN_MS = 30_000;
const SYSTEM_RELOAD_DEBOUNCE_MS = 1_000;
const MAINTENANCE_HOUR = 4;
export const MAINTENANCE_CHECK_INTERVAL_MS = 15_000;

export const STORAGE_KEY_DEPLOY_RELOAD = 'mm_last_reload_timestamp';
export const STORAGE_KEY_DAILY_RELOAD = 'mm_last_daily_reload_date';

interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface ReloadEnvironment {
  now: () => number;
  setTimer: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer: (timer: ReturnType<typeof setTimeout>) => void;
  reload: () => void;
  /** sessionStorage in the browser; null when unavailable. */
  storage: StorageLike | null;
}

export interface ReloadController {
  handleSystemStatus(data: Pick<SystemStatusPayload, 'boot_id'>): void;
  handleSystemReload(data: Pick<SystemReloadPayload, 'reason'>, delayMs?: number): void;
  /** Reloads at 04:00 in `timezone` once per calendar day. Returns true when it reloaded. */
  checkDailyMaintenance(now: Date, timezone: string): boolean;
  dispose(): void;
}

export function createReloadController(env: ReloadEnvironment): ReloadController {
  let currentBootId: string | null = null;
  let lastReloadTimestamp = 0;
  let pendingBootId: string | null = null;
  let pendingDeployTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingSystemTimer: ReturnType<typeof setTimeout> | null = null;
  let lastDailyReloadDate = '';

  const readNumber = (key: string): number => {
    const raw = env.storage?.getItem(key);
    const parsed = parseInt(raw ?? '0', 10);
    return Number.isNaN(parsed) ? 0 : parsed;
  };

  const clearDeployTimer = () => {
    if (pendingDeployTimer !== null) {
      env.clearTimer(pendingDeployTimer);
      pendingDeployTimer = null;
    }
    pendingBootId = null;
  };

  const markDeployReload = (at: number) => {
    lastReloadTimestamp = at;
    env.storage?.setItem(STORAGE_KEY_DEPLOY_RELOAD, String(at));
  };

  function handleSystemStatus(data: Pick<SystemStatusPayload, 'boot_id'>): void {
    const bootId = data.boot_id;
    if (!bootId) return;

    if (currentBootId === null) {
      currentBootId = bootId;
      return;
    }

    if (currentBootId !== bootId) {
      const now = env.now();
      const effectiveLast = Math.max(lastReloadTimestamp, readNumber(STORAGE_KEY_DEPLOY_RELOAD));
      const elapsed = now - effectiveLast;

      if (elapsed >= DEPLOY_RELOAD_COOLDOWN_MS) {
        clearDeployTimer();
        markDeployReload(now);
        currentBootId = bootId;
        env.reload();
        return;
      }

      // Crash-looping server: defer until the cooldown expires so rapid restarts are not dropped.
      pendingBootId = bootId;
      if (pendingDeployTimer === null) {
        const delay = Math.max(0, DEPLOY_RELOAD_COOLDOWN_MS - elapsed);
        pendingDeployTimer = env.setTimer(() => {
          pendingDeployTimer = null;
          const target = pendingBootId;
          pendingBootId = null;
          if (target !== null && target !== currentBootId) {
            const at = env.now();
            markDeployReload(at);
            currentBootId = target;
            env.reload();
          }
        }, delay);
      }
      return;
    }

    // boot_id reverted to the one we already run: cancel a deferred reload.
    clearDeployTimer();
  }

  function handleSystemReload(data: Pick<SystemReloadPayload, 'reason'>, delayMs = SYSTEM_RELOAD_DEBOUNCE_MS): void {
    console.info(`[mirrormere] server-instructed reload (${data.reason})`);
    if (pendingSystemTimer !== null) {
      env.clearTimer(pendingSystemTimer);
      pendingSystemTimer = null;
    }
    if (delayMs <= 0) {
      env.reload();
      return;
    }
    // Each call restarts the debounce window, so a burst of events reloads once.
    pendingSystemTimer = env.setTimer(() => {
      pendingSystemTimer = null;
      env.reload();
    }, delayMs);
  }

  function checkDailyMaintenance(now: Date, timezone: string): boolean {
    const { hour, minute, dateStr } = wallClock(now, timezone);
    const storedDate = env.storage?.getItem(STORAGE_KEY_DAILY_RELOAD) ?? '';
    const effectiveDate = lastDailyReloadDate || storedDate;

    if (hour === MAINTENANCE_HOUR && minute === 0 && effectiveDate !== dateStr) {
      lastDailyReloadDate = dateStr;
      env.storage?.setItem(STORAGE_KEY_DAILY_RELOAD, dateStr);
      env.reload();
      return true;
    }
    return false;
  }

  function dispose(): void {
    clearDeployTimer();
    if (pendingSystemTimer !== null) {
      env.clearTimer(pendingSystemTimer);
      pendingSystemTimer = null;
    }
  }

  return { handleSystemStatus, handleSystemReload, checkDailyMaintenance, dispose };
}

/** Wall-clock hour, minute and date in `timezone`; falls back to local time if the zone is invalid. */
export function wallClock(now: Date, timezone: string): { hour: number; minute: number; dateStr: string } {
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      hour: 'numeric',
      minute: 'numeric',
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
    });
    const parts = Object.fromEntries(fmt.formatToParts(now).map((p) => [p.type, p.value]));
    return {
      hour: parseInt(parts.hour ?? '0', 10),
      minute: parseInt(parts.minute ?? '0', 10),
      dateStr: `${parts.month}/${parts.day}/${parts.year}`,
    };
  } catch {
    return {
      hour: now.getHours(),
      minute: now.getMinutes(),
      dateStr: now.toLocaleDateString('en-US'),
    };
  }
}

/**
 * Hot-swap the stylesheet link on style.reload. The ?t= cache-buster forces a
 * refetch without removing the element, so there is no flash of unstyled content.
 */
export function applyStyleReload(doc: Document, now: number = Date.now()): boolean {
  const link = doc.getElementById('hud-stylesheet');
  if (!(link instanceof HTMLLinkElement)) return false;
  const base = link.href.split('?')[0];
  link.href = `${base}?t=${now}`;
  return true;
}
