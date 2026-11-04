import { useHeader } from '../store/hooks';
import { WeatherIcon } from '../widgets/weather-forecast/WeatherIcon';

const WEATHER_DESC_MAP: Record<string, string> = {
  'weather-sunny': 'Sunny & Mild',
  'weather-clear-night': 'Clear',
  'weather-partly-cloudy': 'Partly Cloudy',
  'weather-night-partly-cloudy': 'Partly Cloudy',
  'weather-cloudy': 'Overcast',
  'weather-fog': 'Misty & Foggy',
  'weather-rainy': 'Light Rain',
  'weather-pouring': 'Heavy Rain',
  'weather-snowy': 'Snow Showers',
  'weather-snowy-rainy': 'Rain & Snow',
  'weather-lightning': 'Thunderstorm',
  'weather-lightning-rainy': 'Severe Storm',
};

const KNOWN_WEATHER_ICONS = new Set([
  'weather-sunny',
  'weather-clear-night',
  'weather-partly-cloudy',
  'weather-night-partly-cloudy',
  'weather-cloudy',
  'weather-fog',
  'weather-rainy',
  'weather-pouring',
  'weather-snowy',
  'weather-snowy-rainy',
  'weather-lightning',
  'weather-lightning-rainy',
]);

export function WeatherPill() {
  const header = useHeader();
  const weather = header?.weather;

  const tempText =
    weather && weather.temperature !== undefined
      ? `${Math.round(weather.temperature)}°${(weather.units || '°').replace('°', '')}`
      : '--°';

  const locationText = weather?.location || 'Oakland';

  const conditionToken = weather?.icon || weather?.condition || '';
  const customDesc = (weather as { description?: string } | null | undefined)?.description;

  let descText = 'Sunny & Mild';
  if (weather) {
    if (customDesc) {
      descText = customDesc;
    } else if (WEATHER_DESC_MAP[conditionToken]) {
      descText = WEATHER_DESC_MAP[conditionToken];
    } else if (weather.condition && WEATHER_DESC_MAP[weather.condition]) {
      descText = WEATHER_DESC_MAP[weather.condition];
    } else if (weather.condition && weather.condition !== conditionToken) {
      descText = weather.condition;
    } else {
      const isNight =
        conditionToken.includes('night') ||
        (weather.condition ? weather.condition.toLowerCase().includes('night') : false);
      descText = isNight ? 'Clear' : (weather.condition || 'Clear Sky');
    }
  }

  const iconKey =
    conditionToken && KNOWN_WEATHER_ICONS.has(conditionToken)
      ? conditionToken
      : conditionToken.includes('night')
        ? 'weather-clear-night'
        : 'weather-partly-cloudy';

  return (
    <div className="header-weather" id="header-weather">
      <div className="pill-badge weather-pill" id="weather-pill">
        <span className="weather-condition" id="weather-condition" title={conditionToken || undefined}>
          <WeatherIcon name={iconKey} size={26} />
        </span>
        <div className="weather-details" id="weather-details">
          <div className="weather-primary-row">
            <span className="weather-temp" id="weather-temp">
              {tempText}
            </span>
            <span className="weather-location" id="weather-location">
              {locationText}
            </span>
          </div>
          <span className="weather-desc" id="weather-desc">
            {descText}
          </span>
        </div>
      </div>
    </div>
  );
}
