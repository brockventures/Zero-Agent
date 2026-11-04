import { useEffect, useRef } from 'react';
import { useDisplayStore } from '../../store';

export interface UseInactivityResetProps {
  isInteractive: boolean;
  onReset: () => void;
}

export function useInactivityReset({
  isInteractive,
  onReset,
}: UseInactivityResetProps) {
  const hasPausedStoreRef = useRef(false);
  const inactivityTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onResetRef = useRef(onReset);

  useEffect(() => {
    onResetRef.current = onReset;
  }, [onReset]);

  const clearInactivityTimer = () => {
    if (inactivityTimerRef.current) {
      clearTimeout(inactivityTimerRef.current);
      inactivityTimerRef.current = null;
    }
  };

  const resetInactivityTimer = () => {
    clearInactivityTimer();
    inactivityTimerRef.current = setTimeout(() => {
      onResetRef.current();
      if (hasPausedStoreRef.current) {
        useDisplayStore.getState().resume();
        hasPausedStoreRef.current = false;
      }
    }, 60000);
  };

  const handleUserActivity = () => {
    if (hasPausedStoreRef.current || inactivityTimerRef.current !== null) {
      resetInactivityTimer();
    }
  };

  const clearInactivityTimerRef = useRef(clearInactivityTimer);
  const resetInactivityTimerRef = useRef(resetInactivityTimer);
  useEffect(() => {
    clearInactivityTimerRef.current = clearInactivityTimer;
    resetInactivityTimerRef.current = resetInactivityTimer;
  });

  useEffect(() => {
    if (isInteractive) {
      if (!hasPausedStoreRef.current) {
        useDisplayStore.getState().pause();
        hasPausedStoreRef.current = true;
      }
      resetInactivityTimerRef.current();
    } else {
      clearInactivityTimerRef.current();
      if (hasPausedStoreRef.current) {
        useDisplayStore.getState().resume();
        hasPausedStoreRef.current = false;
      }
    }
  }, [isInteractive]);

  useEffect(() => {
    return () => {
      clearInactivityTimerRef.current();
      if (hasPausedStoreRef.current) {
        useDisplayStore.getState().resume();
        hasPausedStoreRef.current = false;
      }
    };
  }, []);

  return {
    handleUserActivity,
    clearInactivityTimer,
    resetInactivityTimer,
  };
}
