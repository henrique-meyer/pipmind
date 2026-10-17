# PipMind logo

A cream brain represents the persistent mind described in the [architecture](../docs/architecture.md#persistent-mind). A mint market trace crosses it, connecting that mind to forex decisions. The navy tile, warm cream, flat shapes, and two-tone accent follow the visual family used by DocPilot, QuillSign, and BoltClock.

| File | Use |
| --- | --- |
| `logo.svg` | Square app icon and SVG favicon; 64 × 64 viewBox with a rounded navy tile. |
| `logo.png` | 1024 × 1024 export of the square icon, with transparent outer corners. |
| `logo-mark.svg` | The same symbol with a tight viewBox and transparent background, for headers and inline branding. |
| `logo-mark.png` | Transparent mark exported at 1024 pixels high, preserving its aspect ratio. |

The SVGs are the editable sources. Both contain the same `pipmind-mark` group. Keep those groups identical when making an approved change, then regenerate the PNGs from the SVGs. Do not redraw or independently edit the PNGs.

```html
<link rel="icon" href="/assets/logo.svg" type="image/svg+xml">
```

The transparent mark includes a warm outline so its cream silhouette remains visible on light surfaces. Use the square tile when a surface is too busy for the mark.

In the text wordmark `PipMind.`, the period always uses the brand green (`#199D83`), including header/footer and hover/focus states. Wrap it in the shared `brand-dot` class so it keeps its own color rather than inheriting the link color.

| Color | Hex |
| --- | --- |
| Navy tile and details | `#0C2340` |
| Cream mind | `#F4F0E6` |
| Warm shadow | `#E4D8C4` |
| Silhouette outline | `#CDBFA8` |
| Mint trace | `#52D6AE` |
| Deep teal facet | `#199D83` |

PNG exports use an SVG renderer at a height of 1024 pixels, with transparency preserved. No font, external image, or network resource is required by the SVGs.
