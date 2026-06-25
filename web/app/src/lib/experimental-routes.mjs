// Canonical list of experimental / work-in-progress routes (path only, no trailing slash).
// Single source of truth shared by:
//   - astro.config.mjs        — drops these from the generated sitemap
//   - src/lib/experimental.ts — the redirect guard each such page runs in its frontmatter
//   - public/robots.txt       — mirror any entry here as a `Disallow:` line (static file, manual)
//
// These pages render in `astro dev` for local iteration, but a production build (`just site`,
// i.e. what ships publicly) redirects them to home so they never appear in the public release.
export const EXPERIMENTAL_ROUTES = ['/lab'];
