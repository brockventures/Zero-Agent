import { Fragment } from 'react';
import { computeHourlyGraphLayout, type WeatherHourlyGraphOptions } from '../format';
import { WeatherIconGlyph } from './WeatherIcon';

export interface WeatherHourlyGraphProps {
  hourly: unknown;
  widgetId?: string;
  options?: WeatherHourlyGraphOptions;
}

/**
 * Declarative SVG continuous line graph showing hourly temperature trends,
 * condition icons, precipitation chances, and time throughout the day.
 */
export function WeatherHourlyGraph({
  hourly,
  widgetId,
  options,
}: WeatherHourlyGraphProps) {
  const layout = computeHourlyGraphLayout(hourly, widgetId, options);
  if (!layout) {
    return null;
  }

  return (
    <svg
      className="weather-hourly-graph"
      viewBox={`0 0 ${layout.totalWidth.toFixed(1)} ${layout.svgHeight.toFixed(1)}`}
      style={{
        width: '100%',
        maxWidth: '100%',
        overflow: 'visible',
      }}
    >
      <defs>
        <linearGradient id={layout.gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ff6f59" stopOpacity="0.25" />
          <stop offset="100%" stopColor="#ff6f59" stopOpacity="0.0" />
        </linearGradient>
      </defs>
      <line
        x1="0"
        y1={layout.baseY.toFixed(1)}
        x2={layout.totalWidth.toFixed(1)}
        y2={layout.baseY.toFixed(1)}
        stroke="rgba(90, 60, 30, 0.08)"
        strokeWidth="1"
      />
      {layout.areaPath && <path d={layout.areaPath} fill={`url(#${layout.gradientId})`} />}
      {layout.curvePath && (
        <path
          d={layout.curvePath}
          fill="none"
          stroke="var(--mm-coral, #ff6f59)"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
      {layout.gridLinesPath && (
        <path
          d={layout.gridLinesPath}
          stroke="rgba(90, 60, 30, 0.08)"
          strokeDasharray="2 2"
        />
      )}
      {layout.points.map((pt, i) => (
        <Fragment key={i}>
          {pt.precipProb > 0 && (
            <text
              x={pt.x.toFixed(1)}
              y={pt.precipY}
              textAnchor="middle"
              fontSize="11"
              fontWeight="700"
              fill="var(--mm-sky, #3b9edd)"
            >
              ☂ {pt.precipProb}%
            </text>
          )}
          <g
            transform={`translate(${pt.iconTranslateX.toFixed(1)}, ${pt.iconY}) scale(${pt.iconScale})`}
            fill="none"
            stroke="var(--mm-honey, #f59e0b)"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <WeatherIconGlyph name={pt.icon} />
          </g>
          <text
            x={pt.x.toFixed(1)}
            y={pt.tempY.toFixed(1)}
            textAnchor="middle"
            fontSize={pt.tempFontSize}
            fontWeight="800"
            fill="var(--mm-text-title, #2a2338)"
          >
            {pt.tempRound}°
          </text>
          <circle
            cx={pt.x.toFixed(1)}
            cy={pt.y.toFixed(1)}
            r={pt.circleR}
            fill="#ffffff"
            stroke="var(--mm-coral, #ff6f59)"
            strokeWidth="2.5"
          />
          <text
            x={pt.x.toFixed(1)}
            y={pt.timeY}
            textAnchor="middle"
            fontSize={pt.timeFontSize}
            fontWeight={pt.timeWeight}
            fill={pt.timeColor}
          >
            {pt.timeLabel}
          </text>
        </Fragment>
      ))}
    </svg>
  );
}
