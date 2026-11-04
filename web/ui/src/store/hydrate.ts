import type { ScreensResponse, WidgetDetailResponse } from '../sse/types';
import type { WidgetEntry } from './types';

/**
 * Widget types whose view_model is derived server-side. A widget.update for
 * these re-runs hydrateWidget so the view model stays current without any
 * fetch logic in the widget itself.
 */
export const REFETCH_ON_UPDATE: ReadonlySet<string> = new Set(['calendar-grid']);

function widgetDetailURL(id: string): string {
  // Relative, like the legacy api/events URL: resolves under the page's /api prefix.
  return `api/widgets/${encodeURIComponent(id)}`;
}

export async function fetchWidgetDetail(
  id: string,
  fetchImpl: typeof fetch = fetch,
): Promise<WidgetDetailResponse> {
  const res = await fetchImpl(widgetDetailURL(id), { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    throw new Error(`widget ${id}: HTTP ${res.status}`);
  }
  return (await res.json()) as WidgetDetailResponse;
}

export function toWidgetEntry(body: WidgetDetailResponse): WidgetEntry {
  return {
    meta: {
      type: body.type,
      config: body.config,
      assets_base: body.assets_base,
      origin: body.origin,
      dimensions: body.dimensions,
    },
    state: body.state,
    data: body.data,
    viewModel: body.view_model,
    timestamp: body.timestamp,
  };
}

/** Relative, like widgetDetailURL: resolves under the page's /api prefix. */
const SCREENS_URL = 'api/screens';

export async function fetchScreenLayouts(fetchImpl: typeof fetch = fetch): Promise<ScreensResponse> {
  const res = await fetchImpl(SCREENS_URL, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    throw new Error(`screens: HTTP ${res.status}`);
  }
  return (await res.json()) as ScreensResponse;
}
