# WoWSims MoP Runner Canonical DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace heuristic item metadata scanning with canonical WoWSims MoP `assets/database/db.json` loading and use it for source formatting, item usability, phase filtering, and explicit skip reasons.

**Architecture:** Keep Python stdlib-only by reading the upstream generated JSON database directly. Treat UI optimizer/importer parity as a separate adapter problem; this slice must improve metadata fidelity without pretending to implement the browser-only reforge/gem/enchant optimizer.

**Tech Stack:** Python 3.10+, `unittest`, local `wowsims/mop` checkout, generated WoWSims `assets/database/db.json`.

---

### Research Findings

**Upstream revisions:**
- `wowsims/mop`: `144b74b`
- `wowsims/exporter`: `5c28a2f`

**CLI surface:**
- `cmd/wowsimcli` currently registers `version`, `sim`, and `decodelink`.
- `sim` accepts `--infile`, `--outfile`, and `--verbose`.
- Search did not find `RunBulkSimAsync` or `BulkSimRequest`; the available backend entry points are `RunRaidSim`, `RunRaidSimAsync`, `RunRaidSimConcurrent`, and `RunRaidSimConcurrentAsync`.

**Official WSE importer path:**
- `mop/ui/core/components/individual_sim_ui/importers/individual_addon_importer.tsx`
- Exporter structures reviewed:
  - `exporter/ExportStructures/Character.lua`
  - `exporter/ExportStructures/EquipmentSpec.lua`
  - `exporter/ExportStructures/ItemSpec.lua`

**Official importer behavior to preserve in future work:**
- Parse class/race/professions with UI `nameToClass`, `nameToRace`, and `nameToProfession`.
- Convert glyph spell IDs through `Database.glyphSpellToItemId`.
- Drop null gear entries before `EquipmentSpec.fromJson`.
- Zero-fill gem holes before `EquipmentSpec.fromJson`.
- Load leftovers if an imported item is missing from the main DB, then call `lookupEquipmentSpec`.

**Canonical item DB:**
- `mop/assets/database/db.json` exists and is loaded by the UI because `mop/ui/core/proto_utils/database.ts` has `READ_JSON = true`.
- The DB contains `items`, `npcs`, `zones`, `gems`, `enchants`, `reforgeStats`, `itemEffectRandPropPoints`, `glyphIds`, and more.
- The DB uses numeric proto enum values, so Python must use explicit enum maps from `mop/proto/common.proto` and `mop/proto/ui.proto`.

**Optimizer reality:**
- Browser bulk sim calls `BulkTab.optimizeReforges`, which depends on `ReforgeOptimizer`, player/spec defaults, EP weights, gem rules, worker-backed LP solving, and current UI state.
- Implementing “true optimizer parity” inside Python without reusing that stack would be a hack.
- This slice must keep `--require-optimizer` fail-closed and annotate unoptimized rows honestly.

### Task 1: Canonical DB Fixture Tests

**Files:**
- Create: `tests/test_item_database.py`
- Modify: none

- [ ] **Step 1: Write failing tests for canonical DB parsing and source formatting**

```python
from pathlib import Path
import tempfile
import unittest

import wowsims_mop_runner as runner


class CanonicalItemDatabaseTests(unittest.TestCase):
    def test_loads_canonical_db_json_with_resolved_drop_source(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            index = runner.load_item_index(repo / "mop", Path(temp), refresh=True)

        item = index[60226]
        self.assertEqual(item.name, "Dargonax's Signet")
        self.assertEqual(item.type, "ItemTypeFinger")
        self.assertEqual(item.ilvl, 379)
        self.assertEqual(item.phase, 1)
        self.assertIn("Drop: Sinestra in The Bastion of Twilight", runner.format_sources(item.sources))

    def test_formats_rep_source_with_faction_and_level_names(self):
        text = runner.format_sources([
            {"rep": {"repFactionId": 1270, "repLevel": 8, "factionId": 2}},
        ])

        self.assertEqual(text, "Reputation: Shado-Pan at Exalted (Horde)")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_item_database -v`

Expected: tests fail because `load_item_index` still uses heuristic scanning and `format_sources` does not resolve the canonical numeric enum/source fields.

### Task 2: Canonical DB Loader

**Files:**
- Modify: `wowsims_mop_runner.py`
- Test: `tests/test_item_database.py`

