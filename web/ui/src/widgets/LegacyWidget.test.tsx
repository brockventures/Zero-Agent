import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PaneContext } from '../carousel/PaneContext';
import { useDisplayStore, type WidgetEntry } from '../store';
import { LegacyWidget } from './LegacyWidget';
import { hoistTypeStylesheets, legacyRenderURL, mountHooks, prepareFragment, unmountAll } from './legacyFragment';

const ID = 'legacy-1';

function fragment(label: string, extra = ''): string {
  return `<div class="widget-card"><p class="label">${label}</p>${extra}</div>`;
}

function entry(timestamp: string, type = 'legacy-test', assetsBase = ''): WidgetEntry {
  return {
    meta: { type, config: {}, assets_base: assetsBase, origin: [0, 0], dimensions: [1, 1] },
    state: 'healthy',
    data: {},
    timestamp,
  };
}

function setWidget(timestamp: string, id = ID, type = 'legacy-test', assetsBase = ''): void {
  act(() => {
    useDisplayStore.setState((s) => ({ widgets: { ...s.widgets, [id]: entry(timestamp, type, assetsBase) } }));
  });
}

function element(current: boolean) {
  return (
    <PaneContext.Provider value={{ isCurrentPane: current, paneIndex: 0 }}>
      <LegacyWidget id={ID} type="legacy-test" />
    </PaneContext.Provider>
  );
}

/** Answers each render request. Tests swap `respond` to change what the server returns. */
type Responder = (url: string, init: RequestInit) => Promise<Response>;
let respond: Responder;
let fetchMock: ReturnType<typeof vi.fn>;

async function waitForText(root: ParentNode, text: string): Promise<void> {
  await vi.waitFor(() => {
    expect(root.querySelector('.widget-card')?.textContent).toContain(text);
  });
}

beforeEach(() => {
  useDisplayStore.setState({ widgets: {}, widgetReloads: {}, carousel: { paused: false, pendingRotate: null } });
  respond = async () => new Response(fragment('initial'), { status: 200 });
  fetchMock = vi.fn((url: string, init: RequestInit) => respond(url, init));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.head.querySelectorAll('link[rel="stylesheet"]').forEach((link) => link.remove());
  window.history.replaceState({}, '', '/');
  delete (globalThis as Record<string, unknown>).MirrormereLegacyTest;
});

