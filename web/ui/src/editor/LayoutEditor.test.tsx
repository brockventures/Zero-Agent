import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LayoutEditorResponse } from '../sse/types';
import { useDisplayStore } from '../store';
import { LayoutEditor } from './LayoutEditor';

const initialStore = useDisplayStore.getState();

const mockEditorData: LayoutEditorResponse = {
  allow_layout_edit: true,
  has_override: false,
  grid: [6, 2],
  total_screens: 2,
  widgets: [
    {
      widget_id: 'clock-1',
      type: 'clock',
      screen: 0,
      origin: [0, 0],
      dimensions: [2, 1],
      pinned: false,
      supported_dimensions: [
        [2, 1],
        [2, 2],
      ],
    },
    {
      widget_id: 'weather-1',
      type: 'weather-forecast',
      screen: 0,
      origin: [2, 0],
      dimensions: [2, 1],
      pinned: false,
      supported_dimensions: [
        [2, 1],
        [4, 2],
      ],
    },
    {
      widget_id: 'pinned-1',
      type: 'calendar-feed',
      screen: 0,
      origin: [4, 0],
      dimensions: [2, 1],
      pinned: true,
      supported_dimensions: [[2, 1]],
    },
    {
      widget_id: 'tasks-1',
      type: 'tasks',
      screen: 1,
      origin: [0, 0],
      dimensions: [3, 2],
      pinned: false,
      supported_dimensions: [[3, 2]],
    },
  ],
};

function stubEditorFetch(
  overrides?: Partial<LayoutEditorResponse>,
  status = 200,
  errorMessage = '',
) {
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url === 'api/screens') {
      return new Response(JSON.stringify({ screens: [] }), { status: 200 });
    }
    if (status !== 200) {
      return new Response(errorMessage || 'Error', { status });
    }
    if (init?.method === 'PUT') {
      return new Response(
        JSON.stringify({ ...mockEditorData, ...overrides, has_override: true }),
        { status: 200 },
      );
    }
    if (init?.method === 'DELETE') {
      return new Response(
        JSON.stringify({ ...mockEditorData, ...overrides, has_override: false }),
        { status: 200 },
      );
    }
    return new Response(JSON.stringify({ ...mockEditorData, ...overrides }), { status: 200 });
  });
  return fetchMock;
}

