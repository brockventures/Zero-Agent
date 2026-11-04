import './TasksWidget.css';
import { useEffect, useRef, useState } from 'react';
import { useWidget } from '../../store/hooks';
import type { WidgetComponentProps } from '../registry';
import { tasksStats } from '../format';

export const TASK_UNDO_GRACE_MS = 3500;

interface TaskItemRecord {
  id?: string | number;
  ID?: string | number;
  list_id?: string | number;
  ListID?: string | number;
  listId?: string | number;
  title?: string;
  Title?: string;
  done?: boolean;
  Done?: boolean;
  section?: string;
  Section?: string;
  assignee?: string;
  Assignee?: string;
  due_date?: string;
  DueDate?: string;
  due?: string;
  Due?: string;
  [key: string]: unknown;
}

interface EffectiveTaskItem extends TaskItemRecord {
  itemId: string;
  done: boolean;
  isCompleting: boolean;
  isPendingUndo?: boolean;
}

function resolveCustomTitle(
  config?: Record<string, unknown>,
  data?: Record<string, unknown>,
): string {
  const candidates: unknown[] = [
    config?.list_name,
    config?.ListName,
    (config?.todoist as Record<string, unknown> | undefined)?.project_name,
    config?.name,
    config?.title,
    (data?.list as Record<string, unknown> | undefined)?.name,
    (data?.List as Record<string, unknown> | undefined)?.Name,
  ];
  for (const c of candidates) {
    if (typeof c === 'string' && c.trim() !== '') {
      return c.trim();
    }
  }
  return '';
}

function extractRawItems(data?: Record<string, unknown>): TaskItemRecord[] {
  if (!data) return [];
  const listObj = (data.list ?? data.List) as Record<string, unknown> | undefined;
  const candidate = data.items ?? data.Items ?? listObj?.items ?? listObj?.Items;
  return Array.isArray(candidate) ? (candidate as TaskItemRecord[]) : [];
}

function buildEffectiveItems(
  rawItems: TaskItemRecord[],
  optimisticDone: Record<string, boolean>,
  inFlight: Record<string, boolean>,
  pendingUndo: Record<string, boolean>,
): EffectiveTaskItem[] {
  return rawItems.map((item, idx) => {
    const itemId = String(item.id ?? item.ID ?? `item-${idx}`);
    const isPendingUndo = Boolean(pendingUndo[itemId]);
    const serverDone = Boolean(item.done ?? item.Done);
    const isDone = Boolean(optimisticDone[itemId]) || isPendingUndo || serverDone;
    const isCompleting = Boolean(inFlight[itemId]) || isPendingUndo;
    return {
      ...item,
      itemId,
      done: isDone,
      isCompleting,
      isPendingUndo,
    };
  });
}

function renderTasksHeader(title: string, stats?: { remaining: number; total: number }) {
  return (
    <div className="tasks-header">
      <div className="tasks-header-left">
        <div className="tasks-icon-box" aria-hidden="true">
          <svg
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="3"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>
        <span className="tasks-title">{title}</span>
      </div>
      {stats && (
        <span className="tasks-progress tasks-summary">
          {stats.remaining === 0 && stats.total > 0 ? 'All done' : `${stats.remaining} left`}
          <svg
            width="9"
            height="9"
            viewBox="0 0 24 24"
            fill="currentColor"
            className="tasks-sparkle-icon"
            aria-hidden="true"
          >
            <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z" />
          </svg>
        </span>
      )}
    </div>
  );
}

function renderTasksEmptyState() {
  return (
    <div className="tasks-empty">
      <div className="tasks-empty-icon" aria-hidden="true">
        <svg
          width="26"
          height="26"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--mm-mint)"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <polyline points="22 4 12 14.01 9 11.01" />
        </svg>
      </div>
      <span className="empty-title">All tasks completed!</span>
      <span className="empty-subtitle">Time to put your feet up</span>
    </div>
  );
}

