import type { LayoutEditorWidget } from '../sse/types';

/**
 * Computes the integer cell bitmask for a rectangular placement on a gridCols-wide grid.
 */
export function computeWidgetBitmask(
  origin: [number, number],
  dimensions: [number, number],
  gridCols: number,
): number {
  let mask = 0;
  const [col, row] = origin;
  const [cols, rows] = dimensions;
  for (let r = 0; r < rows; r++) {
    const rowMask = ((1 << cols) - 1) << col;
    mask |= rowMask << ((row + r) * gridCols);
  }
  return mask;
}

/**
 * Computes the aggregate cell bitmask for all placed or pinned widgets on `screen`.
 */
export function computeScreenMask(
  widgets: readonly LayoutEditorWidget[],
  screen: number,
  gridCols: number,
  _gridRows: number,
  excludeWidgetId?: string,
): number {
  let mask = 0;
  for (const w of widgets) {
    if (excludeWidgetId && w.widget_id === excludeWidgetId) {
      continue;
    }
    if (w.pinned || w.screen === screen) {
      mask |= computeWidgetBitmask(w.origin, w.dimensions, gridCols);
    }
  }
  return mask;
}

/**
 * Returns the total number of rotation screens represented by unpinned widgets.
 */
export function getTotalScreens(widgets: readonly LayoutEditorWidget[]): number {
  const unpinned = widgets.filter((w) => !w.pinned);
  if (unpinned.length === 0) return 1;
  return Math.max(1, Math.max(...unpinned.map((w) => w.screen)) + 1);
}

/**
 * Computes the number of non-empty cells occupied on `screen`.
 */
export function getOccupiedCellCount(
  widgets: readonly LayoutEditorWidget[],
  screen: number,
  gridCols: number,
  gridRows: number,
): number {
  const mask = computeScreenMask(widgets, screen, gridCols, gridRows);
  let count = 0;
  let m = mask;
  while (m > 0) {
    count += m & 1;
    m >>= 1;
  }
  return count;
}

/**
 * Validates grid boundaries and checks for bitmask collisions on target screen(s).
 */
export function canPlaceWidget(
  widgets: readonly LayoutEditorWidget[],
  target: {
    widget_id: string;
    screen: number;
    origin: [number, number];
    dimensions: [number, number];
    pinned?: boolean;
  },
  gridCols: number,
  gridRows: number,
): boolean {
  const [col, row] = target.origin;
  const [cols, rows] = target.dimensions;
  if (col < 0 || row < 0 || cols < 1 || rows < 1) {
    return false;
  }
  if (col + cols > gridCols || row + rows > gridRows) {
    return false;
  }
  const targetMask = computeWidgetBitmask(target.origin, target.dimensions, gridCols);
  if (target.pinned) {
    const unpinnedScreens = widgets
      .filter((w) => !w.pinned && w.widget_id !== target.widget_id)
      .map((w) => w.screen);
    const maxS = unpinnedScreens.length === 0 ? 1 : Math.max(1, Math.max(...unpinnedScreens) + 1);
    for (let s = 0; s < maxS; s++) {
      const sMask = computeScreenMask(widgets, s, gridCols, gridRows, target.widget_id);
      if ((sMask & targetMask) !== 0) {
        return false;
      }
    }
    return true;
  }
  const sMask = computeScreenMask(widgets, target.screen, gridCols, gridRows, target.widget_id);
  return (sMask & targetMask) === 0;
}

/**
 * Re-indexes screen indices for unpinned widgets so they form a contiguous 0..K-1 range.
 */
