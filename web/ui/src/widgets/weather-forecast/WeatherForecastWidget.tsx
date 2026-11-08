import type { CSSProperties } from 'react';
import './WeatherForecastWidget.css';
import {
  DEFAULT_SCHOOL_DAY_THRESHOLDS, type DaypartSlot, type SchoolDayThresholds,
  formatSlotForecastSummary, resolveBirthdayOverride, resolveBirthdayPackSpriteUrl,
  resolveCharacterPackSpriteUrl, resolveClothingConditionState, resolveClothingOutfitLabel,
  resolveSchoolDayTargetDate, selectDailyVariantIndex,
} from './schoolDayPack';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import { todayPrecipProb } from '../format';
import { WeatherIcon } from './WeatherIcon';
import { WeatherHourlyGraph } from './WeatherHourlyGraph';

interface SlotWeatherInfo { temp: number; precipProb: number; icon: string; }
interface SchoolDaySlotItem {
  slot: DaypartSlot; displayLabel: string; isTomorrow: boolean; temp: number;
  state: string; outfitLabel: string; forecastSummary: string; spriteUrl: string;
  birthdayBanner?: string;
}
const DAYPART_SLOTS: DaypartSlot[] = ['morning', 'afternoon', 'evening'];

function resolveLocationName(config?: Record<string, unknown>, data?: Record<string, unknown>): string {
  const candidates = [config?.name, config?.location_name, data?.location, data?.Location];
  for (const c of candidates) {
    if (typeof c === 'string' && c.trim() !== '') return c.trim();
  }
  return '';
}
function extractCurrentCandidate(data?: Record<string, unknown>): Record<string, unknown> | undefined {
  const candidate = data?.current ?? data?.Current ?? (data?.temperature !== undefined ? data : undefined);
  if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
    return candidate as Record<string, unknown>;
  }
  return undefined;
}
function extractTemperature(current?: Record<string, unknown>): { roundedTemp: number; tempUnit: string } {
  const rawTemp = current?.temperature ?? current?.Temperature ?? current?.temp ?? current?.Temp;
  const numTemp = typeof rawTemp === 'number' ? rawTemp : Number(rawTemp) || 0;
  const roundedTemp = Math.sign(numTemp) * Math.round(Math.abs(numTemp)) || 0;
  const units = (current?.units ?? current?.Units) as Record<string, unknown> | undefined;
  const tempUnit = String(units?.temperature ?? units?.Temperature ?? '°');
  return { roundedTemp, tempUnit };
}

function extractDailyRange(data?: Record<string, unknown>): { hasDaily: boolean; maxTemp: number; minTemp: number } {
  const daily = (data?.daily ?? data?.Daily) as unknown[] | undefined;
  const today = Array.isArray(daily) && daily.length > 0 && typeof daily[0] === 'object'
    ? (daily[0] as Record<string, unknown>) : undefined;
  const rawMax = today?.temp_max ?? today?.TempMax ?? today?.max_temp ?? today?.MaxTemp;
  const rawMin = today?.temp_min ?? today?.TempMin ?? today?.min_temp ?? today?.MinTemp;
  const hasDaily = rawMax !== undefined && rawMin !== undefined;
  const maxTemp = hasDaily ? Math.sign(Number(rawMax) || 0) * Math.round(Math.abs(Number(rawMax) || 0)) || 0 : 0;
  const minTemp = hasDaily ? Math.sign(Number(rawMin) || 0) * Math.round(Math.abs(Number(rawMin) || 0)) || 0 : 0;
  return { hasDaily, maxTemp, minTemp };
}

