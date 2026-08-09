# WebUI v2 Updater

Opt-in installer/updater for [nodel-webui-v2](https://github.com/mcartmel/nodel-webui-v2)
during the alpha/beta period, until v2 ships inside the Nodel jar itself.
Deploy this node on any host that should carry the v2 overlay.

## What it does

- Installs/updates the v2 file set (`nodes.html`, `nodel.html`, `toolkit.html`,
  `components.html` + the `v2/` tree) into `<hostroot>/custom/content/`
- Verifies the release ZIP against its published SHA-256, then every extracted
  file against the per-file inventory in the release's `release.json`
- Staged install with rename-swap; previous version kept for **Rollback**
  (self-inverse — call it again to roll forward)
- `index.htm` withheld by default (upstream preserve-v1 collision policy);
  opt in via parameter to make v2 the landing page
- Scheduled checks **notify only** (`Update Available` event) — installs are
  always an explicit operator action
- No nodehost restart required; pages are picked up live

## State

Kept in `<hostroot>/custom/.webui-v2-updater/` (not web-served):
`installed.json` marker, one `backup/` generation, transient
`downloads/` + `staging/`.

## Requirements

- Nodel host new enough to serve `custom/content` (2.2.1+, present since 2017)
- Outbound HTTPS to api.github.com / github.com
- For v2's WebSocket transport the host needs a jar with unified HTTP/WS
  ports (Nov 2022, rev 493+); older hosts fall back to polling
