package provider_test

import (
	"context"
	"errors"
	"testing"

	"github.com/azylman/mirrormere/internal/provider"
)

type dummyProvider struct{ init, shut bool }

func (d *dummyProvider) Init(context.Context, map[string]any, provider.InitOptions) error {
	d.init = true
	return nil
}

func (d *dummyProvider) Fetch(context.Context) (any, error) {
	return map[string]any{"dummy": true}, nil
}

func (d *dummyProvider) Subscribe(context.Context, chan<- provider.WidgetPayload) error { return nil }

func (d *dummyProvider) Shutdown(context.Context) error {
	d.shut = true
	return nil
}

func TestRegistry(t *testing.T) {
	t.Parallel()
	reg := provider.NewRegistry()
	builtins := []string{"spacer", "live-view", "http", "weather-forecast", "calendar-feed", "photo-carousel", "tasks"}
	for _, name := range builtins {
		if p, err := reg.Create(name); err != nil || p == nil {
			t.Fatalf("create %s failed: %v", name, err)
		}
	}
	ctx := context.Background()
	for _, name := range []string{"spacer", "live-view"} {
		p, _ := reg.Create(name)
		if p.Init(ctx, nil, provider.InitOptions{}) != nil || p.Subscribe(ctx, make(chan provider.WidgetPayload, 1)) != nil || p.Shutdown(ctx) != nil {
			t.Fatal("lifecycle failed")
		}
		if data, err := p.Fetch(ctx); err != nil || len(data.(map[string]any)) != 0 {
			t.Fatalf("bad fetch: %v %v", data, err)
		}
	}
	dummy := &dummyProvider{}
	reg.Register("custom-dummy", func() provider.Provider { return dummy })
	if got, err := reg.Create("custom-dummy"); err != nil || got != dummy {
		t.Fatalf("bad custom: %v", err)
	}
	if _, err := reg.Create("unknown"); !errors.Is(err, provider.ErrProviderNotRegistered) {
		t.Fatalf("want ErrProviderNotRegistered: %v", err)
	}
}

func TestRegistry_Builtins(t *testing.T) {
	t.Parallel()
	reg := provider.NewRegistry()
	builtins := []string{"spacer", "live-view", "http", "weather-forecast", "calendar-feed", "photo-carousel", "tasks"}
	for _, name := range builtins {
		p, err := reg.Create(name)
		if err != nil {
			t.Fatalf("Create(%q) failed: %v", name, err)
		}
		if p == nil {
			t.Fatalf("Create(%q) returned nil provider", name)
		}
	}
}

func TestInitOptions_GetSecret(t *testing.T) {
	t.Parallel()
	opts := provider.InitOptions{Secrets: map[string]string{"token": "tok-123", "api_key": "secret-xyz"}}
	tests := []struct{ key, want string }{
		{"token", "tok-123"},
		{"api_key", "secret-xyz"},
		{"unset", ""},
	}
	for _, tc := range tests {
		if got := opts.GetSecret(tc.key); got != tc.want {
			t.Errorf("GetSecret(%q) = %q, want %q", tc.key, got, tc.want)
		}
	}
	if got := (provider.InitOptions{}).GetSecret("anything"); got != "" {
		t.Errorf("want empty, got %q", got)
	}
}
