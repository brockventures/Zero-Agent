import { useEffect, useRef } from 'react';
import { useIsCurrentPane } from '../carousel/PaneContext';
import { useDisplayStore } from '../store';
import { useWidget } from '../store/hooks';
import type { WidgetComponentProps } from './registry';
import {
  hoistTypeStylesheets,
  legacyRenderURL,
  mountHooks,
  prepareFragment,
  unavailableCard,
  unmountAll,
  type LegacyMount,
  type LegacyOnloadHook,
} from './legacyFragment';

/** Per-cell state that outlives renders: the in-flight request and what is mounted in the card. */
interface LegacyState {
  /** Timestamp and reload count the last fetch was started for; null before the first fetch and after unmount. */
  fetchedKey: string | null;
  /** widgetReloads count for the type, frozen while the carousel is paused. */
  reloadsSeen: number;
  controller: AbortController | null;
  mounts: LegacyMount[];
}

/**
 * Renders a widget package that has only a legacy HTML template (custom packages in
 * /config/widgets). The fragment's card is injected into a container this component owns, so
 * React never reconciles the legacy DOM. Fetches on mount, and again when the widget's
 * timestamp moves while its pane is current. A pane that is not current keeps its content and
 * fetches once it becomes current, if the timestamp changed meanwhile.
 */
export function LegacyWidget({ id }: WidgetComponentProps) {
  const entry = useWidget(id);
  const current = useIsCurrentPane();
  const host = useRef<HTMLDivElement>(null);
  const state = useRef<LegacyState>({ fetchedKey: null, reloadsSeen: 0, controller: null, mounts: [] });
  const type = entry.meta?.type ?? '';
  const assetsBase = entry.meta?.assets_base ?? '';
  const timestamp = entry.timestamp;
  // A widget.reload (template, manifest or config change) does not move the timestamp, so the
  // reload count is part of the fetch key. Like WidgetHost, a reload waits while paused.
  const reloads = useDisplayStore((s) => (type === '' ? 0 : (s.widgetReloads[type] ?? 0)));
  const paused = useDisplayStore((s) => s.carousel.paused);

  useEffect(() => {
    const s = state.current;
    return () => {
      s.controller?.abort();
      s.controller = null;
      s.fetchedKey = null;
      unmountAll(s.mounts);
    };
  }, []);

  useEffect(() => {
    const s = state.current;
    if (!paused) {
      s.reloadsSeen = reloads;
    }
    const fetchKey = `${timestamp}\u0000${s.reloadsSeen}`;
    if (s.fetchedKey !== null && (!current || s.fetchedKey === fetchKey)) return;
    s.fetchedKey = fetchKey;
    s.controller?.abort();
    const controller = new AbortController();
    s.controller = controller;
    const el = host.current;
    const hadContent = el !== null && el.childElementCount > 0;

    const commit = (card: HTMLElement, hooks: LegacyOnloadHook[]) => {
      if (!el) return;
      // Controllers unmount before their card is replaced; mount after it is in the document.
      unmountAll(s.mounts);
      el.replaceChildren(card);
      s.mounts = mountHooks(hooks, id);
    };

    fetch(legacyRenderURL(id, window.location.search), { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((html) => {
        if (controller.signal.aborted) return;
        const { card, hooks } = prepareFragment(html);
        hoistTypeStylesheets(assetsBase, card);
        commit(card, hooks);
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        console.warn(`[mirrormere] legacy widget ${id} render failed:`, err);
        // Keep the last good card on a failed update; show the unavailable card on the first render.
        if (!hadContent) commit(unavailableCard(id), []);
      });
  }, [id, type, assetsBase, current, paused, reloads, timestamp]);

  return <div ref={host} className="widget-legacy" />;
}
