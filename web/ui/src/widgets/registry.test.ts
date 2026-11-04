import { describe, expect, it } from 'vitest';
import { WIDGET_COMPONENTS, widgetComponentFor } from './registry';

describe('widget registry', () => {
  it('has no component for unknown or missing types, so WidgetHost falls back to LegacyWidget', () => {
    expect(widgetComponentFor(undefined)).toBeUndefined();
    expect(widgetComponentFor('not-a-widget')).toBeUndefined();
  });

  it('does not resolve inherited object keys as components', () => {
    expect(widgetComponentFor('constructor')).toBeUndefined();
    expect(widgetComponentFor('__proto__')).toBeUndefined();
  });

  it('registers spacer, tasks, weather-forecast, chat-log, calendar-feed, calendar-grid, photo-carousel, live-view, plex-recent, and yt-music components', () => {
    expect(widgetComponentFor('spacer')).toBeDefined();
    expect(widgetComponentFor('tasks')).toBeDefined();
    expect(widgetComponentFor('weather-forecast')).toBeDefined();
    expect(widgetComponentFor('chat-log')).toBeDefined();
    expect(widgetComponentFor('calendar-feed')).toBeDefined();
    expect(widgetComponentFor('calendar-grid')).toBeDefined();
    expect(widgetComponentFor('photo-carousel')).toBeDefined();
    expect(widgetComponentFor('live-view')).toBeDefined();
    expect(widgetComponentFor('plex-recent')).toBeDefined();
    expect(widgetComponentFor('yt-music')).toBeDefined();
  });

  it('returns a registered component by type', () => {
    const Stub = () => null;
    Object.assign(WIDGET_COMPONENTS, { 'test-only-type': Stub });
    try {
      expect(widgetComponentFor('test-only-type')).toBe(Stub);
    } finally {
      delete (WIDGET_COMPONENTS as Record<string, unknown>)['test-only-type'];
    }
  });
});
