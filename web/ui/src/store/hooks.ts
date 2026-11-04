import { useShallow } from 'zustand/react/shallow';
import { useDisplayStore } from './index';
import type {
  HeaderSlice,
  SystemSlice,
  VideoSlice,
  WidgetEntry,
} from './types';

// Each hook selects one narrow piece of state. A widget.update for one id
// changes only that id's entry, so only useWidget(thatId) subscribers re-render.
// Slice hooks use shallow equality so an unchanged-field update does not re-render.

const EMPTY_WIDGET: WidgetEntry = Object.freeze({ state: '', data: undefined, timestamp: '' });

/** `{meta, state, data, viewModel, timestamp}` for one widget instance. */
export function useWidget(id: string): WidgetEntry {
  return useDisplayStore((s) => s.widgets[id] ?? EMPTY_WIDGET);
}

export function useHeader(): HeaderSlice {
  return useDisplayStore(useShallow((s) => s.header));
}

export function useVideo(): VideoSlice {
  return useDisplayStore(useShallow((s) => s.video));
}

export function useSystem(): SystemSlice {
  return useDisplayStore(useShallow((s) => s.system));
}

export function useLayoutEditorOpen(): boolean {
  return useDisplayStore((s) => s.layoutEditorOpen);
}

