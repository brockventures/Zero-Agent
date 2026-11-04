/**
 * DOM helpers for widget fragments rendered by the legacy server templates. LegacyWidget
 * owns the element they are injected into; nothing here touches React.
 */

/** A controller the legacy widget scripts register on window, e.g. window.MirrormereLiveView. */
interface LegacyController {
  mount?: (element: HTMLElement) => unknown;
  unmount?: (widgetId: string) => unknown;
}

/** One mounted controller and the key its unmount expects. */
export interface LegacyMount {
  controller: LegacyController;
  key: string;
}

/** An `<img onload>` the template used to mount a controller. Candidates are tried in order. */
export interface LegacyOnloadHook {
  img: Element;
  targets: { name: string; selector: string }[];
}

export interface PreparedFragment {
  card: HTMLElement;
  hooks: LegacyOnloadHook[];
}

/** Matches `window.MirrormereX.mount(this.closest('selector'))` inside an onload attribute. */
const ONLOAD_MOUNT = /window\.(Mirrormere\w+)\.mount\(this\.closest\((['"])(.+?)\2\)\)/g;

/** Relative, like the other widget URLs: resolves under the page's /api prefix. */
export function legacyRenderURL(id: string, search: string): string {
  const base = `api/widgets/${encodeURIComponent(id)}/render`;
  const node = new URLSearchParams(search).get('node');
  return node ? `${base}?node=${encodeURIComponent(node)}` : base;
}

/**
 * Parses a fragment into the widget card. The card is the `.widget-card` root, or the first
 * element; sibling STYLE and SCRIPT nodes move into it, as carousel.js does. Parsing goes
 * through a template, so scripts are inert and images do not fire onload here. `onload`
 * attributes are taken off the images and returned as hooks to run after insert.
 * `<script type="application/json">` blocks stay in the card, as inert data for the controllers.
 */
export function prepareFragment(html: string): PreparedFragment {
  const template = document.createElement('template');
  template.innerHTML = html.trim();
  const root = template.content;
  const card = (root.querySelector<HTMLElement>('.widget-card') ?? root.firstElementChild ?? document.createElement('div')) as HTMLElement;
  for (const child of Array.from(root.children)) {
    if (child !== card && (child.tagName === 'STYLE' || child.tagName === 'SCRIPT')) card.appendChild(child);
  }

  const hooks: LegacyOnloadHook[] = [];
  for (const img of Array.from(card.querySelectorAll('img[onload]'))) {
    const code = img.getAttribute('onload') ?? '';
    img.removeAttribute('onload');
    const targets = Array.from(code.matchAll(ONLOAD_MOUNT), (m) => ({ name: m[1], selector: m[3] }));
    if (targets.length > 0) hooks.push({ img, targets });
  }
  return { card, hooks };
}

/**
 * Takes the stylesheet links under the type's assets base out of the card and makes sure each
 * one is in the document head exactly once. Checked per link, so a link that first appears on a
 * later render of the type is still applied.
 */
export function hoistTypeStylesheets(assetsBase: string, card: ParentNode): void {
  if (assetsBase === '') return;
  for (const link of Array.from(card.querySelectorAll('link[rel="stylesheet"]'))) {
    const href = link.getAttribute('href') ?? '';
    if (!href.startsWith(assetsBase)) continue;
    link.remove();
    const loaded = Array.from(document.head.querySelectorAll('link[rel="stylesheet"]')).some(
      (existing) => existing.getAttribute('href') === href,
    );
    if (!loaded) document.head.appendChild(link);
  }
}

/** Calls the unmount of every controller mounted by the previous fragment. */
export function unmountAll(mounts: LegacyMount[]): void {
  for (const { controller, key } of mounts.splice(0)) {
    if (typeof controller.unmount === 'function') controller.unmount(key);
  }
}

/**
 * Mounts the controller behind each onload hook. Must run after the card is in the document,
 * because the controllers check isConnected. Returns the mounts to unmount later.
 */
export function mountHooks(hooks: LegacyOnloadHook[], id: string): LegacyMount[] {
  const mounts: LegacyMount[] = [];
  const registry = globalThis as unknown as Record<string, LegacyController | undefined>;
  for (const { img, targets } of hooks) {
    for (const { name, selector } of targets) {
      const controller = registry[name];
      const element = img.closest<HTMLElement>(selector);
      if (!controller || typeof controller.mount !== 'function' || !element) continue;
      element.dataset.widgetId ??= id;
      controller.mount(element);
      mounts.push({ controller, key: element.dataset.widgetId });
      break;
    }
  }
  return mounts;
}

/** The card carousel.js shows when a fragment cannot be fetched on the first render. */
export function unavailableCard(id: string): HTMLElement {
  const card = document.createElement('div');
  card.className = 'widget-card';
  card.style.borderColor = 'var(--mm-status-error)';
  const message = document.createElement('div');
  message.style.color = 'var(--mm-status-error)';
  message.style.fontSize = '0.9rem';
  message.textContent = `Widget ${id} unavailable`;
  card.appendChild(message);
  return card;
}