- [ ] **Step 1: Replace heuristic scanning with direct DB JSON loading**

Implement these helpers in `wowsims_mop_runner.py`:

```python
def canonical_db_paths(mop_dir: Path) -> list[Path]:
    return [
        mop_dir / "assets" / "database" / "db.json",
        mop_dir / "assets" / "database" / "leftover_db.json",
    ]
```

```python
def load_canonical_item_index(mop_dir: Path) -> dict[int, ItemMeta]:
    index: dict[int, ItemMeta] = {}
    for path in canonical_db_paths(mop_dir):
        if not path.exists():
            continue
        data = read_json_file(path)
        if not isinstance(data, dict):
            continue
        zones = {int(z["id"]): str(z.get("name") or "") for z in data.get("zones", []) if isinstance(z, dict) and z.get("id")}
        npcs = {int(n["id"]): str(n.get("name") or "") for n in data.get("npcs", []) if isinstance(n, dict) and n.get("id")}
        for item in data.get("items", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            meta = item_meta_from_dict(item, zones=zones, npcs=npcs)
            if meta.id and (meta.id not in index or richer_meta(meta, index[meta.id])):
                index[meta.id] = meta
    return index
```

- [ ] **Step 2: Preserve cache behavior**

Update `load_item_index` so it:
- Reads `.wowsims_mop_runner/cache/item_index.json` unless `refresh=True`.
- On refresh or cache miss, calls `load_canonical_item_index`.
- Fails with `RunnerError` if neither canonical DB file exists.
- Does not fall back to repository-wide regex scanning.

- [ ] **Step 3: Run tests to verify pass**

Run: `python -m unittest tests.test_item_database -v`

Expected: PASS.

### Task 3: Enum Mapping And Source Rendering

**Files:**
- Modify: `wowsims_mop_runner.py`
- Test: `tests/test_item_database.py`

- [ ] **Step 1: Add explicit enum-name maps**

Add dictionaries for:
- `ITEM_TYPE_NAMES`
- `ARMOR_TYPE_NAMES`
- `WEAPON_TYPE_NAMES`
- `HAND_TYPE_NAMES`
- `RANGED_WEAPON_TYPE_NAMES`
- `CLASS_NAMES`
- `PROFESSION_NAMES`
- `DIFFICULTY_NAMES`
- `REP_FACTION_NAMES`
- `REP_LEVEL_NAMES`
- `FACTION_NAMES`
- `QUALITY_NAMES`

The values must come from `mop/proto/common.proto`, `mop/proto/ui.proto`, and `mop/ui/core/proto_utils/names.ts`.

- [ ] **Step 2: Normalize canonical DB fields**

Update `item_meta_from_dict` so numeric canonical DB fields are converted to current string names:
- `type`
- `armorType`
- `weaponType`
- `handType`
- `rangedWeaponType`
- `quality`
- `classAllowlist`
- `requiredProfession`

Use base scaling option `0` or highest scaling option as the source of `ilvl` if top-level `ilvl` is absent.

- [ ] **Step 3: Resolve source names**

Update `format_sources` so it supports canonical JSON oneof object keys:
- `drop`
- `crafted`
- `quest`
- `soldBy`
- `sold_by`
- `rep`

For drops, prefer `otherName`, then resolved NPC name, then NPC id. Resolve `zoneId` via local zones. Format difficulty names with `DIFFICULTY_NAMES`.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_item_database -v`

Expected: PASS.

### Task 4: Usability And Phase Tests

**Files:**
- Modify: `tests/test_item_database.py`
- Modify: `wowsims_mop_runner.py`

- [ ] **Step 1: Write failing tests**

Add tests:

```python
def test_class_restricted_item_is_not_usable_by_wrong_class(self):
    meta = runner.ItemMeta(id=65179, name="Magma Plated Battleplate", type="ItemTypeChest", armor_type="ArmorTypePlate", class_allowlist=["ClassDeathKnight"])

    ok, reason = runner.is_item_usable(meta, "ClassWarrior", set())

    self.assertFalse(ok)
    self.assertIn("class restricted", reason)


def test_phase_filter_skips_items_above_requested_phase(self):
    meta = runner.ItemMeta(id=1, name="future", type="ItemTypeFinger", phase=5)

    ok, reason = runner.is_item_in_phase(meta, 4)

    self.assertFalse(ok)
    self.assertEqual(reason, "phase 5 is above requested phase 4")
