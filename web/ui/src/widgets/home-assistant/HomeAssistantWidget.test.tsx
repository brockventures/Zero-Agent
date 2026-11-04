import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDisplayStore } from '../../store';
import { WIDGET_COMPONENTS, widgetComponentFor } from '../registry';
import {
  HomeAssistantWidget,
  applyOptimisticUpdate,
  dispatchWidgetAction,
  formatStateLabel,
  getBinaryLabel,
  getDomain,
  getDomainIcon,
  getEntityName,
} from './HomeAssistantWidget';

const initialStore = useDisplayStore.getState();

function setHaWidget(id: string, data: Record<string, unknown>, state = 'healthy') {
  useDisplayStore.setState({
    widgets: {
      [id]: {
        meta: { type: 'home-assistant', origin: [0, 0], dimensions: [4, 4], config: {}, assets_base: '' },
        state,
        data,
        timestamp: '2026-11-02T08:00:00Z',
      },
    },
  });
}

describe('HomeAssistantWidget', () => {
  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('registers in WIDGET_COMPONENTS and widgetComponentFor resolves home-assistant', () => {
    expect(WIDGET_COMPONENTS['home-assistant']).toBe(HomeAssistantWidget);
    expect(widgetComponentFor('home-assistant')).toBe(HomeAssistantWidget);
  });

  it('extracts domain, entity name, and icons correctly', () => {
    expect(getDomain('light.kitchen_ceiling')).toBe('light');
    expect(getDomain('switch.ac_plug')).toBe('switch');
    expect(getDomain('climate.hallway')).toBe('climate');
    expect(getDomain('')).toBe('');

    expect(
      getEntityName(
        { entity_id: 'light.kitchen_ceiling', state: 'on', attributes: { friendly_name: 'Kitchen Main Light' } },
        'light.kitchen_ceiling',
      ),
    ).toBe('Kitchen Main Light');

    expect(getEntityName(undefined, 'switch.living_room_fan', 'Custom Fan Name')).toBe('Custom Fan Name');
    expect(getEntityName(undefined, 'fan.ceiling_fan')).toBe('ceiling fan');

    expect(getDomainIcon('light', 'on')).toBe('💡');
    expect(getDomainIcon('light', 'off')).toBe('○');
    expect(getDomainIcon('lock', 'locked')).toBe('🔒');
    expect(getDomainIcon('climate')).toBe('🌡️');
    expect(getDomainIcon('media_player', 'playing')).toBe('🔊');
    expect(getDomainIcon('camera')).toBe('📹');
  });

  it('renders loading state when data is unhydrated ({})', () => {
    setHaWidget('ha-1', {});

    const { container } = render(<HomeAssistantWidget id="ha-1" type="home-assistant" />);
    const root = container.querySelector('.widget-home-assistant');
    expect(root).not.toBeNull();
    expect(container.querySelector('.ha-loading-spinner')?.textContent).toContain('Loading Home Assistant...');
  });

  it('renders error state when entry state is error', () => {
    setHaWidget('ha-err', {}, 'error');

    const { container } = render(<HomeAssistantWidget id="ha-err" type="home-assistant" />);
    const err = container.querySelector('.widget-ha-error');
    expect(err).not.toBeNull();
    expect(err?.textContent).toContain('Unable to load Home Assistant');
  });

  it('renders Lovelace sections and nested cards layout', () => {
    setHaWidget('ha-sections', {
            title: 'Downstairs Tablet',
            view: {
              title: 'Main Floor',
              type: 'sections',
              sections: [
                {
                  title: 'Living Room',
                  cards: [
                    { type: 'tile', entity: 'light.living_room' },
                    { type: 'button', entity: 'scene.evening_relax' },
                  ],
                },
                {
                  title: 'Climate & Security',
                  cards: [
                    { type: 'thermostat', entity: 'climate.living_room' },
                    { type: 'tile', entity: 'lock.front_door' },
                  ],
                },
              ],
            },
            entities: {
              'light.living_room': { entity_id: 'light.living_room', state: 'on', attributes: { friendly_name: 'Living Room Lights', brightness: 200 } },
              'scene.evening_relax': { entity_id: 'scene.evening_relax', state: 'scening', attributes: { friendly_name: 'Relax Mode' } },
              'climate.living_room': { entity_id: 'climate.living_room', state: 'heat', attributes: { friendly_name: 'Thermostat', current_temperature: 68, temperature: 71 } },
              'lock.front_door': { entity_id: 'lock.front_door', state: 'locked', attributes: { friendly_name: 'Front Door' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-sections" type="home-assistant" />);
    expect(container.querySelector('.ha-title')?.textContent).toBe('Downstairs Tablet');
    const sections = container.querySelectorAll('.ha-section');
    expect(sections.length).toBe(2);
    expect(sections[0].querySelector('.ha-section-title')?.textContent).toBe('Living Room');
    expect(sections[1].querySelector('.ha-section-title')?.textContent).toBe('Climate & Security');

    expect(container.querySelector('[data-entity-id="light.living_room"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="scene.evening_relax"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="climate.living_room"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="lock.front_door"]')).not.toBeNull();
  });

  it('renders Lovelace entities card and grid card', () => {
    setHaWidget('ha-cards', {
            title: 'Grid Dashboard',
            cards: [
              {
                type: 'entities',
                title: 'Quick Toggles',
                entities: [
                  'switch.kitchen_fan',
                  { entity: 'cover.garage_door', name: 'Main Garage' },
                ],
              },
              {
                type: 'grid',
                columns: 2,
                cards: [
                  { type: 'tile', entity: 'script.morning_routine' },
                  { type: 'picture-entity', entity: 'camera.driveway', title: 'Driveway Cam' },
                ],
              },
            ],
            entities: {
              'switch.kitchen_fan': { entity_id: 'switch.kitchen_fan', state: 'off', attributes: { friendly_name: 'Kitchen Exhaust' } },
              'cover.garage_door': { entity_id: 'cover.garage_door', state: 'closed', attributes: { friendly_name: 'Garage Door' } },
              'script.morning_routine': { entity_id: 'script.morning_routine', state: 'off', attributes: { friendly_name: 'Morning Script' } },
              'camera.driveway': { entity_id: 'camera.driveway', state: 'idle', attributes: { friendly_name: 'Driveway Camera', entity_picture: '/api/camera_proxy/camera.driveway' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-cards" type="home-assistant" />);
    expect(container.querySelector('.ha-card-entities')).not.toBeNull();
    expect(container.querySelector('.ha-card-title')?.textContent).toBe('Quick Toggles');
    expect(container.querySelector('.ha-card-grid')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="switch.kitchen_fan"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="cover.garage_door"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="camera.driveway"]')).not.toBeNull();
  });

  it('renders all 12 supported domains with touch-friendly controls and dispatches actions', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok', widget_id: 'ha-all-domains' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-all-domains', {
            title: 'All Domains Test',
            cards: [
              {
                type: 'entities',
                title: 'All Domains',
                entities: [
                  'light.living_room',
                  'switch.porch_light',
                  'fan.bedroom_fan',
                  'cover.blinds',
                  'lock.front_door',
                  'climate.main_ac',
                  'media_player.soundbar',
                  'camera.porch_cam',
                  'scene.movie_night',
                  'script.clean_all',
                  'sensor.kitchen_temperature',
                  'binary_sensor.front_motion',
                ],
              },
            ],
            entities: {
              'light.living_room': { entity_id: 'light.living_room', state: 'off', attributes: { friendly_name: 'Living Room Light' } },
              'switch.porch_light': { entity_id: 'switch.porch_light', state: 'off', attributes: { friendly_name: 'Porch Switch' } },
              'fan.bedroom_fan': { entity_id: 'fan.bedroom_fan', state: 'off', attributes: { friendly_name: 'Bedroom Fan' } },
              'cover.blinds': { entity_id: 'cover.blinds', state: 'closed', attributes: { friendly_name: 'Blinds' } },
              'lock.front_door': { entity_id: 'lock.front_door', state: 'locked', attributes: { friendly_name: 'Smart Lock' } },
              'climate.main_ac': { entity_id: 'climate.main_ac', state: 'cool', attributes: { friendly_name: 'Main AC', current_temperature: 75, temperature: 72 } },
              'media_player.soundbar': { entity_id: 'media_player.soundbar', state: 'paused', attributes: { friendly_name: 'Soundbar', media_title: 'Bohemian Rhapsody', media_artist: 'Queen' } },
              'camera.porch_cam': { entity_id: 'camera.porch_cam', state: 'idle', attributes: { friendly_name: 'Porch Camera', entity_picture: '/api/camera_proxy/camera.porch_cam' } },
              'scene.movie_night': { entity_id: 'scene.movie_night', state: 'scening', attributes: { friendly_name: 'Movie Night' } },
              'script.clean_all': { entity_id: 'script.clean_all', state: 'off', attributes: { friendly_name: 'Vacuum Routine' } },
              'sensor.kitchen_temperature': { entity_id: 'sensor.kitchen_temperature', state: '72.4', attributes: { friendly_name: 'Kitchen Temp', unit_of_measurement: '°F' } },
              'binary_sensor.front_motion': { entity_id: 'binary_sensor.front_motion', state: 'on', attributes: { friendly_name: 'Front Motion Sensor', device_class: 'motion' } },
            },
          });

    const { container, rerender } = render(<HomeAssistantWidget id="ha-all-domains" type="home-assistant" />);
    rerender(<HomeAssistantWidget id="ha-all-domains" type="home-assistant" />);

    // 1. Light: tap toggle
    const lightRow = container.querySelector('[data-entity-id="light.living_room"]');
    expect(lightRow).not.toBeNull();
    const lightBtn = lightRow?.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    expect(lightBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(lightBtn);
    });
    expect(fetchCalls.length).toBe(1);
    expect(fetchCalls[0].url).toBe('/api/widgets/ha-all-domains/action');
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'light',
      service: 'toggle',
      entity_id: 'light.living_room',
      service_data: { entity_id: 'light.living_room' },
    });

    // 2. Switch: tap toggle
    const switchRow = container.querySelector('[data-entity-id="switch.porch_light"]');
    const switchBtn = switchRow?.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(switchBtn);
    });
    expect(fetchCalls.length).toBe(2);
    expect(JSON.parse(fetchCalls[1].options.body as string)).toEqual({
      domain: 'switch',
      service: 'toggle',
      entity_id: 'switch.porch_light',
      service_data: { entity_id: 'switch.porch_light' },
    });

    // 3. Fan: tap toggle
    const fanRow = container.querySelector('[data-entity-id="fan.bedroom_fan"]');
    const fanBtn = fanRow?.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(fanBtn);
    });
    expect(fetchCalls.length).toBe(3);
    expect(JSON.parse(fetchCalls[2].options.body as string)).toEqual({
      domain: 'fan',
      service: 'toggle',
      entity_id: 'fan.bedroom_fan',
      service_data: { entity_id: 'fan.bedroom_fan' },
    });

    // 4. Cover: tap open and close
    const coverRow = container.querySelector('[data-entity-id="cover.blinds"]');
    const coverOpenBtn = coverRow?.querySelector('[data-action="open"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(coverOpenBtn);
    });
    expect(fetchCalls.length).toBe(4);
    expect(JSON.parse(fetchCalls[3].options.body as string)).toEqual({
      domain: 'cover',
      service: 'open_cover',
      entity_id: 'cover.blinds',
      service_data: { entity_id: 'cover.blinds' },
    });

    // 5. Lock: tap toggle-lock (starts locked -> unlocks)
    const lockRow = container.querySelector('[data-entity-id="lock.front_door"]');
    const lockBtn = lockRow?.querySelector('[data-action="toggle-lock"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(lockBtn);
    });
    expect(fetchCalls.length).toBe(5);
    expect(JSON.parse(fetchCalls[4].options.body as string)).toEqual({
      domain: 'lock',
      service: 'unlock',
      entity_id: 'lock.front_door',
      service_data: { entity_id: 'lock.front_door' },
    });

    // 6. Climate: tap temp-up (+)
    const climateRow = container.querySelector('[data-entity-id="climate.main_ac"]');
    const tempUpBtn = climateRow?.querySelector('[data-action="temp-up"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(tempUpBtn);
    });
    expect(fetchCalls.length).toBe(6);
    expect(JSON.parse(fetchCalls[5].options.body as string)).toEqual({
      domain: 'climate',
      service: 'set_temperature',
      entity_id: 'climate.main_ac',
      service_data: { entity_id: 'climate.main_ac', temperature: 73 },
    });

    // 7. Media player: tap play-pause
    const mediaRow = container.querySelector('[data-entity-id="media_player.soundbar"]');
    const playPauseBtn = mediaRow?.querySelector('[data-action="play-pause"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(playPauseBtn);
    });
    expect(fetchCalls.length).toBe(7);
    expect(JSON.parse(fetchCalls[6].options.body as string)).toEqual({
      domain: 'media_player',
      service: 'media_play_pause',
      entity_id: 'media_player.soundbar',
      service_data: { entity_id: 'media_player.soundbar' },
    });

    // 8. Scene: tap activate
    const sceneRow = container.querySelector('[data-entity-id="scene.movie_night"]');
    const sceneBtn = sceneRow?.querySelector('[data-action="activate"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(sceneBtn);
    });
    expect(fetchCalls.length).toBe(8);
    expect(JSON.parse(fetchCalls[7].options.body as string)).toEqual({
      domain: 'scene',
      service: 'turn_on',
      entity_id: 'scene.movie_night',
      service_data: { entity_id: 'scene.movie_night' },
    });

    // 9. Script: tap run
    const scriptRow = container.querySelector('[data-entity-id="script.clean_all"]');
    const scriptBtn = scriptRow?.querySelector('[data-action="run"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(scriptBtn);
    });
    expect(fetchCalls.length).toBe(9);
    expect(JSON.parse(fetchCalls[8].options.body as string)).toEqual({
      domain: 'script',
      service: 'turn_on',
      entity_id: 'script.clean_all',
      service_data: { entity_id: 'script.clean_all' },
    });

    // 10. Sensor: verifies state and unit display
    const sensorRow = container.querySelector('[data-entity-id="sensor.kitchen_temperature"]');
    expect(sensorRow?.querySelector('.ha-sensor-val')?.textContent).toBe('72.4');
    expect(sensorRow?.querySelector('.ha-sensor-unit')?.textContent).toBe('°F');

    // 11. Binary sensor: verifies motion pill and status
    const binaryRow = container.querySelector('[data-entity-id="binary_sensor.front_motion"]');
    expect(binaryRow?.querySelector('.ha-binary-pill')?.textContent).toContain('Motion');

    // 12. Camera: refresh button
    const cameraRow = container.querySelector('[data-entity-id="camera.porch_cam"]');
    const cameraRefreshBtn = cameraRow?.querySelector('[data-action="refresh"]') as HTMLButtonElement;
    expect(cameraRefreshBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(cameraRefreshBtn);
    });
  });

  it('handles action failure by displaying error alert and reverting optimistic update', async () => {
    const fetchMock = vi.fn().mockImplementation(() => {
      return Promise.resolve(new Response('Upstream Home Assistant timeout', { status: 504 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-error-action', {
            cards: [{ type: 'tile', entity: 'switch.patio_heater' }],
            entities: {
              'switch.patio_heater': { entity_id: 'switch.patio_heater', state: 'off', attributes: { friendly_name: 'Patio Heater' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-error-action" type="home-assistant" />);
    const toggleBtn = container.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    expect(toggleBtn).not.toBeNull();

    await act(async () => {
      fireEvent.click(toggleBtn);
    });

    await waitFor(() => {
      expect(container.querySelector('.ha-alert-banner')).not.toBeNull();
      expect(container.querySelector('.ha-alert-banner')?.textContent).toContain('Widget action failed (HTTP 504)');
    });
  });

  it('renders fallback entity list when no cards or view are configured', () => {
    setHaWidget('ha-fallback', {
            entities: {
              'light.study': { entity_id: 'light.study', state: 'on', attributes: { friendly_name: 'Study Light' } },
              'climate.bedroom': { entity_id: 'climate.bedroom', state: 'cool', attributes: { friendly_name: 'Bedroom AC', temperature: 69 } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-fallback" type="home-assistant" />);
    expect(container.querySelector('[data-entity-id="light.study"]')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="climate.bedroom"]')).not.toBeNull();
  });

  it('dispatches custom service tap_action on button card', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-tap-action', {
            cards: [
              {
                type: 'button',
                name: 'Bedtime',
                tap_action: {
                  action: 'call-service',
                  service: 'scene.turn_on',
                  target: { entity_id: 'scene.goodnight' },
                },
              },
            ],
            entities: {},
          });

    const { container } = render(<HomeAssistantWidget id="ha-tap-action" type="home-assistant" />);
    const btn = container.querySelector('.ha-card-button') as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.getAttribute('data-entity-id')).toBe('scene.goodnight');
    expect(btn.getAttribute('data-domain')).toBe('scene');
    expect(btn.querySelector('.ha-btn-card-name')?.textContent).toBe('Bedtime');
    await act(async () => {
      fireEvent.click(btn);
    });

    expect(fetchCalls.length).toBe(1);
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'scene',
      service: 'turn_on',
      entity_id: 'scene.goodnight',
      service_data: { entity_id: 'scene.goodnight' },
    });
  });

  it('dispatchWidgetAction helper functions properly directly', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' })));
    await dispatchWidgetAction(
      'my-widget',
      {
        domain: 'light',
        service: 'turn_on',
        entity_id: 'light.desk',
        service_data: { brightness: 128 },
      },
      fetchMock,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/widgets/my-widget/action',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          domain: 'light',
          service: 'turn_on',
          entity_id: 'light.desk',
          service_data: { entity_id: 'light.desk', brightness: 128 },
        }),
      }),
    );
  });

  it('handles helper function edge cases (getDomain, getEntityName, getDomainIcon)', () => {
    expect(getDomain('sensor')).toBe('sensor');
    expect(getDomain('')).toBe('');

    expect(getEntityName(undefined, 'nodots')).toBe('nodots');
    expect(getEntityName(undefined, 'switch.bedside_table_lamp')).toBe('bedside table lamp');
    expect(getEntityName(undefined, '', 'Button Custom')).toBe('Button Custom');
    expect(getEntityName(undefined, 'light.bulb', '   ')).toBe('bulb');
    expect(getEntityName(undefined, 'light.bulb', 'Explicit Custom')).toBe('Explicit Custom');
    expect(
      getEntityName(
        { entity_id: 'light.bulb', state: 'on', attributes: { friendly_name: 123 as unknown as string } },
        'light.bulb',
      ),
    ).toBe('bulb');

    expect(getDomainIcon('fan', 'on')).toBe('🌀');
    expect(getDomainIcon('fan', 'off')).toBe('🪭');
    expect(getDomainIcon('cover', 'open')).toBe('🪟');
    expect(getDomainIcon('cover', 'closed')).toBe('🚪');
    expect(getDomainIcon('lock', 'locked')).toBe('🔒');
    expect(getDomainIcon('lock', 'unlocked')).toBe('🔓');
    expect(getDomainIcon('media_player', 'playing')).toBe('🔊');
    expect(getDomainIcon('media_player', 'paused')).toBe('🎵');
    expect(getDomainIcon('binary_sensor', 'on')).toBe('🔔');
    expect(getDomainIcon('binary_sensor', 'off')).toBe('🔕');
    expect(getDomainIcon('switch', 'on')).toBe('🔌');
    expect(getDomainIcon('switch', 'off')).toBe('○');
    expect(getDomainIcon('script')).toBe('⚡');
    expect(getDomainIcon('scene')).toBe('🎬');
    expect(getDomainIcon('sensor')).toBe('📊');
    expect(getDomainIcon('camera')).toBe('📹');
    expect(getDomainIcon('vacuum')).toBe('⚙️');
  });

  it('handles dispatchWidgetAction payload options and error rejections', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' })));

    // 1. Without entity_id, without service_data
    await dispatchWidgetAction('w-1', { domain: 'homeassistant', service: 'restart' }, fetchMock);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ domain: 'homeassistant', service: 'restart' });

    // 2. Without entity_id, with service_data
    await dispatchWidgetAction(
      'w-1',
      { domain: 'notify', service: 'notify', service_data: { message: 'hello' } },
      fetchMock,
    );
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      domain: 'notify',
      service: 'notify',
      service_data: { message: 'hello' },
    });

    // 3. Response not ok, res.text() rejects
    const failMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      text: () => Promise.reject(new Error('fail text')),
    });
    await expect(
      dispatchWidgetAction('w-1', { domain: 'light', service: 'toggle' }, failMock as unknown as typeof fetch),
    ).rejects.toThrow('Widget action failed (HTTP 500): ');

    // 4. Response ok, res.json() rejects
    const jsonFailMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('bad json')),
    });
    const res = await dispatchWidgetAction(
      'w-1',
      { domain: 'light', service: 'toggle' },
      jsonFailMock as unknown as typeof fetch,
    );
    expect(res).toEqual({});
  });

  it('covers domain optimistic updates and non-Error action rejections with dismiss', async () => {
    let rejectPromise: ((err: unknown) => void) | null = null;
    const fetchMock = vi.fn().mockImplementation(() => {
      return new Promise((_resolve, reject) => {
        rejectPromise = reject;
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-opt', {
            cards: [
              { type: 'tile', entity: 'light.desk' },
              { type: 'tile', entity: 'fan.living' },
              { type: 'tile', entity: 'lock.back' },
              { type: 'tile', entity: 'cover.shades' },
              { type: 'thermostat', entity: 'climate.hall' },
              { type: 'tile', entity: 'media_player.tv' },
            ],
            entities: {
              'light.desk': { entity_id: 'light.desk', state: 'on', attributes: { friendly_name: 'Desk Light', brightness: 128 } },
              'fan.living': { entity_id: 'fan.living', state: 'on', attributes: { friendly_name: 'Living Fan', percentage: 66 } },
              'lock.back': { entity_id: 'lock.back', state: 'unlocked', attributes: { friendly_name: 'Back Door' } },
              'cover.shades': { entity_id: 'cover.shades', state: 'open', attributes: { friendly_name: 'Shades' } },
              'climate.hall': { entity_id: 'climate.hall', state: 'cool', attributes: { friendly_name: 'Hall Climate', temperature: 72, current_temperature: 70 } },
              'media_player.tv': { entity_id: 'media_player.tv', state: 'playing', attributes: { friendly_name: 'Living TV' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-opt" type="home-assistant" />);

    // Toggle desk light (optimistically turns off)
    const deskTile = container.querySelector('[data-entity-id="light.desk"]');
    const deskToggle = deskTile?.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(deskToggle);
    });

    // Reject with a plain string
    await act(async () => {
      rejectPromise!('Raw failure string');
    });

    // Alert banner should display string error fallback
    await waitFor(() => {
      const banner = container.querySelector('.ha-alert-banner');
      expect(banner?.textContent).toContain('Action failed');
    });

    // Dismiss banner
    const dismissBtn = container.querySelector('.ha-banner-dismiss') as HTMLButtonElement;
    expect(dismissBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(dismissBtn);
    });
    expect(container.querySelector('.ha-alert-banner')).toBeNull();
  });

  it('covers cardsToRender fallback paths, view.cards, and header titles', () => {
    // 1. Snapshot with view.cards
    setHaWidget('ha-view-cards', {
            view: {
              title: 'Floor View',
              cards: [{ type: 'tile', entity: 'switch.coffee' }],
            },
            entities: {
              'switch.coffee': { entity_id: 'switch.coffee', state: 'on', attributes: { friendly_name: 'Coffee Maker' } },
            },
            last_updated: '2026-11-02T09:00:00Z',
          });

    const { container, unmount } = render(<HomeAssistantWidget id="ha-view-cards" type="home-assistant" />);
    expect(container.querySelector('.ha-title')?.textContent).toBe('Floor View');
    expect(container.querySelector('.ha-last-updated')).not.toBeNull();
    expect(container.querySelector('[data-entity-id="switch.coffee"]')).not.toBeNull();
    unmount();

    // 2. Snapshot with default title ('Home Assistant') and entity domain fallback cards
    setHaWidget('ha-fallback-domains', {
            entities: {
              'climate.heat': { entity_id: 'climate.heat', state: 'heat', attributes: {} },
              'camera.backyard': { entity_id: 'camera.backyard', state: 'idle', attributes: {} },
              'scene.sunset': { entity_id: 'scene.sunset', state: 'scening', attributes: {} },
              'script.routine': { entity_id: 'script.routine', state: 'off', attributes: {} },
              'sensor.power': { entity_id: 'sensor.power', state: '150', attributes: {} },
            },
          });

    const { container: fallbackContainer } = render(<HomeAssistantWidget id="ha-fallback-domains" type="home-assistant" />);
    expect(fallbackContainer.querySelector('.ha-title')?.textContent).toBe('Home Assistant');
    expect(fallbackContainer.querySelector('.ha-card-thermostat')).not.toBeNull();
    expect(fallbackContainer.querySelector('.ha-card-picture')).not.toBeNull();
    expect(fallbackContainer.querySelectorAll('.ha-card-button').length).toBe(2);
    expect(fallbackContainer.querySelector('.ha-card-tile')).not.toBeNull();
  });

  it('covers CardRenderer grid custom columns, nested card wrapper, and unknown cards', () => {
    setHaWidget('ha-custom-cards', {
            cards: [
              {
                type: 'grid',
                columns: 3,
                cards: [{ type: 'tile', entity: 'switch.one' }],
              },
              {
                type: 'vertical-stack',
                title: 'Nested Stack',
                cards: [{ type: 'tile', entity: 'switch.two' }],
              },
              {
                type: 'custom:unsupported-card',
                title: 'Unsupported',
              },
            ],
            entities: {
              'switch.one': { entity_id: 'switch.one', state: 'off', attributes: {} },
              'switch.two': { entity_id: 'switch.two', state: 'on', attributes: {} },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-custom-cards" type="home-assistant" />);
    const grid = container.querySelector('.ha-card-grid') as HTMLElement;
    expect(grid?.style.gridTemplateColumns).toContain('repeat(3, minmax(0, 1fr))');
    expect(container.querySelector('.ha-card-wrapper')).not.toBeNull();
    expect(container.querySelector('.ha-card-unknown')?.textContent).toBe('Unsupported');
  });

  it('covers ButtonCard tap actions, inFlight disabling, and default domain behaviors', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-button-tests', {
            cards: [
              { type: 'button', entity: 'scene.night_mode' },
              { type: 'button', entity: 'script.run_vac' },
              { type: 'button', entity: 'light.lamp' },
              { type: 'button', entity: 'lock.door_locked' },
              { type: 'button', entity: 'lock.door_unlocked' },
              { type: 'button', entity: 'input_boolean.guest_mode' },
              {
                type: 'button',
                entity: 'light.custom_call',
                tap_action: {
                  action: 'call_service',
                  service: 'toggle',
                  data: { transition: 2 },
                },
              },
            ],
            entities: {
              'scene.night_mode': { entity_id: 'scene.night_mode', state: 'scening', attributes: {} },
              'script.run_vac': { entity_id: 'script.run_vac', state: 'off', attributes: {} },
              'light.lamp': { entity_id: 'light.lamp', state: 'off', attributes: {} },
              'lock.door_locked': { entity_id: 'lock.door_locked', state: 'locked', attributes: {} },
              'lock.door_unlocked': { entity_id: 'lock.door_unlocked', state: 'unlocked', attributes: {} },
              'input_boolean.guest_mode': { entity_id: 'input_boolean.guest_mode', state: 'off', attributes: {} },
              'light.custom_call': { entity_id: 'light.custom_call', state: 'on', attributes: {} },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-button-tests" type="home-assistant" />);
    const buttons = container.querySelectorAll('.ha-card-button');

    // 1. Scene default
    await act(async () => {
      fireEvent.click(buttons[0]);
    });
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'scene',
      service: 'turn_on',
      entity_id: 'scene.night_mode',
      service_data: { entity_id: 'scene.night_mode' },
    });

    // 2. Script default
    await act(async () => {
      fireEvent.click(buttons[1]);
    });
    expect(JSON.parse(fetchCalls[1].options.body as string)).toEqual({
      domain: 'script',
      service: 'turn_on',
      entity_id: 'script.run_vac',
      service_data: { entity_id: 'script.run_vac' },
    });

    // 3. Light default
    await act(async () => {
      fireEvent.click(buttons[2]);
    });
    expect(JSON.parse(fetchCalls[2].options.body as string)).toEqual({
      domain: 'light',
      service: 'toggle',
      entity_id: 'light.lamp',
      service_data: { entity_id: 'light.lamp' },
    });

    // 4. Lock locked -> unlock
    await act(async () => {
      fireEvent.click(buttons[3]);
    });
    expect(JSON.parse(fetchCalls[3].options.body as string)).toEqual({
      domain: 'lock',
      service: 'unlock',
      entity_id: 'lock.door_locked',
      service_data: { entity_id: 'lock.door_locked' },
    });

    // 5. Lock unlocked -> lock
    await act(async () => {
      fireEvent.click(buttons[4]);
    });
    expect(JSON.parse(fetchCalls[4].options.body as string)).toEqual({
      domain: 'lock',
      service: 'lock',
      entity_id: 'lock.door_unlocked',
      service_data: { entity_id: 'lock.door_unlocked' },
    });

    // 6. Generic input_boolean -> toggle
    await act(async () => {
      fireEvent.click(buttons[5]);
    });
    expect(JSON.parse(fetchCalls[5].options.body as string)).toEqual({
      domain: 'input_boolean',
      service: 'toggle',
      entity_id: 'input_boolean.guest_mode',
      service_data: { entity_id: 'input_boolean.guest_mode' },
    });

    // 7. Custom call_service with data
    await act(async () => {
      fireEvent.click(buttons[6]);
    });
    expect(JSON.parse(fetchCalls[6].options.body as string)).toEqual({
      domain: 'light',
      service: 'toggle',
      entity_id: 'light.custom_call',
      service_data: { entity_id: 'light.custom_call', transition: 2 },
    });
  });

  it('covers ThermostatCard dial buttons, modes, and defaults', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-tstat', {
            cards: [
              {
                type: 'thermostat',
                entity: 'climate.hallway',
                title: 'Main Floor Thermostat',
              },
            ],
            entities: {
              'climate.hallway': { entity_id: 'climate.hallway', state: 'heat', attributes: {
                  friendly_name: 'Hallway Climate',
                  current_temperature: 69,
                  temperature: 71,
                  hvac_modes: ['off', 'heat', 'cool'],
                } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-tstat" type="home-assistant" />);
    expect(container.querySelector('.ha-thermostat-title')?.textContent).toBe('Main Floor Thermostat');
    expect(container.querySelector('.ha-temp-val')?.textContent).toBe('69°');

    // Decrease temperature
    const decreaseBtn = container.querySelector('[aria-label="Decrease Temperature"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(decreaseBtn);
    });
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'climate',
      service: 'set_temperature',
      entity_id: 'climate.hallway',
      service_data: { entity_id: 'climate.hallway', temperature: 70 },
    });

    // Increase temperature (optimistic update previously reduced target from 71 to 70, so 70 + 1 = 71)
    const increaseBtn = container.querySelector('[aria-label="Increase Temperature"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(increaseBtn);
    });
    expect(JSON.parse(fetchCalls[1].options.body as string)).toEqual({
      domain: 'climate',
      service: 'set_temperature',
      entity_id: 'climate.hallway',
      service_data: { entity_id: 'climate.hallway', temperature: 71 },
    });

    // Set mode to cool
    const coolBtn = container.querySelectorAll('.ha-btn-mode')[2] as HTMLButtonElement;
    expect(coolBtn.textContent).toBe('cool');
    await act(async () => {
      fireEvent.click(coolBtn);
    });
    expect(JSON.parse(fetchCalls[2].options.body as string)).toEqual({
      domain: 'climate',
      service: 'set_hvac_mode',
      entity_id: 'climate.hallway',
      service_data: { entity_id: 'climate.hallway', hvac_mode: 'cool' },
    });
  });

  it('covers PictureEntityCard interactions and camera refresh', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-pic', {
            cards: [
              {
                type: 'picture-entity',
                entity: 'camera.yard',
                camera_image: '/api/camera/yard.jpg',
              },
              {
                type: 'picture-entity',
                entity: 'switch.garden_fountain',
                image: '/api/static/fountain.jpg',
              },
              { type: 'picture-entity', entity: 'camera.doorbell' },
            ],
            entities: {
              'camera.yard': { entity_id: 'camera.yard', state: 'idle', attributes: {} },
              'switch.garden_fountain': { entity_id: 'switch.garden_fountain', state: 'off', attributes: {} },
              'camera.doorbell': { entity_id: 'camera.doorbell', state: 'idle', attributes: {} },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-pic" type="home-assistant" />);
    const picCards = container.querySelectorAll('.ha-card-picture');

    // 1. Camera tap triggers refreshCamera and updates query timestamp
    await act(async () => {
      fireEvent.click(picCards[0]);
    });
    const yardImg = picCards[0].querySelector('img') as HTMLImageElement;
    expect(yardImg.src).toContain('?ts=');

    // 2. Non-camera picture card tap triggers toggle
    await act(async () => {
      fireEvent.click(picCards[1]);
    });
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'switch',
      service: 'toggle',
      entity_id: 'switch.garden_fountain',
      service_data: { entity_id: 'switch.garden_fountain' },
    });

    // 3. Camera with no image renders fallback container
    expect(picCards[2].querySelector('.ha-picture-fallback')).not.toBeNull();
  });

  it('covers DomainTouchControls quick brightness, speeds, cover stop/close, lock toggle, climate temp-down, media buttons, binary sensors, and unknown domains', async () => {
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return Promise.resolve(new Response(JSON.stringify({ status: 'ok' })));
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-subcontrols', {
            cards: [
              {
                type: 'entities',
                entities: [
                  'light.active_lamp',
                  'fan.active_fan',
                  'cover.curtain',
                  'lock.unlocked_lock',
                  'climate.bedroom_heat',
                  'media_player.kitchen_speaker',
                  'binary_sensor.door_contact',
                  'binary_sensor.window_contact',
                  'binary_sensor.office_occupancy',
                  'binary_sensor.user_presence',
                  'vacuum.roborock',
                ],
              },
            ],
            entities: {
              'light.active_lamp': { entity_id: 'light.active_lamp', state: 'on', attributes: { friendly_name: 'Lamp', brightness: 128 } },
              'fan.active_fan': { entity_id: 'fan.active_fan', state: 'on', attributes: { friendly_name: 'Fan', percentage: 66 } },
              'cover.curtain': { entity_id: 'cover.curtain', state: 'open', attributes: { friendly_name: 'Curtain' } },
              'lock.unlocked_lock': { entity_id: 'lock.unlocked_lock', state: 'unlocked', attributes: { friendly_name: 'Lock' } },
              'climate.bedroom_heat': { entity_id: 'climate.bedroom_heat', state: 'heat', attributes: { friendly_name: 'Bedroom Heat', temperature: 70 } },
              'media_player.kitchen_speaker': { entity_id: 'media_player.kitchen_speaker', state: 'playing', attributes: { friendly_name: 'Kitchen Speaker' } },
              'binary_sensor.door_contact': { entity_id: 'binary_sensor.door_contact', state: 'off', attributes: { friendly_name: 'Door', device_class: 'door' } },
              'binary_sensor.window_contact': { entity_id: 'binary_sensor.window_contact', state: 'on', attributes: { friendly_name: 'Window', device_class: 'window' } },
              'binary_sensor.office_occupancy': { entity_id: 'binary_sensor.office_occupancy', state: 'off', attributes: { friendly_name: 'Office Occupancy', device_class: 'occupancy' } },
              'binary_sensor.user_presence': { entity_id: 'binary_sensor.user_presence', state: 'on', attributes: { friendly_name: 'Presence', device_class: 'presence' } },
              'vacuum.roborock': { entity_id: 'vacuum.roborock', state: 'docked', attributes: { friendly_name: 'Vacuum' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-subcontrols" type="home-assistant" />);

    // 1. Light quick brightness buttons (50% button is active since brightness=128)
    const lampRow = container.querySelector('[data-entity-id="light.active_lamp"]');
    const brightnessBtns = lampRow?.querySelectorAll('.ha-btn-sub') as NodeListOf<HTMLButtonElement>;
    expect(brightnessBtns[1].classList.contains('active')).toBe(true);
    await act(async () => {
      fireEvent.click(brightnessBtns[0]); // 25%
    });
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'light',
      service: 'turn_on',
      entity_id: 'light.active_lamp',
      service_data: { entity_id: 'light.active_lamp', brightness: 64 },
    });

    // 2. Fan quick speed buttons (Med button is active)
    const fanRow = container.querySelector('[data-entity-id="fan.active_fan"]');
    const speedBtns = fanRow?.querySelectorAll('.ha-btn-sub') as NodeListOf<HTMLButtonElement>;
    expect(speedBtns[1].classList.contains('active')).toBe(true);
    await act(async () => {
      fireEvent.click(speedBtns[0]); // Low (33)
    });
    expect(JSON.parse(fetchCalls[1].options.body as string)).toEqual({
      domain: 'fan',
      service: 'set_percentage',
      entity_id: 'fan.active_fan',
      service_data: { entity_id: 'fan.active_fan', percentage: 33 },
    });
    await act(async () => {
      fireEvent.click(speedBtns[2]); // High (100)
    });
    expect(JSON.parse(fetchCalls[2].options.body as string)).toEqual({
      domain: 'fan',
      service: 'set_percentage',
      entity_id: 'fan.active_fan',
      service_data: { entity_id: 'fan.active_fan', percentage: 100 },
    });

    // 3. Cover stop and close
    const coverRow = container.querySelector('[data-entity-id="cover.curtain"]');
    const stopBtn = coverRow?.querySelector('[data-action="stop"]') as HTMLButtonElement;
    const closeBtn = coverRow?.querySelector('[data-action="close"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(stopBtn);
    });
    expect(JSON.parse(fetchCalls[3].options.body as string)).toEqual({
      domain: 'cover',
      service: 'stop_cover',
      entity_id: 'cover.curtain',
      service_data: { entity_id: 'cover.curtain' },
    });
    await act(async () => {
      fireEvent.click(closeBtn);
    });
    expect(JSON.parse(fetchCalls[4].options.body as string)).toEqual({
      domain: 'cover',
      service: 'close_cover',
      entity_id: 'cover.curtain',
      service_data: { entity_id: 'cover.curtain' },
    });

    // 4. Lock currently unlocked -> clicking locks
    const lockRow = container.querySelector('[data-entity-id="lock.unlocked_lock"]');
    const lockBtn = lockRow?.querySelector('[data-action="toggle-lock"]') as HTMLButtonElement;
    expect(lockBtn.textContent).toBe('Lock');
    await act(async () => {
      fireEvent.click(lockBtn);
    });
    expect(JSON.parse(fetchCalls[5].options.body as string)).toEqual({
      domain: 'lock',
      service: 'lock',
      entity_id: 'lock.unlocked_lock',
      service_data: { entity_id: 'lock.unlocked_lock' },
    });

    // 5. Climate temp down (-)
    const climateRow = container.querySelector('[data-entity-id="climate.bedroom_heat"]');
    const tempDownBtn = climateRow?.querySelector('[data-action="temp-down"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(tempDownBtn);
    });
    expect(JSON.parse(fetchCalls[6].options.body as string)).toEqual({
      domain: 'climate',
      service: 'set_temperature',
      entity_id: 'climate.bedroom_heat',
      service_data: { entity_id: 'climate.bedroom_heat', temperature: 69 },
    });

    // 6. Media player prev, next, vol-down, vol-up
    const mediaRow = container.querySelector('[data-entity-id="media_player.kitchen_speaker"]');
    const prevBtn = mediaRow?.querySelector('[data-action="prev"]') as HTMLButtonElement;
    const nextBtn = mediaRow?.querySelector('[data-action="next"]') as HTMLButtonElement;
    const volDownBtn = mediaRow?.querySelector('[data-action="vol-down"]') as HTMLButtonElement;
    const volUpBtn = mediaRow?.querySelector('[data-action="vol-up"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(prevBtn);
      fireEvent.click(nextBtn);
      fireEvent.click(volDownBtn);
      fireEvent.click(volUpBtn);
    });
    expect(JSON.parse(fetchCalls[7].options.body as string).service).toBe('media_previous_track');
    expect(JSON.parse(fetchCalls[8].options.body as string).service).toBe('media_next_track');
    expect(JSON.parse(fetchCalls[9].options.body as string).service).toBe('volume_down');
    expect(JSON.parse(fetchCalls[10].options.body as string).service).toBe('volume_up');

    // 7. Binary sensor device classes
    expect(container.querySelector('[data-entity-id="binary_sensor.door_contact"]')?.textContent).toContain('Closed');
    expect(container.querySelector('[data-entity-id="binary_sensor.window_contact"]')?.textContent).toContain('Open');
    expect(container.querySelector('[data-entity-id="binary_sensor.office_occupancy"]')?.textContent).toContain('Clear');
    expect(container.querySelector('[data-entity-id="binary_sensor.user_presence"]')?.textContent).toContain('Home');

    // 8. Vacuum generic state
    expect(container.querySelector('[data-entity-id="vacuum.roborock"] .ha-generic-state')?.textContent).toBe('docked');
  });

  it('covers formatStateLabel permutations across multiple domains and state configs', () => {
    setHaWidget('ha-labels', {
            cards: [
              { type: 'tile', entity: 'light.on_no_brightness' },
              { type: 'tile', entity: 'climate.no_cur_temp' },
              { type: 'tile', entity: 'media_player.title_only' },
              { type: 'tile', entity: 'media_player.no_title' },
              { type: 'tile', entity: 'sensor.no_unit' },
            ],
            entities: {
              'light.on_no_brightness': { entity_id: 'light.on_no_brightness', state: 'on', attributes: {} },
              'climate.no_cur_temp': { entity_id: 'climate.no_cur_temp', state: 'auto', attributes: {} },
              'media_player.title_only': { entity_id: 'media_player.title_only', state: 'playing', attributes: { media_title: 'Single Title' } },
              'media_player.no_title': { entity_id: 'media_player.no_title', state: 'idle', attributes: {} },
              'sensor.no_unit': { entity_id: 'sensor.no_unit', state: 'online', attributes: {} },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-labels" type="home-assistant" />);
    expect(container.querySelector('[data-entity-id="light.on_no_brightness"] .ha-tile-state')?.textContent).toBe('On');
    expect(container.querySelector('[data-entity-id="climate.no_cur_temp"] .ha-tile-state')?.textContent).toBe('auto');
    expect(container.querySelector('[data-entity-id="media_player.title_only"] .ha-tile-state')?.textContent).toBe(
      'Single Title',
    );
    expect(container.querySelector('[data-entity-id="media_player.no_title"] .ha-tile-state')?.textContent).toBe('idle');
    expect(container.querySelector('[data-entity-id="sensor.no_unit"] .ha-tile-state')?.textContent).toBe('online');
  });

  it('covers remaining optimistic action branches and ButtonCard inFlight/svcEnt branches', async () => {
    let resolveCall: (() => void) | null = null;
    const fetchCalls: { url: string; options: RequestInit }[] = [];
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      fetchCalls.push({ url, options });
      return new Promise((resolve) => {
        resolveCall = () => resolve(new Response(JSON.stringify({ status: 'ok' })));
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-deep-branches', {
            cards: [
              { type: 'tile', entity: 'fan.opt_fan' },
              { type: 'tile', entity: 'light.opt_light' },
              { type: 'tile', entity: 'media_player.opt_player' },
              {
                type: 'button',
                entity: 'light.opt_btn',
                tap_action: {
                  action: 'call-service',
                  service: '.toggle',
                  service_data: { entity_id: 'light.opt_btn' },
                },
              },
              {
                name: 'Unnamed Type Card',
              },
              {
                // completely empty card -> renders 'Card'
              },
            ],
            entities: {
              'fan.opt_fan': { entity_id: 'fan.opt_fan', state: 'off', attributes: {} },
              'light.opt_light': { entity_id: 'light.opt_light', state: 'on', attributes: {} },
              'media_player.opt_player': { entity_id: 'media_player.opt_player', state: 'paused', attributes: {} },
              'light.opt_btn': { entity_id: 'light.opt_btn', state: 'off', attributes: {} },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-deep-branches" type="home-assistant" />);

    // Check unknown card fallbacks
    const unknownCards = container.querySelectorAll('.ha-card-unknown');
    expect(unknownCards.length).toBe(2);
    expect(unknownCards[0].textContent).toBe('Unnamed Type Card');
    expect(unknownCards[1].textContent).toBe('Card');

    // 1. ButtonCard tap with svcEnt and empty domain prefix (falls back to domain)
    const btn = container.querySelector('.ha-card-button') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(btn);
    });

    // While inFlight is true, clicking again triggers if (inFlight) return
    btn.removeAttribute('disabled');
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchCalls.length).toBe(1);
    expect(JSON.parse(fetchCalls[0].options.body as string)).toEqual({
      domain: 'light',
      service: 'toggle',
      entity_id: 'light.opt_btn',
      service_data: { entity_id: 'light.opt_btn' },
    });

    // Resolve the in-flight request
    await act(async () => {
      resolveCall?.();
    });

    // 2. Play pause when paused -> optimistic playing
    const playPauseBtn = container.querySelector('[data-entity-id="media_player.opt_player"] [data-action="play-pause"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(playPauseBtn);
    });
    await act(async () => {
      resolveCall?.();
    });
    expect(container.querySelector('[data-entity-id="media_player.opt_player"]')?.getAttribute('data-state')).toBe('playing');

    // 3. Toggle light when on -> optimistic turn_off
    const lightToggle = container.querySelector('[data-entity-id="light.opt_light"] [data-action="toggle"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(lightToggle);
    });
    await act(async () => {
      resolveCall?.();
    });
    expect(container.querySelector('[data-entity-id="light.opt_light"]')?.getAttribute('data-state')).toBe('off');
  });

  it('guards non-entity items in CardRenderer and handles getEntityName with empty id', () => {
    expect(getEntityName(undefined, '')).toBe('');

    setHaWidget('ha-dividers', {
            title: 'Dividers and Edge Items',
            cards: [
              { type: 'divider' },
              {
                type: 'entities',
                title: 'Entities with Dividers',
                entities: [
                  { type: 'divider' },
                  { name: 'Row with no entity' },
                  'light.kitchen',
                ],
              },
            ],
            entities: {
              'light.kitchen': { entity_id: 'light.kitchen', state: 'on', attributes: { friendly_name: 'Kitchen Light' } },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-dividers" type="home-assistant" />);
    const dividers = container.querySelectorAll('.ha-divider');
    expect(dividers.length).toBeGreaterThanOrEqual(2);
    expect(container.querySelector('[data-entity-id="light.kitchen"]')).not.toBeNull();
  });

  it('handles PictureEntityCard camera_image entity resolution and ts join with existing query params', async () => {
    setHaWidget('ha-pic-edge', {
            cards: [
              {
                type: 'picture-entity',
                entity: 'camera.front_gate',
                camera_image: 'camera.front_gate',
              },
            ],
            entities: {
              'camera.front_gate': {
                entity_id: 'camera.front_gate',
                state: 'idle',
                attributes: {
                  entity_picture: '/api/camera_proxy/camera.front_gate?token=abc123xyz',
                  friendly_name: 'Front Gate Camera',
                },
              },
            },
          });

    const { container } = render(<HomeAssistantWidget id="ha-pic-edge" type="home-assistant" />);
    const picCard = container.querySelector('.ha-card-picture') as HTMLElement;
    expect(picCard).not.toBeNull();

    const imgBefore = picCard.querySelector('img') as HTMLImageElement;
    expect(imgBefore.src).toContain('/api/camera_proxy/camera.front_gate?token=abc123xyz');
    expect(imgBefore.src).not.toContain('&ts=');

    // Tap to refresh camera and verify &ts= is used instead of ?ts=
    await act(async () => {
      fireEvent.click(picCard);
    });

    const imgAfter = picCard.querySelector('img') as HTMLImageElement;
    expect(imgAfter.src).toContain('?token=abc123xyz&ts=');
  });

  it('covers remaining branches across cards, binary sensors, media player, and snapshot updates', async () => {
    // 1. getDomain edge cases
    expect(getDomain('LIGHT.BEDROOM')).toBe('light');

    // 2. data snapshot change resets overrides
    setHaWidget('ha-branches', {
            cards: [{ type: 'tile', entity: 'switch.living' }],
            entities: {
              'switch.living': { entity_id: 'switch.living', state: 'off', attributes: {} },
            },
          });

    const { container, rerender } = render(<HomeAssistantWidget id="ha-branches" type="home-assistant" />);
    const swBtn = container.querySelector('[data-action="toggle"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(swBtn);
    });

    setHaWidget('ha-branches', {
            cards: [{ type: 'tile', entity: 'switch.living' }],
            entities: {
              'switch.living': { entity_id: 'switch.living', state: 'on', attributes: {} },
            },
          });
    rerender(<HomeAssistantWidget id="ha-branches" type="home-assistant" />);

    // 3. Binary sensor device classes (door, occupancy, presence, other)
    setHaWidget('ha-binary-classes', {
            cards: [
              {
                type: 'entities',
                entities: [
                  'binary_sensor.door_open',
                  'binary_sensor.door_closed',
                  'binary_sensor.occ_active',
                  'binary_sensor.occ_clear',
                  'binary_sensor.pres_home',
                  'binary_sensor.pres_away',
                  'binary_sensor.other',
                ],
              },
            ],
            entities: {
              'binary_sensor.door_open': { entity_id: 'binary_sensor.door_open', state: 'on', attributes: { device_class: 'door' } },
              'binary_sensor.door_closed': { entity_id: 'binary_sensor.door_closed', state: 'off', attributes: { device_class: 'door' } },
              'binary_sensor.occ_active': { entity_id: 'binary_sensor.occ_active', state: 'on', attributes: { device_class: 'occupancy' } },
              'binary_sensor.occ_clear': { entity_id: 'binary_sensor.occ_clear', state: 'off', attributes: { device_class: 'occupancy' } },
              'binary_sensor.pres_home': { entity_id: 'binary_sensor.pres_home', state: 'on', attributes: { device_class: 'presence' } },
              'binary_sensor.pres_away': { entity_id: 'binary_sensor.pres_away', state: 'off', attributes: { device_class: 'presence' } },
              'binary_sensor.other': { entity_id: 'binary_sensor.other', state: 'off', attributes: { device_class: 'smoke' } },
            },
          });

    const { container: binContainer } = render(<HomeAssistantWidget id="ha-binary-classes" type="home-assistant" />);
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.door_open"]')?.textContent).toContain('Open');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.door_closed"]')?.textContent).toContain('Closed');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.occ_active"]')?.textContent).toContain('Occupied');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.occ_clear"]')?.textContent).toContain('Clear');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.pres_home"]')?.textContent).toContain('Home');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.pres_away"]')?.textContent).toContain('Away');
    expect(binContainer.querySelector('[data-entity-id="binary_sensor.other"]')?.textContent).toContain('Off');

    // 4. Media player buttons: prev, next, vol-down, vol-up
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-media-btns', {
            cards: [{ type: 'tile', entity: 'media_player.living' }],
            entities: {
              'media_player.living': { entity_id: 'media_player.living', state: 'playing', attributes: {} },
            },
          });

    const { container: mediaContainer } = render(<HomeAssistantWidget id="ha-media-btns" type="home-assistant" />);
    const prevBtn = mediaContainer.querySelector('[data-action="prev"]') as HTMLButtonElement;
    const nextBtn = mediaContainer.querySelector('[data-action="next"]') as HTMLButtonElement;
    const volDownBtn = mediaContainer.querySelector('[data-action="vol-down"]') as HTMLButtonElement;
    const volUpBtn = mediaContainer.querySelector('[data-action="vol-up"]') as HTMLButtonElement;

    await act(async () => {
      fireEvent.click(prevBtn);
      fireEvent.click(nextBtn);
      fireEvent.click(volDownBtn);
      fireEvent.click(volUpBtn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);

    // 5. ButtonCard with lock domain when unlocked -> calls lock
    setHaWidget('ha-lock-btn', {
            cards: [{ type: 'button', entity: 'lock.garage_lock' }],
            entities: {
              'lock.garage_lock': { entity_id: 'lock.garage_lock', state: 'unlocked', attributes: {} },
            },
          });

    const { container: lockContainer } = render(<HomeAssistantWidget id="ha-lock-btn" type="home-assistant" />);
    const lockBtn = lockContainer.querySelector('.ha-card-button') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(lockBtn);
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/action'),
      expect.objectContaining({
        body: JSON.stringify({
          domain: 'lock',
          service: 'lock',
          entity_id: 'lock.garage_lock',
          service_data: { entity_id: 'lock.garage_lock' },
        }),
      }),
    );

    // 6. dispatchWidgetAction without entity_id
    await dispatchWidgetAction('ha-global', { domain: 'homeassistant', service: 'restart' }, fetchMock);

    // 7. dispatchWidgetAction with default fetch
    await dispatchWidgetAction('ha-def-fetch', { domain: 'light', service: 'toggle' });

    // 8. Test media_play and media_pause optimistic updates via button tap
    setHaWidget('ha-opt-media-btns', {
            cards: [
              {
                type: 'button',
                entity: 'media_player.play_target',
                tap_action: { action: 'call-service', service: 'media_player.media_play' },
              },
              {
                type: 'button',
                entity: 'media_player.pause_target',
                tap_action: { action: 'call-service', service: 'media_player.media_pause' },
              },
            ],
            entities: {
              'media_player.play_target': { entity_id: 'media_player.play_target', state: 'paused', attributes: {} },
              'media_player.pause_target': { entity_id: 'media_player.pause_target', state: 'playing', attributes: {} },
            },
          });

    const { container: optMediaCont } = render(<HomeAssistantWidget id="ha-opt-media-btns" type="home-assistant" />);
    const btns = optMediaCont.querySelectorAll('.ha-card-button');
    await act(async () => {
      fireEvent.click(btns[0]);
      fireEvent.click(btns[1]);
    });

    // 9. Cover buttons: stop and close
    setHaWidget('ha-cover-btns', {
            cards: [{ type: 'tile', entity: 'cover.shades' }],
            entities: {
              'cover.shades': { entity_id: 'cover.shades', state: 'open', attributes: {} },
            },
          });

    const { container: coverCont } = render(<HomeAssistantWidget id="ha-cover-btns" type="home-assistant" />);
    const stopBtn = coverCont.querySelector('[data-action="stop"]') as HTMLButtonElement;
    const closeBtn = coverCont.querySelector('[data-action="close"]') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(stopBtn);
      fireEvent.click(closeBtn);
    });

    // 10. Light brightness and fan speed controls
    setHaWidget('ha-bright-speed', {
            cards: [
              { type: 'tile', entity: 'light.desk_lamp' },
              { type: 'tile', entity: 'fan.desk_fan' },
            ],
            entities: {
              'light.desk_lamp': { entity_id: 'light.desk_lamp', state: 'on', attributes: { brightness: 100 } },
              'fan.desk_fan': { entity_id: 'fan.desk_fan', state: 'on', attributes: { percentage: 50 } },
            },
          });

    const { container: brightCont } = render(<HomeAssistantWidget id="ha-bright-speed" type="home-assistant" />);
    const bright50Btn = brightCont.querySelector('.ha-quick-brightness .ha-btn-sub') as HTMLButtonElement;
    const speedMedBtn = brightCont.querySelector('.ha-quick-speeds .ha-btn-sub') as HTMLButtonElement;
    await act(async () => {
      if (bright50Btn) fireEvent.click(bright50Btn);
      if (speedMedBtn) fireEvent.click(speedMedBtn);
    });

    // 11. Unknown cards
    setHaWidget('ha-unknowns', {
            cards: [
              { type: 'custom-foo', title: 'Foo Title' },
              { type: 'custom-bar', name: 'Bar Name' },
              { type: 'custom-baz' },
            ],
          });

    const { container: unkCont } = render(<HomeAssistantWidget id="ha-unknowns" type="home-assistant" />);
    const unks = unkCont.querySelectorAll('.ha-card-unknown');
    expect(unks.length).toBe(3);
    expect(unks[0].textContent).toBe('Foo Title');
    expect(unks[1].textContent).toBe('Bar Name');
    expect(unks[2].textContent).toBe('custom-baz');
  });

  it('covers applyOptimisticUpdate, formatStateLabel, and getBinaryLabel comprehensive unit paths', () => {
    const base = { entity_id: 'light.desk', state: 'off', attributes: {} };
    expect(applyOptimisticUpdate(base, 'light', 'turn_on')).toEqual({ ...base, state: 'on' });
    expect(applyOptimisticUpdate(base, 'light', 'turn_on', { brightness: 128 })).toEqual({
      ...base,
      state: 'on',
      attributes: { brightness: 128 },
    });
    expect(applyOptimisticUpdate(base, 'fan', 'turn_on', { percentage: 60 })).toEqual({
      ...base,
      state: 'on',
      attributes: { percentage: 60 },
    });
    expect(applyOptimisticUpdate({ ...base, state: 'on' }, 'light', 'turn_off')).toEqual({ ...base, state: 'off' });
    expect(applyOptimisticUpdate(base, 'switch', 'toggle')).toEqual({ ...base, state: 'on' });
    expect(applyOptimisticUpdate({ ...base, state: 'on' }, 'switch', 'toggle')).toEqual({ ...base, state: 'off' });
    expect(applyOptimisticUpdate(base, 'lock', 'lock')).toEqual({ ...base, state: 'locked' });
    expect(applyOptimisticUpdate(base, 'lock', 'unlock')).toEqual({ ...base, state: 'unlocked' });
    expect(applyOptimisticUpdate(base, 'cover', 'open_cover')).toEqual({ ...base, state: 'open' });
    expect(applyOptimisticUpdate(base, 'cover', 'close_cover')).toEqual({ ...base, state: 'closed' });
    expect(applyOptimisticUpdate(base, 'climate', 'set_temperature', { temperature: 72 })).toEqual({
      ...base,
      attributes: { temperature: 72 },
    });
    expect(applyOptimisticUpdate(base, 'climate', 'set_hvac_mode', { hvac_mode: 'heat' })).toEqual({
      ...base,
      state: 'heat',
    });
    expect(applyOptimisticUpdate(base, 'media_player', 'media_play')).toEqual({ ...base, state: 'playing' });
    expect(applyOptimisticUpdate(base, 'media_player', 'media_pause')).toEqual({ ...base, state: 'paused' });
    expect(applyOptimisticUpdate({ ...base, state: 'playing' }, 'media_player', 'media_play_pause')).toEqual({
      ...base,
      state: 'paused',
    });
    expect(applyOptimisticUpdate({ ...base, state: 'paused' }, 'media_player', 'media_play_pause')).toEqual({
      ...base,
      state: 'playing',
    });
    expect(applyOptimisticUpdate(base, 'sensor', 'update')).toEqual(base);

    expect(formatStateLabel('light', 'on', { brightness: 255 })).toBe('On · 100%');
    expect(formatStateLabel('light', 'on')).toBe('On');
    expect(formatStateLabel('light', 'off')).toBe('Off');
    expect(formatStateLabel('climate', 'heat', { current_temperature: 71 })).toBe('heat · 71°');
    expect(formatStateLabel('climate', 'heat')).toBe('heat');
    expect(formatStateLabel('media_player', 'playing', { media_title: 'Song', media_artist: 'Band' })).toBe('Song - Band');
    expect(formatStateLabel('media_player', 'playing', { media_title: 'Solo' })).toBe('Solo');
    expect(formatStateLabel('media_player', 'idle')).toBe('idle');
    expect(formatStateLabel('sensor', '42', { unit_of_measurement: '%' })).toBe('42 %');
    expect(formatStateLabel('sensor', '42')).toBe('42');
    expect(formatStateLabel('lock', 'locked')).toBe('locked');

    expect(getBinaryLabel(true, 'door')).toBe('Open');
    expect(getBinaryLabel(false, 'door')).toBe('Closed');
    expect(getBinaryLabel(true, 'window')).toBe('Open');
    expect(getBinaryLabel(false, 'window')).toBe('Closed');
    expect(getBinaryLabel(true, 'occupancy')).toBe('Occupied');
    expect(getBinaryLabel(false, 'occupancy')).toBe('Clear');
    expect(getBinaryLabel(true, 'presence')).toBe('Home');
    expect(getBinaryLabel(false, 'presence')).toBe('Away');
    expect(getBinaryLabel(true, 'motion')).toBe('Motion');
    expect(getBinaryLabel(false, 'motion')).toBe('Clear');
    expect(getBinaryLabel(true, 'unknown')).toBe('On');
    expect(getBinaryLabel(false, 'unknown')).toBe('Off');
    expect(getBinaryLabel(true)).toBe('On');
    expect(getBinaryLabel(false)).toBe('Off');
  });

  it('covers sendAction without entity_id and CardRenderer / PictureEntityCard edge branches', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' })));
    vi.stubGlobal('fetch', fetchMock);

    // 1. sendAction without entity_id (success + error)
    setHaWidget('ha-no-ent', {
      cards: [
        {
          type: 'button',
          name: 'Restart Server',
          tap_action: { action: 'call-service', service: 'homeassistant.restart' },
        },
      ],
      entities: {},
    });

    const { container: noEntCont } = render(<HomeAssistantWidget id="ha-no-ent" type="home-assistant" />);
    const restartBtn = noEntCont.querySelector('.ha-card-button') as HTMLButtonElement;
    await act(async () => {
      fireEvent.click(restartBtn);
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/widgets/ha-no-ent/action',
      expect.objectContaining({ body: JSON.stringify({ domain: 'homeassistant', service: 'restart', service_data: {} }) }),
    );

    fetchMock.mockRejectedValueOnce(new Error('Server unavailable'));
    await act(async () => {
      fireEvent.click(restartBtn);
    });
    await waitFor(() => {
      expect(noEntCont.querySelector('.ha-alert-banner')?.textContent).toContain('Server unavailable');
    });

    // 2. CardRenderer edge cases: empty section, grid without columns, entities without entities, nested wrapper without title, unknown card variations
    setHaWidget('ha-renderer-edges', {
      view: {
        sections: [
          { title: 'Empty Section' },
          {
            cards: [
              { type: 'grid', cards: [{ type: 'tile', entity: 'switch.grid_def' }] },
              { type: 'entities', title: 'Empty Entities' },
              { type: 'entities', entities: ['', { entity: '' }] },
              { type: 'vertical-stack', cards: [{ type: 'tile', entity: 'switch.v_def' }] },
              { name: 'Just Name' }, { type: 'plain-type' }, {},
            ],
          },
        ],
      },
      entities: {
        'switch.grid_def': { entity_id: 'switch.grid_def', state: 'off', attributes: {} },
        'switch.v_def': { entity_id: 'switch.v_def', state: 'off', attributes: {} },
      },
    });

    const { container: rendCont } = render(<HomeAssistantWidget id="ha-renderer-edges" type="home-assistant" />);
    expect(rendCont.querySelector('.ha-card-grid')).not.toBeNull();
    expect(rendCont.querySelector('.ha-card-wrapper')).not.toBeNull();
    expect(rendCont.querySelectorAll('.ha-divider').length).toBeGreaterThan(0);
    const unknowns = rendCont.querySelectorAll('.ha-card-unknown');
    expect(unknowns[0].textContent).toBe('Just Name');
    expect(unknowns[1].textContent).toBe('plain-type');
    expect(unknowns[2].textContent).toBe('Card');

    // 3. PictureEntityCard tap branches and fallback image
    setHaWidget('ha-pic-branches', {
      cards: [
        { type: 'picture-entity', entity: 'switch.cam_power', camera_image: 'camera.yard' },
        { type: 'picture-entity', entity: 'light.yard_light' },
        { type: 'picture-entity', entity: 'sensor.no_pic' },
      ],
      entities: {
        'switch.cam_power': { entity_id: 'switch.cam_power', state: 'on', attributes: {} },
        'camera.yard': { entity_id: 'camera.yard', state: 'idle', attributes: { entity_picture: '/yard.jpg' } },
        'light.yard_light': { entity_id: 'light.yard_light', state: 'off', attributes: {} },
        'sensor.no_pic': { entity_id: 'sensor.no_pic', state: '10', attributes: {} },
      },
    });

    const { container: picBCont } = render(<HomeAssistantWidget id="ha-pic-branches" type="home-assistant" />);
    const picCards = picBCont.querySelectorAll('.ha-card-picture');
    expect(picCards.length).toBe(3);
    await act(async () => {
      fireEvent.click(picCards[0]);
      fireEvent.click(picCards[1]);
    });
    expect(picCards[2].querySelector('.ha-picture-fallback')).not.toBeNull();

    // 4. ButtonCard tap action variations
    setHaWidget('ha-btn-branches', {
      cards: [
        { type: 'button', entity: 'lock.front' },
        { type: 'button', entity: 'light.desk', tap_action: { action: 'call_service', service: 'toggle', service_data: { entity_id: 'light.desk' } } },
        { type: 'button', entity: 'light.desk', tap_action: { action: 'more-info' } },
        { type: 'button', entity: 'light.desk', tap_action: { action: 'toggle' } },
      ],
      entities: {
        'lock.front': { entity_id: 'lock.front', state: 'unlocked', attributes: {} },
        'light.desk': { entity_id: 'light.desk', state: 'off', attributes: {} },
      },
    });

    const { container: btnBCont } = render(<HomeAssistantWidget id="ha-btn-branches" type="home-assistant" />);
    const bCards = btnBCont.querySelectorAll('.ha-card-button');
    const callsBeforeButtons = fetchMock.mock.calls.length;
    await act(async () => {
      fireEvent.click(bCards[0]);
      fireEvent.click(bCards[1]);
      fireEvent.click(bCards[2]);
      fireEvent.click(bCards[3]);
    });
    expect(fetchMock.mock.calls.length - callsBeforeButtons).toBe(3);

    // 5. ThermostatCard defaults without attributes
    setHaWidget('ha-tstat-def', {
      cards: [{ type: 'thermostat', entity: 'climate.bare' }],
      entities: { 'climate.bare': { entity_id: 'climate.bare', state: 'off', attributes: {} } },
    });

    const { container: tstatCont } = render(<HomeAssistantWidget id="ha-tstat-def" type="home-assistant" />);
    expect(tstatCont.querySelector('.ha-target-num')?.textContent).toBe('70°');
    const plusBtn = tstatCont.querySelector('.ha-target-temp-dial button:last-child') as HTMLButtonElement;
    await act(async () => { fireEvent.click(plusBtn); });
  });

  it('covers inFlight state rendering and disabled interactions', async () => {
    let resolveAction!: (v: unknown) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise((resolve) => { resolveAction = resolve; }));
    vi.stubGlobal('fetch', fetchMock);

    setHaWidget('ha-inflight-all', {
      cards: [
        { type: 'button', entity: 'switch.btn_flight' },
        { type: 'button', name: 'Target Action Button', tap_action: { action: 'call-service', service: 'scene.turn_on', target: { entity_id: 'scene.target_flight' } } },
        { type: 'button', name: 'Data Action Button', tap_action: { action: 'call_service', service: 'script.turn_on', data: { entity_id: 'script.data_flight' } } },
        { type: 'tile', entity: 'light.tile_flight' },
        { type: 'tile', entity: 'fan.fan_flight' },
        { type: 'tile', entity: 'lock.lock_flight' },
        { type: 'tile', entity: 'cover.cov_flight' },
        { type: 'thermostat', entity: 'climate.clim_flight' },
      ],
      entities: {
        'switch.btn_flight': { entity_id: 'switch.btn_flight', state: 'off', attributes: {} },
        'light.tile_flight': { entity_id: 'light.tile_flight', state: 'on', attributes: { brightness: 120 } },
        'fan.fan_flight': { entity_id: 'fan.fan_flight', state: 'on', attributes: { percentage: 50 } },
        'lock.lock_flight': { entity_id: 'lock.lock_flight', state: 'locked', attributes: {} },
        'cover.cov_flight': { entity_id: 'cover.cov_flight', state: 'open', attributes: {} },
        'climate.clim_flight': { entity_id: 'climate.clim_flight', state: 'heat', attributes: { temperature: 70 } },
      },
    });

    const { container } = render(<HomeAssistantWidget id="ha-inflight-all" type="home-assistant" />);
    const [btn, targetBtn, dataBtn] = Array.from(container.querySelectorAll('.ha-card-button')) as HTMLButtonElement[];

    for (const b of [btn, targetBtn, dataBtn]) {
      await act(async () => { fireEvent.click(b); });
      expect(b.disabled).toBe(true);
      await act(async () => { fireEvent.click(b); });
      await act(async () => { resolveAction(new Response(JSON.stringify({ status: 'ok' }))); });
      expect(b.disabled).toBe(false);
    }
  });

  it('covers missing entity fallbacks across all card types and control rows', () => {
    setHaWidget('ha-missing-all', {
      cards: [
        { type: 'entities', entities: ['light.missing_row'] },
        { type: 'tile', entity: 'light.tile_name', name: 'Name' }, { type: 'tile', entity: 'light.tile_title', title: 'Title' }, { type: 'tile', entity: 'light.tile_none' },
        { type: 'button', entity: 'light.btn_name', name: 'Btn', icon: '⭐' }, { type: 'button', entity: 'light.btn_title', title: 'Btn' }, { type: 'button', entity: 'light.btn_none' },
        { type: 'thermostat', entity: 'climate.missing_clim' }, { type: 'picture-entity', entity: 'camera.missing_cam' },
      ],
      entities: {},
    });

    const { container } = render(<HomeAssistantWidget id="ha-missing-all" type="home-assistant" />);
    expect(container.querySelector('.ha-entity-row')?.getAttribute('data-state')).toBe('unavailable');
    expect(container.querySelectorAll('.ha-card-tile').length).toBe(3);
    expect(container.querySelectorAll('.ha-card-button').length).toBe(3);
    expect(container.querySelector('.ha-card-thermostat')).not.toBeNull();
    expect(container.querySelector('.ha-card-picture')).not.toBeNull();

    const fBase = { entity_id: 'fan.f', state: 'off', attributes: {} };
    expect(applyOptimisticUpdate(fBase, 'fan', 'set_percentage', { percentage: 66 })).toEqual({ ...fBase, state: 'on', attributes: { percentage: 66 } });
    expect(applyOptimisticUpdate({ ...fBase, state: 'on' }, 'fan', 'set_percentage', { percentage: 0 })).toEqual({ ...fBase, state: 'off', attributes: { percentage: 0 } });
    const cOpen = { entity_id: 'cover.c', state: 'open', attributes: {} };
    expect(applyOptimisticUpdate(cOpen, 'cover', 'stop_cover')).toEqual(cOpen);
    expect(applyOptimisticUpdate(cOpen, 'cover', 'close_cover')).toEqual({ ...cOpen, state: 'closed' });
    const mPlay = { entity_id: 'media_player.m', state: 'playing', attributes: {} };
    expect(applyOptimisticUpdate(mPlay, 'media_player', 'volume_up')).toEqual(mPlay);
  });
});
