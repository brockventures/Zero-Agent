import { createContext, useContext } from 'react';
import { useDisplayStore } from '../store';

/**
 * Provided by each Screen pane. Widgets pause timers, decoders and animations while
 * `isCurrentPane` is false, so off-screen neighbours stay mounted but quiet.
 */
export interface PaneContextValue {
  isCurrentPane: boolean;
  paneIndex: number;
}

export const PaneContext = createContext<PaneContextValue>({ isCurrentPane: true, paneIndex: 0 });

export function useIsCurrentPane(): boolean {
  const { isCurrentPane } = useContext(PaneContext);
  const isVideoMode = useDisplayStore(
    (s) => (s.video?.mode || (s.video?.primary ? 'video' : 'widgets')) === 'video',
  );
  return isCurrentPane && !isVideoMode;
}


