// Payload types for the SSE event stream served at api/events.
// Event names and shapes follow internal/events/event.go and hydration.go and
// the JSON schemas in api/schemas/*.json. Keep the field names identical to the
// wire format; the store maps them to camelCase where it needs to.

interface WidgetUpdatePayload {
  widget_id: string;
  timestamp: string;
  state: string;
  /** Domain data; shape is defined by the widget type's manifest response_schema. */
  data: unknown;
}

interface WidgetReloadPayload {
  type: string;
}

interface StyleReloadPayload {
  file: string;
  timestamp: string;
}

export interface SystemReloadPayload {
  reason: string;
  timestamp: string;
}

export type ProviderHealth = 'healthy' | 'degraded' | 'error';

export interface SystemStatusPayload {
  online: boolean;
  time: string;
  config_status: 'ok' | 'error';
  config_error: string | null;
  providers?: Record<string, ProviderHealth>;
  boot_id?: string;
}

export interface ScreenRotateWidget {
  widget_id: string;
  origin: [number, number];
  dimensions: [number, number];
}

export interface ScreenRotatePayload {
  current_screen: number;
  total_screens: number;
  interval_seconds: number;
  widgets: ScreenRotateWidget[];
}

export interface HeaderWeather {
  temperature: number;
  units?: string;
  weather_code?: number;
  icon?: string;
  condition?: string;
  location?: string;
}

interface HeaderUpdatePayload {
  timestamp: string;
  timezone: string;
  weather?: HeaderWeather;
}

type VideoStreamType = 'webrtc' | 'hls' | 'mjpeg';
type VideoPlayerState = 'playing' | 'paused' | 'buffering' | 'idle';

export interface VideoSource {
  id: string;
  stream_url: string;
  type: VideoStreamType;
  priority?: 'persistent' | 'temporary';
  timeout_seconds?: number;
  controllable?: boolean;
  control_url?: string;
  player_state?: VideoPlayerState;
  muted?: boolean;
  title?: string;
  series?: string;
  season?: string | number;
  episode?: string | number;
  position?: number;
  duration?: number;
  position_updated_at?: number;
}

export interface VideoStatePayload {
  mode: 'widgets' | 'video';
  primary: VideoSource | null;
  pip: VideoSource | null;
}

export interface AudioStatePayload {
  volume: number;
  muted: boolean;
}

export type VoiceState =
  | 'idle'
  | 'listening'
  | 'transcribing'
  | 'thinking'
  | 'synthesizing'
  | 'speaking'
  | 'error';

export interface VoiceStatePayload {
  state: VoiceState;
  transcript: string | null;
  reply: string | null;
  tts_engine: string | null;
  status: string | null;
}

interface ThemeUpdatePayload {
  theme: string;
}

export interface LayoutEditorWidget {
  widget_id: string;
  type: string;
  screen: number;
  origin: [number, number];
  dimensions: [number, number];
  pinned: boolean;
  supported_dimensions: [number, number][];
}

export interface LayoutEditorResponse {
  allow_layout_edit: boolean;
  has_override: boolean;
  grid: [number, number];
  total_screens: number;
  widgets: LayoutEditorWidget[];
}

export interface LayoutEditorWidgetPlacement {
  widget_id: string;
  screen: number;
  origin: [number, number];
  dimensions: [number, number];
  pinned?: boolean;
}

export interface LayoutEditorUpdateRequest {
  widgets: LayoutEditorWidgetPlacement[];
}

/** Synthetic event emitted by the client on open and on error; not sent by the server. */
export interface ConnectionPayload {
  status: 'connected' | 'disconnected';
  lastEventId: string | null;
  error?: unknown;
}

/** Response of GET api/widgets/{widget_id} (internal/api WidgetDetailResponse). */
export interface WidgetDetailResponse {
  widget_id: string;
  type: string;
  origin: [number, number];
  dimensions: [number, number];
  config: Record<string, unknown>;
  state: string;
  data: unknown;
  timestamp: string;
  online: boolean;
  assets_base: string;
  /** Present only for calendar-grid. */
  view_model?: Record<string, unknown>;
}

/** Every event name the client can receive, mapped to its payload. */
export interface StreamEventMap {
  'screen.rotate': ScreenRotatePayload;
  'widget.update': WidgetUpdatePayload;
  'widget.reload': WidgetReloadPayload;
  'style.reload': StyleReloadPayload;
  'header.update': HeaderUpdatePayload;
  'system.status': SystemStatusPayload;
  'system.reload': SystemReloadPayload;
  'video.state': VideoStatePayload;
  'audio.state': AudioStatePayload;
  'voice.state': VoiceStatePayload;
  'theme.update': ThemeUpdatePayload;
  connection: ConnectionPayload;
}

export type StreamEventName = keyof StreamEventMap;

/** Server-sent events (everything except the synthetic connection event). */
export type ServerEventName = Exclude<StreamEventName, 'connection'>;

/** One widget cell of a screen as listed by GET api/screens (internal/api WidgetPlacement). */
export interface WidgetPlacement {
  widget_id: string;
  type: string;
  origin: [number, number];
  dimensions: [number, number];
}

/** One screen of the rotation layout (internal/api ScreenLayout). */
export interface ScreenLayout {
  index: number;
  widgets: WidgetPlacement[];
}

/** Response of GET api/screens (internal/api ScreensResponse). */
export interface ScreensResponse {
  current_screen: number;
  interval_seconds: number;
  screens: ScreenLayout[];
}
