package render_test

import (
	"html/template"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/provider"
	"github.com/azylman/mirrormere/internal/render"
	"github.com/azylman/mirrormere/internal/tasks"
)

func calEv(title, start, end string, allDay bool) provider.CalendarEvent {
	return provider.CalendarEvent{Title: title, Start: start, End: end, AllDay: allDay}
}

func TestParseTime(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 9, 25, 20, 0, 0, 0, time.UTC)

	tests := []struct {
		name      string
		val       any
		wantValid bool
	}{
		{name: "time.Time", val: now, wantValid: true},
		{name: "*time.Time non-nil", val: &now, wantValid: true},
		{name: "*time.Time nil", val: (*time.Time)(nil), wantValid: false},
		{name: "RFC3339 string", val: "2026-09-25T20:00:00Z", wantValid: true},
		{name: "RFC3339Nano string", val: "2026-09-25T20:00:00.123456789Z", wantValid: true},
		{name: "DateTime string", val: "2026-09-25 20:00:00", wantValid: true},
		{name: "DateOnly string", val: "2026-09-25", wantValid: true},
		{name: "TimeOnly string", val: "20:00:00", wantValid: true},
		{name: "ShortTime string", val: "20:00", wantValid: true},
		{name: "OpenMeteo short ISO string", val: "2026-09-26T15:00", wantValid: true},
		{name: "OpenMeteo space short ISO string", val: "2026-09-26 15:00", wantValid: true},
		{name: "12h time string", val: "8:00 PM", wantValid: true},
		{name: "0-padded 12h time", val: "08:00 PM", wantValid: true},
		{name: "int64 unix", val: int64(1758830400), wantValid: true},
		{name: "int unix", val: int(1758830400), wantValid: true},
		{name: "float64 unix", val: float64(1758830400), wantValid: true},
		{name: "invalid string", val: "not-a-date", wantValid: false},
		{name: "unsupported type bool", val: true, wantValid: false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if _, ok := render.ParseTime(tc.val); ok != tc.wantValid {
				t.Errorf("expected ok=%v, got %v", tc.wantValid, ok)
			}
		})
	}
}

func TestFormatDate(t *testing.T) {
	t.Parallel()
	target := time.Date(2026, 9, 25, 14, 30, 0, 0, time.UTC)
	laLoc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed to load America/Los_Angeles: %v", err)
	}
	eventUTC := "2026-10-12T03:30:00Z"

	tests := []struct {
		name string
		loc  *time.Location
		args []any
		want string
	}{
		{name: "no args", args: []any{}, want: ""},
		{name: "single arg time.Time", args: []any{target}, want: "2026-09-25"},
		{name: "single arg string", args: []any{"2026-09-25T14:30:00Z"}, want: "2026-09-25"},
		{name: "single arg invalid", args: []any{"not-a-date"}, want: "not-a-date"},
		{name: "layout and time", args: []any{"Jan 02, 2006", target}, want: "Sep 25, 2026"},
		{name: "empty layout defaults", args: []any{"", target}, want: "2026-09-25"},
		{name: "default layout keyword", args: []any{"default", target}, want: "2026-09-25"},
		{name: "custom layout with unparseable value", args: []any{"2006-01-02", "bad-date"}, want: "bad-date"},
		{name: "timezone conversion date", loc: laLoc, args: []any{"2006-01-02", eventUTC}, want: "2026-10-11"},
		{name: "timezone conversion layout Jan 2", loc: laLoc, args: []any{"Jan 2", eventUTC}, want: "Oct 11"},
		{name: "timezone conversion date-only preserved", loc: laLoc, args: []any{"2006-01-02", "2026-10-12"}, want: "2026-10-12"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := render.FormatDateInLocation(tc.loc, tc.args...); got != tc.want {
				t.Errorf("FormatDateInLocation(%v) = %q, want %q", tc.args, got, tc.want)
			}
		})
	}
}

