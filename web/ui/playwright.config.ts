import { defineConfig, devices } from '@playwright/test';
import fs from 'node:fs';

const chromeExecutable =
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ||
  (['/usr/bin/google-chrome-stable', '/usr/bin/google-chrome'].find((p) =>
    fs.existsSync(p),
  ));

export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:18080',
    trace: 'retain-on-failure',
    ...(chromeExecutable
      ? { launchOptions: { executablePath: chromeExecutable } }
      : {}),
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command:
          'sh -c "test -f ../static/ui/app.js || npm run build" && mkdir -p ../../bin && go build -o ../../bin/server-e2e ../../cmd/server && sh -c \'CONFIG_E2E=$(mktemp) && sed "s/port: 8080/port: 18080/" "$CONFIG_PATH" > "$CONFIG_E2E" && CONFIG_PATH="$CONFIG_E2E" ../../bin/server-e2e\'',
        port: 18080,
        reuseExistingServer: !process.env.CI,
        timeout: 30000,
        env: {
          CONFIG_PATH: '../../deploy/examples/config.yaml',
          FAMILY_CALENDAR_URL: 'http://127.0.0.1:9/fixture.ics',
          TODOIST_API_TOKEN: 'fixture-token',
          GOOGLE_PHOTOS_ALBUM_URL: 'http://127.0.0.1:9/fixture-photos',
        },
      },
});
