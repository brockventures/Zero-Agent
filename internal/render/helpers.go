package render

import (
	"encoding/json"
	"fmt"
	"html/template"
	"math"
	"reflect"
	"strings"
	"time"

	"github.com/azylman/mirrormere/internal/domain"
	"github.com/azylman/mirrormere/internal/provider"
)

var timeLayouts = []string{
	time.RFC3339Nano,
	time.RFC3339,
	"2006-01-02T15:04:05",
	"2006-01-02 15:04:05",
	"2006-01-02T15:04",
	"2006-01-02 15:04",
	"2006-01-02",
	"15:04:05",
	"15:04",
	"3:04 PM",
	"03:04 PM",
}

// StandardFuncMap returns the authoritative template helper functions for Mirrormere widget views.
func StandardFuncMap(nowFunc func() time.Time) template.FuncMap {
	if nowFunc == nil {
		nowFunc = time.Now
	}

	weekViewFn := func(data any, cfg map[string]any, dims domain.Dimension) (*FamilyViewModel, error) {
		return BuildFamilyView(data, cfg, dims, nowFunc())
	}
	monthViewFn := func(data any, cfg map[string]any, dims domain.Dimension) (*FamilyMonthViewModel, error) {
		return BuildFamilyMonthView(data, cfg, dims, nowFunc())
	}

	return template.FuncMap{
		"formatDate": func(args ...any) string {
			return FormatDateInLocation(nowFunc().Location(), args...)
		},
		"formatTime": func(args ...any) string {
			return FormatTimeInLocation(nowFunc().Location(), args...)
		},
		"json":               JSONHelper,
		"jsonJS":             JSONJSHelper,
		"weatherIcon":        WeatherIcon,
		"weatherHourlyGraph": WeatherHourlyGraph,
		"todayPrecipProb":    TodayPrecipProb,
		"calendarGridView":   weekViewFn,
		"monthView":          monthViewFn,
		"isMonthView": func(cfg map[string]any) bool {
			return strings.EqualFold(stringOr(cfg, "default_view", "week"), "month")
		},
		"calendarGridData": BuildFamilyCombinedData,
		"calendarFeedDayGroups": func(data any, args ...any) []CalendarFeedDayGroup {
			return CalendarFeedDayGroups(data, nowFunc())
		},
		"calendarFeedActiveCalendars": CalendarFeedActiveCalendars,
		"activeCalendars":             CalendarFeedActiveCalendars,
		"tasksStats":                  TasksStats,
		"stringOr":                    stringOr,
		"sub": func(a, b int) int {
			return a - b
		},
	}
}

// ParseTime attempts to parse an unknown time value into a time.Time struct.
func ParseTime(v any) (time.Time, bool) {
	switch val := v.(type) {
	case time.Time:
		return val, true
	case *time.Time:
		if val == nil {
			return time.Time{}, false
		}
		return *val, true
	case string:
		for _, layout := range timeLayouts {
			if t, err := time.Parse(layout, val); err == nil {
				return t, true
			}
		}
		return time.Time{}, false
	case int64:
		return time.Unix(val, 0), true
	case int:
		return time.Unix(int64(val), 0), true
	case float64:
		return time.Unix(int64(val), 0), true
	default:
		return time.Time{}, false
	}
}

func isDateOnly(v any) bool {
	if s, ok := v.(string); ok {
		s = strings.TrimSpace(s)
		if len(s) == 10 && s[4] == '-' && s[7] == '-' {
			_, err := time.Parse("2006-01-02", s)
			return err == nil
		}
	}
	return false
}

func resolveFormatArgs(defaultLayout string, args []any) (layout string, val any, ok bool) {
	if len(args) == 0 {
		return "", nil, false
	}
	layout = defaultLayout
	if len(args) == 1 {
		val = args[0]
	} else {
		layoutStr := fmt.Sprint(args[0])
		if layoutStr != "" && layoutStr != "default" {
			layout = layoutStr
		}
		val = args[1]
	}
	return layout, val, true
}