describe('LegacyWidget', () => {
  it('inserts the fragment card into its own container on mount', async () => {
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    const card = container.querySelector<HTMLElement>('.widget-legacy > .widget-card');
    expect(card?.querySelector('.label')?.textContent).toBe('initial');
    expect(fetchMock).toHaveBeenCalledWith(`api/widgets/${ID}/render`, expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('sends the node query parameter the legacy page sends', async () => {
    window.history.replaceState({}, '', '/display?node=kiosk-1');
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    expect(fetchMock.mock.calls[0][0]).toBe(`api/widgets/${ID}/render?node=kiosk-1`);
  });

  it('replaces the card on a timestamp change and keeps one card', async () => {
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    respond = async () => new Response(fragment('updated'), { status: 200 });
    setWidget('t2');
    await waitForText(container, 'updated');
    expect(container.querySelectorAll('.widget-card')).toHaveLength(1);
  });

  it('does not re-fetch on update while its pane is not current, then fetches once when it becomes current', async () => {
    setWidget('t1');
    const { container, rerender } = render(element(false));
    await waitForText(container, 'initial');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    respond = async () => new Response(fragment('updated'), { status: 200 });
    setWidget('t2');
    await act(async () => {});
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.widget-card')?.textContent).toContain('initial');

    rerender(element(true));
    await waitForText(container, 'updated');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('unmounts the previous controller before replacing its card, and mounts the new one after insert', async () => {
    const seen = { unmountSawOldCard: false, mountConnected: [] as boolean[] };
    const controller = {
      mount: vi.fn((el: HTMLElement) => {
        seen.mountConnected.push(el.isConnected);
      }),
      unmount: vi.fn(() => {
        seen.unmountSawOldCard = document.querySelector('.label')?.textContent === 'old-card';
      }),
    };
    Object.assign(globalThis, { MirrormereLegacyTest: controller });
    const withHook = (label: string) =>
      fragment(
        label,
        `<div class="legacy-body"><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="" onload="window.MirrormereLegacyTest.mount(this.closest('.legacy-body'))"></div>`,
      );

    respond = async () => new Response(withHook('old-card'), { status: 200 });
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'old-card');
    expect(controller.mount).toHaveBeenCalledTimes(1);
    expect(seen.mountConnected).toEqual([true]);

    respond = async () => new Response(withHook('new-card'), { status: 200 });
    setWidget('t2');
    await waitForText(container, 'new-card');
    expect(controller.unmount).toHaveBeenCalledWith(ID);
    expect(seen.unmountSawOldCard).toBe(true);
    expect(controller.mount).toHaveBeenCalledTimes(2);
    expect(seen.mountConnected).toEqual([true, true]);
  });

  it('aborts a stale fetch when a newer one starts, and the one in flight on unmount', async () => {
    const calls: { signal: AbortSignal; resolve: (r: Response) => void }[] = [];
    respond = (_url, init) =>
      new Promise<Response>((resolve, reject) => {
        const signal = init.signal as AbortSignal;
        signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
        calls.push({ signal, resolve });
      });
    setWidget('t1');
    const { container, unmount } = render(element(true));
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    setWidget('t2');
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0].signal.aborted).toBe(true);
    expect(calls[1].signal.aborted).toBe(false);

    calls[1].resolve(new Response(fragment('second'), { status: 200 }));
    await waitForText(container, 'second');

    setWidget('t3');
    await vi.waitFor(() => expect(calls).toHaveLength(3));
    unmount();
    expect(calls[2].signal.aborted).toBe(true);
  });

  it('shows the unavailable card when the first render fails', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    respond = async () => new Response('gone', { status: 500 });
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'Widget legacy-1 unavailable');
  });

  it('keeps the last good card when an update fails', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    respond = async () => new Response('gone', { status: 500 });
    setWidget('t2');
    await vi.waitFor(() => expect(console.warn).toHaveBeenCalled());
    expect(container.querySelector('.widget-card')?.textContent).toContain('initial');
  });

  it('keeps a JSON data block in the card as inert data', async () => {
    respond = async () =>
      new Response(fragment('data', '<script type="application/json" class="legacy-data">{"a":1}</script>'), { status: 200 });
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'data');
    const block = container.querySelector<HTMLScriptElement>('.widget-card > script.legacy-data');
    expect(JSON.parse(block?.textContent ?? '')).toEqual({ a: 1 });
  });

  it('loads a type stylesheet once for every cell of that type', async () => {
    const assets = 'widget-types/legacy-css-1/assets/';
    const css = fragment('css', `<link rel="stylesheet" href="${assets}a.css">`);
    respond = async () => new Response(css, { status: 200 });
    setWidget('t1', ID, 'legacy-css-1', assets);
    setWidget('t1', 'legacy-2', 'legacy-css-1', assets);
    const { container } = render(
      <>
        <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
          <LegacyWidget id={ID} type="legacy-css-1" />
          <LegacyWidget id="legacy-2" type="legacy-css-1" />
        </PaneContext.Provider>
      </>,
    );
    await vi.waitFor(() => expect(container.querySelectorAll('.widget-card')).toHaveLength(2));
    expect(document.head.querySelectorAll(`link[href="${assets}a.css"]`)).toHaveLength(1);
    expect(container.querySelector(`link[href="${assets}a.css"]`)).toBeNull();
  });

  it('renders unavailableCard when first fetch fails, and keeps previous content when subsequent fetch fails', async () => {
    respond = async () => new Response('Server Error', { status: 500 });
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'unavailable');

    // Subsequent successful fetch
    respond = async () => new Response(fragment('good-content'), { status: 200 });
    setWidget('t2');
    await waitForText(container, 'good-content');

    // Subsequent failed fetch: keeps previous good card
    respond = async () => new Response('Server Error 500', { status: 500 });
    setWidget('t3');
    await act(async () => {});
    expect(container.querySelector('.widget-card')?.textContent).toContain('good-content');
  });

  it('handles widget with empty type string', async () => {
    setWidget('t1', ID, '');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
  });
});

