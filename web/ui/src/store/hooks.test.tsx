import { act, render } from '@testing-library/react';
import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useDisplayStore } from './index';
import { useWidget } from './hooks';

const initial = useDisplayStore.getState();

const renders: Record<string, number> = {};

function WidgetProbe({ id }: { id: string }) {
  const widget = useWidget(id);
  useLayoutEffect(() => {
    renders[id] = (renders[id] ?? 0) + 1;
  });
  return <span data-testid={id}>{widget.state || 'empty'}</span>;
}

describe('useWidget subscriptions', () => {
  beforeEach(() => {
    useDisplayStore.setState(initial, true);
    for (const key of Object.keys(renders)) delete renders[key];
  });

  afterEach(() => {
    useDisplayStore.setState(initial, true);
  });

  it('re-renders only the subscriber of the widget that changed', () => {
    const { getByTestId } = render(
      <>
        <WidgetProbe id="a" />
        <WidgetProbe id="b" />
      </>,
    );
    expect(renders).toEqual({ a: 1, b: 1 });

    act(() => {
      useDisplayStore.getState().applyEvents([
        { type: 'widget.update', data: { widget_id: 'a', timestamp: 't1', state: 'healthy', data: 1 } },
      ]);
    });

    expect(renders).toEqual({ a: 2, b: 1 });
    expect(getByTestId('a').textContent).toBe('healthy');
    expect(getByTestId('b').textContent).toBe('empty');
  });

  it('does not re-render on a deep-equal widget.update', () => {
    render(<WidgetProbe id="a" />);
    act(() => {
      useDisplayStore.getState().applyEvents([
        { type: 'widget.update', data: { widget_id: 'a', timestamp: 't1', state: 'healthy', data: { x: [1] } } },
      ]);
    });
    const before = renders.a;

    act(() => {
      useDisplayStore.getState().applyEvents([
        { type: 'widget.update', data: { widget_id: 'a', timestamp: 't2', state: 'healthy', data: { x: [1] } } },
      ]);
    });

    expect(renders.a).toBe(before);
  });

  it('re-renders a batch once per subscriber', () => {
    function Both() {
      const a = useWidget('a');
      const b = useWidget('b');
      useLayoutEffect(() => {
        renders.both = (renders.both ?? 0) + 1;
      });
      return <span>{`${a.state}${b.state}`}</span>;
    }
    render(<Both />);
    act(() => {
      useDisplayStore.getState().applyEvents([
        { type: 'widget.update', data: { widget_id: 'a', timestamp: 't', state: 'x', data: 1 } },
        { type: 'widget.update', data: { widget_id: 'b', timestamp: 't', state: 'y', data: 2 } },
      ]);
    });
    expect(renders.both).toBe(2);
  });
});