function extractWindAndHumidity(current?: Record<string, unknown>): {
  humidity: string; windSpeed: number; windUnit: string;
} {
  const rawHumidity = current?.humidity ?? current?.Humidity;
  const humidity = rawHumidity !== undefined ? `${rawHumidity}%` : '--%';
  const rawWind = current?.wind_speed ?? current?.WindSpeed ?? current?.wind ?? current?.Wind;
  const windSpeed = Math.sign(Number(rawWind) || 0) * Math.round(Math.abs(Number(rawWind) || 0)) || 0;
  const units = (current?.units ?? current?.Units) as Record<string, unknown> | undefined;
  const windUnit = String(units?.wind_speed ?? units?.WindSpeed ?? 'mph');
  return { humidity, windSpeed, windUnit };
}

function extractSchoolDayConfig(config?: Record<string, unknown>): {
  characterPack: string; variantsPerState: number; thresholds: SchoolDayThresholds;
} {
  const rawPack = typeof config?.character_pack === 'string' ? config.character_pack.trim() : '';
  const characterPack = rawPack && rawPack.toLowerCase() !== 'none'
    ? rawPack.replace(/[^a-zA-Z0-9_-]/g, '') : 'ghibli_school_day';
  const variantsPerState = typeof config?.variants_per_state === 'number' && config.variants_per_state >= 1
    ? Math.floor(config.variants_per_state) : 5;
  const numOr = (val: unknown, fallback: number) => (typeof val === 'number' ? val : fallback);
  const thresholds: SchoolDayThresholds = {
    rainThresholdPct: numOr(config?.rain_threshold_pct, DEFAULT_SCHOOL_DAY_THRESHOLDS.rainThresholdPct),
    coldMaxF: numOr(config?.cold_max_f, DEFAULT_SCHOOL_DAY_THRESHOLDS.coldMaxF),
    coolMaxF: numOr(config?.cool_max_f, DEFAULT_SCHOOL_DAY_THRESHOLDS.coolMaxF),
    warmMaxF: numOr(config?.warm_max_f, DEFAULT_SCHOOL_DAY_THRESHOLDS.warmMaxF),
  };
  return { characterPack, variantsPerState, thresholds };
}

function findExplicitSlot(slot: DaypartSlot, data?: Record<string, unknown>): SlotWeatherInfo | undefined {
  const slots = data?.slots as Record<string, Record<string, unknown>> | undefined;
  const slotObj = slots?.[slot] ?? (data?.[slot] as Record<string, unknown> | undefined);
  if (!slotObj) return undefined;
  return {
    temp: Math.round(Number(slotObj.temp ?? slotObj.temperature ?? 0)),
    precipProb: Math.round(Number(slotObj.precip_prob ?? slotObj.precip ?? 0)),
    icon: String(slotObj.icon ?? 'weather-sunny'),
  };
}

function filterHourlyByDate(hourly: unknown[], targetDateStr?: string): unknown[] {
  if (!targetDateStr) return hourly;
  const matching = hourly.filter((item) => {
    if (!item || typeof item !== 'object') return false;
    const h = item as Record<string, unknown>;
    return String(h.time ?? h.Time ?? '').startsWith(targetDateStr);
  });
  return matching.length > 0 ? matching : hourly;
}

function findBestHourlySlot(slot: DaypartSlot, hourly: unknown[], targetDateStr?: string): SlotWeatherInfo {
  const pool = filterHourlyByDate(hourly, targetDateStr);
  const targetHour = slot === 'morning' ? 8 : slot === 'afternoon' ? 14 : 18;
  let bestItem: Record<string, unknown> | undefined;
  let bestDiff = Infinity;

  for (const item of pool) {
    if (!item || typeof item !== 'object') continue;
    const h = item as Record<string, unknown>;
    const match = String(h.time ?? h.Time ?? '').match(/(?:T|\s|^)(\d{1,2}):/);
    if (match) {
      const diff = Math.abs(parseInt(match[1], 10) - targetHour);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestItem = h;
      }
    }
  }

  if (!bestItem) {
    const idx = slot === 'morning' ? 0 : slot === 'afternoon' ? Math.floor(pool.length / 2) : pool.length - 1;
    bestItem = (pool[idx] ?? {}) as Record<string, unknown>;
  }

  return {
    temp: Math.round(Number(bestItem.temp ?? bestItem.Temp ?? 0)),
    precipProb: Math.round(Number(bestItem.precip_prob ?? bestItem.PrecipProb ?? 0)),
    icon: String(bestItem.icon ?? bestItem.Icon ?? 'weather-partly-cloudy'),
  };
}

