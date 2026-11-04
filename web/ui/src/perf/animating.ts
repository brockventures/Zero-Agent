/**
 * Marks the document while any animation runs so the perf.js frame sampler (#766)
 * records frame timing. Ref-counted: nested or overlapping animations keep the
 * flag set until the last one ends. Every animated component calls these around
 * its animation.
 */
let active = 0;

function sync(): void {
  if (typeof document === 'undefined' || !document.body) return;
  document.body.dataset.animating = active > 0 ? '1' : '0';
}

export function beginAnimation(): void {
  active++;
  sync();
}

export function endAnimation(): void {
  if (active > 0) active--;
  sync();
}
