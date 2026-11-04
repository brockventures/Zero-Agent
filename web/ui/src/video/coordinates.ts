export interface NormalizedCoordinates {
  x: number;
  y: number;
}

export type CoordinateEvent =
  | PointerEvent
  | MouseEvent
  | TouchEvent
  | {
      clientX?: number;
      clientY?: number;
      button?: number;
      touches?: ArrayLike<{ clientX: number; clientY: number }>;
      changedTouches?: ArrayLike<{ clientX: number; clientY: number }>;
      preventDefault?: () => void;
      stopPropagation?: () => void;
    };

/**
 * Maps DOM pointer/touch event coordinates into normalized [0.0, 1.0] video space,
 * compensating for letterboxing/pillarboxing with object-fit: contain.
 */
export function getNormalizedVideoCoordinates(
  e: CoordinateEvent,
  videoEl:
    | HTMLVideoElement
    | HTMLImageElement
    | {
        getBoundingClientRect: () =>
          | DOMRect
          | { left: number; top: number; width: number; height: number };
        videoWidth?: number;
        videoHeight?: number;
        naturalWidth?: number;
        naturalHeight?: number;
      }
    | null
    | undefined,
): NormalizedCoordinates | null {
  if (!videoEl || typeof videoEl.getBoundingClientRect !== 'function') return null;
  const rect = videoEl.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;

  let clientX: number | null = null;
  let clientY: number | null = null;
  if ('touches' in e && e.touches && e.touches.length > 0) {
    clientX = e.touches[0].clientX;
    clientY = e.touches[0].clientY;
  } else if ('changedTouches' in e && e.changedTouches && e.changedTouches.length > 0) {
    clientX = e.changedTouches[0].clientX;
    clientY = e.changedTouches[0].clientY;
  } else if (
    'clientX' in e &&
    typeof e.clientX === 'number' &&
    'clientY' in e &&
    typeof e.clientY === 'number'
  ) {
    clientX = e.clientX;
    clientY = e.clientY;
  }

  if (clientX === null || clientY === null) return null;

  let contentWidth = rect.width;
  let contentHeight = rect.height;
  let contentLeft = rect.left;
  let contentTop = rect.top;

  const vWidth =
    'videoWidth' in videoEl && typeof videoEl.videoWidth === 'number' && videoEl.videoWidth > 0
      ? videoEl.videoWidth
      : 'naturalWidth' in videoEl &&
          typeof videoEl.naturalWidth === 'number' &&
          videoEl.naturalWidth > 0
        ? videoEl.naturalWidth
        : 0;

  const vHeight =
    'videoHeight' in videoEl && typeof videoEl.videoHeight === 'number' && videoEl.videoHeight > 0
      ? videoEl.videoHeight
      : 'naturalHeight' in videoEl &&
          typeof videoEl.naturalHeight === 'number' &&
          videoEl.naturalHeight > 0
        ? videoEl.naturalHeight
        : 0;

  // Compensate for letterboxing or pillarboxing with object-fit: contain
  if (vWidth > 0 && vHeight > 0) {
    const videoRatio = vWidth / vHeight;
    const elementRatio = rect.width / rect.height;

    if (elementRatio > videoRatio) {
      // Pillarboxed: black bars on left and right
      contentWidth = rect.height * videoRatio;
      contentLeft = rect.left + (rect.width - contentWidth) / 2;
    } else {
      // Letterboxed: black bars on top and bottom
      contentHeight = rect.width / videoRatio;
      contentTop = rect.top + (rect.height - contentHeight) / 2;
    }
  }

  if (contentWidth <= 0 || contentHeight <= 0) return null;

  const relX = (clientX - contentLeft) / contentWidth;
  const relY = (clientY - contentTop) / contentHeight;

  // Clamp coordinates to [0.0, 1.0]
  return {
    x: Math.max(0.0, Math.min(1.0, relX)),
    y: Math.max(0.0, Math.min(1.0, relY)),
  };
}
