import { useState } from 'react';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as formatModule from '../format';
import { useDisplayStore } from '../../store';
import { WeatherForecastWidget } from './WeatherForecastWidget';
import weatherForecastCss from './WeatherForecastWidget.css?raw';
import { WeatherIcon, WeatherIconGlyph } from './WeatherIcon';
import { WeatherHourlyGraph } from './WeatherHourlyGraph';
import {
  addOneCalendarDay,
  formatSlotForecastSummary,
  resolveClothingOutfitLabel,
  resolveIconConditionLabel,
  resolveSchoolDayTargetDate,
} from './schoolDayPack';

const initialStore = useDisplayStore.getState();

describe('WeatherForecastWidget', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
  });

  afterEach(() => {
    cleanup();
  });

  it('renders unnamed loading state without a header when data is {}', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-unnamed': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy', data: {}, timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-unnamed" type="weather-forecast" />);
    const root = container.querySelector('.widget-weather');
    expect(root).not.toBeNull();
    expect(root?.querySelector('.weather-header')).toBeNull();
    expect(root?.querySelector('.weather-loading')?.textContent).toContain('Loading weather forecast...');
  });

  it('renders named loading state with header when config.location_name is set but data is unhydrated', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-named': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: { location_name: 'San Francisco' }, assets_base: '' },
          state: 'healthy', data: {}, timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-named" type="weather-forecast" />);
    const root = container.querySelector('.widget-weather');
    expect(root?.querySelector('.weather-header')).not.toBeNull();
    expect(root?.querySelector('.weather-location-name')?.textContent).toBe('San Francisco');
    expect(root?.querySelector('.weather-loading')?.textContent).toContain('Loading weather forecast...');
  });

  it('renders header using config.name', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-by-name': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: { name: 'Home Base' }, assets_base: '' },
          state: 'healthy', data: { current: { temperature: 72 } }, timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-by-name" type="weather-forecast" />);
    expect(container.querySelector('.weather-location-name')?.textContent).toBe('Home Base');
  });

  it('renders header with default Weather title when location is omitted', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-no-loc': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy', data: { current: { temperature: 70 } }, timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-no-loc" type="weather-forecast" />);
    expect(container.querySelector('.weather-header-title')?.textContent).toBe('Weather');
  });

  it('renders negative temperatures accurately', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-freezing': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: { current: { temperature: -4.2 }, daily: [{ temp_max: 0, temp_min: -8 }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-freezing" type="weather-forecast" />);
    expect(container.querySelector('.weather-temp-main')?.textContent).toBe('-4°');
    expect(container.querySelector('.weather-temp-range')?.textContent).toBe('H: 0° • L: -8°');
  });

  it('renders full current conditions and hourly SVG graph with snake_case payload', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-snake': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            location: 'Oakland',
            current: {
              temperature: 68.4,
              condition_text: 'Sunny',
              icon: 'weather-sunny',
              humidity: 55,
              wind_speed: 12.3,
              units: {
                temperature: '°F',
                wind_speed: 'mph',
              },
            },
            daily: [
              {
                temp_max: 74.2,
                temp_min: 52.8,
                precip_prob_max: 10,
              },
            ],
            hourly: [
              { time: '2026-10-31T12:00:00Z', temp: 68, precip_prob: 0, icon: 'weather-sunny' },
              { time: '2026-10-31T13:00:00Z', temp: 70, precip_prob: 5, icon: 'weather-sunny' },
              {
                time: '2026-10-31T14:00:00Z',
                temp: 72,
                precip_prob: 10,
                icon: 'weather-partly-cloudy',
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-snake" type="weather-forecast" />,
    );
    const root = container.querySelector('.widget-weather');
    expect(root).not.toBeNull();

    // Header
    expect(root?.querySelector('.weather-location-name')?.textContent).toBe('Oakland');
    expect(root?.querySelector('.weather-condition-text')?.textContent).toBe('Sunny');
    expect(root?.querySelector('.weather-icon-badge svg')).not.toBeNull();

    // Main temp and high/low
    expect(root?.querySelector('.weather-temp-main')?.textContent).toBe('68°F');
    expect(root?.querySelector('.weather-temp-range')?.textContent).toBe('H: 74° • L: 53°');

    // Hourly graph SVG inside timeline
    const timeline = root?.querySelector('.weather-hourly-timeline');
    expect(timeline).not.toBeNull();
    const svg = timeline?.querySelector('svg.weather-hourly-graph');
    expect(svg).not.toBeNull();

    // Micro stats
    expect(root?.querySelector('.weather-meta-humidity')?.textContent).toContain('55%');
    expect(root?.querySelector('.weather-meta-wind')?.textContent).toContain('12 mph');
    expect(root?.querySelector('.weather-meta-precip')?.textContent).toContain('10%');
  });

  it('renders full current conditions and hourly SVG graph with PascalCase payload', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-pascal': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            Location: 'Seattle',
            Current: {
              Temperature: 54.6,
              ConditionText: 'Rainy',
              Icon: 'weather-rainy',
              Humidity: 88,
              WindSpeed: 8.7,
              Units: {
                Temperature: '°',
                WindSpeed: 'knots',
              },
            },
            Daily: [
              {
                TempMax: 58.1,
                TempMin: 49.3,
                PrecipProbMax: 80,
              },
            ],
            Hourly: [
              { Time: '2026-10-31T12:00:00Z', Temp: 54, PrecipProb: 80, Icon: 'weather-rainy' },
              { Time: '2026-10-31T13:00:00Z', Temp: 55, PrecipProb: 75, Icon: 'weather-rainy' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-pascal" type="weather-forecast" />,
    );
    const root = container.querySelector('.widget-weather');
    expect(root).not.toBeNull();

    expect(root?.querySelector('.weather-location-name')?.textContent).toBe('Seattle');
    expect(root?.querySelector('.weather-condition-text')?.textContent).toBe('Rainy');
    expect(root?.querySelector('.weather-temp-main')?.textContent).toBe('55°');
    expect(root?.querySelector('.weather-temp-range')?.textContent).toBe('H: 58° • L: 49°');

    expect(root?.querySelector('.weather-meta-humidity')?.textContent).toContain('88%');
    expect(root?.querySelector('.weather-meta-wind')?.textContent).toContain('9 knots');
    expect(root?.querySelector('.weather-meta-precip')?.textContent).toContain('80%');
  });

  it('renders empty forecast placeholder when current exists but hourly is empty', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-no-hourly': {
          meta: { type: 'weather-forecast', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: { current: { temperature: 70, condition_text: 'Clear', icon: 'weather-sunny' }, hourly: [] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });
    const { container } = render(<WeatherForecastWidget id="weather-no-hourly" type="weather-forecast" />);
    const empty = container.querySelector('.weather-empty');
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toContain('No hourly forecast available');
  });

  it('renders widget-error-banner when state is error', () => {
    useDisplayStore.setState({
      widgets: { 'weather-err': { state: 'error', data: {}, timestamp: '' } },
    });
    const { container } = render(<WeatherForecastWidget id="weather-err" type="weather-forecast" />);
    const banner = container.querySelector('.widget-error-banner');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Widget Error');
  });

  it('renders school-day mode with character pack strip and sprite images', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
              character_pack: 'ghibli_school_day',
              variants_per_state: 5,
              rain_threshold_pct: 35,
              cold_max_f: 49,
              cool_max_f: 64,
              warm_max_f: 77,
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            location: 'Springfield',
            current: {
              temperature: 62,
              condition_text: 'Partly Cloudy',
              icon: 'weather-partly-cloudy',
              humidity: 45,
              wind_speed: 8,
            },
            daily: [
              {
                temp_max: 75,
                temp_min: 48,
                precip_prob_max: 20,
              },
            ],
            hourly: [
              { time: '2026-10-31T08:00:00Z', temp: 48, precip_prob: 10, icon: 'weather-sunny' },
              {
                time: '2026-10-31T14:00:00Z',
                temp: 75,
                precip_prob: 15,
                icon: 'weather-partly-cloudy',
              },
              { time: '2026-10-31T18:00:00Z', temp: 60, precip_prob: 10, icon: 'weather-cloudy' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school" type="weather-forecast" />,
    );

    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip).not.toBeNull();

    const slots = container.querySelectorAll('.weather-school-day-slot');
    expect(slots.length).toBe(3);

    const sprites = container.querySelectorAll('img.weather-school-day-sprite');
    expect(sprites.length).toBe(3);

    // Morning slot: temp 48 <= 49 -> cold_puffer
    const morningSlot = container.querySelector('.weather-school-day-slot-morning');
    expect(morningSlot?.textContent).toContain('morning');
    expect(morningSlot?.textContent).toContain('48°');
    expect(morningSlot?.textContent).toContain('cold_puffer');
    const morningImg = morningSlot?.querySelector('img.weather-school-day-sprite');
    expect(morningImg?.getAttribute('src')).toMatch(
      /^\/static\/ui\/assets\/weather-packs\/ghibli_school_day\/cold_puffer_v\d+\.jpg$/,
    );

    // Afternoon slot: temp 75 <= 77 -> warm_pants
    const afternoonSlot = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoonSlot?.textContent).toContain('afternoon');
    expect(afternoonSlot?.textContent).toContain('75°');
    expect(afternoonSlot?.textContent).toContain('warm_pants');
    const afternoonImg = afternoonSlot?.querySelector('img.weather-school-day-sprite');
    expect(afternoonImg?.getAttribute('src')).toMatch(
      /^\/static\/ui\/assets\/weather-packs\/ghibli_school_day\/warm_pants_v\d+\.jpg$/,
    );

    // Evening slot: temp 60 <= 64 -> cool_evening
    const eveningSlot = container.querySelector('.weather-school-day-slot-evening');
    expect(eveningSlot?.textContent).toContain('evening');
    expect(eveningSlot?.textContent).toContain('60°');
    expect(eveningSlot?.textContent).toContain('cool_evening');
    const eveningImg = eveningSlot?.querySelector('img.weather-school-day-sprite');
    expect(eveningImg?.getAttribute('src')).toMatch(
      /^\/static\/ui\/assets\/weather-packs\/ghibli_school_day\/cool_evening_v\d+\.jpg$/,
    );
  });

  it('renders school-day mode with default character pack and fallback when hourly is missing', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-default': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
              character_pack: 'none',
              variants_per_state: 0,
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 82,
              condition_text: 'Sunny',
              icon: 'weather-sunny',
            },
            daily: [
              {
                temp_max: 85,
                temp_min: 52,
                date: '2026-10-31',
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-default" type="weather-forecast" />,
    );

    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip).not.toBeNull();
    // Character pack defaults to ghibli_school_day when none/empty
    const sprites = container.querySelectorAll('img.weather-school-day-sprite');
    expect(sprites.length).toBe(3);
    for (const sprite of sprites) {
      expect(sprite.getAttribute('src')).toContain('/ghibli_school_day/');
    }

    // Morning uses minTemp (52 > 49 -> morning_mild)
    const morning = container.querySelector('.weather-school-day-slot-morning');
    expect(morning?.textContent).toContain('52°');
    expect(morning?.textContent).toContain('morning_mild');

    // Afternoon uses maxTemp (85 > 77 -> hot_dress)
    const afternoon = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoon?.textContent).toContain('85°');
    expect(afternoon?.textContent).toContain('hot_dress');
  });

  it('renders afternoon and evening in school-day mode with daily temp range fallback', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-daily': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
              date: '2026-10-31',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 70,
            },
            daily: [
              {
                temp_max: 80,
                temp_min: 50,
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-daily" type="weather-forecast" />,
    );
    const evening = container.querySelector('.weather-school-day-slot-evening');
    // (50 + 80) / 2 = 65 -> warm_pants (65 <= 77)
    expect(evening?.textContent).toContain('65°');
    expect(evening?.textContent).toContain('warm_pants');
  });

  it('renders school-day mode with direct slot overrides and rainy conditions', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-overrides': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
              character_pack: 'custom-pack',
              variants_per_state: 3,
              date: '2026-10-31',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 55,
              icon: 'weather-rainy',
            },
            slots: {
              morning: {
                temp: 50,
                precip_prob: 60,
                icon: 'weather-rainy',
              },
              afternoon: {
                temp: 55,
                precip_prob: 80,
                icon: 'weather-pouring',
              },
              evening: {
                temp: 45,
                precip_prob: 10,
                icon: 'weather-cloudy',
              },
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-overrides" type="weather-forecast" />,
    );

    const morning = container.querySelector('.weather-school-day-slot-morning');
    expect(morning?.textContent).toContain('morning_rain');
    expect(morning?.querySelector('img')?.getAttribute('src')).toContain('/custom-pack/');

    const afternoon = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoon?.textContent).toContain('rainy_boots');

    const evening = container.querySelector('.weather-school-day-slot-evening');
    expect(evening?.textContent).toContain('cold_puffer');
  });

  it('renders school-day mode fallback with root temperature and unparsed hourly times', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-unparsed': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            temperature: 68,
            hourly: [
              { time: 'invalid-time', temp: 65, precip_prob: 0, icon: 'weather-sunny' },
              { time: 'another-invalid', temp: 72, precip_prob: 5, icon: 'weather-sunny' },
              { time: 'third-invalid', temp: 60, precip_prob: 0, icon: 'weather-sunny' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-unparsed" type="weather-forecast" />,
    );

    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip).not.toBeNull();
    const morning = container.querySelector('.weather-school-day-slot-morning');
    expect(morning?.textContent).toContain('65°');
  });

  it('covers PascalCase and alternative property names for hourly and slots', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-pascal-slots': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            Current: {
              Temperature: 65,
              Icon: 'weather-sunny',
            },
            slots: {
              morning: {
                temperature: 45,
                precip: 10,
              },
            },
            Hourly: [
              null,
              { Time: '2026-10-31T14:00:00Z', Temp: 75, PrecipProb: 10, Icon: 'weather-sunny' },
              { Time: '2026-10-31T18:00:00Z', Temp: 60, PrecipProb: 5, Icon: 'weather-cloudy' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-pascal-slots" type="weather-forecast" />,
    );
    expect(container.querySelector('.weather-school-day-slot-morning')?.textContent).toContain('45°');
    expect(container.querySelector('.weather-school-day-slot-afternoon')?.textContent).toContain('75°');
  });

  it('covers PascalCase Daily in school-day mode fallback without hourly', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-pascal-daily': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            Current: {
              Temperature: 65,
            },
            Daily: [
              {
                TempMax: 78,
                TempMin: 48,
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-pascal-daily" type="weather-forecast" />,
    );
    expect(container.querySelector('.weather-school-day-slot-morning')?.textContent).toContain('48°');
    expect(container.querySelector('.weather-school-day-slot-afternoon')?.textContent).toContain('78°');
  });

  it('renders safely when widget id is not found in store', () => {
    const { container } = render(
      <WeatherForecastWidget id="non-existent-widget" type="weather-forecast" />,
    );
    expect(container.querySelector('.weather-loading')?.textContent).toContain(
      'Loading weather forecast...',
    );
    expect(container.querySelector('.weather-header')).toBeNull();
  });

  it('renders error banner alongside current weather data when state is error', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-err-with-data': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { name: 'Faulty Station' },
            assets_base: '',
          },
          state: 'error',
          data: {
            current: {
              temperature: 65,
              condition_text: 'Degraded',
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-err-with-data" type="weather-forecast" />,
    );
    expect(container.querySelector('.widget-error-banner')).not.toBeNull();
    expect(container.querySelector('.weather-location-name')?.textContent).toBe('Faulty Station');
    expect(container.querySelector('.weather-temp-main')?.textContent).toContain('65°');
  });

  it('extracts wind from wind and Wind properties and handles hourly with no temp or precip', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-wind-alt': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { mode: 'school-day' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 70,
              wind: 14,
            },
            hourly: [
              { time: '2026-10-31T08:00:00Z' },
              { time: '2026-10-31T14:00:00Z' },
              { time: '2026-10-31T18:00:00Z' },
            ],
          },
          timestamp: '2026-10',
        },
      },
    });

    const { container, rerender } = render(
      <WeatherForecastWidget id="weather-wind-alt" type="weather-forecast" />,
    );
    expect(container.querySelector('.weather-meta-wind')?.textContent).toContain('14 mph');

    // Also test PascalCase Wind and empty slots
    useDisplayStore.setState({
      widgets: {
        'weather-wind-alt': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { mode: 'school-day' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 70,
              Wind: 18,
            },
            slots: {
              morning: {},
              afternoon: {},
              evening: {},
            },
          },
          timestamp: '',
        },
      },
    });

    rerender(<WeatherForecastWidget id="weather-wind-alt" type="weather-forecast" />);
    expect(container.querySelector('.weather-meta-wind')?.textContent).toContain('18 mph');
    expect(container.querySelector('.weather-school-day-slot-morning')?.textContent).toContain('0°');
  });

  it('covers PascalCase Hourly properties for all daypart slots', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-pascal-hourly': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: { mode: 'school-day' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            Current: { Temperature: 60 },
            Hourly: [
              { Time: '2026-10-31T08:00:00Z', Temp: 52, PrecipProb: 5, Icon: 'weather-sunny' },
              { Time: '2026-10-31T14:00:00Z', Temp: 74, PrecipProb: 15, Icon: 'weather-partly-cloudy' },
              { Time: '2026-10-31T18:00:00Z', Temp: 58, PrecipProb: 10, Icon: 'weather-clear-night' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-pascal-hourly" type="weather-forecast" />,
    );
    expect(container.querySelector('.weather-school-day-slot-morning')?.textContent).toContain('52°');
    expect(container.querySelector('.weather-school-day-slot-afternoon')?.textContent).toContain('74°');
    expect(container.querySelector('.weather-school-day-slot-evening')?.textContent).toContain('58°');
  });

  it('covers fallbackSlotWeather without daily and without current', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-fallback-nodaily': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 3],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            temperature: 62,
            daily: [],
          },
          timestamp: 'short',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-fallback-nodaily" type="weather-forecast" />,
    );
    const slots = container.querySelectorAll('.weather-school-day-slot');
    expect(slots.length).toBe(3);
    expect(container.querySelector('.weather-school-day-slot-morning')?.textContent).toContain('62°');
    expect(container.querySelector('.weather-school-day-slot-afternoon')?.textContent).toContain('62°');
    expect(container.querySelector('.weather-school-day-slot-evening')?.textContent).toContain('62°');
  });
});