describe('legacy fragment helpers', () => {
  it('re-fetches on a widget.reload for its type with an unchanged timestamp', async () => {
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    respond = async () => new Response(fragment('reloaded'), { status: 200 });
    act(() => {
      useDisplayStore.setState({ widgetReloads: { 'other-type': 1 } });
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    act(() => {
      useDisplayStore.setState({ widgetReloads: { 'other-type': 1, 'legacy-test': 1 } });
    });
    await waitForText(container, 'reloaded');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('holds a widget.reload while the carousel is paused and fetches once on resume', async () => {
    setWidget('t1');
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    respond = async () => new Response(fragment('reloaded'), { status: 200 });
    act(() => {
      useDisplayStore.setState({ carousel: { paused: true, pendingRotate: null } });
    });
    act(() => {
      useDisplayStore.setState({ widgetReloads: { 'legacy-test': 1 } });
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    act(() => {
      useDisplayStore.setState({ carousel: { paused: false, pendingRotate: null } });
    });
    await waitForText(container, 'reloaded');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('applies a type stylesheet that first appears on a later render', async () => {
    const base = 'widget-types/legacy-test/assets/';
    setWidget('t1', ID, 'legacy-test', base);
    const { container } = render(element(true));
    await waitForText(container, 'initial');
    expect(document.head.querySelector(`link[href="${base}late.css"]`)).toBeNull();
    respond = async () =>
      new Response(fragment('styled', `<link rel="stylesheet" href="${base}late.css">`), { status: 200 });
    setWidget('t2', ID, 'legacy-test', base);
    await waitForText(container, 'styled');
    expect(document.head.querySelectorAll(`link[href="${base}late.css"]`)).toHaveLength(1);
    expect(container.querySelector('link')).toBeNull();
  });

  it('builds the render URL with and without a node parameter', () => {
    expect(legacyRenderURL('a b', '')).toBe('api/widgets/a%20b/render');
    expect(legacyRenderURL('a', '?node=n/1')).toBe('api/widgets/a/render?node=n%2F1');
  });

  it('moves sibling style and script nodes into the card and removes onload from images', () => {
    const { card, hooks } = prepareFragment(
      '<style>.x{}</style><div class="widget-card"><img onload="window.MirrormereX.mount(this.closest(\'.x\'))"></div><script>1</script>',
    );
    expect(card.querySelector('style')).not.toBeNull();
    expect(card.querySelector('script')).not.toBeNull();
    expect(card.querySelector('img')?.hasAttribute('onload')).toBe(false);
    expect(hooks[0].targets).toEqual([{ name: 'MirrormereX', selector: '.x' }]);
  });

  it('leaves a link outside the assets base in the card', () => {
    const { card } = prepareFragment('<div class="widget-card"><link rel="stylesheet" href="other/x.css"></div>');
    hoistTypeStylesheets('widget-types/helper-type/assets/', card);
    expect(card.querySelector('link')).not.toBeNull();
    expect(document.head.querySelector('link[href="other/x.css"]')).toBeNull();
  });

  it('covers prepareFragment empty fallback and onload without mount match', () => {
    const { card, hooks } = prepareFragment('');
    expect(card.tagName).toBe('DIV');
    expect(hooks).toHaveLength(0);

    const { hooks: ignoredHooks } = prepareFragment(
      '<div class="widget-card"><img onload="console.log(1)"></div>',
    );
    expect(ignoredHooks).toHaveLength(0);
  });

  it('covers hoistTypeStylesheets with empty assetsBase and link without href', () => {
    const card = document.createElement('div');
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    card.appendChild(link);

    // Empty assetsBase returns early
    hoistTypeStylesheets('', card);
    expect(card.contains(link)).toBe(true);

    // Link without href continues loop
    hoistTypeStylesheets('assets/', card);
    expect(card.contains(link)).toBe(true);
  });

  it('covers unmountAll and mountHooks without mount or controller', () => {
    // unmountAll with controller without unmount
    unmountAll([{ controller: {}, key: 'k1' }]);

    // mountHooks when controller does not exist in window/globalThis
    const div = document.createElement('div');
    div.className = 'test-target';
    const img = document.createElement('img');
    div.appendChild(img);
    document.body.appendChild(div);

    const mounts = mountHooks(
      [{ img, targets: [{ name: 'NonExistentController', selector: '.test-target' }] }],
      'w-1',
    );
    expect(mounts).toHaveLength(0);

    // mountHooks when target element is not found
    const orphanImg = document.createElement('img');
    const mounts2 = mountHooks(
      [{ img: orphanImg, targets: [{ name: 'MirrormereX', selector: '.missing' }] }],
      'w-1',
    );
    expect(mounts2).toHaveLength(0);

    document.body.removeChild(div);
  });

  it('covers empty type, empty assetsBase, and abort in-flight fetch on unmount', async () => {
    act(() => {
      useDisplayStore.setState({
        widgets: {
          'legacy-empty-meta': {
            state: 'healthy',
            data: {},
            timestamp: 't-empty',
          },
        },
      });
    });

    const { unmount } = render(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <LegacyWidget id="legacy-empty-meta" type="" />
      </PaneContext.Provider>
    );

    unmount();
  });
});
