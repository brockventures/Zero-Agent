package provider

import (
	"strings"
	"testing"
)

func TestCalDAV_InternalHelpers(t *testing.T) {
	t.Parallel()
	if c := NewCalDAVClient(nil, nil); c == nil || c.client == nil || c.logger == nil {
		t.Fatal("expected non-nil client and logger defaults")
	}
	for _, tc := range []struct{ in, want string }{
		{"https://example.com/dav", "https://example.com/dav/"},
		{"https://example.com/dav/", "https://example.com/dav/"},
		{"https://example.com/dav/file.ics", "https://example.com/dav/file.ics"},
		{"http:// invalid url", "http:// invalid url"},
	} {
		if got := ensureTrailingSlash(tc.in); got != tc.want {
			t.Errorf("ensureTrailingSlash(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
	for _, tc := range []struct{ in, wantSub, wantExact string }{
		{"https://user:secret@example.com/dav", "xxxxx", ""}, {"http:// invalid url", "", "http:// invalid url"},
	} {
		if got := redactURL(tc.in); (tc.wantSub != "" && !strings.Contains(got, tc.wantSub)) || (tc.wantExact != "" && got != tc.wantExact) {
			t.Errorf("redactURL(%q) = %q", tc.in, got)
		}
	}
	for _, tc := range []struct{ base, ref, want string }{
		{"https://example.com/dav/", "/cal/", "https://example.com/cal/"},
		{"https://example.com/dav/", "cal/", "https://example.com/dav/cal/"},
		{"http:// invalid base", "cal/", "cal/"},
		{"https://example.com/dav/", "http:// invalid ref", "http:// invalid ref"},
	} {
		if got := resolveHRef(tc.base, tc.ref); got != tc.want {
			t.Errorf("resolveHRef(%q, %q) = %q, want %q", tc.base, tc.ref, got, tc.want)
		}
	}
}
