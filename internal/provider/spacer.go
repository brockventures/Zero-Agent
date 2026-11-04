package provider

import (
	"context"
)

func init() {
	registerBuiltin("spacer", func() Provider {
		return NewSpacerProvider()
	})
}

// SpacerProvider provides the data lifecycle implementation for native spacer tiles.
// Conforms to  Spacer Tiles (zero overhead, empty domain payload).
type SpacerProvider struct{}

// NewSpacerProvider constructs a SpacerProvider.
func NewSpacerProvider() *SpacerProvider {
	return &SpacerProvider{}
}

// Init initializes the spacer provider (no-op).
func (s *SpacerProvider) Init(ctx context.Context, config map[string]any, opts InitOptions) error {
	return nil
}

// Fetch returns the nominal empty domain data for the spacer tile.
func (s *SpacerProvider) Fetch(ctx context.Context) (any, error) {
	return map[string]any{}, nil
}

// Subscribe is a no-op as spacer tiles do not emit dynamic push events.
func (s *SpacerProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown cleans up any resources (no-op).
func (s *SpacerProvider) Shutdown(ctx context.Context) error {
	return nil
}