export function compactScreens(widgets: readonly LayoutEditorWidget[]): LayoutEditorWidget[] {
  const unpinned = widgets.filter((w) => !w.pinned);
  if (unpinned.length === 0) {
    return widgets.map((w) => (w.screen !== 0 ? { ...w, screen: 0 } : w));
  }
  const screenSet = new Set<number>();
  for (const w of unpinned) {
    screenSet.add(w.screen);
  }
  const sortedScreens = Array.from(screenSet).sort((a, b) => a - b);
  const screenMap = new Map<number, number>();
  sortedScreens.forEach((oldScreen, newScreen) => {
    screenMap.set(oldScreen, newScreen);
  });
  return widgets.map((w) => {
    if (w.pinned) {
      return w.screen === 0 ? w : { ...w, screen: 0 };
    }
    const newScreen = screenMap.get(w.screen) ?? 0;
    return w.screen === newScreen ? w : { ...w, screen: newScreen };
  });
}

function verifyNoCollisions(
  widgets: readonly LayoutEditorWidget[],
  gridCols: number,
): boolean {
  const maxS = getTotalScreens(widgets);
  for (let s = 0; s < maxS; s++) {
    let sMask = 0;
    for (const w of widgets) {
      if (w.pinned || w.screen === s) {
        const m = computeWidgetBitmask(w.origin, w.dimensions, gridCols);
        if ((sMask & m) !== 0) {
          return false;
        }
        sMask |= m;
      }
    }
  }
  return true;
}

function candidateOriginsForWidget(
  w: LayoutEditorWidget,
  gridCols: number,
  gridRows: number,
  preferCurrent: boolean,
): [number, number][] {
  const [cols, rows] = w.dimensions;
  const candidates: [number, number][] = preferCurrent ? [w.origin] : [];
  for (let r = 0; r <= gridRows - rows; r++) {
    for (let c = 0; c <= gridCols - cols; c++) {
      if (!preferCurrent || c !== w.origin[0] || r !== w.origin[1]) candidates.push([c, r]);
    }
  }
  return candidates;
}

function packWidgetsIntoScreen(
  toPack: readonly LayoutEditorWidget[],
  idx: number,
  occupiedMask: number,
  gridCols: number,
  gridRows: number,
  preferCurrent: boolean,
  placed: Map<string, [number, number]>,
): boolean {
  if (idx >= toPack.length) return true;
  const w = toPack[idx];
  for (const origin of candidateOriginsForWidget(w, gridCols, gridRows, preferCurrent)) {
    const mask = computeWidgetBitmask(origin, w.dimensions, gridCols);
    if ((occupiedMask & mask) !== 0) continue;
    placed.set(w.widget_id, origin);
    if (packWidgetsIntoScreen(toPack, idx + 1, occupiedMask | mask, gridCols, gridRows, preferCurrent, placed)) {
      return true;
    }
    placed.delete(w.widget_id);
  }
  return false;
}

function applyPlacedMap(
  widgets: readonly LayoutEditorWidget[],
  sourceId: string,
  targetScreenActual: number,
  targetOrigin: [number, number],
  placed: Map<string, [number, number]>,
  gridCols: number,
): LayoutEditorWidget[] | null {
  const candidate = widgets.map((w) => {
    if (w.widget_id === sourceId) return { ...w, screen: targetScreenActual, origin: targetOrigin };
    const nextOrigin = placed.get(w.widget_id);
    return nextOrigin ? { ...w, origin: nextOrigin } : w;
  });
  return verifyNoCollisions(candidate, gridCols) ? candidate : null;
}

