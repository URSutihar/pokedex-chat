# Vendored frontend libraries

No CDN, no third-party script origin — which is what makes storing a credential
in `localStorage` defensible, and what lets the CSP be `script-src 'self'`.

The cost of vendoring is that nothing tells you when an upstream security fix
lands. Mermaid and DOMPurify both have XSS advisory history, so this file records
exactly what is here. Check it against upstream releases when CI flags an advisory.

| File | Package | Bytes | SRI (sha384) |
|---|---|---:|---|
| `marked.umd.js` | marked@18.0.7 | 42,757 | `sha384-7njNzKcJUBdezPGfqUrIFizi2Qk…` |
| `purify.min.js` | dompurify@3.4.12 | 29,209 | `sha384-piCcpDdJ7qVeK4Tv8Z6Hpcr3ZBI…` |
| `highlight.min.js` | @highlightjs/cdn-assets@11.11.1 | 127,496 | `sha384-RH2xi4eIQ/gjtbs9fUXM68sLSi9…` |
| `mermaid.min.js` | mermaid@10.9.6 | 3,337,508 | `sha384-qX9VvWkP79m/O121ZE6sOYp0nf/…` |
| `katex/katex.min.js` | katex@0.18.1 | 271,715 | `sha384-ycJ6GAwiS15LoUPipwJOrWTvkUH…` |
| `katex/katex.min.css` | katex@0.18.1 | 24,727 | `sha384-1vdNCNel6Tx/NQa8IR1mGOGKsbG…` |
| `katex/auto-render.min.js` | katex@0.18.1 | 3,486 | `sha384-bjyGPfbij8/NDKJhSGZNP/khQVg…` |

Refresh:

```bash
npm i marked dompurify katex highlight.js mermaid@10 @highlightjs/cdn-assets
# copy the dist files listed above into static/vendor/, then:
python scripts/vendor_manifest.py     # rewrites this table
```

Full hashes: `static/vendor/_sri.json`.
