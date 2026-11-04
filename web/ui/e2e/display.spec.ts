import { test, expect } from '@playwright/test';

let pageErrors: Error[] = [];

test.beforeEach(async ({ page }) => {
  pageErrors = [];
  page.on('pageerror', (err) => {
    pageErrors.push(err);
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const text = msg.text();
      // Ignore browser-internal network 404 logs (e.g. favicon or unmocked probe assets)
      if (text.includes('Failed to load resource') && text.includes('404')) {
        return;
      }
      pageErrors.push(new Error(`Console error: ${text}`));
    }
  });

  // Satisfy Chrome's automatic favicon request
  await page.route('**/favicon.ico', (route) => {
    void route.fulfill({ status: 204, body: '' });
  });
});

test.afterEach(async () => {
  expect(pageErrors).toEqual([]);
});

test.describe('Mirrormere Display E2E', () => {
  test('Kiosk Hydration & Live SSE (1920x1080)', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto('/display?ui=react');

    // Verify key structural containers mount cleanly
    await expect(page.locator('#mirrormere-app')).toBeVisible();
    await expect(page.locator('.header-zone')).toBeVisible();
    await expect(page.locator('#header-clock')).toBeVisible();
    await expect(page.locator('.carousel-viewport')).toBeVisible();

    // Verify live SSE connection reaches status-ok
    const statusDot = page.locator('#system-status-dot');
    await expect(statusDot).toHaveClass(/status-ok/, { timeout: 15000 });

    // Verify theme toggle cycling (day/night/auto)
    const themeBtn = page.locator('#theme-toggle-btn');
    await expect(themeBtn).toBeVisible();

    const initialTheme = await page.locator('html').getAttribute('data-theme');
    await themeBtn.click();
    const toggledTheme = await page.locator('html').getAttribute('data-theme');
    expect(toggledTheme).not.toBe(initialTheme);
    await expect(themeBtn).toHaveAttribute('data-mode', 'manual');

    // Cycle again
    await themeBtn.click();
    // Cycle third time back to auto
    await themeBtn.click();
    await expect(themeBtn).toHaveAttribute('data-mode', 'auto');
  });

  test('Pointer Swipe Physics & Blocker Isolation (1920x1080)', async ({ page }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto('/display?ui=react');

    await expect(page.locator('.carousel-viewport')).toBeVisible();
    await expect(page.locator('.screen-dot.active')).toHaveAttribute('data-screen-index', '0');

    // Ensure blocker element is present with [data-prevent-screen-swipe]
    await page.evaluate(() => {
      const blocker = document.querySelector('[data-prevent-screen-swipe]');
      if (!blocker) {
        const card = document.querySelector('.widget-card');
        if (card) {
          card.setAttribute('data-prevent-screen-swipe', 'true');
        }
      }
    });

    const blocker = page.locator('[data-prevent-screen-swipe]').first();
    await expect(blocker).toBeVisible();
    const blockerBox = await blocker.boundingBox();
    if (blockerBox) {
      const dragStart = blockerBox.x + blockerBox.width * 0.8;
      const dragEnd = blockerBox.x + blockerBox.width * 0.2;
      const dragY = blockerBox.y + blockerBox.height * 0.5;

      await page.mouse.move(dragStart, dragY);
      await page.mouse.down();
      await page.mouse.move(dragEnd, dragY, { steps: 10 });
      await page.mouse.up();

      // Blocker isolation: swipe inside [data-prevent-screen-swipe] does not advance carousel
      await expect(page.locator('.screen-dot.active')).toHaveAttribute('data-screen-index', '0');
    }

    // Pointer drag sequence across unblocked .carousel-viewport (>25% width)
    const viewport = page.locator('.carousel-viewport');
    const vpBox = await viewport.boundingBox();
    if (!vpBox) throw new Error('Viewport bounding box not found');

    const startX = vpBox.x + vpBox.width * 0.8;
    const endX = vpBox.x + vpBox.width * 0.2;
    const y = vpBox.y + vpBox.height - 15; // Bottom strip of carousel

    await page.mouse.move(startX, y);
    await page.mouse.down();
    await page.mouse.move(endX, y, { steps: 15 });
    await page.mouse.up();

    // Verify screen transition commits and updates .screen-dot.active
    await expect(page.locator('.screen-dot.active')).toHaveAttribute('data-screen-index', '1', {
      timeout: 5000,
    });
  });

  test('Interactive Widget & Video HUD Controls', async ({ page }) => {
    // Intercept screens and widgets for deterministic interactive calendar-grid and tasks
    const calendarData = {
      week: {
        range_label: 'Nov 1 – 7, 2026',
        days: [
          { date: '2026-11-01', day_name: 'Sun', day_num: '1', is_today: true, is_weekend: true, events: [] },
          { date: '2026-11-02', day_name: 'Mon', day_num: '2', is_today: false, is_weekend: false, events: [] },
          { date: '2026-11-03', day_name: 'Tue', day_num: '3', is_today: false, is_weekend: false, events: [] },
          { date: '2026-11-04', day_name: 'Wed', day_num: '4', is_today: false, is_weekend: false, events: [] },
          { date: '2026-11-05', day_name: 'Thu', day_num: '5', is_today: false, is_weekend: false, events: [] },
          { date: '2026-11-06', day_name: 'Fri', day_num: '6', is_today: false, is_weekend: false, events: [] },
          { date: '2026-11-07', day_name: 'Sat', day_num: '7', is_today: false, is_weekend: true, events: [] },
        ],
        members: [{ name: 'Family', color: '#8b5cf6', pattern: 'solid' }],
      },
      month: {
        month_label: 'November 2026',
        weeks: [],
        members: [{ name: 'Family', color: '#8b5cf6', pattern: 'solid' }],
      },
    };

    let taskCompleteCalled = false;
    await page.route('**/api/lists/*/items/*/complete', async (route) => {
      taskCompleteCalled = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });

    await page.route('**/api/tasks/*/complete', async (route) => {
      taskCompleteCalled = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });

    await page.route('**/api/screens', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          current_screen: 0,
          interval_seconds: 30,
          screens: [
            {
              index: 0,
              widgets: [
                { widget_id: 'interactive-cal', type: 'calendar-grid', origin: [0, 0], dimensions: [4, 2] },
                { widget_id: 'interactive-tasks', type: 'tasks', origin: [4, 0], dimensions: [2, 2] },
              ],
            },
          ],
        }),
      });
    });

    await page.route('**/api/widgets/interactive-cal', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          widget_id: 'interactive-cal',
          type: 'calendar-grid',
          origin: [0, 0],
          dimensions: [4, 2],
          config: { title: 'Family Calendar' },
          data: calendarData,
          view_model: calendarData,
          state: 'ok',
          online: true,
          assets_base: '/widgets/calendar-grid',
          timestamp: new Date().toISOString(),
        }),
      });
    });

    await page.route('**/api/widgets/interactive-tasks', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          widget_id: 'interactive-tasks',
          type: 'tasks',
          origin: [4, 0],
          dimensions: [2, 2],
          config: { list_name: 'Chores' },
          data: {
            items: [
              { id: 'chore-1', title: 'Water plants', done: false },
            ],
          },
          state: 'ok',
          online: true,
          assets_base: '/widgets/tasks',
          timestamp: new Date().toISOString(),
        }),
      });
    });

    await page.setViewportSize({ width: 1920, height: 1080 });
    await page.goto('/display?ui=react');

    // 1. Verify calendar view switching
    const monthBtn = page.locator('.cf-view-btn[data-view="month"]');
    const weekBtn = page.locator('.cf-view-btn[data-view="week"]');
    await expect(monthBtn).toBeVisible({ timeout: 10000 });
    await expect(weekBtn).toBeVisible();

    await monthBtn.click();
    await expect(page.locator('.widget-calendar-grid')).toHaveClass(/cf-view-month/);
    await expect(monthBtn).toHaveClass(/active/);

    await weekBtn.click();
    await expect(page.locator('.widget-calendar-grid')).toHaveClass(/cf-view-week/);
    await expect(weekBtn).toHaveClass(/active/);

    // 2. Verify task completion API interaction
    const taskItem = page.locator('.task-item[data-item-id="chore-1"]');
    await expect(taskItem).toBeVisible();
    await taskItem.click();

    // Verify optimistic completion state
    await expect(taskItem).toHaveClass(/task-item-done/);
    await expect(taskItem).toHaveClass(/pending-undo/);

    // Fast-forward or wait for the undo grace timer to fire API dispatch
    await page.waitForFunction(() => {
      return (window as unknown as { __taskCompleteCalled?: boolean }).__taskCompleteCalled === true;
    }, undefined, { timeout: 6000 }).catch(async () => {
      // If grace period is still counting down, check if request happened
      await expect.poll(() => taskCompleteCalled, { timeout: 5000 }).toBe(true);
    });
    expect(taskCompleteCalled).toBe(true);
  });

  test('E-Ink Settle & Zero-Overflow Contract (800x480)', async ({ page }) => {
    await page.setViewportSize({ width: 800, height: 480 });
    await page.goto('/display?ui=react');

    // Wait for e-ink hydration marker
    await page.waitForFunction(() => window.__einkReady === true, undefined, { timeout: 15000 });

    // Assert zero-overflow contract
    const { scrollWidth, scrollHeight } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
    }));

    expect(scrollWidth).toBeLessThanOrEqual(800);
    expect(scrollHeight).toBeLessThanOrEqual(480);
  });
});
