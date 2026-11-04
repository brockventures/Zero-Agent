import { describe, expect, it, vi } from 'vitest';
import type { LayoutEditorResponse, LayoutEditorWidgetPlacement } from '../sse/types';
import {
  LAYOUT_EDITOR_URL,
  fetchLayoutEditor,
  resetLayoutEditor,
  saveLayoutEditor,
} from './api';

const sampleResponse: LayoutEditorResponse = {
  allow_layout_edit: true,
  has_override: false,
  grid: [6, 2],
  total_screens: 1,
  widgets: [
    {
      widget_id: 'w1',
      type: 'clock',
      screen: 0,
      origin: [0, 0],
      dimensions: [2, 1],
      pinned: false,
      supported_dimensions: [[2, 1]],
    },
  ],
};

describe('editor api', () => {
  it('fetchLayoutEditor fetches GET api/layout/editor successfully', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (url, init) => {
      expect(String(url)).toBe(LAYOUT_EDITOR_URL);
      expect(init?.headers).toEqual({ Accept: 'application/json' });
      return new Response(JSON.stringify(sampleResponse), { status: 200 });
    });

    const res = await fetchLayoutEditor(fetchMock);
    expect(res).toEqual(sampleResponse);
  });

  it('fetchLayoutEditor throws on HTTP error', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('Internal error', { status: 500 }));
    await expect(fetchLayoutEditor(fetchMock)).rejects.toThrow('fetch layout editor: HTTP 500');
  });

  it('saveLayoutEditor puts PUT api/layout/editor successfully', async () => {
    const placements: LayoutEditorWidgetPlacement[] = [
      { widget_id: 'w1', screen: 0, origin: [2, 0], dimensions: [2, 1] },
    ];

    const fetchMock = vi.fn<typeof fetch>(async (url, init) => {
      expect(String(url)).toBe(LAYOUT_EDITOR_URL);
      expect(init?.method).toBe('PUT');
      expect(init?.headers).toEqual({
        'Content-Type': 'application/json',
        Accept: 'application/json',
      });
      expect(JSON.parse(init?.body as string)).toEqual({ widgets: placements });
      return new Response(JSON.stringify({ ...sampleResponse, has_override: true }), { status: 200 });
    });

    const res = await saveLayoutEditor(placements, fetchMock);
    expect(res.has_override).toBe(true);
  });

  it('saveLayoutEditor throws on HTTP error', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('Validation failed', { status: 400 }));
    await expect(saveLayoutEditor([], fetchMock)).rejects.toThrow('save layout editor: HTTP 400');
  });

  it('resetLayoutEditor deletes DELETE api/layout/editor successfully', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (url, init) => {
      expect(String(url)).toBe(LAYOUT_EDITOR_URL);
      expect(init?.method).toBe('DELETE');
      expect(init?.headers).toEqual({ Accept: 'application/json' });
      return new Response(JSON.stringify(sampleResponse), { status: 200 });
    });

    const res = await resetLayoutEditor(fetchMock);
    expect(res).toEqual(sampleResponse);
  });

  it('resetLayoutEditor throws on HTTP error', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('Forbidden', { status: 403 }));
    await expect(resetLayoutEditor(fetchMock)).rejects.toThrow('reset layout editor: HTTP 403');
  });
});
