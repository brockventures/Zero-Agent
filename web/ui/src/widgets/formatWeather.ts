import { formatTime } from './formatDate';

/** Escapes XML/HTML special characters for safe inclusion in SVG markup. */
export function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function extractPrecipProb(item: unknown): number | null {
  if (!item || typeof item !== 'object') return null;
  const rec = item as Record<string, unknown>;
  const val =
    rec.precip_prob_max ??
    rec.PrecipProbMax ??
    rec.precip_prob ??
    rec.PrecipProb ??
    rec.PrecipitationProbability;
  if (typeof val === 'number' && !isNaN(val)) {
    return Math.max(0, Math.min(100, Math.round(val)));
  }
  return null;
}

function extractHourlyMaxProb(hourly: unknown[]): number {
  let maxProb = 0;
  for (const item of hourly.slice(0, 12)) {
    const p = extractPrecipProb(item);
    if (p !== null) {
      maxProb = Math.max(maxProb, p);
    }
  }
  return maxProb;
}

/**
 * Extracts today's maximum precipitation probability percentage (0-100)
 * from daily forecast, hourly forecast, current snapshot, or map data.
 */
export function todayPrecipProb(data: unknown): number {
  if (!data || typeof data !== 'object') {
    return 0;
  }

  const d = data as Record<string, unknown>;

  // Daily array
  const daily = d.daily ?? d.Daily;
  if (Array.isArray(daily) && daily.length > 0) {
    const p = extractPrecipProb(daily[0]);
    if (p !== null) return p;
  }

  // Hourly array (max across first 12 hours)
  const hourly = d.hourly ?? d.Hourly;
  if (Array.isArray(hourly) && hourly.length > 0) {
    const maxProb = extractHourlyMaxProb(hourly);
    if (maxProb > 0) {
      return Math.max(0, Math.min(100, maxProb));
    }
  }

  // Current
  const current = d.current ?? d.Current;
  const currentProb = extractPrecipProb(current);
  if (currentProb !== null) return currentProb;

  // Direct precip_prob_max
  const direct = d.precip_prob_max ?? d.PrecipProbMax;
  if (typeof direct === 'number' && !isNaN(direct)) {
    return Math.max(0, Math.min(100, Math.round(direct)));
  }

  return 0;
}

export interface WeatherHourlyGraphOptions {
  svgHeight?: number;
  curveTop?: number;
  curveBottom?: number;
  baseY?: number;
  containerWidth?: number;
  maxPoints?: number;
}

export interface HourlyPointLayout {
  x: number;
  y: number;
  lineY1: number;
  precipProb: number;
  precipY: number;
  icon: string;
  iconTranslateX: number;
  iconY: number;
  iconScale: number;
  tempRound: number;
  tempY: number;
  tempFontSize: number;
  circleR: number;
  timeLabel: string;
  timeWeight: string;
  timeColor: string;
  timeY: number;
  timeFontSize: number;
}

export interface HourlyGraphLayout {
  totalWidth: number;
  svgHeight: number;
  baseY: number;
  isTall: boolean;
  gradientId: string;
  areaPath: string;
  curvePath: string;
  gridLinesPath: string;
  points: HourlyPointLayout[];
}

interface RawHourlyPoint {
  timeStr: string;
  temp: number;
  precipProb: number;
  icon: string;
}

function parseRawHourlyPoints(hourly: unknown[]): RawHourlyPoint[] {
  const rawPts: RawHourlyPoint[] = [];

  for (const item of hourly) {
    if (!item || typeof item !== 'object') continue;
    const m = item as Record<string, unknown>;

    let timeStr = '';
    const rawTime = m.time ?? m.Time;
    if (typeof rawTime === 'string') {
      timeStr = rawTime;
    }

    let temp = Number(m.temp ?? m.Temp);
    if (isNaN(temp) || !isFinite(temp)) {
      temp = 0.0;
    }

    let precipProb = 0;
    const rawPrecip = m.precip_prob ?? m.PrecipProb;
    if (typeof rawPrecip === 'number' && !isNaN(rawPrecip)) {
      precipProb = Math.round(rawPrecip);
    }

    let icon = 'weather-cloudy';
    const rawIcon = m.icon ?? m.Icon;
    if (typeof rawIcon === 'string' && rawIcon.trim() !== '') {
      icon = rawIcon.trim();
    }

    rawPts.push({ timeStr, temp, precipProb, icon });
  }

  return rawPts;
}

function computeTemperatureSpan(pts: RawHourlyPoint[]): { minTemp: number; span: number } {
  let minTemp = pts[0].temp;
  let maxTemp = pts[0].temp;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i].temp < minTemp) minTemp = pts[i].temp;
    if (pts[i].temp > maxTemp) maxTemp = pts[i].temp;
  }

  let span = maxTemp - minTemp;
  if (span < 4.0) {
    const mid = (minTemp + maxTemp) / 2.0;
    minTemp = mid - 2.0;
    span = 4.0;
  }
  return { minTemp, span };
}

