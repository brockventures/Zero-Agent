import {
  useEffect,
  useRef,
  useState,
  ViewTransition,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import type { LayoutEditorWidget, LayoutEditorWidgetPlacement } from '../sse/types';
import { useDisplayStore } from '../store';
import { useLayoutEditorOpen } from '../store/hooks';
import { fetchLayoutEditor, resetLayoutEditor, saveLayoutEditor } from './api';
import {
  compactScreens,
  getOccupiedCellCount,
  getTotalScreens,
  moveOrSwapWidget,
  moveWidgetToScreen,
  resizeWidget,
  resolvePreviewOrigin,
  togglePinWidget,
} from './editorMath';

export interface LayoutEditorProps {
  fetchImpl?: typeof fetch;
}

function PinFilledIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="currentColor"
      aria-hidden="true"
      style={{ verticalAlign: 'middle', marginLeft: '4px' }}
    >
      <path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z" />
    </svg>
  );
}

function PinOutlineIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ verticalAlign: 'middle', marginLeft: '4px' }}
    >
      <path d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z" />
    </svg>
  );
}

export function LayoutEditor({ fetchImpl }: LayoutEditorProps = {}) {
  const isOpen = useLayoutEditorOpen();
  if (!isOpen) {
    return null;
  }
  return (
    <ViewTransition default="none">
      <LayoutEditorModal fetchImpl={fetchImpl} />
    </ViewTransition>
  );
}