// FormatDateInLocation formats a time value with the specified date layout in the given location.
// If layout is omitted, defaults to "2006-01-02".
// If loc is nil, UTC/original location is preserved.
func FormatDateInLocation(loc *time.Location, args ...any) string {
	layout, val, ok := resolveFormatArgs("2006-01-02", args)
	if !ok {
		return ""
	}

	if isDateOnly(val) {
		s := strings.TrimSpace(fmt.Sprint(val))
		t, err := time.Parse("2006-01-02", s)
		if err != nil {
			return s
		}
		return t.Format(layout)
	}

	t, ok := ParseTime(val)
	if !ok {
		return fmt.Sprint(val)
	}
	if loc != nil {
		t = t.In(loc)
	}
	return t.Format(layout)
}

// FormatTimeInLocation formats a time value with the specified time layout in the given location.
// If layout is omitted, defaults to "15:04".
// If loc is nil, UTC/original location is preserved.
func FormatTimeInLocation(loc *time.Location, args ...any) string {
	layout, val, ok := resolveFormatArgs("15:04", args)
	if !ok {
		return ""
	}

	if isDateOnly(val) {
		return ""
	}

	t, ok := ParseTime(val)
	if !ok {
		return fmt.Sprint(val)
	}
	if loc != nil {
		t = t.In(loc)
	}
	return t.Format(layout)
}

// RelDate returns human-readable relative date expressions ("Today", "Tomorrow", "Yesterday", "in X days", "X days ago").
func RelDate(refTime time.Time, args ...any) string {
	if len(args) == 0 {
		return ""
	}
	val := args[0]
	if len(args) >= 2 {
		if customRef, refOk := ParseTime(args[1]); refOk {
			refTime = customRef
		}
	}

	refLoc := refTime.Location()
	if refLoc == nil {
		refLoc = time.UTC
	}

	var tDate time.Time
	if isDateOnly(val) {
		s := strings.TrimSpace(fmt.Sprint(val))
		t, err := time.Parse("2006-01-02", s)
		if err != nil {
			return s
		}
		tDate = time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, refLoc)
	} else {
		t, ok := ParseTime(val)
		if !ok {
			return fmt.Sprint(val)
		}
		tInLoc := t.In(refLoc)
		tDate = time.Date(tInLoc.Year(), tInLoc.Month(), tInLoc.Day(), 0, 0, 0, 0, refLoc)
	}

	refDate := time.Date(refTime.Year(), refTime.Month(), refTime.Day(), 0, 0, 0, 0, refLoc)
	diffHours := tDate.Sub(refDate).Hours()
	diffDays := int(math.Round(diffHours / 24))

	switch {
	case diffDays == 0:
		return "Today"
	case diffDays == 1:
		return "Tomorrow"
	case diffDays == -1:
		return "Yesterday"
	case diffDays > 1:
		return fmt.Sprintf("in %d days", diffDays)
	default:
		return fmt.Sprintf("%d days ago", -diffDays)
	}
}

// JSONHelper marshals any value into unescaped template.HTML.
func JSONHelper(v any) (template.HTML, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return template.HTML(b), nil
}

// JSONJSHelper marshals any value into safe template.JS.
func JSONJSHelper(v any) (template.JS, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return template.JS(b), nil
}