describe('WeatherIcon component', () => {
  afterEach(() => {
    cleanup();
  });

  const tokens = [
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
  ];

  for (const token of tokens) {
    it(`renders declarative SVG for token: ${token}`, () => {
      const { container } = render(<WeatherIcon name={token} size={24} />);
      const svg = container.querySelector('svg');
      expect(svg).not.toBeNull();
      expect(svg?.classList.contains(`mm-icon-${token}`)).toBe(true);
      expect(svg?.getAttribute('width')).toBe('24');
      expect(svg?.getAttribute('height')).toBe('24');
      expect(svg?.children.length).toBeGreaterThan(0);
    });
  }

  it('clamps size within [12, 64] and defaults to 20', () => {
    const { container: defaultContainer } = render(<WeatherIcon name="weather-sunny" />);
    expect(defaultContainer.querySelector('svg')?.getAttribute('width')).toBe('20');

    const { container: smallContainer } = render(<WeatherIcon name="weather-sunny" size={8} />);
    expect(smallContainer.querySelector('svg')?.getAttribute('width')).toBe('12');

    const { container: largeContainer } = render(<WeatherIcon name="weather-sunny" size={120} />);
    expect(largeContainer.querySelector('svg')?.getAttribute('width')).toBe('64');
  });

  it('falls back to weather-cloudy for unknown or null tokens and sanitizes token', () => {
    const { container: nullContainer } = render(<WeatherIcon name={null} />);
    expect(nullContainer.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(
      true,
    );

    const { container: emptyContainer } = render(<WeatherIcon name="" />);
    expect(emptyContainer.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(
      true,
    );

    const { container: spacesContainer } = render(<WeatherIcon name="   " size={NaN} />);
    expect(spacesContainer.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(
      true,
    );
    expect(spacesContainer.querySelector('svg')?.getAttribute('width')).toBe('20');

    const { container: negContainer } = render(<WeatherIcon name="weather-sunny" size={-5} />);
    expect(negContainer.querySelector('svg')?.getAttribute('width')).toBe('20');

    const { container: zeroContainer } = render(<WeatherIcon name="weather-sunny" size={0} />);
    expect(zeroContainer.querySelector('svg')?.getAttribute('width')).toBe('20');

    const { container: unknownContainer } = render(<WeatherIcon name="unsupported <script>" />);
    expect(
      unknownContainer.querySelector('svg')?.classList.contains('mm-icon-unsupportedscript'),
    ).toBe(true);

    const { container: pureSpecialContainer } = render(<WeatherIcon name="###$$$@@@" />);
    expect(
      pureSpecialContainer.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy'),
    ).toBe(true);
  });

  it('applies custom className and exercises memo cache on prop update', () => {
    const { container, rerender } = render(
      <WeatherIcon name="weather-sunny" className="custom-test-class" />,
    );
    expect(container.querySelector('svg')?.classList.contains('custom-test-class')).toBe(true);

    rerender(<WeatherIcon name="weather-sunny" className="updated-test-class" />);
    expect(container.querySelector('svg')?.classList.contains('updated-test-class')).toBe(true);
  });
});

describe('WeatherHourlyGraph component', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders nothing when hourly data is empty or invalid', () => {
    const { container: emptyContainer } = render(<WeatherHourlyGraph hourly={[]} />);
    expect(emptyContainer.firstChild).toBeNull();

    const { container: nullContainer } = render(<WeatherHourlyGraph hourly={null} />);
    expect(nullContainer.firstChild).toBeNull();
  });

  it('renders declarative SVG graph with timeline points and icons', () => {
    const hourly = [
      { time: '2026-10-31T12:00:00Z', temp: 68, precip_prob: 0, icon: 'weather-sunny' },
      { time: '2026-10-31T13:00:00Z', temp: 70, precip_prob: 25, icon: 'weather-rainy' },
    ];
    const { container, rerender } = render(
      <WeatherHourlyGraph hourly={hourly} widgetId="test-w" />,
    );
    const svg = container.querySelector('svg.weather-hourly-graph');
    expect(svg).not.toBeNull();
    expect(svg?.querySelector('linearGradient#mm-weather-grad-test-w')).not.toBeNull();
    expect(svg?.querySelector('path[d^="M"]')).not.toBeNull();
    expect(container.textContent).toContain('Now');
    // Exercise memoization cache hit on rerender with changed widgetId but identical hourly reference
    rerender(<WeatherHourlyGraph hourly={hourly} widgetId="test-w2" />);
    expect(container.querySelector('svg.weather-hourly-graph')).not.toBeNull();
    expect(container.textContent).toContain('☂ 25%');
    expect(container.textContent).toContain('68°');
    expect(container.textContent).toContain('70°');
  });

  it('fits 24 hourly points inside a narrow card without pixel minWidth clipping (#867)', () => {
    const cardWidth = 480;
    const pts = Array.from({ length: 24 }, (_, i) => ({
      time: `2026-09-26T${String(i).padStart(2, '0')}:00`,
      temp: 55 + (i % 10),
      icon: 'weather-sunny',
    }));
    const { container } = render(
      <div style={{ width: `${cardWidth}px` }}>
        <WeatherHourlyGraph
          hourly={pts}
          widgetId="narrow-card"
          options={{ containerWidth: cardWidth }}
        />
      </div>,
    );
    const svg = container.querySelector('svg.weather-hourly-graph') as SVGSVGElement;
    expect(svg).not.toBeNull();
    expect(svg.style.minWidth).toBe('');
    expect(svg.style.width).toBe('100%');
    expect(svg.style.maxWidth).toBe('100%');
    expect(svg.getAttribute('viewBox')).toBe('0 0 480.0 270.0');

    const labels = Array.from(svg.querySelectorAll('text'));
    const lastHourLabel = labels.find((el) => el.textContent === '9 AM');
    expect(lastHourLabel).toBeDefined();
    const lastX = Number(lastHourLabel?.getAttribute('x'));
    expect(lastX).toBeGreaterThan(0);
    expect(lastX).toBeLessThan(cardWidth);
  });

  it('exercises React compiler memo hit/miss cycles on WeatherHourlyGraph via parent state toggle', () => {
    function GraphParent({ hourly }: { hourly: unknown }) {
      const [count, setCount] = useState(0);
      return (
        <div>
          <button data-testid="rerender-btn" onClick={() => setCount((c) => c + 1)}>
            Increment {count}
          </button>
          <WeatherHourlyGraph hourly={hourly} widgetId="memo-graph" />
        </div>
      );
    }

    const hourly = [
      { time: '2026-10-31T12:00:00Z', temp: 65, precip_prob: 10, icon: 'weather-sunny' },
      { time: '2026-10-31T13:00:00Z', temp: 68, precip_prob: 0, icon: 'weather-partly-cloudy' },
    ];

    const { getByTestId, container } = render(<GraphParent hourly={hourly} />);
    expect(container.querySelector('svg.weather-hourly-graph')).not.toBeNull();

    // Trigger parent rerender with identical hourly reference to hit memo cache branch
    fireEvent.click(getByTestId('rerender-btn'));
    expect(getByTestId('rerender-btn').textContent).toContain('Increment 1');
    expect(container.querySelector('svg.weather-hourly-graph')).not.toBeNull();
  });

  it('renders WeatherHourlyGraph when gridLinesPath, areaPath, curvePath are empty or present with precipitation', () => {
    // 1. All paths present with non-zero precipitation
    const spy1 = vi.spyOn(formatModule, 'computeHourlyGraphLayout').mockReturnValueOnce({
      totalWidth: 500, svgHeight: 200, baseY: 180, isTall: false, gradientId: 'grad-full',
      areaPath: 'M 0 180 L 100 100 L 200 180 Z', curvePath: 'M 0 100 L 100 100',
      gridLinesPath: 'M 50 0 L 50 180',
      points: [{
        x: 50, y: 50, lineY1: 54, precipProb: 40, precipY: 12, icon: 'weather-rainy',
        iconTranslateX: 40, iconY: 14, iconScale: 0.8, tempRound: 65, tempY: 44,
        tempFontSize: 12, circleR: 3.5, timeLabel: 'Now', timeWeight: '800',
        timeColor: '#000', timeY: 102, timeFontSize: 11,
      }],
    });
    const { container: cFull } = render(<WeatherHourlyGraph hourly={[{ temp: 65 }]} />);
    expect(cFull.querySelector('svg.weather-hourly-graph')).not.toBeNull();
    expect(cFull.textContent).toContain('☂ 40%');
    expect(cFull.querySelectorAll('path').length).toBeGreaterThanOrEqual(3);
    spy1.mockRestore();

    // 2. All paths empty / falsy and precipProb = 0
    const spy2 = vi.spyOn(formatModule, 'computeHourlyGraphLayout').mockReturnValueOnce({
      totalWidth: 500, svgHeight: 200, baseY: 180, isTall: false, gradientId: 'grad-empty',
      areaPath: '', curvePath: '', gridLinesPath: '',
      points: [{
        x: 50, y: 50, lineY1: 54, precipProb: 0, precipY: 12, icon: 'weather-sunny',
        iconTranslateX: 40, iconY: 14, iconScale: 0.8, tempRound: 65, tempY: 44,
        tempFontSize: 12, circleR: 3.5, timeLabel: 'Now', timeWeight: '800',
        timeColor: '#000', timeY: 102, timeFontSize: 11,
      }],
    });
    const { container: cEmpty } = render(<WeatherHourlyGraph hourly={[{ temp: 65 }]} />);
    expect(cEmpty.querySelector('svg.weather-hourly-graph')).not.toBeNull();
    expect(cEmpty.textContent).not.toContain('☂');
    // Only the icon inside point glyph has a path; areaPath, curvePath, gridLinesPath are absent
    expect(cEmpty.querySelectorAll('path').length).toBe(1);
    spy2.mockRestore();
  });

  it('renders WeatherIcon size bounds, custom class, and glyph variations', () => {
    // Clamping > 64 to 64
    const { container: c1 } = render(
      <WeatherIcon name="weather-sunny" size={100} className="large-icon" />,
    );
    const svg1 = c1.querySelector('svg');
    expect(svg1?.getAttribute('width')).toBe('64');
    expect(svg1?.getAttribute('height')).toBe('64');
    expect(svg1?.classList.contains('large-icon')).toBe(true);

    // Clamping < 12 to 12
    const { container: c2 } = render(<WeatherIcon name="weather-cloudy" size={5} />);
    const svg2 = c2.querySelector('svg');
    expect(svg2?.getAttribute('width')).toBe('12');
    expect(svg2?.getAttribute('height')).toBe('12');

    // Invalid / NaN / negative size defaults to 20
    const { container: cDef1 } = render(<WeatherIcon name="weather-sunny" size={-10} />);
    expect(cDef1.querySelector('svg')?.getAttribute('width')).toBe('20');
    const { container: cDef2 } = render(<WeatherIcon name="weather-sunny" size={NaN} />);
    expect(cDef2.querySelector('svg')?.getAttribute('width')).toBe('20');

    // All WeatherIconGlyph branches
    const glyphs = [
      'weather-sunny', 'weather-clear-night', 'weather-partly-cloudy', 'weather-night-partly-cloudy',
      'weather-cloudy', 'weather-fog', 'weather-rainy', 'weather-pouring',
      'weather-snowy', 'weather-snowy-rainy', 'weather-lightning', 'weather-lightning-rainy',
    ];
    for (const glyph of glyphs) {
      const { container } = render(<WeatherIcon name={glyph} />);
      expect(container.querySelector('svg')).not.toBeNull();
      const { container: glyphContainer } = render(<svg><WeatherIconGlyph name={glyph} /></svg>);
      expect(glyphContainer.querySelector('path')).not.toBeNull();
    }

    // Name normalization: empty, whitespace, null, undefined, special chars sanitization
    const { container: cEmpty } = render(<WeatherIcon name="" />);
    expect(cEmpty.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(true);
    const { container: cSpaces } = render(<WeatherIcon name="   " />);
    expect(cSpaces.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(true);
    const { container: cNull } = render(<WeatherIcon name={null} />);
    expect(cNull.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(true);
    const { container: cSpecial } = render(<WeatherIcon name="weather$$sunny!!" />);
    expect(cSpecial.querySelector('svg')?.classList.contains('mm-icon-weathersunny')).toBe(true);
    const { container: cAllSpecial } = render(<WeatherIcon name="$$$###@@@" />);
    expect(cAllSpecial.querySelector('svg')?.classList.contains('mm-icon-weather-cloudy')).toBe(true);

    // WeatherIconGlyph standalone direct render inside SVG with special characters
    const { container: cGlyphSpecial } = render(<svg><WeatherIconGlyph name="weather#fog" /></svg>);
    expect(cGlyphSpecial.querySelector('path')).not.toBeNull();

    // WeatherIconGlyph default branch for unrecognized tokens
    const { container: cGlyphDefault } = render(<svg><WeatherIconGlyph name="weather-tornado" /></svg>);
    expect(cGlyphDefault.querySelector('path')).not.toBeNull();

    // WeatherIconGlyph with null / whitespace / non-matching characters
    const { container: cGlyphNull } = render(<svg><WeatherIconGlyph name={null} /></svg>);
    expect(cGlyphNull.querySelector('path')).not.toBeNull();
    const { container: cGlyphEmpty } = render(<svg><WeatherIconGlyph name="   " /></svg>);
    expect(cGlyphEmpty.querySelector('path')).not.toBeNull();
    const { container: cGlyphSymbols } = render(<svg><WeatherIconGlyph name="$$$" /></svg>);
    expect(cGlyphSymbols.querySelector('path')).not.toBeNull();
  });

  it('exercises React compiler memoization on WeatherIcon and WeatherIconGlyph across all tokens', () => {
    const allTokens = [
      'weather-sunny', 'weather-clear-night', 'weather-partly-cloudy', 'weather-night-partly-cloudy',
      'weather-cloudy', 'weather-fog', 'weather-rainy', 'weather-pouring',
      'weather-snowy', 'weather-snowy-rainy', 'weather-lightning', 'weather-lightning-rainy',
      'unknown-token', '',
    ];

    for (const token of allTokens) {
      const { rerender: rerenderGlyph, unmount: unmountGlyph } = render(<svg><WeatherIconGlyph name={token} /></svg>);
      rerenderGlyph(<svg><WeatherIconGlyph name={token} /></svg>);
      unmountGlyph();

      const { rerender: rerenderIcon, unmount: unmountIcon } = render(
        <WeatherIcon name={token} size={24} className="test-icon" />,
      );
      rerenderIcon(<WeatherIcon name={token} size={24} className="test-icon" />);
      unmountIcon();
    }
  });

  it('renders widget error banner when widget state is error', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-error-widget': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'error',
          data: {
            current: {
              temperature: 65,
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-error-widget" type="weather-forecast" />,
    );
    expect(container.querySelector('.widget-error-banner')).not.toBeNull();
    expect(container.querySelector('.widget-error-banner')?.textContent).toContain('Widget Error');
  });

  it('handles hourly slot resolution when time strings are missing and exercises hourly index fallback', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-hourly-no-time': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { mode: 'school-day' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              Temperature: 64,
            },
            hourly: [
              { Temp: 58, PrecipProb: 10 },
              { Temp: 68, PrecipProb: 20 },
              { Temp: 62, PrecipProb: 30 },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-hourly-no-time" type="weather-forecast" />,
    );
    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip).not.toBeNull();
    const morningSlot = strip?.querySelector('.weather-school-day-slot-morning');
    expect(morningSlot?.textContent).toContain('58°');
    const afternoonSlot = strip?.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoonSlot?.textContent).toContain('68°');
    const eveningSlot = strip?.querySelector('.weather-school-day-slot-evening');
    expect(eveningSlot?.textContent).toContain('62°');
  });

  it('handles school-day mode with fallback slot weather using PascalCase Temperature without daily or hourly', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-fallback-pascal': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { mode: 'school-day' },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              Temperature: 55,
              ConditionText: 'Cloudy',
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-fallback-pascal" type="weather-forecast" />,
    );
    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip).not.toBeNull();
    expect(strip?.textContent).toContain('55°');
  });

  it('rolls over school-day strip to tomorrow at night (>= 19:00) using tomorrow hourly and daily data', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-tomorrow': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 56,
              condition_text: 'Clear',
              icon: 'weather-clear-night',
            },
            hourly: [
              { time: '2026-10-31T20:00:00Z', temp: 55, precip_prob: 5, icon: 'weather-clear-night' },
              { time: '2026-11-01T08:00:00Z', temp: 46, precip_prob: 40, icon: 'weather-rainy' },
              { time: '2026-11-01T14:00:00Z', temp: 68, precip_prob: 15, icon: 'weather-partly-cloudy' },
              { time: '2026-11-01T18:00:00Z', temp: 58, precip_prob: 0, icon: 'weather-sunny' },
            ],
          },
          timestamp: '2026-10-31T20:15:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-tomorrow" type="weather-forecast" />,
    );

    const strip = container.querySelector('.weather-school-day-strip');
    expect(strip?.getAttribute('data-day')).toBe('tomorrow');

    const morningSlot = container.querySelector('.weather-school-day-slot-morning');
    expect(morningSlot?.getAttribute('data-day')).toBe('tomorrow');
    expect(morningSlot?.querySelector('.weather-school-day-name')?.textContent).toBe('tomorrow morning');
    expect(morningSlot?.querySelector('.weather-school-day-temp')?.textContent).toBe('46°');
    expect(morningSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe('Raincoat & Umbrella');
    expect(morningSlot?.querySelector('.weather-school-day-forecast')?.textContent).toBe('Rain Showers · 40% rain');

    const afternoonSlot = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoonSlot?.querySelector('.weather-school-day-name')?.textContent).toBe('tomorrow afternoon');
    expect(afternoonSlot?.querySelector('.weather-school-day-temp')?.textContent).toBe('68°');
    expect(afternoonSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe('T-Shirt & Long Pants');
    expect(afternoonSlot?.querySelector('.weather-school-day-forecast')?.textContent).toBe('Partly Cloudy · 15% rain');

    const eveningSlot = container.querySelector('.weather-school-day-slot-evening');
    expect(eveningSlot?.querySelector('.weather-school-day-name')?.textContent).toBe('tomorrow evening');
    expect(eveningSlot?.querySelector('.weather-school-day-temp')?.textContent).toBe('58°');
    expect(eveningSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe('Hoodie or Fleece');
    expect(eveningSlot?.querySelector('.weather-school-day-forecast')?.textContent).toBe('Sunny · 0% rain');
  });

  it('uses daily[1] fallback when rolling over to tomorrow at night without hourly data', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-tomorrow-daily': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 54,
              icon: 'weather-clear-night',
            },
            daily: [
              { temp_max: 65, temp_min: 50, date: '2026-10-31' },
              { temp_max: 80, temp_min: 44, date: '2026-11-01' },
            ],
          },
          timestamp: '2026-10-31T19:00:00Z',
        },
      },
    });

    const { container } = render(
      <WeatherForecastWidget id="weather-school-tomorrow-daily" type="weather-forecast" />,
    );

    const morningSlot = container.querySelector('.weather-school-day-slot-morning');
    expect(morningSlot?.querySelector('.weather-school-day-temp')?.textContent).toBe('44°');
    expect(morningSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe('Warm Puffer Coat');

    const afternoonSlot = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoonSlot?.querySelector('.weather-school-day-temp')?.textContent).toBe('80°');
    expect(afternoonSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe('Shorts or Sun Dress');
  });

  it('evaluates schoolDayPack outfit labels, icon conditions, and date rollover helpers', () => {
    expect(resolveClothingOutfitLabel('morning_rain')).toBe('Raincoat & Umbrella');
    expect(resolveClothingOutfitLabel('rainy_boots')).toBe('Raincoat & Boots');
    expect(resolveClothingOutfitLabel('cold_puffer')).toBe('Warm Puffer Coat');
    expect(resolveClothingOutfitLabel('morning_mild')).toBe('Light Jacket & Pants');
    expect(resolveClothingOutfitLabel('cool_evening')).toBe('Hoodie or Fleece');
    expect(resolveClothingOutfitLabel('warm_pants')).toBe('T-Shirt & Long Pants');
    expect(resolveClothingOutfitLabel('hot_dress')).toBe('Shorts or Sun Dress');

    expect(resolveIconConditionLabel('weather-pouring')).toBe('Heavy Rain');
    expect(resolveIconConditionLabel('weather-lightning-rainy')).toBe('Thunderstorms');
    expect(resolveIconConditionLabel('weather-snowy')).toBe('Snow');
    expect(resolveIconConditionLabel('weather-rainy')).toBe('Rain Showers');
    expect(resolveIconConditionLabel('weather-fog')).toBe('Foggy');
    expect(resolveIconConditionLabel('weather-partly-cloudy')).toBe('Partly Cloudy');
    expect(resolveIconConditionLabel('weather-cloudy')).toBe('Cloudy');
    expect(resolveIconConditionLabel('weather-clear-night')).toBe('Clear');
    expect(resolveIconConditionLabel('weather-sunny')).toBe('Sunny');

    expect(formatSlotForecastSummary('weather-partly-cloudy', 25)).toBe('Partly Cloudy · 25% rain');
    expect(formatSlotForecastSummary('weather-sunny', NaN)).toBe('Sunny · 0% rain');

    expect(addOneCalendarDay('2026-10-31')).toBe('2026-11-01');
    expect(addOneCalendarDay('invalid-date')).toBe('invalid-date');

    expect(resolveSchoolDayTargetDate({ date: '2026-12-25' }, '2026-10-31T22:00:00Z')).toEqual({
      dateStr: '2026-12-25',
      isTomorrow: false,
    });
    expect(resolveSchoolDayTargetDate({}, '2026-10-31T19:05:00Z')).toEqual({
      dateStr: '2026-11-01',
      isTomorrow: true,
    });
    expect(
      resolveSchoolDayTargetDate({ timezone: 'America/Los_Angeles' }, '2026-11-03T04:15:00Z'),
    ).toEqual({
      dateStr: '2026-11-03',
      isTomorrow: true,
    });
    expect(
      resolveSchoolDayTargetDate({ timezone: 'Invalid/Timezone' }, '2026-10-31T19:05:00Z'),
    ).toEqual({
      dateStr: '2026-11-01',
      isTomorrow: true,
    });
    expect(resolveSchoolDayTargetDate({}, '2026-10-31')).toEqual({
      dateStr: '2026-10-31',
      isTomorrow: false,
    });
  });

  it('renders configurable birthday override badge, outfit prefix, and birthday sprite URLs when birthdays match', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-birthday': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
              character_pack: 'ghibli_school_day',
              date: '2026-11-07',
              birthdays: [{ name: 'ChildOne', date: '11-07' }],
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 65,
              icon: 'weather-sunny',
            },
            slots: {
              morning: { temp: 52, precip_prob: 0, icon: 'weather-sunny' },
              afternoon: { temp: 72, precip_prob: 0, icon: 'weather-sunny' },
              evening: { temp: 60, precip_prob: 0, icon: 'weather-clear-night' },
            },
          },
          timestamp: '2026-11-07T09:00:00Z',
        },
      },
    });

    const { container, rerender } = render(
      <WeatherForecastWidget id="weather-school-birthday" type="weather-forecast" />,
    );

    const morningSlot = container.querySelector('.weather-school-day-slot-morning');
    expect(morningSlot?.getAttribute('data-birthday')).toBe('true');
    expect(morningSlot?.querySelector('.weather-school-day-birthday-badge')?.textContent).toBe(
      '🎂 Happy Birthday ChildOne!',
    );
    expect(morningSlot?.querySelector('.weather-school-day-outfit')?.textContent).toBe(
      '🎂 Birthday Celebration! · Light Jacket & Pants',
    );
    expect(morningSlot?.querySelector('img.weather-school-day-sprite')?.getAttribute('src')).toBe(
      '/static/ui/assets/weather-packs/ghibli_school_day/birthday_childone_morning.jpg',
    );

    const afternoonSlot = container.querySelector('.weather-school-day-slot-afternoon');
    expect(afternoonSlot?.querySelector('img.weather-school-day-sprite')?.getAttribute('src')).toBe(
      '/static/ui/assets/weather-packs/ghibli_school_day/birthday_childone_afternoon.jpg',
    );

    const eveningSlot = container.querySelector('.weather-school-day-slot-evening');
    expect(eveningSlot?.querySelector('img.weather-school-day-sprite')?.getAttribute('src')).toBe(
      '/static/ui/assets/weather-packs/ghibli_school_day/birthday_childone_evening.jpg',
    );

    // Non-matching date should not render birthday override
    useDisplayStore.setState({
      widgets: {
        'weather-school-birthday': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
              character_pack: 'ghibli_school_day',
              date: '2026-11-08',
              birthdays: [{ name: 'ChildOne', date: '11-07' }],
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 65,
              icon: 'weather-sunny',
            },
            slots: {
              morning: { temp: 52, precip_prob: 0, icon: 'weather-sunny' },
              afternoon: { temp: 72, precip_prob: 0, icon: 'weather-sunny' },
              evening: { temp: 60, precip_prob: 0, icon: 'weather-clear-night' },
            },
          },
          timestamp: '2026-11-08T09:00:00Z',
        },
      },
    });

    rerender(<WeatherForecastWidget id="weather-school-birthday" type="weather-forecast" />);
    const nonBirthdayMorning = container.querySelector('.weather-school-day-slot-morning');
    expect(nonBirthdayMorning?.getAttribute('data-birthday')).toBeNull();
    expect(nonBirthdayMorning?.querySelector('.weather-school-day-birthday-badge')).toBeNull();
    expect(nonBirthdayMorning?.querySelector('.weather-school-day-outfit')?.textContent).toBe(
      'Light Jacket & Pants',
    );
    expect(
      nonBirthdayMorning?.querySelector('img.weather-school-day-sprite')?.getAttribute('src'),
    ).toMatch(/^\/static\/ui\/assets\/weather-packs\/ghibli_school_day\/morning_mild_v\d+\.jpg$/);
  });

  it('applies custom shadow_opacity config as --weather-scrim-opacity on .weather-school-day-strip', () => {
    useDisplayStore.setState({
      widgets: {
        'weather-school-shadow': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
              shadow_opacity: 0.9,
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 68,
              icon: 'weather-sunny',
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container, rerender } = render(
      <WeatherForecastWidget id="weather-school-shadow" type="weather-forecast" />,
    );

    const strip = container.querySelector('.weather-school-day-strip') as HTMLElement | null;
    expect(strip).not.toBeNull();
    expect(strip?.style.getPropertyValue('--weather-scrim-opacity')).toBe('0.9');

    // Out-of-range shadow_opacity (> 1) should be ignored
    useDisplayStore.setState({
      widgets: {
        'weather-school-shadow': {
          meta: {
            type: 'weather-forecast',
            origin: [0, 0],
            dimensions: [2, 1],
            config: {
              mode: 'school-day',
              shadow_opacity: 1.5,
            },
            assets_base: '',
          },
          state: 'healthy',
          data: {
            current: {
              temperature: 68,
              icon: 'weather-sunny',
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    rerender(<WeatherForecastWidget id="weather-school-shadow" type="weather-forecast" />);
    const stripInvalid = container.querySelector('.weather-school-day-strip') as HTMLElement | null;
    expect(stripInvalid?.style.getPropertyValue('--weather-scrim-opacity')).toBe('');
  });

  it('defines school-day full-bleed container, hidden legacy sections, slot styling, and slot hour labels in WeatherForecastWidget.css', () => {
    expect(weatherForecastCss).toMatch(
      /\.widget-card\[data-widget-type=["']weather-forecast["']\]:has\(\.weather-school-day-strip\)\s*\{[^}]*padding:\s*6px;/,
    );
    expect(weatherForecastCss).toMatch(
      /\.widget-weather:has\(\.weather-school-day-strip\)\s*\{[^}]*display:\s*block;[^}]*padding:\s*0;[^}]*width:\s*100%;[^}]*height:\s*100%;/s,
    );
    expect(weatherForecastCss).toMatch(
      /\.widget-weather:has\(\.weather-school-day-strip\)\s*>\s*\.weather-header[\s\S]*?\.widget-weather:has\(\.weather-school-day-strip\)\s*>\s*\.weather-container[\s\S]*?\.widget-weather:has\(\.weather-school-day-strip\)\s*>\s*\.weather-micro-stats\s*\{[^}]*display:\s*none;/,
    );
    expect(weatherForecastCss).toMatch(
      /\.weather-school-day-strip\s*\{[^}]*gap:\s*8px;/s,
    );
    expect(weatherForecastCss).toMatch(
      /\.weather-school-day-slot\s*\{[^}]*box-shadow:\s*none;[^}]*border:\s*2px solid rgba\(255,\s*255,\s*255,\s*0\.32\);/s,
    );
    expect(weatherForecastCss).toMatch(
      /\.weather-school-day-slot-morning\s+\.weather-school-day-name::after\s*\{[^}]*content:\s*['"]\s*·\s*8 AM['"];/s,
    );
    expect(weatherForecastCss).toMatch(
      /\.weather-school-day-slot-afternoon\s+\.weather-school-day-name::after\s*\{[^}]*content:\s*['"]\s*·\s*2 PM['"];/s,
    );
    expect(weatherForecastCss).toMatch(
      /\.weather-school-day-slot-evening\s+\.weather-school-day-name::after\s*\{[^}]*content:\s*['"]\s*·\s*6 PM['"];/s,
    );
  });
});
