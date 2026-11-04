/**
 * Cursor suppression query overrides (?hide_cursor=true/false).
 * Ported from web/static/js/display.js.
 */
export function applyCursorSuppression(search?: string, body?: HTMLElement | null): void {
  const targetBody = body !== undefined ? body : (typeof document !== 'undefined' ? document.body : null);
  if (!targetBody) return;

  const searchStr = search !== undefined ? search : (typeof window !== 'undefined' && window.location ? window.location.search : '');
  if (!searchStr) return;

  const urlParams = new URLSearchParams(searchStr);
  const hideCursor = urlParams.get('hide_cursor');

  if (hideCursor === 'true') {
    targetBody.classList.add('mm-touch-display', 'mm-touch-kiosk');
  } else if (hideCursor === 'false') {
    targetBody.classList.remove('mm-touch-display', 'mm-touch-kiosk');
  }
}
