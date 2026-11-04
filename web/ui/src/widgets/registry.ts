import type { ComponentType } from 'react';
import { LegacyWidget } from './LegacyWidget';

/** Props every widget component receives from WidgetHost. */
export interface WidgetComponentProps {
  id: string;
  type: string;
}

const modules = import.meta.glob<Record<string, ComponentType<WidgetComponentProps>>>('./*/*Widget.tsx', { eager: true });

/**
 * Widget type -> component. Automatically discovered from widget files.
 * A type missing from the map renders through LEGACY_WIDGET_FALLBACK.
 */
export const WIDGET_COMPONENTS: Readonly<Record<string, ComponentType<WidgetComponentProps>>> = Object.fromEntries(
  Object.entries(modules).map(([path, mod]) => {
    const [, type, file] = path.split('/');
    return [type, mod[file.slice(0, -4)]];
  })
);

/**
 * Renders any type with no registered component: a custom or not-yet-ported package, served as
 * its legacy HTML fragment.
 */
export const LEGACY_WIDGET_FALLBACK: ComponentType<WidgetComponentProps> = LegacyWidget;

export function widgetComponentFor(type: string | undefined): ComponentType<WidgetComponentProps> | undefined {
  if (type === undefined || !Object.prototype.hasOwnProperty.call(WIDGET_COMPONENTS, type)) return undefined;
  return WIDGET_COMPONENTS[type];
}
