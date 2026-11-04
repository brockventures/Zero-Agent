export interface EntityState {
  entity_id: string;
  state: string;
  attributes: Record<string, unknown>;
  last_changed?: string;
  last_updated?: string;
}

export interface LovelaceCard {
  type: string;
  title?: string;
  entity?: string;
  entities?: (string | { entity?: string; type?: string; name?: string; icon?: string; [k: string]: unknown })[];
  cards?: LovelaceCard[];
  name?: string;
  icon?: string;
  image?: string;
  camera_image?: string;
  tap_action?: Record<string, unknown>;
  columns?: number;
  [k: string]: unknown;
}

export interface HomeAssistantSnapshot {
  title?: string;
  view?: {
    title?: string;
    sections?: { title?: string; cards?: LovelaceCard[] }[];
    cards?: LovelaceCard[];
  };
  cards?: LovelaceCard[];
  entities?: Record<string, EntityState>;
  last_updated?: string;
}

export interface ActionPayload {
  domain: string;
  service: string;
  entity_id?: string;
  service_data?: Record<string, unknown>;
}

export interface Ctx {
  entities: Record<string, EntityState>;
  inFlight: Record<string, boolean>;
  sendAction: (payload: ActionPayload) => Promise<void>;
  refreshCamera: (entityId: string) => void;
  cameraTimestamps: Record<string, number>;
}

export function getDomain(entityId: string): string {
  if (!entityId) {
    return '';
  }
  return entityId.split('.')[0].toLowerCase();
}

export function getEntityName(entity: EntityState | undefined, entityId: string, customName?: string): string {
  if (customName && customName.trim() !== '') {
    return customName;
  }
  if (!entityId) {
    return '';
  }
  if (entity?.attributes?.friendly_name && typeof entity.attributes.friendly_name === 'string') {
    return entity.attributes.friendly_name;
  }
  const parts = entityId.split('.');
  return parts.length > 1 ? parts[1].replace(/_/g, ' ') : entityId;
}

const DOMAIN_ICONS: Record<string, string | ((s?: string) => string)> = {
  light: (s) => (s === 'on' ? '💡' : '○'),
  switch: (s) => (s === 'on' ? '🔌' : '○'),
  fan: (s) => (s === 'on' ? '🌀' : '🪭'),
  cover: (s) => (s === 'open' ? '🪟' : '🚪'),
  lock: (s) => (s === 'locked' ? '🔒' : '🔓'),
  climate: '🌡️',
  media_player: (s) => (s === 'playing' ? '🔊' : '🎵'),
  camera: '📹',
  scene: '🎬',
  script: '⚡',
  sensor: '📊',
  binary_sensor: (s) => (s === 'on' ? '🔔' : '🔕'),
};

export function getDomainIcon(domain: string, state?: string): string {
  const icon = DOMAIN_ICONS[domain];
  return typeof icon === 'function' ? icon(state) : (icon ?? '⚙️');
}

export async function dispatchWidgetAction(
  widgetId: string,
  payload: ActionPayload,
  fetchImpl = fetch,
): Promise<unknown> {
  const url = `/api/widgets/${encodeURIComponent(widgetId)}/action`;
  const body: Record<string, unknown> = { domain: payload.domain, service: payload.service };
  if (payload.entity_id) {
    body.entity_id = payload.entity_id;
  }
  if (payload.service_data) {
    body.service_data = {
      ...(payload.entity_id ? { entity_id: payload.entity_id } : {}),
      ...payload.service_data,
    };
  } else if (payload.entity_id) {
    body.service_data = { entity_id: payload.entity_id };
  }
  const res = await fetchImpl(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const errText = await res.text().catch(() => '');
    throw new Error(`Widget action failed (HTTP ${res.status}): ${errText}`);
  }
  return await res.json().catch(() => ({}));
}

export function applyOptimisticUpdate(
  curr: EntityState,
  domain: string,
  service: string,
  data?: Record<string, unknown>,
): EntityState {
  let state = curr.state;
  const attrs = { ...curr.attributes };

  if (domain === 'light' || domain === 'switch' || domain === 'fan') {
    if (service === 'toggle') {
      state = curr.state === 'on' ? 'off' : 'on';
    } else if (service === 'turn_on') {
      state = 'on';
      if (data?.brightness !== undefined) {
        attrs.brightness = data.brightness;
      }
      if (data?.percentage !== undefined) {
        attrs.percentage = data.percentage;
      }
    } else if (service === 'turn_off') {
      state = 'off';
    } else if (service === 'set_percentage' && data?.percentage !== undefined) {
      attrs.percentage = data.percentage;
      state = Number(data.percentage) > 0 ? 'on' : 'off';
    }
  } else if (domain === 'lock') {
    state = service === 'lock' ? 'locked' : 'unlocked';
  } else if (domain === 'cover') {
    if (service === 'open_cover') {
      state = 'open';
    } else if (service === 'close_cover') {
      state = 'closed';
    }
  } else if (domain === 'climate') {
    if (data?.temperature !== undefined) {
      attrs.temperature = data.temperature;
    }
    if (data?.hvac_mode !== undefined) {
      state = String(data.hvac_mode);
    }
  } else if (domain === 'media_player') {
    if (service === 'media_play') {
      state = 'playing';
    } else if (service === 'media_pause') {
      state = 'paused';
    } else if (service === 'media_play_pause') {
      state = curr.state === 'playing' ? 'paused' : 'playing';
    }
  }

  return { ...curr, state, attributes: attrs };
}

const BINARY_LABELS: Record<string, [string, string]> = {
  door: ['Open', 'Closed'],
  window: ['Open', 'Closed'],
  occupancy: ['Occupied', 'Clear'],
  presence: ['Home', 'Away'],
  motion: ['Motion', 'Clear'],
};

export function getBinaryLabel(isOn: boolean, dc?: string): string {
  const pair = dc ? BINARY_LABELS[dc] : undefined;
  if (pair) {
    return isOn ? pair[0] : pair[1];
  }
  return isOn ? 'On' : 'Off';
}

export function formatStateLabel(domain: string, state: string, attrs?: Record<string, unknown>): string {
  if (domain === 'light') {
    const b = state === 'on' && typeof attrs?.brightness === 'number' ? Math.round((attrs.brightness / 255) * 100) : null;
    return b !== null ? `On · ${b}%` : state === 'on' ? 'On' : 'Off';
  }
  if (domain === 'climate' && attrs?.current_temperature !== undefined) {
    return `${state} · ${attrs.current_temperature}°`;
  }
  if (domain === 'media_player' && attrs?.media_title) {
    return attrs.media_artist ? `${attrs.media_title} - ${attrs.media_artist}` : String(attrs.media_title);
  }
  if (domain === 'sensor') {
    return `${state} ${attrs?.unit_of_measurement || ''}`.trim();
  }
  return state;
}
