/**
 * Structural equality for JSON-shaped values (widget data, view models).
 * Used to keep store references stable when a stream or fetch repeats data.
 */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;

  if (Array.isArray(a)) {
    const other = b as unknown[];
    if (a.length !== other.length) return false;
    return a.every((value, index) => deepEqual(value, other[index]));
  }

  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) return false;
  return keys.every(
    (key) => Object.prototype.hasOwnProperty.call(right, key) && deepEqual(left[key], right[key]),
  );
}