func TestFormatTime(t *testing.T) {
	t.Parallel()
	target := time.Date(2026, 9, 25, 14, 30, 0, 0, time.UTC)
	laLoc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed to load America/Los_Angeles: %v", err)
	}
	eventUTC := "2026-10-12T03:30:00Z"

	tests := []struct {
		name string
		loc  *time.Location
		args []any
		want string
	}{
		{name: "no args", args: []any{}, want: ""},
		{name: "single arg time.Time", args: []any{target}, want: "14:30"},
		{name: "single arg string", args: []any{"2026-09-25T14:30:00Z"}, want: "14:30"},
		{name: "single arg invalid", args: []any{"bad-time"}, want: "bad-time"},
		{name: "12h layout and time", args: []any{"3:04 PM", target}, want: "2:30 PM"},
		{name: "empty layout defaults", args: []any{"", target}, want: "14:30"},
		{name: "default layout keyword", args: []any{"default", target}, want: "14:30"},
		{name: "custom layout with unparseable value", args: []any{"15:04", "bad-time"}, want: "bad-time"},
		{name: "date-only returns empty", args: []any{"15:04", "2026-10-12"}, want: ""},
		{name: "timezone conversion 3:04", loc: laLoc, args: []any{"3:04", eventUTC}, want: "8:30"},
		{name: "timezone conversion PM", loc: laLoc, args: []any{"PM", eventUTC}, want: "PM"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := render.FormatTimeInLocation(tc.loc, tc.args...); got != tc.want {
				t.Errorf("FormatTimeInLocation(%v) = %q, want %q", tc.args, got, tc.want)
			}
		})
	}
}

func TestRelDate(t *testing.T) {
	t.Parallel()
	ref := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
	laLoc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Fatalf("failed to load America/Los_Angeles: %v", err)
	}
	eventUTC := "2026-10-12T03:30:00Z"
	satNow := time.Date(2026, 10, 10, 19, 0, 0, 0, laLoc)
	sunNow := time.Date(2026, 10, 11, 14, 0, 0, 0, laLoc)

	tests := []struct {
		name    string
		refTime time.Time
		args    []any
		want    string
	}{
		{name: "no args", refTime: ref, args: []any{}, want: ""},
		{name: "unparseable", refTime: ref, args: []any{"invalid"}, want: "invalid"},
		{name: "today same hour", refTime: ref, args: []any{ref}, want: "Today"},
		{name: "today earlier hour", refTime: ref, args: []any{time.Date(2026, 9, 25, 8, 0, 0, 0, time.UTC)}, want: "Today"},
		{name: "tomorrow", refTime: ref, args: []any{time.Date(2026, 9, 26, 9, 0, 0, 0, time.UTC)}, want: "Tomorrow"},
		{name: "yesterday", refTime: ref, args: []any{time.Date(2026, 9, 24, 23, 0, 0, 0, time.UTC)}, want: "Yesterday"},
		{name: "in 3 days", refTime: ref, args: []any{time.Date(2026, 9, 28, 10, 0, 0, 0, time.UTC)}, want: "in 3 days"},
		{name: "4 days ago", refTime: ref, args: []any{time.Date(2026, 9, 21, 10, 0, 0, 0, time.UTC)}, want: "4 days ago"},
		{name: "custom reference parameter", refTime: ref, args: []any{time.Date(2026, 10, 2, 10, 0, 0, 0, time.UTC), "2026-10-01T00:00:00Z"}, want: "Tomorrow"},
		{name: "date-only today", refTime: ref, args: []any{"2026-09-25"}, want: "Today"},
		{name: "timezone conversion tomorrow", refTime: satNow, args: []any{eventUTC}, want: "Tomorrow"},
		{name: "timezone conversion today", refTime: sunNow, args: []any{eventUTC}, want: "Today"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := render.RelDate(tc.refTime, tc.args...); got != tc.want {
				t.Errorf("RelDate(%v) = %q, want %q", tc.args, got, tc.want)
			}
		})
	}
}

func TestJSONHelpers(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name     string
		val      any
		wantErr  bool
		wantHTML string
		wantJS   string
	}{
		{name: "valid map", val: map[string]any{"key": "value", "count": 42}, wantHTML: `"key":"value"`, wantJS: `"count":42`},
		{name: "unserializable channel", val: make(chan int), wantErr: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			htmlOut, htmlErr := render.JSONHelper(tc.val)
			jsOut, jsErr := render.JSONJSHelper(tc.val)
			if tc.wantErr {
				if htmlErr == nil || jsErr == nil {
					t.Fatalf("expected error for %s, got htmlErr=%v, jsErr=%v", tc.name, htmlErr, jsErr)
				}
				return
			}
			if htmlErr != nil || jsErr != nil || !strings.Contains(string(htmlOut), tc.wantHTML) || !strings.Contains(string(jsOut), tc.wantJS) {
				t.Errorf("JSONHelper/JSONJSHelper mismatch: html=(%q, %v), js=(%q, %v)", htmlOut, htmlErr, jsOut, jsErr)
			}
		})
	}
}

