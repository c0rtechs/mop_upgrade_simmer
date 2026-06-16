/goal Review, harden, and complete the WoWSims MoP local runner in `wowsims_mop_runner.py` so it accurately supports WSE addon exports, normal sims, batch sims, and custom upgrade sims against the latest `wowsims/mop` and `wowsims/exporter` repositories.

## Context

The script currently performs repo clone/update, obtains `wowsimcli`, prompts for WSE exports, builds or injects a `RaidSimRequest`, runs the MoP CLI `sim` command, generates batch/single-swap requests, caches local item metadata, optionally falls back to Wowhead MoP Classic item pages for source data, and writes CSV/markdown reports.

The known hard parts are intentionally isolated:

1. WSE addon JSON -> fully faithful WoWSims `IndividualSimSettings` / `RaidSimRequest` conversion.
2. Accurate default class/spec settings, APL, buffs, encounter, consumables, professions, glyphs, and race/class/spec enum mapping.
3. True per-candidate gem/reforge/enchant optimization.
4. Accurate item metadata, source metadata, class usability, weapon restrictions, phase filtering, and unique-equipped handling.
5. Fast, reliable batch/upgrade execution without overloading the machine.

## Required research first

1. Pull the latest `wowsims/mop` and `wowsims/exporter`.
2. Inspect `cmd/wowsimcli` in MoP and confirm available commands/flags.
3. Inspect UI import code for the Addon/WSE importer. Find the exact TypeScript path that parses the WSE JSON produced by `exporter/ExportStructures/Character.lua`, `EquipmentSpec.lua`, and `ItemSpec.lua`.
4. Inspect UI logic for batch sims, bulk items, reforge optimizer, gem optimizer, enchant optimizer, and any stat-weight or EP machinery.
5. Inspect item database generation/assets for `UIDatabase`, `UIItem`, `UIItemSource`, `UINPC`, and `UIZone` to identify the most reliable local source data file.
6. Check whether current MoP backend has `RunBulkSimAsync`, `BulkSimRequest`, or analogous APIs hidden outside `wowsimcli`; if it does, expose a CLI command instead of running thousands of separate processes.

## Implementation tasks

- [ ] Replace all heuristic WSE -> request conversion with the same mapping used by the official UI importer.
- [ ] Add a deterministic path to create a valid `RaidSimRequest` from WSE-only input for every launched MoP spec, using official presets/defaults where available.
- [ ] Add a test fixture for at least one exported WSE character JSON and verify the generated `RaidSimRequest` is accepted by `wowsimcli sim`.
- [x] Add fixture tests for WSE bag export parsing and slot mapping.
- [ ] Implement true optimizer support for candidate items:
  - [ ] Auto-gem candidate based on official gem optimizer/default gems.
  - [ ] Auto-enchant candidate based on official enchant optimizer/default enchants.
  - [ ] Auto-reforge each candidate based on official reforge optimizer/settings.
  - [ ] Preserve profession-only gem/enchant restrictions.
  - [ ] Handle hit/expertise caps and soft caps exactly like the UI.
- [ ] If MoP backend supports bulk sim APIs, add a local CLI patch/overlay command and have Python call it instead of one process per candidate.
- [x] If backend bulk APIs are absent, optimize the Python multi-process runner with stable request hashing, result caching, resume support, and safe process limits.
- [x] Replace the metadata scanner with direct loading of the canonical generated item database.
- [x] Resolve `UIItemSource` into human-readable source text using local `UINPC` and `UIZone` tables before falling back to Wowhead.
- [ ] Improve Wowhead fallback parsing for MoP Classic pages, including drop bosses, raid difficulty, vendors, crafting spells, quests, reputation, and contained-in sources.
- [ ] Enforce usable-item filtering:
  - [x] Class allowlist.
  - [x] Armor proficiency.
  - [x] Weapon proficiency.
  - [x] Hand/offhand/two-hand constraints.
  - [x] Unique-equipped and limit-category constraints.
  - [x] Profession requirements.
  - [x] Faction restrictions.
  - [x] Phase and item-level filters.
- [x] Add a `--phase` option and default it from the template/settings when possible.
- [ ] Add richer reports:
  - [ ] Baseline DPS and confidence/iterations.
  - [ ] Upgrade DPS delta and percent delta.
  - [x] Source location/drop/craft/vendor/quest/reputation.
  - [ ] Slot, item level, phase, quality.
  - [ ] Optimization details: gems, enchants, reforges selected.
  - [x] Skipped items and exact skip reason.
- [ ] Add regression tests for:
  - [ ] `decodelink` share-link parsing.
  - [x] WSE character parsing.
  - [x] WSE bag parsing.
  - [ ] IndividualSimSettings -> RaidSimRequest conversion.
  - [x] Request gear injection.
  - [x] DPS extraction from `RaidSimResult`.
  - [x] Item source formatting.
  - [x] Slot filtering and class usability.
- [ ] Ensure Windows, macOS, and Linux behavior works.
- [ ] Update the README with exact tested commands and known limitations.

## Acceptance criteria

- [ ] `python wowsims_mop_runner.py --mode normal --export @fixtures/<spec>_wse.json --template @fixtures/<spec>_template.json --iterations 1000 --no-prompt` runs successfully and prints baseline DPS.
- [ ] `upgrade` mode runs baseline plus single-swap candidate sims from a WSE bag export and writes `upgrade_report.md` + `upgrade_results.csv`.
- [x] Every upgrade row includes item name, slot, DPS, percent change, and best available source location.
- [ ] When optimizer support is enabled, every candidate request is gemmed/enchanted/reforged using the official WoWSims logic or an explicitly equivalent backend implementation.
- [ ] If optimizer support is not available, the script refuses `--require-optimizer` and clearly annotates report rows as not fully optimized.
- [x] No skipped item is skipped silently; every skip has a reason.
- [x] The script never reports a candidate as usable unless the local DB or official importer proves it is usable.
- [ ] Existing behavior remains stdlib-only from Python's perspective.

## Final deliverable

Commit changes with a detailed message summarizing:

- How WSE import fidelity was achieved.
- How optimizer fidelity was achieved or why it is still explicitly unavailable.
- How source lookup works.
- How to run normal, batch, and upgrade modes.
- Test coverage added and the exact commands used to validate.
