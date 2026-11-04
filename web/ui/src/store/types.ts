import type {
  AudioStatePayload,
  HeaderWeather,
  ProviderHealth,
  ScreenLayout,
  ScreenRotatePayload,
  ScreenRotateWidget,
  VideoStatePayload,
  VoiceStatePayload,
  WidgetDetailResponse,
} from '../sse/types';

export type WidgetMeta = Pick<WidgetDetailResponse, 'assets_base' | 'config' | 'origin' | 'dimensions'> & {
  type: string;
};

/** Per-widget slice. Only `state`, `data` and `timestamp` come from widget.update. */
export interface WidgetEntry {
  meta?: WidgetMeta;
  state: string;
  data: unknown;
  viewModel?: Record<string, unknown>;
  timestamp: string;
}

export interface ScreenSlice {
  currentScreen: number;
  totalScreens: number;
  intervalSeconds: number;
  widgets: ScreenRotateWidget[];
}

/** Carousel state the store owns: a paused carousel queues the latest screen.rotate. */
export interface CarouselSlice {
  paused: boolean;
  pendingRotate: ScreenRotatePayload | null;
}

export interface HeaderSlice {
  timestamp: string | null;
  timezone: string;
  weather: HeaderWeather | null;
}

interface ThemeSlice {
  theme: string;
}

export type VideoSlice = VideoStatePayload;
export type AudioSlice = AudioStatePayload;
export type VoiceSlice = VoiceStatePayload;

export interface SystemSlice {
  connection: 'connecting' | 'connected' | 'disconnected';
  online: boolean;
  serverTime: string | null;
  configStatus: 'ok' | 'error';
  configError: string | null;
  providers: Record<string, ProviderHealth>;
  bootId: string | null;
}

interface VisibilitySlice {
  /** False while the document is hidden or the page is being unloaded. */
  visible: boolean;
}

export interface DisplayData {
  screen: ScreenSlice;
  widgets: Record<string, WidgetEntry>;
  header: HeaderSlice;
  theme: ThemeSlice;
  video: VideoSlice;
  audio: AudioSlice;
  voice: VoiceSlice;
  system: SystemSlice;
  visibility: VisibilitySlice;
  /** Rotation layout of every screen from GET api/screens. Empty until loaded. */
  layout: ScreenLayout[];
  /** Bumped on widget.reload and on config or boot changes; the carousel refetches the layout. */
  layoutVersion: number;
  /** Count of widget.reload events per widget type. A mounted widget re-hydrates when its type's count changes. */
  widgetReloads: Record<string, number>;
  carousel: CarouselSlice;
  /** Whether the full-screen live canvas layout editor is open. */
  layoutEditorOpen: boolean;
}
