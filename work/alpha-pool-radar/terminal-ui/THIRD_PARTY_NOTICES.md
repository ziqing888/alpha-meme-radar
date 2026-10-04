# Frontend provenance

Derived from https://github.com/bytegen-dev/evm-market-maker/tree/3f37476efa2e6d5a3bcd8c7427e240f748832010/dashboard

Pinned commit: 3f37476efa2e6d5a3bcd8c7427e240f748832010. License: ISC (see LICENSE).

Imported the dashboard source, UI primitives, query provider, formatting helpers, chart components and styles. Original business pages remain as reference, outside the application entry graph. Terminal.tsx and terminal-api.ts connect this derivative to the local Alpha Radar data and controls. Removed third-party font loads; adjusted tabs for the new page list. The upstream volume-generation backend and trade routes are not installed or invoked.

Windows installation regenerated package-lock.json because upstream's lock omitted optional npm dependency records. No install scripts are run.

IBM Plex Sans and IBM Plex Mono are bundled locally through @fontsource packages under SIL Open Font License 1.1. Copyright and license texts ship in public/licenses/. No font requests are sent to Google Fonts. Chinese text uses the locally installed Microsoft YaHei UI / Microsoft YaHei or platform fallback.
