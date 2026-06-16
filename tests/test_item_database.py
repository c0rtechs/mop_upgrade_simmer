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


def request_for_player(class_enum: str, race: str = "RaceHuman", spec_field: str | None = None, items: list[dict] | None = None) -> dict:
    player = {
        "class": class_enum,
        "race": race,
        "equipment": {"items": items or []},
    }
    if spec_field:
        player[spec_field] = {}
    return {
        "raid": {
            "parties": [{"players": [player]}],
            "num_active_parties": 1,
        }
    }


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

    def test_wowhead_source_parser_reads_plain_looted_from_prose(self):
        html = "<html><body><p>It is looted from Burilgi Despoiler.</p></body></html>"

        self.assertEqual(runner.extract_wowhead_source(html), "Dropped by: Burilgi Despoiler")

    def test_wowhead_source_parser_includes_listview_difficulty_and_zone(self):
        html = """
        <script>
        new Listview({
          id: 'dropped-by',
          data: [
            { id: 123, name_enus: 'Sha of Fear', location: [6622], modes: { 5: 15.2 } }
          ]
        });
        </script>
        """

        self.assertEqual(
            runner.extract_wowhead_source(html),
            "Dropped by: Sha of Fear (25-player Raid)",
        )

    def test_wowhead_source_parser_reads_vendor_crafting_quest_reputation_and_container(self):
        cases = [
            ("<p>It is sold by Commander Oxheart.</p>", "Sold by: Commander Oxheart"),
            ("<p>It is crafted by Blacksmithing.</p>", "Created by: Blacksmithing"),
            ("<p>It is a quest reward from The Final Power.</p>", "Reward from: The Final Power"),
            ("<p>Requires <a href=\"/faction=1270\">Shado-Pan</a> - Exalted</p>", "Reputation: Shado-Pan at Exalted"),
            ("<p>It is contained in Cache of Sha-Touched Gold.</p>", "Contained in: Cache of Sha-Touched Gold"),
        ]

        for html, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(runner.extract_wowhead_source(html), expected)

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
        request = request_for_player("ClassWarrior")
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

    def test_hunter_melee_weapon_is_rejected_by_official_class_rules(self):
        request = request_for_player("ClassHunter", spec_field="marksmanship_hunter")
        db = {
            1: runner.ItemMeta(
                id=1,
                name="Axe for Someone Else",
                type="ItemTypeWeapon",
                weapon_type="WeaponTypeAxe",
                hand_type="HandTypeOneHand",
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
        )

        self.assertEqual(candidates, [])
        self.assertIn("weapon type WeaponTypeAxe not usable by ClassHunter", skipped[0].reason)

    def test_hunter_ranged_weapon_replaces_main_hand(self):
        request = request_for_player("ClassHunter", spec_field="marksmanship_hunter")
        db = {
            1: runner.ItemMeta(
                id=1,
                name="Longbow",
                type="ItemTypeRanged",
                ranged_weapon_type="RangedWeaponTypeBow",
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
        )
        replacements = runner.replacement_requests_for_item(request, candidates[0], db)

        self.assertEqual(skipped, [])
        self.assertEqual([slot for _label, _req, slot, _slot_name in replacements], [14])

    def test_one_hand_weapon_does_not_generate_offhand_replacement_for_non_dual_wielder(self):
        request = request_for_player("ClassMage", spec_field="frost_mage")
        db = {
            1: runner.ItemMeta(
                id=1,
                name="Spell Sword",
                type="ItemTypeWeapon",
                weapon_type="WeaponTypeSword",
                hand_type="HandTypeOneHand",
            )
        }

        candidates, _skipped = runner.build_candidate_specs(
            request,
            "none",
            None,
            db,
            source_mode="db",
            max_db_candidates=0,
            min_ilvl=None,
            max_ilvl=None,
        )
        replacements = runner.replacement_requests_for_item(request, candidates[0], db)

        self.assertEqual([slot for _label, _req, slot, _slot_name in replacements], [14])

    def test_faction_restricted_item_is_rejected_when_player_race_proves_wrong_faction(self):
        request = request_for_player("ClassWarrior", race="RaceOrc", spec_field="arms_warrior")
        db = {
            1: runner.ItemMeta(
                id=1,
                name="Alliance Ring",
                type="ItemTypeFinger",
                faction_restriction="Alliance",
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
        )

        self.assertEqual(candidates, [])
        self.assertIn("restricted to Alliance", skipped[0].reason)

    def test_limit_category_conflict_only_allows_replacing_conflicting_slot(self):
        request = request_for_player(
            "ClassWarrior",
            spec_field="arms_warrior",
            items=[{} for _ in range(13)],
        )
        request["raid"]["parties"][0]["players"][0]["equipment"]["items"][12] = {"id": 10}
        db = {
            10: runner.ItemMeta(
                id=10,
                name="Equipped Trinket",
                type="ItemTypeTrinket",
                limit_category=99,
            ),
            11: runner.ItemMeta(
                id=11,
                name="Candidate Trinket",
                type="ItemTypeTrinket",
                limit_category=99,
            ),
        }

        candidates, _skipped = runner.build_candidate_specs(
            request,
            "none",
            None,
            db,
            source_mode="db",
            max_db_candidates=0,
            min_ilvl=None,
            max_ilvl=None,
        )
        replacements = runner.replacement_requests_for_item(request, candidates[0], db)

        self.assertEqual([slot for _label, _req, slot, _slot_name in replacements], [12])

    def test_frontend_ep_upgrades_are_auto_included_from_db_when_enabled(self):
        request = request_for_player(
            "ClassWarrior",
            spec_field="arms_warrior",
            items=[{} for _ in range(12)],
        )
        request["raid"]["parties"][0]["players"][0]["equipment"]["items"][10] = {"id": 10}
        request["raid"]["parties"][0]["players"][0]["equipment"]["items"][11] = {"id": 13}
        db = {
            10: runner.ItemMeta(
                id=10,
                name="Current Ring",
                type="ItemTypeFinger",
                raw={
                    "id": 10,
                    "name": "Current Ring",
                    "type": 11,
                    "scalingOptions": {"0": {"stats": {"0": 10}, "ilvl": 100}},
                },
            ),
            13: runner.ItemMeta(
                id=13,
                name="Other Current Ring",
                type="ItemTypeFinger",
                raw={
                    "id": 13,
                    "name": "Other Current Ring",
                    "type": 11,
                    "scalingOptions": {"0": {"stats": {"0": 10}, "ilvl": 100}},
                },
            ),
            11: runner.ItemMeta(
                id=11,
                name="EP Upgrade Ring",
                type="ItemTypeFinger",
                raw={
                    "id": 11,
                    "name": "EP Upgrade Ring",
                    "type": 11,
                    "scalingOptions": {"0": {"stats": {"0": 12}, "ilvl": 101}},
                },
            ),
            12: runner.ItemMeta(
                id=12,
                name="EP Downgrade Ring",
                type="ItemTypeFinger",
                raw={
                    "id": 12,
                    "name": "EP Downgrade Ring",
                    "type": 11,
                    "scalingOptions": {"0": {"stats": {"0": 9}, "ilvl": 102}},
                },
            ),
        }

        candidates, skipped = runner.build_candidate_specs(
            request,
            "none",
            None,
            db,
            source_mode="bag",
            max_db_candidates=0,
            min_ilvl=None,
            max_ilvl=None,
            auto_frontend_ep_upgrades=True,
            frontend_ep_weights={"stats": [1.0]},
        )

        self.assertEqual(skipped, [])
        self.assertEqual(candidates, [{"id": 11}])

    def test_frontend_ep_upgrades_are_not_auto_included_when_disabled(self):
        request = request_for_player(
            "ClassWarrior",
            spec_field="arms_warrior",
            items=[{} for _ in range(12)],
        )
        request["raid"]["parties"][0]["players"][0]["equipment"]["items"][10] = {"id": 10}
        db = {
            10: runner.ItemMeta(
                id=10,
                name="Current Ring",
                type="ItemTypeFinger",
                raw={"scalingOptions": {"0": {"stats": {"0": 10}, "ilvl": 100}}},
            ),
            11: runner.ItemMeta(
                id=11,
                name="EP Upgrade Ring",
                type="ItemTypeFinger",
                raw={"scalingOptions": {"0": {"stats": {"0": 12}, "ilvl": 101}}},
            ),
        }

        candidates, skipped = runner.build_candidate_specs(
            request,
            "none",
            None,
            db,
            source_mode="bag",
            max_db_candidates=0,
            min_ilvl=None,
            max_ilvl=None,
            auto_frontend_ep_upgrades=False,
            frontend_ep_weights={"stats": [1.0]},
        )

        self.assertEqual(skipped, [])
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
