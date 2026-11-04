import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PaneContext } from '../../carousel/PaneContext';
import { useDisplayStore } from '../../store';
import { PlexRecentWidget, type PlexItem, type SeerrResultItem } from './PlexRecentWidget';

const initialStore = useDisplayStore.getState();

const mockRecentItems: PlexItem[] = [
  {
    id: 'm-1',
    title: 'Dune: Part Two',
    subtitle: '2024',
    year: '2024',
    category: 'movie',
    type: 'movie',
    badge: 'MOVIE',
    rating: 8.5,
    thumb: 'https://example.com/dune.jpg',
    summary: 'Paul Atreides unites with Chani and the Fremen while seeking revenge.',
  },
  {
    id: 'tv-1',
    title: 'Fallout',
    subtitle: 'Season 1',
    year: '2024',
    category: 'tv',
    type: 'show',
    badge: 'TV',
    rating: 8.4,
    thumb: 'https://example.com/fallout.jpg',
    summary: 'In a future post-apocalyptic Los Angeles, citizens must live in underground bunkers.',
  },
  {
    id: 'ab-1',
    title: 'Project Hail Mary',
    subtitle: 'Andy Weir',
    year: '2021',
    category: 'audiobook',
    type: 'audiobook',
    badge: 'BOOK',
    rating: 4.9,
    thumb: 'https://example.com/phm.jpg',
    summary: 'Ryland Grace is the sole survivor on a desperate last-chance mission.',
  },
];

const mockUpcomingItems: PlexItem[] = [
  {
    id: 'up-1',
    title: 'Gladiator II',
    subtitle: 'Nov 22',
    year: '2024',
    category: 'upcoming',
    type: 'movie',
    badge: 'MOVIE',
    rating: 7.8,
    thumb: 'https://example.com/gladiator.jpg',
    summary: 'Years after witnessing the death of Maximus, Lucius must enter the Colosseum.',
  },
];

const mockSeerrResults: SeerrResultItem[] = [
  {
    id: 'sr-1',
    tmdb_id: 101,
    media_type: 'movie',
    title: 'The Batman',
    year: 2022,
    thumb: 'https://example.com/thebatman.jpg',
    status: 'available',
  },
  {
    id: 'sr-2',
    tmdb_id: 102,
    media_type: 'movie',
    title: 'Batman Begins',
    year: 2005,
    thumb: 'https://example.com/batmanbegins.jpg',
    status: 'requested',
  },
  {
    id: 'sr-3',
    tmdb_id: 103,
    media_type: 'movie',
    title: 'The Dark Knight',
    year: 2008,
    thumb: 'https://example.com/darkknight.jpg',
    status: 'pending',
  },
];

