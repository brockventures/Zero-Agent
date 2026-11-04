import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ScreenIndicator } from './ScreenIndicator';

describe('ScreenIndicator', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders dots according to total and marks active screen', () => {
    const onSelect = vi.fn();
    const onSwipe = vi.fn();
    const { container } = render(
      <ScreenIndicator total={3} active={1} hidden={false} onSelect={onSelect} onSwipe={onSwipe} />,
    );

    const indicator = container.querySelector('#screen-indicator');
    expect(indicator).not.toBeNull();
    expect(indicator?.getAttribute('style')).toContain('display: inline-flex');

    const dots = container.querySelectorAll('.screen-indicator-dot');
    expect(dots).toHaveLength(3);
    expect(dots[1]?.classList.contains('active')).toBe(true);
    expect(dots[0]?.classList.contains('active')).toBe(false);
  });

  it('calls onSelect when a dot is clicked', () => {
    const onSelect = vi.fn();
    const onSwipe = vi.fn();
    const { container } = render(
      <ScreenIndicator total={3} active={0} hidden={false} onSelect={onSelect} onSwipe={onSwipe} />,
    );

    const dots = container.querySelectorAll('.screen-indicator-dot');
    fireEvent.click(dots[2]!);
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it('hides indicator when hidden prop is true or total is 1', () => {
    const onSelect = vi.fn();
    const onSwipe = vi.fn();
    const { container, rerender } = render(
      <ScreenIndicator total={3} active={0} hidden={true} onSelect={onSelect} onSwipe={onSwipe} />,
    );

    const indicator = container.querySelector('#screen-indicator');
    expect(indicator?.classList.contains('paused')).toBe(true);
    expect(indicator?.getAttribute('style')).toContain('display: none');

    rerender(<ScreenIndicator total={1} active={0} hidden={false} onSelect={onSelect} onSwipe={onSwipe} />);
    expect(indicator?.getAttribute('style')).toContain('display: none');
  });

  it('dispatches onSwipe on swipe gesture across pill', () => {
    const onSelect = vi.fn();
    const onSwipe = vi.fn();
    const { container } = render(
      <ScreenIndicator total={3} active={0} hidden={false} onSelect={onSelect} onSwipe={onSwipe} />,
    );

    const indicator = container.querySelector<HTMLElement>('#screen-indicator')!;
    act(() => {
      indicator.dispatchEvent(
        Object.assign(new Event('pointerdown', { bubbles: true, cancelable: true }), {
          clientX: 200,
          clientY: 50,
          pointerId: 1,
        }),
      );
      indicator.dispatchEvent(
        Object.assign(new Event('pointerup', { bubbles: true, cancelable: true }), {
          clientX: 140,
          clientY: 50,
          pointerId: 1,
        }),
      );
    });

    expect(onSwipe).toHaveBeenCalledWith('next');
  });

  describe('screen-indicator-pin', () => {
    it('does not render pin button when onTogglePin is omitted', () => {
      const { container } = render(
        <ScreenIndicator total={3} active={0} hidden={false} onSelect={vi.fn()} onSwipe={vi.fn()} />,
      );
      expect(container.querySelector('.screen-indicator-pin')).toBeNull();
    });

    it('renders pin button with unpinned state attributes and icon', () => {
      const onTogglePin = vi.fn();
      const { container } = render(
        <ScreenIndicator
          total={3}
          active={0}
          hidden={false}
          pinned={false}
          onSelect={vi.fn()}
          onSwipe={vi.fn()}
          onTogglePin={onTogglePin}
        />,
      );

      const pinBtn = container.querySelector<HTMLButtonElement>('.screen-indicator-pin');
      expect(pinBtn).not.toBeNull();
      expect(pinBtn?.classList.contains('active')).toBe(false);
      expect(pinBtn?.getAttribute('aria-label')).toBe('Pin current screen');
      expect(pinBtn?.getAttribute('aria-pressed')).toBe('false');
      expect(pinBtn?.getAttribute('data-pinned')).toBe('false');
      expect(pinBtn?.querySelector('svg')).not.toBeNull();
    });

    it('renders pin button with pinned active state attributes and icon', () => {
      const onTogglePin = vi.fn();
      const { container } = render(
        <ScreenIndicator
          total={3}
          active={0}
          hidden={false}
          pinned={true}
          onSelect={vi.fn()}
          onSwipe={vi.fn()}
          onTogglePin={onTogglePin}
        />,
      );

      const pinBtn = container.querySelector<HTMLButtonElement>('.screen-indicator-pin');
      expect(pinBtn).not.toBeNull();
      expect(pinBtn?.classList.contains('active')).toBe(true);
      expect(pinBtn?.getAttribute('aria-label')).toBe('Unpin screen rotation');
      expect(pinBtn?.getAttribute('aria-pressed')).toBe('true');
      expect(pinBtn?.getAttribute('data-pinned')).toBe('true');
      expect(pinBtn?.querySelector('svg')).not.toBeNull();
    });

    it('calls onTogglePin on click', () => {
      const onTogglePin = vi.fn();
      const { container } = render(
        <ScreenIndicator
          total={3}
          active={0}
          hidden={false}
          pinned={false}
          onSelect={vi.fn()}
          onSwipe={vi.fn()}
          onTogglePin={onTogglePin}
        />,
      );

      const pinBtn = container.querySelector<HTMLButtonElement>('.screen-indicator-pin')!;
      fireEvent.click(pinBtn);
      expect(onTogglePin).toHaveBeenCalledTimes(1);
    });

    it('does not capture pointer on pin button pointerdown', () => {
      const onTogglePin = vi.fn();
      const { container } = render(
        <ScreenIndicator
          total={3}
          active={0}
          hidden={false}
          pinned={false}
          onSelect={vi.fn()}
          onSwipe={vi.fn()}
          onTogglePin={onTogglePin}
        />,
      );

      const indicator = container.querySelector<HTMLElement>('#screen-indicator')!;
      const setPointerCapture = vi.fn();
      Object.assign(indicator, { setPointerCapture });

      const pinBtn = container.querySelector<HTMLButtonElement>('.screen-indicator-pin')!;
      act(() => {
        pinBtn.dispatchEvent(
          Object.assign(new Event('pointerdown', { bubbles: true, cancelable: true }), {
            clientX: 100,
            clientY: 50,
            pointerId: 1,
          }),
        );
      });

      expect(setPointerCapture).not.toHaveBeenCalled();
    });
  });
});