function selectDailyEntry(daily?: unknown[], isTomorrow?: boolean): Record<string, unknown> | undefined {
  if (!Array.isArray(daily) || daily.length === 0) return undefined;
  const targetIdx = isTomorrow && daily.length > 1 ? 1 : 0;
  const candidate = daily[targetIdx];
  return candidate && typeof candidate === 'object' ? (candidate as Record<string, unknown>) : undefined;
}

function fallbackSlotWeather(
  slot: DaypartSlot, data?: Record<string, unknown>, current?: Record<string, unknown>,
  daily?: unknown[], isTomorrow?: boolean,
): SlotWeatherInfo {
  const curTemp = Number(current?.temperature ?? current?.Temperature ?? 0) || 0;
  const dayEntry = selectDailyEntry(daily, isTomorrow);
  const rawMax = dayEntry?.temp_max ?? dayEntry?.TempMax;
  const rawMin = dayEntry?.temp_min ?? dayEntry?.TempMin;
  const hasDaily = rawMax !== undefined && rawMin !== undefined;
  const maxTemp = hasDaily ? Number(rawMax) : curTemp;
  const minTemp = hasDaily ? Number(rawMin) : curTemp;
  const temp = hasDaily
    ? (slot === 'morning' ? minTemp : slot === 'afternoon' ? maxTemp : (minTemp + maxTemp) / 2)
    : curTemp;
  return {
    temp: Math.round(temp),
    precipProb: todayPrecipProb(data),
    icon: String(current?.icon ?? current?.Icon ?? 'weather-partly-cloudy'),
  };
}

function resolveSlotWeather(
  slot: DaypartSlot, data?: Record<string, unknown>, current?: Record<string, unknown>,
  daily?: unknown[], hourly?: unknown[], targetDateStr?: string, isTomorrow?: boolean,
): SlotWeatherInfo {
  const explicit = findExplicitSlot(slot, data);
  if (explicit) return explicit;
  if (Array.isArray(hourly) && hourly.length > 0) {
    return findBestHourlySlot(slot, hourly, isTomorrow ? targetDateStr : undefined);
  }
  return fallbackSlotWeather(slot, data, current, daily, isTomorrow);
}

function buildSchoolDaySlots(
  data: Record<string, unknown> | undefined, current: Record<string, unknown> | undefined,
  daily: unknown[] | undefined, hourly: unknown[] | undefined,
  config: Record<string, unknown> | undefined, timestamp?: string,
): SchoolDaySlotItem[] {
  const { characterPack, variantsPerState, thresholds } = extractSchoolDayConfig(config);
  const { dateStr, isTomorrow } = resolveSchoolDayTargetDate(config, timestamp);
  const birthday = resolveBirthdayOverride(dateStr, config?.birthdays);

  return DAYPART_SLOTS.map((slot) => {
    const info = resolveSlotWeather(slot, data, current, daily, hourly, dateStr, isTomorrow);
    const state = resolveClothingConditionState(info.temp, info.precipProb, info.icon, slot, thresholds);
    const variantIndex = selectDailyVariantIndex(dateStr, slot, variantsPerState);
    const enableHolidays = Boolean(config?.holidays);
    const spriteUrl = birthday
      ? resolveBirthdayPackSpriteUrl(characterPack, birthday.key, slot)
      : resolveCharacterPackSpriteUrl(characterPack, state, variantIndex, dateStr, enableHolidays);
    const baseOutfitLabel = resolveClothingOutfitLabel(state);
    const outfitLabel = birthday ? `${birthday.outfitLabel} · ${baseOutfitLabel}` : baseOutfitLabel;
    const forecastSummary = formatSlotForecastSummary(info.icon, info.precipProb);
    const displayLabel = isTomorrow ? `tomorrow ${slot}` : slot;
    return {
      slot, displayLabel, isTomorrow, temp: info.temp, state,
      outfitLabel, forecastSummary, spriteUrl, birthdayBanner: birthday?.bannerLabel,
    };
  });
}