func weatherIconPath(token string) string {
	switch token {
	case "weather-sunny":
		return `<circle cx="12" cy="12" r="5" fill="#f59e0b" stroke="#f59e0b" stroke-width="1"/><g stroke="#f59e0b" stroke-width="2" stroke-linecap="round"><line x1="12" y1="1.5" x2="12" y2="3.5"/><line x1="12" y1="20.5" x2="12" y2="22.5"/><line x1="4.5" y1="4.5" x2="6" y2="6"/><line x1="18" y1="18" x2="19.5" y2="19.5"/><line x1="1.5" y1="12" x2="3.5" y2="12"/><line x1="20.5" y1="12" x2="22.5" y2="12"/><line x1="4.5" y1="19.5" x2="6" y2="18"/><line x1="18" y1="6" x2="19.5" y2="4.5"/></g>`
	case "weather-clear-night":
		return `<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" fill="#f8fafc" stroke="#94a3b8" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`
	case "weather-partly-cloudy":
		return `<circle cx="16" cy="9" r="4" fill="#f59e0b"/><g stroke="#f59e0b" stroke-width="1.8" stroke-linecap="round"><line x1="16" y1="2" x2="16" y2="3.5"/><line x1="21.5" y1="6" x2="20" y2="7"/><line x1="21.5" y1="12" x2="20" y2="11"/><line x1="11.5" y1="4" x2="12.5" y2="5"/></g><path d="M15.5 19H7a4.5 4.5 0 0 1-.8-8.9A5.5 5.5 0 0 1 16.8 11 3.5 3.5 0 0 1 15.5 19z" fill="#ffffff" stroke="#94a3b8" stroke-width="1.6" stroke-linejoin="round"/>`
	case "weather-night-partly-cloudy":
		return `<path d="M19 10.5A6 6 0 0 1 12.5 4a5 5 0 0 0 6.5 6.5z" fill="#f8fafc" stroke="#94a3b8" stroke-width="1.5" stroke-linejoin="round"/><path d="M15.5 19H7a4.5 4.5 0 0 1-.8-8.9A5.5 5.5 0 0 1 16.8 11 3.5 3.5 0 0 1 15.5 19z" fill="#ffffff" stroke="#94a3b8" stroke-width="1.6" stroke-linejoin="round"/>`
	case "weather-cloudy":
		return `<path d="M17.5 19H7a5 5 0 0 1-1-9.9A6 6 0 0 1 17.8 11 4 4 0 0 1 17.5 19z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.7" stroke-linejoin="round"/>`
	case "weather-fog":
		return `<g stroke="#94a3b8" stroke-width="2" stroke-linecap="round"><line x1="4" y1="10" x2="20" y2="10"/><line x1="3" y1="14" x2="21" y2="14"/><line x1="5" y1="18" x2="19" y2="18"/><line x1="7" y1="6" x2="17" y2="6"/></g>`
	case "weather-rainy":
		return `<path d="M17.5 15H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 7 3.5 3.5 0 0 1 17.5 15z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.7" stroke-linejoin="round"/><g stroke="#38bdf8" stroke-width="2" stroke-linecap="round"><line x1="8" y1="18" x2="7" y2="21"/><line x1="12" y1="18" x2="11" y2="21"/><line x1="16" y1="18" x2="15" y2="21"/></g>`
	case "weather-pouring":
		return `<path d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z" fill="#e2e8f0" stroke="#64748b" stroke-width="1.7" stroke-linejoin="round"/><g stroke="#0284c7" stroke-width="2" stroke-linecap="round"><line x1="6" y1="17" x2="4" y2="21"/><line x1="10" y1="17" x2="8" y2="21"/><line x1="14" y1="17" x2="12" y2="21"/><line x1="18" y1="17" x2="16" y2="21"/></g>`
	case "weather-snowy":
		return `<path d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.7" stroke-linejoin="round"/><g fill="#93c5fd"><circle cx="8" cy="18" r="1.3"/><circle cx="12" cy="18" r="1.3"/><circle cx="16" cy="18" r="1.3"/><circle cx="10" cy="21" r="1.3"/><circle cx="14" cy="21" r="1.3"/></g>`
	case "weather-snowy-rainy":
		return `<path d="M17.5 14H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 6 3.5 3.5 0 0 1 17.5 14z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.7" stroke-linejoin="round"/><g fill="#93c5fd"><circle cx="8" cy="18" r="1.3"/><circle cx="14" cy="18" r="1.3"/></g><g stroke="#38bdf8" stroke-width="2" stroke-linecap="round"><line x1="11" y1="18" x2="10" y2="21"/></g>`
	case "weather-lightning", "weather-lightning-rainy":
		return `<path d="M17.5 13H7a4.5 4.5 0 0 1-1-8.9A5.5 5.5 0 0 1 16.8 5 3.5 3.5 0 0 1 17.5 13z" fill="#e2e8f0" stroke="#64748b" stroke-width="1.7" stroke-linejoin="round"/><polygon points="12 13 9 18 13 18 11 23 16 16 12 16 14 13" fill="#f59e0b" stroke="#f59e0b" stroke-width="1" stroke-linejoin="round"/>`
	default:
		return `<path d="M17.5 19H7a5 5 0 0 1-1-9.9A6 6 0 0 1 17.8 11 4 4 0 0 1 17.5 19z" fill="#f1f5f9" stroke="#94a3b8" stroke-width="1.7" stroke-linejoin="round"/>`
	}
}

