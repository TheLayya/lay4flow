# lay

The visual direction was approved from the character, login and dashboard concepts on 2026-10-14. The original character is a fully clothed young adult heroine drawn in a Japanese anime style, with sakura hair, teal eyes, ice-blue ribbons and a white-and-blue studio jacket.

All raster illustrations were generated using this project's existing Flow image API with `gemini-3.0-pro-image`, referencing the approved character. No stock artist or character artwork is used. UI text, navigation, statistics and forms remain real Vue / Element Plus elements.

| Asset | Purpose | Preparation |
|---|---|---|
| `koharu-character.webp` | Full character, missing-page illustration | Chroma-key extraction, connected silhouette mask, WebP |
| `koharu-portrait.webp` | Navigation and compact empty states | Face crop from the same character |
| `koharu-avatar.webp` | Shared brand avatar on login and sidebar | Square face crop from `koharu-portrait.webp` |
| `favicon.png` | Browser tab icon | Rounded 64 × 64 PNG from `koharu-avatar.webp` |
| `koharu-welcome.webp` | Studio canvas and credential guidance | Upper-body crop with alpha |
| `koharu-login.webp` | Login scene | Standalone 2K anime studio illustration, responsive WebP |
| `koharu-banner.webp` | Dashboard welcome banner | Standalone 2K landscape illustration, calm text-safe area |
| `koharu-login-background.webp` | Animated login scene background plate | Reference edit of the approved room, character removed |
| `koharu-motion-*.webp` | Body, head, hair tips and greeting arm | Segmented from the approved character; same 772×1680 coordinates |

| `koharu-motion-base.webp` | SVG character body | Approved character with moving interiors removed and a small seam underlap |
| `koharu-motion-head.webp` | SVG head and ribbons | Same original artwork and 772 × 1680 coordinate system |
| `koharu-motion-wave.webp` | SVG greeting hand and cuff | Same original artwork, separated along the cuff boundary |
| `koharu-motion-hair-left.webp`, `koharu-motion-hair-right.webp` | SVG long-hair tips | Same original artwork, isolated for restrained secondary motion |
| `koharu-motion-base-energetic.webp`, `koharu-motion-head-energetic.webp` | Larger head gesture | Original artwork with wider collar and hair-edge overlap |
| `koharu-wave-displacement.png` | Continuous forearm wave | Smooth, neutral-centered R/G displacement field, 972 × 1680 with 100 px overscan |

The login character is assembled by `components/KoharuScene.vue` inside an inline SVG. The energetic performance has a 750 ms spring entrance and a 3.6 s loop of body sway, head inclination, large continuous forearm waving, hair follow-through and drawn motion accents. A smooth `feDisplacementMap` moves the assembled arm and sleeve together; the old cuff layer remains at rest. Clipped original eye artwork and SVG lashes create the blink. The original detailed anime artwork remains raster inside SVG image layers, preserving the approved style. CSS and SVG timelines pause when hidden, support the on-page pause/replay controls and respect reduced-motion preferences. `koharu-motion.json` records layer pivots, mask geometry and the energetic displacement parameters; it is production metadata, not credentials.

The original layer records retain their original small-angle limits; the active energetic rig is described in the `energetic` entry. The wave field is sampled in sRGB, centered by subtracting `0.5/255` from R/G, smoothed by 0.6 source pixels and animated between ±120. Source-space rest and extreme-pose visual checks are kept in `../../output/anime-motion/`.

Generated source images, the exact prompts and preparation metadata are kept outside the checkout in `../../output/imagegen/anime-preview/` from the repository root. The approved concepts themselves are review artifacts, not screenshots baked into the application. Production assets live here and are emitted into `static/app/assets/` by Vite.