function tryReflowScreen(
  widgets: readonly LayoutEditorWidget[],
  source: LayoutEditorWidget,
  targetScreen: number,
  targetOrigin: [number, number],
  overlapping: readonly LayoutEditorWidget[],
  gridCols: number,
  gridRows: number,
): LayoutEditorWidget[] | null {
  if (!source.pinned && source.screen !== targetScreen) return null;
  if (overlapping.some((w) => w.pinned)) return null;

  const targetScreenActual = source.pinned ? 0 : targetScreen;
  const sourceMask = computeWidgetBitmask(targetOrigin, source.dimensions, gridCols);
  const pinnedMask = widgets
    .filter((w) => w.pinned && w.widget_id !== source.widget_id)
    .reduce((acc, w) => acc | computeWidgetBitmask(w.origin, w.dimensions, gridCols), 0);
  if ((sourceMask & pinnedMask) !== 0) return null;

  // Pass 1: Keep non-overlapping widgets fixed and pack only the overlapping widgets into vacated slots
  const overlappingIds = new Set(overlapping.map((w) => w.widget_id));
  const fixedMask = widgets
    .filter((w) => w.widget_id !== source.widget_id && !overlappingIds.has(w.widget_id) && (w.pinned || w.screen === targetScreen))
    .reduce((acc, w) => acc | computeWidgetBitmask(w.origin, w.dimensions, gridCols), sourceMask);
  const sortedOverlapping = [...overlapping].sort((a, b) => a.origin[1] - b.origin[1] || a.origin[0] - b.origin[0]);
  const placedPass1 = new Map<string, [number, number]>();
  if (packWidgetsIntoScreen(sortedOverlapping, 0, fixedMask, gridCols, gridRows, false, placedPass1)) {
    const res = applyPlacedMap(widgets, source.widget_id, targetScreenActual, targetOrigin, placedPass1, gridCols);
    if (res) return res;
  }

  // Pass 2: Repack all unpinned widgets on targetScreen around source at targetOrigin
  const allOtherUnpinned = widgets
    .filter((w) => !w.pinned && w.screen === targetScreen && w.widget_id !== source.widget_id)
    .sort((a, b) => b.dimensions[0] * b.dimensions[1] - a.dimensions[0] * a.dimensions[1] || a.origin[1] - b.origin[1] || a.origin[0] - b.origin[0]);
  const placedPass2 = new Map<string, [number, number]>();
  if (packWidgetsIntoScreen(allOtherUnpinned, 0, sourceMask | pinnedMask, gridCols, gridRows, true, placedPass2)) {
    return applyPlacedMap(widgets, source.widget_id, targetScreenActual, targetOrigin, placedPass2, gridCols);
  }
  return null;
}

function tryPlaceAtOrigin(
  widgets: readonly LayoutEditorWidget[],
  source: LayoutEditorWidget,
  targetScreen: number,
  targetOrigin: [number, number],
  gridCols: number,
  gridRows: number,
): LayoutEditorWidget[] | null {
  const targetScreenActual = source.pinned ? 0 : targetScreen;
  if (canPlaceWidget(widgets, { ...source, screen: targetScreenActual, origin: targetOrigin }, gridCols, gridRows)) {
    return widgets.map((w) => (w.widget_id === source.widget_id ? { ...w, screen: targetScreenActual, origin: targetOrigin } : w));
  }

  const targetMask = computeWidgetBitmask(targetOrigin, source.dimensions, gridCols);
  const overlapping = widgets.filter(
    (w) =>
      w.widget_id !== source.widget_id &&
      (w.pinned || w.screen === targetScreen) &&
      (computeWidgetBitmask(w.origin, w.dimensions, gridCols) & targetMask) !== 0,
  );

  if (overlapping.length === 1 && (source.pinned || source.screen === targetScreen)) {
    const other = overlapping[0];
    const fitsAtSource =
      source.origin[0] >= 0 &&
      source.origin[1] >= 0 &&
      source.origin[0] + other.dimensions[0] <= gridCols &&
      source.origin[1] + other.dimensions[1] <= gridRows;
    if (fitsAtSource) {
      const placed = new Map<string, [number, number]>([[other.widget_id, source.origin]]);
      const swapped = applyPlacedMap(widgets, source.widget_id, targetScreenActual, targetOrigin, placed, gridCols);
      if (swapped) {
        return swapped.map((w) => (w.widget_id === other.widget_id ? { ...w, screen: other.pinned ? 0 : targetScreen } : w));
      }
    }
  }

  return tryReflowScreen(widgets, source, targetScreen, targetOrigin, overlapping, gridCols, gridRows);
}

/**
 * Moves a widget to a target origin on targetScreen, swapping or reflowing overlapping widgets if possible.
 */
