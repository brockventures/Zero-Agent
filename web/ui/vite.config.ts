/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react, { reactCompilerPreset } from '@vitejs/plugin-react';
import babel from '@rolldown/plugin-babel';

// The build output is written to web/static/ui/ (fixed names, no hashes) during
// local builds and the multi-stage Docker build. See AGENTS.md ("web/ui/").
export default defineConfig({
  base: './',
  plugins: [react(), babel({ presets: [reactCompilerPreset()] })],
  // Tests read the e-ink settle predicate from sidecars/ (raw import), outside this root.
  server: { fs: { allow: ['../..'] } },
  build: {
    outDir: '../static/ui',
    emptyOutDir: true,
    rollupOptions: {
      // The JS entry, not index.html: index.html is only the dev-server shell.
      input: 'src/main.tsx',
      output: {
        entryFileNames: 'app.js',
        assetFileNames: 'app[extname]',
        codeSplitting: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    css: true,
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/**/types.ts', 'src/sse/fake-event-source.ts'],
      thresholds: {
        statements: 95,
        branches: 90,
        functions: 95,
        lines: 95,
      },
    },
  },
});
