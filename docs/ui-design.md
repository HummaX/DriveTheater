# UI design and interaction notes

## Direction

A dark projection room rather than a monitoring dashboard: blue-black canvas (`#0c1423`), cool layered surfaces (`#162438`), near-white ink (`#edf3ff`), quiet secondary text (`#acbcd4`), cyan light (`#79d8ed`) and violet accents (`#b09aff`). Gradients belong to room surfaces and primary actions, not every text label.

Body copy uses a local Trebuchet/Candara/DejaVu Sans stack; headings use Bahnschrift with local fallbacks. No font CDN or external font-download dependency is introduced.

## Layout and behavior

- Keep Drive library and Discover distinct. A movie catalog is not a list of files already cached on the server.
- Put movies and series in one artwork shelf. A Series badge opens its seasons and episodes; do not expand every episode in the main grid.
- Discovery uses portrait posters and a keyboard-focusable native movie dialog. Opening metadata never silently chooses a torrent.
- Keep Save to server and Download to device distinct. The latter is a direct attachment link, not a full-file JavaScript download buffer.
- Display real connection/job states, not decorative KPI counts or fabricated catalog entries.
- Use restrained card hover/focus feedback. Honor reduced motion and keep important information available on touch screens.
- Only icon-only and selected complex actions have visual tooltips. Delay them 500 ms, allow Escape dismissal, and keep them available while trigger/popup hover or keyboard focus remains. Ordinary labeled navigation does not need extra popups.

## Checks performed

Representative WCAG text-contrast calculations: primary ink on surface **14.04:1**, secondary text **8.11:1**, dark text on the cyan/violet primary-button stops **11.54:1 / 7.82:1**, tooltip text **10.78:1**. These sampled checks are not a complete accessibility certification.

Synthetic browser checks include no horizontal page overflow at 375 px, a movie dialog contained inside that viewport with focus inside it, mixed movie/series cards, real delayed tooltip behavior, and no source transfer on series/catalog navigation. Preview artwork/catalog records are explicitly synthetic and exist only in `tests/serve_fixture.py`.

## Research references

- [W3C: Content on Hover or Focus](https://www.w3.org/WAI/WCAG22/Understanding/content-on-hover-or-focus.html) — dismissible, hoverable, persistent help and keyboard access.
- [MDN: prefers-reduced-motion](https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-reduced-motion) — reduce non-essential motion without removing state feedback.

These references informed interaction/accessibility choices; no third-party movie UI or copyrighted artwork was copied. Web-search service availability was limited, so no claim is made of an exhaustive current-design survey.
