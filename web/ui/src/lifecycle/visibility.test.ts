import { afterEach, describe, expect, it, vi } from 'vitest';
import { observeDocumentVisibility, reflectVisibleAttribute } from './visibility';

let hidden = false;
Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });

describe('observeDocumentVisibility', () => {
  afterEach(() => {
    hidden = false;
  });

  it('reports visibility changes and pagehide, and stops after dispose', () => {
    const setVisible = vi.fn();
    const dispose = observeDocumentVisibility(document, window, setVisible);
    expect(setVisible).toHaveBeenLastCalledWith(true);

    hidden = true;
    document.dispatchEvent(new Event('visibilitychange'));
    expect(setVisible).toHaveBeenLastCalledWith(false);

    hidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    expect(setVisible).toHaveBeenLastCalledWith(true);

    window.dispatchEvent(new Event('pagehide'));
    expect(setVisible).toHaveBeenLastCalledWith(false);

    window.dispatchEvent(new Event('pageshow'));
    expect(setVisible).toHaveBeenLastCalledWith(true);

    const callsBeforeDispose = setVisible.mock.calls.length;
    dispose();
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('pagehide'));
    expect(setVisible).toHaveBeenCalledTimes(callsBeforeDispose);
  });
});

describe('reflectVisibleAttribute', () => {
  it('writes the visibility slice to <html data-visible> as "true" or "false"', () => {
    const doc = { documentElement: { dataset: {} as Record<string, string> } } as unknown as Document;
    reflectVisibleAttribute(doc, false);
    expect(doc.documentElement.dataset.visible).toBe('false');
    reflectVisibleAttribute(doc, true);
    expect(doc.documentElement.dataset.visible).toBe('true');
  });
});
