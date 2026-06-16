# WoWSims MoP Local Runner

This bundle contains a Python orchestration script for running local WoWSims Mists of Pandaria Classic sims from WowSimsExporter data.

## Files

- `wowsims_mop_runner.py` — main Python script.
- `CODEX_GOAL_wowsims_mop_runner.md` — a `/goal` prompt for Codex to harden the repo-specific adapters.

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
4. When prompted for a WoWSims template/share link, provide a known-good WoWSims share link or `RaidSimRequest` JSON for the same class/spec. This preserves your sim settings, APL, buffs, debuffs, and encounter settings.
5. Choose one of:
   - `normal` — current gear only.
   - `batch` — current gear plus bag-item combinations.
   - `upgrade` — single-item replacements and an upgrade report.
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
  --min-ilvl 522 \
  --max-ilvl 580 \
  --max-db-candidates 300
```

## Output

Every run writes to a timestamped folder under `wowsims_mop_results/`:

- `effective_raid_sim_request.json`
- `normal_report.md`, `batch_report.md`, or `upgrade_report.md`
- `batch_results.csv` or `upgrade_results.csv`
- per-sim request/result JSON files under `runs/`

## Important limitations

The current MoP `wowsimcli` is a low-level sim runner. It accepts `RaidSimRequest` protojson and emits `RaidSimResult` JSON. The browser UI contains additional import, default-setting, batch, reforge, gem, and enchant behavior. This script is designed around that reality:

- It can run accurate sims when given a known-good WoWSims template/share link or `RaidSimRequest` JSON.
- It can inject WSE gear/talents/glyphs into that template.
- It can run single-item replacement simulations.
- It will not pretend to have fully optimized gem/enchant/reforge results unless a proven upstream optimizer adapter is added.

Use `CODEX_GOAL_wowsims_mop_runner.md` to have Codex wire the repo-specific importer/optimizer layer into the script.