interface WeatherCurrentHeaderProps {
  locationName: string; hasCurrent: boolean; conditionText: string; iconName: string;
  roundedTemp: number; tempUnit: string; hasDaily: boolean; maxTemp: number; minTemp: number;
}

function renderCurrentHeader(props: WeatherCurrentHeaderProps) {
  const { locationName, hasCurrent, conditionText, iconName, roundedTemp, tempUnit, hasDaily, maxTemp, minTemp } = props;
  return (
    <div className="weather-header">
      <div className="weather-header-left">
        <div className="weather-icon-badge" title={conditionText}>
          <WeatherIcon name={iconName} size={20} />
        </div>
        <div className="weather-header-titles">
          {locationName ? (
            <h2 className="weather-location-name">{locationName}</h2>
          ) : (
            <h2 className="weather-header-title">Weather</h2>
          )}
          <span className="weather-condition-text">{conditionText || 'Forecast'}</span>
        </div>
      </div>

      {hasCurrent && (
        <div className="weather-header-right">
          <div className="weather-temp-main weather-current-main bubbly-font" title={conditionText}>
            {roundedTemp}{tempUnit}
          </div>
          {hasDaily && <div className="weather-temp-range">H: {maxTemp}° • L: {minTemp}°</div>}
        </div>
      )}
    </div>
  );
}

function renderSchoolDayStrip(slots: SchoolDaySlotItem[], shadowOpacity?: number) {
  const isTomorrow = slots.length > 0 && slots[0].isTomorrow;
  const stripStyle =
    shadowOpacity !== undefined
      ? ({ '--weather-scrim-opacity': String(shadowOpacity) } as CSSProperties)
      : undefined;
  return (
    <div
      className="weather-school-day-strip"
      data-day={isTomorrow ? 'tomorrow' : 'today'}
      style={stripStyle}
    >
      {slots.map((item) => (
        <div
          key={item.slot}
          className={`weather-school-day-slot weather-school-day-slot-${item.slot}`}
          data-slot={item.slot}
          data-day={item.isTomorrow ? 'tomorrow' : 'today'}
          data-birthday={item.birthdayBanner ? 'true' : undefined}
        >
          <span className="weather-school-day-name weather-school-day-slot-name">{item.displayLabel}</span>
          {item.birthdayBanner && <span className="weather-school-day-birthday-badge">{item.birthdayBanner}</span>}
          <span className="weather-school-day-temp">{item.temp}°</span>
          <span className="weather-school-day-state weather-school-day-condition">{item.state}</span>
          <div className="weather-school-day-copy">
            <span className="weather-school-day-outfit">{item.outfitLabel}</span>
            <span className="weather-school-day-forecast">{item.forecastSummary}</span>
          </div>
          <img className="weather-school-day-sprite" src={item.spriteUrl} alt={item.state} />
        </div>
      ))}
    </div>
  );
}

interface WeatherHourlySectionProps { hourly?: unknown[]; widgetId: string; }

function renderHourlySection({ hourly, widgetId }: WeatherHourlySectionProps) {
  const hasHourly = Array.isArray(hourly) && hourly.length > 0;
  return (
    <div className="weather-container weather-hero weather-Hero">
      {hasHourly ? (
        <div className="weather-hourly-timeline">
          <WeatherHourlyGraph hourly={hourly} widgetId={widgetId} />
        </div>
      ) : (
        <div className="weather-empty">
          <span className="empty-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="4" /><path d="M12 2v2" /><path d="M12 20v2" />
              <path d="m4.93 4.93 1.41 1.41" /><path d="m17.66 17.66 1.41 1.41" />
              <path d="M2 12h2" /><path d="M20 12h2" />
              <path d="m6.34 17.66-1.41 1.41" /><path d="m19.07 4.93-1.41 1.41" />
            </svg>
          </span>
          <span className="empty-title">No hourly forecast available</span>
        </div>
      )}
    </div>
  );
}