func TestStandardFuncMap(t *testing.T) {
	t.Parallel()
	fixedNow := time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)
	funcs := render.StandardFuncMap(func() time.Time { return fixedNow })
	familyCfg := map[string]any{
		"members":   []any{map[string]any{"name": "Alex", "color": "#E07A5F", "calendars": []any{"alex-personal"}}},
		"calendars": []any{map[string]any{"name": "alex-personal"}},
		"date":      "2026-09-27",
	}

	tests := []struct {
		name         string
		funcMap      template.FuncMap
		tmpl         string
		data         any
		wantContains []string
	}{
		{name: "formatDate", tmpl: `{{formatDate "2006" "2026-09-25"}}`, wantContains: []string{"2026"}},
		{name: "formatTime", tmpl: `{{formatTime "15:04" "2026-09-25T14:30:00Z"}}`, wantContains: []string{"14:30"}},
		{name: "json", tmpl: `{{json .}}`, data: map[string]string{"foo": "bar"}, wantContains: []string{`"foo":"bar"`}},
		{name: "jsonJS", tmpl: `<script>const data = {{jsonJS .}};</script>`, data: map[string]int{"count": 42}, wantContains: []string{`"count":42`}},
		{name: "weatherIcon", tmpl: `{{weatherIcon "weather-partly-cloudy" 24}}`, wantContains: []string{`width="24"`, `mm-icon-weather-partly-cloudy`}},
		{
			name: "weatherHourlyGraph", tmpl: `{{weatherHourlyGraph .Hourly .ID}}`,
			data:         map[string]any{"Hourly": []testHourlyItem{{Time: "2026-09-25T12:00", Temp: 68.0, PrecipProb: 0, Icon: "weather-sunny"}, {Time: "2026-09-25T13:00", Temp: 71.0, PrecipProb: 20, Icon: "weather-partly-cloudy"}}, "ID": "widget-123"},
			wantContains: []string{`class="weather-hourly-graph"`, `id="mm-weather-grad-widget-123"`, `68°`, `71°`},
		},
		{name: "todayPrecipProb", tmpl: `{{todayPrecipProb .}}%`, data: provider.WeatherSnapshot{Daily: []provider.WeatherDaily{{PrecipProbMax: 42}}}, wantContains: []string{"42%"}},
		{
			name: "calendarFeedDayGroups", tmpl: `{{ range (calendarFeedDayGroups .) }}{{ .Date }}:{{ len .Events }};{{ end }}`,
			data:         provider.CalendarSnapshot{Events: []provider.CalendarEvent{calEv("E1", "2026-09-25T14:00:00Z", "2026-09-25T15:00:00Z", false), calEv("E2", "2026-09-25T16:00:00Z", "2026-09-25T17:00:00Z", false)}},
			wantContains: []string{"2026-09-25:2;"},
		},
		{
			name: "calendarFeedActiveCalendars and alias", tmpl: `{{ $c := calendarFeedActiveCalendars . }}{{ len $c }}:{{ range (activeCalendars .) }}{{ .name }};{{ end }}`,
			data:         map[string]any{"calendars": []any{map[string]any{"name": "Cal 1", "enabled": true}, map[string]any{"name": "Cal 2", "enabled": false}}},
			wantContains: []string{"1:Cal 1;"},
		},
		{name: "tasksStats", tmpl: `{{ $s := tasksStats . }}{{ $s.Summary }}`, data: []map[string]any{{"done": true}, {"done": false}}, wantContains: []string{"1 to do"}},
		{name: "stringOr and math", tmpl: `{{stringOr . "missing" "fallback"}}:{{sub 5 3}}`, data: map[string]any{"exists": "value"}, wantContains: []string{"fallback:2"}},
		{
			name:         "family view helpers and combined data",
			tmpl:         `{{ $w := calendarGridView .Data .Cfg .Dims }}{{ $m := monthView .Data .Cfg .Dims }}{{ $c := calendarGridData $w $m .Cfg }}{{ $w.RangeStart }}:{{ $m.MonthName }}:{{ index $c "range_start" }}:{{ isMonthView .MonthCfg }}:{{ isMonthView .Cfg }}`,
			data:         map[string]any{"Data": provider.CalendarSnapshot{}, "Cfg": familyCfg, "MonthCfg": map[string]any{"default_view": "month"}, "Dims": domain.NewDimension(6, 2)},
			wantContains: []string{"2026-09-27:September:2026-09-27:true:false"},
		},
		{name: "nil nowFunc fallback", funcMap: render.StandardFuncMap(nil), tmpl: `{{formatDate "2006" "2026-09-25"}}`, wantContains: []string{"2026"}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			fm := tc.funcMap
			if fm == nil {
				fm = funcs
			}
			tmpl, err := template.New(tc.name).Funcs(fm).Parse(tc.tmpl)
			if err != nil {
				t.Fatalf("failed to parse template: %v", err)
			}
			var sb strings.Builder
			if err := tmpl.Execute(&sb, tc.data); err != nil {
				t.Fatalf("failed to execute template: %v", err)
			}
			out := sb.String()
			for _, sub := range tc.wantContains {
				if !strings.Contains(out, sub) {
					t.Errorf("template output %q does not contain %q", out, sub)
				}
			}
		})
	}
}