describe('LayoutEditor component', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    (document as unknown as { elementFromPoint?: unknown }).elementFromPoint = () => null;
  });

  afterEach(() => {
    cleanup();
    delete (document as unknown as { elementFromPoint?: unknown }).elementFromPoint;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function settle() {
    await act(async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    });
  }

  it('renders nothing when layoutEditorOpen is false', () => {
    const { container } = render(<LayoutEditor />);
    expect(container.firstChild).toBeNull();
  });

  it('renders loading indicator while fetch is in flight then renders grid and cards', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    expect(container.querySelector('.layout-editor-loading')).not.toBeNull();

    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector('.layout-editor-loading')).toBeNull();
    expect(container.querySelector('#layout-editor-overlay')).not.toBeNull();
    expect(container.querySelector('.layout-editor-grid')).not.toBeNull();

    // 12 background drop slots
    expect(container.querySelectorAll('.layout-editor-cell')).toHaveLength(12);

    // Toolbar reuses .header-zone and cards reuse .widget-card
    expect(container.querySelector('.layout-editor-toolbar.header-zone')).not.toBeNull();
    expect(container.querySelectorAll('.layout-editor-card.widget-card')).toHaveLength(3);

    // Status shows occupied cell count: clock-1 (2) + weather-1 (2) + pinned-1 (2) = 6/12 cells
    const status = container.querySelector('.layout-editor-status');
    expect(status?.textContent).toContain('6/12 cells • Auto-spacer padded');
  });

  it('displays error message when layout editor fetch fails', async () => {
    const fetchMock = stubEditorFetch({}, 500, 'Server unavailable');
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector('.layout-editor-error')?.textContent).toContain(
      'fetch layout editor: HTTP 500',
    );
  });

  it('closes editor on Cancel button click', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });

    const cancelBtn = container.querySelector('.layout-editor-btn-cancel') as HTMLButtonElement;
    expect(cancelBtn).not.toBeNull();

    act(() => {
      fireEvent.click(cancelBtn);
    });

    expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
  });

  it('calls saveLayoutEditor on Save Layout click and reloads screens', async () => {
    const fetchMock = stubEditorFetch();
    vi.stubGlobal('fetch', fetchMock);
    useDisplayStore.setState({ layoutEditorOpen: true });
    const loadScreensSpy = vi.spyOn(useDisplayStore.getState(), 'loadScreens');

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const saveBtn = container.querySelector('.layout-editor-btn-save') as HTMLButtonElement;
    act(() => {
      fireEvent.click(saveBtn);
    });
    await settle();

    expect(fetchMock).toHaveBeenCalledWith(
      'api/layout/editor',
      expect.objectContaining({ method: 'PUT' }),
    );
    expect(loadScreensSpy).toHaveBeenCalled();
    expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
  });

  it('handles saveLayoutEditor error gracefully', async () => {
    let callCount = 0;
    const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
      if (init?.method === 'PUT') {
        return new Response('Conflict', { status: 400 });
      }
      callCount++;
      return new Response(JSON.stringify(mockEditorData), { status: 200 });
    });
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(callCount).toBe(1);

    const saveBtn = container.querySelector('.layout-editor-btn-save') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(saveBtn);
      await Promise.resolve();
    });

    expect(container.querySelector('.layout-editor-error')?.textContent).toContain(
      'save layout editor: HTTP 400',
    );
    expect(useDisplayStore.getState().layoutEditorOpen).toBe(true);
  });

  it('calls resetLayoutEditor on Reset to Default click', async () => {
    const fetchMock = stubEditorFetch();
    vi.stubGlobal('fetch', fetchMock);
    useDisplayStore.setState({ layoutEditorOpen: true });
    const loadScreensSpy = vi.spyOn(useDisplayStore.getState(), 'loadScreens');

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const resetBtn = container.querySelector('.layout-editor-btn-reset') as HTMLButtonElement;
    act(() => {
      fireEvent.click(resetBtn);
    });
    await settle();

    expect(fetchMock).toHaveBeenCalledWith(
      'api/layout/editor',
      expect.objectContaining({ method: 'DELETE' }),
    );
    expect(loadScreensSpy).toHaveBeenCalled();
    expect(useDisplayStore.getState().layoutEditorOpen).toBe(false);
  });

  it('handles resetLayoutEditor error gracefully', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (_url, init) => {
      if (init?.method === 'DELETE') {
        return new Response('Forbidden', { status: 403 });
      }
      return new Response(JSON.stringify(mockEditorData), { status: 200 });
    });
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });

    const resetBtn = container.querySelector('.layout-editor-btn-reset') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(resetBtn);
      await Promise.resolve();
    });

    expect(container.querySelector('.layout-editor-error')?.textContent).toContain(
      'reset layout editor: HTTP 403',
    );
    expect(useDisplayStore.getState().layoutEditorOpen).toBe(true);
  });

  it('switches active screen when dock pill is clicked', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });

    const pills = container.querySelectorAll('.layout-editor-screen-pill');
    expect(pills.length).toBeGreaterThanOrEqual(3); // Screen 1, Screen 2, + New Screen

    // Switch to Screen 2
    act(() => {
      fireEvent.click(pills[1]);
    });

    // On Screen 2 (screen 1): tasks-1 (3x2) and pinned-1 (2x1) = 8 cells
    const status = container.querySelector('.layout-editor-status');
    expect(status?.textContent).toContain('8/12 cells • Auto-spacer padded');

    // Cards visible on Screen 2: tasks-1 and pinned-1
    expect(container.querySelectorAll('.layout-editor-card')).toHaveLength(2);

    // Switch to + New Screen (index 2)
    const newScreenPill = container.querySelector('.new-screen-pill') as HTMLElement;
    act(() => {
      fireEvent.click(newScreenPill);
    });
    // On new screen (screen 2), only pinned-1 is visible
    expect(container.querySelectorAll('.layout-editor-card')).toHaveLength(1);
  });

  it('toggles pin on widget when pin button is clicked', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });

    const clockCard = container.querySelector('[data-widget-id="clock-1"]') as HTMLElement;
    const pinBtn = clockCard.querySelector('.layout-editor-pin-btn') as HTMLButtonElement;
    expect(pinBtn.getAttribute('aria-label')).toBe('Pin Widget');
    expect(pinBtn.textContent?.trim()).toBe('Pin');
    expect(pinBtn.querySelector('svg')).not.toBeNull();

    // Unpinned -> Pinned (at [0, 0] it collides with tasks-1 on screen 1, so toggle fails!)
    act(() => {
      fireEvent.click(pinBtn);
    });
    expect(pinBtn.getAttribute('aria-label')).toBe('Pin Widget');
    expect(pinBtn.textContent?.trim()).toBe('Pin'); // Collided, stays unpinned
    expect(pinBtn.querySelector('svg')).not.toBeNull();

    // Pinned widget: pinned-1
    const pinnedCard = container.querySelector('[data-widget-id="pinned-1"]') as HTMLElement;
    const unpinBtn = pinnedCard.querySelector('.layout-editor-pin-btn') as HTMLButtonElement;
    expect(unpinBtn.getAttribute('aria-label')).toBe('Unpin Widget');
    expect(unpinBtn.textContent?.trim()).toBe('Pinned');
    expect(unpinBtn.querySelector('svg')).not.toBeNull();

    act(() => {
      fireEvent.click(unpinBtn);
    });
    expect(unpinBtn.getAttribute('aria-label')).toBe('Pin Widget');
    expect(unpinBtn.textContent?.trim()).toBe('Pin');
    expect(unpinBtn.querySelector('svg')).not.toBeNull();
  });

  it('resizes widget when size chip is clicked', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const clockCard = container.querySelector('[data-widget-id="clock-1"]') as HTMLElement;
    expect(clockCard.querySelector('.layout-editor-card-dim')?.textContent).toBe('2×1');

    const clockChips = clockCard.querySelectorAll('.size-chip');
    expect(clockChips).toHaveLength(2);

    // Click 2×2 chip on clock-1 (expands to 2x2 on row 0 and 1)
    act(() => {
      fireEvent.click(clockChips[1]);
    });
    expect(clockCard.querySelector('.layout-editor-card-dim')?.textContent).toBe('2×2');

    // Click 4×2 chip on weather-1 (blocked due to collision with pinned-1)
    const weatherCard = container.querySelector('[data-widget-id="weather-1"]') as HTMLElement;
    const weatherChips = weatherCard.querySelectorAll('.size-chip');
    act(() => {
      fireEvent.click(weatherChips[1]);
    });
    expect(weatherCard.querySelector('.layout-editor-card-dim')?.textContent).toBe('2×1');
  });

  it('supports drag and drop on grid to move widget', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const grid = container.querySelector('.layout-editor-grid') as HTMLElement;
    vi.spyOn(grid, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      width: 600,
      height: 200,
      right: 600,
      bottom: 200,
      x: 0,
      y: 0,
      toJSON: () => {},
    });

    const clockCard = container.querySelector('[data-widget-id="clock-1"]') as HTMLElement;
    clockCard.setPointerCapture = vi.fn();
    clockCard.releasePointerCapture = vi.fn();

    // PointerDown on clock-1
    fireEvent.pointerDown(clockCard, { pointerId: 1, clientX: 50, clientY: 50, button: 0 });
    expect(container.querySelector('.layout-editor-ghost')).not.toBeNull();

    // PointerMove to col 0, row 1 (x: 50, y: 150)
    fireEvent.pointerMove(clockCard, { pointerId: 1, clientX: 50, clientY: 150 });

    // PointerUp to drop
    fireEvent.pointerUp(clockCard, { pointerId: 1, clientX: 50, clientY: 150 });
    expect(container.querySelector('.layout-editor-ghost')).toBeNull();

    // clock-1 moved to row 1
    expect(clockCard.style.gridRow).toBe('2 / span 1');
  });

  it('supports dragging 4x2 family-calendar grabbed near its center to the right side and swapping with two 2x1 widgets', async () => {
    const fetchMock = stubEditorFetch({
      widgets: [
        {
          widget_id: 'family-calendar',
          type: 'calendar-monthly',
          screen: 0,
          origin: [0, 0],
          dimensions: [4, 2],
          pinned: false,
          supported_dimensions: [[4, 2]],
        },
        {
          widget_id: 'home-weather',
          type: 'weather-forecast',
          screen: 0,
          origin: [4, 0],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
        {
          widget_id: 'family-tasks',
          type: 'tasks',
          screen: 0,
          origin: [4, 1],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
      ],
    });
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const grid = container.querySelector('.layout-editor-grid') as HTMLElement;
    vi.spyOn(grid, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      width: 600,
      height: 200,
      right: 600,
      bottom: 200,
      x: 0,
      y: 0,
      toJSON: () => {},
    });

    const calendarCard = container.querySelector('[data-widget-id="family-calendar"]') as HTMLElement;
    calendarCard.setPointerCapture = vi.fn();
    calendarCard.releasePointerCapture = vi.fn();

    // Grab family-calendar at col 2 (clientX: 250) and drag right by 200px (clientX: 450)
    fireEvent.pointerDown(calendarCard, { pointerId: 1, clientX: 250, clientY: 50, button: 0 });
    fireEvent.pointerMove(calendarCard, { pointerId: 1, clientX: 450, clientY: 50 });
    fireEvent.pointerUp(calendarCard, { pointerId: 1, clientX: 450, clientY: 50 });

    const weatherCard = container.querySelector('[data-widget-id="home-weather"]') as HTMLElement;
    const tasksCard = container.querySelector('[data-widget-id="family-tasks"]') as HTMLElement;

    expect(calendarCard.style.gridColumn).toBe('3 / span 4');
    expect(weatherCard.style.gridColumn).toBe('1 / span 2');
    expect(weatherCard.style.gridRow).toBe('1 / span 1');
    expect(tasksCard.style.gridColumn).toBe('1 / span 2');
    expect(tasksCard.style.gridRow).toBe('2 / span 1');
  });

  it('supports dragging widget and dropping onto screen dock pill', async () => {
    const fetchMock = stubEditorFetch();
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const clockCard = container.querySelector('[data-widget-id="clock-1"]') as HTMLElement;
    clockCard.setPointerCapture = vi.fn();
    clockCard.releasePointerCapture = vi.fn();

    const pills = container.querySelectorAll('.layout-editor-screen-pill');
    const screen2Pill = pills[1] as HTMLElement;

    (document as unknown as { elementFromPoint?: unknown }).elementFromPoint = () => screen2Pill;

    fireEvent.pointerDown(clockCard, { pointerId: 1, clientX: 50, clientY: 50, button: 0 });
    fireEvent.pointerMove(clockCard, { pointerId: 1, clientX: 300, clientY: 500 });
    fireEvent.pointerUp(clockCard, { pointerId: 1, clientX: 300, clientY: 500 });

    // clock-1 moved to Screen 2 and activeScreen switched to Screen 2 (screen 1)
    const cardsOnScreen2 = container.querySelectorAll('.layout-editor-card');
    const ids = Array.from(cardsOnScreen2).map((c) => c.getAttribute('data-widget-id'));
    expect(ids).toContain('clock-1');
  });

  it('syncs activeScreen to compacted screen index when dragging last unpinned widget to a higher screen', async () => {
    const fetchMock = stubEditorFetch({
      widgets: [
        {
          widget_id: 'sole-0',
          type: 'clock',
          screen: 0,
          origin: [0, 0],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
        {
          widget_id: 'tasks-1',
          type: 'tasks',
          screen: 1,
          origin: [2, 0],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
      ],
    });
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    const soleCard = container.querySelector('[data-widget-id="sole-0"]') as HTMLElement;
    const newScreenPill = container.querySelector('.new-screen-pill') as HTMLElement;
    (document as unknown as { elementFromPoint?: unknown }).elementFromPoint = () => newScreenPill;

    fireEvent.pointerDown(soleCard, { pointerId: 1, clientX: 50, clientY: 50, button: 0 });
    fireEvent.pointerUp(soleCard, { pointerId: 1, clientX: 300, clientY: 500 });

    // Screen 0 emptied, so tasks-1 compacted to screen 0 and sole-0 compacted to screen 1.
    // activeScreen must be 1 (where sole-0 lives), not uncompacted 2.
    const cards = container.querySelectorAll('.layout-editor-card');
    const ids = Array.from(cards).map((c) => c.getAttribute('data-widget-id'));
    expect(ids).toContain('sole-0');
  });

  it('clamps activeScreen when pinning the last unpinned widget on the highest screen', async () => {
    const fetchMock = stubEditorFetch({
      widgets: [
        {
          widget_id: 'clock-1',
          type: 'clock',
          screen: 0,
          origin: [0, 0],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
        {
          widget_id: 'tasks-1',
          type: 'tasks',
          screen: 1,
          origin: [2, 0],
          dimensions: [2, 1],
          pinned: false,
          supported_dimensions: [[2, 1]],
        },
        {
          widget_id: 'pinned-1',
          type: 'calendar-feed',
          screen: 0,
          origin: [4, 0],
          dimensions: [2, 1],
          pinned: true,
          supported_dimensions: [[2, 1]],
        },
      ],
    });
    useDisplayStore.setState({ layoutEditorOpen: true });

    const { container } = render(<LayoutEditor fetchImpl={fetchMock} />);
    await settle();

    // Switch to Screen 2 (screen index 1)
    const pills = container.querySelectorAll('.layout-editor-screen-pill');
    act(() => {
      fireEvent.click(pills[1]);
    });

    // Unpinning pinned-1 while on Screen 2 keeps it on Screen 2
    const pinnedCard = container.querySelector('[data-widget-id="pinned-1"]') as HTMLElement;
    const unpinBtn = pinnedCard.querySelector('.layout-editor-pin-btn') as HTMLButtonElement;
    act(() => {
      fireEvent.click(unpinBtn);
    });
    expect(
      Array.from(container.querySelectorAll('.layout-editor-card')).map((c) =>
        c.getAttribute('data-widget-id'),
      ),
    ).toContain('pinned-1');

    // Pinning pinned-1 back and then pinning tasks-1 collapses Screen 2 into 1 total screen and clamps activeScreen to 0
    act(() => {
      fireEvent.click(unpinBtn);
    });
    const tasksCard = container.querySelector('[data-widget-id="tasks-1"]') as HTMLElement;
    const tasksPinBtn = tasksCard.querySelector('.layout-editor-pin-btn') as HTMLButtonElement;
    act(() => {
      fireEvent.click(tasksPinBtn);
    });
    expect(
      Array.from(container.querySelectorAll('.layout-editor-card')).map((c) =>
        c.getAttribute('data-widget-id'),
      ),
    ).toContain('clock-1');
  });
});
