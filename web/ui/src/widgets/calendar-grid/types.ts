export interface FamilyMember {
  name: string;
  initial: string;
  color: string;
  text_color: string;
  pattern: string;
  calendars?: string[];
}

export interface FamilyEvent {
  id: string;
  title: string;
  location?: string;
  image_url?: string;
  description?: string;
  start?: string;
  end?: string;
  all_day?: boolean;
  owners?: string[];
  initials?: string[];
  colors?: string[];
  color?: string;
  text_color?: string;
  patterns?: string[];
  pattern?: string;
}

export interface FamilySpanEvent extends FamilyEvent {
  start_col: number;
  col_span: number;
}

export interface FamilyDayEvent extends FamilyEvent {
  top_pct: number;
  height_pct: number;
  left_pct: number;
  width_pct: number;
  is_early_edge?: boolean;
  is_late_edge?: boolean;
  col_span?: number;
  link_glyph?: string;
  is_linked?: boolean;
}

export interface TimelineMarker {
  label: string;
  top_pct: number;
  is_start?: boolean;
  is_end?: boolean;
}

export interface CollapsedHoursGap {
  start_hour: number;
  end_hour: number;
  start_str?: string;
  end_str?: string;
  label: string;
  top_pct: number;
  height_pct: number;
}

export interface FamilyDay {
  date: string;
  weekday_abbr?: string;
  day_of_month: number;
  is_today: boolean;
  is_weekend: boolean;
  special_label?: string;
  all_day?: FamilyEvent[];
  meals?: FamilyEvent[];
  timed?: FamilyEvent[];
  overflow?: number;
  events?: FamilyDayEvent[];
  early_events?: FamilyDayEvent[];
  late_events?: FamilyDayEvent[];
}

export interface FamilyViewModel {
  range_start: string;
  range_end: string;
  range_label: string;
  today: string;
  time_format?: '12h' | '24h' | string;
  hours_start?: string;
  hours_end?: string;
  hour_markers?: string[];
  timeline_markers?: TimelineMarker[];
  collapsed_gaps?: CollapsedHoursGap[];
  members?: FamilyMember[];
  days?: FamilyDay[];
  all_day_events?: FamilySpanEvent[];
  shared_color?: string;
  cached_range_start?: string;
  cached_range_end?: string;
}

interface FamilyMonthDot {
  name: string;
  initial: string;
  color: string;
  text_color: string;
  pattern: string;
  is_shared: boolean;
  member_index: number;
}

export interface FamilyMonthDay {
  date: string;
  day_of_month: number;
  weekday_abbr?: string;
  is_current_month: boolean;
  is_today: boolean;
  is_weekend: boolean;
  special_label?: string;
  dots?: FamilyMonthDot[];
  event_title?: string;
  events_count?: number;
  events?: FamilyEvent[];
}

export interface FamilyMonthWeek {
  week_number: number;
  days: FamilyMonthDay[];
}

export interface FamilyMonthViewModel {
  month_label: string;
  month_name: string;
  year: number;
  month: number;
  range_start: string;
  range_end: string;
  today: string;
  weekday_headers: string[];
  members?: FamilyMember[];
  weeks?: FamilyMonthWeek[];
  days?: FamilyMonthDay[];
  shared_color?: string;
  cached_range_start?: string;
  cached_range_end?: string;
}

export interface CalendarGridViewModel {
  week?: FamilyViewModel;
  month?: FamilyMonthViewModel;
  active_view?: string;
  cached_range_start?: string;
  cached_range_end?: string;
}

export type ViewMode = 'day' | 'week' | 'month';