func TestWeatherIcon(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name         string
		args         []any
		wantContains []string
	}{
		{name: "weather-sunny", args: []any{"weather-sunny"}, wantContains: []string{`<svg`, `</svg>`, `width="20"`, `height="20"`, `mm-icon-weather-sunny`}},
		{name: "weather-clear-night", args: []any{"weather-clear-night"}, wantContains: []string{`mm-icon-weather-clear-night`}},
		{name: "weather-partly-cloudy", args: []any{"weather-partly-cloudy"}, wantContains: []string{`mm-icon-weather-partly-cloudy`}},
		{name: "weather-night-partly-cloudy", args: []any{"weather-night-partly-cloudy"}, wantContains: []string{`mm-icon-weather-night-partly-cloudy`}},
		{name: "weather-cloudy", args: []any{"weather-cloudy"}, wantContains: []string{`mm-icon-weather-cloudy`}},
		{name: "weather-fog", args: []any{"weather-fog"}, wantContains: []string{`mm-icon-weather-fog`}},
		{name: "weather-rainy", args: []any{"weather-rainy"}, wantContains: []string{`mm-icon-weather-rainy`}},
		{name: "weather-pouring", args: []any{"weather-pouring"}, wantContains: []string{`mm-icon-weather-pouring`}},
		{name: "weather-snowy", args: []any{"weather-snowy"}, wantContains: []string{`mm-icon-weather-snowy`}},
		{name: "weather-snowy-rainy", args: []any{"weather-snowy-rainy"}, wantContains: []string{`mm-icon-weather-snowy-rainy`}},
		{name: "weather-lightning", args: []any{"weather-lightning"}, wantContains: []string{`mm-icon-weather-lightning`}},
		{name: "weather-lightning-rainy", args: []any{"weather-lightning-rainy"}, wantContains: []string{`mm-icon-weather-lightning-rainy`}},
		{name: "unknown-token", args: []any{"unknown-token"}, wantContains: []string{`mm-icon-unknown-token`}},
		{name: "no args", args: []any{}, wantContains: []string{`width="20"`}},
		{name: "nil arg", args: []any{nil}, wantContains: []string{`width="20"`}},
		{name: "custom int", args: []any{"weather-sunny", 32}, wantContains: []string{`width="32"`}},
		{name: "custom int64", args: []any{"weather-sunny", int64(28)}, wantContains: []string{`width="28"`}},
		{name: "custom float64", args: []any{"weather-sunny", float64(24)}, wantContains: []string{`width="24"`}},
		{name: "custom string", args: []any{"weather-sunny", "36"}, wantContains: []string{`width="36"`}},
		{name: "invalid string", args: []any{"weather-sunny", "bad-size"}, wantContains: []string{`width="20"`}},
		{name: "clamp below min", args: []any{"weather-sunny", 5}, wantContains: []string{`width="12"`}},
		{name: "clamp above max", args: []any{"weather-sunny", 120}, wantContains: []string{`width="64"`}},
		{name: "negative int", args: []any{"weather-sunny", -10}, wantContains: []string{`width="20"`}},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			svg := string(render.WeatherIcon(tc.args...))
			for _, sub := range tc.wantContains {
				if !strings.Contains(svg, sub) {
					t.Errorf("WeatherIcon(%v) missing %q in %s", tc.args, sub, svg)
				}
			}
		})
	}
}

type testHourlyItem struct {
	Time       string
	Temp       float64
	PrecipProb int
	Icon       string
}