export function moveOrSwapWidget(
  widgets: readonly LayoutEditorWidget[],
  widgetId: string,
  targetScreen: number,
  targetOrigin: [number, number],
  gridCols: number,
  gridRows: number,
): LayoutEditorWidget[] {
  const source = widgets.find((w) => w.widget_id === widgetId);
  if (!source) return [...widgets];
  if ((source.pinned || source.screen === targetScreen) && source.origin[0] === targetOrigin[0] && source.origin[1] === targetOrigin[1]) {
    return [...widgets];
  }

  const [tCol, tRow] = targetOrigin;
  if (tCol < 0 || tRow < 0 || tCol + source.dimensions[0] > gridCols || tRow + source.dimensions[1] > gridRows) {
    return [...widgets];
  }

  const direct = tryPlaceAtOrigin(widgets, source, targetScreen, targetOrigin, gridCols, gridRows);
  if (direct) return direct;
  if (source.pinned || source.screen !== targetScreen) return [...widgets];

  // Snap to nearest valid reflow origin along the drag axis when an intermediate cell splits free space
  const [sCol, sRow] = source.origin;
  const dCol = tCol - sCol;
  const dRow = tRow - sRow;
  const snapCandidates: [number, number][] = [];
  for (let r = 0; r <= gridRows - source.dimensions[1]; r++) {
    for (let c = 0; c <= gridCols - source.dimensions[0]; c++) {
      if ((c === sCol && r === sRow) || (c === tCol && r === tRow)) continue;
      if (dCol === 0 ? c !== tCol : dCol > 0 ? c <= sCol : c >= sCol) continue;
      if (dRow === 0 ? r !== tRow : dRow > 0 ? r <= sRow : r >= sRow) continue;
      snapCandidates.push([c, r]);
    }
  }
  snapCandidates.sort((a, b) => Math.abs(a[0] - tCol) + Math.abs(a[1] - tRow) - (Math.abs(b[0] - tCol) + Math.abs(b[1] - tRow)));
  for (const cand of snapCandidates) {
    const candMask = computeWidgetBitmask(cand, source.dimensions, gridCols);
    const candOverlapping = widgets.filter(
      (w) =>
        w.widget_id !== source.widget_id &&
        (w.pinned || w.screen === targetScreen) &&
        (computeWidgetBitmask(w.origin, w.dimensions, gridCols) & candMask) !== 0,
    );
    if (candOverlapping.length === 0) continue;
    const snapped = tryReflowScreen(widgets, source, targetScreen, cand, candOverlapping, gridCols, gridRows);
    if (snapped) return snapped;
  }

  return [...widgets];
}

/**
 * Resolves the effective preview drop origin for a dragged widget, snapping to valid swap/reflow origins.
 */
export function resolvePreviewOrigin(
  widgets: readonly LayoutEditorWidget[],
  widgetId: string,
  targetScreen: number,
  rawOrigin: [number, number],
  gridCols: number,
  gridRows: number,
): [number, number] {
  const source = widgets.find((w) => w.widget_id === widgetId);
  if (!source) return rawOrigin;
  const resolved = moveOrSwapWidget(widgets, widgetId, targetScreen, rawOrigin, gridCols, gridRows);
  const moved = resolved.find((w) => w.widget_id === widgetId);
  return moved && (moved.origin[0] !== source.origin[0] || moved.origin[1] !== source.origin[1]) ? moved.origin : rawOrigin;
}

/**
 * Moves a widget to targetScreen, finding an open placement and compacting empty screens.
 */
