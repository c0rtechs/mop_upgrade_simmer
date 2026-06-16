import json
from pathlib import Path
import tempfile
import unittest

import wowsims_mop_runner as runner


def write_canonical_db(root: Path) -> Path:
    db_dir = root / "assets" / "database"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "db.json"
    data = {
        "zones": [{"id": 5334, "name": "The Bastion of Twilight"}],
        "npcs": [{"id": 168, "name": "Sinestra", "zoneId": 5334}],
        "items": [
            {
                "id": 60226,
                "name": "Dargonax's Signet",
                "type": 11,
                "phase": 1,
                "quality": 4,
                "unique": True,
                "sources": [
                    {
                        "drop": {
                            "difficulty": 6,
                            "npcId": 168,
                            "zoneId": 5334,
                            "otherName": "Sinestra",
                        }
                    }
                ],
                "scalingOptions": {
                    "0": {
                        "stats": {"0": 229, "2": 344, "6": 113, "11": 153},
                        "ilvl": 379,
                    }
                },
            },
            {
                "id": 65179,
                "name": "Magma Plated Battleplate",
                "type": 5,
                "armorType": 4,
                "phase": 1,
                "quality": 4,
                "classAllowlist": [6],
                "scalingOptions": {"0": {"ilvl": 372}},
            },
        ],
    }
    db_path.write_text(json.dumps(data), encoding="utf-8")
    return db_path


class CanonicalItemDatabaseTests(unittest.TestCase):
    def test_loads_canonical_db_json_with_resolved_drop_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_canonical_db(root)

            index = runner.load_item_index(root, root / "cache", refresh=True)

        item = index[60226]
        self.assertEqual(item.name, "Dargonax's Signet")
        self.assertEqual(item.type, "ItemTypeFinger")
        self.assertEqual(item.ilvl, 379)
        self.assertEqual(item.phase, 1)
        self.assertIn(
            "Drop: Sinestra in The Bastion of Twilight",
            runner.format_sources(item.sources),
        )

    def test_formats_rep_source_with_faction_and_level_names(self):
        text = runner.format_sources(
            [
                {
                    "rep": {
                        "repFactionId": 1270,
                        "repLevel": 8,
                        "factionId": 2,
                    }
                }
            ]
        )

        self.assertEqual(text, "Reputation: Shado-Pan at Exalted (Horde)")

    def test_class_restricted_item_is_not_usable_by_wrong_class(self):
        meta = runner.ItemMeta(
            id=65179,
            name="Magma Plated Battleplate",
            type="ItemTypeChest",
            armor_type="ArmorTypePlate",
            class_allowlist=["ClassDeathKnight"],
        )

        ok, reason = runner.is_item_usable(meta, "ClassWarrior", set())

        self.assertFalse(ok)
        self.assertIn("class restricted", reason)

    def test_phase_filter_skips_items_above_requested_phase(self):
        meta = runner.ItemMeta(id=1, name="future", type="ItemTypeFinger", phase=5)

        ok, reason = runner.is_item_in_phase(meta, 4)

        self.assertFalse(ok)
        self.assertEqual(reason, "phase 5 is above requested phase 4")

    def test_candidate_builder_returns_skipped_reasons(self):
        request = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassWarrior",
                                "equipment": {"items": []},
                            }
                        ]
                    }
                ],
                "num_active_parties": 1,
            }
        }
        db = {
            1: runner.ItemMeta(
                id=1,
                name="future",
                type="ItemTypeFinger",
                phase=5,
            )
        }

        candidates, skipped = runner.build_candidate_specs(
            request,
            "none",
            None,
            db,
            source_mode="db",
            max_db_candidates=0,
            min_ilvl=None,
            max_ilvl=None,
            phase=4,
        )

        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0].item_id, 1)
        self.assertEqual(skipped[0].item_name, "future")
        self.assertEqual(skipped[0].reason, "phase 5 is above requested phase 4")


if __name__ == "__main__":
    unittest.main()