func TestWeatherHourlyGraph(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name            string
		args            []any
		wantEmpty       bool
		wantContains    []string
		wantNotContains []string
	}{
		{name: "no args", args: []any{}, wantEmpty: true},
		{name: "nil arg", args: []any{nil}, wantEmpty: true},
		{name: "unsupported type string", args: []any{"invalid"}, wantEmpty: true},
		{name: "unsupported type int", args: []any{123}, wantEmpty: true},
		{name: "empty slice of any", args: []any{[]any{}}, wantEmpty: true},
		{name: "empty typed slice", args: []any{[]testHourlyItem{}}, wantEmpty: true},
		{name: "slice with all nil items", args: []any{[]any{nil, nil}}, wantEmpty: true},
		{
			name:            "single item",
			args:            []any{[]testHourlyItem{{Time: "2026-09-26T14:00", Temp: 72.0, PrecipProb: 0, Icon: "weather-sunny"}}},
			wantContains:    []string{"72°", "Now"},
			wantNotContains: []string{`<path d="M`},
		},
		{
			name:         "flatline temperatures",
			args:         []any{[]testHourlyItem{{Time: "2026-09-26T14:00", Temp: 65.0, PrecipProb: 10, Icon: "weather-cloudy"}, {Time: "2026-09-26T15:00", Temp: 65.0, PrecipProb: 10, Icon: "weather-cloudy"}, {Time: "2026-09-26T16:00", Temp: 65.0, PrecipProb: 0, Icon: "weather-cloudy"}}},
			wantContains: []string{`stroke-width="2.5"`, "10%", "3 PM", "4 PM"},
		},
		{
			name:         "negative and zero-crossing temperatures with custom gradient",
			args:         []any{[]testHourlyItem{{Time: "2026-09-26T00:00", Temp: -5.0, PrecipProb: 50, Icon: "weather-snowy"}, {Time: "2026-09-26T01:00", Temp: 0.0, PrecipProb: 30, Icon: "weather-snowy-rainy"}, {Time: "2026-09-26T02:00", Temp: 10.0, PrecipProb: 0, Icon: "weather-sunny"}}, "custom-id"},
			wantContains: []string{`id="mm-weather-grad-custom-id"`, "-5°", "0°", "10°", "50%"},
		},
		{
			name:            "map slice with NaN Inf int temps and unparseable time",
			args:            []any{[]map[string]any{{"time": "2026-09-26T14:00", "temp": math.NaN(), "precip_prob": 25.0, "icon": ""}, {"time": "custom-time-format", "temp": math.Inf(1), "precip_prob": 0, "icon": "weather-partly-cloudy"}, {"time": "2026-09-26T16:00", "temp": 70, "precip_prob": 40, "icon": "weather-rainy"}}},
			wantContains:    []string{"custom-time-format", "70°", "40%"},
			wantNotContains: []string{"NaN", "Inf"},
		},
		{
			name:            "scaled vertical coordinates",
			args:            []any{[]testHourlyItem{{Time: "2026-09-26T12:00", Temp: 80.0, PrecipProb: 40, Icon: "weather-rainy"}, {Time: "2026-09-26T13:00", Temp: 50.0, PrecipProb: 0, Icon: "weather-cloudy"}}},
			wantContains:    []string{`viewBox="0 0 560.0 270.0"`, `width: 100%; max-width: 100%;`, `y1="238.0"`, `y2="238.0"`, `y="12" text-anchor="middle" font-size="11"`, `, 18) scale(1.2)`, `r="4.0"`, `y="262" text-anchor="middle" font-size="13"`, `y="66.0" text-anchor="middle" font-size="14" font-weight="800"`, `y="202.0" text-anchor="middle" font-size="14" font-weight="800"`},
			wantNotContains: []string{"min-width:"},
		},
		{
			name: "capping at ten points preserves layout width and truncates eleventh point",
			args: []any{[]testHourlyItem{
				{Time: "2026-09-26T14:00", Temp: 60.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T15:00", Temp: 61.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T16:00", Temp: 62.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T17:00", Temp: 63.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T18:00", Temp: 64.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T19:00", Temp: 65.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T20:00", Temp: 66.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T21:00", Temp: 67.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T22:00", Temp: 68.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-26T23:00", Temp: 69.0, PrecipProb: 0, Icon: "weather-sunny"},
				{Time: "2026-09-27T00:00", Temp: 70.0, PrecipProb: 0, Icon: "weather-sunny"},
			}},
			wantContains:    []string{`viewBox="0 0 560.0 270.0"`, "11 PM"},
			wantNotContains: []string{"12 AM"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := string(render.WeatherHourlyGraph(tc.args...))
			if tc.wantEmpty {
				if got != "" {
					t.Fatalf("expected empty HTML, got: %s", got)
				}
				return
			}
			if !strings.HasPrefix(got, "<svg") || !strings.HasSuffix(got, "</svg>") {
				t.Fatalf("expected valid SVG, got: %s", got)
			}
			for _, sub := range tc.wantContains {
				if !strings.Contains(got, sub) {
					t.Errorf("WeatherHourlyGraph() missing %q in %s", sub, got)
				}
			}
			for _, sub := range tc.wantNotContains {
				if strings.Contains(got, sub) {
					t.Errorf("WeatherHourlyGraph() should not contain %q in %s", sub, got)
				}
			}
		})
	}
}

