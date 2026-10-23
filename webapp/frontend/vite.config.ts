import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import tsconfigPaths from 'vite-tsconfig-paths';

export default defineConfig({
  base: '/dashboard/',
  plugins: [react(), tailwindcss(), tsconfigPaths()],
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
  },
  server: { host: '0.0.0.0' },
});
