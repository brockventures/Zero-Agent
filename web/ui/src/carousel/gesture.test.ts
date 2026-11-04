import { describe, expect, it } from 'vitest';
import {
  COMMIT_DISTANCE_RATIO,
  INTENT_PX,
  isExcludedTarget,
  releaseDirection,
  SwipeGesture,
  velocityOf,
  type PointerSample,
} from './gesture';

const at = (x: number, y: number, t: number): PointerSample => ({ x, y, t });

describe('releaseDirection', () => {
  const width = 1000;

  it('commits next when the drag passes 25% of the width to the left', () => {
    expect(releaseDirection({ dx: -(width * COMMIT_DISTANCE_RATIO) - 1, velocity: 0 }, width)).toBe('next');
  });

  it('commits prev when the drag passes 25% of the width to the right', () => {
    expect(releaseDirection({ dx: width * COMMIT_DISTANCE_RATIO + 1, velocity: 0 }, width)).toBe('prev');
  });

  it('springs back at exactly 25% of the width', () => {
    expect(releaseDirection({ dx: -width * COMMIT_DISTANCE_RATIO, velocity: 0 }, width)).toBeNull();
  });

  it('commits on velocity above 0.4 px/ms in the drag direction, however short the drag', () => {
    expect(releaseDirection({ dx: -40, velocity: -0.41 }, width)).toBe('next');
    expect(releaseDirection({ dx: 40, velocity: 0.41 }, width)).toBe('prev');
  });

  it('springs back at exactly 0.4 px/ms', () => {
    expect(releaseDirection({ dx: -40, velocity: -0.4 }, width)).toBeNull();
  });

  it('springs back on a fast flick against the drag direction', () => {
    expect(releaseDirection({ dx: -40, velocity: 2 }, width)).toBeNull();
  });

  it('springs back on a short, slow drag', () => {
    expect(releaseDirection({ dx: -50, velocity: -0.1 }, width)).toBeNull();
  });

  it('never commits without a width to measure against', () => {
    expect(releaseDirection({ dx: -500, velocity: -1 }, 0)).toBeNull();
  });
});

describe('velocityOf', () => {
  it('is zero with fewer than two samples', () => {
    expect(velocityOf([])).toBe(0);
    expect(velocityOf([at(0, 0, 0)])).toBe(0);
  });

  it('uses only the last 100 ms of samples', () => {
    const samples = [at(0, 0, 0), at(500, 0, 400), at(520, 0, 450), at(540, 0, 500)];
    // Window starts at t=400: (540 - 500) / (500 - 400) = 0.4
    expect(velocityOf(samples)).toBeCloseTo(0.4);
  });

  it('is near zero when the finger stopped before lifting', () => {
    const samples = [at(0, 0, 0), at(200, 0, 100), at(200, 0, 300), at(200, 0, 350)];
    expect(velocityOf(samples)).toBe(0);
  });
});

describe('isExcludedTarget', () => {
  it('excludes elements inside data-prevent-screen-swipe', () => {
    const guarded = document.createElement('div');
    const inner = document.createElement('span');
    guarded.setAttribute('data-prevent-screen-swipe', '');
    guarded.appendChild(inner);
    expect(isExcludedTarget(guarded)).toBe(true);
    expect(isExcludedTarget(inner)).toBe(true);
  });

  it('does not exclude calendar grids, ordinary widgets, or non-element targets', () => {
    for (const cls of ['widget-calendar-grid', 'widget-calendar-family']) {
      const el = document.createElement('div');
      el.className = cls;
      expect(isExcludedTarget(el)).toBe(false);
    }
    const plain = document.createElement('div');
    expect(isExcludedTarget(plain)).toBe(false);
    expect(isExcludedTarget(null)).toBe(false);
  });
});

describe('SwipeGesture', () => {
  it('ignores a gesture that is not allowed', () => {
    const g = new SwipeGesture();
    expect(g.begin(at(100, 100, 0), false)).toBe(false);
    expect(g.active).toBe(false);
    expect(g.move(at(50, 100, 10))).toBeNull();
  });

  it('stays pending below the intent threshold and reports no release for a tap', () => {
    const g = new SwipeGesture();
    g.begin(at(100, 100, 0), true);
    expect(g.move(at(100 - INTENT_PX + 1, 100, 10))).toBeNull();
    expect(g.horizontal).toBe(false);
    expect(g.end(at(100 - INTENT_PX + 1, 100, 20))).toBeNull();
  });

  it('becomes horizontal at 10 px and reports the displacement from the press point', () => {
    const g = new SwipeGesture();
    g.begin(at(100, 100, 0), true);
    expect(g.move(at(88, 102, 10))).toBe(-12);
    expect(g.horizontal).toBe(true);
    expect(g.move(at(70, 103, 20))).toBe(-30);
  });

  it('releases vertically when |dy| > |dx| at the intent threshold, and never tracks again', () => {
    const g = new SwipeGesture();
    g.begin(at(100, 100, 0), true);
    expect(g.move(at(104, 115, 10))).toBeNull();
    expect(g.horizontal).toBe(false);
    expect(g.move(at(40, 115, 20))).toBeNull();
    expect(g.end(at(40, 115, 30))).toBeNull();
  });

  it('reports dx and velocity on release of a horizontal gesture', () => {
    const g = new SwipeGesture();
    g.begin(at(300, 100, 0), true);
    g.move(at(280, 100, 20));
    g.move(at(200, 100, 40));
    const release = g.end(at(120, 100, 60));
    expect(release?.dx).toBe(-180);
    // Window from t=-40 (clamped to first sample at t=0): (120 - 300) / 60 = -3
    expect(release?.velocity).toBeCloseTo(-3);
    expect(g.active).toBe(false);
  });

  it('cancel reports whether a horizontal drag must spring back', () => {
    const g = new SwipeGesture();
    g.begin(at(0, 0, 0), true);
    expect(g.cancel()).toBe(false);

    g.begin(at(0, 0, 0), true);
    g.move(at(-30, 0, 10));
    expect(g.cancel()).toBe(true);
    expect(g.active).toBe(false);
  });
});
