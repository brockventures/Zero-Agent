import { describe, expect, it, vi } from 'vitest';
import { createEventBatcher, type BufferedEvent } from './batch';

const widget = (id: string, value: number): BufferedEvent => ({
  type: 'widget.update',
  data: { widget_id: id, timestamp: 't', state: 'healthy', data: { value } },
});

describe('createEventBatcher', () => {
  it('applies events pushed in one tick as a single batch', () => {
    const apply = vi.fn();
    const timer: { run?: () => void } = {};
    const batcher = createEventBatcher(apply, (flush) => {
      timer.run = flush;
    });

    batcher.push(widget('a', 1));
    batcher.push(widget('b', 2));
    batcher.push(widget('c', 3));
    expect(apply).not.toHaveBeenCalled();

    timer.run?.();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply.mock.calls[0][0]).toHaveLength(3);
  });

  it('flushes immediately on theme.update, the last event of a hydration batch', () => {
    const apply = vi.fn();
    const batcher = createEventBatcher(apply, () => {});

    batcher.push(widget('a', 1));
    batcher.push({ type: 'theme.update', data: { theme: 'day' } });

    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply.mock.calls[0][0].map((e: BufferedEvent) => e.type)).toEqual(['widget.update', 'theme.update']);
  });

  it('does nothing when the scheduled flush finds an empty buffer', () => {
    const apply = vi.fn();
    const timer: { run?: () => void } = {};
    const batcher = createEventBatcher(apply, (flush) => {
      timer.run = flush;
    });

    batcher.push({ type: 'theme.update', data: { theme: 'day' } });
    timer.run?.();
    expect(apply).toHaveBeenCalledTimes(1);
  });
});
