export interface SchoolDayThresholds {
  rainThresholdPct: number; // default: 35
  coldMaxF: number;         // default: 49 (< 50F -> cold puffer coat)
  coolMaxF: number;         // default: 64 (50-64F -> hoodie/fleece + long pants)
  warmMaxF: number;         // default: 77 (65-77F -> t-shirt + long pants; >= 78F -> summer dress / hot weather outfit)
}

export const DEFAULT_SCHOOL_DAY_THRESHOLDS: SchoolDayThresholds = {
  rainThresholdPct: 35,
  coldMaxF: 49,
  coolMaxF: 64,
  warmMaxF: 77,
};

const DEFAULT_SCHOOL_DAY_ROLLOVER_HOUR = 19;

export type DaypartSlot = 'morning' | 'afternoon' | 'evening';

export type ClothingConditionState =
  | 'morning_rain'
  | 'rainy_boots'
  | 'cold_puffer'
  | 'morning_mild'
  | 'cool_evening'
  | 'warm_pants'
  | 'hot_dress';

const OUTFIT_LABELS: Record<ClothingConditionState, string> = {
  morning_rain: 'Raincoat & Umbrella',
  rainy_boots: 'Raincoat & Boots',
  cold_puffer: 'Warm Puffer Coat',
  morning_mild: 'Light Jacket & Pants',
  cool_evening: 'Hoodie or Fleece',
  warm_pants: 'T-Shirt & Long Pants',
  hot_dress: 'Shorts or Sun Dress',
};

export function resolveClothingOutfitLabel(state: ClothingConditionState): string {
  return OUTFIT_LABELS[state];
}

const CONDITION_KEYWORDS: ReadonlyArray< readonly [string, string] > = [
  ['pouring', 'Heavy Rain'],
  ['lightning', 'Thunderstorms'],
  ['snow', 'Snow'],
  ['rain', 'Rain Showers'],
  ['fog', 'Foggy'],
  ['partly-cloudy', 'Partly Cloudy'],
  ['cloudy', 'Cloudy'],
  ['clear-night', 'Clear'],
];

export function resolveIconConditionLabel(iconToken: string): string {
  const normalized = iconToken.toLowerCase();
  for (const [needle, label] of CONDITION_KEYWORDS) {
    if (normalized.includes(needle)) {
      return label;
    }
  }
  return 'Sunny';
}

export function formatSlotForecastSummary(iconToken: string, precipProbPct: number): string {
  const condition = resolveIconConditionLabel(iconToken);
  const clampedPrecip = Math.max(0, Math.min(100, Math.round(Number(precipProbPct) || 0)));
  return `${condition} · ${clampedPrecip}% rain`;
}

