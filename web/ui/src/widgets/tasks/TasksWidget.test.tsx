import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { Profiler } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../../store';
import { TASK_UNDO_GRACE_MS, TasksWidget } from './TasksWidget';

const initialStore = useDisplayStore.getState();

describe('TasksWidget', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useDisplayStore.setState(initialStore, true);
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('renders loading state when data is unhydrated ({}) and omits header when unnamed', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-1': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-1" type="tasks" />);
    const root = container.querySelector('.widget-tasks');
    expect(root).not.toBeNull();
    expect(root?.querySelector('.tasks-loading')?.textContent).toContain('Loading checklist...');
    expect(root?.querySelector('.tasks-header')).toBeNull();
  });

  it('renders loading state with custom title header when config.list_name or todoist.project_name is present', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-listname': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { list_name: 'Chores Manifest' },
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
        'tasks-todoist': {
          meta: {
            type: 'tasks',
            origin: [3, 0],
            dimensions: [3, 2],
            config: { todoist: { project_name: 'Work Tasks' } },
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c1 } = render(<TasksWidget id="tasks-listname" type="tasks" />);
    expect(c1.querySelector('.tasks-title')?.textContent).toBe('Chores Manifest');

    const { container: c2 } = render(<TasksWidget id="tasks-todoist" type="tasks" />);
    expect(c2.querySelector('.tasks-title')?.textContent).toBe('Work Tasks');
  });

  it('renders loading state with custom title header when config.title or config.name is present', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-named': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { title: 'Groceries' },
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-named" type="tasks" />);
    const root = container.querySelector('.widget-tasks');
    expect(root?.querySelector('.tasks-header')).not.toBeNull();
    expect(root?.querySelector('.tasks-title')?.textContent).toBe('Groceries');
    expect(root?.querySelector('.tasks-loading')?.textContent).toContain('Loading checklist...');
  });

  it('renders empty state when hydrated with empty items array ([])', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-empty': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            list: { name: 'Weekend Chores' },
            items: [],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-empty" type="tasks" />);
    const root = container.querySelector('.widget-tasks');
    expect(root?.querySelector('.tasks-header')).not.toBeNull();
    expect(root?.querySelector('.tasks-title')?.textContent).toBe('Weekend Chores');
    expect(root?.querySelector('.tasks-summary')?.textContent).toContain('0 left');
    expect(root?.querySelector('.tasks-empty')).not.toBeNull();
    expect(root?.querySelector('.empty-title')?.textContent).toBe('All tasks completed!');
    expect(root?.querySelector('.empty-subtitle')?.textContent).toBe('Time to put your feet up');
  });

  it('renders populated task items with assignees, sections, due dates, and summary', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-pop': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            items: [
              {
                id: 'item-1',
                list_id: 'list-123',
                title: 'Clean kitchen counter',
                done: false,
                section: 'Kitchen',
                assignee: 'Alex',
                due_date: '2026-11-01',
              },
              {
                id: 'item-2',
                list_id: 'list-123',
                title: 'Water plants',
                done: true,
              },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-pop" type="tasks" />);
    const root = container.querySelector('.widget-tasks');
    expect(root?.querySelector('.tasks-title')?.textContent).toBe('Tasks');
    expect(root?.querySelector('.tasks-summary')?.textContent).toContain('1 left');

    const items = root?.querySelectorAll('.task-item');
    expect(items?.length).toBe(2);

    // Item 1: active
    const item1 = items?.[0] as HTMLElement;
    expect(item1.className).toContain('task-item-active');
    expect(item1.dataset.itemId).toBe('item-1');
    expect(item1.dataset.listId).toBe('list-123');
    expect(item1.querySelector('.task-title')?.textContent).toBe('Clean kitchen counter');
    expect(item1.querySelector('.task-tag-section')?.textContent).toBe('#Kitchen');
    expect(item1.querySelector('.task-tag-assignee')?.textContent).toBe('@Alex');
    expect(item1.querySelector('.task-tag-due')?.textContent).toBe('2026-11-01');
    expect(item1.querySelector('.task-checkbox-ring')).not.toBeNull();

    // Item 2: done
    const item2 = items?.[1] as HTMLElement;
    expect(item2.className).toContain('task-item-done');
    expect(item2.querySelector('.task-checkbox-done')).not.toBeNull();
  });

  it('handles optimistic task completion, clears data-completing on success, and ignores already completed tasks', async () => {
    let resolveFetch!: (res: Response) => void;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.fn().mockImplementation(() => fetchPromise);
    vi.stubGlobal('fetch', fetchMock);

    useDisplayStore.setState({
      widgets: {
        'tasks-opt': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            list: { id: 'list-99' },
            items: [
              { id: 't-1', title: 'Sweep floor', done: false },
              { id: 't-2', title: 'Take out trash', done: true },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-opt" type="tasks" />);
    const item1 = container.querySelector('[data-item-id="t-1"]') as HTMLElement;
    const item2 = container.querySelector('[data-item-id="t-2"]') as HTMLElement;
    const summary = container.querySelector('.tasks-summary');

    expect(summary?.textContent).toContain('1 left');

    // Clicking already-done item does nothing
    fireEvent.click(item2);
    expect(fetchMock).not.toHaveBeenCalled();

    // Click active item: immediately optimistic
    fireEvent.click(item1);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(item1.dataset.completing).toBe('true');
    expect(item1.dataset.pendingUndo).toBe('true');
    expect(item1.className).toContain('task-item-done');
    expect(item1.className).toContain('pending-undo');
    expect(summary?.textContent).toContain('All done');

    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('api/lists/list-99/items/t-1/complete', {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });

    expect(item1.dataset.completing).toBe('true');
    expect(item1.dataset.pendingUndo).toBeUndefined();
    expect(item1.className).toContain('task-item-done');
    expect(summary?.textContent).toContain('All done');

    // Clicking again while in-flight does not send another request
    fireEvent.click(item1);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Resolve network request successfully
    await act(async () => {
      resolveFetch(new Response('{}', { status: 200 }));
    });

    // In-flight completing attribute cleared, remains done
    expect(item1.dataset.completing).toBeUndefined();
    expect(item1.className).toContain('task-item-done');
    expect(summary?.textContent).toContain('All done');
  });

  it('preserves optimistic completed state across concurrent SSE widget.update events while in-flight', async () => {
    let resolveFetch!: (res: Response) => void;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => fetchPromise));

    useDisplayStore.setState({
      widgets: {
        'tasks-sse': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            items: [{ id: 'task-concurrent', title: 'Buy milk', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-sse" type="tasks" />);
    const item = container.querySelector('[data-item-id="task-concurrent"]') as HTMLElement;

    // Trigger completion
    fireEvent.click(item);
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS);
    });
    expect(item.dataset.completing).toBe('true');
    expect(item.className).toContain('task-item-done');

    // Simulate concurrent SSE widget.update arriving with old uncompleted item data
    act(() => {
      useDisplayStore.getState().applyEvents([
        {
          type: 'widget.update',
          data: {
            widget_id: 'tasks-sse',
            timestamp: '2026-10-31T12:01:00Z',
            state: 'healthy',
            data: {
              items: [{ id: 'task-concurrent', title: 'Buy milk', done: false }],
            },
          },
        },
      ]);
    });

    // Optimistic done and completing state must be preserved
    const refreshedItem = container.querySelector('[data-item-id="task-concurrent"]') as HTMLElement;
    expect(refreshedItem.dataset.completing).toBe('true');
    expect(refreshedItem.className).toContain('task-item-done');

    // Now resolve fetch
    await act(async () => {
      resolveFetch(new Response('{}', { status: 200 }));
    });

    expect(refreshedItem.dataset.completing).toBeUndefined();
    expect(refreshedItem.className).toContain('task-item-done');
  });

  it('clears optimisticDone when server snapshot updates and reflects reopened or reset recurring tasks', async () => {
    let resolveFetch!: (res: Response) => void;
    const fetchPromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => fetchPromise));

    useDisplayStore.setState({
      widgets: {
        'tasks-reopen': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            items: [{ id: 'task-recur', title: 'Daily vitamins', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-reopen" type="tasks" />);
    const item = container.querySelector('[data-item-id="task-recur"]') as HTMLElement;

    // Complete the task
    fireEvent.click(item);
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS);
    });
    expect(item.className).toContain('task-item-done');
    expect(item.dataset.completing).toBe('true');

    // Resolve fetch
    await act(async () => {
      resolveFetch(new Response('{}', { status: 200 }));
    });
    expect(item.dataset.completing).toBeUndefined();
    expect(item.className).toContain('task-item-done');

    // Server snapshot arrives confirming the task was completed
    act(() => {
      useDisplayStore.getState().applyEvents([
        {
          type: 'widget.update',
          data: {
            widget_id: 'tasks-reopen',
            timestamp: '2026-10-31T12:01:00Z',
            state: 'healthy',
            data: {
              items: [{ id: 'task-recur', title: 'Daily vitamins', done: true }],
            },
          },
        },
      ]);
    });
    expect(item.className).toContain('task-item-done');

    // Later, recurring task resets / is reopened upstream with done: false
    act(() => {
      useDisplayStore.getState().applyEvents([
        {
          type: 'widget.update',
          data: {
            widget_id: 'tasks-reopen',
            timestamp: '2026-10-31T12:05:00Z',
            state: 'healthy',
            data: {
              items: [{ id: 'task-recur', title: 'Daily vitamins', done: false }],
            },
          },
        },
      ]);
    });

    // The task item should no longer be stuck in optimistic done
    const refreshedItem = container.querySelector('[data-item-id="task-recur"]') as HTMLElement;
    expect(refreshedItem.className).toContain('task-item-active');
  });

  it('rolls back optimistic state and data-completing on 500 response or network error', async () => {
    let rejectFetch!: (err: Error) => void;
    const fetchPromise = new Promise<Response>((_, reject) => {
      rejectFetch = reject;
    });
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => fetchPromise));

    useDisplayStore.setState({
      widgets: {
        'tasks-rollback': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            items: [{ id: 'item-fail', title: 'Mow lawn', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-rollback" type="tasks" />);
    const item = container.querySelector('[data-item-id="item-fail"]') as HTMLElement;
    const summary = container.querySelector('.tasks-summary');

    expect(summary?.textContent).toContain('1 left');

    fireEvent.click(item);
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS);
    });
    expect(item.dataset.completing).toBe('true');
    expect(item.className).toContain('task-item-done');
    expect(summary?.textContent).toContain('All done');

    // Network failure
    await act(async () => {
      rejectFetch(new Error('Network error'));
    });

    // Rolled back to active
    expect(item.dataset.completing).toBeUndefined();
    expect(item.className).toContain('task-item-active');
    expect(summary?.textContent).toContain('1 left');
  });

  it('rolls back optimistic state on non-200 HTTP response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('Server Error', { status: 500 })));

    useDisplayStore.setState({
      widgets: {
        'tasks-500': {
          meta: {
            type: 'tasks',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {
            items: [{ id: 'item-500', title: 'Fix bug', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-500" type="tasks" />);
    const item = container.querySelector('[data-item-id="item-500"]') as HTMLElement;

    fireEvent.click(item);
    await act(async () => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS);
    });

    expect(item.dataset.completing).toBeUndefined();
    expect(item.className).toContain('task-item-active');
  });

  it('enters pending-undo state with undo badge on first tap and delays fetch until grace window elapses', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    useDisplayStore.setState({
      widgets: {
        'tasks-undo-1': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: { items: [{ id: 'task-1', title: 'Buy groceries', done: false }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-undo-1" type="tasks" />);
    const item = container.querySelector('[data-item-id="task-1"]') as HTMLElement;
    const summary = container.querySelector('.tasks-summary');

    expect(summary?.textContent).toContain('1 left');
    expect(item.className).toContain('task-item-active');
    expect(item.querySelector('.task-undo-badge')).toBeNull();

    // First tap: enters pending-undo state immediately
    fireEvent.click(item);

    expect(item.className).toContain('task-item-done');
    expect(item.className).toContain('pending-undo');
    expect(item.dataset.completing).toBe('true');
    expect(item.dataset.pendingUndo).toBe('true');
    expect(item.querySelector('.task-undo-badge')).not.toBeNull();
    expect(item.querySelector('.task-undo-badge')?.textContent).toBe('Undo');
    expect(summary?.textContent).toContain('All done');

    // fetch must not be called yet before grace window elapses
    expect(fetchMock).not.toHaveBeenCalled();

    // Advance halfway through grace window: still no fetch
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS / 2);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // Advance remainder of grace window: fetch is dispatched
    await act(async () => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS / 2);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('api/lists/tasks-undo-1/items/task-1/complete', {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    expect(item.dataset.pendingUndo).toBeUndefined();
    expect(item.querySelector('.task-undo-badge')).toBeNull();
  });

  it('cancels completion and restores active state when tapped a second time during grace window', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    useDisplayStore.setState({
      widgets: {
        'tasks-undo-cancel': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: { items: [{ id: 'task-cancel', title: 'Call dentist', done: false }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-undo-cancel" type="tasks" />);
    const item = container.querySelector('[data-item-id="task-cancel"]') as HTMLElement;
    const summary = container.querySelector('.tasks-summary');

    expect(summary?.textContent).toContain('1 left');

    // First tap: optimistic done with pending-undo badge
    fireEvent.click(item);
    expect(item.dataset.pendingUndo).toBe('true');
    expect(summary?.textContent).toContain('All done');

    // Advance partially into grace window
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    // Second tap: undo completion
    fireEvent.click(item);

    expect(item.className).toContain('task-item-active');
    expect(item.className).not.toContain('task-item-done');
    expect(item.className).not.toContain('pending-undo');
    expect(item.dataset.completing).toBeUndefined();
    expect(item.dataset.pendingUndo).toBeUndefined();
    expect(item.querySelector('.task-undo-badge')).toBeNull();
    expect(summary?.textContent).toContain('1 left');
    expect(fetchMock).not.toHaveBeenCalled();

    // Advance well past initial grace window: fetch must never be called
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS * 2);
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('clears grace timer on unmount and prevents fetch from dispatching', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    useDisplayStore.setState({
      widgets: {
        'tasks-unmount': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: { items: [{ id: 'task-unmount', title: 'Unmount test', done: false }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container, unmount } = render(<TasksWidget id="tasks-unmount" type="tasks" />);
    const item = container.querySelector('[data-item-id="task-unmount"]') as HTMLElement;

    // First tap starts grace timer
    fireEvent.click(item);
    expect(item.dataset.pendingUndo).toBe('true');
    expect(fetchMock).not.toHaveBeenCalled();

    // Unmount while timer is pending
    unmount();

    // Advance time past grace window
    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS * 2);
    });

    // fetch must not be dispatched
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('renders error banner when widget state is error', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-err': {
          state: 'error',
          data: {},
          timestamp: '',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-err" type="tasks" />);
    const banner = container.querySelector('.widget-error-banner');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Widget Error');
  });

  it('memoizes store updates: React Profiler verifies widget.update for tasks-1 commits ONLY tasks-1', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-1': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 1], config: {}, assets_base: '' },
          state: 'healthy',
          data: { items: [{ id: 't1', title: 'Task 1', done: false }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
        'tasks-2': {
          meta: { type: 'tasks', origin: [3, 0], dimensions: [3, 1], config: {}, assets_base: '' },
          state: 'healthy',
          data: { items: [{ id: 't2', title: 'Task 2', done: false }] },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const onRender1 = vi.fn();
    const onRender2 = vi.fn();

    render(
      <div>
        <Profiler id="tasks-1-profiler" onRender={onRender1}>
          <TasksWidget id="tasks-1" type="tasks" />
        </Profiler>
        <Profiler id="tasks-2-profiler" onRender={onRender2}>
          <TasksWidget id="tasks-2" type="tasks" />
        </Profiler>
      </div>,
    );

    // Initial mount commits both once
    expect(onRender1).toHaveBeenCalledTimes(1);
    expect(onRender2).toHaveBeenCalledTimes(1);

    // Dispatch widget.update targeting tasks-1 ONLY
    act(() => {
      useDisplayStore.getState().applyEvents([
        {
          type: 'widget.update',
          data: {
            widget_id: 'tasks-1',
            timestamp: '2026-10-31T12:01:00Z',
            state: 'healthy',
            data: {
              items: [
                { id: 't1', title: 'Task 1', done: true },
                { id: 't1-new', title: 'Task 1 New', done: false },
              ],
            },
          },
        },
      ]);
    });

    // tasks-1 committed a second time, tasks-2 remained untouched
    expect(onRender1).toHaveBeenCalledTimes(2);
    expect(onRender2).toHaveBeenCalledTimes(1);
  });

  it('renders individual tags (section only, assignee only, dueDate only) cleanly', () => {
    useDisplayStore.setState({
      widgets: {
        'tasks-tags': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            items: [
              { id: 'tag-1', title: 'Only Section', done: false, section: 'Errands' },
              { id: 'tag-2', title: 'Only Assignee', done: false, assignee: 'Sam' },
              { id: 'tag-3', title: 'Only Due Date', done: false, due_date: 'Tomorrow' },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-tags" type="tasks" />);
    const item1 = container.querySelector('[data-item-id="tag-1"]');
    expect(item1?.querySelector('.task-tag-section')?.textContent).toBe('#Errands');
    expect(item1?.querySelector('.task-tag-assignee')).toBeNull();
    expect(item1?.querySelector('.task-tag-due')).toBeNull();

    const item2 = container.querySelector('[data-item-id="tag-2"]');
    expect(item2?.querySelector('.task-tag-section')).toBeNull();
    expect(item2?.querySelector('.task-tag-assignee')?.textContent).toBe('@Sam');
    expect(item2?.querySelector('.task-tag-due')).toBeNull();

    const item3 = container.querySelector('[data-item-id="tag-3"]');
    expect(item3?.querySelector('.task-tag-section')).toBeNull();
    expect(item3?.querySelector('.task-tag-assignee')).toBeNull();
    expect(item3?.querySelector('.task-tag-due')?.textContent).toBe('Tomorrow');
  });

  it('handles PascalCase Items, list.items, List.Items, and fallback item indices', () => {
    // 1. data.Items (PascalCase) and items with ID, Title, Done
    useDisplayStore.setState({
      widgets: {
        'tasks-pascal-items': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: { ListName: 'Chores' }, assets_base: '' },
          state: 'healthy',
          data: {
            Items: [
              { ID: 'p-1', Title: 'Pascal Item 1', Done: false },
              { Title: 'No ID Item', Done: true }, // exercises item-${idx} and Title fallback
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c1 } = render(<TasksWidget id="tasks-pascal-items" type="tasks" />);
    expect(c1.querySelector('.tasks-title')?.textContent).toBe('Chores');
    expect(c1.querySelector('[data-item-id="p-1"]')?.textContent).toContain('Pascal Item 1');
    expect(c1.querySelector('[data-item-id="item-1"]')?.textContent).toContain('No ID Item');

    // 2. data.list.items
    useDisplayStore.setState({
      widgets: {
        'tasks-nested-list': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: { name: 'Nested List' }, assets_base: '' },
          state: 'healthy',
          data: {
            list: {
              items: [{ id: 'n-1', title: 'Nested Item 1', done: false }],
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c2 } = render(<TasksWidget id="tasks-nested-list" type="tasks" />);
    expect(c2.querySelector('.tasks-title')?.textContent).toBe('Nested List');
    expect(c2.querySelector('[data-item-id="n-1"]')?.textContent).toContain('Nested Item 1');

    // 3. data.List.Items and data.List.Name
    useDisplayStore.setState({
      widgets: {
        'tasks-pascal-nested': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            List: {
              Name: 'Pascal Nested',
              Items: [{ ID: 'pn-1', Title: 'Pascal Nested Item', Done: false }],
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c3 } = render(<TasksWidget id="tasks-pascal-nested" type="tasks" />);
    expect(c3.querySelector('.tasks-title')?.textContent).toBe('Pascal Nested');
    expect(c3.querySelector('[data-item-id="pn-1"]')?.textContent).toContain('Pascal Nested Item');

    // 4. data.list.name
    useDisplayStore.setState({
      widgets: {
        'tasks-data-title': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            list: {
              name: 'Data Title List',
              items: [{ id: 'dt-1', title: 'Item DT', done: false }],
            },
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container: c4 } = render(<TasksWidget id="tasks-data-title" type="tasks" />);
    expect(c4.querySelector('.tasks-title')?.textContent).toBe('Data Title List');
  });

  it('handles item without any title string and reconciles optimistic done when server marks item done', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({
      widgets: {
        'tasks-empty-title': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            items: [
              { id: 'et-1', done: false },
            ],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-empty-title" type="tasks" />);
    expect(container.querySelector('.task-title')?.textContent).toBe('');

    const item = container.querySelector('[data-item-id="et-1"]')!;
    fireEvent.click(item);

    act(() => {
      useDisplayStore.setState({
        widgets: {
          'tasks-empty-title': {
            meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
            state: 'healthy',
            data: {
              items: [{ id: 'et-1', done: true }],
            },
            timestamp: '2026-10-31T12:01:00Z',
          },
        },
      });
    });

    expect(container.querySelector('.task-item-done')).not.toBeNull();
    vi.useRealTimers();
  });

  it('clears optimistic state when item is removed from server list during update', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({
      widgets: {
        'tasks-rem': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            items: [{ id: 'rem-1', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-rem" type="tasks" />);
    const item = container.querySelector('[data-item-id="rem-1"]')!;
    fireEvent.click(item);

    act(() => {
      vi.advanceTimersByTime(TASK_UNDO_GRACE_MS + 10);
    });

    act(() => {
      useDisplayStore.setState({
        widgets: {
          'tasks-rem': {
            meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
            state: 'healthy',
            data: {
              items: [],
            },
            timestamp: '2026-10-31T12:02:00Z',
          },
        },
      });
    });

    expect(container.querySelector('[data-item-id="rem-1"]')).toBeNull();
    vi.useRealTimers();
  });

  it('handles keyboard navigation on task items with Enter and Space', () => {
    vi.useFakeTimers();
    useDisplayStore.setState({
      widgets: {
        'tasks-kb': {
          meta: { type: 'tasks', origin: [0, 0], dimensions: [3, 2], config: {}, assets_base: '' },
          state: 'healthy',
          data: {
            items: [{ id: 'kb-1', title: 'Keyboard task', done: false }],
          },
          timestamp: '2026-10-31T12:00:00Z',
        },
      },
    });

    const { container } = render(<TasksWidget id="tasks-kb" type="tasks" />);
    const item = container.querySelector('[data-item-id="kb-1"]') as HTMLElement;
    expect(item.getAttribute('role')).toBe('button');
    expect(item.getAttribute('tabindex')).toBe('0');

    // Trigger Enter
    fireEvent.keyDown(item, { key: 'Enter' });
    expect(item.getAttribute('data-pending-undo')).toBe('true');

    // Trigger Space to cancel/undo
    fireEvent.keyDown(item, { key: ' ' });
    expect(item.getAttribute('data-pending-undo')).toBeNull();

    // Trigger other key (no-op)
    fireEvent.keyDown(item, { key: 'Escape' });
    vi.useRealTimers();
  });
});
