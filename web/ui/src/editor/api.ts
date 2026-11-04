import type {
  LayoutEditorResponse,
  LayoutEditorUpdateRequest,
  LayoutEditorWidgetPlacement,
} from '../sse/types';

export const LAYOUT_EDITOR_URL = 'api/layout/editor';

export async function fetchLayoutEditor(
  fetchImpl: typeof fetch = fetch,
): Promise<LayoutEditorResponse> {
  const res = await fetchImpl(LAYOUT_EDITOR_URL, {
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`fetch layout editor: HTTP ${res.status}`);
  }
  return (await res.json()) as LayoutEditorResponse;
}

export async function saveLayoutEditor(
  widgets: LayoutEditorWidgetPlacement[],
  fetchImpl: typeof fetch = fetch,
): Promise<LayoutEditorResponse> {
  const payload: LayoutEditorUpdateRequest = { widgets };
  const res = await fetchImpl(LAYOUT_EDITOR_URL, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`save layout editor: HTTP ${res.status}`);
  }
  return (await res.json()) as LayoutEditorResponse;
}

export async function resetLayoutEditor(
  fetchImpl: typeof fetch = fetch,
): Promise<LayoutEditorResponse> {
  const res = await fetchImpl(LAYOUT_EDITOR_URL, {
    method: 'DELETE',
    headers: { Accept: 'application/json' },
  });
  if (!res.ok) {
    throw new Error(`reset layout editor: HTTP ${res.status}`);
  }
  return (await res.json()) as LayoutEditorResponse;
}