export function addOneCalendarDay(dateStr: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (!match) return dateStr;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const utc = new Date(Date.UTC(year, month - 1, day + 1));
  const y = utc.getUTCFullYear();
  const m = String(utc.getUTCMonth() + 1).padStart(2, '0');
  const d = String(utc.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function resolveLocalDateAndHour(
  timestamp: string,
  timezone?: string,
): { baseDate: string; hour: number } {
  const tz =
    timezone ||
    (typeof document !== 'undefined' ? document.body?.dataset?.timezone : undefined);
  if (tz && timestamp.endsWith('Z')) {
    try {
      const d = new Date(timestamp);
      if (!isNaN(d.getTime())) {
        const parts = new Intl.DateTimeFormat('en-CA', {
          timeZone: tz,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          hour12: false,
        }).formatToParts(d);
        const y = parts.find((p) => p.type === 'year')?.value;
        const m = parts.find((p) => p.type === 'month')?.value;
        const day = parts.find((p) => p.type === 'day')?.value;
        const h = parts.find((p) => p.type === 'hour')?.value;
        if (y && m && day && h !== undefined) {
          const parsedHour = parseInt(h, 10) % 24;
          return { baseDate: `${y}-${m}-${day}`, hour: parsedHour };
        }
      }
    } catch {
      // Fall back to ISO string slicing below
    }
  }
  const baseDate = timestamp.slice(0, 10);
  const hourMatch = /T(\d{2}):/.exec(timestamp);
  const hour = hourMatch ? parseInt(hourMatch[1], 10) : 0;
  return { baseDate, hour };
}

export function resolveSchoolDayTargetDate(
  config?: Record<string, unknown>,
  timestamp?: string,
  rolloverHour = DEFAULT_SCHOOL_DAY_ROLLOVER_HOUR,
): { dateStr: string; isTomorrow: boolean } {
  if (typeof config?.date === 'string' && config.date) {
    return { dateStr: config.date, isTomorrow: false };
  }
  if (typeof timestamp === 'string' && timestamp.length >= 10) {
    const configTz = typeof config?.timezone === 'string' ? config.timezone : undefined;
    const { baseDate, hour } = resolveLocalDateAndHour(timestamp, configTz);
    if (hour >= rolloverHour) {
      return { dateStr: addOneCalendarDay(baseDate), isTomorrow: true };
    }
    return { dateStr: baseDate, isTomorrow: false };
  }
  return { dateStr: '2026-10-31', isTomorrow: false };
}

export function resolveClothingConditionState(
  tempF: number,
  precipProbPct: number,
  iconToken: string,
  slot: DaypartSlot,
  thresholds: SchoolDayThresholds = DEFAULT_SCHOOL_DAY_THRESHOLDS,
): ClothingConditionState {
  const isWetIcon =
    iconToken.includes('rain') ||
    iconToken.includes('pouring') ||
    iconToken.includes('lightning');
  if (precipProbPct >= thresholds.rainThresholdPct || isWetIcon) {
    return slot === 'morning' ? 'morning_rain' : 'rainy_boots';
  }
  if (tempF <= thresholds.coldMaxF) {
    return 'cold_puffer';
  }
  if (slot === 'morning') {
    return 'morning_mild';
  }
  if (tempF <= thresholds.coolMaxF) {
    return 'cool_evening';
  }
  if (tempF <= thresholds.warmMaxF) {
    return 'warm_pants';
  }
  return 'hot_dress';
}

export function selectDailyVariantIndex(
  dateStr: string,
  slot: DaypartSlot,
  variantsCount = 5,
): number {
  const count = Math.max(1, variantsCount);
  let hash = 0;
  for (let i = 0; i < dateStr.length; i++) {
    hash = (hash * 31 + dateStr.charCodeAt(i)) >>> 0;
  }
  const slotOffset = slot === 'morning' ? 0 : slot === 'afternoon' ? 1 : 2;
  return ((hash + slotOffset) % count) + 1;
}

interface BirthdayConfigEntry {
  key?: string;
  id?: string;
  name?: string;
  date?: string;
  month_day?: string;
  month?: number;
  day?: number;
  label?: string;
  banner_label?: string;
  outfit_label?: string;
}

export interface BirthdayMatch {
  key: string;
  name: string;
  bannerLabel: string;
  outfitLabel: string;
}

function normalizeMonthDay(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const trimmed = raw.trim();
  const fullMatch = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(trimmed);
  if (fullMatch) {
    return `${fullMatch[2].padStart(2, '0')}-${fullMatch[3].padStart(2, '0')}`;
  }
  const mdMatch = /^(\d{1,2})[-/](\d{1,2})$/.exec(trimmed);
  if (mdMatch) {
    return `${mdMatch[1].padStart(2, '0')}-${mdMatch[2].padStart(2, '0')}`;
  }
  return undefined;
}

function sanitizeBirthdayKey(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

function formatBirthdayMatch(
  keyRaw: string,
  nameRaw: string,
  bannerRaw?: string,
  outfitRaw?: string,
): BirthdayMatch | undefined {
  const key = sanitizeBirthdayKey(keyRaw);
  if (!key) return undefined;
  const name = nameRaw.trim() || key.charAt(0).toUpperCase() + key.slice(1);
  const bannerLabel =
    typeof bannerRaw === 'string' && bannerRaw.trim()
      ? bannerRaw.trim()
      : `🎂 Happy Birthday ${name}!`;
  const outfitLabel =
    typeof outfitRaw === 'string' && outfitRaw.trim()
      ? outfitRaw.trim()
      : `🎂 Birthday Celebration!`;
  return { key, name, bannerLabel, outfitLabel };
}

function matchBirthdayEntryObject(
  entry: BirthdayConfigEntry & Record<string, unknown>,
  targetMonthDay: string,
  fallbackKey?: string,
): BirthdayMatch | undefined {
  let entryMonthDay = normalizeMonthDay(entry.date ?? entry.month_day);
  if (
    !entryMonthDay &&
    typeof entry.month === 'number' &&
    typeof entry.day === 'number'
  ) {
    entryMonthDay = `${String(entry.month).padStart(2, '0')}-${String(entry.day).padStart(2, '0')}`;
  }
  if (!entryMonthDay && fallbackKey) {
    entryMonthDay = normalizeMonthDay(fallbackKey);
  }
  if (entryMonthDay !== targetMonthDay) return undefined;

  const rawName =
    typeof entry.name === 'string'
      ? entry.name
      : typeof entry.key === 'string'
        ? entry.key
        : typeof entry.id === 'string'
          ? entry.id
          : fallbackKey && !normalizeMonthDay(fallbackKey)
            ? fallbackKey
            : '';
  const rawKey =
    typeof entry.key === 'string'
      ? entry.key
      : typeof entry.id === 'string'
        ? entry.id
        : rawName;
  const bannerRaw =
    typeof entry.banner_label === 'string'
      ? entry.banner_label
      : typeof entry.label === 'string'
        ? entry.label
        : undefined;
  const outfitRaw = typeof entry.outfit_label === 'string' ? entry.outfit_label : undefined;
  return formatBirthdayMatch(rawKey, rawName, bannerRaw, outfitRaw);
}

export function resolveBirthdayOverride(
  dateStr?: string,
  birthdaysConfig?: unknown,
): BirthdayMatch | undefined {
  if (!dateStr || !birthdaysConfig || typeof birthdaysConfig === 'boolean') {
    return undefined;
  }
  const targetMonthDay = normalizeMonthDay(dateStr);
  if (!targetMonthDay) return undefined;

  if (Array.isArray(birthdaysConfig)) {
    for (const item of birthdaysConfig) {
      if (item && typeof item === 'object' && !Array.isArray(item)) {
        const match = matchBirthdayEntryObject(
          item as BirthdayConfigEntry & Record<string, unknown>,
          targetMonthDay,
        );
        if (match) return match;
      }
    }
    return undefined;
  }

  if (typeof birthdaysConfig === 'object') {
    const map = birthdaysConfig as Record<string, unknown>;
    for (const [k, v] of Object.entries(map)) {
      if (typeof v === 'string') {
        const kAsDate = normalizeMonthDay(k);
        const vAsDate = normalizeMonthDay(v);
        if (vAsDate === targetMonthDay) {
          const match = formatBirthdayMatch(k, k);
          if (match) return match;
        } else if (kAsDate === targetMonthDay) {
          const match = formatBirthdayMatch(v, v);
          if (match) return match;
        }
      } else if (v && typeof v === 'object' && !Array.isArray(v)) {
        const match = matchBirthdayEntryObject(
          v as BirthdayConfigEntry & Record<string, unknown>,
          targetMonthDay,
          k,
        );
        if (match) return match;
      }
    }
  }

  return undefined;
}

export function resolveBirthdayPackSpriteUrl(
  packName: string,
  birthdayKey: string,
  slot: DaypartSlot,
  assetsBase = '/static/ui/assets/weather-packs',
): string {
  return `${assetsBase}/${packName}/birthday_${birthdayKey}_${slot}.jpg`;
}

const HOLIDAY_SUPPORTED_STATES: Record<string, ReadonlySet<ClothingConditionState>> = {
  new_years: new Set(['cold_puffer', 'cool_evening', 'morning_mild', 'morning_rain', 'rainy_boots']),
  valentines: new Set(['cold_puffer', 'cool_evening', 'morning_mild', 'morning_rain', 'rainy_boots', 'warm_pants']),
  easter: new Set(['cold_puffer', 'cool_evening', 'hot_dress', 'morning_mild', 'morning_rain', 'rainy_boots', 'warm_pants']),
  memorial_day: new Set(['cool_evening', 'hot_dress', 'morning_mild', 'rainy_boots', 'warm_pants']),
  july_4th: new Set(['cool_evening', 'hot_dress', 'morning_mild', 'warm_pants']),
  labor_day: new Set(['cool_evening', 'hot_dress', 'morning_mild', 'warm_pants']),
  halloween: new Set(['cold_puffer', 'cool_evening', 'morning_mild', 'morning_rain', 'rainy_boots', 'warm_pants']),
  thanksgiving: new Set(['cold_puffer', 'cool_evening', 'morning_mild', 'morning_rain', 'rainy_boots', 'warm_pants']),
  christmas: new Set(['cold_puffer', 'cool_evening', 'morning_mild', 'morning_rain', 'rainy_boots']),
};

function computeEasterMonthDay(year: number): [number, number] {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return [month, day];
}

function resolveUSHolidayKey(dateStr?: string): string | undefined {
  if (!dateStr) return undefined;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  if (!match) return undefined;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month === 1 && day === 1) return 'new_years';
  if (month === 2 && day === 14) return 'valentines';
  if (month === 7 && day === 4) return 'july_4th';
  if (month === 10 && day === 31) return 'halloween';
  if (month === 12 && (day === 24 || day === 25)) return 'christmas';
  const [eMonth, eDay] = computeEasterMonthDay(year);
  if (month === eMonth && day === eDay) return 'easter';
  const dow = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  if (month === 5 && dow === 1 && day >= 25) return 'memorial_day';
  if (month === 9 && dow === 1 && day <= 7) return 'labor_day';
  if (month === 11 && dow === 4 && day >= 22 && day <= 28) return 'thanksgiving';
  return undefined;
}

export function resolveCharacterPackSpriteUrl(
  packName: string,
  state: ClothingConditionState,
  variantIndex: number,
  dateStrOrAssetsBase?: string,
  enableHolidays?: boolean,
  assetsBase = '/static/ui/assets/weather-packs',
): string {
  const isLegacyBase =
    typeof dateStrOrAssetsBase === 'string' &&
    (dateStrOrAssetsBase.startsWith('/') || dateStrOrAssetsBase.startsWith('http'));
  const base = isLegacyBase ? dateStrOrAssetsBase : assetsBase;
  const dateStr = isLegacyBase ? undefined : dateStrOrAssetsBase;
  if (enableHolidays) {
    const holidayKey = resolveUSHolidayKey(dateStr);
    if (holidayKey && HOLIDAY_SUPPORTED_STATES[holidayKey]?.has(state)) {
      return `${base}/${packName}/holiday_${holidayKey}_${state}.jpg`;
    }
  }
  return `${base}/${packName}/${state}_v${variantIndex}.jpg`;
}
