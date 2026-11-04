import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useDisplayStore } from '../../store';
import { WidgetHost } from '../WidgetHost';
import { SpacerWidget } from './SpacerWidget';

const initialStore = useDisplayStore.getState();

describe('SpacerWidget', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
  });

  afterEach(() => {
    cleanup();
  });

  it('renders .widget-spacer with data-widget-type and aria-hidden without data-widget-id', () => {
    const { container, rerender } = render(<SpacerWidget id="s1" type="spacer" />);
    const el = container.firstElementChild as HTMLElement;

    expect(el).not.toBeNull();
    expect(el.className).toBe('widget-spacer');
    expect(el.getAttribute('data-widget-type')).toBe('spacer');
    expect(el.getAttribute('aria-hidden')).toBe('true');
    expect(el.getAttribute('data-widget-id')).toBeNull();

    rerender(<SpacerWidget id="s2" type="spacer" />);
    expect(container.firstElementChild?.getAttribute('data-widget-type')).toBe('spacer');
  });

  it('integrates with WidgetHost across [1, 1] dimensions without duplicate data-widget-id', () => {
    useDisplayStore.setState({
      widgets: {
        'spacer-1x1': {
          meta: {
            type: 'spacer',
            origin: [0, 0],
            dimensions: [1, 1],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<WidgetHost id="spacer-1x1" origin={[0, 0]} dimensions={[1, 1]} />);
    const card = container.querySelector('.widget-card') as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.getAttribute('data-widget-id')).toBe('spacer-1x1');
    expect(card.getAttribute('data-widget-type')).toBe('spacer');
    expect(card.style.gridColumn).toBe('1 / span 1');
    expect(card.style.gridRow).toBe('1 / span 1');

    // Inner spacer must NOT duplicate data-widget-id or wrap in .widget-card
    const inner = card.querySelector('.widget-spacer') as HTMLElement;
    expect(inner).not.toBeNull();
    expect(inner.getAttribute('data-widget-id')).toBeNull();

    const ids = container.querySelectorAll('[data-widget-id]');
    expect(ids.length).toBe(1);
  });

  it('integrates with WidgetHost across [6, 4] dimensions', () => {
    useDisplayStore.setState({
      widgets: {
        'spacer-6x4': {
          meta: {
            type: 'spacer',
            origin: [0, 0],
            dimensions: [6, 4],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<WidgetHost id="spacer-6x4" origin={[0, 0]} dimensions={[6, 4]} />);
    const card = container.querySelector('.widget-card') as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.style.gridColumn).toBe('1 / span 6');
    expect(card.style.gridRow).toBe('1 / span 4');

    const ids = container.querySelectorAll('[data-widget-id]');
    expect(ids.length).toBe(1);
  });
});
