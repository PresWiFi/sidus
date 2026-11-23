# Proxmox VE script

These files follow the [community-scripts](https://github.com/community-scripts/ProxmoxVE) layout, so the same
script can later be submitted there:

| File | What it does |
|---|---|
| `ct/sidus.sh` | Runs on the Proxmox host: creates the container and handles `update` |
| `install/sidus-install.sh` | Runs inside the container: installs Sidus from the latest GitHub release |
| `json/sidus.json` | Metadata for the community-scripts website |

`ct/sidus.sh` points `_CS_DEFAULT_URL` at this folder, so the engine fetches `install/sidus-install.sh`
from this repo. The engine itself comes from `community-scripts/core`.

The script installs from GitHub releases, so at least one release must exist (see "Releasing" in the main README).

## Testing a branch

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/PresWiFi/sidus/<branch>/proxmox/ct/sidus.sh | sed 's#sidus/main/proxmox#sidus/<branch>/proxmox#')"
```

Test both a fresh install and an `update` after publishing a newer release.

## Submitting to community-scripts

New scripts go to [DevScripts](https://github.com/community-scripts/DevScripts) first. They're promoted to ProxmoxVE after testing there.
Their requirements for new apps: **600+ GitHub stars, at least 6 months old, actively maintained, and published releases.**
Sidus was created on 2026-10-25, so it turns 6 months old on **2027-04-25**. It also needs the stars by then.

When it qualifies:

1. Fork DevScripts and copy `ct/sidus.sh`, `install/sidus-install.sh`, and `json/sidus.json` to the same paths.
2. In `ct/sidus.sh`, set line 2 to `_CS_DEFAULT_URL="https://raw.githubusercontent.com/community-scripts/DevScripts/main"`.
   Also remove the `_CS_FALLBACK_HEADER` banner lines; their generator makes the official header.
3. Check the script against their current [AGENTS.md](https://github.com/community-scripts/DevScripts/blob/main/AGENTS.md) checklist; the rules change.
4. Test on a real Proxmox host (fresh install and update), then open a "New Script" issue and a PR in DevScripts.
   Their PR template asks which AI model helped write the script.
5. Ask selfhst/icons for a Sidus icon, or keep the logo URL pointing at this repo.