describe('PlexRecentWidget', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    useDisplayStore.setState(initialStore, true);
    useDisplayStore.setState({
      visibility: { visible: true },
      widgets: {
        'plex-1': {
          meta: {
            type: 'plex-recent',
            origin: [0, 0],
            dimensions: [3, 2],
            config: {},
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-11-01T12:00:00Z',
        },
      },
    });

    fetchMock = vi.fn(async (url: string | URL | Request) => {
      const urlStr = String(url);
      if (urlStr.includes('/plex/recent')) {
        return new Response(JSON.stringify({ items: mockRecentItems }), { status: 200 });
      }
      if (urlStr.includes('/plex/upcoming')) {
        return new Response(JSON.stringify({ items: mockUpcomingItems }), { status: 200 });
      }
      if (urlStr.includes('/plex/play')) {
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (urlStr.includes('/seerr/search')) {
        return new Response(JSON.stringify({ items: mockSeerrResults }), { status: 200 });
      }
      if (urlStr.includes('/seerr/request')) {
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return new Response('{}', { status: 404 });
    });

    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders category tabs and filters items across movie, tv, audiobook, and upcoming categories', async () => {
    const { container } = render(<PlexRecentWidget id="plex-1" type="plex-recent" />);

    // Wait for initial fetch
    await waitFor(() => {
      const cards = container.querySelectorAll('#poster-grid .item-card');
      expect(cards.length).toBe(3);
    });

    // Default "All Recent" should show movie, tv, audiobook
    const titleElements = container.querySelectorAll('#poster-grid .title');
    const titles = Array.from(titleElements).map((el) => el.textContent);
    expect(titles).toEqual(['Dune: Part Two', 'Fallout', 'Project Hail Mary']);

    // Check tabs
    const tabs = container.querySelectorAll('.tab-btn');
    expect(tabs.length).toBe(5);

    // Switch to Movies
    const moviesTab = container.querySelector('.tab-btn[data-cat="movie"]') as HTMLButtonElement;
    expect(moviesTab).not.toBeNull();
    fireEvent.click(moviesTab);
    expect(moviesTab.classList.contains('active')).toBe(true);

    let currentTitles = Array.from(container.querySelectorAll('#poster-grid .title')).map((el) => el.textContent);
    expect(currentTitles).toEqual(['Dune: Part Two']);

    // Switch to TV Shows
    const tvTab = container.querySelector('.tab-btn[data-cat="tv"]') as HTMLButtonElement;
    fireEvent.click(tvTab);
    currentTitles = Array.from(container.querySelectorAll('#poster-grid .title')).map((el) => el.textContent);
    expect(currentTitles).toEqual(['Fallout']);

    // Switch to Audiobooks
    const audiobooksTab = container.querySelector('.tab-btn[data-cat="audiobook"]') as HTMLButtonElement;
    fireEvent.click(audiobooksTab);
    currentTitles = Array.from(container.querySelectorAll('#poster-grid .title')).map((el) => el.textContent);
    expect(currentTitles).toEqual(['Project Hail Mary']);

    // Switch to Upcoming ("Dropping Soon")
    const upcomingTab = container.querySelector('.tab-btn[data-cat="upcoming"]') as HTMLButtonElement;
    fireEvent.click(upcomingTab);
    currentTitles = Array.from(container.querySelectorAll('#poster-grid .title')).map((el) => el.textContent);
    expect(currentTitles).toEqual(['Gladiator II']);

    // Return to All Recent
    const allTab = container.querySelector('.tab-btn[data-cat="all"]') as HTMLButtonElement;
    fireEvent.click(allTab);
    currentTitles = Array.from(container.querySelectorAll('#poster-grid .title')).map((el) => el.textContent);
    expect(currentTitles).toEqual(['Dune: Part Two', 'Fallout', 'Project Hail Mary']);
  });

  it('opens detail modal on card click, displays metadata, and dispatches TV playback POST', async () => {
    const { container } = render(<PlexRecentWidget id="plex-1" type="plex-recent" />);

    await waitFor(() => {
      expect(container.querySelectorAll('#poster-grid .item-card').length).toBe(3);
    });

    const duneCard = container.querySelectorAll('#poster-grid .item-card')[0];
    fireEvent.click(duneCard);

    // Detail modal opens
    const modal = container.querySelector('#detail-modal');
    expect(modal?.classList.contains('open')).toBe(true);

    const titleEl = container.querySelector('#detail-title');
    expect(titleEl?.textContent).toBe('Dune: Part Two');

    const subEl = container.querySelector('#detail-sub');
    expect(subEl?.textContent).toBe('2024 · ★ 8.5');

    const summaryEl = container.querySelector('#detail-summary');
    expect(summaryEl?.textContent).toContain('Paul Atreides unites with Chani');

    // Living Room TV play button is present for movie
    const playBtn = container.querySelector('#tv-play-btn') as HTMLButtonElement;
    expect(playBtn).not.toBeNull();
    expect(playBtn.textContent).toContain('Play on Living Room TV');

    // Click Play button
    fireEvent.click(playBtn);

    await waitFor(() => {
      expect(playBtn.textContent).toBe('✓ Launching on Living Room TV');
    });

    // Verify POST payload to /hearth-api/plex/play
    const playCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes('/plex/play'));
    expect(playCalls.length).toBe(1);
    expect(playCalls[0][1]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rating_key: 'm-1',
        title: 'Dune: Part Two',
        type: 'movie',
      }),
    });

    // Close detail modal
    const closeBtn = container.querySelector('#close-detail-btn') as HTMLButtonElement;
    fireEvent.click(closeBtn);
    expect(modal?.classList.contains('open')).toBe(false);
  });

  it('hides Living Room TV playback button for audiobook and upcoming categories', async () => {
    const { container } = render(<PlexRecentWidget id="plex-1" type="plex-recent" />);

    await waitFor(() => {
      expect(container.querySelectorAll('#poster-grid .item-card').length).toBe(3);
    });

    // Click Audiobook card
    const audioCard = container.querySelectorAll('#poster-grid .item-card')[2];
    fireEvent.click(audioCard);

    const modal = container.querySelector('#detail-modal');
    expect(modal?.classList.contains('open')).toBe(true);
    expect(container.querySelector('#detail-title')?.textContent).toBe('Project Hail Mary');
    // TV play button must NOT be present
    expect(container.querySelector('#tv-play-btn')).toBeNull();

    // Close modal
    fireEvent.click(container.querySelector('#close-detail-btn') as HTMLButtonElement);

    // Switch to Upcoming tab
    const upcomingTab = container.querySelector('.tab-btn[data-cat="upcoming"]') as HTMLButtonElement;
    fireEvent.click(upcomingTab);

    // Click Upcoming card
    const upCard = container.querySelector('#poster-grid .item-card') as HTMLElement;
    fireEvent.click(upCard);
    expect(container.querySelector('#detail-title')?.textContent).toBe('Gladiator II');
    // TV play button must NOT be present
    expect(container.querySelector('#tv-play-btn')).toBeNull();
  });

  it('opens Seerr modal, executes search, displays statuses, and handles request POST', async () => {
    const { container } = render(<PlexRecentWidget id="plex-1" type="plex-recent" />);

    // Click + Request button
    const openReqBtn = container.querySelector('#open-seerr-btn') as HTMLButtonElement;
    fireEvent.click(openReqBtn);

    const seerrModal = container.querySelector('#seerr-modal');
    expect(seerrModal?.classList.contains('open')).toBe(true);

    const seerrInput = container.querySelector('#seerr-input') as HTMLInputElement;
    fireEvent.change(seerrInput, { target: { value: 'Batman' } });
    expect(seerrInput.value).toBe('Batman');

    // Submit search form
    const form = container.querySelector('#seerr-form') as HTMLFormElement;
    fireEvent.submit(form);

    await waitFor(() => {
      const searchCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes('/seerr/search'));
      expect(searchCalls.length).toBe(1);
      expect(String(searchCalls[0][0])).toContain('/seerr/search?q=Batman');
    });

    await waitFor(() => {
      const resultCards = container.querySelectorAll('#seerr-results .item-card');
      expect(resultCards.length).toBe(3);
    });

    const resultButtons = container.querySelectorAll<HTMLButtonElement>('#seerr-results .req-action-btn');
    expect(resultButtons.length).toBe(3);

    // Item 101: available -> "✓ In Plex", disabled
    expect(resultButtons[0].textContent).toBe('✓ In Plex');
    expect(resultButtons[0].disabled).toBe(true);

    // Item 102: requested -> "✓ Requested", disabled
    expect(resultButtons[1].textContent).toBe('✓ Requested');
    expect(resultButtons[1].disabled).toBe(true);

    // Item 103: pending -> "+ Request", enabled
    expect(resultButtons[2].textContent).toBe('+ Request');
    expect(resultButtons[2].disabled).toBe(false);

    // Click request button on item 103
    fireEvent.click(resultButtons[2]);

    await waitFor(() => {
      expect(resultButtons[2].textContent).toBe('✓ Requested');
      expect(resultButtons[2].disabled).toBe(true);
    });

    // Verify request POST payload
    const reqCalls = fetchMock.mock.calls.filter(([url]) => String(url).includes('/seerr/request'));
    expect(reqCalls.length).toBe(1);
    expect(reqCalls[0][1]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        media_type: 'movie',
        tmdb_id: 103,
      }),
    });

    // Close Seerr modal
    const closeSeerrBtn = container.querySelector('#close-seerr-btn') as HTMLButtonElement;
    fireEvent.click(closeSeerrBtn);
    expect(seerrModal?.classList.contains('open')).toBe(false);
  });

  it('respects custom api_base configured in widget meta', async () => {
    useDisplayStore.setState({
      widgets: {
        'plex-custom': {
          meta: {
            type: 'plex-recent',
            origin: [0, 0],
            dimensions: [3, 2],
            config: { api_base: '/custom-gateway' },
            assets_base: '',
          },
          state: 'healthy',
          data: {},
          timestamp: '2026-11-01T12:00:00Z',
        },
      },
    });

    render(<PlexRecentWidget id="plex-custom" type="plex-recent" />);

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([url]) => String(url));
      expect(urls.some((u) => u.startsWith('/custom-gateway/plex/recent'))).toBe(true);
      expect(urls.some((u) => u.startsWith('/custom-gateway/plex/upcoming'))).toBe(true);
    });
  });

  it('pauses polling when off-pane or hidden, and resumes when active', async () => {
    vi.useFakeTimers();

    // 1. Off-pane test: isCurrentPane = false
    const { rerender } = render(
      <PaneContext.Provider value={{ isCurrentPane: false, paneIndex: 1 }}>
        <PlexRecentWidget id="plex-1" type="plex-recent" />
      </PaneContext.Provider>
    );

    // No fetch should occur while off-pane
    expect(fetchMock).not.toHaveBeenCalled();

    // Advance 180s while off-pane: still no fetch
    act(() => {
      vi.advanceTimersByTime(180000);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // 2. Rerender with active pane: fetch fires immediately
    rerender(
      <PaneContext.Provider value={{ isCurrentPane: true, paneIndex: 0 }}>
        <PlexRecentWidget id="plex-1" type="plex-recent" />
      </PaneContext.Provider>
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(2); // recent + upcoming
    const initialCalls = fetchMock.mock.calls.length;

    // Advance 180s while active: interval re-fetches
    await act(async () => {
      vi.advanceTimersByTime(180000);
      await Promise.resolve();
    });

    expect(fetchMock.mock.calls.length).toBe(initialCalls + 2);

    // 3. Document visibility hidden: polling pauses
    act(() => {
      useDisplayStore.setState({ visibility: { visible: false } });
    });

    const callsBeforeHidden = fetchMock.mock.calls.length;
    act(() => {
      vi.advanceTimersByTime(180000);
    });
    // Should not have fetched again while document was hidden
    expect(fetchMock.mock.calls.length).toBe(callsBeforeHidden);

    vi.useRealTimers();
  });

  it('marks horizontal tabs, modal overlay, and search input with data-prevent-screen-swipe', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: mockRecentItems }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const { container } = render(<PlexRecentWidget id="plex-swipe" type="plex-recent" />);
    await waitFor(() => {
      expect(container.querySelector('.tabs')).not.toBeNull();
    });

    expect(container.querySelector('.tabs')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('#detail-modal')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('#seerr-modal')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
    expect(container.querySelector('.seerr-input')?.hasAttribute('data-prevent-screen-swipe')).toBe(true);
  });

  it('covers fallback badges, TV play failure, Seerr search/request errors, and modal close buttons', async () => {
    useDisplayStore.setState({
      widgets: {
        'plex-cov': {
          state: 'ready',
          data: null,
          timestamp: '2026-01-01T00:00:00Z',
          meta: { type: 'plex-recent', assets_base: '', origin: [0, 0], dimensions: [1, 1], config: { api_base: '/custom-plex/' } },
        },
      },
    });
    const edgeItems: PlexItem[] = [
      { id: '1', title: 'No Badge Movie', year: 2025, category: 'movie' },
      { id: '2', title: 'No Badge TV', category: 'tv' },
      { id: '3', title: 'No Badge Book', category: 'audiobook' },
    ];
    let seerrSearchShouldFail = true;
    let seerrReqMode: 'http-fail' | 'throw' = 'http-fail';
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/plex/recent')) {
        return new Response(JSON.stringify({ items: edgeItems }), { status: 200 });
      }
      if (url.includes('/plex/upcoming')) {
        return new Response('err', { status: 500 });
      }
      if (url.includes('/plex/play')) {
        return new Response(JSON.stringify({ ok: false }), { status: 500 });
      }
      if (url.includes('/seerr/search')) {
        if (seerrSearchShouldFail) throw new Error('seerr search boom');
        return new Response(
          JSON.stringify({
            items: [
              { tmdb_id: 901, media_type: 'movie', title: 'Available Film', status: 'available' },
              { tmdb_id: 902, media_type: 'tv', title: 'Req Film', year: 2026 },
            ],
          }),
          { status: 200 },
        );
      }
      if (url.includes('/seerr/request')) {
        if (seerrReqMode === 'throw') throw new Error('network drop');
        return new Response(JSON.stringify({ ok: false }), { status: 500 });
      }
      return new Response('{}', { status: 200 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { container } = render(<PlexRecentWidget id="plex-cov" type="plex-recent" />);
    await waitFor(() => {
      expect(container.querySelectorAll('.item-card').length).toBe(3);
    });

    // Open detail modal on movie without summary or badge, trigger failing TV play, then close
    fireEvent.click(container.querySelectorAll('.item-card')[0]!);
    expect(container.querySelector('#detail-summary')?.textContent).toBe('No summary available.');
    fireEvent.click(container.querySelector('#tv-play-btn')!);
    await waitFor(() => {
      expect(container.querySelector('#tv-play-btn')?.textContent).toContain('Launch Error');
    });
    fireEvent.click(container.querySelector('#close-detail-btn')!);

    // Open Seerr modal, submit empty query, submit failing query, then succeeding query + failing request
    fireEvent.click(container.querySelector('#open-seerr-btn')!);
    fireEvent.submit(container.querySelector('#seerr-form')!);
    fireEvent.change(container.querySelector('#seerr-input')!, { target: { value: 'alien' } });
    fireEvent.submit(container.querySelector('#seerr-form')!);
    await waitFor(() => {
      expect(container.querySelector('#seerr-results')?.textContent).toContain('Search failed');
    });

    seerrSearchShouldFail = false;
    fireEvent.submit(container.querySelector('#seerr-form')!);
    await waitFor(() => {
      expect(container.querySelectorAll('#seerr-results .req-action-btn').length).toBe(2);
    });

    // Trigger HTTP 500 request error -> shows 'Error' label (line 379)
    const reqBtns = container.querySelectorAll('#seerr-results .req-action-btn');
    fireEvent.click(reqBtns[1]!);
    await waitFor(() => {
      expect(container.querySelectorAll('#seerr-results .req-action-btn')[1]?.textContent).toBe('Error');
    });

    // Trigger thrown request error
    seerrReqMode = 'throw';
    fireEvent.click(container.querySelectorAll('#seerr-results .req-action-btn')[1]!);
    await waitFor(() => {
      expect(container.querySelectorAll('#seerr-results .req-action-btn')[1]?.textContent).toBe('Error');
    });

    fireEvent.click(container.querySelector('#close-seerr-btn')!);
  });

  it('activates item card on Enter and Space keydown', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: mockRecentItems }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const { container } = render(<PlexRecentWidget id="plex-kb" type="plex-recent" />);
    await waitFor(() => {
      expect(container.querySelectorAll('.item-card').length).toBeGreaterThan(0);
    });

    const card = container.querySelector('.item-card') as HTMLElement;
    expect(card.getAttribute('role')).toBe('button');
    expect(card.getAttribute('tabindex')).toBe('0');

    // Trigger Space
    fireEvent.keyDown(card, { key: ' ' });
    expect(container.querySelector('#detail-modal')?.classList.contains('open')).toBe(true);
    fireEvent.click(container.querySelector('#close-detail-btn')!);

    // Trigger Enter
    fireEvent.keyDown(card, { key: 'Enter' });
    expect(container.querySelector('#detail-modal')?.classList.contains('open')).toBe(true);

    // Other key (no-op)
    fireEvent.keyDown(card, { key: 'Escape' });
  });
});
