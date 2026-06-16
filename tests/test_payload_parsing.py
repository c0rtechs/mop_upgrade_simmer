import unittest
from pathlib import Path
import tempfile

import wowsims_mop_runner as runner


class PayloadParsingTests(unittest.TestCase):
    def test_wse_character_builds_minimal_request_with_mapped_player_fields(self):
        character = {
            "name": "Example",
            "class": "monk",
            "spec": "brewmaster",
            "race": "orc",
            "talents": "123123",
            "professions": [{"name": "Engineering"}, {"name": "Blacksmithing"}],
            "glyphs": {"major": [{"spellID": 123}], "minor": [{"spellID": 456}]},
            "gear": {"items": [{"id": 1}, {"id": 2, "gems": [3, None]}]},
        }

        request = runner.minimal_request_from_wse_character(character, 1000)
        player = runner.get_request_player(request)

        self.assertEqual(player["class"], "ClassMonk")
        self.assertEqual(player["race"], "RaceOrc")
        self.assertIn("brewmaster_monk", player)
        self.assertEqual(player["profession1"], "Engineering")
        self.assertEqual(player["profession2"], "Blacksmithing")
        self.assertEqual(player["glyphs"], {"major1": 123, "minor1": 456})
        self.assertEqual(player["equipment"]["items"][1], {"id": 2, "gems": [3, 0]})
        self.assertEqual(request["sim_options"]["iterations"], 1000)

    def test_equipment_spec_bag_payload_normalizes_item_specs(self):
        bag = {
            "items": [
                {"itemId": "10", "randomSuffix": "2", "upgradeStep": "1"},
                None,
                {"id": 11, "reforge": 123, "gems": ["1", "", None]},
            ]
        }

        normalized = runner.extract_equipment_items_from_payload("equipment_spec", bag)

        self.assertEqual(normalized[0], {"id": 10, "random_suffix": 2, "upgrade_step": 1})
        self.assertEqual(normalized[1], {})
        self.assertEqual(normalized[2], {"id": 11, "gems": [1, 0, 0], "reforging": 123})

    def test_inject_wse_character_preserves_template_settings_and_replaces_gear(self):
        template = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassWarrior",
                                "race": "RaceHuman",
                                "arms_warrior": {},
                                "equipment": {"items": [{"id": 99}]},
                            }
                        ]
                    }
                ],
                "num_active_parties": 1,
            },
            "encounter": {"duration": 300},
            "sim_options": {"iterations": 10},
            "type": "SimTypeIndividual",
        }
        character = {
            "class": "monk",
            "race": "orc",
            "professions": [{"name": "Engineering"}],
            "gear": {"items": [{"id": 1}]},
        }

        request = runner.inject_wse_character_into_request(template, character, iterations=250)
        player = runner.get_request_player(request)

        self.assertEqual(request["encounter"], {"duration": 300})
        self.assertEqual(request["sim_options"]["iterations"], 250)
        self.assertEqual(player["class"], "ClassMonk")
        self.assertEqual(player["race"], "RaceOrc")
        self.assertEqual(player["profession1"], "Engineering")
        self.assertEqual(player["equipment"], {"items": [{"id": 1}]})

    def test_individual_settings_convert_to_raid_request(self):
        settings = {
            "player": {
                "name": "Example",
                "class": "ClassMonk",
                "race": "RaceOrc",
                "brewmaster_monk": {},
                "equipment": {"items": [{"id": 1}]},
            },
            "settings": {"iterations": 500},
            "encounter": {"duration": 180},
            "raidBuffs": {"arcaneBrilliance": True},
            "partyBuffs": {"leaderOfThePack": True},
            "debuffs": {"weakenedArmor": True},
            "targetDummies": 1,
        }

        request = runner.convert_individual_settings_to_raid_request(settings)

        self.assertEqual(request["type"], "SimTypeIndividual")
        self.assertEqual(request["sim_options"]["iterations"], 500)
        self.assertEqual(request["encounter"], {"duration": 180})
        self.assertEqual(request["raid"]["parties"][0]["players"][0]["name"], "Example")
        self.assertEqual(request["raid"]["buffs"], {"arcaneBrilliance": True})
        self.assertEqual(request["raid"]["parties"][0]["buffs"], {"leaderOfThePack": True})
        self.assertEqual(request["raid"]["debuffs"], {"weakenedArmor": True})
        self.assertEqual(request["raid"]["target_dummies"], 1)

    def test_extract_dps_handles_raid_metrics_and_recursive_fallback(self):
        self.assertEqual(runner.extract_dps({"raidMetrics": {"dps": {"avg": 123.5}}}), 123.5)
        self.assertEqual(runner.extract_dps({"raid_metrics": {"parties": [{"dps": {"avg": 234.5}}]}}), 234.5)
        self.assertEqual(runner.extract_dps({"nested": {"unit": {"dps": {"avg": 345.5}}}}), 345.5)

    def test_request_hash_is_stable_for_json_key_order(self):
        left = {"request_id": "first", "raid": {"b": 2, "a": [{"z": 1, "y": 2}]}}
        right = {"request_id": "second", "raid": {"a": [{"y": 2, "z": 1}], "b": 2}}

        self.assertEqual(runner.request_hash(left), runner.request_hash(right))

    def test_sim_cache_paths_are_hash_based(self):
        request = {"raid": {"a": 1}}
        paths = runner.sim_cache_paths(Path("runs"), "Label With Spaces", request)

        self.assertEqual(paths.digest, runner.request_hash(request))
        self.assertEqual(paths.request_path, Path("runs") / "_requests" / f"{paths.digest}.request.json")
        self.assertEqual(paths.result_path, Path("runs") / "_results" / f"{paths.digest}.result.json")

    def test_item_spec_mod_summary_reports_selected_mods(self):
        spec = {"id": 1, "gems": [10, 0, 11], "enchant": 22, "reforging": 33, "upgrade_step": 2}

        self.assertEqual(
            runner.item_spec_mod_summary(spec),
            "gems=10/11; enchant=22; reforge=33; upgrade_step=2",
        )

    def test_sim_result_from_json_extracts_iterations_and_dps_stdev(self):
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.json"
            request_path = Path(temp) / "request.json"
            result_path.write_text(
                """
                {
                  "raidMetrics": {"dps": {"avg": 123.5, "stdev": 4.25}},
                  "iterationsDone": 1000
                }
                """,
                encoding="utf-8",
            )

            result = runner.sim_result_from_json("baseline", request_path, result_path, "abc", seconds=1.5)

        self.assertEqual(result.dps, 123.5)
        self.assertEqual(result.dps_stdev, 4.25)
        self.assertAlmostEqual(result.dps_ci95, 0.2634, places=4)
        self.assertEqual(result.iterations_done, 1000)
        self.assertEqual(result.request_hash, "abc")


if __name__ == "__main__":
    unittest.main()
