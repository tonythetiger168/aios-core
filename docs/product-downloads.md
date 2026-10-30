# Product website and downloads

The existing Cloudflare Pages project `aios-registry` serves the product homepage and four
local edition ZIP files directly from `/downloads/`. Visitors do not need a GitHub account.
The skill registry protocol remains at `/index.json` and `/skills/`; its human catalog is
at `/skills.html`.

Build locally with Python 3.11+:

```sh
python scripts/build-registry.py registry _site --url https://aios-registry-47b.pages.dev/
mv _site/index.html _site/skills.html
python scripts/build-products.py _site
python -m http.server --directory _site
```

The registry workflow builds these same files on main and publishes them to Cloudflare Pages
only (GitHub Pages is no longer used). Publication requires the
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` repository secrets. PR builds validate without
deploying. Product, runtime, guide, and packaging changes trigger a fresh download build.

`products.json` records each package version, size and SHA-256; `/downloads/SHA256SUMS.txt`
lets visitors verify their downloads. ZIP files contain the runtime, edition settings,
policy and Traditional Chinese guide. Python, model weights and Docker are not bundled.

Only Core editions are downloadable. Team features, independent roadmap modules and the paid
Cloud service are not released by this packaging process. Cloud pricing follows the existing
edition roadmap; the page explicitly labels it planned and offers no checkout until a real
hosted service and billing integration exist.
