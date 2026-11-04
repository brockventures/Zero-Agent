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
  const seed = `${dateStr}:${slot}`;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
  }
  return (hash % count) + 1;
}

export function resolveCharacterPackSpriteUrl(
  packName: string,
  state: ClothingConditionState,
  variantIndex: number,
  assetsBase = '/static/ui/assets/weather-packs',
): string {
  return `${assetsBase}/${packName}/${state}_v${variantIndex}.jpg`;
}
