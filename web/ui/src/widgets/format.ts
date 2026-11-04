/**
 * Shared formatting and SVG helpers for Mirrormere widgets.
 * Re-exports domain formatters from split modules.
 */

export interface TasksStatsResult {
  total: number;
  completed: number;
  remaining: number;
  overdue: number;
}

/** Escapes XML/HTML special characters for safe inclusion in SVG markup. */
export { escapeHtml } from './formatWeather';

function parseTaskDue(rawDue: unknown, nowUTC: number, nowMs: number): boolean {
  if (typeof rawDue !== 'string' || rawDue.trim() === '') return false;
  const dueStr = rawDue.trim();
  if (dueStr.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(dueStr)) {
    const [y, m, d] = dueStr.split('-').map(Number);
    const parsedUTC = Date.UTC(y, m - 1, d);
    return nowUTC > parsedUTC;
  }
  const parsed = new Date(dueStr);
  return !isNaN(parsed.getTime()) && nowMs > parsed.getTime();
}

/**
 * Calculates task completion metrics (total, completed, remaining, overdue).
 * Accepts an array of task items or an object containing an items/list array.
 * Supports both snake_case (done, due_date) and PascalCase (Done, DueDate).
 */
export function tasksStats(input: unknown, now: Date = new Date()): TasksStatsResult {
  if (!input) {
    return { total: 0, completed: 0, remaining: 0, overdue: 0 };
  }

  let items: unknown[] | null = null;
  if (Array.isArray(input)) {
    items = input;
  } else if (typeof input === 'object') {
    const obj = input as Record<string, unknown>;
    const candidate = obj.items ?? obj.Items ?? obj.list ?? obj.List;
    if (Array.isArray(candidate)) {
      items = candidate;
    }
  }

  if (!items) {
    return { total: 0, completed: 0, remaining: 0, overdue: 0 };
  }

  let total = 0;
  let completed = 0;
  let overdue = 0;

  const nowUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const nowMs = now.getTime();

  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    const task = item as Record<string, unknown>;

    // Done check
    const doneVal = task.done ?? task.Done;
    const isDone = Boolean(doneVal);
    total++;
    if (isDone) {
      completed++;
      continue;
    }

    // Overdue check on pending tasks
    const rawDue = task.due_date ?? task.DueDate ?? task.due ?? task.Due;
    if (parseTaskDue(rawDue, nowUTC, nowMs)) {
      overdue++;
    }
  }

  return {
    total,
    completed,
    remaining: Math.max(total - completed, 0),
    overdue,
  };
}

export {
  isValidTimezone,
  formatTime,
  formatDate,
  relDate,
} from './formatDate';

export {
  calendarFeedActiveCalendars,
  calendarFeedDayGroups,
  type CalendarFeedActiveCalendar,
  type CalendarFeedEvent,
  type CalendarFeedDayGroup,
} from './formatCalendar';

export {
  todayPrecipProb,
  computeHourlyGraphLayout,
  type WeatherHourlyGraphOptions,
  type HourlyGraphLayout,
  type HourlyPointLayout,
} from './formatWeather';
