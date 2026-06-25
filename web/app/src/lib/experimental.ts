import { EXPERIMENTAL_ROUTES } from './experimental-routes.mjs';

export { EXPERIMENTAL_ROUTES };

// Visible only in `astro dev`. Every production build (PROD) — which is what gets deployed as the
// public release — hides experimental pages. There is intentionally no opt-in here: a route listed
// in EXPERIMENTAL_ROUTES is never part of a shipped site.
export const showExperimental = import.meta.env.DEV;

// Frontmatter guard for an experimental page. In dev returns undefined (render normally); in a
// production build returns a redirect Response to home, so the page is never served publicly.
//   ---
//   import { redirectIfExperimental } from '../lib/experimental';
//   const gone = redirectIfExperimental(Astro);
//   if (gone) return gone;
//   ---
export function redirectIfExperimental(astro: { redirect: (path: string, status?: number) => Response }): Response | undefined {
  return showExperimental ? undefined : astro.redirect('/', 302);
}
