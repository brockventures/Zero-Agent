import './SpacerWidget.css';
import type { WidgetComponentProps } from '../registry';

/**
 * Spacer widget tile: zero-overhead empty tile for grid padding.
 * Outer .widget-card is owned and styled by WidgetHost.
 * Inner root must NOT set data-widget-id or wrap in another .widget-card.
 */
export function SpacerWidget({ type }: WidgetComponentProps) {
  return <div className="widget-spacer" data-widget-type={type} aria-hidden="true" />;
}
