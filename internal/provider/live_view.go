package provider

func init() {
	registerBuiltin("live-view", func() Provider {
		return NewLiveViewProvider()
	})
}

// LiveViewProvider provides the lifecycle implementation for client-side live-view video widgets.
// Like SpacerProvider, live-view video streams connect client-side over WebRTC/MJPEG/HLS
// with zero backend polling overhead.
type LiveViewProvider = SpacerProvider

// NewLiveViewProvider constructs a LiveViewProvider.
func NewLiveViewProvider() *LiveViewProvider {
	return NewSpacerProvider()
}
