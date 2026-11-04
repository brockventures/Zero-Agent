import { useEffect, useRef, useState } from 'react';
import { addDays, addMonths, parseISODate, formatISODate as toISODate } from './dateUtils';
import type { CalendarGridViewModel, FamilyViewModel, ViewMode } from './types';

export interface UseCalendarRangeDataProps {
  id: string;
  baseDate: Date;
  baseWeekStart: Date;
  weekStep: number;
  activeView: ViewMode;
  setActiveView: React.Dispatch<React.SetStateAction<ViewMode>>;
  entryTimestamp?: string;
  entryViewModel?: Record<string, unknown>;
  cachedRangeStartConfig?: string;
  cachedRangeEndConfig?: string;
  dataCachedRangeStart?: string;
  dataCachedRangeEnd?: string;
  onActivity?: () => void;
}

export function useCalendarRangeData({
  id,
  baseDate,
  baseWeekStart,
  weekStep,
  activeView,
  setActiveView,
  entryTimestamp,
  entryViewModel,
  cachedRangeStartConfig,
  cachedRangeEndConfig,
  dataCachedRangeStart,
  dataCachedRangeEnd,
  onActivity,
}: UseCalendarRangeDataProps) {
  const [fetchedViewModel, setFetchedViewModel] = useState<Record<string, unknown> | undefined>(
    undefined,
  );

  const [pagedDate, setPagedDate] = useState<Date | null>(null);
  const [pagedWeekStart, setPagedWeekStart] = useState<Date | null>(null);

  const currentDate = pagedDate ?? baseDate;
  const currentWeekStart = pagedWeekStart ?? baseWeekStart;

  const pageOffset = (() => {
    if (activeView === 'day') {
      return Math.round((currentDate.getTime() - baseDate.getTime()) / 86400000);
    }
    if (activeView === 'month') {
      return (currentDate.getFullYear() - baseDate.getFullYear()) * 12 + (currentDate.getMonth() - baseDate.getMonth());
    }
    const diffMs = currentWeekStart.getTime() - baseWeekStart.getTime();
    if (diffMs === 0) return 0;
    const step = Math.round(diffMs / (weekStep * 86400000));
    return step === 0 ? (diffMs > 0 ? 1 : -1) : step;
  })();

  const isAtBaseDate = pageOffset === 0;

  const [prevTimestamp, setPrevTimestamp] = useState(entryTimestamp);
  if (entryTimestamp !== prevTimestamp) {
    setPrevTimestamp(entryTimestamp);
    if (isAtBaseDate) {
      setFetchedViewModel(undefined);
    }
  }

  const currentViewModel = fetchedViewModel ?? entryViewModel;
  const setCurrentViewModel = (vm: Record<string, unknown> | undefined) => {
    setFetchedViewModel(vm === entryViewModel ? undefined : vm);
  };

  const lruCacheRef = useRef<Map<string, CalendarGridViewModel>>(new Map());
  const abortControllerRef = useRef<AbortController | null>(null);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showSyncIndicator = (msg: string) => {
    if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
    setSyncMessage(msg);
    syncTimerRef.current = setTimeout(() => {
      setSyncMessage(null);
      syncTimerRef.current = null;
    }, 2500);
  };

  useEffect(() => {
    lruCacheRef.current.clear();
  }, [entryTimestamp]);

  const resolveCachedRange = () => {
    const vm = currentViewModel as Record<string, unknown> | undefined;
    let start = cachedRangeStartConfig || (vm?.cached_range_start as string) || dataCachedRangeStart;
    let end = cachedRangeEndConfig || (vm?.cached_range_end as string) || dataCachedRangeEnd;

    if (!start) start = toISODate(addDays(baseDate, -90));
    if (!end) end = toISODate(addDays(baseDate, 180));
    return { start, end };
  };

  const abortInFlight = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  };

  const loadDateView = async (
    targetDate: Date,
    targetView: ViewMode,
    rollbackFn: () => void,
    syncWeekStart?: boolean
  ) => {
    abortInFlight();

    const dateStr = toISODate(targetDate);
    const viewStr = targetView;
    const cacheKey = `${dateStr}:${viewStr}`;

    const cache = lruCacheRef.current;
    if (cache.has(cacheKey)) {
      const cachedVM = cache.get(cacheKey)!;
      cache.delete(cacheKey);
      cache.set(cacheKey, cachedVM);
      setCurrentViewModel(cachedVM as Record<string, unknown>);
      if (syncWeekStart && cachedVM.week?.range_start) {
        const parsedWeekStart = parseISODate(cachedVM.week.range_start);
        setPagedWeekStart(parsedWeekStart);
        if (targetView === 'week') setPagedDate(parsedWeekStart);
      }
      return;
    }

    const ac = new AbortController();
    abortControllerRef.current = ac;

    try {
      let url = `api/widgets/${encodeURIComponent(id)}?date=${encodeURIComponent(dateStr)}&view=${encodeURIComponent(viewStr)}`;
      if (typeof window !== 'undefined' && window.location?.search) {
        const node = new URLSearchParams(window.location.search).get('node');
        if (node) url += `&node=${encodeURIComponent(node)}`;
      }

      const res = await fetch(url, { signal: ac.signal });
      if (ac.signal.aborted) return;
      if (!res.ok) throw new Error(`Fetch failed with status ${res.status}`);

      const body = await res.json();
      if (ac.signal.aborted) return;
      const vm = (body.view_model ?? body) as CalendarGridViewModel;

      if (cache.size >= 20) {
        const oldestKey = cache.keys().next().value;
        if (oldestKey !== undefined) cache.delete(oldestKey);
      }
      cache.set(cacheKey, vm);
      setCurrentViewModel(vm as Record<string, unknown>);
      if (syncWeekStart && vm.week?.range_start) {
        const parsedWeekStart = parseISODate(vm.week.range_start);
        setPagedWeekStart(parsedWeekStart);
        if (targetView === 'week') setPagedDate(parsedWeekStart);
      }
    } catch (err: unknown) {
      if (ac.signal.aborted || (err instanceof Error && err.name === 'AbortError')) return;
      showSyncIndicator('Sync failed');
      rollbackFn();
    } finally {
      if (abortControllerRef.current === ac) abortControllerRef.current = null;
    }
  };

  const stepDate = (direction: 1 | -1) => {
    onActivity?.();
    const cached = resolveCachedRange();
    const prevPagedDate = pagedDate;
    const prevPagedWeekStart = pagedWeekStart;
    const rollbackFn = () => {
      setPagedDate(prevPagedDate);
      setPagedWeekStart(prevPagedWeekStart);
    };

    if (activeView === 'day') {
      const target = addDays(currentDate, direction);
      const targetStr = toISODate(target);
      if (direction > 0 ? targetStr > cached.end : targetStr < cached.start) {
        showSyncIndicator('Not synced yet');
        return;
      }
      if (targetStr === toISODate(baseDate) && entryViewModel) {
        abortInFlight();
        setPagedDate(null);
        setPagedWeekStart(null);
        setCurrentViewModel(entryViewModel);
        return;
      }
      setPagedDate(target);
      loadDateView(target, 'day', rollbackFn);
    } else if (activeView === 'week') {
      const targetStart = addDays(currentWeekStart, direction * weekStep);
      const checkStr = direction > 0 ? toISODate(targetStart) : toISODate(addDays(targetStart, weekStep - 1));
      if (direction > 0 ? checkStr > cached.end : checkStr < cached.start) {
        showSyncIndicator('Not synced yet');
        return;
      }
      if (toISODate(targetStart) === toISODate(baseWeekStart) && entryViewModel) {
        abortInFlight();
        setPagedDate(null);
        setPagedWeekStart(null);
        setCurrentViewModel(entryViewModel);
        return;
      }
      setPagedWeekStart(targetStart);
      setPagedDate(targetStart);
      loadDateView(targetStart, 'week', rollbackFn);
    } else if (activeView === 'month') {
      const targetMonth = addMonths(currentDate, direction);
      const targetMonthStr = toISODate(targetMonth).slice(0, 7);
      if (direction > 0 ? targetMonthStr > cached.end.slice(0, 7) : targetMonthStr < cached.start.slice(0, 7)) {
        showSyncIndicator('Not synced yet');
        return;
      }
      if (
        targetMonth.getFullYear() === baseDate.getFullYear() &&
        targetMonth.getMonth() === baseDate.getMonth() &&
        entryViewModel
      ) {
        abortInFlight();
        setPagedDate(null);
        setPagedWeekStart(null);
        setCurrentViewModel(entryViewModel);
        return;
      }
      setPagedDate(targetMonth);
      loadDateView(targetMonth, 'month', rollbackFn);
    }
  };

  const pageNext = () => stepDate(1);
  const pagePrev = () => stepDate(-1);

  const resetDate = () => {
    setPagedDate(null);
    setPagedWeekStart(null);
    setSyncMessage(null);
    abortInFlight();
    if (entryViewModel) {
      setCurrentViewModel(entryViewModel);
    } else {
      loadDateView(baseDate, activeView, () => {});
    }
    onActivity?.();
  };

  const handleViewSwitch = (targetView: ViewMode) => {
    if (targetView === activeView) return;
    const prevActiveView = activeView;
    setActiveView(targetView);
    onActivity?.();

    const currentISO = toISODate(currentDate);
    const baseDateISO = toISODate(baseDate);
    const baseWeekStartISO = toISODate(baseWeekStart);
    const baseWeekEndISO = toISODate(addDays(baseWeekStart, weekStep - 1));

    const isTargetAtBase =
      targetView === 'day'
        ? currentISO === baseDateISO
        : targetView === 'week'
        ? currentISO >= baseWeekStartISO && currentISO <= baseWeekEndISO
        : currentDate.getFullYear() === baseDate.getFullYear() &&
          currentDate.getMonth() === baseDate.getMonth();

    if (isTargetAtBase) {
      const baseVM = (entryViewModel ?? (isAtBaseDate ? currentViewModel : undefined)) as
        | Record<string, unknown>
        | undefined;
      const hasMonth = Boolean(baseVM?.month);
      const hasWeek = Boolean(baseVM?.week);
      if (
        (targetView === 'month' && hasMonth) ||
        ((targetView === 'week' || targetView === 'day') && hasWeek)
      ) {
        abortInFlight();
        setPagedDate(null);
        setPagedWeekStart(null);
        if (baseVM) setCurrentViewModel(baseVM);
        return;
      }
    }

    const prevPagedDate = pagedDate;
    const prevPagedWeekStart = pagedWeekStart;
    const fetchAnchorDate = isTargetAtBase ? baseDate : currentDate;
    if (isTargetAtBase) {
      setPagedDate(null);
      setPagedWeekStart(null);
    } else if (targetView === 'week') {
      setPagedWeekStart(currentDate);
    }
    loadDateView(
      fetchAnchorDate,
      targetView,
      () => {
        setActiveView(prevActiveView);
        setPagedDate(prevPagedDate);
        setPagedWeekStart(prevPagedWeekStart);
      },
      !isTargetAtBase
    );
  };

  const resetToBaseline = (defaultView: ViewMode) => {
    setActiveView(defaultView);
    setPagedDate(null);
    setPagedWeekStart(null);
    setSyncMessage(null);
    abortInFlight();
    if (entryViewModel) {
      setCurrentViewModel(entryViewModel);
    } else {
      loadDateView(baseDate, defaultView, () => {});
    }
  };

  const selectDayView = (isoDate: string) => {
    if (!isoDate || !/^\d{4}-\d{2}-\d{2}/.test(isoDate)) return;
    const prevActiveView = activeView;
    const prevPagedDate = pagedDate;
    const prevPagedWeekStart = pagedWeekStart;

    const targetDate = parseISODate(isoDate);
    onActivity?.();
    setActiveView('day');
    setPagedDate(targetDate);

    // Abort any in-flight paging fetch and synchronize pagedWeekStart so subsequent
    // view switching and week paging step from the active week rather than a stale paged offset.
    const currentWeekVM = currentViewModel?.week as FamilyViewModel | undefined;
    const currentWeekDays = currentWeekVM?.days;
    if (currentWeekDays?.some((d) => d.date === isoDate)) {
      abortInFlight();
      if (currentWeekVM?.range_start) {
        if (toISODate(baseWeekStart) === currentWeekVM.range_start) {
          setPagedWeekStart(null);
        } else {
          setPagedWeekStart(parseISODate(currentWeekVM.range_start));
        }
      }
      return;
    }

    const entryWeekVM = entryViewModel?.week as FamilyViewModel | undefined;
    const entryWeekDays = entryWeekVM?.days;
    if (entryWeekDays?.some((d) => d.date === isoDate)) {
      abortInFlight();
      if (entryViewModel) {
        setCurrentViewModel(entryViewModel);
      }
      setPagedWeekStart(null);
      return;
    }

    loadDateView(
      targetDate,
      'day',
      () => {
        setActiveView(prevActiveView);
        setPagedDate(prevPagedDate);
        setPagedWeekStart(prevPagedWeekStart);
      },
      true
    );
  };

  useEffect(() => {
    return () => {
      if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
      abortInFlight();
    };
  }, []);

  return {
    currentViewModel,
    setCurrentViewModel,
    pagedDate,
    pagedWeekStart,
    currentDate,
    currentWeekStart,
    pageOffset,
    isAtBaseDate,
    syncMessage,
    showSyncIndicator,
    loadDateView,
    abortInFlight,
    resolveCachedRange,
    pageNext,
    pagePrev,
    resetDate,
    handleViewSwitch,
    resetToBaseline,
    selectDayView,
  };
}