func TestTodayPrecipProb(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		data any
		want int
	}{
		{"nil data", nil, 0},
		{"WeatherSnapshot with daily", provider.WeatherSnapshot{Daily: []provider.WeatherDaily{{PrecipProbMax: 65}}}, 65},
		{"WeatherSnapshot with current only", provider.WeatherSnapshot{Current: provider.WeatherCurrent{PrecipProbMax: 40}}, 40},
		{"*WeatherSnapshot pointer", &provider.WeatherSnapshot{Daily: []provider.WeatherDaily{{PrecipProbMax: 80}}}, 80},
		{"nil *WeatherSnapshot pointer", (*provider.WeatherSnapshot)(nil), 0},
		{"WeatherCurrent value", provider.WeatherCurrent{PrecipProbMax: 35}, 35},
		{"*WeatherCurrent pointer", &provider.WeatherCurrent{PrecipProbMax: 45}, 45},
		{"nil *WeatherCurrent pointer", (*provider.WeatherCurrent)(nil), 0},
		{"[]WeatherDaily slice", []provider.WeatherDaily{{PrecipProbMax: 90}}, 90},
		{"empty []WeatherDaily slice", []provider.WeatherDaily{}, 0},
		{"map with daily slice", map[string]any{"daily": []any{map[string]any{"precip_prob_max": 50}}}, 50},
		{"map with daily float precip", map[string]any{"daily": []any{map[string]any{"precip_prob_max": float64(50)}}}, 50},
		{"map with current map", map[string]any{"current": map[string]any{"precip_prob_max": 25}}, 25},
		{"map with current map float", map[string]any{"current": map[string]any{"precip_prob_max": float64(25)}}, 25},
		{"map with WeatherDaily slice", map[string]any{"daily": []provider.WeatherDaily{{PrecipProbMax: 70}}}, 70},
		{"map with WeatherCurrent", map[string]any{"current": provider.WeatherCurrent{PrecipProbMax: 15}}, 15},
		{"map with direct precip_prob_max", map[string]any{"precip_prob_max": 85}, 85},
		{"map with direct float precip_prob_max", map[string]any{"precip_prob_max": float64(85)}, 85},
		{"unrelated type", "not-weather-data", 0},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := render.TodayPrecipProb(tc.data); got != tc.want {
				t.Errorf("TodayPrecipProb(%v) = %d, want %d", tc.name, got, tc.want)
			}
		})
	}
}