interface TaskRowProps {
  item: EffectiveTaskItem;
  defaultListId: string;
  onItemClick: (itemId: string, listId: string, currentDone: boolean) => void;
}

function TaskRow({ item, defaultListId, onItemClick }: TaskRowProps) {
  const itemId = item.itemId;
  const listId = String(
    item.list_id ??
      item.ListID ??
      item.listId ??
      defaultListId,
  );
  const isDone = item.done;
  const isCompleting = item.isCompleting;
  const isPendingUndo = Boolean(item.isPendingUndo);
  const title = String(item.title ?? item.Title ?? '');
  const section = item.section ?? item.Section;
  const assignee = item.assignee ?? item.Assignee;
  const dueDate = item.due_date ?? item.DueDate ?? item.due ?? item.Due;
  const hasTags = Boolean(section || assignee || dueDate);

  return (
    <div
      key={itemId}
      className={`task-item ${isDone ? 'task-item-done' : 'task-item-active'}${isCompleting ? ' completing' : ''}${isPendingUndo ? ' pending-undo' : ''}`}
      role="button"
      tabIndex={0}
      aria-label={`${title}${isPendingUndo ? ', Tap to undo' : isDone ? ', Completed' : ''}`}
      data-item-id={itemId}
      data-task-id={itemId}
      data-list-id={listId}
      data-completing={isCompleting ? 'true' : undefined}
      data-pending-undo={isPendingUndo ? 'true' : undefined}
      onClick={() => onItemClick(itemId, listId, isDone)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onItemClick(itemId, listId, isDone);
        }
      }}
    >
      <div className="task-check-glyph">
        {isDone ? (
          <div className="task-checkbox-done" aria-hidden="true">
            <svg
              width="11"
              height="11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="3.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="20 6 9 17 4 12" />
            </svg>
          </div>
        ) : (
          <div className="task-checkbox-ring" aria-hidden="true" />
        )}
      </div>
      <div className="task-body">
        <span className="task-title">{title}</span>
        {hasTags && (
          <div className="task-tags">
            {section ? (
              <span className="task-tag-section">#{String(section)}</span>
            ) : null}
            {assignee ? (
              <span className="task-tag-assignee">@{String(assignee)}</span>
            ) : null}
            {dueDate ? (
              <span className="task-tag-due">{String(dueDate)}</span>
            ) : null}
          </div>
        )}
      </div>
      {isPendingUndo && (
        <span className="task-undo-badge" aria-label="Tap to undo">
          Undo
        </span>
      )}
    </div>
  );
}

interface TaskGroupSectionProps {
  items: EffectiveTaskItem[];
  defaultListId: string;
  onItemClick: (itemId: string, listId: string, currentDone: boolean) => void;
}