```

- [ ] **Step 2: Run tests to verify fail**

Run: `python -m unittest tests.test_item_database -v`

Expected: phase helper test fails because the helper does not exist.

- [ ] **Step 3: Implement phase helper and use it in candidate building**

Add:

```python
def is_item_in_phase(meta: ItemMeta | None, phase: int | None) -> tuple[bool, str]:
    if meta is None or phase is None or phase <= 0 or meta.phase is None:
        return True, ""
    if meta.phase > phase:
        return False, f"phase {meta.phase} is above requested phase {phase}"
    return True, ""
```

Update `build_candidate_specs` to accept `phase: int | None`, apply the helper, and record skipped item reasons.

- [ ] **Step 4: Add `--phase` argument**

Add parser option:

```python
parser.add_argument("--phase", type=int, default=None, help="Maximum MoP content phase for DB candidates. Defaults to template/settings phase when available.")
```

Use an explicit helper to derive the effective phase from CLI args or `request["sim_options"]["phase"]` / `request["settings"]["phase"]` when present.

- [ ] **Step 5: Run tests**

Run: `python -m unittest tests.test_item_database -v`

Expected: PASS.

### Task 5: Report Skipped Items

**Files:**
- Modify: `wowsims_mop_runner.py`
- Modify: `tests/test_item_database.py`

- [ ] **Step 1: Write failing test for returned skip reasons**

```python
def test_candidate_builder_returns_skipped_reasons(self):
    request = {
        "raid": {"parties": [{"players": [{"class": "ClassWarrior", "equipment": {"items": []}}]}], "num_active_parties": 1}
    }
    db = {
        1: runner.ItemMeta(id=1, name="future", type="ItemTypeFinger", phase=5),
    }

    candidates, skipped = runner.build_candidate_specs(
        request, "none", None, db, source_mode="db", max_db_candidates=0, min_ilvl=None, max_ilvl=None, phase=4
    )

    self.assertEqual(candidates, [])
    self.assertEqual(skipped[0].item_id, 1)
    self.assertEqual(skipped[0].reason, "phase 5 is above requested phase 4")
```

- [ ] **Step 2: Implement `SkippedItem` dataclass**

```python
@dataclasses.dataclass
class SkippedItem:
    item_id: int
    item_name: str
    reason: str
```

Update `build_candidate_specs` to return `tuple[list[dict[str, Any]], list[SkippedItem]]`, then update batch/upgrade callers.

- [ ] **Step 3: Include skip reasons in reports**

Write skip reasons into:
- `upgrade_results.csv` when relevant as rows with empty DPS and `error = "skipped: <reason>"`, or
- `upgrade_report.md` under `## Skipped items`.

Use the markdown section to avoid polluting sim result CSV semantics.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_item_database -v`

Expected: PASS.

### Task 6: Documentation And Honest Limitations

**Files:**
- Modify: `wowsims_mop_runner_README.md`
- Modify: `CODEX_GOAL_wowsims_mop_runner.md`

- [ ] **Step 1: Update README**

Document:
- Current upstream revisions tested.
- `--phase`.
- Canonical DB source lookup.
- `--require-optimizer` fail-closed behavior.
- Optimizer parity remains unavailable without a UI/worker or upstream CLI adapter.

- [ ] **Step 2: Update goal checklist only for completed items**

Mark only completed checklist items:
- Metadata scanner replaced with canonical generated DB loader.
- `UIItemSource` local source resolution improved.
- `--phase` option added.
- Skipped item reasons surfaced.
- Tests added for source formatting and usability/phase filtering.

Leave WSE full importer parity and true optimizer parity unchecked.

### Verification

- [ ] Run: `python -m unittest -v`
- [ ] Run: `python -m py_compile wowsims_mop_runner.py tests/test_item_database.py`
- [ ] Run a smoke metadata load: `python -c "from pathlib import Path; import wowsims_mop_runner as r; print(len(r.load_item_index(Path('mop'), Path('.wowsims_mop_runner/cache'), refresh=True)))"`
- [ ] Run: `git status --short`

### Commit

- [ ] Stage only tracked implementation, tests, docs, and plan files.
- [ ] Commit with a message that states optimizer fidelity is still explicitly unavailable and `--require-optimizer` remains fail-closed.

