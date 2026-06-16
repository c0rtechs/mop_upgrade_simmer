# WoWSims MoP Local Runner

This bundle contains a Python orchestration script for running local WoWSims Mists of Pandaria Classic sims from WowSimsExporter data.

## Files

- `wowsims_mop_runner.py` - main Python script.
- `CODEX_GOAL_wowsims_mop_runner.md` - a `/goal` prompt for Codex to harden the repo-specific adapters.

## Requirements

- Python 3.10+
- `git` on PATH
- Internet access for first-run clone/update and optional Wowhead source lookups
- Recommended fallback build tools if a matching release CLI asset is unavailable:
  - Go toolchain
  - The build dependencies required by the `wowsims/mop` repo

The script has no Python package dependencies outside the standard library.

## Basic usage

Place `wowsims_mop_runner.py` in a folder where you want the repos and results to live, then run:

```bash
python wowsims_mop_runner.py
```

On first run, it will create:

```text
./mop
./exporter
./.wowsims_mop_runner/
./wowsims_mop_results/
```

## Recommended accurate workflow

1. In-game, run:

   ```text
   /wse export
   ```

2. Copy the exporter string.
3. Start the script and paste the WSE export when prompted.
4. When prompted for a WoWSims template/share link, provide a known-good WoWSims share link or `RaidSimRequest` JSON for the same class/spec when you want an exact UI configuration. If no template is provided, the runner loads an official default build JSON from the checked-out `wowsims/mop` UI files when one can be resolved for the exported spec. If no official default build is available, it fails closed instead of inventing sim settings.
5. Choose one of:
   - `normal` - current gear only.
   - `batch` - current gear plus bag-item combinations.
   - `upgrade` - single-item replacements and an upgrade report.
6. For `batch` or `upgrade`, paste the WSE bag-items export when prompted.

## Noninteractive examples

Normal sim with a WSE export file and a WoWSims share link:

```bash
python wowsims_mop_runner.py \
  --mode normal \
  --export @my_wse_export.json \
  --template "https://www.wowsims.com/mop/windwalker_monk/#..." \
  --iterations 20000
```

Normal sim from WSE-only input, using an official default build from the local
`wowsims/mop` checkout when available:

```bash
python wowsims_mop_runner.py \
  --mode normal \
  --export @fixtures/brewmaster_wse.json \
  --iterations 1000 \
  --no-prompt
```

Normal sim with the committed fixture WSE export and fixture template:

```bash
python wowsims_mop_runner.py \
  --mode normal \
  --export @fixtures/brewmaster_wse.json \
  --template @fixtures/brewmaster_template.json \
  --iterations 1000 \
  --no-prompt
```

Upgrade sims from bag items, reporting upgrades of at least 5% DPS:

```bash
python wowsims_mop_runner.py \
  --mode upgrade \
  --export @my_wse_export.json \
  --template @my_known_good_raid_sim_request.json \
  --bag-export @my_wse_bag_export.json \
  --iterations 20000 \
  --upgrade-threshold 5 \
  --workers 4
```

Scan local DB candidates instead of bag candidates:

```bash
python wowsims_mop_runner.py \
  --mode upgrade \
  --export @my_wse_export.json \
  --template @my_known_good_raid_sim_request.json \
  --upgrade-candidate-source db \
  --phase 4 \
  --min-ilvl 522 \
  --max-ilvl 580 \
  --max-db-candidates 300
```

`--phase` limits candidate items to a maximum MoP content phase. If omitted, the
runner uses a phase value from template/settings JSON when one is present.

Resume an interrupted run by choosing a stable output directory:

```bash
python wowsims_mop_runner.py \
  --mode upgrade \
  --export @my_wse_export.json \
  --template @my_known_good_raid_sim_request.json \
  --bag-export @my_wse_bag_export.json \
  --output-dir ./wowsims_mop_results/my_upgrade_run \
  --resume
```

Sim requests and results are cached by a stable SHA-256 hash of the canonical
`RaidSimRequest` JSON under `runs/_requests/` and `runs/_results/`. The
results CSV includes the request hash used for resume lookup.

## Output

Every run writes to a timestamped folder under `wowsims_mop_results/`:

