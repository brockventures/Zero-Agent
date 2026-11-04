package render

import "context"

// RenderWidget executes the HTML template for the given widget instance, returning the rendered HTML fragment.
func (e *Engine) RenderWidget(ctx context.Context, widgetID string) ([]byte, error) {
	return e.RenderWidgetWithOverrides(ctx, widgetID, "", nil)
}

// RenderWidgetForNode renders a widget for one display, identified by its voice node_id.
func (e *Engine) RenderWidgetForNode(ctx context.Context, widgetID, nodeID string) ([]byte, error) {
	return e.RenderWidgetWithOverrides(ctx, widgetID, nodeID, nil)
}
