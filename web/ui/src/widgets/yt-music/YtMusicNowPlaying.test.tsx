import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { YtMusicNowPlaying } from './YtMusicNowPlaying';

describe('YtMusicNowPlaying', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const baseProps = {
    nowPlaying: {
      title: 'Starboy',
      artist: 'The Weeknd',
      thumb: 'https://example.com/starboy.jpg',
      state: 'playing',
      volume: 50,
      queue_index: 0,
    },
    queue: [
      { title: 'Starboy', artist: 'The Weeknd', thumb: 'https://example.com/starboy.jpg' },
      { title: 'Blinding Lights', artist: 'The Weeknd', thumb: 'https://example.com/blinding.jpg' },
    ],
    activeSpeaker: 'media_player.living_room',
    speakers: [
      { entity_id: 'media_player.living_room', name: 'Living Room' },
      { entity_id: 'media_player.kitchen', name: 'Kitchen' },
    ],
    onPrev: vi.fn(),
    onPlayPause: vi.fn(),
    onNext: vi.fn(),
    onStop: vi.fn(),
    onVolumeChange: vi.fn(),
    onPlayQueueIndex: vi.fn(),
    onBrowseLibrary: vi.fn(),
  };

  it('renders now playing metadata, active speaker badge, and transport controls', () => {
    const { container } = render(<YtMusicNowPlaying {...baseProps} />);

    expect(container.querySelector('#np-title')?.textContent).toBe('Starboy');
    expect(container.querySelector('#np-artist')?.textContent).toBe('The Weeknd');
    expect(container.querySelector('#np-art')?.getAttribute('src')).toBe('https://example.com/starboy.jpg');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('Living Room');

    const playBtn = container.querySelector('#btn-play');
    expect(playBtn?.getAttribute('aria-label')).toBe('Pause');

    const volSlider = container.querySelector('#vol-slider') as HTMLInputElement;
    expect(volSlider.value).toBe('50');
    expect(volSlider.hasAttribute('data-prevent-screen-swipe')).toBe(true);

    const queueRows = container.querySelectorAll('.np-queue-row');
    expect(queueRows.length).toBe(2);
    expect(queueRows[0].classList.contains('active')).toBe(true);
    expect(queueRows[0].querySelector('.np-queue-playing-indicator')).not.toBeNull();
  });

  it('calls transport control callbacks and browse library callback', () => {
    const { container } = render(<YtMusicNowPlaying {...baseProps} />);

    fireEvent.click(container.querySelector('#btn-browse-library')!);
    expect(baseProps.onBrowseLibrary).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('#btn-prev')!);
    expect(baseProps.onPrev).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('#btn-play')!);
    expect(baseProps.onPlayPause).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('#btn-next')!);
    expect(baseProps.onNext).toHaveBeenCalledTimes(1);

    fireEvent.click(container.querySelector('#btn-stop')!);
    expect(baseProps.onStop).toHaveBeenCalledTimes(1);

    const volSlider = container.querySelector('#vol-slider')!;
    fireEvent.change(volSlider, { target: { value: '80' } });
    expect(baseProps.onVolumeChange).toHaveBeenCalledTimes(1);
  });

  it('handles queue item selection via click and keyboard (Enter / Space)', () => {
    const { container } = render(<YtMusicNowPlaying {...baseProps} />);

    const secondRow = container.querySelectorAll('.np-queue-row')[1];
    fireEvent.click(secondRow);
    expect(baseProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: 'Enter' });
    expect(baseProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: ' ' });
    expect(baseProps.onPlayQueueIndex).toHaveBeenCalledWith(1);

    fireEvent.keyDown(secondRow, { key: 'Escape' });
    expect(baseProps.onPlayQueueIndex).toHaveBeenCalledTimes(3);
  });

  it('handles starting playback state and paused playback state', () => {
    const { container, rerender } = render(
      <YtMusicNowPlaying
        {...baseProps}
        startingPlayback={true}
        nowPlaying={{ state: 'paused' }}
      />
    );

    expect(container.querySelector('#np-title')?.textContent).toBe('Starting playback...');
    expect(container.querySelector('#btn-play')?.getAttribute('aria-label')).toBe('Play');

    // Rerender with empty state
    rerender(
      <YtMusicNowPlaying
        {...baseProps}
        nowPlaying={{}}
        queue={[]}
        activeSpeaker="this_device"
        speakers={[]}
      />
    );

    expect(container.querySelector('#np-title')?.textContent).toBe('YouTube Music');
    expect(container.querySelector('#np-artist')?.textContent).toBe('Pick a playlist or search a song');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('This Device');
    expect(container.querySelector('.np-queue-empty')?.textContent).toBe('Queue is empty');
  });

  it('renders single track count in queue and handles custom speaker string and missing queue index callback', () => {
    const singleQueueProps = {
      ...baseProps,
      activeSpeaker: 'media_player.unknown_group',
      speakers: [],
      queue: [{ title: 'Solo', artist: 'Artist', id: 'solo-id' }],
      onPlayQueueIndex: undefined,
    };
    const { container } = render(<YtMusicNowPlaying {...singleQueueProps} />);

    expect(container.querySelector('.np-queue-count')?.textContent).toBe('1 track');
    expect(container.querySelector('#np-device-badge')?.textContent).toContain('media_player.unknown_group');

    // Click row when onPlayQueueIndex is undefined (should not throw)
    const row = container.querySelector('.np-queue-row')!;
    fireEvent.click(row);
    fireEvent.keyDown(row, { key: 'Enter' });
  });
});
