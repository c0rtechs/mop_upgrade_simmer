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

        request = runner.minimal_request_from_wse_character(character, 1000, glyph_spell_to_item={123: 456123, 456: 456456})
        player = runner.get_request_player(request)

        self.assertEqual(player["class"], "ClassMonk")
        self.assertEqual(player["race"], "RaceOrc")
        self.assertIn("brewmaster_monk", player)
        self.assertEqual(player["profession1"], "Engineering")
        self.assertEqual(player["profession2"], "Blacksmithing")
        self.assertEqual(player["glyphs"], {"major1": 456123, "minor1": 456456})
        self.assertEqual(player["equipment"]["items"][1], {"id": 2, "gems": [3, 0]})
        self.assertEqual(request["sim_options"]["iterations"], 1000)

    def test_wse_spec_mapping_accepts_mop_exporter_names(self):
        cases = {
            ("shaman", "elemental"): "elemental_shaman",
            ("shaman", "enhancement"): "enhancement_shaman",
            ("shaman", "restoration"): "restoration_shaman",
            ("hunter", "beast_mastery"): "beast_mastery_hunter",
            ("hunter", "marksman"): "marksmanship_hunter",
            ("hunter", "survival"): "survival_hunter",
            ("druid", "balance"): "balance_druid",
            ("druid", "feral"): "feral_druid",
            ("druid", "guardian"): "guardian_druid",
            ("druid", "restoration"): "restoration_druid",
            ("warlock", "affliction"): "affliction_warlock",
            ("warlock", "demonology"): "demonology_warlock",
            ("warlock", "destruction"): "destruction_warlock",
            ("rogue", "assassination"): "assassination_rogue",
            ("rogue", "combat"): "combat_rogue",
            ("rogue", "subtlety"): "subtlety_rogue",
            ("mage", "arcane"): "arcane_mage",
            ("mage", "fire"): "fire_mage",
            ("mage", "frost"): "frost_mage",
            ("warrior", "arms"): "arms_warrior",
            ("warrior", "fury"): "fury_warrior",
            ("warrior", "protection"): "protection_warrior",
            ("paladin", "holy"): "holy_paladin",
            ("paladin", "protection"): "protection_paladin",
            ("paladin", "retribution"): "retribution_paladin",
            ("priest", "disc"): "discipline_priest",
            ("priest", "holy"): "holy_priest",
            ("priest", "shadow"): "shadow_priest",
            ("deathknight", "blood"): "blood_death_knight",
            ("deathknight", "frost"): "frost_death_knight",
            ("deathknight", "unholy"): "unholy_death_knight",
            ("monk", "brewmaster"): "brewmaster_monk",
            ("monk", "mistweaver"): "mistweaver_monk",
            ("monk", "windwalker"): "windwalker_monk",
        }

        for (class_name, spec_name), expected in cases.items():
            with self.subTest(class_name=class_name, spec_name=spec_name):
                self.assertEqual(runner.spec_field_from_wse(class_name, spec_name), expected)

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

    def test_wse_character_gear_drops_null_items_like_official_addon_importer(self):
        character = {
            "class": "monk",
            "spec": "brewmaster",
            "race": "orc",
            "gear": {"version": 1, "items": [{"id": 10}, None, {"id": 11, "gems": [None, 12]}]},
        }

        request = runner.minimal_request_from_wse_character(character, 100)
        player = runner.get_request_player(request)

        self.assertEqual(player["equipment"], {"items": [{"id": 10}, {"id": 11, "gems": [0, 12]}]})

    def test_wse_character_item_extraction_uses_addon_importer_gear_normalization(self):
        character = {
            "class": "monk",
            "spec": "brewmaster",
            "race": "orc",
            "gear": {"items": [{"id": 10}, None, {"id": 11, "gems": [None, 12]}]},
        }

        items = runner.extract_equipment_items_from_payload("wse_character", character)

        self.assertEqual(items, [{"id": 10}, {"id": 11, "gems": [0, 12]}])

    def test_wse_legacy_glyph_names_fail_closed_without_ui_name_mapping(self):
        character = {
            "class": "monk",
            "spec": "brewmaster",
            "race": "orc",
            "gear": {"items": [{"id": 10}]},
            "glyphs": {"major": ["Glyph of Guard"], "minor": []},
        }

        with self.assertRaises(runner.RunnerError) as ctx:
            runner.minimal_request_from_wse_character(character, 100)

        self.assertIn("legacy WSE glyph name 'Glyph of Guard'", str(ctx.exception))

    def test_parse_decodelink_stdout_accepts_json_with_cli_prefix(self):
        parsed = runner.parse_decodelink_stdout('decoded ok\n{"raid": {}, "simOptions": {}}\n')

        self.assertEqual(parsed, {"raid": {}, "simOptions": {}})

    def test_load_glyph_spell_to_item_map_from_canonical_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_dir = root / "assets" / "database"
            db_dir.mkdir(parents=True)
            (db_dir / "db.json").write_text(
                '{"items": [], "glyphIds": [{"itemId": 40896, "spellId": 54810}]}',
                encoding="utf-8",
            )

            mapping = runner.load_glyph_spell_to_item_map(root)

        self.assertEqual(mapping, {54810: 40896})

    def test_inject_wse_character_preserves_template_settings_and_replaces_gear(self):
        template = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassMonk",
                                "race": "RacePandaren",
                                "brewmaster_monk": {},
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
            "spec": "brewmaster",
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

    def test_inject_wse_character_rejects_wrong_class_template(self):
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
            "spec": "brewmaster",
            "race": "orc",
            "gear": {"items": [{"id": 1}]},
        }

        with self.assertRaises(runner.RunnerError) as ctx:
            runner.inject_wse_character_into_request(template, character)

        self.assertIn("Template class ClassWarrior does not match WSE class ClassMonk", str(ctx.exception))

    def test_inject_wse_character_rejects_wrong_spec_template(self):
        template = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassMonk",
                                "race": "RacePandaren",
                                "windwalker_monk": {},
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
            "spec": "brewmaster",
            "race": "orc",
            "gear": {"items": [{"id": 1}]},
        }

        with self.assertRaises(runner.RunnerError) as ctx:
            runner.inject_wse_character_into_request(template, character)

        self.assertIn("Template spec SpecWindwalkerMonk does not match WSE spec SpecBrewmasterMonk", str(ctx.exception))

    def test_inject_wse_character_rejects_unknown_class_race_and_profession(self):
        template = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassMonk",
                                "race": "RaceOrc",
                                "brewmaster_monk": {},
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

        cases = [
            ({"class": "tinker", "race": "orc", "gear": {"items": []}}, "Could not parse WSE class 'tinker'"),
            ({"class": "monk", "race": "naga", "gear": {"items": []}}, "Could not parse WSE race 'naga'"),
            (
                {
                    "class": "monk",
                    "race": "orc",
                    "professions": [{"name": "Engineering"}, {"name": "Underwater Basket Weaving"}],
                    "gear": {"items": []},
                },
                "Could not parse WSE profession 'Underwater Basket Weaving'",
            ),
        ]

        for character, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(runner.RunnerError) as ctx:
                    runner.inject_wse_character_into_request(template, character)
                self.assertIn(expected, str(ctx.exception))

    def test_inject_wse_character_does_not_create_duplicate_sim_options_alias(self):
        template = {
            "raid": {
                "parties": [
                    {
                        "players": [
                            {
                                "class": "ClassMonk",
                                "race": "RaceOrc",
                                "brewmaster_monk": {"options": {"class_options": {}}},
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
        character = {"class": "monk", "race": "orc", "gear": {"items": [{"id": 1}]}}

        request = runner.inject_wse_character_into_request(template, character, iterations=250)

        self.assertEqual(request["sim_options"], {"iterations": 250})
        self.assertNotIn("simOptions", request)

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

    def test_individual_settings_conversion_allocates_party_capacity_for_target_dummies(self):
        settings = {
            "player": {
                "name": "Example",
                "class": "ClassMonk",
                "race": "RaceOrc",
                "brewmaster_monk": {"options": {"class_options": {}}},
                "equipment": {"items": [{"id": 1}]},
            },
            "settings": {"iterations": 500},
            "encounter": {"duration": 180},
            "targetDummies": 9,
        }

        request = runner.convert_individual_settings_to_raid_request(settings)

        self.assertEqual(request["raid"]["target_dummies"], 9)
        self.assertEqual(request["raid"]["num_active_parties"], 2)
        self.assertEqual(len(request["raid"]["parties"]), 2)

    def test_input_context_preserves_individual_settings_ep_weights(self):
        settings = {
            "player": {
                "name": "Example",
                "class": "ClassMonk",
                "race": "RaceOrc",
                "brewmaster_monk": {"options": {"class_options": {}}},
                "equipment": {"items": [{"id": 1}]},
            },
            "settings": {"iterations": 500},
            "epWeightsStats": {"stats": [1.0, 2.0], "pseudoStats": [3.0]},
        }

        context = runner.build_input_context_from_payload(
            "individual_settings",
            settings,
            Path("unused-wowsimcli"),
            Path("unused-out"),
            250,
        )

        self.assertEqual(context.request["sim_options"], {"iterations": 250})
        self.assertEqual(context.ep_weights_stats, {"stats": [1.0, 2.0], "pseudoStats": [3.0]})
        self.assertEqual(context.ep_weights_source, "input IndividualSimSettings")

    def test_wse_only_build_uses_official_default_build_before_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec_dir = root / "ui" / "monk" / "brewmaster"
            builds_dir = spec_dir / "builds"
            builds_dir.mkdir(parents=True)
            (spec_dir / "sim.ts").write_text(
                "const SPEC_CONFIG = registerSpecConfig(Spec.SpecBrewmasterMonk, {\n"
                "  defaultBuild: Presets.PRESET_BUILD_HORRIDON,\n"
                "});\n",
                encoding="utf-8",
            )
            (spec_dir / "presets.ts").write_text(
                "import HorridonBuild from './builds/horridon_encounter_only.build.json';\n"
                "export const PRESET_BUILD_HORRIDON = PresetUtils.makePresetBuildFromJSON('Horridon', Spec.SpecBrewmasterMonk, HorridonBuild);\n",
                encoding="utf-8",
            )
            (builds_dir / "horridon_encounter_only.build.json").write_text(
                """
                {
                  "player": {
                    "name": "Preset",
                    "race": "RaceOrc",
                    "class": "ClassMonk",
                    "brewmasterMonk": {"options": {"classOptions": {}}},
                    "talentsString": "111111"
                  },
                  "encounter": {"duration": 93},
                  "targetDummies": 9
                }
                """,
                encoding="utf-8",
            )
            character = {
                "name": "WSE",
                "class": "monk",
                "spec": "brewmaster",
                "race": "orc",
                "talents": "213121",
                "gear": {"items": [{"id": 1}]},
            }

            request = runner.build_request_from_payload(
                "wse_character",
                character,
                Path("unused-wowsimcli"),
                root / "out",
                250,
                mop_dir=root,
            )

        player = runner.get_request_player(request)
        self.assertEqual(request["encounter"], {"duration": 93})
        self.assertEqual(request["sim_options"], {"iterations": 250})
        self.assertEqual(request["raid"]["target_dummies"], 9)
        self.assertEqual(request["raid"]["num_active_parties"], 2)
        self.assertEqual(player["equipment"], {"items": [{"id": 1}]})
        self.assertEqual(player["talents_string"], "213121")
        self.assertEqual(player["brewmasterMonk"], {"options": {"classOptions": {}}})

    def test_wse_only_without_official_default_build_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            character = {
                "name": "WSE",
                "class": "monk",
                "spec": "brewmaster",
                "race": "orc",
                "gear": {"items": [{"id": 1}]},
            }

            with self.assertRaises(runner.RunnerError) as ctx:
                runner.build_request_from_payload(
                    "wse_character",
                    character,
                    Path("unused-wowsimcli"),
                    Path(temp) / "out",
                    250,
                    mop_dir=Path(temp),
                )

        self.assertIn("No official WoWSims default build", str(ctx.exception))

    def test_official_default_build_resolves_build_matching_default_gear(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec_dir = root / "ui" / "death_knight" / "unholy"
            builds_dir = spec_dir / "builds"
            gear_dir = spec_dir / "gear_sets"
            builds_dir.mkdir(parents=True)
            gear_dir.mkdir(parents=True)
            (spec_dir / "sim.ts").write_text(
                "const SPEC_CONFIG = registerSpecConfig(Spec.SpecUnholyDeathKnight, {\n"
                "  defaults: {\n"
                "    gear: Presets.P5_GEAR_PRESET.gear,\n"
                "  },\n"
                "  presets: {\n"
                "    builds: [Presets.PREBIS_PRESET, Presets.P5_PRESET],\n"
                "  },\n"
                "});\n",
                encoding="utf-8",
            )
            (spec_dir / "presets.ts").write_text(
                "import P5Gear from './gear_sets/p5.gear.json';\n"
                "import PrebisBuild from './builds/prebis.build.json';\n"
                "import P5Build from './builds/p5.build.json';\n"
                "export const P5_GEAR_PRESET = PresetUtils.makePresetGear('P5', P5Gear);\n"
                "export const PREBIS_PRESET = PresetUtils.makePresetBuildFromJSON('Prebis', Spec.SpecUnholyDeathKnight, PrebisBuild);\n"
                "export const P5_PRESET = PresetUtils.makePresetBuildFromJSON('P5', Spec.SpecUnholyDeathKnight, P5Build);\n",
                encoding="utf-8",
            )
            (gear_dir / "p5.gear.json").write_text(
                '{"items": [{"id": 20, "gems": [1, 0]}, {"id": 21}]}',
                encoding="utf-8",
            )
            (builds_dir / "prebis.build.json").write_text(
                '{"player": {"equipment": {"items": [{"id": 10}]}}}',
                encoding="utf-8",
            )
            (builds_dir / "p5.build.json").write_text(
                '{"player": {"equipment": {"items": [{"id": 20, "gems": [1, 0]}, {"id": 21}]}}}',
                encoding="utf-8",
            )

            build_path = runner.official_default_build_path(root, "unholy_death_knight")

        self.assertEqual(build_path, builds_dir / "p5.build.json")

    def test_player_spec_enum_accepts_official_camel_case_oneof_key(self):
        player = {"class": "ClassMonk", "brewmasterMonk": {"options": {"classOptions": {}}}}

        self.assertEqual(runner.player_spec_enum(player), "SpecBrewmasterMonk")

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

    def test_ensure_sim_cache_dirs_creates_request_and_result_parents(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = runner.sim_cache_paths(Path(temp) / "runs", "label", {"raid": {"a": 1}})

            runner.ensure_sim_cache_dirs(paths)

            self.assertTrue(paths.request_path.parent.is_dir())
            self.assertTrue(paths.result_path.parent.is_dir())

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