// WeatherIcon returns a sanitized, scalable vector inline SVG for WMO condition tokens.
func WeatherIcon(args ...any) template.HTML {
	token := "weather-cloudy"
	size := 20
	if len(args) > 0 && args[0] != nil {
		s := fmt.Sprint(args[0])
		if s != "" {
			token = s
		}
	}
	if len(args) > 1 {
		switch v := args[1].(type) {
		case int:
			if v > 0 {
				size = v
			}
		case int64:
			if v > 0 {
				size = int(v)
			}
		case float64:
			if v > 0 {
				size = int(v)
			}
		case string:
			var parsedSize int
			if n, err := fmt.Sscanf(v, "%d", &parsedSize); err == nil && n > 0 && parsedSize > 0 {
				size = parsedSize
			}
		}
	}
	if size < 12 {
		size = 12
	} else if size > 64 {
		size = 64
	}

	pathHTML := weatherIconPath(token)
	return template.HTML(fmt.Sprintf(`<svg width="%d" height="%d" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="mm-weather-icon mm-icon-%s">%s</svg>`, size, size, template.HTMLEscapeString(token), pathHTML))
}

type hourlyPoint struct {
	timeStr    string
	temp       float64
	precipProb int
	icon       string
	x          float64
	y          float64
}

func derefSlice(arg any) (reflect.Value, bool) {
	if arg == nil {
		return reflect.Value{}, false
	}
	val := reflect.ValueOf(arg)
	if val.Kind() == reflect.Pointer {
		if val.IsNil() {
			return reflect.Value{}, false
		}
		val = val.Elem()
	}
	if (val.Kind() != reflect.Slice && val.Kind() != reflect.Array) || val.Len() == 0 {
		return reflect.Value{}, false
	}
	return val, true
}

func mapFloat(m map[string]any, key string) (float64, bool) {
	if v, ok := m[key].(float64); ok {
		return v, true
	}
	if v, ok := m[key].(int); ok {
		return float64(v), true
	}
	return 0, false
}

func mapInt(m map[string]any, key string) (int, bool) {
	if v, ok := m[key].(int); ok {
		return v, true
	}
	if v, ok := m[key].(float64); ok {
		return int(v), true
	}
	return 0, false
}

func structString(v reflect.Value, field string) string {
	if f := v.FieldByName(field); f.IsValid() && f.Kind() == reflect.String {
		return f.String()
	}
	return ""
}

func structFloat(v reflect.Value, field string) float64 {
	if f := v.FieldByName(field); f.IsValid() {
		if f.Kind() == reflect.Float64 || f.Kind() == reflect.Float32 {
			return f.Float()
		}
		if f.Kind() == reflect.Int || f.Kind() == reflect.Int64 {
			return float64(f.Int())
		}
	}
	return 0
}

func structInt(v reflect.Value, field string) int {
	if f := v.FieldByName(field); f.IsValid() {
		if f.Kind() == reflect.Int || f.Kind() == reflect.Int64 {
			return int(f.Int())
		}
		if f.Kind() == reflect.Float64 || f.Kind() == reflect.Float32 {
			return int(f.Float())
		}
	}
	return 0
}

func hourlyPointFromMap(m map[string]any) hourlyPoint {
	var pt hourlyPoint
	if t, ok := m["time"].(string); ok {
		pt.timeStr = t
	}
	if tmp, ok := mapFloat(m, "temp"); ok {
		pt.temp = tmp
	}
	if p, ok := mapInt(m, "precip_prob"); ok {
		pt.precipProb = p
	}
	if ic, ok := m["icon"].(string); ok {
		pt.icon = ic
	}
	return pt
}

func hourlyPointFromStruct(item any) (hourlyPoint, bool) {
	v := reflect.ValueOf(item)
	if v.Kind() == reflect.Pointer {
		if v.IsNil() {
			return hourlyPoint{}, false
		}
		v = v.Elem()
	}
	if v.Kind() != reflect.Struct {
		return hourlyPoint{}, false
	}
	return hourlyPoint{
		timeStr:    structString(v, "Time"),
		temp:       structFloat(v, "Temp"),
		precipProb: structInt(v, "PrecipProb"),
		icon:       structString(v, "Icon"),
	}, true
}

