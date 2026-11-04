import './WidgetHost.css';
import { createElement, useEffect, useRef, type CSSProperties } from 'react';
import { hydratedReloadCount, needsHydrate, useDisplayStore } from '../store';
import { useWidget } from '../store/hooks';
import { LEGACY_WIDGET_FALLBACK, widgetComponentFor } from './registry';

export interface WidgetHostProps {
  id: string;
  origin: [number, number];
  dimensions: [number, number];
}

/** Grid cell style from a placement, as the legacy carousel sets it (1-indexed grid lines). */
function cellStyle(origin: [number, number], dimensions: [number, number]): CSSProperties {
  const colSpan = dimensions[0] || 1;
  const rowSpan = dimensions[1] || 1;
  return {
    gridColumn: `${origin[0] + 1} / span ${colSpan}`,
    gridRow: `${origin[1] + 1} / span ${rowSpan}`,
  };
}

/**
 * One widget cell. Renders its card on the first commit, so the e-ink settle
 * predicate sees the cell before the data arrives. Fetches the widget detail on mount
 * when the store has no meta for it, or a reload of its type arrived since (a remount
 * after a reload). Re-fetches when a widget.reload names its
 * type; a reload that arrives while the carousel is paused waits for resume.
 */
export function WidgetHost({ id, origin, dimensions }: WidgetHostProps) {
  const entry = useWidget(id);
  const type = entry.meta?.type;
  const reloads = useDisplayStore((s) => (type === undefined ? undefined : s.widgetReloads[type]));
  const paused = useDisplayStore((s) => s.carousel.paused);
  // The type and reload count this cell last acted on. Meta arrives after mount, so the
  // first time a type appears it becomes the baseline and does not count as a reload.
  const seen = useRef({ type, count: type === undefined ? 0 : hydratedReloadCount(id) });

  const dimCols = dimensions[0];
  const dimRows = dimensions[1];

  useEffect(() => {
    const currentDims: [number, number] = [dimCols, dimRows];
    if (needsHydrate(id, currentDims)) {
      // This fetch reads the latest reload count, so the reload effect must not repeat it.
      const reloadsNow = useDisplayStore.getState().widgetReloads;
      seen.current = { ...seen.current, count: reloadsNow[seen.current.type ?? ''] ?? 0 };
      const currentEntry = useDisplayStore.getState().widgets[id];
      const metaDims = currentEntry?.meta?.dimensions;
      const dimChanged =
        metaDims !== undefined &&
        (metaDims[0] !== dimCols || metaDims[1] !== dimRows);
      useDisplayStore
        .getState()
        .hydrateWidget(id, dimChanged ? { fresh: true } : undefined)
        .catch((err: unknown) => {
          console.warn(`[mirrormere] widget ${id} hydrate failed:`, err);
        });
    }
  }, [id, dimCols, dimRows]);

  useEffect(() => {
    const count = reloads ?? 0;
    if (seen.current.type !== type) {
      // The first time this cell learns its type, the reloads already counted are its baseline.
      seen.current = { type, count: hydratedReloadCount(id) };
    }
    if (paused || count <= seen.current.count) return;
    seen.current = { type, count };
    useDisplayStore.getState().hydrateWidget(id).catch((err: unknown) => {
      console.warn(`[mirrormere] widget ${id} reload hydrate failed:`, err);
    });
  }, [id, type, reloads, paused]);

  // No meta yet means no type to pick a component for; the cell stays empty until the detail arrives.
  const componentFn = type === undefined ? undefined : (widgetComponentFor(type) ?? LEGACY_WIDGET_FALLBACK);
  return (
    <div className="widget-card" data-widget-id={id} data-widget-type={type} style={cellStyle(origin, dimensions)}>
      {componentFn ? createElement(componentFn, { id, type: type ?? '' }) : <div className="widget-card-placeholder">{type ?? ''}</div>}
    </div>
  );
}