function buildGraphPaths(
  coords: { x: number; y: number }[],
  baseY: number
): { curvePath: string; areaPath: string } {
  const n = coords.length;
  if (n <= 1) return { curvePath: '', areaPath: '' };

  let curvePath = `M ${coords[0].x.toFixed(1)} ${coords[0].y.toFixed(1)}`;
  let areaPath = `M ${coords[0].x.toFixed(1)} ${baseY.toFixed(1)} L ${coords[0].x.toFixed(1)} ${coords[0].y.toFixed(1)}`;

  for (let i = 0; i < n - 1; i++) {
    const dx = (coords[i + 1].x - coords[i].x) / 2.0;
    const seg = ` C ${(coords[i].x + dx).toFixed(1)} ${coords[i].y.toFixed(1)}, ${(coords[i + 1].x - dx).toFixed(1)} ${coords[i + 1].y.toFixed(1)}, ${coords[i + 1].x.toFixed(1)} ${coords[i + 1].y.toFixed(1)}`;
    curvePath += seg;
    areaPath += seg;
  }
  areaPath += ` L ${coords[n - 1].x.toFixed(1)} ${baseY.toFixed(1)} Z`;
  return { curvePath, areaPath };
}

function buildPointLayout(
  pt: RawHourlyPoint,
  coord: { x: number; y: number },
  isTall: boolean,
  i: number
): HourlyPointLayout {
  const lineTopOffset = isTall ? 6.0 : 4.0;
  const lineY1 = coord.y + lineTopOffset;
  const precipY = 12;

  const iconY = isTall ? 18 : 14;
  const iconScale = isTall ? 1.2 : 0.8;
  const iconOffsetX = isTall ? 14.4 : 9.6;
  const iconTranslateX = coord.x - iconOffsetX;

  // Round half-away-from-zero to match Go math.Round
  const tempRound = Math.sign(pt.temp) * Math.round(Math.abs(pt.temp)) || 0;
  const tempY = isTall ? coord.y - 10.0 : coord.y - 6.0;
  const tempFontSize = isTall ? 14 : 12;
  const circleR = isTall ? 4.0 : 3.5;

  let timeLabel = 'Now';
  let timeWeight = '800';
  let timeColor = 'var(--mm-coral, #ff6f59)';
  if (i > 0) {
    timeWeight = '600';
    timeColor = 'var(--mm-text-dim, #8b819f)';
    const formatted = formatTime('3 PM', pt.timeStr);
    timeLabel = formatted !== '' ? formatted : pt.timeStr;
  }
  const timeY = isTall ? 262 : 102;
  const timeFontSize = isTall ? 13 : 11;

  return {
    x: coord.x,
    y: coord.y,
    lineY1,
    precipProb: pt.precipProb,
    precipY,
    icon: pt.icon,
    iconTranslateX,
    iconY,
    iconScale,
    tempRound,
    tempY,
    tempFontSize,
    circleR,
    timeLabel,
    timeWeight,
    timeColor,
    timeY,
    timeFontSize,
  };
}

/**
 * Computes deterministic layout metrics and SVG geometry for an hourly weather forecast graph.
 */
export function computeHourlyGraphLayout(
  hourly: unknown,
  widgetId?: string,
  options?: WeatherHourlyGraphOptions,
): HourlyGraphLayout | null {
  if (!hourly || !Array.isArray(hourly) || hourly.length === 0) {
    return null;
  }

  const rawPts = parseRawHourlyPoints(hourly);
  if (rawPts.length === 0) {
    return null;
  }

  const maxPoints = options?.maxPoints && options.maxPoints > 0 ? options.maxPoints : 10;
  const pts = rawPts.slice(0, maxPoints);
  const n = pts.length;
  const gradientId =
    widgetId && widgetId.trim() !== ''
      ? `mm-weather-grad-${escapeHtml(widgetId.trim())}`
      : 'mm-weather-hourly-grad';

  const baseColWidth = 56.0;
  const minGraphWidth = 560.0;
  const svgHeight = options?.svgHeight ?? 270.0;
  const curveTop = options?.curveTop ?? 76.0;
  const curveBottom = options?.curveBottom ?? 212.0;
  const baseY = options?.baseY ?? (svgHeight === 108.0 ? 90.0 : 238.0);
  const isTall = svgHeight >= 200.0;

  let colWidth = baseColWidth;
  if (options?.containerWidth && options.containerWidth > 0 && n > 0) {
    colWidth = options.containerWidth / n;
  } else if (n * colWidth < minGraphWidth && n > 0) {
    colWidth = minGraphWidth / n;
  }
  const totalWidth = n * colWidth;

  const { minTemp, span } = computeTemperatureSpan(pts);
  const edgeInset = isTall ? 18.0 : 12.0;
  const step = n > 1 ? (totalWidth - 2.0 * edgeInset) / (n - 1) : 0.0;

  const coords: { x: number; y: number }[] = [];
  for (let i = 0; i < n; i++) {
    const x = n > 1 ? edgeInset + i * step : totalWidth / 2.0;
    const ratio = (pts[i].temp - minTemp) / span;
    const y = curveBottom - ratio * (curveBottom - curveTop);
    coords.push({ x, y });
  }

  const { curvePath, areaPath } = buildGraphPaths(coords, baseY);
  const points: HourlyPointLayout[] = pts.map((pt, i) =>
    buildPointLayout(pt, coords[i], isTall, i)
  );

  let gridLinesPath = '';
  for (let i = 0; i < n; i++) {
    const pt = points[i];
    gridLinesPath += `M ${pt.x.toFixed(1)} ${pt.lineY1.toFixed(1)} L ${pt.x.toFixed(1)} ${baseY.toFixed(1)} `;
  }

  return {
    totalWidth,
    svgHeight,
    baseY,
    isTall,
    gradientId,
    areaPath,
    curvePath,
    gridLinesPath: gridLinesPath.trim(),
    points,
  };
}