func extractHourlyPoints(arg any) []hourlyPoint {
	val, ok := derefSlice(arg)
	if !ok {
		return nil
	}

	n := val.Len()
	pts := make([]hourlyPoint, 0, n)
	for i := range n {
		itemVal := val.Index(i)
		if !itemVal.IsValid() {
			continue
		}
		item := itemVal.Interface()
		if item == nil {
			continue
		}

		var pt hourlyPoint
		if m, ok := item.(map[string]any); ok {
			pt = hourlyPointFromMap(m)
		} else if parsed, ok := hourlyPointFromStruct(item); ok {
			pt = parsed
		} else {
			continue
		}

		if math.IsNaN(pt.temp) || math.IsInf(pt.temp, 0) {
			pt.temp = 0.0
		}
		if pt.icon == "" {
			pt.icon = "weather-cloudy"
		}
		pts = append(pts, pt)
	}

	return pts
}

func computeHourlyTempSpan(pts []hourlyPoint) (float64, float64) {
	minTemp := pts[0].temp
	maxTemp := pts[0].temp
	for _, p := range pts[1:] {
		if p.temp < minTemp {
			minTemp = p.temp
		}
		if p.temp > maxTemp {
			maxTemp = p.temp
		}
	}

	span := maxTemp - minTemp
	if span < 4.0 {
		mid := (minTemp + maxTemp) / 2.0
		minTemp = mid - 2.0
		span = 4.0
	}
	return minTemp, span
}

