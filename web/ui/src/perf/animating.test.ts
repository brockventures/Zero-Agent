import { afterEach, describe, expect, it } from 'vitest';
import { beginAnimation, endAnimation } from './animating';

describe('animating ref-count', () => {
  afterEach(() => {
    // Drain any leftover count so tests stay independent.
    for (let i = 0; i < 5; i++) endAnimation();
    delete document.body.dataset.animating;
  });

  it('keeps the flag set while any animation runs', () => {
    beginAnimation();
    beginAnimation();
    expect(document.body.dataset.animating).toBe('1');

    endAnimation();
    expect(document.body.dataset.animating).toBe('1');

    endAnimation();
    expect(document.body.dataset.animating).toBe('0');
  });

  it('does not go negative on extra endAnimation calls', () => {
    beginAnimation();
    endAnimation();
    endAnimation();
    beginAnimation();
    expect(document.body.dataset.animating).toBe('1');
    endAnimation();
    expect(document.body.dataset.animating).toBe('0');
  });
});