interface WeatherMicroStatsProps { humidity: string; windSpeed: number; windUnit: string; precipProb: number; }

function renderMicroStats({ humidity, windSpeed, windUnit, precipProb }: WeatherMicroStatsProps) {
  return (
    <div className="weather-micro-stats">
      <span className="weather-meta-stat weather-meta-humidity" title={`Humidity: ${humidity}`}>
        <svg className="weather-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z" />
        </svg>
        <span>{humidity}</span>
      </span>
      <span className="weather-meta-stat weather-meta-wind" title={`Wind: ${windSpeed} ${windUnit}`}>
        <svg className="weather-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M17.7 7.7a2.5 2.5 0 1 1 1.8 4.3H2" /><path d="M9.6 4.6A2 2 0 1 1 11 8H2" /><path d="M12.6 19.4A2 2 0 1 0 14 16H2" />
        </svg>
        <span>{windSpeed} {windUnit}</span>
      </span>
      <span className="weather-meta-stat weather-meta-precip" title={`Precipitation: ${precipProb}%`}>
        <svg className="weather-meta-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M22 12a10.06 10.06 0 0 0-20 0Z" /><path d="M12 12v8a2 2 0 0 0 4 0" /><path d="M12 2v1" />
        </svg>
        <span>{precipProb}%</span>
      </span>
    </div>
  );
}

/**
 * Native React widget component for weather forecasts.
 * Ports widgets/weather-forecast/views/widget.html.
 */
export function WeatherForecastWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const data = entry?.data as Record<string, unknown> | undefined;
  const isError = entry?.state === 'error';

  const current = extractCurrentCandidate(data);
  const hasCurrent = Boolean(current);

  const locationName = resolveLocationName(config, data);
  const showHeader = Boolean(locationName || hasCurrent);

  const conditionText = hasCurrent ? String(current?.condition_text ?? current?.ConditionText ?? '') : '';
  const iconName = hasCurrent ? String(current?.icon ?? current?.Icon ?? '') : 'weather-partly-cloudy';

  const { roundedTemp, tempUnit } = extractTemperature(current);
  const { hasDaily, maxTemp, minTemp } = extractDailyRange(data);
  const { humidity, windSpeed, windUnit } = extractWindAndHumidity(current);
  const precipProb = todayPrecipProb(data);
  const hourly = (data?.hourly ?? data?.Hourly) as unknown[] | undefined;
  const daily = (data?.daily ?? data?.Daily) as unknown[] | undefined;

  const isSchoolDay = config?.mode === 'school-day';
  const rawShadow = config?.shadow_opacity;
  const shadowOpacity =
    typeof rawShadow === 'number' && rawShadow >= 0 && rawShadow <= 1 ? rawShadow : undefined;
  const schoolDaySlots = isSchoolDay
    ? buildSchoolDaySlots(data, current, daily, hourly, config, entry?.timestamp)
    : [];

  return (
    <div className="widget-weather">
      {isError && (
        <div className="widget-error-banner">
          <span className="error-icon">⚠</span>
          <span>Widget Error</span>
        </div>
      )}

      {showHeader && renderCurrentHeader({
        locationName, hasCurrent, conditionText, iconName, roundedTemp, tempUnit, hasDaily, maxTemp, minTemp,
      })}

      {hasCurrent ? (
        <>
          {isSchoolDay && renderSchoolDayStrip(schoolDaySlots, shadowOpacity)}
          {renderHourlySection({ hourly, widgetId: id })}
          {renderMicroStats({ humidity, windSpeed, windUnit, precipProb })}
        </>
      ) : (
        <div className="weather-loading">
          <span>Loading weather forecast...</span>
        </div>
      )}
    </div>
  );
}