export function moveWidgetToScreen(
  widgets: readonly LayoutEditorWidget[],
  widgetId: string,
  targetScreen: number,
  gridCols: number,
  gridRows: number,
): LayoutEditorWidget[] {
  const widget = widgets.find((w) => w.widget_id === widgetId);
  if (!widget || targetScreen < 0) return [...widgets];
  if (widget.screen === targetScreen && !widget.pinned) return [...widgets];

  let placed: LayoutEditorWidget | null = null;
  if (canPlaceWidget(widgets, { ...widget, screen: targetScreen, origin: widget.origin, pinned: false }, gridCols, gridRows)) {
    placed = { ...widget, screen: targetScreen, origin: widget.origin, pinned: false };
  } else {
    for (let r = 0; r <= gridRows - widget.dimensions[1] && !placed; r++) {
      for (let c = 0; c <= gridCols - widget.dimensions[0]; c++) {
        if (canPlaceWidget(widgets, { ...widget, screen: targetScreen, origin: [c, r], pinned: false }, gridCols, gridRows)) {
          placed = { ...widget, screen: targetScreen, origin: [c, r], pinned: false };
          break;
        }
      }
    }
  }

  if (!placed) return [...widgets];
  return compactScreens(widgets.map((w) => (w.widget_id === widgetId ? placed : w)));
}

/**
 * Resizes a widget to newDimensions, adjusting origin or relocating to an open slot if needed.
 */
export function resizeWidget(
  widgets: readonly LayoutEditorWidget[],
  widgetId: string,
  newDimensions: [number, number],
  gridCols: number,
  gridRows: number,
): LayoutEditorWidget[] {
  const widget = widgets.find((w) => w.widget_id === widgetId);
  if (!widget) return [...widgets];
  const [newCols, newRows] = newDimensions;
  if (widget.dimensions[0] === newCols && widget.dimensions[1] === newRows) return [...widgets];
  if (newCols < 1 || newRows < 1 || newCols > gridCols || newRows > gridRows) return [...widgets];

  if (canPlaceWidget(widgets, { ...widget, dimensions: newDimensions }, gridCols, gridRows)) {
    return widgets.map((w) => (w.widget_id === widgetId ? { ...w, dimensions: newDimensions } : w));
  }

  const clampedCol = Math.min(widget.origin[0], gridCols - newCols);
  const clampedRow = Math.min(widget.origin[1], gridRows - newRows);
  if (
    clampedCol >= 0 &&
    clampedRow >= 0 &&
    canPlaceWidget(widgets, { ...widget, origin: [clampedCol, clampedRow], dimensions: newDimensions }, gridCols, gridRows)
  ) {
    return widgets.map((w) =>
      w.widget_id === widgetId ? { ...w, origin: [clampedCol, clampedRow], dimensions: newDimensions } : w,
    );
  }

  for (let r = 0; r <= gridRows - newRows; r++) {
    for (let c = 0; c <= gridCols - newCols; c++) {
      if (canPlaceWidget(widgets, { ...widget, origin: [c, r], dimensions: newDimensions }, gridCols, gridRows)) {
        return widgets.map((w) => (w.widget_id === widgetId ? { ...w, origin: [c, r], dimensions: newDimensions } : w));
      }
    }
  }

  return [...widgets];
}

/**
 * Toggles pinned state of a widget across all screens if collision-free.
 */
export function togglePinWidget(
  widgets: readonly LayoutEditorWidget[],
  widgetId: string,
  gridCols: number,
  gridRows: number,
  activeScreen = 0,
): LayoutEditorWidget[] {
  const widget = widgets.find((w) => w.widget_id === widgetId);
  if (!widget) return [...widgets];

  if (widget.pinned) {
    const targetScreen = Math.max(0, activeScreen);
    const updated = widgets.map((w) =>
      w.widget_id === widgetId ? { ...w, pinned: false, screen: targetScreen } : w,
    );
    return compactScreens(updated);
  }

  const maxS = getTotalScreens(widgets);
  const targetMask = computeWidgetBitmask(widget.origin, widget.dimensions, gridCols);
  for (let s = 0; s < maxS; s++) {
    const sMask = computeScreenMask(widgets, s, gridCols, gridRows, widgetId);
    if ((sMask & targetMask) !== 0) {
      return [...widgets];
    }
  }

  const updated = widgets.map((w) =>
    w.widget_id === widgetId ? { ...w, pinned: true, screen: 0 } : w,
  );
  return compactScreens(updated);
}
