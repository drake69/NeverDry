# Publishing to HACS — Step-by-step guide

## Prerequisites

- A public GitHub repository containing the integration
- The repo must follow the HACS structure conventions
- A GitHub account

## Repository structure for HACS

HACS expects this layout in the **root** of the GitHub repository:

```
/
├── custom_components/
│   └── never_dry/
│       ├── __init__.py
│       ├── const.py
│       ├── manifest.json
│       ├── sensor.py
│       └── hacs.json         ← HACS metadata (optional, for custom repos)
├── hacs.json                 ← HACS metadata (for default repos)
├── README.md
└── LICENSE
```

> **Important**: HACS looks for `custom_components/` at the repository root.
> Our dev layout has it under `sw_artifacts/`, so we need to either:
> 1. Publish from a separate repo with the correct structure, or
> 2. Restructure `sw_artifacts/` so `custom_components/` is at root.

## Option A: Separate GitHub repo (recommended)

Create a dedicated repo (e.g., `drake69/NeverDry`) with only the publishable files:

```bash
# From sw_artifacts/
mkdir -p /tmp/ha-dryness-index/custom_components/never_dry
cp custom_components/never_dry/*.py /tmp/ha-dryness-index/custom_components/never_dry/
cp custom_components/never_dry/manifest.json /tmp/ha-dryness-index/custom_components/never_dry/
cp custom_components/never_dry/hacs.json /tmp/ha-dryness-index/
cp README.md /tmp/ha-dryness-index/
# Add LICENSE file
```

## Option B: Publish from this repo

Move `custom_components/` to the repository root and update paths accordingly.

## Steps to publish

### 1. As a custom repository (immediate)

Users can install from any public GitHub repo:

1. Push your repo to GitHub
2. In Home Assistant, go to **HACS** → **Integrations** → **⋮** → **Custom repositories**
3. Enter the GitHub URL and select **Integration**
4. Search for "NeverDry" and install
5. Restart Home Assistant

This works immediately — no approval process needed.

### 2. As a default HACS repository (requires review)

To appear in the HACS store by default:

1. **Ensure your repo meets all requirements:**
   - [ ] Public GitHub repository
   - [ ] `hacs.json` in repository root
   - [ ] `manifest.json` in `custom_components/never_dry/`
   - [ ] `README.md` with description and installation instructions
   - [ ] `LICENSE` file (MIT, Apache 2.0, etc.)
   - [ ] At least one GitHub release/tag
   - [ ] Repository description set on GitHub
   - [ ] Repository topics include `hacs` and `home-assistant`

2. **Create a GitHub release:**
   ```bash
   git tag -a v0.1.0 -m "Initial release"
   git push origin v0.1.0
   ```

3. **Submit for inclusion:**
   - Go to https://github.com/hacs/default
   - Open a new issue using the "New default repository" template
   - Fill in the repository URL and category (Integration)
   - Wait for automated validation and maintainer review

4. **Automated checks will verify:**
   - Repository structure is correct
   - `manifest.json` has all required fields
   - Integration loads without errors
   - README exists and is informative
   - At least one release exists

### Timeline

- **Custom repository**: instant — works as soon as the repo is public
- **Default repository**: typically 1–4 weeks for review and approval

## Versioning

Update version in two places when releasing:
1. `manifest.json` → `"version": "X.Y.Z"`
2. Git tag → `vX.Y.Z`

These must match for HACS to correctly track updates.

## GitHub Actions (optional)

Add CI to validate on every push:

```yaml
# .github/workflows/validate.yml
name: Validate
on: [push, pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with:
          category: integration
```

This runs the same checks HACS uses during the default repo submission.
