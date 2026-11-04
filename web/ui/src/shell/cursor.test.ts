import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { applyCursorSuppression } from './cursor';

describe('applyCursorSuppression', () => {
  let body: HTMLBodyElement;

  beforeEach(() => {
    body = document.createElement('body');
  });

  afterEach(() => {
    body.className = '';
  });

  it('adds touch display and kiosk classes when hide_cursor=true', () => {
    applyCursorSuppression('?hide_cursor=true', body);
    expect(body.classList.contains('mm-touch-display')).toBe(true);
    expect(body.classList.contains('mm-touch-kiosk')).toBe(true);
  });

  it('removes touch classes when hide_cursor=false', () => {
    body.classList.add('mm-touch-display', 'mm-touch-kiosk');
    applyCursorSuppression('?hide_cursor=false', body);
    expect(body.classList.contains('mm-touch-display')).toBe(false);
    expect(body.classList.contains('mm-touch-kiosk')).toBe(false);
  });

  it('leaves body unchanged on unrelated query parameters', () => {
    applyCursorSuppression('?foo=bar', body);
    expect(body.className).toBe('');
    applyCursorSuppression('?cursor=none', body);
    expect(body.className).toBe('');
    applyCursorSuppression('?cursor=visible', body);
    expect(body.className).toBe('');
  });
});
