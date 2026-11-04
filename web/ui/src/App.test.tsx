import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from './store';
import { App } from './App';

const onChildRender = vi.fn();

function PlainChild({ items, onClick }: { items: string[]; onClick: () => void }) {
  onChildRender(items);
  return (
    <button type="button" onClick={onClick}>
      {items.join(',')}
    </button>
  );
}

function ParentWithUnrelatedState() {
  const [counter, setCounter] = useState(0);
  const items = ['a', 'b'];

  return (
    <div>
      <button type="button" data-testid="inc-btn" onClick={() => setCounter((c) => c + 1)}>
        {counter}
      </button>
      <PlainChild items={items} onClick={() => {}} />
    </div>
  );
}

describe('App', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders the display shell with Header and the carousel grid canvas', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              current_screen: 0,
              interval_seconds: 30,
              screens: [{ index: 0, widgets: [] }],
            }),
            { status: 200 }
          )
      )
    );
    const { container } = render(<App />);
    await act(async () => {});

    const shell = container.querySelector('#mirrormere-app');
    expect(shell).not.toBeNull();
    expect(shell?.querySelector('header.header-zone')).not.toBeNull();
    expect(shell?.querySelectorAll('#grid-canvas')).toHaveLength(1);
    expect(shell?.querySelector('.carousel-track')).not.toBeNull();
  });

  it('preserves child render stability under React Compiler when parent updates unrelated state', () => {
    onChildRender.mockClear();
    const { getByTestId } = render(<ParentWithUnrelatedState />);
    expect(onChildRender).toHaveBeenCalledTimes(1);

    act(() => {
      fireEvent.click(getByTestId('inc-btn'));
    });

    expect(getByTestId('inc-btn').textContent).toBe('1');
    expect(onChildRender).toHaveBeenCalledTimes(1);
  });

  it('renders layout editor overlay when layoutEditorOpen is true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              allow_layout_edit: true,
              has_override: false,
              grid: [6, 2],
              total_screens: 1,
              widgets: [],
              current_screen: 0,
              interval_seconds: 30,
              screens: [{ index: 0, widgets: [] }],
            }),
            { status: 200 }
          )
      )
    );

    const { container } = render(<App />);
    expect(container.querySelector('#layout-editor-overlay')).toBeNull();

    act(() => {
      useDisplayStore.getState().setLayoutEditorOpen(true);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.querySelector('#layout-editor-overlay')).not.toBeNull();
  });
});
