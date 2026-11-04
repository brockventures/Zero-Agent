import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BufferedEvent } from '../sse/batch';
import type { ScreenRotatePayload, ScreensResponse } from '../sse/types';
import { rotateChangesLayout, useDisplayStore } from './index';

const initial = useDisplayStore.getState();

const rotate = (current: number, total = 3): BufferedEvent => ({
  type: 'screen.rotate',
  data: {
    current_screen: current,
    total_screens: total,
    interval_seconds: 30,
    widgets: [{ widget_id: `w-${current}`, origin: [0, 0], dimensions: [2, 2] }],
  } satisfies ScreenRotatePayload,
});

const screens: ScreensResponse = {
  current_screen: 1,
  interval_seconds: 30,
  screens: [0, 1, 2].map((index) => ({
    index,
    widgets: [{ widget_id: `w-${index}`, type: 'clock', origin: [0, 0], dimensions: [2, 2] }],
  })),
};

function stubScreens(body: ScreensResponse = screens) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('carousel store', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useDisplayStore.setState(initial, true);
  });

  describe('pause and resume', () => {
    it('queues the latest screen.rotate while paused and leaves the shown screen alone', () => {
      useDisplayStore.getState().pause();
      useDisplayStore.getState().applyEvents([rotate(1)]);
      useDisplayStore.getState().applyEvents([rotate(2)]);

      const state = useDisplayStore.getState();
      expect(state.screen.currentScreen).toBe(0);
      expect(state.carousel.paused).toBe(true);
      expect(state.carousel.pendingRotate?.current_screen).toBe(2);
    });

    it('applies the queued rotate on resume and clears it', () => {
      useDisplayStore.getState().pause();
      useDisplayStore.getState().applyEvents([rotate(2)]);
      useDisplayStore.getState().resume();

      const state = useDisplayStore.getState();
      expect(state.carousel.paused).toBe(false);
      expect(state.carousel.pendingRotate).toBeNull();
      expect(state.screen.currentScreen).toBe(2);
      expect(state.screen.widgets).toEqual([{ widget_id: 'w-2', origin: [0, 0], dimensions: [2, 2] }]);
    });

    it('keeps the shown screen when resuming with nothing queued', () => {
      useDisplayStore.getState().applyEvents([rotate(1)]);
      useDisplayStore.getState().pause();
      useDisplayStore.getState().resume();
      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
    });

    it('is idempotent for repeated pause and resume calls', () => {
      useDisplayStore.getState().pause();
      const paused = useDisplayStore.getState().carousel;
      useDisplayStore.getState().pause();
      expect(useDisplayStore.getState().carousel).toBe(paused);

      useDisplayStore.getState().resume();
      const resumed = useDisplayStore.getState().carousel;
      useDisplayStore.getState().resume();
      expect(useDisplayStore.getState().carousel).toBe(resumed);
    });
  });

  describe('layoutVersion', () => {
    it('bumps on widget.reload', () => {
      useDisplayStore.getState().applyEvents([{ type: 'widget.reload', data: { type: 'clock' } }]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);
    });

    it('bumps when the config status or error changes', () => {
      useDisplayStore.getState().applyEvents([
        { type: 'system.status', data: { online: true, time: 't', config_status: 'error', config_error: 'bad' } },
      ]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);

      useDisplayStore.getState().applyEvents([
        { type: 'system.status', data: { online: true, time: 't', config_status: 'error', config_error: 'bad' } },
      ]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);
    });

    it('bumps when the boot id changes after the first one is known', () => {
      useDisplayStore.getState().applyEvents([
        { type: 'system.status', data: { online: true, time: 't', config_status: 'ok', config_error: null, boot_id: 'a' } },
      ]);
      expect(useDisplayStore.getState().layoutVersion).toBe(0);

      useDisplayStore.getState().applyEvents([
        { type: 'system.status', data: { online: true, time: 't', config_status: 'ok', config_error: null, boot_id: 'b' } },
      ]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);
    });

    it('does not bump for ordinary status heartbeats', () => {
      useDisplayStore.getState().applyEvents([
        { type: 'system.status', data: { online: true, time: 't', config_status: 'ok', config_error: null } },
      ]);
      expect(useDisplayStore.getState().layoutVersion).toBe(0);
    });
  });

  describe('rotateChangesLayout', () => {
    const current = screens.screens;
    const payload = (overrides: Partial<ScreenRotatePayload> = {}): ScreenRotatePayload => ({
      current_screen: 1,
      total_screens: 3,
      interval_seconds: 30,
      widgets: [{ widget_id: 'w-1', origin: [0, 0], dimensions: [2, 2] }],
      ...overrides,
    });

    it('is false for an empty layout, because nothing is known yet', () => {
      expect(rotateChangesLayout([], payload({ total_screens: 5 }))).toBe(false);
    });

    it('is false for identical placements', () => {
      expect(rotateChangesLayout(current, payload())).toBe(false);
    });

    it('is true for a changed total_screens', () => {
      expect(rotateChangesLayout(current, payload({ total_screens: 4 }))).toBe(true);
    });

    it('is true when the current screen is missing from the layout', () => {
      expect(rotateChangesLayout(current, payload({ current_screen: 7, total_screens: 3 }))).toBe(true);
    });

    it('is true for a moved widget', () => {
      expect(rotateChangesLayout(current, payload({ widgets: [{ widget_id: 'w-1', origin: [1, 0], dimensions: [2, 2] }] }))).toBe(true);
    });

    it('is true for a resized widget and for an added or removed widget', () => {
      expect(rotateChangesLayout(current, payload({ widgets: [{ widget_id: 'w-1', origin: [0, 0], dimensions: [3, 2] }] }))).toBe(true);
      expect(
        rotateChangesLayout(current, payload({ widgets: [{ widget_id: 'w-1', origin: [0, 0], dimensions: [2, 2] }, { widget_id: 'x', origin: [0, 0], dimensions: [1, 1] }] })),
      ).toBe(true);
      expect(rotateChangesLayout(current, payload({ widgets: [] }))).toBe(true);
    });

    it('is true for a changed widget id', () => {
      expect(rotateChangesLayout(current, payload({ widgets: [{ widget_id: 'w-9', origin: [0, 0], dimensions: [2, 2] }] }))).toBe(true);
    });
  });

  describe('layoutVersion on rotate and resume', () => {
    const moved = (): BufferedEvent => ({
      type: 'screen.rotate',
      data: {
        current_screen: 1,
        total_screens: 3,
        interval_seconds: 30,
        widgets: [{ widget_id: 'w-1', origin: [4, 0], dimensions: [2, 2] }],
      } satisfies ScreenRotatePayload,
    });

    beforeEach(() => {
      useDisplayStore.setState({ layout: screens.screens });
    });

    it('bumps layoutVersion when a rotate changes placements, with the carousel unpaused', () => {
      useDisplayStore.getState().applyEvents([moved()]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);
      expect(useDisplayStore.getState().screen.currentScreen).toBe(1);
    });

    it('bumps layoutVersion when a rotate changes placements, with the carousel paused', () => {
      useDisplayStore.getState().pause();
      useDisplayStore.getState().applyEvents([moved()]);
      expect(useDisplayStore.getState().layoutVersion).toBe(1);
      expect(useDisplayStore.getState().carousel.pendingRotate).not.toBeNull();
    });

    it('does not bump layoutVersion for a rotate that matches the layout', () => {
      useDisplayStore.getState().applyEvents([rotate(1)]);
      expect(useDisplayStore.getState().layoutVersion).toBe(0);
    });

    it('bumps layoutVersion on resume when the pending rotate changes the layout', () => {
      useDisplayStore.getState().pause();
      useDisplayStore.getState().applyEvents([moved()]);
      const before = useDisplayStore.getState().layoutVersion;
      useDisplayStore.getState().resume();
      expect(useDisplayStore.getState().layoutVersion).toBe(before + 1);
      expect(useDisplayStore.getState().screen.widgets).toEqual([{ widget_id: 'w-1', origin: [4, 0], dimensions: [2, 2] }]);
    });

    it('does not bump layoutVersion on resume when the pending rotate matches the layout', () => {
      useDisplayStore.getState().pause();
      useDisplayStore.getState().applyEvents([rotate(2)]);
      const before = useDisplayStore.getState().layoutVersion;
      useDisplayStore.getState().resume();
      expect(useDisplayStore.getState().layoutVersion).toBe(before);
    });
  });

  describe('setCurrentScreen', () => {
    it('sets currentScreen and leaves the other screen fields alone', () => {
      useDisplayStore.getState().applyEvents([rotate(1)]);
      const before = useDisplayStore.getState().screen;
      useDisplayStore.getState().setCurrentScreen(2);
      expect(useDisplayStore.getState().screen).toEqual({ ...before, currentScreen: 2 });
    });

    it('does nothing when the value is already the current screen', () => {
      const before = useDisplayStore.getState().screen;
      const listener = vi.fn();
      const unsubscribe = useDisplayStore.subscribe(listener);
      useDisplayStore.getState().setCurrentScreen(before.currentScreen);
      expect(useDisplayStore.getState().screen).toBe(before);
      expect(listener).not.toHaveBeenCalled();
      unsubscribe();
    });
  });

  describe('loadScreens', () => {
    it('stores the layout of every screen and syncs the screen count', async () => {
      const fetchMock = stubScreens();
      await useDisplayStore.getState().loadScreens();

      expect(fetchMock).toHaveBeenCalledWith('api/screens', expect.anything());
      expect(useDisplayStore.getState().layout).toEqual(screens.screens);
      expect(useDisplayStore.getState().screen.totalScreens).toBe(3);
    });

    it('leaves the layout reference alone when the response is unchanged', async () => {
      stubScreens();
      await useDisplayStore.getState().loadScreens();
      const first = useDisplayStore.getState().layout;
      await useDisplayStore.getState().loadScreens();
      expect(useDisplayStore.getState().layout).toBe(first);
    });

    it('rejects on a non-OK response and keeps the previous layout', async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 500 })));
      await expect(useDisplayStore.getState().loadScreens()).rejects.toThrow('HTTP 500');
      expect(useDisplayStore.getState().layout).toEqual([]);
    });
  });
});
