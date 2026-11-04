export interface WeatherIconProps {
  name?: string | null;
  size?: number;
  className?: string;
}

/**
 * Returns declarative SVG child elements for a WMO weather condition token.
 */
export function WeatherIconGlyph({ name }: { name?: string | null }) {
  const rawToken = typeof name === 'string' && name.trim() !== '' ? name.trim() : 'weather-cloudy';
  const token = /^[a-z0-9_-]+$/.test(rawToken) ? rawToken : rawToken.replace(/[^a-z0-9_-]/gi, '') || 'weather-cloudy';

  switch (token) {
    case 'weather-sunny':
      return (
        <>
          <circle cx="12" cy="12" r="5" fill="#f59e0b" stroke="#f59e0b" strokeWidth={1} />
          <path
            d="M 12 1.5 L 12 3.5 M 12 20.5 L 12 22.5 M 4.5 4.5 L 6 6 M 18 18 L 19.5 19.5 M 1.5 12 L 3.5 12 M 20.5 12 L 22.5 12 M 4.5 19.5 L 6 18 M 18 6 L 19.5 4.5"
            stroke="#f59e0b"
            strokeWidth={2}
            strokeLinecap="round"
          />
        </>
      );
    case 'weather-clear-night':
      return (
        <path
          d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"
          fill="#f8fafc"
          stroke="#94a3b8"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      );
    case 'weather-partly-cloudy':
      return (
        <>
          <circle cx="16" cy="9" r="4" fill="#f59e0b" />
          <path
            d="M 16 2 L 16 3.5 M 21.5 6 L 20 7 M 21.5 12 L 20 11 M 11.5 4 L 12.5 5"
            stroke="#f59e0b"
            strokeWidth={1.8}
            strokeLinecap="round"
          />
          <path
            d="M15.5 19H7a4.5 4.5 0 0 1-.8-8.9A5.5 5.5 0 0 1 16.8 11 3.5 3.5 0 0 1 15.5 19z"
            fill="#ffffff"
            stroke="#94a3b8"
            strokeWidth={1.6}
            strokeLinejoin="round"
          />
        </>
      );
    case 'weather-night-partly-cloudy':
      return (
        <>
          <path
            d="M19 10.5A6 6 0 0 1 12.5 4a5 5 0 0 0 6.5 6.5z"
            fill="#f8fafc"
            stroke="#94a3b8"
            strokeWidth={1.5}
            strokeLinejoin="round"
          />
          <path
            d="M15.5 19H7a4.5 4.5 0 0 1-.8-8.9A5.5 5.5 0 0 1 16.8 11 3.5 3.5 0 0 1 15.5 19z"
            fill="#ffffff"
            stroke="#94a3b8"
            strokeWidth={1.6}
            strokeLinejoin="round"
          />
        </>
      );
    case 'weather-cloudy':
      return (
        <path
          d="M17.5 19H7a5 5 0 0 1-1-9.9A6 6 0 0 1 17.8 11 4 4 0 0 1 17.5 19z"
          fill="#f1f5f9"
          stroke="#94a3b8"
          strokeWidth={1.7}
          strokeLinejoin="round"
        />
      );
    case 'weather-fog':
      return (
        <path
          d="M 4 10 L 20 10 M 3 14 L 21 14 M 5 18 L 19 18 M 7 6 L 17 6"
          stroke="#94a3b8"
          strokeWidth={2}
          strokeLinecap="round"
        />
      );
    case 'weather-rainy':
      return (
        <>
          <path
            d="M17.5 15H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 7 3.5 3.5 0 0 1 17.5 15z"
            fill="#f1f5f9"
            stroke="#94a3b8"
            strokeWidth={1.7}
            strokeLinejoin="round"
          />
          <path
            d="M 8 18 L 7 21 M 12 18 L 11 21 M 16 18 L 15 21"
            stroke="#38bdf8"
            strokeWidth={2}
            strokeLinecap="round"
          />
        </>
      );
    case 'weather-pouring':
      return (
        <>
          <path
            d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z"
            fill="#e2e8f0"
            stroke="#64748b"
            strokeWidth={1.7}
            strokeLinejoin="round"
          />
          <path
            d="M 6 17 L 4 21 M 10 17 L 8 21 M 14 17 L 12 21 M 18 17 L 16 21"
            stroke="#0284c7"
            strokeWidth={2}
            strokeLinecap="round"
          />
        </>
      );
    case 'weather-snowy':
      return (
        <>
          <path
            d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z"
            fill="#f1f5f9"
            stroke="#94a3b8"
            strokeWidth={1.7}
            strokeLinejoin="round"
          />
          <g fill="#93c5fd">
            <circle cx="8" cy="18" r={1.3} />
            <circle cx="12" cy="18" r={1.3} />
            <circle cx="16" cy="18" r={1.3} />
            <circle cx="10" cy="21" r={1.3} />
            <circle cx="14" cy="21" r={1.3} />
          </g>
        </>
      );
    case 'weather-snowy-rainy':
      return (
        <>
          <path
            d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z"
            fill="#f1f5f9"
            stroke="#94a3b8"
            strokeWidth={1.7}
            strokeLinejoin="round"
          />
          <g fill="#93c5fd">
            <circle cx="8" cy="18" r={1.3} />
            <circle cx="14" cy="18" r={1.3} />
          </g>
          <g stroke="#38bdf8" strokeWidth={2} strokeLinecap="round">
            <line x1="11" y1="18" x2="10" y2="21" />
          </g>
        </>
      );
    case 'weather-lightning':
    case 'weather-lightning-rainy':
      return (
        <>
          <path
            d="M17.5 13H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 5 3.5 3.5 0 0 1 17.5 13z"
            fill="#e2e8f0"
            stroke="#64748b"
            strokeWidth={1.7}
            strokeLinejoin="round"
          />
          <polygon
            points="12 13 9 18 13 18 11 23 16 16 12 16 14 13"
            fill="#f59e0b"
            stroke="#f59e0b"
            strokeWidth={1}
            strokeLinejoin="round"
          />
        </>
      );
    default:
      return (
        <path
          d="M17.5 19H7a5 5 0 0 1-1-9.9A6 6 0 0 1 17.8 11 4 4 0 0 1 17.5 19z"
          fill="#f1f5f9"
          stroke="#94a3b8"
          strokeWidth={1.7}
          strokeLinejoin="round"
        />
      );
  }
}

/**
 * Declarative SVG icon for WMO condition tokens.
 */
export function WeatherIcon({
  name,
  size = 20,
  className,
}: WeatherIconProps) {
  let s = typeof size === 'number' && !isNaN(size) && size > 0 ? Math.round(size) : 20;
  if (s < 12) s = 12;
  else if (s > 64) s = 64;

  const rawToken = typeof name === 'string' && name.trim() !== '' ? name.trim() : 'weather-cloudy';
  const safeToken = rawToken.replace(/[^a-z0-9_-]/gi, '') || 'weather-cloudy';
  const baseClass = 'mm-weather-icon mm-icon-' + safeToken;
  const fullClass = className ? `${baseClass} ${className}` : baseClass;

  return (
    <svg
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={fullClass}
    >
      <WeatherIconGlyph name={safeToken} />
    </svg>
  );
}