// WeatherHourlyGraph renders an SVG continuous line graph showing hourly temperature trends,
// condition icons, precipitation chances, and time throughout the day.
func WeatherHourlyGraph(args ...any) template.HTML {
	if len(args) == 0 || args[0] == nil {
		return ""
	}

	pts := extractHourlyPoints(args[0])
	if len(pts) == 0 {
		return ""
	}
	if len(pts) > 10 {
		pts = pts[:10]
	}

	gradientID := "mm-weather-hourly-grad"
	if len(args) > 1 && args[1] != nil {
		if idStr := fmt.Sprint(args[1]); idStr != "" {
			gradientID = "mm-weather-grad-" + template.HTMLEscapeString(idStr)
		}
	}

	const (
		baseColWidth  = 56.0
		minGraphWidth = 560.0
		svgHeight     = 270.0
		minY          = 76.0  // High temp y-coord
		maxY          = 212.0 // Low temp y-coord (amplitude 136px)
		baseY         = 238.0 // Baseline axis y-coord
		edgeInset     = 18.0
	)

	n := len(pts)
	colWidth := baseColWidth
	if float64(n)*colWidth < minGraphWidth && n > 0 {
		colWidth = minGraphWidth / float64(n)
	}
	totalWidth := float64(n) * colWidth

	minTemp, span := computeHourlyTempSpan(pts)

	step := 0.0
	if n > 1 {
		step = (totalWidth - 2.0*edgeInset) / float64(n-1)
	}

	for i := range pts {
		if n > 1 {
			pts[i].x = edgeInset + float64(i)*step
		} else {
			pts[i].x = totalWidth / 2.0
		}
		ratio := (pts[i].temp - minTemp) / span
		pts[i].y = maxY - ratio*(maxY-minY)
	}

	var sb strings.Builder
	sb.Grow(4096)

	fmt.Fprintf(&sb, `<svg class="weather-hourly-graph" viewBox="0 0 %.1f %.1f" style="height: 100%%; width: 100%%; max-width: 100%%; display: block; overflow: visible;">`,
		totalWidth, svgHeight)

	fmt.Fprintf(&sb, `<defs><linearGradient id="%s" x1="0" y1="0" x2="0" y2="1">`+
		`<stop offset="0%%" stop-color="#ff6f59" stop-opacity="0.25"/>`+
		`<stop offset="100%%" stop-color="#ff6f59" stop-opacity="0.0"/>`+
		`</linearGradient></defs>`, gradientID)

	fmt.Fprintf(&sb, `<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" stroke="rgba(90, 60, 30, 0.08)" stroke-width="1"/>`,
		baseY, totalWidth, baseY)

	if n > 1 {
		var curvePath strings.Builder
		var areaPath strings.Builder

		fmt.Fprintf(&curvePath, "M %.1f %.1f", pts[0].x, pts[0].y)
		fmt.Fprintf(&areaPath, "M %.1f %.1f L %.1f %.1f", pts[0].x, baseY, pts[0].x, pts[0].y)

		for i := range n - 1 {
			dx := (pts[i+1].x - pts[i].x) / 2.0
			seg := fmt.Sprintf(" C %.1f %.1f, %.1f %.1f, %.1f %.1f",
				pts[i].x+dx, pts[i].y,
				pts[i+1].x-dx, pts[i+1].y,
				pts[i+1].x, pts[i+1].y)
			curvePath.WriteString(seg)
			areaPath.WriteString(seg)
		}
		fmt.Fprintf(&areaPath, " L %.1f %.1f Z", pts[n-1].x, baseY)

		fmt.Fprintf(&sb, `<path d="%s" fill="url(#%s)"/>`, areaPath.String(), gradientID)
		fmt.Fprintf(&sb, `<path d="%s" fill="none" stroke="var(--mm-coral, #ff6f59)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`,
			curvePath.String())
	}

	for i, pt := range pts {
		fmt.Fprintf(&sb, `<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="rgba(90, 60, 30, 0.08)" stroke-dasharray="2 2"/>`,
			pt.x, pt.y+6.0, pt.x, baseY)

		if pt.precipProb > 0 {
			fmt.Fprintf(&sb, `<text x="%.1f" y="12" text-anchor="middle" font-size="11" font-weight="700" fill="var(--mm-sky, #3b9edd)" style="user-select: none; pointer-events: none;">☂ %d%%</text>`,
				pt.x, pt.precipProb)
		}

		iconPath := weatherIconPath(pt.icon)
		fmt.Fprintf(&sb, `<g transform="translate(%.1f, 18) scale(1.2)" fill="none" stroke="var(--mm-honey, #f59e0b)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">%s</g>`,
			pt.x-14.4, iconPath)

		tempRound := int(math.Round(pt.temp))
		fmt.Fprintf(&sb, `<text x="%.1f" y="%.1f" text-anchor="middle" font-size="14" font-weight="800" fill="var(--mm-text-title, #2a2338)" style="user-select: none; pointer-events: none;">%d°</text>`,
			pt.x, pt.y-10.0, tempRound)

		fmt.Fprintf(&sb, `<circle cx="%.1f" cy="%.1f" r="4.0" fill="#ffffff" stroke="var(--mm-coral, #ff6f59)" stroke-width="2.5"/>`,
			pt.x, pt.y)

		timeLabel := "Now"
		timeWeight := "800"
		timeColor := "var(--mm-coral, #ff6f59)"
		if i > 0 {
			timeWeight = "600"
			timeColor = "var(--mm-text-dim, #8b819f)"
			if formatted := FormatTimeInLocation(nil, "3 PM", pt.timeStr); formatted != "" {
				timeLabel = formatted
			} else {
				timeLabel = pt.timeStr
			}
		}
		fmt.Fprintf(&sb, `<text x="%.1f" y="262" text-anchor="middle" font-size="13" font-weight="%s" fill="%s" style="user-select: none; pointer-events: none;">%s</text>`,
			pt.x, timeWeight, timeColor, template.HTMLEscapeString(timeLabel))
	}

	sb.WriteString("</svg>")
	return template.HTML(sb.String())
}

func precipProbFromMap(v map[string]any) int {
	if daily, ok := v["daily"].([]any); ok && len(daily) > 0 {
		if first, ok := daily[0].(map[string]any); ok {
			if p, ok := mapInt(first, "precip_prob_max"); ok {
				return p
			}
		}
	}
	if daily, ok := v["daily"].([]provider.WeatherDaily); ok && len(daily) > 0 {
		return daily[0].PrecipProbMax
	}
	if cur, ok := v["current"].(map[string]any); ok {
		if p, ok := mapInt(cur, "precip_prob_max"); ok {
			return p
		}
	}
	if cur, ok := v["current"].(provider.WeatherCurrent); ok {
		return cur.PrecipProbMax
	}
	if p, ok := mapInt(v, "precip_prob_max"); ok {
		return p
	}
	return 0
}