- `effective_raid_sim_request.json`
- `normal_report.md`, `batch_report.md`, or `upgrade_report.md`
- `batch_results.csv` or `upgrade_results.csv`
- `skipped_items.csv` when candidate items are rejected before simming
- hash-addressed per-sim request/result JSON files under `runs/_requests/` and
  `runs/_results/`

Upgrade CSV rows include item name, slot, item level, phase, quality, DPS,
DPS stdev and 95% confidence half-width when reported by the sim, absolute DPS
delta, percent delta, source text, optimization status, selected
gem/enchant/reforge details, request hash, and request/result paths.

## Item metadata and sources

The runner loads item metadata from the generated WoWSims MoP UI database:

```text
./mop/assets/database/db.json
./mop/assets/database/leftover_db.json
```

This is the same canonical `UIDatabase` JSON path currently used by the UI. The
runner resolves `UIItemSource` records with local `UINPC` and `UIZone` tables
before falling back to Wowhead. Upgrade reports include source text for simmed
items, and skipped candidate items are written with exact reasons to
`skipped_items.csv`.

The current tested upstream revisions for this metadata path were:

- `wowsims/mop`: `144b74b`
- `wowsims/exporter`: `5c28a2f`

## Important limitations

The current MoP `wowsimcli` is a low-level sim runner. It accepts `RaidSimRequest` protojson and emits `RaidSimResult` JSON. The browser UI contains additional import, default-setting, batch, reforge, gem, and enchant behavior. This script is designed around that reality:

- It can run accurate sims when given a known-good WoWSims template/share link or `RaidSimRequest` JSON.
- It can inject WSE gear/talents/glyphs into that template. WSE glyph spell IDs are converted to WoWSims glyph item IDs using the canonical local `glyphIds` database table, matching the official Addon importer behavior.
- For WSE-only input, it resolves the exported class/spec to the official UI spec directory, parses `defaultBuild: Presets.X` from `sim.ts` or `sim.tsx`, resolves `X` through `presets.ts`, and uses the imported `.build.json` as the default `IndividualSimSettings` before injecting WSE gear/talents/glyphs. If a spec has no unambiguous official build JSON, the runner refuses to run without `--template`.
- It can run single-item replacement simulations.
- It will not pretend to have fully optimized gem/enchant/reforge results unless a proven upstream optimizer adapter is added.
- `--require-optimizer` remains fail-closed. The current upstream optimizer lives in browser UI code and worker-backed reforge logic, not in `wowsimcli`.
- For exact production-quality UI settings, continue to provide a known-good template/share link from the official UI; the WSE-only path uses the official default build, not a custom UI configuration.
- Candidate filtering uses canonical DB class, armor, weapon/ranged weapon, hand/offhand, profession, faction, unique-equipped, limit-category, item-level, and phase data. Weapon and slot rules mirror the upstream UI class tables and `canEquipItem` behavior where it can be represented from the local request.

## Validation

Current local validation commands:

```bash
python -m unittest -v
python -m py_compile wowsims_mop_runner.py tests/test_item_database.py tests/test_payload_parsing.py
python wowsims_mop_runner.py --mode normal --export @fixtures/brewmaster_wse.json --template @fixtures/brewmaster_template.json --iterations 1000 --no-prompt --output-dir ./.wowsims_mop_runner/debug_fixture_template_normal_1000
python wowsims_mop_runner.py --mode normal --export @exports/equipped_only_export.json --iterations 1 --no-prompt --output-dir ./.wowsims_mop_runner/debug_wse_only_official_default
python wowsims_mop_runner.py --mode upgrade --export @exports/equipped_only_export.json --bag-export @exports/batch_bag_items_export.json --iterations 1 --no-prompt --output-dir ./.wowsims_mop_runner/debug_upgrade_wse_only_official_default --workers 4 --upgrade-threshold 0
```

The saved local `exports/equipped_only_export.json` and
`exports/batch_bag_items_export.json` were also used as a smoke check for the
canonical DB candidate path. They are local user data and are not required by
the test suite.

Use `CODEX_GOAL_wowsims_mop_runner.md` to have Codex wire the repo-specific importer/optimizer layer into the script.