func TestCalendarFeedEvents(t *testing.T) {
	t.Parallel()
	refTime := time.Date(2026, 10, 10, 15, 30, 0, 0, time.UTC)

	tests := []struct {
		name       string
		data       any
		wantTitles []string
	}{
		{name: "nil data", data: nil},
		{name: "empty events slice", data: provider.CalendarSnapshot{Events: []provider.CalendarEvent{}}},
		{
			name: "filters out past events and keeps ongoing/upcoming",
			data: provider.CalendarSnapshot{Events: []provider.CalendarEvent{
				calEv("Past 86 Days Ago Timed", "2026-07-16T10:00:00Z", "2026-07-16T11:00:00Z", false),
				calEv("Past 86 Days Ago All-Day", "2026-07-16", "2026-07-16", true),
				calEv("Yesterday Timed", "2026-10-09T20:00:00Z", "2026-10-09T21:00:00Z", false),
				calEv("Yesterday All-Day", "2026-10-09", "2026-10-09", true),
				calEv("Earlier Today Finished Timed", "2026-10-10T13:00:00Z", "2026-10-10T14:00:00Z", false),
				calEv("Currently Ongoing Timed", "2026-10-10T15:00:00Z", "2026-10-10T16:00:00Z", false),
				calEv("Starting Exactly Now Timed", "2026-10-10T15:30:00Z", "2026-10-10T16:30:00Z", false),
				calEv("Later Today Timed", "2026-10-10T18:00:00Z", "2026-10-10T19:00:00Z", false),
				calEv("Today All-Day", "2026-10-10", "2026-10-10", true),
				calEv("Multi-Day All-Day Spanning Today", "2026-10-08", "2026-10-12", true),
				calEv("Tomorrow All-Day", "2026-10-11", "2026-10-11", true),
				calEv("Future Timed", "2026-10-12T09:00:00Z", "2026-10-12T10:00:00Z", false),
			}},
			wantTitles: []string{"Currently Ongoing Timed", "Starting Exactly Now Timed", "Later Today Timed", "Today All-Day", "Multi-Day All-Day Spanning Today", "Tomorrow All-Day", "Future Timed"},
		},
		{
			name:       "fallback when End time is missing",
			data:       &provider.CalendarSnapshot{Events: []provider.CalendarEvent{calEv("Past Missing End", "2026-10-10T13:00:00Z", "", false), calEv("Future Missing End", "2026-10-10T16:00:00Z", "", false)}},
			wantTitles: []string{"Future Missing End"},
		},
		{
			name:       "supports map[string]any payload",
			data:       map[string]any{"events": []any{map[string]any{"title": "Map Past", "start": "2026-07-01T10:00:00Z", "end": "2026-07-01T11:00:00Z", "all_day": false}, map[string]any{"title": "Map Future", "start": "2026-10-10T17:00:00Z", "end": "2026-10-10T18:00:00Z", "all_day": false}}},
			wantTitles: []string{"Map Future"},
		},
		{name: "nil snapshot pointer", data: (*provider.CalendarSnapshot)(nil)},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := render.CalendarFeedEvents(tc.data, refTime)
			if len(got) != len(tc.wantTitles) {
				t.Fatalf("got %d events, want %d", len(got), len(tc.wantTitles))
			}
			for i := range got {
				if got[i].Title != tc.wantTitles[i] {
					t.Errorf("at index %d: got %q, want %q", i, got[i].Title, tc.wantTitles[i])
				}
			}
		})
	}
}

func TestCalendarFeedDayGroups(t *testing.T) {
	t.Parallel()
	refTime := time.Date(2026, 10, 10, 8, 0, 0, 0, time.UTC)

	tests := []struct {
		name       string
		data       any
		wantNil    bool
		wantDates  []string
		wantRels   []string
		wantCounts []int
	}{
		{name: "nil data", data: nil, wantNil: true},
		{name: "empty snapshot", data: provider.CalendarSnapshot{}, wantNil: true},
		{
			name:       "groups multiple events occurring on the same day",
			data:       provider.CalendarSnapshot{Events: []provider.CalendarEvent{calEv("Morning Meeting", "2026-10-10T09:00:00Z", "2026-10-10T10:00:00Z", false), calEv("Afternoon Sync", "2026-10-10T14:00:00Z", "2026-10-10T15:00:00Z", false), calEv("Tomorrow Coffee", "2026-10-11T10:00:00Z", "2026-10-11T11:00:00Z", false)}},
			wantDates:  []string{"2026-10-10", "2026-10-11"},
			wantRels:   []string{"Today", "Tomorrow"},
			wantCounts: []int{2, 1},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			groups := render.CalendarFeedDayGroups(tc.data, refTime)
			if tc.wantNil {
				if groups != nil {
					t.Fatalf("expected nil groups, got %+v", groups)
				}
				return
			}
			if len(groups) != len(tc.wantDates) {
				t.Fatalf("expected %d groups, got %d", len(tc.wantDates), len(groups))
			}
			for i := range groups {
				if groups[i].Date != tc.wantDates[i] || groups[i].Rel != tc.wantRels[i] || len(groups[i].Events) != tc.wantCounts[i] {
					t.Errorf("group %d = %+v", i, groups[i])
				}
			}
		})
	}
}