// TodayPrecipProb extracts today's maximum precipitation probability percentage (0-100)
// from WeatherSnapshot, WeatherCurrent, map data, or slices.
func TodayPrecipProb(arg any) int {
	if arg == nil {
		return 0
	}

	switch v := arg.(type) {
	case provider.WeatherSnapshot:
		return v.TodayPrecipProbMax()
	case *provider.WeatherSnapshot:
		if v != nil {
			return v.TodayPrecipProbMax()
		}
	case provider.WeatherCurrent:
		return v.PrecipProbMax
	case *provider.WeatherCurrent:
		if v != nil {
			return v.PrecipProbMax
		}
	case []provider.WeatherDaily:
		if len(v) > 0 {
			return v[0].PrecipProbMax
		}
	case map[string]any:
		return precipProbFromMap(v)
	}

	return 0
}

func extractCalendarSnapshot(data any) provider.CalendarSnapshot {
	switch v := data.(type) {
	case provider.CalendarSnapshot:
		return v
	case *provider.CalendarSnapshot:
		if v != nil {
			return *v
		}
	case map[string]any:
		b, err := json.Marshal(v)
		if err == nil {
			var snap provider.CalendarSnapshot
			if err := json.Unmarshal(b, &snap); err == nil {
				return snap
			}
		}
	}
	return provider.CalendarSnapshot{}
}

func isTimedCalendarEventConcluded(ev provider.CalendarEvent, refTime time.Time) bool {
	if ev.End != "" {
		if t, ok := ParseTime(ev.End); ok {
			return t.Before(refTime)
		}
	}
	if ev.Start != "" {
		if t, ok := ParseTime(ev.Start); ok {
			return t.Add(time.Hour).Before(refTime)
		}
	}
	return false
}

// CalendarFeedEvents filters a CalendarSnapshot (or equivalent payload) down to events starting
// at or after the reference time (i.e. currently ongoing events and upcoming events).
// All-day events for today and future days are included; past all-day events are excluded.
// Timed events ending at or after refTime are included; events ending before refTime are excluded.
func CalendarFeedEvents(data any, refTime time.Time) []provider.CalendarEvent {
	snapshot := extractCalendarSnapshot(data)
	if len(snapshot.Events) == 0 {
		return nil
	}

	todayStr := refTime.Format("2006-01-02")
	filtered := make([]provider.CalendarEvent, 0, len(snapshot.Events))

	for _, ev := range snapshot.Events {
		if ev.AllDay {
			endDateStr := ev.End
			if endDateStr == "" {
				endDateStr = ev.Start
			}
			// In RFC 5545 formatEventRange, End is an inclusive date formatted as YYYY-MM-DD.
			// If End < todayStr, the event has already concluded before today.
			if endDateStr < todayStr {
				continue
			}
			filtered = append(filtered, ev)
			continue
		}

		if isTimedCalendarEventConcluded(ev, refTime) {
			continue
		}

		filtered = append(filtered, ev)
	}

	return filtered
}

// CalendarFeedDayGroup groups calendar feed events occurring on the same local calendar day.
type CalendarFeedDayGroup struct {
	Date   string                   `json:"date"`
	Rel    string                   `json:"rel"`
	Start  string                   `json:"start"`
	Events []provider.CalendarEvent `json:"events"`
}

// CalendarFeedDayGroups groups filtered calendar feed events by local calendar date so that
// all events for a given day can be presented together in a unified day pill/card.
func CalendarFeedDayGroups(data any, refTime time.Time) []CalendarFeedDayGroup {
	events := CalendarFeedEvents(data, refTime)
	if len(events) == 0 {
		return nil
	}

	loc := refTime.Location()
	if loc == nil {
		loc = time.UTC
	}

	var groups []CalendarFeedDayGroup
	for _, ev := range events {
		dateStr := FormatDateInLocation(loc, "2006-01-02", ev.Start)
		if len(groups) == 0 || groups[len(groups)-1].Date != dateStr {
			rel := RelDate(refTime, ev.Start)
			groups = append(groups, CalendarFeedDayGroup{
				Date:   dateStr,
				Rel:    rel,
				Start:  ev.Start,
				Events: []provider.CalendarEvent{ev},
			})
		} else {
			groups[len(groups)-1].Events = append(groups[len(groups)-1].Events, ev)
		}
	}

	return groups
}