function TaskGroupSection({ items, defaultListId, onItemClick }: TaskGroupSectionProps) {
  return (
    <div className="tasks-items-container">
      <div className="tasks-list">
        {items.map((item) => (
          <TaskRow
            key={item.itemId}
            item={item}
            defaultListId={defaultListId}
            onItemClick={onItemClick}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * Native React widget component for task checklists.
 * Ports widgets/tasks/views/widget.html.
 */
export function TasksWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const config = entry?.meta?.config as Record<string, unknown> | undefined;
  const data = entry?.data as Record<string, unknown> | undefined;
  const isError = entry?.state === 'error';

  const isHydrated = Boolean(
    data && ('items' in data || 'Items' in data || 'list' in data || 'List' in data),
  );

  const customTitle = resolveCustomTitle(config, data);
  const rawItems = extractRawItems(data);

  const [inFlight, setInFlight] = useState<Record<string, boolean>>({});
  const [optimisticDone, setOptimisticDone] = useState<Record<string, boolean>>({});
  const [pendingUndo, setPendingUndo] = useState<Record<string, boolean>>({});
  const pendingTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const [prevSnapshot, setPrevSnapshot] = useState({ data, timestamp: entry?.timestamp });

  useEffect(() => {
    return () => {
      for (const timer of Object.values(pendingTimersRef.current)) {
        clearTimeout(timer);
      }
      pendingTimersRef.current = {};
    };
  }, []);

  if (entry?.timestamp !== prevSnapshot.timestamp || data !== prevSnapshot.data) {
    setPrevSnapshot({ data, timestamp: entry?.timestamp });
    if (Object.keys(optimisticDone).length > 0) {
      let changed = false;
      const next = { ...optimisticDone };
      for (const itemId of Object.keys(optimisticDone)) {
        const item = rawItems.find((it) => String(it.id ?? it.ID ?? '') === itemId);
        const serverDone = item ? Boolean(item.done ?? item.Done) : false;
        if (serverDone || (!inFlight[itemId] && !pendingUndo[itemId])) {
          delete next[itemId];
          changed = true;
        }
      }
      if (changed) {
        setOptimisticDone(next);
      }
    }
  }

  const effectiveItems = buildEffectiveItems(rawItems, optimisticDone, inFlight, pendingUndo);
  const stats = tasksStats(effectiveItems);

  const defaultListId = String(
    (data?.list as Record<string, unknown> | undefined)?.id ??
      (data?.List as Record<string, unknown> | undefined)?.ID ??
      id,
  );

  const handleItemClick = async (itemId: string, listId: string, currentDone: boolean) => {
    if (pendingTimersRef.current[itemId] !== undefined || pendingUndo[itemId]) {
      if (pendingTimersRef.current[itemId] !== undefined) {
        clearTimeout(pendingTimersRef.current[itemId]);
        delete pendingTimersRef.current[itemId];
      }
      setPendingUndo((prev) => {
        const next = { ...prev };
        delete next[itemId];
        return next;
      });
      setOptimisticDone((prev) => {
        const next = { ...prev };
        delete next[itemId];
        return next;
      });
      return;
    }

    if (currentDone || optimisticDone[itemId] || inFlight[itemId]) return;

    setPendingUndo((prev) => ({ ...prev, [itemId]: true }));
    setOptimisticDone((prev) => ({ ...prev, [itemId]: true }));

    pendingTimersRef.current[itemId] = setTimeout(async () => {
      delete pendingTimersRef.current[itemId];
      setPendingUndo((prev) => {
        const next = { ...prev };
        delete next[itemId];
        return next;
      });
      setInFlight((prev) => ({ ...prev, [itemId]: true }));

      try {
        const resp = await fetch(
          `api/lists/${encodeURIComponent(listId)}/items/${encodeURIComponent(itemId)}/complete`,
          {
            method: 'POST',
            headers: { Accept: 'application/json' },
          },
        );
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        setInFlight((prev) => {
          const next = { ...prev };
          delete next[itemId];
          return next;
        });
      } catch {
        setInFlight((prev) => {
          const next = { ...prev };
          delete next[itemId];
          return next;
        });
        setOptimisticDone((prev) => {
          const next = { ...prev };
          delete next[itemId];
          return next;
        });
      }
    }, TASK_UNDO_GRACE_MS);
  };

  return (
    <div className="widget-tasks">
      {isError && (
        <div className="widget-error-banner">
          <span className="error-icon">⚠</span>
          <span>Widget Error</span>
        </div>
      )}

      {!isHydrated ? (
        <>
          {customTitle ? renderTasksHeader(customTitle) : null}
          <div className="tasks-loading">
            <span>Loading checklist...</span>
          </div>
        </>
      ) : (
        <>
          {renderTasksHeader(customTitle || 'Tasks', stats)}

          {effectiveItems.length === 0 ? (
            renderTasksEmptyState()
          ) : (
            <TaskGroupSection
              items={effectiveItems}
              defaultListId={defaultListId}
              onItemClick={handleItemClick}
            />
          )}
        </>
      )}
    </div>
  );
}