func TestCalendarFeedActiveCalendars(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name      string
		input     any
		wantNames []string
	}{
		{name: "nil input", input: nil},
		{name: "empty map", input: map[string]any{}},
		{
			name:      "config map with enabled and disabled feeds",
			input:     map[string]any{"calendars": []any{map[string]any{"name": "Alex", "color": "#ff6f59"}, map[string]any{"name": "Disabled Feed", "color": "#cccccc", "enabled": false}, map[string]any{"name": "Amelie", "color": "#9d8df1", "enabled": true}, map[string]any{"name": "Explicit False String", "color": "#000000", "enabled": "false"}}},
			wantNames: []string{"Alex", "Amelie"},
		},
		{name: "slice of any directly", input: []any{map[string]any{"name": "Feed 1", "enabled": true}, map[string]any{"name": "Feed 2", "enabled": false}}, wantNames: []string{"Feed 1"}},
		{name: "slice of map[string]any directly", input: []map[string]any{{"name": "Feed A", "enabled": true}, {"name": "Feed B", "enabled": false}}, wantNames: []string{"Feed A"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := render.CalendarFeedActiveCalendars(tt.input)
			if len(got) != len(tt.wantNames) {
				t.Fatalf("got %d items, want %d", len(got), len(tt.wantNames))
			}
			for i, name := range tt.wantNames {
				if got[i]["name"] != name {
					t.Errorf("at index %d: got %v, want %s", i, got[i]["name"], name)
				}
			}
		})
	}
}

func TestTasksStats(t *testing.T) {
	t.Parallel()
	trueVal, falseVal := true, false
	snapshot := &tasks.TasksSnapshot{Items: []tasks.ListItem{{Title: "T1", Done: true}, {Title: "T2", Done: false}}}
	itemsSlice := []tasks.ListItem{{Title: "Task 1", Done: true}, {Title: "Task 2", Done: true}, {Title: "Task 3", Done: true}, {Title: "Task 4", Done: false}}

	tests := []struct {
		name          string
		input         any
		wantTotal     int
		wantDone      int
		wantRemaining int
		wantSummary   string
	}{
		{name: "nil input", input: nil},
		{name: "nil pointer snapshot", input: (*tasks.TasksSnapshot)(nil)},
		{name: "typed ListItem slice", input: itemsSlice, wantTotal: 4, wantDone: 3, wantRemaining: 1, wantSummary: "1 to do"},
		{name: "pointer to ListItem slice", input: &itemsSlice, wantTotal: 4, wantDone: 3, wantRemaining: 1, wantSummary: "1 to do"},
		{name: "slice with nil item pointer", input: []*tasks.ListItem{nil, {Title: "Done", Done: true}, {Title: "Pending", Done: false}}, wantTotal: 2, wantDone: 1, wantRemaining: 1, wantSummary: "1 to do"},
		{name: "tasks snapshot pointer", input: snapshot, wantTotal: 2, wantDone: 1, wantRemaining: 1, wantSummary: "1 to do"},
		{name: "map with items and pointer bool", input: map[string]any{"items": []any{map[string]any{"title": "M1", "done": &trueVal}, map[string]any{"title": "M2", "done": &falseVal}, map[string]any{"title": "M3", "done": true}}}, wantTotal: 3, wantDone: 2, wantRemaining: 1, wantSummary: "1 to do"},
		{name: "map with nil items pointer", input: map[string]any{"items": (*[]tasks.ListItem)(nil)}},
		{name: "multiple remaining tasks", input: []tasks.ListItem{{Title: "T1", Done: true}, {Title: "T2", Done: false}, {Title: "T3", Done: false}}, wantTotal: 3, wantDone: 1, wantRemaining: 2, wantSummary: "2 to do"},
		{name: "all tasks completed", input: []tasks.ListItem{{Title: "T1", Done: true}, {Title: "T2", Done: true}}, wantTotal: 2, wantDone: 2, wantRemaining: 0, wantSummary: "All done"},
		{name: "zero tasks completed", input: []tasks.ListItem{{Title: "T1", Done: false}}, wantTotal: 1, wantDone: 0, wantRemaining: 1, wantSummary: "1 to do"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stats := render.TasksStats(tc.input)
			if stats.Total != tc.wantTotal || stats.Done != tc.wantDone || stats.Remaining != tc.wantRemaining || stats.Summary != tc.wantSummary {
				t.Fatalf("TasksStats(%v) = %+v, want (%d, %d, %d, %q)", tc.name, stats, tc.wantTotal, tc.wantDone, tc.wantRemaining, tc.wantSummary)
			}
		})
	}
}