// CalendarFeedActiveCalendars filters widget configuration calendars down to those that are enabled.
// If a calendar does not specify an 'enabled' field, it defaults to true. Feeds with enabled explicitly
// set to false are excluded so header chips and count badges accurately reflect active synchronized calendars.
func CalendarFeedActiveCalendars(config any) []map[string]any {
	var rawCalendars []any

	switch v := config.(type) {
	case map[string]any:
		if c, ok := v["calendars"]; ok {
			return CalendarFeedActiveCalendars(c)
		}
		return nil
	case []any:
		rawCalendars = v
	case []map[string]any:
		var result []map[string]any
		for _, item := range v {
			if isCalendarItemEnabled(item) {
				result = append(result, item)
			}
		}
		return result
	default:
		return nil
	}

	var result []map[string]any
	for _, item := range rawCalendars {
		if m, ok := item.(map[string]any); ok {
			if isCalendarItemEnabled(m) {
				result = append(result, m)
			}
		}
	}
	return result
}

func isCalendarItemEnabled(m map[string]any) bool {
	if enabledVal, ok := m["enabled"]; ok {
		switch b := enabledVal.(type) {
		case bool:
			return b
		case string:
			return !strings.EqualFold(b, "false")
		}
	}
	return true
}

// TasksStatsResult contains summary completion statistics for a tasks list.
type TasksStatsResult struct {
	Total     int    `json:"total"`
	Done      int    `json:"done"`
	Remaining int    `json:"remaining"`
	Summary   string `json:"summary"`
}

func derefReflectValue(v reflect.Value) (reflect.Value, bool) {
	for v.Kind() == reflect.Pointer || v.Kind() == reflect.Interface {
		if v.IsNil() {
			return reflect.Value{}, false
		}
		v = v.Elem()
	}
	return v, true
}

func extractItemsValue(val reflect.Value) reflect.Value {
	if val.Kind() == reflect.Struct {
		itemsField := val.FieldByName("Items")
		if itemsField.IsValid() {
			return itemsField
		}
	} else if val.Kind() == reflect.Map {
		for _, key := range val.MapKeys() {
			if strings.EqualFold(fmt.Sprint(key.Interface()), "items") {
				return val.MapIndex(key)
			}
		}
	}
	return val
}

func isTaskItemDone(v reflect.Value) (bool, bool) {
	v, ok := derefReflectValue(v)
	if !ok {
		return false, false
	}
	switch v.Kind() {
	case reflect.Struct:
		f := v.FieldByName("Done")
		if f.IsValid() && f.Kind() == reflect.Bool && f.Bool() {
			return true, true
		}
		return true, false
	case reflect.Map:
		for _, key := range v.MapKeys() {
			if strings.EqualFold(fmt.Sprint(key.Interface()), "done") {
				val := v.MapIndex(key)
				val, ok := derefReflectValue(val)
				if ok && val.Kind() == reflect.Bool && val.Bool() {
					return true, true
				}
				return true, false
			}
		}
		return true, false
	default:
		return true, false
	}
}

// TasksStats inspects a tasks list or slice of items and calculates completion metrics.
func TasksStats(input any) TasksStatsResult {
	if input == nil {
		return TasksStatsResult{}
	}

	val, ok := derefReflectValue(reflect.ValueOf(input))
	if !ok {
		return TasksStatsResult{}
	}

	val = extractItemsValue(val)
	val, ok = derefReflectValue(val)
	if !ok {
		return TasksStatsResult{}
	}

	var total, done int
	if val.Kind() == reflect.Slice || val.Kind() == reflect.Array {
		n := val.Len()
		for i := range n {
			if valid, isDone := isTaskItemDone(val.Index(i)); valid {
				total++
				if isDone {
					done++
				}
			}
		}
	}

	rem := max(total-done, 0)

	summary := ""
	if total > 0 {
		switch rem {
		case 0:
			summary = "All done"
		case 1:
			summary = "1 to do"
		default:
			summary = fmt.Sprintf("%d to do", rem)
		}
	}

	return TasksStatsResult{
		Total:     total,
		Done:      done,
		Remaining: rem,
		Summary:   summary,
	}
}