function LayoutEditorModal({ fetchImpl }: LayoutEditorProps = {}) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [grid, setGrid] = useState<[number, number]>([6, 2]);
  const [widgets, setWidgets] = useState<LayoutEditorWidget[]>([]);
  const [activeScreen, setActiveScreen] = useState(0);
  const [, setHasOverride] = useState(false);

  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dragGhost, setDragGhost] = useState<{
    origin: [number, number];
    dimensions: [number, number];
  } | null>(null);
  const [dragHoverScreen, setDragHoverScreen] = useState<number | null>(null);

  const gridRef = useRef<HTMLDivElement>(null);
  const dragStartRef = useRef<{
    pointerId: number;
    widgetId: string;
    origin: [number, number];
    dimensions: [number, number];
    grabOffset: [number, number];
  } | null>(null);

  useEffect(() => {
    let active = true;
    fetchLayoutEditor(fetchImpl)
      .then((data) => {
        if (!active) return;
        setGrid(data.grid && data.grid.length === 2 ? [data.grid[0], data.grid[1]] : [6, 2]);
        setWidgets(data.widgets || []);
        setHasOverride(Boolean(data.has_override));
        setActiveScreen(0);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [fetchImpl]);

  const [gridCols, gridRows] = grid;
  const totalCells = gridCols * gridRows;
  const occupied = getOccupiedCellCount(widgets, activeScreen, gridCols, gridRows);
  const totalScreens = getTotalScreens(widgets);
  const visibleWidgets = widgets.filter((w) => w.pinned || w.screen === activeScreen);

  const handleReset = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await resetLayoutEditor(fetchImpl);
      setWidgets(res.widgets || []);
      setHasOverride(Boolean(res.has_override));
      setActiveScreen(0);
      await useDisplayStore.getState().loadScreens();
      useDisplayStore.getState().setLayoutEditorOpen(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const compacted = compactScreens(widgets);
      const placements: LayoutEditorWidgetPlacement[] = compacted.map((w) => ({
        widget_id: w.widget_id,
        screen: w.screen,
        origin: w.origin,
        dimensions: w.dimensions,
        pinned: w.pinned ? true : undefined,
      }));
      const res = await saveLayoutEditor(placements, fetchImpl);
      setWidgets(res.widgets || []);
      setHasOverride(Boolean(res.has_override));
      await useDisplayStore.getState().loadScreens();
      useDisplayStore.getState().setLayoutEditorOpen(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleTogglePin = (widgetId: string) => {
    const nextWidgets = togglePinWidget(widgets, widgetId, gridCols, gridRows, activeScreen);
    setWidgets(nextWidgets);
    const maxScreen = Math.max(0, getTotalScreens(nextWidgets) - 1);
    if (activeScreen > maxScreen) {
      setActiveScreen(maxScreen);
    }
  };

  const handleResize = (widgetId: string, newDimensions: [number, number]) => {
    setWidgets((prev) => resizeWidget(prev, widgetId, newDimensions, gridCols, gridRows));
  };

  const handlePointerDown = (e: ReactPointerEvent<HTMLDivElement>, widget: LayoutEditorWidget) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    if ((e.target as HTMLElement).closest('button')) return;
    const currentTarget = e.currentTarget;
    if (currentTarget.setPointerCapture) {
      try {
        currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // Ignored in non-supporting jsdom
      }
    }
    let grabOffset: [number, number] = [0, 0];
    if (gridRef.current) {
      const rect = gridRef.current.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        const pointerCol = Math.floor(((e.clientX - rect.left) / rect.width) * gridCols);
        const pointerRow = Math.floor(((e.clientY - rect.top) / rect.height) * gridRows);
        grabOffset = [
          Math.max(0, Math.min(pointerCol - widget.origin[0], widget.dimensions[0] - 1)),
          Math.max(0, Math.min(pointerRow - widget.origin[1], widget.dimensions[1] - 1)),
        ];
      }
    }
    dragStartRef.current = {
      pointerId: e.pointerId,
      widgetId: widget.widget_id,
      origin: widget.origin,
      dimensions: widget.dimensions,
      grabOffset,
    };
    setDraggingId(widget.widget_id);
    setDragGhost({ origin: widget.origin, dimensions: widget.dimensions });
  };

  const handlePointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragStartRef.current || dragStartRef.current.pointerId !== e.pointerId) return;

    if (typeof document !== 'undefined' && typeof document.elementFromPoint === 'function') {
      const dockPill = document
        .elementFromPoint(e.clientX, e.clientY)
        ?.closest('.layout-editor-screen-pill');
      if (dockPill) {
        const targetScreenIdx = Number(dockPill.getAttribute('data-screen-index'));
        setDragHoverScreen(Number.isNaN(targetScreenIdx) ? null : targetScreenIdx);
      } else {
        setDragHoverScreen(null);
      }
    }

    if (gridRef.current) {
      const rect = gridRef.current.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        const pointerCol = Math.floor(((e.clientX - rect.left) / rect.width) * gridCols);
        const pointerRow = Math.floor(((e.clientY - rect.top) / rect.height) * gridRows);
        const [wCols, wRows] = dragStartRef.current.dimensions;
        const [offsetCol, offsetRow] = dragStartRef.current.grabOffset;
        const clampedCol = Math.max(0, Math.min(pointerCol - offsetCol, gridCols - wCols));
        const clampedRow = Math.max(0, Math.min(pointerRow - offsetRow, gridRows - wRows));
        const snappedOrigin = resolvePreviewOrigin(
          widgets,
          dragStartRef.current.widgetId,
          activeScreen,
          [clampedCol, clampedRow],
          gridCols,
          gridRows,
        );
        setDragGhost({ origin: snappedOrigin, dimensions: [wCols, wRows] });
      }
    }
  };

  const handlePointerEnd = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragStartRef.current || dragStartRef.current.pointerId !== e.pointerId) return;
    const { widgetId } = dragStartRef.current;
    if (e.currentTarget.releasePointerCapture) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // Ignored in non-supporting jsdom
      }
    }

    let droppedOnDock = false;
    if (typeof document !== 'undefined' && typeof document.elementFromPoint === 'function') {
      const dockPill = document
        .elementFromPoint(e.clientX, e.clientY)
        ?.closest('.layout-editor-screen-pill');
      if (dockPill) {
        const targetScreenIdx = Number(dockPill.getAttribute('data-screen-index'));
        if (!Number.isNaN(targetScreenIdx)) {
          droppedOnDock = true;
          const nextWidgets = moveWidgetToScreen(
            widgets,
            widgetId,
            targetScreenIdx,
            gridCols,
            gridRows,
          );
          const prevWidget = widgets.find((w) => w.widget_id === widgetId);
          const nextWidget = nextWidgets.find((w) => w.widget_id === widgetId);
          if (nextWidget && nextWidget !== prevWidget) {
            setWidgets(nextWidgets);
            setActiveScreen(nextWidget.screen);
          }
        }
      }
    }

    if (!droppedOnDock && dragGhost) {
      setWidgets((prev) =>
        moveOrSwapWidget(prev, widgetId, activeScreen, dragGhost.origin, gridCols, gridRows),
      );
    }

    dragStartRef.current = null;
    setDraggingId(null);
    setDragGhost(null);
    setDragHoverScreen(null);
  };

  const gridStyle: CSSProperties = {
    display: 'grid',
    gridTemplateColumns: `repeat(${gridCols}, 1fr)`,
    gridTemplateRows: `repeat(${gridRows}, 1fr)`,
  };

  const slotKeys = Array.from({ length: totalCells }, (_, i) => {
    const col = i % gridCols;
    const row = Math.floor(i / gridCols);
    return { col, row };
  });

  return (
    <div
      className="layout-editor-overlay"
      id="layout-editor-overlay"
      data-prevent-screen-swipe="true"
    >
      <header className="header-zone layout-editor-toolbar">
        <div className="layout-editor-status" data-testid="layout-editor-status">
          {occupied}/{totalCells} cells • Auto-spacer padded
        </div>
        {error && <div className="layout-editor-error">{error}</div>}
        <div className="layout-editor-actions">
          <button
            className="layout-editor-btn layout-editor-btn-reset"
            type="button"
            aria-label="Reset to Default"
            onClick={() => void handleReset()}
            disabled={saving || loading}
          >
            Reset to Default
          </button>
          <button
            className="layout-editor-btn layout-editor-btn-cancel"
            type="button"
            aria-label="Cancel"
            onClick={() => useDisplayStore.getState().setLayoutEditorOpen(false)}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            className="layout-editor-btn layout-editor-btn-save"
            type="button"
            aria-label="Save Layout"
            onClick={() => void handleSave()}
            disabled={saving || loading}
          >
            {saving ? 'Saving...' : 'Save Layout'}
          </button>
        </div>
      </header>

      <main className="layout-editor-canvas">
        {loading ? (
          <div className="layout-editor-loading">Loading layout...</div>
        ) : (
          <div
            ref={gridRef}
            className="layout-editor-grid"
            data-testid="layout-editor-grid"
            style={gridStyle}
          >
            {slotKeys.map(({ col, row }) => (
              <div
                key={`slot-${col}-${row}`}
                className="layout-editor-cell"
                data-col={col}
                data-row={row}
                style={{
                  gridColumn: col + 1,
                  gridRow: row + 1,
                }}
              />
            ))}

            {dragGhost && draggingId && (
              <div
                className="layout-editor-ghost"
                data-testid="layout-editor-ghost"
                style={{
                  gridColumn: `${dragGhost.origin[0] + 1} / span ${dragGhost.dimensions[0]}`,
                  gridRow: `${dragGhost.origin[1] + 1} / span ${dragGhost.dimensions[1]}`,
                }}
              />
            )}

            {visibleWidgets.map((w) => {
              const isDragging = draggingId === w.widget_id;
              return (
                <div
                  key={w.widget_id}
                  className={`widget-card layout-editor-card ${w.pinned ? 'pinned' : ''} ${isDragging ? 'dragging' : ''}`}
                  data-widget-id={w.widget_id}
                  data-widget-type={w.type}
                  style={{
                    gridColumn: `${w.origin[0] + 1} / span ${w.dimensions[0]}`,
                    gridRow: `${w.origin[1] + 1} / span ${w.dimensions[1]}`,
                    touchAction: 'none',
                  }}
                  onPointerDown={(e) => handlePointerDown(e, w)}
                  onPointerMove={handlePointerMove}
                  onPointerUp={handlePointerEnd}
                  onPointerCancel={handlePointerEnd}
                >
                  <div className="layout-editor-card-header">
                    <span className="layout-editor-card-title">{w.widget_id}</span>
                    <span className="layout-editor-card-type">{w.type}</span>
                    <button
                      className={`layout-editor-pin-btn ${w.pinned ? 'active' : ''}`}
                      type="button"
                      aria-label={w.pinned ? 'Unpin Widget' : 'Pin Widget'}
                      title={w.pinned ? 'Unpin Widget' : 'Pin Widget'}
                      onClick={(e) => {
                        e.stopPropagation();
                        handleTogglePin(w.widget_id);
                      }}
                    >
                      {w.pinned ? (
                        <>
                          Pinned <PinFilledIcon />
                        </>
                      ) : (
                        <>
                          Pin <PinOutlineIcon />
                        </>
                      )}
                    </button>
                  </div>
                  <div className="layout-editor-card-body">
                    <span className="layout-editor-card-dim">
                      {w.dimensions[0]}×{w.dimensions[1]}
                    </span>
                  </div>
                  <div className="layout-editor-card-footer">
                    {w.supported_dimensions.map(([cols, rows]) => {
                      const isActive = w.dimensions[0] === cols && w.dimensions[1] === rows;
                      return (
                        <button
                          key={`${cols}x${rows}`}
                          className={`size-chip ${isActive ? 'active' : ''}`}
                          type="button"
                          aria-label={`Resize to ${cols} by ${rows}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleResize(w.widget_id, [cols, rows]);
                          }}
                        >
                          {cols}×{rows}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>

      <footer className="layout-editor-dock">
        {Array.from({ length: totalScreens }, (_, s) => (
          <button
            key={`dock-screen-${s}`}
            className={`layout-editor-screen-pill ${activeScreen === s ? 'active' : ''} ${dragHoverScreen === s ? 'dock-drop-hover' : ''}`}
            type="button"
            data-screen-index={s}
            aria-label={`Screen ${s + 1}`}
            onClick={() => setActiveScreen(s)}
          >
            Screen {s + 1}
          </button>
        ))}
        <button
          key="dock-screen-new"
          className={`layout-editor-screen-pill new-screen-pill ${dragHoverScreen === totalScreens ? 'dock-drop-hover' : ''}`}
          type="button"
          data-screen-index={totalScreens}
          aria-label="Add New Screen"
          onClick={() => setActiveScreen(totalScreens)}
        >
          + New Screen
        </button>
      </footer>
    </div>
  );
}
