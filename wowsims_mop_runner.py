#!/usr/bin/env python3
"""
wowsims_mop_runner.py

Local orchestration helper for WoWSims MoP Classic + WowSimsExporter (WSE).

What this script does:
  * Ensures https://github.com/wowsims/mop is cloned/fast-forwarded beside this script.
  * Ensures https://github.com/wowsims/exporter is cloned/fast-forwarded beside this script.
  * Finds or downloads the latest wowsimcli release asset for the local OS, falling back to
    building cmd/wowsimcli when Go is available.
  * Prompts for a WSE addon export / wowsims share link / RaidSimRequest JSON / file path.
  * Runs a normal sim, batch single/all-combination gear sims, or upgrade single-swap sims.
  * Writes machine-readable JSON/CSV plus a markdown report.

Important honesty note:
  The current MoP wowsimcli exposes a low-level `sim` command that accepts a RaidSimRequest
  protojson file. The browser UI owns some richer behavior such as Addon-import translation,
  batch-sim UX, auto-gem/enchant/reforge workflows, and preset/default settings. This script
  has adapters for all of that, but intentionally fails loudly when it cannot prove an upstream
  optimizer/import adapter is available. This avoids producing fake "fully optimized" upgrade
  results.

Recommended usage for accurate results:
  1) Paste your WSE export from `/wse export`.
  2) Provide a known-good WoWSims share link or RaidSimRequest JSON from the UI for the same
     class/spec when prompted. The script injects current gear/talents/glyphs from WSE into that
     request so buffs/APL/encounter are preserved.
  3) For batch/upgrade sims, paste the WSE bag export as well.

Python: 3.10+
Dependencies: stdlib only. External tools used when available: git, Go toolchain.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import csv
import dataclasses
import datetime as _dt
import hashlib
import html
import itertools
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence

MOP_REPO_URL = "https://github.com/wowsims/mop"
EXPORTER_REPO_URL = "https://github.com/wowsims/exporter"
GITHUB_API = "https://api.github.com"
WOWHEAD_MOP_CLASSIC_ITEM_URL = "https://www.wowhead.com/mop-classic/item={item_id}"

SCRIPT_VERSION = "2026.06.16-1"
RAID_PARTY_SIZE = 5
MAX_RAID_PARTIES = 5

# EquipmentSpec item order from WSE / WoWSims proto. WSE still includes a ranged slot
# in the exported equipment layout; MoP proto ItemSlot only enumerates through offhand.
GEAR_INDEX_TO_SLOT = {
    0: "ItemSlotHead",
    1: "ItemSlotNeck",
    2: "ItemSlotShoulder",
    3: "ItemSlotBack",
    4: "ItemSlotChest",
    5: "ItemSlotWrist",
    6: "ItemSlotHands",
    7: "ItemSlotWaist",
    8: "ItemSlotLegs",
    9: "ItemSlotFeet",
    10: "ItemSlotFinger1",
    11: "ItemSlotFinger2",
    12: "ItemSlotTrinket1",
    13: "ItemSlotTrinket2",
    14: "ItemSlotMainHand",
    15: "ItemSlotOffHand",
    16: "ItemSlotRangedUnsupported",
}
SLOT_TO_INDEXES: dict[str, list[int]] = {}
for idx, slot in GEAR_INDEX_TO_SLOT.items():
    SLOT_TO_INDEXES.setdefault(slot, []).append(idx)

ITEM_TYPE_TO_SLOT_INDEXES = {
    "ItemTypeHead": [0],
    "head": [0],
    "ItemTypeNeck": [1],
    "neck": [1],
    "ItemTypeShoulder": [2],
    "shoulder": [2],
    "ItemTypeBack": [3],
    "back": [3],
    "cloak": [3],
    "ItemTypeChest": [4],
    "chest": [4],
    "ItemTypeWrist": [5],
    "wrist": [5],
    "bracer": [5],
    "ItemTypeHands": [6],
    "hands": [6],
    "gloves": [6],
    "ItemTypeWaist": [7],
    "waist": [7],
    "belt": [7],
    "ItemTypeLegs": [8],
    "legs": [8],
    "ItemTypeFeet": [9],
    "feet": [9],
    "boots": [9],
    "ItemTypeFinger": [10, 11],
    "finger": [10, 11],
    "ring": [10, 11],
    "ItemTypeTrinket": [12, 13],
    "trinket": [12, 13],
    "ItemTypeWeapon": [14, 15],
    "weapon": [14, 15],
    # MoP equips bows/crossbows/guns/wands through the weapon flow. The old
    # exported ranged index is legacy WSE shape, not a proto ItemSlot.
    "ItemTypeRanged": [14],
    "ranged": [14],
}

CLASS_ENUM_BY_WSE = {
    "warrior": "ClassWarrior",
    "paladin": "ClassPaladin",
    "hunter": "ClassHunter",
    "rogue": "ClassRogue",
    "priest": "ClassPriest",
    "deathknight": "ClassDeathKnight",
    "death knight": "ClassDeathKnight",
    "shaman": "ClassShaman",
    "mage": "ClassMage",
    "warlock": "ClassWarlock",
    "monk": "ClassMonk",
    "druid": "ClassDruid",
}

RACE_ENUM_BY_WSE = {
    "bloodelf": "RaceBloodElf",
    "blood elf": "RaceBloodElf",
    "draenei": "RaceDraenei",
    "dwarf": "RaceDwarf",
    "gnome": "RaceGnome",
    "human": "RaceHuman",
    "nightelf": "RaceNightElf",
    "night elf": "RaceNightElf",
    "orc": "RaceOrc",
    "tauren": "RaceTauren",
    "troll": "RaceTroll",
    "undead": "RaceUndead",
    "scourge": "RaceUndead",
    "worgen": "RaceWorgen",
    "goblin": "RaceGoblin",
    "pandaren (a)": "RaceAlliancePandaren",
    "pandaren alliance": "RaceAlliancePandaren",
    "alliance pandaren": "RaceAlliancePandaren",
    "pandaren (h)": "RaceHordePandaren",
    "pandaren horde": "RaceHordePandaren",
    "horde pandaren": "RaceHordePandaren",
    "pandaren": "RaceAlliancePandaren",
}

SPEC_ONEOF_FIELD_BY_WSE = {
    "blood": "blood_death_knight",
    "frost": "frost_death_knight",  # ambiguous DK/Mage; template injection is preferred.
    "unholy": "unholy_death_knight",
    "balance": "balance_druid",
    "feral": "feral_druid",
    "guardian": "guardian_druid",
    "restoration": "restoration_druid",  # ambiguous Druid/Shaman; class disambiguated below.
    "beastmastery": "beast_mastery_hunter",
    "beast mastery": "beast_mastery_hunter",
    "marksman": "marksmanship_hunter",
    "marksmanship": "marksmanship_hunter",
    "survival": "survival_hunter",
    "arcane": "arcane_mage",
    "fire": "fire_mage",
    "mistweaver": "mistweaver_monk",
    "brewmaster": "brewmaster_monk",
    "windwalker": "windwalker_monk",
    "holy": "holy_paladin",  # ambiguous Priest/Paladin; class disambiguated below.
    "protection": "protection_paladin",  # ambiguous Warrior/Paladin; class disambiguated below.
    "retribution": "retribution_paladin",
    "disc": "discipline_priest",
    "discipline": "discipline_priest",
    "shadow": "shadow_priest",
    "assassination": "assassination_rogue",
    "combat": "combat_rogue",
    "subtlety": "subtlety_rogue",
    "elemental": "elemental_shaman",
    "enhancement": "enhancement_shaman",
    "affliction": "affliction_warlock",
    "demonology": "demonology_warlock",
    "destruction": "destruction_warlock",
    "arms": "arms_warrior",
    "fury": "fury_warrior",
}

SPEC_FIELD_BY_CLASS_AND_SPEC = {
    ("paladin", "holy"): "holy_paladin",
    ("paladin", "protection"): "protection_paladin",
    ("warrior", "protection"): "protection_warrior",
    ("priest", "holy"): "holy_priest",
    ("druid", "restoration"): "restoration_druid",
    ("shaman", "restoration"): "restoration_shaman",
    ("mage", "frost"): "frost_mage",
    ("deathknight", "frost"): "frost_death_knight",
    ("death knight", "frost"): "frost_death_knight",
}

UI_CLASS_DIR_SUFFIXES = (
    "death_knight",
    "druid",
    "hunter",
    "mage",
    "monk",
    "paladin",
    "priest",
    "rogue",
    "shaman",
    "warlock",
    "warrior",
)

PROFESSION_ENUM_BY_NAME = {
    "alchemy": "Alchemy",
    "blacksmithing": "Blacksmithing",
    "enchanting": "Enchanting",
    "engineering": "Engineering",
    "herbalism": "Herbalism",
    "inscription": "Inscription",
    "jewelcrafting": "Jewelcrafting",
    "leatherworking": "Leatherworking",
    "mining": "Mining",
    "skinning": "Skinning",
    "tailoring": "Tailoring",
    "archeology": "Archeology",
    "archaeology": "Archeology",
}

ITEM_TYPE_NAMES = {
    0: "ItemTypeUnknown",
    1: "ItemTypeHead",
    2: "ItemTypeNeck",
    3: "ItemTypeShoulder",
    4: "ItemTypeBack",
    5: "ItemTypeChest",
    6: "ItemTypeWrist",
    7: "ItemTypeHands",
    8: "ItemTypeWaist",
    9: "ItemTypeLegs",
    10: "ItemTypeFeet",
    11: "ItemTypeFinger",
    12: "ItemTypeTrinket",
    13: "ItemTypeWeapon",
    14: "ItemTypeRanged",
}

ARMOR_TYPE_NAMES = {
    0: "ArmorTypeUnknown",
    1: "ArmorTypeCloth",
    2: "ArmorTypeLeather",
    3: "ArmorTypeMail",
    4: "ArmorTypePlate",
}

WEAPON_TYPE_NAMES = {
    0: "WeaponTypeUnknown",
    1: "WeaponTypeAxe",
    2: "WeaponTypeDagger",
    3: "WeaponTypeFist",
    4: "WeaponTypeMace",
    5: "WeaponTypeOffHand",
    6: "WeaponTypePolearm",
    7: "WeaponTypeShield",
    8: "WeaponTypeStaff",
    9: "WeaponTypeSword",
}

HAND_TYPE_NAMES = {
    0: "HandTypeUnknown",
    1: "HandTypeMainHand",
    2: "HandTypeOneHand",
    3: "HandTypeOffHand",
    4: "HandTypeTwoHand",
}

RANGED_WEAPON_TYPE_NAMES = {
    0: "RangedWeaponTypeUnknown",
    1: "RangedWeaponTypeBow",
    2: "RangedWeaponTypeCrossbow",
    3: "RangedWeaponTypeGun",
    5: "RangedWeaponTypeThrown",
    6: "RangedWeaponTypeWand",
}

CLASS_NAMES = {
    0: "ClassUnknown",
    1: "ClassWarrior",
    2: "ClassPaladin",
    3: "ClassHunter",
    4: "ClassRogue",
    5: "ClassPriest",
    6: "ClassDeathKnight",
    7: "ClassShaman",
    8: "ClassMage",
    9: "ClassWarlock",
    10: "ClassMonk",
    11: "ClassDruid",
}

PROFESSION_NAMES = {
    0: "ProfessionUnknown",
    1: "Alchemy",
    2: "Blacksmithing",
    3: "Enchanting",
    4: "Engineering",
    5: "Herbalism",
    6: "Inscription",
    7: "Jewelcrafting",
    8: "Leatherworking",
    9: "Mining",
    10: "Skinning",
    11: "Tailoring",
    12: "Archeology",
}

QUALITY_NAMES = {
    0: "ItemQualityJunk",
    1: "ItemQualityCommon",
    2: "ItemQualityUncommon",
    3: "ItemQualityRare",
    4: "ItemQualityEpic",
    5: "ItemQualityLegendary",
    6: "ItemQualityArtifact",
    7: "ItemQualityHeirloom",
}

PSEUDO_STAT_MAIN_HAND_DPS = 0
PSEUDO_STAT_OFF_HAND_DPS = 1
PSEUDO_STAT_RANGED_DPS = 2

GEM_COLOR_META = 1
GEM_COLOR_RED = 2
GEM_COLOR_BLUE = 3
GEM_COLOR_YELLOW = 4
GEM_COLOR_GREEN = 5
GEM_COLOR_ORANGE = 6
GEM_COLOR_PURPLE = 7
GEM_COLOR_PRISMATIC = 8
GEM_COLOR_COGWHEEL = 9
GEM_COLOR_SHA_TOUCHED = 10
SPECIAL_GEM_COLORS = {GEM_COLOR_META, GEM_COLOR_COGWHEEL, GEM_COLOR_SHA_TOUCHED}
SOCKET_TO_MATCHING_GEM_COLORS = {
    GEM_COLOR_META: {GEM_COLOR_META},
    GEM_COLOR_BLUE: {GEM_COLOR_BLUE, GEM_COLOR_PURPLE, GEM_COLOR_GREEN, GEM_COLOR_PRISMATIC},
    GEM_COLOR_RED: {GEM_COLOR_RED, GEM_COLOR_PURPLE, GEM_COLOR_ORANGE, GEM_COLOR_PRISMATIC},
    GEM_COLOR_YELLOW: {GEM_COLOR_YELLOW, GEM_COLOR_ORANGE, GEM_COLOR_GREEN, GEM_COLOR_PRISMATIC},
    GEM_COLOR_PRISMATIC: {
        GEM_COLOR_RED,
        GEM_COLOR_ORANGE,
        GEM_COLOR_YELLOW,
        GEM_COLOR_GREEN,
        GEM_COLOR_BLUE,
        GEM_COLOR_PURPLE,
        GEM_COLOR_PRISMATIC,
    },
    GEM_COLOR_COGWHEEL: {GEM_COLOR_COGWHEEL},
    GEM_COLOR_SHA_TOUCHED: {GEM_COLOR_SHA_TOUCHED},
}

DIFFICULTY_NAMES = {
    0: "Unknown",
    1: "Normal",
    2: "Heroic",
    3: "10-player Raid",
    4: "10-player Heroic Raid",
    5: "25-player Raid",
    6: "25-player Heroic Raid",
    7: "Titan Rune Alpha",
    8: "Titan Rune Beta",
    9: "Raid Finder",
    10: "Celestial",
    11: "Flexible Raid",
    12: "Vendor",
    "DifficultyUnknown": "Unknown",
    "DifficultyNormal": "Normal",
    "DifficultyHeroic": "Heroic",
    "DifficultyTitanRuneAlpha": "Titan Rune Alpha",
    "DifficultyTitanRuneBeta": "Titan Rune Beta",
    "DifficultyCelestial": "Celestial",
    "DifficultyRaid10": "10-player Raid",
    "DifficultyRaid10H": "10-player Heroic Raid",
    "DifficultyRaid25": "25-player Raid",
    "DifficultyRaid25H": "25-player Heroic Raid",
    "DifficultyRaid25RF": "Raid Finder",
    "DifficultyRaidFlex": "Flexible Raid",
    "DifficultyVendor": "Vendor",
}

REP_LEVEL_NAMES = {
    0: "Unknown",
    1: "Hated",
    2: "Hostile",
    3: "Unfriendly",
    4: "Neutral",
    5: "Friendly",
    6: "Honored",
    7: "Revered",
    8: "Exalted",
}

REP_FACTION_NAMES = {
    0: "Unknown",
    1135: "The Earthen Ring",
    1158: "Guardians of Hyjal",
    1171: "Therazane",
    1172: "Dragonmaw Clan",
    1173: "Ramkahen",
    1174: "Wildhammer Clan",
    1177: "Baradin's Wardens",
    1178: "Hellscream's Reach",
    1204: "Avengers of Hyjal",
    1269: "Golden Lotus",
    1270: "Shado-Pan",
    1271: "Order of the Cloud Serpent",
    1272: "The Tillers",
    1302: "The Anglers",
    1337: "The Klaxxi",
    1341: "The August Celestials",
    1345: "The Lorewalkers",
    1351: "The Brewmasters",
    1359: "The Black Prince",
    1375: "Dominance Offensive",
    1376: "Operation: Shieldwall",
    1387: "Kirin Tor Offensive",
    1388: "Sunreaver Onslaught",
    1435: "Shado-Pan Assault",
    1492: "Emperor Shaohao",
}

FACTION_NAMES = {
    0: "Unknown",
    1: "Alliance",
    2: "Horde",
}

CLASS_ARMOR_MAX = {
    "ClassWarrior": "ArmorTypePlate",
    "ClassPaladin": "ArmorTypePlate",
    "ClassDeathKnight": "ArmorTypePlate",
    "ClassHunter": "ArmorTypeMail",
    "ClassShaman": "ArmorTypeMail",
    "ClassRogue": "ArmorTypeLeather",
    "ClassMonk": "ArmorTypeLeather",
    "ClassDruid": "ArmorTypeLeather",
    "ClassPriest": "ArmorTypeCloth",
    "ClassMage": "ArmorTypeCloth",
    "ClassWarlock": "ArmorTypeCloth",
}
ARMOR_ORDER = ["ArmorTypeCloth", "ArmorTypeLeather", "ArmorTypeMail", "ArmorTypePlate"]

# Mirrors mop/ui/core/player_classes/*.ts. The bool is the upstream
# EligibleWeaponType.canUseTwoHand flag.
CLASS_WEAPON_ELIGIBILITY = {
    "ClassWarrior": {
        "WeaponTypeAxe": True,
        "WeaponTypeDagger": False,
        "WeaponTypeFist": False,
        "WeaponTypeMace": True,
        "WeaponTypeOffHand": False,
        "WeaponTypePolearm": True,
        "WeaponTypeShield": False,
        "WeaponTypeStaff": True,
        "WeaponTypeSword": True,
    },
    "ClassPaladin": {
        "WeaponTypeAxe": True,
        "WeaponTypeMace": True,
        "WeaponTypeOffHand": False,
        "WeaponTypePolearm": True,
        "WeaponTypeShield": False,
        "WeaponTypeSword": True,
    },
    "ClassHunter": {},
    "ClassRogue": {
        "WeaponTypeAxe": False,
        "WeaponTypeDagger": False,
        "WeaponTypeFist": False,
        "WeaponTypeMace": False,
        "WeaponTypeOffHand": False,
        "WeaponTypeSword": False,
    },
    "ClassPriest": {
        "WeaponTypeDagger": False,
        "WeaponTypeMace": False,
        "WeaponTypeOffHand": False,
        "WeaponTypeStaff": True,
    },
    "ClassDeathKnight": {
        "WeaponTypeAxe": True,
        "WeaponTypeMace": True,
        "WeaponTypePolearm": True,
        "WeaponTypeSword": True,
    },
    "ClassShaman": {
        "WeaponTypeAxe": True,
        "WeaponTypeDagger": False,
        "WeaponTypeFist": False,
        "WeaponTypeMace": True,
        "WeaponTypeOffHand": False,
        "WeaponTypeShield": False,
        "WeaponTypeStaff": True,
    },
    "ClassMage": {
        "WeaponTypeDagger": False,
        "WeaponTypeOffHand": False,
        "WeaponTypeStaff": True,
        "WeaponTypeSword": False,
    },
    "ClassWarlock": {
        "WeaponTypeDagger": False,
        "WeaponTypeOffHand": False,
        "WeaponTypeStaff": True,
        "WeaponTypeSword": False,
    },
    "ClassMonk": {
        "WeaponTypeAxe": False,
        "WeaponTypeFist": False,
        "WeaponTypeMace": False,
        "WeaponTypeOffHand": False,
        "WeaponTypePolearm": True,
        "WeaponTypeStaff": True,
        "WeaponTypeSword": False,
    },
    "ClassDruid": {
        "WeaponTypeDagger": False,
        "WeaponTypeFist": False,
        "WeaponTypeMace": True,
        "WeaponTypeOffHand": False,
        "WeaponTypePolearm": True,
        "WeaponTypeStaff": True,
    },
}
CLASS_RANGED_WEAPONS = {
    "ClassWarrior": {"RangedWeaponTypeBow", "RangedWeaponTypeCrossbow", "RangedWeaponTypeGun", "RangedWeaponTypeThrown"},
    "ClassPaladin": set(),
    "ClassHunter": {"RangedWeaponTypeBow", "RangedWeaponTypeCrossbow", "RangedWeaponTypeGun"},
    "ClassRogue": set(),
    "ClassPriest": {"RangedWeaponTypeWand"},
    "ClassDeathKnight": set(),
    "ClassShaman": set(),
    "ClassMage": {"RangedWeaponTypeWand"},
    "ClassWarlock": {"RangedWeaponTypeWand"},
    "ClassMonk": set(),
    "ClassDruid": set(),
}

SPEC_ENUM_BY_PLAYER_FIELD = {
    "blood_death_knight": "SpecBloodDeathKnight",
    "frost_death_knight": "SpecFrostDeathKnight",
    "unholy_death_knight": "SpecUnholyDeathKnight",
    "balance_druid": "SpecBalanceDruid",
    "feral_druid": "SpecFeralDruid",
    "guardian_druid": "SpecGuardianDruid",
    "restoration_druid": "SpecRestorationDruid",
    "beast_mastery_hunter": "SpecBeastMasteryHunter",
    "marksmanship_hunter": "SpecMarksmanshipHunter",
    "survival_hunter": "SpecSurvivalHunter",
    "arcane_mage": "SpecArcaneMage",
    "fire_mage": "SpecFireMage",
    "frost_mage": "SpecFrostMage",
    "brewmaster_monk": "SpecBrewmasterMonk",
    "mistweaver_monk": "SpecMistweaverMonk",
    "windwalker_monk": "SpecWindwalkerMonk",
    "holy_paladin": "SpecHolyPaladin",
    "protection_paladin": "SpecProtectionPaladin",
    "retribution_paladin": "SpecRetributionPaladin",
    "discipline_priest": "SpecDisciplinePriest",
    "holy_priest": "SpecHolyPriest",
    "shadow_priest": "SpecShadowPriest",
    "assassination_rogue": "SpecAssassinationRogue",
    "combat_rogue": "SpecCombatRogue",
    "subtlety_rogue": "SpecSubtletyRogue",
    "elemental_shaman": "SpecElementalShaman",
    "enhancement_shaman": "SpecEnhancementShaman",
    "restoration_shaman": "SpecRestorationShaman",
    "affliction_warlock": "SpecAfflictionWarlock",
    "demonology_warlock": "SpecDemonologyWarlock",
    "destruction_warlock": "SpecDestructionWarlock",
    "arms_warrior": "SpecArmsWarrior",
    "fury_warrior": "SpecFuryWarrior",
    "protection_warrior": "SpecProtectionWarrior",
}

DUAL_WIELD_SPECS = {
    "SpecArmsWarrior",
    "SpecFuryWarrior",
    "SpecProtectionWarrior",
    "SpecBeastMasteryHunter",
    "SpecMarksmanshipHunter",
    "SpecSurvivalHunter",
    "SpecBloodDeathKnight",
    "SpecFrostDeathKnight",
    "SpecUnholyDeathKnight",
    "SpecAssassinationRogue",
    "SpecCombatRogue",
    "SpecSubtletyRogue",
    "SpecEnhancementShaman",
    "SpecBrewmasterMonk",
    "SpecWindwalkerMonk",
}

ALLIANCE_RACES = {
    "RaceHuman",
    "RaceDwarf",
    "RaceNightElf",
    "RaceGnome",
    "RaceDraenei",
    "RaceWorgen",
    "RaceAlliancePandaren",
}
HORDE_RACES = {
    "RaceBloodElf",
    "RaceOrc",
    "RaceTauren",
    "RaceTroll",
    "RaceUndead",
    "RaceGoblin",
    "RaceHordePandaren",
}

SOURCE_DIFFICULTY_NAMES = DIFFICULTY_NAMES


class RunnerError(RuntimeError):
    """Expected user-facing failure."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclasses.dataclass
class ItemMeta:
    id: int
    name: str = ""
    type: str = ""
    armor_type: str = ""
    weapon_type: str = ""
    hand_type: str = ""
    ranged_weapon_type: str = ""
    ilvl: int | None = None
    quality: str = ""
    phase: int | None = None
    unique: bool = False
    limit_category: int | None = None
    faction_restriction: str = ""
    class_allowlist: list[str] = dataclasses.field(default_factory=list)
    required_profession: str = ""
    sources: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class SkippedItem:
    item_id: int
    item_name: str
    reason: str


@dataclasses.dataclass(frozen=True)
class PlayerContext:
    class_enum: str
    spec_enum: str
    race_enum: str
    faction: str
    professions: frozenset[str]

    @property
    def can_dual_wield(self) -> bool:
        return self.spec_enum in DUAL_WIELD_SPECS

    @property
    def can_dual_wield_two_hand(self) -> bool:
        return self.spec_enum == "SpecFuryWarrior"


@dataclasses.dataclass(frozen=True)
class InputContext:
    request: dict[str, Any]
    ep_weights_stats: dict[str, Any] | None = None
    ep_weights_source: str = ""


@dataclasses.dataclass(frozen=True)
class FrontendEPData:
    gems: tuple[dict[str, Any], ...] = ()
    random_suffixes: Mapping[int, dict[str, Any]] = dataclasses.field(default_factory=dict)
    reforge_stats: tuple[dict[str, Any], ...] = ()


@dataclasses.dataclass
class SimRunResult:
    label: str
    request_path: Path
    result_path: Path
    dps: float | None
    request_hash: str = ""
    dps_stdev: float | None = None
    dps_ci95: float | None = None
    iterations_done: int | None = None
    dps_delta: float | None = None
    percent_change: float | None = None
    item_id: int | None = None
    item_name: str = ""
    item_ilvl: int | None = None
    item_phase: int | None = None
    item_quality: str = ""
    slot_index: int | None = None
    slot: str = ""
    source: str = ""
    optimization_status: str = ""
    optimization_details: str = ""
    error: str = ""
    seconds: float = 0.0


@dataclasses.dataclass
class RunnerPaths:
    root: Path
    mop: Path
    exporter: Path
    cache: Path
    bin_dir: Path
    results: Path


@dataclasses.dataclass(frozen=True)
class SimCachePaths:
    digest: str
    request_path: Path
    result_path: Path


# ----------------------------- Generic utilities -----------------------------


def info(message: str) -> None:
    print(f"[info] {message}")


def warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr)


def die(message: str, code: int = 2) -> None:
    raise RunnerError(message)


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def run_cmd(
    args: Sequence[str | os.PathLike[str]],
    cwd: Path | None = None,
    check: bool = True,
    timeout: int | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    cmd = [str(a) for a in args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=dict(os.environ, **dict(env or {})),
    )
    result = CommandResult(cmd, proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RunnerError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr}")
    return result


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def enum_name(value: Any, names: Mapping[int, str], default: str = "") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        if value in names.values():
            return value
        parsed = as_int(value)
        if parsed is None:
            return value
        return names.get(parsed, default or value)
    parsed = as_int(value)
    if parsed is None:
        return default
    return names.get(parsed, default or str(value))


def first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def numeric_stat_map(value: Any) -> dict[int, float]:
    if isinstance(value, Mapping):
        out: dict[int, float] = {}
        for key, raw in value.items():
            idx = as_int(key)
            num = as_float(raw)
            if idx is not None and num is not None:
                out[idx] = num
        return out
    if isinstance(value, list):
        out = {}
        for idx, raw in enumerate(value):
            num = as_float(raw)
            if num is not None:
                out[idx] = num
        return out
    return {}


def write_json_file(path: Path, data: Any, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, indent=2, sort_keys=False)
            f.write("\n")
        else:
            json.dump(data, f, separators=(",", ":"), sort_keys=False)


NON_SEMANTIC_REQUEST_HASH_KEYS = {"request_id", "requestId"}


def hashable_request_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: hashable_request_payload(child)
            for key, child in value.items()
            if key not in NON_SEMANTIC_REQUEST_HASH_KEYS
        }
    if isinstance(value, list):
        return [hashable_request_payload(child) for child in value]
    return value


def request_hash(request: Mapping[str, Any]) -> str:
    canonical = json.dumps(hashable_request_payload(request), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sim_cache_paths(run_dir: Path, label: str, request: Mapping[str, Any]) -> SimCachePaths:
    del label
    digest = request_hash(request)
    return SimCachePaths(
        digest=digest,
        request_path=run_dir / "_requests" / f"{digest}.request.json",
        result_path=run_dir / "_results" / f"{digest}.result.json",
    )


def ensure_sim_cache_dirs(paths: SimCachePaths) -> None:
    paths.request_path.parent.mkdir(parents=True, exist_ok=True)
    paths.result_path.parent.mkdir(parents=True, exist_ok=True)


def ensure_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def http_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/json;q=0.9,*/*;q=0.1",
            "User-Agent": f"wowsims-mop-runner/{SCRIPT_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return json.loads(resp.read().decode(charset))


def http_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
            "User-Agent": f"wowsims-mop-runner/{SCRIPT_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# ----------------------------- Git / repo setup ------------------------------


def ensure_git_available() -> None:
    if not shutil.which("git"):
        die("git is required for clone/update but was not found on PATH.")


def git_default_branch(repo_dir: Path) -> str:
    for candidate in (
        ["git", "remote", "show", "origin"],
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
    ):
        result = run_cmd(candidate, cwd=repo_dir, check=False)
        if result.returncode == 0:
            text = result.stdout.strip()
            m = re.search(r"HEAD branch:\s*(\S+)", text)
            if m:
                return m.group(1)
            m = re.search(r"refs/remotes/origin/(\S+)$", text)
            if m:
                return m.group(1)
    for branch in ("main", "master"):
        res = run_cmd(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=repo_dir, check=False)
        if res.returncode == 0:
            return branch
    return "master"


def ensure_repo(repo_url: str, dest: Path, skip_update: bool = False) -> None:
    ensure_git_available()
    if dest.exists() and not (dest / ".git").exists():
        die(f"{dest} already exists but is not a git repo. Move it aside or delete it.")
    if not dest.exists():
        info(f"Cloning {repo_url} -> {dest.name}")
        run_cmd(["git", "clone", repo_url, str(dest)], timeout=600)
        return
    if skip_update:
        info(f"Using existing {dest.name} repo without update (--skip-update).")
        return
    info(f"Checking {dest.name} for updates")
    fetch = run_cmd(["git", "fetch", "--prune", "origin"], cwd=dest, check=False, timeout=300)
    if fetch.returncode != 0:
        warn(f"Could not fetch {dest.name}; using local checkout. Details: {fetch.stderr.strip() or fetch.stdout.strip()}")
        return
    branch = git_default_branch(dest)
    local = run_cmd(["git", "rev-parse", "HEAD"], cwd=dest, check=True).stdout.strip()
    remote_res = run_cmd(["git", "rev-parse", f"origin/{branch}"], cwd=dest, check=False)
    if remote_res.returncode != 0:
        warn(f"Could not determine origin/{branch}; leaving {dest.name} as-is.")
        return
    remote = remote_res.stdout.strip()
    if local == remote:
        info(f"{dest.name} is already at latest origin/{branch} ({local[:8]}).")
        return
    # Only fast-forward. Do not destroy local changes.
    status = run_cmd(["git", "status", "--porcelain"], cwd=dest, check=True).stdout.strip()
    if status:
        warn(f"{dest.name} has local changes; fetched latest but did not pull. Commit/stash changes, then rerun.")
        return
    info(f"Fast-forwarding {dest.name} from {local[:8]} to {remote[:8]}")
    run_cmd(["git", "pull", "--ff-only", "origin", branch], cwd=dest, timeout=300)


# ----------------------------- wowsimcli setup --------------------------------


def current_platform_asset_patterns() -> list[str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    is_x64 = machine in {"x86_64", "amd64"}
    is_arm64 = machine in {"arm64", "aarch64"}
    if system == "windows":
        return [r"wowsimcli.*windows.*\.zip$", r"wowsimcli.*\.exe\.zip$"]
    if system == "linux" and is_x64:
        return [r"wowsimcli.*amd64.*linux.*\.zip$", r"wowsimcli.*linux.*amd64.*\.zip$"]
    if system == "linux" and is_arm64:
        return [r"wowsimcli.*arm64.*linux.*\.zip$", r"wowsimcli.*linux.*arm64.*\.zip$"]
    if system == "darwin" and is_arm64:
        return [r"wowsimcli.*arm64.*darwin.*\.zip$", r"wowsimcli.*darwin.*arm64.*\.zip$"]
    if system == "darwin" and is_x64:
        return [r"wowsimcli.*amd64.*darwin.*\.zip$", r"wowsimcli.*darwin.*amd64.*\.zip$"]
    return [r"wowsimcli.*\.zip$"]


def find_binary_in_dir(path: Path) -> Path | None:
    names = ["wowsimcli.exe", "wowsimcli-windows.exe", "wowsimcli", "wowsimcli-amd64-linux", "wowsimcli-arm64-darwin"]
    for name in names:
        candidate = path / name
        if candidate.exists() and candidate.is_file():
            return candidate
    for candidate in path.rglob("wowsimcli*"):
        if candidate.is_file() and not candidate.name.endswith((".zip", ".json", ".txt")):
            return candidate
    return None


def download_latest_wowsimcli(bin_dir: Path, force: bool = False) -> Path | None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = bin_dir / "wowsimcli-release.json"
    try:
        release = http_json(f"{GITHUB_API}/repos/wowsims/mop/releases/latest")
    except Exception as exc:  # noqa: BLE001
        warn(f"Could not query latest wowsimcli release from GitHub API: {exc}")
        return None

    tag = release.get("tag_name") or release.get("name") or "latest"
    existing = find_binary_in_dir(bin_dir)
    if existing and marker.exists() and not force:
        with contextlib.suppress(Exception):
            meta = read_json_file(marker)
            if meta.get("tag_name") == tag:
                ensure_executable(existing)
                info(f"Using cached wowsimcli {tag}: {existing}")
                return existing

    assets = release.get("assets") or []
    patterns = current_platform_asset_patterns()
    selected: dict[str, Any] | None = None
    for pat in patterns:
        regex = re.compile(pat, re.IGNORECASE)
        for asset in assets:
            if regex.search(asset.get("name", "")):
                selected = asset
                break
        if selected:
            break

    if not selected:
        warn("No matching wowsimcli release asset found for this platform; will try local Go build.")
        return None

    asset_url = selected.get("browser_download_url")
    asset_name = selected.get("name") or "wowsimcli.zip"
    if not asset_url:
        warn("Latest release asset did not include a browser_download_url; will try local Go build.")
        return None

    info(f"Downloading wowsimcli {tag} asset {asset_name}")
    zip_path = bin_dir / asset_name
    req = urllib.request.Request(asset_url, headers={"User-Agent": f"wowsims-mop-runner/{SCRIPT_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, zip_path.open("wb") as out:
            shutil.copyfileobj(resp, out)
        # Clean previous extracted cli-ish binaries, but leave marker/cache.
        for child in bin_dir.iterdir():
            if child == zip_path or child == marker:
                continue
            if child.is_file() and child.name.startswith("wowsimcli"):
                child.unlink(missing_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(bin_dir)
    except Exception as exc:  # noqa: BLE001
        warn(f"Failed downloading/extracting wowsimcli release asset: {exc}")
        return None

    binary = find_binary_in_dir(bin_dir)
    if not binary:
        warn("Downloaded wowsimcli asset, but no executable-looking file was found inside it.")
        return None
    ensure_executable(binary)
    write_json_file(marker, {"tag_name": tag, "asset_name": asset_name, "downloaded_at_utc": utc_stamp()})
    info(f"Using wowsimcli: {binary}")
    return binary


def build_wowsimcli(mop_dir: Path, bin_dir: Path) -> Path | None:
    if not shutil.which("go"):
        warn("Go was not found on PATH; cannot build wowsimcli fallback.")
        return None
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = "wowsimcli.exe" if os.name == "nt" else "wowsimcli"
    out = bin_dir / exe
    info("Building wowsimcli from local mop checkout")
    commands = [
        ["go", "build", "-tags=with_db", "-o", str(out), "./cmd/wowsimcli/cli_main.go"],
        ["go", "build", "-o", str(out), "./cmd/wowsimcli/cli_main.go"],
    ]
    last_error = ""
    for cmd in commands:
        res = run_cmd(cmd, cwd=mop_dir, check=False, timeout=900)
        if res.returncode == 0 and out.exists():
            ensure_executable(out)
            return out
        last_error = res.stderr.strip() or res.stdout.strip()
    warn(f"Could not build wowsimcli. Last error:\n{last_error}")
    return None


def ensure_wowsimcli(paths: RunnerPaths, force_download: bool = False) -> Path:
    existing = find_binary_in_dir(paths.bin_dir)
    if existing and not force_download:
        ensure_executable(existing)
        return existing
    downloaded = download_latest_wowsimcli(paths.bin_dir, force=force_download)
    if downloaded:
        return downloaded
    built = build_wowsimcli(paths.mop, paths.bin_dir)
    if built:
        return built
    die(
        "Could not obtain wowsimcli. Install Go and rerun, or manually download a wowsimcli "
        "release asset into .wowsims_mop_runner/bin."
    )
    raise AssertionError("unreachable")


# ----------------------------- Input parsing ---------------------------------


def prompt_blob(label: str, allow_blank: bool = False) -> str:
    print()
    print(label)
    print("Paste a one-line string, type @path/to/file.json, or paste multiple lines and finish with a line containing only END.")
    first = input("> ").rstrip("\n")
    if not first and allow_blank:
        return ""
    if first.strip().upper() == "END":
        return ""
    if first.startswith("@") and len(first) > 1:
        path = Path(first[1:].strip()).expanduser()
        if not path.exists():
            die(f"File not found: {path}")
        return path.read_text(encoding="utf-8")
    lines = [first]
    # Heuristic: if the first line is already parseable or a URL, don't block for END.
    stripped = first.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or stripped.startswith("http"):
        return first
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def load_blob_or_path(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("@"):
        path = Path(value[1:]).expanduser()
        return path.read_text(encoding="utf-8")
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def maybe_unwrap_json_string(data: Any) -> Any:
    # Sometimes a pasted export is a JSON string containing JSON.
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("{") or s.startswith("["):
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(s)
    return data


def parse_jsonish(blob: str) -> Any:
    text = blob.strip().strip("\ufeff")
    if not text:
        die("No input was provided.")
    # Support accidental wrapping in code fences.
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return maybe_unwrap_json_string(data)
    except json.JSONDecodeError as exc:
        # If this looks like SavedVariables assignment, pull the JSON-ish object after '='.
        if "=" in text and "{" in text:
            candidate = text[text.find("{") :]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        die(f"Input is not valid JSON. Parse error: {exc}")
    raise AssertionError("unreachable")


def run_decodelink(wowsimcli: Path, link: str, out_dir: Path) -> Any:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run_cmd([str(wowsimcli), "decodelink", link], check=False, timeout=120)
    if result.returncode != 0:
        die(f"wowsimcli decodelink failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return parse_decodelink_stdout(result.stdout)


def parse_decodelink_stdout(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", stdout):
            with contextlib.suppress(json.JSONDecodeError):
                parsed, _end = decoder.raw_decode(stdout[match.start() :])
                return parsed
        die(f"wowsimcli decodelink did not return JSON:\n{stdout[:1000]}")


def classify_payload(data: Any) -> str:
    if isinstance(data, dict):
        keys = set(data.keys())
        if "raid" in keys and ("simOptions" in keys or "sim_options" in keys or "type" in keys):
            return "raid_request"
        if "player" in keys and ("settings" in keys or "encounter" in keys):
            return "individual_settings"
        if "gear" in keys and "class" in keys and "spec" in keys:
            return "wse_character"
        if "items" in keys and isinstance(data.get("items"), list):
            return "equipment_spec"
        if "raidSimRequest" in keys:
            return "raid_request_wrapped"
        if "individualSimSettings" in keys:
            return "individual_settings_wrapped"
    return "unknown"


def load_user_payload(blob: str, wowsimcli: Path, out_dir: Path) -> tuple[str, Any]:
    text = blob.strip()
    if re.match(r"^https?://", text) and "#" in text:
        data = run_decodelink(wowsimcli, text, out_dir)
        kind = classify_payload(data)
        if kind == "unknown" and "player" in data:
            kind = "individual_settings"
        return kind, data
    data = parse_jsonish(text)
    kind = classify_payload(data)
    if kind == "raid_request_wrapped":
        return "raid_request", data["raidSimRequest"]
    if kind == "individual_settings_wrapped":
        return "individual_settings", data["individualSimSettings"]
    return kind, data


# ----------------------------- RaidSimRequest building ------------------------


def proto_enum_from_wse_class(value: Any) -> str:
    s_text = normalize_text(value)
    s_key = normalize_key(value)
    return CLASS_ENUM_BY_WSE.get(s_text) or CLASS_ENUM_BY_WSE.get(s_key) or "ClassUnknown"


def proto_enum_from_wse_race(value: Any) -> str:
    s_text = normalize_text(value)
    s_key = normalize_key(value)
    return RACE_ENUM_BY_WSE.get(s_text) or RACE_ENUM_BY_WSE.get(s_key) or "RaceUnknown"


def spec_field_from_wse(class_name: Any, spec_name: Any) -> str:
    cls_text = normalize_text(class_name)
    cls_key = normalize_key(class_name)
    spec_text = normalize_text(spec_name)
    spec_key = normalize_key(spec_name)
    return (
        SPEC_FIELD_BY_CLASS_AND_SPEC.get((cls_text, spec_text))
        or SPEC_FIELD_BY_CLASS_AND_SPEC.get((cls_key, spec_text))
        or SPEC_FIELD_BY_CLASS_AND_SPEC.get((cls_text, spec_key))
        or SPEC_FIELD_BY_CLASS_AND_SPEC.get((cls_key, spec_key))
        or SPEC_ONEOF_FIELD_BY_WSE.get(spec_text)
        or SPEC_ONEOF_FIELD_BY_WSE.get(spec_key)
        or ""
    )


def spec_enum_from_wse(class_name: Any, spec_name: Any) -> str:
    field = spec_field_from_wse(class_name, spec_name)
    return SPEC_ENUM_BY_PLAYER_FIELD.get(field, "SpecUnknown")


def normalize_item_spec(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    out: dict[str, Any] = {}
    # Keep proto snake_case names; protojson accepts them and they match WSE.
    for src, dst in (
        ("id", "id"),
        ("itemId", "id"),
        ("item_id", "id"),
        ("randomSuffix", "random_suffix"),
        ("random_suffix", "random_suffix"),
        ("enchant", "enchant"),
        ("gems", "gems"),
        ("reforging", "reforging"),
        ("reforge", "reforging"),
        ("upgradeStep", "upgrade_step"),
        ("upgrade_step", "upgrade_step"),
        ("challengeMode", "challenge_mode"),
        ("challenge_mode", "challenge_mode"),
        ("tinker", "tinker"),
    ):
        if src in item and item[src] not in (None, ""):
            out[dst] = item[src]
    if "gems" in out and isinstance(out["gems"], list):
        out["gems"] = [int(g or 0) for g in out["gems"]]
    for key in ("id", "random_suffix", "enchant", "reforging", "upgrade_step", "tinker"):
        if key in out and out[key] is not None:
            with contextlib.suppress(ValueError, TypeError):
                out[key] = int(out[key])
    return out


def item_spec_mod_summary(item: Mapping[str, Any]) -> str:
    parts: list[str] = []
    gems = [str(gem) for gem in item.get("gems", []) if as_int(gem)]
    if gems:
        parts.append("gems=" + "/".join(gems))
    enchant = as_int(item.get("enchant"))
    if enchant:
        parts.append(f"enchant={enchant}")
    reforge = as_int(item.get("reforging", item.get("reforge")))
    if reforge:
        parts.append(f"reforge={reforge}")
    tinker = as_int(item.get("tinker"))
    if tinker:
        parts.append(f"tinker={tinker}")
    upgrade_step = as_int(item.get("upgrade_step", item.get("upgradeStep")))
    if upgrade_step:
        parts.append(f"upgrade_step={upgrade_step}")
    return "; ".join(parts) if parts else "none selected"


def normalize_equipment_spec(equipment: Any) -> dict[str, Any]:
    if not isinstance(equipment, dict):
        return {"items": []}
    items = equipment.get("items") or []
    if not isinstance(items, list):
        return {"items": []}
    return {"items": [normalize_item_spec(item) if item else {} for item in items]}


def normalize_wse_character_gear(equipment: Any) -> dict[str, Any]:
    if not isinstance(equipment, dict):
        return {"items": []}
    items = equipment.get("items") or []
    if not isinstance(items, list):
        return {"items": []}
    return {"items": [normalize_item_spec(item) for item in items if item is not None]}


def normalize_wse_glyphs(glyphs: Any, glyph_spell_to_item: Mapping[int, int] | None = None) -> dict[str, int]:
    if not isinstance(glyphs, dict):
        return {}
    out: dict[str, int] = {}

    def glyph_id(entry: Any) -> int | None:
        if isinstance(entry, dict):
            value = entry.get("spellID", entry.get("spellId", entry.get("spell_id")))
            spell_id = as_int(value)
            if glyph_spell_to_item is not None:
                return glyph_spell_to_item.get(spell_id or 0, 0)
            return spell_id
        value = entry
        parsed = as_int(value)
        if parsed is not None:
            return parsed
        if isinstance(value, str) and value.strip():
            die(
                f"Cannot import legacy WSE glyph name {value!r}; this runner only accepts "
                "current WSE glyph tables with spellID values or numeric glyph IDs."
            )
        return None

    def glyph_ids(entries: Any) -> list[int]:
        if not isinstance(entries, list):
            return []
        values: list[int] = []
        for entry in entries:
            value = glyph_id(entry)
            if value:
                values.append(value)
        return values

    majors = glyph_ids(glyphs.get("major", []))
    minors = glyph_ids(glyphs.get("minor", []))
    for i, value in enumerate(majors[:3], 1):
        out[f"major{i}"] = value
    for i, value in enumerate(minors[:3], 1):
        out[f"minor{i}"] = value
    return out


def professions_from_wse(professions: Any) -> tuple[str, str]:
    if not isinstance(professions, list):
        return "ProfessionUnknown", "ProfessionUnknown"
    vals: list[str] = []
    for prof in professions:
        name = prof.get("name") if isinstance(prof, dict) else prof
        enum = PROFESSION_ENUM_BY_NAME.get(normalize_text(name)) or PROFESSION_ENUM_BY_NAME.get(normalize_key(name))
        if enum:
            vals.append(enum)
    vals = vals[:2]
    while len(vals) < 2:
        vals.append("ProfessionUnknown")
    return vals[0], vals[1]


def validate_wse_import_values(character: Mapping[str, Any]) -> None:
    class_value = character.get("class")
    if proto_enum_from_wse_class(class_value) == "ClassUnknown":
        die(f"Could not parse WSE class {class_value!r}.")

    race_value = character.get("race")
    if proto_enum_from_wse_race(race_value) == "RaceUnknown":
        die(f"Could not parse WSE race {race_value!r}.")

    professions = character.get("professions")
    if professions in (None, ""):
        return
    if not isinstance(professions, list):
        die("WSE professions must be a list.")
    for prof in professions:
        name = prof.get("name") if isinstance(prof, Mapping) else prof
        enum = (
            PROFESSION_ENUM_BY_NAME.get(normalize_text(name))
            or PROFESSION_ENUM_BY_NAME.get(normalize_key(name))
        )
        if not enum:
            die(f"Could not parse WSE profession {name!r}.")


def lower_camel_from_snake(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def player_spec_oneof_keys() -> set[str]:
    keys: set[str] = set()
    for field in SPEC_ENUM_BY_PLAYER_FIELD:
        keys.add(field)
        keys.add(lower_camel_from_snake(field))
    return keys


PLAYER_NODE_HINT_KEYS = {
    "class",
    "race",
    "talentsString",
    "talents_string",
    "glyphs",
    "rotation",
    "profession1",
    "profession2",
    "consumables",
    "buffs",
    "cooldowns",
}


def is_player_node(obj: Mapping[str, Any]) -> bool:
    keys = set(obj.keys())
    if keys & player_spec_oneof_keys():
        return True
    if ("equipment" in keys or "gear" in keys) and keys & PLAYER_NODE_HINT_KEYS:
        return True
    if "class" in keys and len(keys & PLAYER_NODE_HINT_KEYS) >= 2:
        return True
    return False


def find_first_player_node(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        if is_player_node(obj):
            return obj
        for value in obj.values():
            found = find_first_player_node(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_first_player_node(value)
            if found is not None:
                return found
    return None


def get_request_player(request: dict[str, Any]) -> dict[str, Any]:
    found = find_first_player_node(request)
    if found is None:
        die("Could not find a Player node in the RaidSimRequest.")
    return found


def convert_individual_settings_to_raid_request(settings: dict[str, Any], iterations: int | None = None) -> dict[str, Any]:
    player = copy.deepcopy(settings.get("player") or {})
    if not player:
        die("IndividualSimSettings did not contain a player field.")
    sim_settings = settings.get("settings") or {}
    sim_options = {"iterations": int(iterations or sim_settings.get("iterations") or 10000)}
    raid: dict[str, Any] = {
        "parties": [
            {
                "players": [player],
                "buffs": copy.deepcopy(settings.get("partyBuffs") or settings.get("party_buffs") or {}),
            }
        ],
        "num_active_parties": 1,
        "buffs": copy.deepcopy(settings.get("raidBuffs") or settings.get("raid_buffs") or {}),
        "debuffs": copy.deepcopy(settings.get("debuffs") or {}),
        "tanks": copy.deepcopy(settings.get("tanks") or []),
    }
    target_dummies = int(settings.get("targetDummies") or settings.get("target_dummies") or 0)
    if target_dummies:
        raid["target_dummies"] = target_dummies
    ensure_raid_party_capacity(raid)
    return {
        "request_id": f"wse-runner-{utc_stamp()}",
        "raid": raid,
        "encounter": copy.deepcopy(settings.get("encounter") or default_encounter()),
        "sim_options": sim_options,
        "type": "SimTypeIndividual",
    }


def default_encounter() -> dict[str, Any]:
    # A minimal fallback; a UI/template-derived encounter is strongly preferred.
    return {
        "api_version": 9,
        "duration": 300,
        "duration_variation": 0,
        "execute_proportion_20": 0.2,
        "targets": [
            {
                "id": 0,
                "name": "Target Dummy",
                "level": 93,
                "mob_type": "MobTypeHumanoid",
            }
        ],
    }


def ensure_raid_party_capacity(raid: dict[str, Any]) -> None:
    parties = raid.get("parties")
    if not isinstance(parties, list) or not parties:
        parties = [{}]
        raid["parties"] = parties

    explicit_players = 0
    for party in parties:
        if not isinstance(party, Mapping):
            continue
        players = party.get("players")
        if isinstance(players, list):
            explicit_players += len([player for player in players if player])

    target_dummies = as_int(raid.get("target_dummies", raid.get("targetDummies"))) or 0
    required_slots = explicit_players + target_dummies
    required_parties = max(1, math.ceil(required_slots / RAID_PARTY_SIZE))
    if required_parties > MAX_RAID_PARTIES:
        die(
            f"Raid requires {required_parties} active parties for {explicit_players} players and "
            f"{target_dummies} target dummies, but WoWSims supports at most {MAX_RAID_PARTIES}."
        )

    current_active_parties = as_int(raid.get("num_active_parties", raid.get("numActiveParties"))) or len(parties)
    active_parties = max(1, current_active_parties, required_parties)
    if active_parties > MAX_RAID_PARTIES:
        die(
            f"Raid declares {active_parties} active parties, but WoWSims supports at most {MAX_RAID_PARTIES}."
        )

    while len(parties) < active_parties:
        parties.append({})
    if "numActiveParties" in raid and "num_active_parties" not in raid:
        raid["numActiveParties"] = active_parties
    else:
        raid["num_active_parties"] = active_parties


def minimal_request_from_wse_character(
    character: dict[str, Any],
    iterations: int,
    glyph_spell_to_item: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    validate_wse_import_values(character)
    class_enum = proto_enum_from_wse_class(character.get("class"))
    race_enum = proto_enum_from_wse_race(character.get("race"))
    spec_field = spec_field_from_wse(character.get("class"), character.get("spec"))
    profession1, profession2 = professions_from_wse(character.get("professions"))
    player: dict[str, Any] = {
        "name": str(character.get("name") or "WSE Character"),
        "race": race_enum,
        "class": class_enum,
        "equipment": normalize_wse_character_gear(character.get("gear")),
        "talents_string": str(character.get("talents") or ""),
        "glyphs": normalize_wse_glyphs(character.get("glyphs"), glyph_spell_to_item),
        "profession1": profession1,
        "profession2": profession2,
    }
    if spec_field:
        player[spec_field] = {}
    else:
        warn("Could not map WSE spec to a WoWSims player oneof field; a template/share link may be required.")
    return {
        "request_id": f"wse-runner-{utc_stamp()}",
        "raid": {"parties": [{"players": [player]}], "num_active_parties": 1},
        "encounter": default_encounter(),
        "sim_options": {"iterations": iterations},
        "type": "SimTypeIndividual",
    }


def inject_wse_character_into_request(
    request: dict[str, Any],
    character: dict[str, Any],
    iterations: int | None = None,
    glyph_spell_to_item: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    validate_wse_import_values(character)
    req = copy.deepcopy(request)
    player = get_request_player(req)
    template_class = str(player.get("class") or player.get("class_") or "ClassUnknown")
    template_spec = player_spec_enum(player)
    class_enum = proto_enum_from_wse_class(character.get("class"))
    spec_enum = spec_enum_from_wse(character.get("class"), character.get("spec"))
    if (
        class_enum != "ClassUnknown"
        and template_class != "ClassUnknown"
        and template_class != class_enum
    ):
        die(
            f"Template class {template_class} does not match WSE class {class_enum}. "
            "Provide a matching WoWSims template/share link."
        )
    if (
        spec_enum != "SpecUnknown"
        and template_spec != "SpecUnknown"
        and template_spec != spec_enum
    ):
        die(
            f"Template spec {template_spec} does not match WSE spec {spec_enum}. "
            "Provide a matching WoWSims template/share link."
        )
    player["equipment"] = normalize_wse_character_gear(character.get("gear"))
    player.pop("gear", None)
    if character.get("talents"):
        player["talents_string"] = str(character.get("talents"))
        player.pop("talentsString", None)
    glyphs = normalize_wse_glyphs(character.get("glyphs"), glyph_spell_to_item)
    if glyphs:
        player["glyphs"] = glyphs
    race_enum = proto_enum_from_wse_race(character.get("race"))
    if class_enum != "ClassUnknown":
        player["class"] = class_enum
    if race_enum != "RaceUnknown":
        player["race"] = race_enum
    profession1, profession2 = professions_from_wse(character.get("professions"))
    if profession1 != "ProfessionUnknown":
        player["profession1"] = profession1
    if profession2 != "ProfessionUnknown":
        player["profession2"] = profession2
    if iterations:
        sim_options = ensure_sim_options(req)
        sim_options["iterations"] = int(iterations)
    return req


def ui_spec_dir_for_player_field(mop_dir: Path, spec_field: str) -> Path | None:
    for class_dir in sorted(UI_CLASS_DIR_SUFFIXES, key=len, reverse=True):
        suffix = f"_{class_dir}"
        if spec_field.endswith(suffix):
            spec_dir = spec_field[: -len(suffix)]
            if spec_dir:
                return mop_dir / "ui" / class_dir / spec_dir
    return None


def ts_json_imports(path: Path, suffix: str) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    imports: dict[str, str] = {}
    pattern = rf"import\s+(\w+)\s+from\s+['\"]([^'\"]+{re.escape(suffix)})['\"]\s*;?"
    for match in re.finditer(pattern, text):
        imports[match.group(1)] = match.group(2)
    return imports


def resolve_ts_import_path(source_path: Path, import_path: str) -> Path | None:
    path = (source_path.parent / Path(import_path)).resolve()
    return path if path.exists() else None


def resolve_preset_build_path(spec_dir: Path, preset_const: str) -> Path | None:
    presets_path = spec_dir / "presets.ts"
    if not presets_path.exists():
        return None
    text = presets_path.read_text(encoding="utf-8")
    imports = ts_json_imports(presets_path, ".build.json")

    export_pattern = re.compile(
        rf"export\s+const\s+{re.escape(preset_const)}\s*=\s*PresetUtils\.makePresetBuildFromJSON\((.*?)\);",
        flags=re.DOTALL,
    )
    export_match = export_pattern.search(text)
    if not export_match:
        return None
    body = export_match.group(1)
    build_arg = re.search(r"Spec\.\w+\s*,\s*(\w+)", body)
    if not build_arg:
        return None
    import_path = imports.get(build_arg.group(1))
    if not import_path:
        return None
    return resolve_ts_import_path(presets_path, import_path)


def resolve_preset_gear_path(spec_dir: Path, preset_const: str) -> Path | None:
    presets_path = spec_dir / "presets.ts"
    if not presets_path.exists():
        return None
    text = presets_path.read_text(encoding="utf-8")
    imports = ts_json_imports(presets_path, ".gear.json")

    export_pattern = re.compile(
        rf"export\s+const\s+{re.escape(preset_const)}\s*=\s*PresetUtils\.makePresetGear\((.*?)\);",
        flags=re.DOTALL,
    )
    export_match = export_pattern.search(text)
    if not export_match:
        return None
    body = export_match.group(1)
    gear_arg = re.search(r",\s*(\w+)\s*(?:,|$)", body.strip())
    if not gear_arg:
        return None
    import_path = imports.get(gear_arg.group(1))
    if not import_path:
        return None
    return resolve_ts_import_path(presets_path, import_path)


def default_gear_path_from_sim(spec_dir: Path, sim_text: str) -> Path | None:
    default_gear = re.search(r"defaults\s*:\s*\{[\s\S]*?gear\s*:\s*Presets\.(\w+)\.gear", sim_text)
    if not default_gear:
        return None
    return resolve_preset_gear_path(spec_dir, default_gear.group(1))


def preset_build_consts_from_sim(sim_text: str) -> list[str]:
    builds = re.search(r"builds\s*:\s*\[(.*?)\]", sim_text, flags=re.DOTALL)
    if not builds:
        return []
    return re.findall(r"Presets\.(\w+)", builds.group(1))


def normalized_equipment_for_match(equipment: Any) -> dict[str, Any]:
    return normalize_equipment_spec(equipment)


def build_matches_default_gear(build_path: Path, default_gear: Mapping[str, Any]) -> bool:
    build = read_json_file(build_path)
    if not isinstance(build, Mapping):
        return False
    player = build.get("player")
    if not isinstance(player, Mapping):
        return False
    equipment = player.get("equipment")
    if not isinstance(equipment, Mapping):
        return False
    return normalized_equipment_for_match(equipment) == normalized_equipment_for_match(default_gear)


def resolve_build_matching_default_gear(spec_dir: Path, sim_text: str) -> Path | None:
    gear_path = default_gear_path_from_sim(spec_dir, sim_text)
    if gear_path is None:
        return None
    default_gear = read_json_file(gear_path)
    if not isinstance(default_gear, Mapping):
        return None

    matches: list[Path] = []
    for build_const in preset_build_consts_from_sim(sim_text):
        build_path = resolve_preset_build_path(spec_dir, build_const)
        if build_path is not None and build_matches_default_gear(build_path, default_gear):
            matches.append(build_path)
    return matches[0] if len(matches) == 1 else None


def official_default_build_path(mop_dir: Path, spec_field: str) -> Path | None:
    spec_dir = ui_spec_dir_for_player_field(mop_dir, spec_field)
    if spec_dir is None or not spec_dir.exists():
        return None

    for sim_name in ("sim.ts", "sim.tsx"):
        sim_path = spec_dir / sim_name
        if not sim_path.exists():
            continue
        sim_text = sim_path.read_text(encoding="utf-8")
        default_build = re.search(r"defaultBuild\s*:\s*Presets\.(\w+)", sim_text)
        if default_build:
            resolved = resolve_preset_build_path(spec_dir, default_build.group(1))
            if resolved is not None:
                return resolved
        resolved = resolve_build_matching_default_gear(spec_dir, sim_text)
        if resolved is not None:
            return resolved

    build_dir = spec_dir / "builds"
    build_files = sorted(build_dir.glob("*.build.json")) if build_dir.exists() else []
    if len(build_files) == 1:
        return build_files[0]
    return None


def official_default_settings_for_wse_character(mop_dir: Path, character: dict[str, Any]) -> dict[str, Any]:
    spec_field = spec_field_from_wse(character.get("class"), character.get("spec"))
    if not spec_field:
        die(
            "Could not map the WSE class/spec to a WoWSims player spec. "
            "Provide a WoWSims template/share link with --template."
        )
    build_path = official_default_build_path(mop_dir, spec_field)
    if build_path is None:
        die(
            f"No official WoWSims default build was found for WSE spec {spec_field!r}. "
            "Provide a WoWSims template/share link with --template so buffs, APL, encounter, and spec options are known."
        )
    info(f"Using official WoWSims default build for WSE-only import: {build_path}")
    settings = read_json_file(build_path)
    if classify_payload(settings) != "individual_settings":
        die(f"Official default build {build_path} was not an IndividualSimSettings JSON file.")
    return settings


def extract_ep_weights_stats(settings: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = first_present(settings, "epWeightsStats", "ep_weights_stats")
    if not isinstance(raw, Mapping):
        return None
    return copy.deepcopy(dict(raw))


def build_input_context_from_payload(
    kind: str,
    payload: Any,
    wowsimcli: Path,
    out_dir: Path,
    iterations: int,
    template_blob: str = "",
    glyph_spell_to_item: Mapping[int, int] | None = None,
    mop_dir: Path | None = None,
) -> InputContext:
    if kind == "raid_request":
        req = copy.deepcopy(payload)
        set_iterations(req, iterations)
        return InputContext(request=req)
    if kind == "individual_settings":
        ep_weights_stats = extract_ep_weights_stats(payload)
        return InputContext(
            request=convert_individual_settings_to_raid_request(payload, iterations=iterations),
            ep_weights_stats=ep_weights_stats,
            ep_weights_source="input IndividualSimSettings" if ep_weights_stats else "",
        )
    if kind != "wse_character":
        die(f"Cannot build a RaidSimRequest from payload kind {kind!r}.")

    template_blob = template_blob.strip()
    if template_blob:
        t_kind, t_payload = load_user_payload(template_blob, wowsimcli, out_dir)
        ep_weights_stats: dict[str, Any] | None = None
        ep_weights_source = ""
        if t_kind == "individual_settings":
            base = convert_individual_settings_to_raid_request(t_payload, iterations=iterations)
            ep_weights_stats = extract_ep_weights_stats(t_payload)
            if ep_weights_stats:
                ep_weights_source = "template IndividualSimSettings"
        elif t_kind == "raid_request":
            base = copy.deepcopy(t_payload)
            set_iterations(base, iterations)
        else:
            die(f"Template/share-link input must decode to IndividualSimSettings or RaidSimRequest, got {t_kind!r}.")
        return InputContext(
            request=inject_wse_character_into_request(base, payload, iterations=iterations, glyph_spell_to_item=glyph_spell_to_item),
            ep_weights_stats=ep_weights_stats,
            ep_weights_source=ep_weights_source,
        )

    if mop_dir is None:
        die(
            "No WoWSims template/share link was provided and no MoP repo path was available to load official defaults. "
            "Provide --template or call minimal_request_from_wse_character() explicitly for diagnostic-only requests."
        )
    settings = official_default_settings_for_wse_character(mop_dir, payload)
    base = convert_individual_settings_to_raid_request(settings, iterations=iterations)
    ep_weights_stats = extract_ep_weights_stats(settings)
    return InputContext(
        request=inject_wse_character_into_request(base, payload, iterations=iterations, glyph_spell_to_item=glyph_spell_to_item),
        ep_weights_stats=ep_weights_stats,
        ep_weights_source="official default IndividualSimSettings" if ep_weights_stats else "",
    )


def build_request_from_payload(
    kind: str,
    payload: Any,
    wowsimcli: Path,
    out_dir: Path,
    iterations: int,
    template_blob: str = "",
    glyph_spell_to_item: Mapping[int, int] | None = None,
    mop_dir: Path | None = None,
) -> dict[str, Any]:
    return build_input_context_from_payload(
        kind,
        payload,
        wowsimcli,
        out_dir,
        iterations,
        template_blob=template_blob,
        glyph_spell_to_item=glyph_spell_to_item,
        mop_dir=mop_dir,
    ).request


def set_iterations(request: dict[str, Any], iterations: int | None) -> None:
    if not iterations:
        return
    sim_options = ensure_sim_options(request)
    sim_options["iterations"] = int(iterations)


def ensure_sim_options(request: dict[str, Any]) -> dict[str, Any]:
    has_snake = "sim_options" in request
    has_camel = "simOptions" in request
    if has_snake and has_camel:
        die("RaidSimRequest contains both sim_options and simOptions; remove one casing before running.")
    key = "sim_options" if has_snake or not has_camel else "simOptions"
    sim_options = request.get(key)
    if not isinstance(sim_options, dict):
        sim_options = {}
        request[key] = sim_options
    return sim_options


def request_equipment_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    player = get_request_player(request)
    equipment = player.get("equipment") or player.get("gear")
    if not isinstance(equipment, dict):
        die("Player did not have an equipment/gear object.")
    items = equipment.setdefault("items", [])
    if not isinstance(items, list):
        die("Player equipment.items was not a list.")
    return items


# ----------------------------- Item metadata ---------------------------------


def load_item_index(mop_dir: Path, cache_dir: Path, refresh: bool = False) -> dict[int, ItemMeta]:
    cache_path = cache_dir / "canonical_item_index.json"
    if cache_path.exists() and not refresh:
        with contextlib.suppress(Exception):
            raw = read_json_file(cache_path)
            return {int(k): ItemMeta(**v) for k, v in raw.items()}
    info("Loading item metadata from WoWSims generated database")
    index = load_canonical_item_index(mop_dir)
    if not index:
        paths = ", ".join(str(path) for path in canonical_db_paths(mop_dir))
        die(f"Could not load WoWSims item metadata. Expected generated database at one of: {paths}")
    write_json_file(cache_path, {str(k): dataclasses.asdict(v) for k, v in index.items()})
    info(f"Loaded metadata for {len(index):,} items from generated WoWSims database")
    return index


def canonical_db_paths(mop_dir: Path) -> list[Path]:
    return [
        mop_dir / "assets" / "database" / "db.json",
        mop_dir / "assets" / "database" / "leftover_db.json",
    ]


def load_canonical_item_index(mop_dir: Path) -> dict[int, ItemMeta]:
    index: dict[int, ItemMeta] = {}
    for path in canonical_db_paths(mop_dir):
        if not path.exists():
            continue
        data = read_json_file(path)
        if not isinstance(data, dict):
            continue
        zones = {
            int(zone["id"]): str(zone.get("name") or "")
            for zone in data.get("zones", []) or []
            if isinstance(zone, dict) and as_int(zone.get("id")) is not None
        }
        npcs = {
            int(npc["id"]): str(npc.get("name") or "")
            for npc in data.get("npcs", []) or []
            if isinstance(npc, dict) and as_int(npc.get("id")) is not None
        }
        for item in data.get("items", []) or []:
            if not isinstance(item, dict) or as_int(item.get("id")) is None:
                continue
            meta = item_meta_from_dict(item, zones=zones, npcs=npcs)
            if meta.id and (meta.id not in index or richer_meta(meta, index[meta.id])):
                index[meta.id] = meta
    return index


def load_glyph_spell_to_item_map(mop_dir: Path) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for path in canonical_db_paths(mop_dir):
        if not path.exists():
            continue
        data = read_json_file(path)
        if not isinstance(data, Mapping):
            continue
        for glyph in data.get("glyphIds", []) or []:
            if not isinstance(glyph, Mapping):
                continue
            spell_id = as_int(glyph.get("spellId"))
            item_id = as_int(glyph.get("itemId"))
            if spell_id and item_id:
                mapping[spell_id] = item_id
    return mapping


def parse_json_item_file(text: str, index: dict[int, ItemMeta]) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return

    zones = {}
    npcs = {}
    if isinstance(data, dict):
        for z in data.get("zones", []) or []:
            if isinstance(z, dict) and "id" in z:
                zones[int(z["id"])] = z.get("name", "")
        for npc in data.get("npcs", []) or []:
            if isinstance(npc, dict) and "id" in npc:
                npcs[int(npc["id"])] = npc.get("name", "")

    candidates: list[Any] = []
    if isinstance(data, dict):
        for key in ("items", "Items"):
            if isinstance(data.get(key), list):
                candidates.extend(data[key])
    elif isinstance(data, list):
        candidates = data
    for item in candidates:
        if not isinstance(item, dict) or "id" not in item:
            continue
        with contextlib.suppress(Exception):
            item_id = int(item["id"])
            meta = item_meta_from_dict(item, zones=zones, npcs=npcs)
            if item_id and (item_id not in index or richer_meta(meta, index[item_id])):
                index[item_id] = meta


def best_scaling_option(item: Mapping[str, Any]) -> Mapping[str, Any]:
    options = item.get("scalingOptions") or item.get("scaling_options") or {}
    if not isinstance(options, Mapping):
        return {}
    keyed: list[tuple[int, Mapping[str, Any]]] = []
    for key, value in options.items():
        parsed = as_int(key)
        if parsed is None or not isinstance(value, Mapping):
            continue
        keyed.append((parsed, value))
    if not keyed:
        return {}
    return max(keyed, key=lambda pair: pair[0])[1]


def source_mapping(src: Mapping[str, Any], *keys: str) -> MutableMapping[str, Any] | None:
    for key in keys:
        value = src.get(key)
        if isinstance(value, MutableMapping):
            return value
    return None


def normalize_sources(sources: Any, zones: Mapping[int, str], npcs: Mapping[int, str]) -> list[dict[str, Any]]:
    if not isinstance(sources, list):
        return []
    normalized = copy.deepcopy(sources)
    for src in normalized:
        if not isinstance(src, MutableMapping):
            continue
        for node in (
            source_mapping(src, "drop", "Drop"),
            source_mapping(src, "soldBy", "sold_by", "SoldBy"),
        ):
            if node is None:
                continue
            npc_id = as_int(node.get("npcId", node.get("npc_id")))
            zone_id = as_int(node.get("zoneId", node.get("zone_id")))
            if npc_id is not None:
                node.setdefault("npcName", npcs.get(npc_id, ""))
                node.setdefault("npc_name", npcs.get(npc_id, ""))
            if zone_id is not None:
                node.setdefault("zoneName", zones.get(zone_id, ""))
                node.setdefault("zone_name", zones.get(zone_id, ""))
    return [dict(src) for src in normalized if isinstance(src, Mapping)]


def item_meta_from_dict(item: dict[str, Any], zones: Mapping[int, str] | None = None, npcs: Mapping[int, str] | None = None) -> ItemMeta:
    zones = zones or {}
    npcs = npcs or {}
    item_id = as_int(first_present(item, "id", "itemId")) or 0
    scaling = best_scaling_option(item)
    ilvl = as_int(first_present(item, "ilvl"))
    if ilvl is None:
        ilvl = as_int(first_present(scaling, "ilvl"))
    raw_sources = first_present(item, "sources", "Sources") or []
    sources = normalize_sources(raw_sources, zones=zones, npcs=npcs)
    required_profession = enum_name(
        first_present(item, "requiredProfession", "required_profession"),
        PROFESSION_NAMES,
    )
    return ItemMeta(
        id=item_id,
        name=str(item.get("name") or item.get("Name") or item.get("name_enus") or ""),
        type=enum_name(first_present(item, "type", "itemType", "item_type"), ITEM_TYPE_NAMES),
        armor_type=enum_name(first_present(item, "armorType", "armor_type", "armorTypeName"), ARMOR_TYPE_NAMES),
        weapon_type=enum_name(first_present(item, "weaponType", "weapon_type", "weaponTypeName"), WEAPON_TYPE_NAMES),
        hand_type=enum_name(first_present(item, "handType", "hand_type", "handTypeName"), HAND_TYPE_NAMES),
        ranged_weapon_type=enum_name(first_present(item, "rangedWeaponType", "ranged_weapon_type"), RANGED_WEAPON_TYPE_NAMES),
        ilvl=ilvl,
        quality=enum_name(first_present(item, "quality"), QUALITY_NAMES),
        phase=as_int(first_present(item, "phase")),
        unique=bool(item.get("unique") or False),
        limit_category=as_int(first_present(item, "limitCategory", "limit_category")),
        faction_restriction=enum_name(first_present(item, "factionRestriction", "faction_restriction"), {0: "", 1: "Alliance", 2: "Horde"}),
        class_allowlist=[
            enum_name(value, CLASS_NAMES)
            for value in item.get("classAllowlist", item.get("class_allowlist", [])) or []
            if enum_name(value, CLASS_NAMES)
        ],
        required_profession="" if required_profession == "ProfessionUnknown" else required_profession,
        sources=sources if isinstance(sources, list) else [],
        raw=item,
    )


def parse_code_item_file(text: str, index: dict[int, ItemMeta]) -> None:
    # Heuristic parser for TS/Go generated item objects. It is intentionally permissive.
    if "UIItem" not in text and "ItemType" not in text and "ItemsByID" not in text and "Name:" not in text:
        return
    # Split around id markers to keep regex bounded.
    for m in re.finditer(r"(?:\bid\s*[:=]|\bID\s*:)\s*(\d{3,7})", text):
        item_id = int(m.group(1))
        start = max(0, m.start() - 500)
        end = min(len(text), m.end() + 2500)
        chunk = text[start:end]
        name = regex_first(chunk, [r"\bname\s*[:=]\s*[\"']([^\"']+)", r"\bName\s*:\s*[\"']([^\"']+)"])
        itype = regex_first(chunk, [r"ItemType\.([A-Za-z0-9_]+)", r"\btype\s*[:=]\s*[\"']?([A-Za-z0-9_]+)"])
        armor = regex_first(chunk, [r"ArmorType\.([A-Za-z0-9_]+)", r"\barmor[_A-Za-z]*\s*[:=]\s*[\"']?([A-Za-z0-9_]+)"])
        weapon = regex_first(chunk, [r"WeaponType\.([A-Za-z0-9_]+)", r"\bweapon[_A-Za-z]*\s*[:=]\s*[\"']?([A-Za-z0-9_]+)"])
        hand = regex_first(chunk, [r"HandType\.([A-Za-z0-9_]+)", r"\bhand[_A-Za-z]*\s*[:=]\s*[\"']?([A-Za-z0-9_]+)"])
        ilvl_s = regex_first(chunk, [r"\bilvl\s*[:=]\s*(\d+)", r"\bIlvl\s*:\s*(\d+)"])
        phase_s = regex_first(chunk, [r"\bphase\s*[:=]\s*(\d+)", r"\bPhase\s*:\s*(\d+)"])
        if itype and not itype.startswith("ItemType"):
            itype = f"ItemType{itype}" if itype[0].isupper() else itype
        if armor and not armor.startswith("ArmorType") and armor[0].isupper():
            armor = f"ArmorType{armor}"
        if weapon and not weapon.startswith("WeaponType") and weapon[0].isupper():
            weapon = f"WeaponType{weapon}"
        if hand and not hand.startswith("HandType") and hand[0].isupper():
            hand = f"HandType{hand}"
        meta = ItemMeta(
            id=item_id,
            name=html.unescape(name),
            type=itype,
            armor_type=armor,
            weapon_type=weapon,
            hand_type=hand,
            ilvl=int(ilvl_s) if ilvl_s else None,
            phase=int(phase_s) if phase_s else None,
        )
        if item_id not in index or richer_meta(meta, index[item_id]):
            index[item_id] = meta


def regex_first(text: str, patterns: Sequence[str]) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1)
    return ""


def richer_meta(candidate: ItemMeta, existing: ItemMeta) -> bool:
    score_candidate = sum(bool(getattr(candidate, f)) for f in ("name", "type", "armor_type", "weapon_type", "hand_type", "ilvl")) + len(candidate.sources)
    score_existing = sum(bool(getattr(existing, f)) for f in ("name", "type", "armor_type", "weapon_type", "hand_type", "ilvl")) + len(existing.sources)
    return score_candidate > score_existing


def load_frontend_ep_data(mop_dir: Path) -> FrontendEPData:
    gems: dict[int, dict[str, Any]] = {}
    random_suffixes: dict[int, dict[str, Any]] = {}
    reforge_stats: dict[int, dict[str, Any]] = {}
    for path in canonical_db_paths(mop_dir):
        if not path.exists():
            continue
        data = read_json_file(path)
        if not isinstance(data, Mapping):
            continue
        for gem in data.get("gems", []) or []:
            if isinstance(gem, dict) and as_int(gem.get("id")) is not None:
                gems[int(gem["id"])] = gem
        for suffix in data.get("randomSuffixes", data.get("random_suffixes", [])) or []:
            if isinstance(suffix, dict) and as_int(suffix.get("id")) is not None:
                random_suffixes[int(suffix["id"])] = suffix
        for reforge in data.get("reforgeStats", data.get("reforge_stats", [])) or []:
            if isinstance(reforge, dict) and as_int(reforge.get("id")) is not None:
                reforge_stats[int(reforge["id"])] = reforge
    return FrontendEPData(
        gems=tuple(gems[item_id] for item_id in sorted(gems)),
        random_suffixes={item_id: random_suffixes[item_id] for item_id in sorted(random_suffixes)},
        reforge_stats=tuple(reforge_stats[item_id] for item_id in sorted(reforge_stats)),
    )


def unit_stats_weight_maps(ep_weights: Mapping[str, Any] | None) -> tuple[dict[int, float], dict[int, float]]:
    if not isinstance(ep_weights, Mapping):
        return {}, {}
    return (
        numeric_stat_map(first_present(ep_weights, "stats", "Stats") or []),
        numeric_stat_map(first_present(ep_weights, "pseudoStats", "pseudo_stats", "PseudoStats") or []),
    )


def has_nonzero_ep_weights(ep_weights: Mapping[str, Any] | None) -> bool:
    stat_weights, pseudo_weights = unit_stats_weight_maps(ep_weights)
    return any(value != 0 for value in itertools.chain(stat_weights.values(), pseudo_weights.values()))


def compute_frontend_stats_ep(
    stats: Mapping[int, float] | None,
    pseudo_stats: Mapping[int, float] | None,
    ep_weights: Mapping[str, Any],
) -> float:
    stat_weights, pseudo_weights = unit_stats_weight_maps(ep_weights)
    total = 0.0
    for idx, value in (stats or {}).items():
        total += value * stat_weights.get(idx, 0.0)
    for idx, value in (pseudo_stats or {}).items():
        total += value * pseudo_weights.get(idx, 0.0)
    return total


def frontend_scaling_option(item: Mapping[str, Any], upgrade_step: int = 0) -> Mapping[str, Any]:
    options = item.get("scalingOptions") or item.get("scaling_options") or {}
    if not isinstance(options, Mapping):
        return {}
    for key, value in options.items():
        if as_int(key) == upgrade_step and isinstance(value, Mapping):
            return value
    return {}


def frontend_item_base_stats(meta: ItemMeta, slot_idx: int) -> tuple[dict[int, float], dict[int, float], float]:
    raw = meta.raw if isinstance(meta.raw, Mapping) else {}
    scaling = frontend_scaling_option(raw, 0)
    stats = numeric_stat_map(first_present(scaling, "stats", "Stats") or first_present(raw, "stats", "Stats") or [])
    pseudo_stats: dict[int, float] = {}
    rand_prop_points = as_float(first_present(scaling, "randPropPoints", "rand_prop_points")) or as_float(
        first_present(raw, "randPropPoints", "rand_prop_points")
    ) or 0.0

    weapon_speed = as_float(first_present(raw, "weaponSpeed", "weapon_speed")) or 0.0
    damage_min = as_float(first_present(scaling, "weaponDamageMin", "weapon_damage_min") or first_present(raw, "weaponDamageMin", "weapon_damage_min"))
    damage_max = as_float(first_present(scaling, "weaponDamageMax", "weapon_damage_max") or first_present(raw, "weaponDamageMax", "weapon_damage_max"))
    if weapon_speed > 0 and damage_min is not None and damage_max is not None:
        weapon_dps = (damage_min + damage_max) / 2.0 / weapon_speed
        if slot_idx == 14:
            pseudo_idx = PSEUDO_STAT_RANGED_DPS if meta.ranged_weapon_type and meta.ranged_weapon_type != "RangedWeaponTypeUnknown" else PSEUDO_STAT_MAIN_HAND_DPS
            pseudo_stats[pseudo_idx] = pseudo_stats.get(pseudo_idx, 0.0) + weapon_dps
        elif slot_idx == 15:
            pseudo_stats[PSEUDO_STAT_OFF_HAND_DPS] = pseudo_stats.get(PSEUDO_STAT_OFF_HAND_DPS, 0.0) + weapon_dps
    return stats, pseudo_stats, rand_prop_points


def gem_color_matches_socket(gem_color: int, socket_color: int) -> bool:
    return gem_color == socket_color or gem_color in SOCKET_TO_MATCHING_GEM_COLORS.get(socket_color, set())


def gem_eligible_for_socket(gem: Mapping[str, Any], socket_color: int) -> bool:
    gem_color = as_int(first_present(gem, "color", "gemColor", "gem_color")) or 0
    if socket_color == GEM_COLOR_META:
        return gem_color == GEM_COLOR_META
    if socket_color == GEM_COLOR_COGWHEEL:
        return gem_color == GEM_COLOR_COGWHEEL
    if socket_color == GEM_COLOR_SHA_TOUCHED:
        return gem_color == GEM_COLOR_SHA_TOUCHED
    return gem_color not in SPECIAL_GEM_COLORS


def gem_matches_socket(gem: Mapping[str, Any], socket_color: int) -> bool:
    gem_color = as_int(first_present(gem, "color", "gemColor", "gem_color")) or 0
    return gem_color_matches_socket(gem_color, socket_color)


def profession_is_unknown(value: Any) -> bool:
    profession = enum_name(value, PROFESSION_NAMES, "ProfessionUnknown")
    return profession in {"", "ProfessionUnknown"}


def frontend_gem_is_unrestricted(gem: Mapping[str, Any], phase: int | None) -> bool:
    gem_phase = as_int(first_present(gem, "phase"))
    return (
        not bool(first_present(gem, "unique"))
        and profession_is_unknown(first_present(gem, "requiredProfession", "required_profession"))
        and (phase is None or gem_phase is None or gem_phase <= phase)
    )


def frontend_compute_gem_ep(gem: Mapping[str, Any], ep_weights: Mapping[str, Any]) -> float:
    ep = compute_frontend_stats_ep(numeric_stat_map(first_present(gem, "stats", "Stats") or []), {}, ep_weights)
    if bool(first_present(gem, "unique")):
        ep -= 0.01
    return ep


def frontend_gems_for_socket(ep_data: FrontendEPData, socket_color: int, phase: int | None) -> list[dict[str, Any]]:
    return [
        gem
        for gem in ep_data.gems
        if gem_eligible_for_socket(gem, socket_color) and frontend_gem_is_unrestricted(gem, phase)
    ]


def frontend_compute_item_ep(
    meta: ItemMeta,
    slot_idx: int,
    ep_weights: Mapping[str, Any],
    ep_data: FrontendEPData | None,
    phase: int | None,
) -> float | None:
    if not has_nonzero_ep_weights(ep_weights):
        return None
    ep_data = ep_data or FrontendEPData()
    raw = meta.raw if isinstance(meta.raw, Mapping) else {}
    stats, pseudo_stats, rand_prop_points = frontend_item_base_stats(meta, slot_idx)
    ep = compute_frontend_stats_ep(stats, pseudo_stats, ep_weights)

    suffix_eps: list[float] = []
    for suffix_id_raw in first_present(raw, "randomSuffixOptions", "random_suffix_options") or []:
        suffix_id = as_int(suffix_id_raw)
        suffix = ep_data.random_suffixes.get(suffix_id or 0)
        if suffix:
            suffix_eps.append(compute_frontend_stats_ep(numeric_stat_map(first_present(suffix, "stats", "Stats") or []), {}, ep_weights))
    if suffix_eps:
        ep += (max(suffix_eps) * rand_prop_points) / 10000.0

    reforge_eps: list[float] = []
    for reforge in ep_data.reforge_stats:
        from_stat = as_int(first_present(reforge, "fromStat", "from_stat"))
        to_stat = as_int(first_present(reforge, "toStat", "to_stat"))
        multiplier = as_float(first_present(reforge, "multiplier"))
        if from_stat is None or to_stat is None or multiplier is None:
            continue
        from_value = stats.get(from_stat, 0.0)
        if from_value > 0 and stats.get(to_stat, 0.0) == 0:
            reforge_stats = {
                from_stat: math.ceil(-from_value * multiplier),
                to_stat: math.floor(from_value * multiplier),
            }
            reforge_eps.append(compute_frontend_stats_ep(reforge_stats, {}, ep_weights))
    if reforge_eps:
        ep += max(reforge_eps)

    if meta.unique:
        ep -= 0.01

    socket_colors = [color for color in (as_int(raw_color) for raw_color in first_present(raw, "gemSockets", "gem_sockets") or []) if color is not None]
    if socket_colors:
        best_not_matching = 0.0
        best_matching = 0.0
        for socket_color in socket_colors:
            socket_gems = frontend_gems_for_socket(ep_data, socket_color, phase)
            if socket_gems:
                best_not_matching += max(frontend_compute_gem_ep(gem, ep_weights) for gem in socket_gems)
            matching_gems = [gem for gem in socket_gems if gem_matches_socket(gem, socket_color)]
            if matching_gems:
                best_matching += max(frontend_compute_gem_ep(gem, ep_weights) for gem in matching_gems)
        best_matching += compute_frontend_stats_ep(numeric_stat_map(first_present(raw, "socketBonus", "socket_bonus") or []), {}, ep_weights)
        ep += max(best_matching, best_not_matching)

    return ep


def frontend_ep_upgrade_specs(
    request: dict[str, Any],
    item_index: Mapping[int, ItemMeta],
    ep_weights: Mapping[str, Any] | None,
    ep_data: FrontendEPData | None,
    min_ilvl: int | None,
    max_ilvl: int | None,
    phase: int | None,
) -> list[dict[str, Any]]:
    if not has_nonzero_ep_weights(ep_weights):
        return []

    context = player_context_from_request(request)
    equipped_ids = {int(item.get("id")) for item in request_equipment_items(request) if isinstance(item, dict) and item.get("id")}
    equipped_by_slot = equipped_item_meta_by_slot(request, item_index)
    equipped_ep_by_slot: dict[int, float] = {}
    for slot_idx, meta in equipped_by_slot.items():
        ep = frontend_compute_item_ep(meta, slot_idx, ep_weights, ep_data, phase)
        if ep is not None:
            equipped_ep_by_slot[slot_idx] = ep

    scored: list[tuple[float, int, int, dict[str, Any]]] = []
    for item_id, meta in item_index.items():
        if item_id in equipped_ids:
            continue
        if min_ilvl is not None and meta.ilvl is not None and meta.ilvl < min_ilvl:
            continue
        if max_ilvl is not None and meta.ilvl is not None and meta.ilvl > max_ilvl:
            continue
        ok, _reason = is_item_in_phase(meta, phase)
        if not ok:
            continue
        ok, _reason = is_item_usable(meta, context.class_enum, set(context.professions), faction=context.faction)
        if not ok:
            continue
        slots, _reason = eligible_slot_indexes_for_item(meta, {"id": item_id}, context, equipped_by_slot)
        if not slots:
            continue

        best_delta: float | None = None
        for slot_idx in slots:
            candidate_ep = frontend_compute_item_ep(meta, slot_idx, ep_weights, ep_data, phase)
            if candidate_ep is None:
                continue
            delta = candidate_ep - equipped_ep_by_slot.get(slot_idx, 0.0)
            if delta > 0 and (best_delta is None or delta > best_delta):
                best_delta = delta
        if best_delta is not None:
            scored.append((best_delta, meta.ilvl or 0, item_id, {"id": item_id}))

    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return [spec for _delta, _ilvl, _item_id, spec in scored]


def item_name(item_id: int, item_index: Mapping[int, ItemMeta]) -> str:
    meta = item_index.get(item_id)
    return meta.name if meta and meta.name else f"Item {item_id}"


def slot_indexes_for_item(
    meta: ItemMeta | None,
    item_spec: Mapping[str, Any] | None = None,
    context: PlayerContext | None = None,
) -> list[int]:
    if meta:
        if meta.type == "ItemTypeWeapon":
            if context and context.can_dual_wield_two_hand:
                return [14, 15]
            if meta.hand_type == "HandTypeMainHand":
                return [14]
            if meta.hand_type == "HandTypeOffHand":
                return [15]
            return [14, 15]
        if meta.type == "ItemTypeRanged":
            return [14]
        keys = [meta.type, normalize_text(meta.type), normalize_key(meta.type)]
        for key in keys:
            if key in ITEM_TYPE_TO_SLOT_INDEXES:
                return ITEM_TYPE_TO_SLOT_INDEXES[key]
            # Handle enum names that were parsed without prefix.
            prefixed = f"ItemType{key[:1].upper()}{key[1:]}" if key else ""
            if prefixed in ITEM_TYPE_TO_SLOT_INDEXES:
                return ITEM_TYPE_TO_SLOT_INDEXES[prefixed]
        if meta.weapon_type:
            return [14, 15]
    if item_spec:
        for key in ("slot", "itemSlot", "item_slot", "equipSlot", "equip_slot"):
            value = item_spec.get(key)
            if value:
                text = str(value)
                if text in SLOT_TO_INDEXES:
                    return SLOT_TO_INDEXES[text]
                norm = normalize_key(text)
                for slot, idxs in SLOT_TO_INDEXES.items():
                    if normalize_key(slot).endswith(norm) or norm.endswith(normalize_key(slot)):
                        return idxs
    return []


def equipped_item_meta_by_slot(request: dict[str, Any], item_index: Mapping[int, ItemMeta]) -> dict[int, ItemMeta]:
    equipped: dict[int, ItemMeta] = {}
    for idx, item in enumerate(request_equipment_items(request)):
        if not isinstance(item, Mapping):
            continue
        item_id = as_int(item.get("id"))
        if item_id is None:
            continue
        meta = item_index.get(item_id)
        if meta is not None:
            equipped[idx] = meta
    return equipped


def item_conflict_slots(candidate: ItemMeta, equipped_by_slot: Mapping[int, ItemMeta]) -> list[int]:
    conflicts: list[int] = []
    for slot, equipped in equipped_by_slot.items():
        if candidate.unique and equipped.id == candidate.id:
            conflicts.append(slot)
            continue
        if candidate.limit_category not in (None, 0) and equipped.limit_category == candidate.limit_category:
            conflicts.append(slot)
    return conflicts


def unique_or_limit_conflict(items: Sequence[Any], item_index: Mapping[int, ItemMeta]) -> str:
    seen_unique: dict[int, str] = {}
    seen_limit: dict[int, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_id = as_int(item.get("id"))
        if item_id is None:
            continue
        meta = item_index.get(item_id)
        if meta is None:
            continue
        if meta.unique:
            if meta.id in seen_unique:
                return f"duplicate unique item {meta.name or meta.id}"
            seen_unique[meta.id] = meta.name or str(meta.id)
        if meta.limit_category not in (None, 0):
            if meta.limit_category in seen_limit:
                return f"duplicate limit category {meta.limit_category}"
            seen_limit[meta.limit_category] = meta.name or str(meta.id)
    return ""


def weapon_combo_conflict(items: Sequence[Any], item_index: Mapping[int, ItemMeta], context: PlayerContext) -> str:
    def meta_at(idx: int) -> ItemMeta | None:
        if len(items) <= idx or not isinstance(items[idx], Mapping):
            return None
        item_id = as_int(items[idx].get("id"))
        return item_index.get(item_id) if item_id is not None else None

    main_hand = meta_at(14)
    off_hand = meta_at(15)
    if main_hand is not None and off_hand is not None:
        if main_hand.hand_type == "HandTypeTwoHand" and (not context.can_dual_wield_two_hand or main_hand.weapon_type == "WeaponTypeStaff"):
            return "main-hand two-handed weapon conflicts with equipped off hand"
        if off_hand.hand_type == "HandTypeTwoHand" and (not context.can_dual_wield_two_hand or off_hand.weapon_type == "WeaponTypeStaff"):
            return "offhand two-handed weapon is not a valid weapon combo"
    return ""


def is_item_usable_in_slot(meta: ItemMeta | None, context: PlayerContext, slot_idx: int) -> tuple[bool, str]:
    ok, reason = is_item_usable(meta, context.class_enum, set(context.professions), faction=context.faction)
    if not ok:
        return ok, reason
    if meta is None:
        return False, "missing item metadata; cannot prove item is usable"
    if meta.type == "ItemTypeWeapon":
        if slot_idx == 14 and meta.hand_type == "HandTypeOffHand":
            return False, "offhand-only weapon cannot be equipped in main hand"
        if slot_idx == 15 and meta.hand_type == "HandTypeMainHand":
            return False, "main-hand-only weapon cannot be equipped in off hand"
        if slot_idx == 15 and meta.hand_type == "HandTypeTwoHand" and not context.can_dual_wield_two_hand:
            return False, "two-handed weapon cannot be equipped in off hand by this spec"
        if (
            slot_idx == 15
            and meta.hand_type in {"HandTypeOneHand", "HandTypeOffHand"}
            and meta.weapon_type not in {"WeaponTypeShield", "WeaponTypeOffHand"}
            and not context.can_dual_wield
        ):
            return False, "offhand weapon requires a dual-wield spec"
        if slot_idx == 15 and meta.hand_type == "HandTypeTwoHand" and meta.weapon_type == "WeaponTypeStaff":
            return False, "two-handed staves cannot be equipped in off hand"
    if meta.type == "ItemTypeRanged" and slot_idx != 14:
        return False, "MoP ranged weapons must use the main-hand weapon slot"
    return True, ""


def eligible_slot_indexes_for_item(
    meta: ItemMeta | None,
    item_spec: Mapping[str, Any] | None,
    context: PlayerContext,
    equipped_by_slot: Mapping[int, ItemMeta],
) -> tuple[list[int], str]:
    slots = slot_indexes_for_item(meta, item_spec, context)
    if not slots:
        return [], "no recognized equip slot"

    usable_slots: list[int] = []
    rejected: list[str] = []
    for slot in slots:
        ok, reason = is_item_usable_in_slot(meta, context, slot)
        if ok:
            usable_slots.append(slot)
        elif reason:
            rejected.append(reason)

    if meta is not None and usable_slots:
        conflicts = item_conflict_slots(meta, equipped_by_slot)
        if conflicts:
            conflict_set = set(conflicts)
            usable_slots = [slot for slot in usable_slots if conflict_set == {slot}]
            if not usable_slots:
                names = ", ".join(GEAR_INDEX_TO_SLOT.get(slot, f"slot{slot}") for slot in conflicts)
                rejected.append(f"unique/limit-category conflict with equipped {names}")

    if usable_slots:
        return usable_slots, ""
    return [], rejected[0] if rejected else "no legal equip slot for this player"


def source_text_for_item(item_id: int, item_index: Mapping[int, ItemMeta], cache_dir: Path, no_wowhead: bool = False) -> str:
    meta = item_index.get(item_id)
    if meta and meta.sources:
        local = format_sources(meta.sources)
        if local:
            return local
    if no_wowhead:
        return "Source not available in local WoWSims DB"
    return wowhead_source_lookup(item_id, cache_dir)


def format_sources(sources: Sequence[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for src in sources:
        if not isinstance(src, Mapping):
            continue
        crafted = first_mapping(src, "crafted", "Crafted")
        if crafted is not None:
            prof = enum_name(crafted.get("profession") or crafted.get("Profession"), PROFESSION_NAMES, "Crafting")
            if prof == "ProfessionUnknown":
                prof = "Crafting"
            spell = crafted.get("spell_id") or crafted.get("spellId") or crafted.get("spellID")
            chunks.append(f"Crafted: {prof}" + (f" (spell {spell})" if spell else ""))
            continue
        drop = first_mapping(src, "drop", "Drop")
        if drop is not None:
            npc = drop.get("other_name") or drop.get("otherName") or drop.get("npc_name") or drop.get("npcName") or drop.get("npc_id") or drop.get("npcId") or "drop source"
            zone = drop.get("zone_name") or drop.get("zoneName") or drop.get("zone_id") or drop.get("zoneId") or ""
            difficulty_value = first_present(drop, "difficulty")
            diff = SOURCE_DIFFICULTY_NAMES.get(as_int(difficulty_value) if as_int(difficulty_value) is not None else difficulty_value, difficulty_value or "")
            category = drop.get("category") or ""
            text = f"Drop: {npc}"
            if zone:
                text += f" in {zone}"
            extras = ", ".join(str(x) for x in (diff, category) if x)
            if extras:
                text += f" ({extras})"
            chunks.append(text)
            continue
        quest = first_mapping(src, "quest", "Quest")
        if quest is not None:
            chunks.append(f"Quest: {quest.get('name') or quest.get('id') or 'quest reward'}")
            continue
        sold = first_mapping(src, "soldBy", "sold_by", "soldBy", "SoldBy")
        if sold is not None:
            npc = sold.get("npc_name") or sold.get("npcName") or sold.get("npc_id") or sold.get("npcId") or "vendor"
            zone = sold.get("zone_name") or sold.get("zoneName") or sold.get("zone_id") or sold.get("zoneId") or ""
            chunks.append(f"Sold by: {npc}" + (f" in {zone}" if zone else ""))
            continue
        rep = first_mapping(src, "rep", "Rep")
        if rep is not None:
            faction_value = first_present(rep, "rep_faction_id", "repFactionId", "repFactionID")
            faction_id = as_int(faction_value)
            faction = REP_FACTION_NAMES.get(faction_id if faction_id is not None else -1, str(faction_value or "reputation"))
            level_value = first_present(rep, "rep_level", "repLevel")
            level_id = as_int(level_value)
            level = REP_LEVEL_NAMES.get(level_id if level_id is not None else -1, str(level_value or ""))
            side_value = first_present(rep, "faction_id", "factionId")
            side_id = as_int(side_value)
            side = FACTION_NAMES.get(side_id if side_id is not None else -1, "")
            text = f"Reputation: {faction}"
            if level and level != "Unknown":
                text += f" at {level}"
            if side and side != "Unknown":
                text += f" ({side})"
            chunks.append(text)
            continue
    # Keep output readable.
    seen: list[str] = []
    for chunk in chunks:
        if chunk not in seen:
            seen.append(chunk)
    return "; ".join(seen[:5])


def first_mapping(src: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
    for key in keys:
        value = src.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def wowhead_source_lookup(item_id: int, cache_dir: Path) -> str:
    cache_path = cache_dir / "wowhead_sources.json"
    cache: dict[str, Any] = {}
    if cache_path.exists():
        with contextlib.suppress(Exception):
            cache = read_json_file(cache_path)
    key = str(item_id)
    if key in cache:
        return str(cache[key])
    url = WOWHEAD_MOP_CLASSIC_ITEM_URL.format(item_id=item_id)
    try:
        page = http_text(url, timeout=30)
        title = html.unescape(regex_first(page, [r"<title>(.*?)</title>"]))
        title = re.sub(r"\s+-\s+Item\s+-.*$", "", title).strip()
        source = extract_wowhead_source(page)
        if source:
            value = source
        elif title:
            value = f"Wowhead MoP Classic: {title} ({url})"
        else:
            value = f"Wowhead MoP Classic item page ({url})"
    except urllib.error.HTTPError as exc:
        value = f"Wowhead lookup failed: HTTP {exc.code} ({url})"
    except Exception as exc:  # noqa: BLE001
        value = f"Wowhead lookup failed: {exc} ({url})"
    cache[key] = value
    with contextlib.suppress(Exception):
        write_json_file(cache_path, cache)
    # Be polite if many source fallbacks are needed.
    time.sleep(0.15)
    return value


def extract_wowhead_source(page: str) -> str:
    text = html.unescape(page)
    prose_patterns = [
        ("Dropped by", r"\bIt is looted from\s+([^.<]+)"),
        ("Sold by", r"\bIt is sold by\s+([^.<]+)"),
        ("Created by", r"\bIt is crafted by\s+([^.<]+)"),
        ("Created by", r"\bIt is created by\s+([^.<]+)"),
        ("Reward from", r"\bIt is a quest reward from\s+([^.<]+)"),
        ("Contained in", r"\bIt is contained in\s+([^.<]+)"),
    ]
    for label, pattern in prose_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            name = re.sub(r"\s+", " ", re.sub(r"<.*?>", "", match.group(1))).strip()
            if name:
                return f"{label}: {name}"

    plain_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text))
    rep_match = re.search(r"Requires\s+(.+?)\s+[-–]\s+([A-Za-z]+)", plain_text, flags=re.IGNORECASE)
    if rep_match:
        faction = re.sub(r"\s+", " ", rep_match.group(1)).strip()
        level = rep_match.group(2).strip()
        if faction and level:
            return f"Reputation: {faction} at {level}"

    # Best effort against Wowhead's generated JS/listview markup.
    labels = [
        ("Dropped by", r"id:\s*['\"]dropped-by['\"].{0,12000}"),
        ("Contained in", r"id:\s*['\"]contained-in-item['\"].{0,12000}"),
        ("Sold by", r"id:\s*['\"]sold-by['\"].{0,12000}"),
        ("Created by", r"id:\s*['\"]created-by['\"].{0,12000}"),
        ("Reward from", r"id:\s*['\"]reward-from-quest['\"].{0,12000}"),
    ]
    for label, pattern in labels:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        chunk = m.group(0)
        names = []
        for name_pat in (r"name_enus:\s*['\"]([^'\"]+)", r"name:\s*['\"]([^'\"]+)", r"\"name\":\s*\"([^\"]+)\""):
            for name in re.findall(name_pat, chunk, flags=re.IGNORECASE):
                clean = re.sub(r"<.*?>", "", html.unescape(name)).strip()
                if clean and clean not in names:
                    names.append(clean)
                if len(names) >= 5:
                    break
            if names:
                break
        if names:
            source = f"{label}: " + ", ".join(names[:5])
            if label == "Dropped by":
                difficulties = wowhead_listview_difficulties(chunk)
                if difficulties:
                    source += " (" + ", ".join(difficulties) + ")"
            return source
    return ""


def wowhead_listview_difficulties(chunk: str) -> list[str]:
    difficulties: list[str] = []
    for modes in re.findall(r"modes\s*:\s*\{([^}]*)\}", chunk, flags=re.IGNORECASE | re.DOTALL):
        for mode_id_s in re.findall(r"(\d+)\s*:", modes):
            mode_id = as_int(mode_id_s)
            if mode_id is None:
                continue
            name = SOURCE_DIFFICULTY_NAMES.get(mode_id)
            if name and name != "Unknown" and name not in difficulties:
                difficulties.append(name)
    return difficulties


# ----------------------------- Usability / candidates -------------------------


def is_item_in_phase(meta: ItemMeta | None, phase: int | None) -> tuple[bool, str]:
    if meta is None or phase is None or phase <= 0 or meta.phase is None:
        return True, ""
    if meta.phase > phase:
        return False, f"phase {meta.phase} is above requested phase {phase}"
    return True, ""


def is_item_usable(
    meta: ItemMeta | None,
    class_enum: str,
    professions: set[str],
    faction: str = "",
) -> tuple[bool, str]:
    if meta is None:
        return False, "missing item metadata; cannot prove item is usable"
    if meta.class_allowlist:
        allow = {str(x) for x in meta.class_allowlist}
        if class_enum not in allow and str(class_enum).replace("Class", "") not in allow:
            return False, f"class restricted ({', '.join(meta.class_allowlist)})"
    if meta.required_profession:
        req = str(meta.required_profession)
        if req not in {"ProfessionUnknown", "0", ""} and req not in professions:
            return False, f"requires profession {req}"
    if meta.faction_restriction and faction:
        if meta.faction_restriction != faction:
            return False, f"restricted to {meta.faction_restriction}; player faction is {faction}"
    # Armor restriction only for armor slots, not cloak/neck/rings/trinkets/weapons.
    if meta.armor_type and meta.type in {"ItemTypeHead", "ItemTypeShoulder", "ItemTypeChest", "ItemTypeWrist", "ItemTypeHands", "ItemTypeWaist", "ItemTypeLegs", "ItemTypeFeet"}:
        max_armor = CLASS_ARMOR_MAX.get(class_enum)
        if max_armor and meta.armor_type in ARMOR_ORDER:
            if ARMOR_ORDER.index(meta.armor_type) > ARMOR_ORDER.index(max_armor):
                return False, f"armor type {meta.armor_type} is above class max {max_armor}"
    if meta.type == "ItemTypeWeapon":
        eligibility = CLASS_WEAPON_ELIGIBILITY.get(class_enum)
        if eligibility is None:
            return False, f"no weapon usability rules for {class_enum}"
        if not meta.weapon_type:
            return False, "weapon type missing; cannot prove weapon usability"
        if meta.weapon_type not in eligibility:
            return False, f"weapon type {meta.weapon_type} not usable by {class_enum}"
        if meta.hand_type == "HandTypeTwoHand" and not eligibility[meta.weapon_type]:
            return False, f"two-handed {meta.weapon_type} not usable by {class_enum}"
    if meta.type == "ItemTypeRanged":
        allowed_ranged = CLASS_RANGED_WEAPONS.get(class_enum)
        if allowed_ranged is None:
            return False, f"no ranged weapon usability rules for {class_enum}"
        if not meta.ranged_weapon_type:
            return False, "ranged weapon type missing; cannot prove ranged weapon usability"
        if meta.ranged_weapon_type not in allowed_ranged:
            return False, f"ranged weapon type {meta.ranged_weapon_type} not usable by {class_enum}"
    return True, ""


def player_class_and_professions(request: dict[str, Any]) -> tuple[str, set[str]]:
    context = player_context_from_request(request)
    return context.class_enum, set(context.professions)


def player_spec_enum(player: Mapping[str, Any]) -> str:
    for field, spec_enum in SPEC_ENUM_BY_PLAYER_FIELD.items():
        if field in player or lower_camel_from_snake(field) in player:
            return spec_enum
    value = player.get("spec") or player.get("spec_enum") or player.get("specEnum")
    if isinstance(value, str) and value.startswith("Spec"):
        return value
    return "SpecUnknown"


def faction_from_race(race_enum: str) -> str:
    if race_enum in ALLIANCE_RACES:
        return "Alliance"
    if race_enum in HORDE_RACES:
        return "Horde"
    return ""


def player_context_from_request(request: dict[str, Any]) -> PlayerContext:
    player = get_request_player(request)
    class_enum = str(player.get("class") or player.get("class_") or "ClassUnknown")
    race_enum = str(player.get("race") or "RaceUnknown")
    faction = str(player.get("faction") or "") or faction_from_race(race_enum)
    professions = frozenset(str(x) for x in (player.get("profession1") or "", player.get("profession2") or "") if x)
    return PlayerContext(
        class_enum=class_enum,
        spec_enum=player_spec_enum(player),
        race_enum=race_enum,
        faction=faction,
        professions=professions,
    )


def request_phase(request: Mapping[str, Any]) -> int | None:
    for key in ("sim_options", "simOptions", "settings"):
        value = request.get(key)
        if isinstance(value, Mapping):
            phase = as_int(value.get("phase"))
            if phase is not None:
                return phase
    return None


def effective_phase(args: argparse.Namespace, request: Mapping[str, Any]) -> int | None:
    return args.phase if args.phase is not None else request_phase(request)


def extract_equipment_items_from_payload(kind: str, payload: Any) -> list[dict[str, Any]]:
    if kind == "equipment_spec":
        return normalize_equipment_spec(payload)["items"]
    if kind == "wse_character":
        return normalize_wse_character_gear(payload.get("gear"))["items"]
    if kind in {"raid_request", "individual_settings"}:
        req = payload if kind == "raid_request" else convert_individual_settings_to_raid_request(payload)
        return [normalize_item_spec(x) for x in request_equipment_items(req)]
    return []


def prompt_bag_payload(args: argparse.Namespace, wowsimcli: Path, out_dir: Path) -> tuple[str, Any] | tuple[str, None]:
    blob = load_blob_or_path(args.bag_export) if args.bag_export else ""
    if not blob and not args.no_prompt:
        blob = prompt_blob(
            "For batch/upgrade mode, paste the WSE bag-items export from the addon UI, or press Enter to skip bag candidates.",
            allow_blank=True,
        )
    if not blob.strip():
        return "none", None
    return load_user_payload(blob, wowsimcli, out_dir)


def build_candidate_specs(
    request: dict[str, Any],
    bag_kind: str,
    bag_payload: Any,
    item_index: Mapping[int, ItemMeta],
    source_mode: str,
    max_db_candidates: int,
    min_ilvl: int | None,
    max_ilvl: int | None,
    phase: int | None = None,
    auto_frontend_ep_upgrades: bool = False,
    frontend_ep_weights: Mapping[str, Any] | None = None,
    frontend_ep_data: FrontendEPData | None = None,
) -> tuple[list[dict[str, Any]], list[SkippedItem]]:
    equipped_ids = {int(item.get("id")) for item in request_equipment_items(request) if isinstance(item, dict) and item.get("id")}
    context = player_context_from_request(request)
    equipped_by_slot = equipped_item_meta_by_slot(request, item_index)
    candidates: dict[int, dict[str, Any]] = {}
    skipped: list[SkippedItem] = []

    def skip(item_id: int, reason: str) -> None:
        skipped.append(SkippedItem(item_id=item_id, item_name=item_name(item_id, item_index), reason=reason))

    if source_mode in {"bag", "both"} and bag_payload is not None:
        for spec in extract_equipment_items_from_payload(bag_kind, bag_payload):
            item_id = int(spec.get("id") or 0)
            if not item_id or item_id in equipped_ids:
                continue
            candidates[item_id] = spec

    if auto_frontend_ep_upgrades and frontend_ep_weights:
        ep_specs = frontend_ep_upgrade_specs(
            request,
            item_index,
            frontend_ep_weights,
            frontend_ep_data,
            min_ilvl=min_ilvl,
            max_ilvl=max_ilvl,
            phase=phase,
        )
        added = 0
        for spec in ep_specs:
            item_id = int(spec.get("id") or 0)
            if item_id and item_id not in equipped_ids and item_id not in candidates:
                candidates[item_id] = spec
                added += 1
        if added:
            info(f"Automatically included {added:,} frontend EP upgrade candidate items.")

    if source_mode in {"db", "both"}:
        db_items: list[tuple[int, ItemMeta]] = list(item_index.items())
        # Prefer higher-quality/high-ilvl items, but keep deterministic ordering.
        db_items.sort(key=lambda kv: ((kv[1].ilvl or 0), kv[1].id), reverse=True)
        added = 0
        for item_id, meta in db_items:
            if item_id in equipped_ids or item_id in candidates:
                continue
            if min_ilvl is not None and meta.ilvl is not None and meta.ilvl < min_ilvl:
                skip(item_id, f"item level {meta.ilvl} is below minimum {min_ilvl}")
                continue
            if max_ilvl is not None and meta.ilvl is not None and meta.ilvl > max_ilvl:
                skip(item_id, f"item level {meta.ilvl} is above maximum {max_ilvl}")
                continue
            ok, reason = is_item_in_phase(meta, phase)
            if not ok:
                skip(item_id, reason)
                continue
            ok, _reason = is_item_usable(meta, context.class_enum, set(context.professions), faction=context.faction)
            if not ok:
                skip(item_id, _reason)
                continue
            slots, reason = eligible_slot_indexes_for_item(meta, {"id": item_id}, context, equipped_by_slot)
            if not slots:
                skip(item_id, reason)
                continue
            candidates[item_id] = {"id": item_id}
            added += 1
            if max_db_candidates and added >= max_db_candidates:
                break

    usable: list[dict[str, Any]] = []
    for item_id, spec in candidates.items():
        meta = item_index.get(item_id)
        ok, reason = is_item_in_phase(meta, phase)
        if not ok:
            skip(item_id, reason)
            continue
        ok, reason = is_item_usable(meta, context.class_enum, set(context.professions), faction=context.faction)
        if not ok:
            skip(item_id, reason)
            continue
        slots, reason = eligible_slot_indexes_for_item(meta, spec, context, equipped_by_slot)
        if not slots:
            skip(item_id, reason)
            continue
        usable.append(normalize_item_spec(spec))
    if skipped:
        first = "; ".join(f"{item.item_id}: {item.reason}" for item in skipped[:5])
        info(f"Skipped {len(skipped)} candidate items that were not safely usable/mappable. First few: {first}")
    return usable, skipped


def replacement_requests_for_item(base_request: dict[str, Any], item_spec: dict[str, Any], item_index: Mapping[int, ItemMeta]) -> list[tuple[str, dict[str, Any], int, str]]:
    item_id = int(item_spec.get("id") or 0)
    meta = item_index.get(item_id)
    context = player_context_from_request(base_request)
    indexes, _reason = eligible_slot_indexes_for_item(meta, item_spec, context, equipped_item_meta_by_slot(base_request, item_index))
    if not indexes:
        return []
    out: list[tuple[str, dict[str, Any], int, str]] = []
    for idx in indexes:
        req = copy.deepcopy(base_request)
        items = request_equipment_items(req)
        while len(items) <= idx:
            items.append({})
        items[idx] = normalize_item_spec(item_spec)
        # Two-handed weapons should clear offhand for the single-swap trial.
        if idx == 14 and meta and meta.hand_type == "HandTypeTwoHand" and len(items) > 15:
            items[15] = {}
        slot = GEAR_INDEX_TO_SLOT.get(idx, f"slot{idx}")
        label = f"{item_name(item_id, item_index)}@{slot}"
        out.append((label, req, idx, slot))
    return out


def combination_requests(
    base_request: dict[str, Any],
    candidates: Sequence[dict[str, Any]],
    item_index: Mapping[int, ItemMeta],
    max_combinations: int,
) -> list[tuple[str, dict[str, Any], tuple[int, ...]]]:
    # Choices per slot: None/current or one of the candidates for that slot.
    context = player_context_from_request(base_request)
    equipped_by_slot = equipped_item_meta_by_slot(base_request, item_index)
    by_slot: dict[int, list[dict[str, Any]]] = {idx: [] for idx in range(16)}
    for spec in candidates:
        meta = item_index.get(int(spec.get("id") or 0))
        indexes, _reason = eligible_slot_indexes_for_item(meta, spec, context, equipped_by_slot)
        for idx in indexes:
            if 0 <= idx <= 15:
                by_slot[idx].append(spec)
    slots = [idx for idx, specs in by_slot.items() if specs]
    choices = [[None] + by_slot[idx] for idx in slots]
    total = math.prod(len(x) for x in choices) if choices else 0
    if total == 0:
        return []
    if max_combinations and total > max_combinations:
        warn(f"Batch combinations would create {total:,} sims; capping to first {max_combinations:,}. Use --max-batch-combinations 0 for no cap.")
    out: list[tuple[str, dict[str, Any], tuple[int, ...]]] = []
    for combo_num, combo in enumerate(itertools.product(*choices), 1):
        if max_combinations and combo_num > max_combinations:
            break
        if all(x is None for x in combo):
            continue
        req = copy.deepcopy(base_request)
        items = request_equipment_items(req)
        labels: list[str] = []
        used_ids: list[int] = []
        for idx, spec in zip(slots, combo):
            if spec is None:
                continue
            while len(items) <= idx:
                items.append({})
            normalized = normalize_item_spec(spec)
            items[idx] = normalized
            item_id = int(normalized.get("id") or 0)
            meta = item_index.get(item_id)
            if idx == 14 and meta and meta.hand_type == "HandTypeTwoHand" and len(items) > 15:
                items[15] = {}
            used_ids.append(item_id)
            labels.append(f"{item_name(item_id, item_index)}@{GEAR_INDEX_TO_SLOT.get(idx, idx)}")
        conflict = unique_or_limit_conflict(items, item_index) or weapon_combo_conflict(items, item_index, context)
        if conflict:
            continue
        label = "; ".join(labels)
        out.append((label, req, tuple(used_ids)))
    return out


# ----------------------------- Sim execution ---------------------------------


def extract_dps(result: Any) -> float | None:
    distribution = find_dps_distribution(result)
    return distribution_avg(distribution)


def find_dps_distribution(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    for raid_key in ("raidMetrics", "raid_metrics"):
        raid = result.get(raid_key)
        if isinstance(raid, dict):
            if distribution_avg(raid.get("dps")) is not None:
                return raid.get("dps")
            for party_key in ("parties",):
                parties = raid.get(party_key)
                if isinstance(parties, list) and parties:
                    party_dps = parties[0].get("dps") if isinstance(parties[0], dict) else None
                    if distribution_avg(party_dps) is not None:
                        return party_dps
    # Fallback: recursively find first dps.avg.
    return recursive_find_dps_distribution(result)


def distribution_avg(value: Any) -> float | None:
    if isinstance(value, dict):
        avg = value.get("avg")
        if isinstance(avg, (int, float)):
            return float(avg)
    return None


def distribution_stdev(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("stdev", "stddev", "stdDev", "std_dev"):
            stdev = value.get(key)
            if isinstance(stdev, (int, float)):
                return float(stdev)
    return None


def extract_dps_stdev(result: Any) -> float | None:
    return distribution_stdev(find_dps_distribution(result))


def extract_iterations_done(result: Any) -> int | None:
    if not isinstance(result, Mapping):
        return None
    for key in ("iterationsDone", "iterations_done"):
        value = as_int(result.get(key))
        if value is not None:
            return value
    return None


def confidence_95_half_width(stdev: float | None, iterations: int | None) -> float | None:
    if stdev is None or iterations is None or iterations <= 0:
        return None
    return 1.96 * stdev / math.sqrt(iterations)


def recursive_find_dps_distribution(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "dps" in obj:
            if distribution_avg(obj["dps"]) is not None:
                return obj["dps"]
        for value in obj.values():
            found = recursive_find_dps_distribution(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = recursive_find_dps_distribution(value)
            if found is not None:
                return found
    return None


def sim_result_from_json(
    label: str,
    request_path: Path,
    result_path: Path,
    digest: str,
    seconds: float,
) -> SimRunResult:
    try:
        result = read_json_file(result_path)
    except Exception as exc:  # noqa: BLE001
        return SimRunResult(
            label=label,
            request_path=request_path,
            result_path=result_path,
            request_hash=digest,
            dps=None,
            error=f"Could not parse result JSON: {exc}",
            seconds=seconds,
        )
    if isinstance(result, dict):
        err = result.get("error") or result.get("Error")
        if isinstance(err, dict) and err.get("message"):
            return SimRunResult(label=label, request_path=request_path, result_path=result_path, request_hash=digest, dps=None, error=str(err.get("message")), seconds=seconds)
    dps = extract_dps(result)
    if dps is None:
        return SimRunResult(label=label, request_path=request_path, result_path=result_path, request_hash=digest, dps=None, error="Result did not contain a DPS average", seconds=seconds)
    dps_stdev = extract_dps_stdev(result)
    iterations_done = extract_iterations_done(result)
    return SimRunResult(
        label=label,
        request_path=request_path,
        result_path=result_path,
        request_hash=digest,
        dps=dps,
        dps_stdev=dps_stdev,
        dps_ci95=confidence_95_half_width(dps_stdev, iterations_done),
        iterations_done=iterations_done,
        seconds=seconds,
    )


def run_single_sim(
    wowsimcli: Path,
    request: dict[str, Any],
    run_dir: Path,
    label: str,
    timeout: int,
    verbose: bool = False,
    resume: bool = False,
) -> SimRunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = sim_cache_paths(run_dir, label, request)
    ensure_sim_cache_dirs(paths)
    write_json_file(paths.request_path, request)
    if resume and paths.result_path.exists():
        cached = sim_result_from_json(label, paths.request_path, paths.result_path, paths.digest, seconds=0.0)
        if cached.dps is not None or cached.error:
            return cached
    start = time.perf_counter()
    cmd = [str(wowsimcli), "sim", "--infile", str(paths.request_path), "--outfile", str(paths.result_path)]
    if verbose:
        cmd.append("--verbose")
    proc = run_cmd(cmd, check=False, timeout=timeout)
    seconds = time.perf_counter() - start
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"wowsimcli exited {proc.returncode}"
        return SimRunResult(label=label, request_path=paths.request_path, result_path=paths.result_path, request_hash=paths.digest, dps=None, error=err, seconds=seconds)
    return sim_result_from_json(label, paths.request_path, paths.result_path, paths.digest, seconds=seconds)


def run_many_sims(
    wowsimcli: Path,
    jobs: Sequence[tuple[str, dict[str, Any], dict[str, Any]]],
    run_dir: Path,
    timeout: int,
    workers: int,
    verbose: bool = False,
    resume: bool = False,
) -> list[SimRunResult]:
    results: list[SimRunResult] = []
    total = len(jobs)
    if total == 0:
        return []
    safe_workers = max(1, min(int(workers or 1), total, os.cpu_count() or 1))
    if safe_workers != workers:
        warn(f"Adjusted worker count from {workers} to {safe_workers} for safe local process limits.")
    workers = safe_workers
    info(f"Running {total:,} sims with {workers} worker(s)")

    def one(job: tuple[str, dict[str, Any], dict[str, Any]]) -> SimRunResult:
        label, req, meta = job
        result = run_single_sim(wowsimcli, req, run_dir, label, timeout=timeout, verbose=verbose, resume=resume)
        for key, value in meta.items():
            setattr(result, key, value)
        return result

    completed = 0
    if workers <= 1:
        for job in jobs:
            results.append(one(job))
            completed += 1
            if completed % 10 == 0 or completed == total:
                info(f"Completed {completed:,}/{total:,} sims")
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(one, job): job for job in jobs}
        for fut in concurrent.futures.as_completed(future_map):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                label = future_map[fut][0]
                results.append(SimRunResult(label=label, request_path=run_dir / "missing", result_path=run_dir / "missing", dps=None, error=str(exc)))
            completed += 1
            if completed % 10 == 0 or completed == total:
                info(f"Completed {completed:,}/{total:,} sims")
    return results


# ----------------------------- Reports ---------------------------------------


def write_results_csv(path: Path, results: Sequence[SimRunResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "label",
        "item_id",
        "item_name",
        "slot",
        "item_ilvl",
        "item_phase",
        "item_quality",
        "dps",
        "dps_stdev",
        "dps_ci95",
        "dps_delta",
        "percent_change",
        "source",
        "optimization_status",
        "optimization_details",
        "error",
        "seconds",
        "iterations_done",
        "request_hash",
        "request_path",
        "result_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "label": r.label,
                    "item_id": r.item_id or "",
                    "item_name": r.item_name,
                    "slot": r.slot,
                    "item_ilvl": r.item_ilvl or "",
                    "item_phase": r.item_phase or "",
                    "item_quality": r.item_quality,
                    "dps": f"{r.dps:.3f}" if r.dps is not None else "",
                    "dps_stdev": f"{r.dps_stdev:.3f}" if r.dps_stdev is not None else "",
                    "dps_ci95": f"{r.dps_ci95:.3f}" if r.dps_ci95 is not None else "",
                    "dps_delta": f"{r.dps_delta:.3f}" if r.dps_delta is not None else "",
                    "percent_change": f"{r.percent_change:.4f}" if r.percent_change is not None else "",
                    "source": r.source,
                    "optimization_status": r.optimization_status,
                    "optimization_details": r.optimization_details,
                    "error": r.error,
                    "seconds": f"{r.seconds:.2f}",
                    "iterations_done": r.iterations_done or "",
                    "request_hash": r.request_hash,
                    "request_path": str(r.request_path),
                    "result_path": str(r.result_path),
                }
            )


def write_skipped_items_csv(path: Path, skipped: Sequence[SkippedItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["item_id", "item_name", "reason"])
        writer.writeheader()
        for item in skipped:
            writer.writerow(
                {
                    "item_id": item.item_id,
                    "item_name": item.item_name,
                    "reason": item.reason,
                }
            )


def write_upgrade_report(
    path: Path,
    baseline: SimRunResult,
    upgrades: Sequence[SimRunResult],
    threshold: float,
    all_results_csv: Path,
    skipped: Sequence[SkippedItem] = (),
    skipped_csv: Path | None = None,
) -> None:
    winners = [r for r in upgrades if r.dps is not None and (r.percent_change or 0) >= threshold]
    winners.sort(key=lambda r: (r.percent_change or -999, r.dps or -1), reverse=True)
    lines = [
        "# WoWSims MoP Upgrade Sim Report",
        "",
        f"Generated UTC: {utc_stamp()}",
        f"Baseline DPS: {baseline.dps:.2f}" if baseline.dps is not None else f"Baseline failed: {baseline.error}",
        f"Baseline DPS stdev: {baseline.dps_stdev:.2f}" if baseline.dps_stdev is not None else "Baseline DPS stdev: not reported",
        f"Baseline DPS 95% CI half-width: {baseline.dps_ci95:.2f}" if baseline.dps_ci95 is not None else "Baseline DPS 95% CI half-width: not reported",
        f"Iterations done: {baseline.iterations_done}" if baseline.iterations_done is not None else "Iterations done: not reported",
        f"Upgrade threshold: {threshold:.2f}%",
        f"All results CSV: `{all_results_csv.name}`",
        "",
        "## Upgrades meeting threshold",
        "",
    ]
    if not winners:
        lines.append("No candidate reached the configured threshold.")
    else:
        lines.append("| Rank | Item | Slot | Ilvl | Phase | Quality | DPS | Delta | Upgrade % | Source | Optimization | Details |")
        lines.append("|---:|---|---|---:|---:|---|---:|---:|---:|---|---|---|")
        for i, r in enumerate(winners, 1):
            lines.append(
                "| {rank} | {item} | {slot} | {ilvl} | {phase} | {quality} | {dps:.2f} | {delta:.2f} | {pct:.2f}% | {source} | {opt} | {details} |".format(
                    rank=i,
                    item=escape_md(r.item_name or r.label),
                    slot=escape_md(r.slot),
                    ilvl=r.item_ilvl or "",
                    phase=r.item_phase or "",
                    quality=escape_md(r.item_quality),
                    dps=r.dps or 0,
                    delta=r.dps_delta or 0,
                    pct=r.percent_change or 0,
                    source=escape_md(r.source or "Unknown"),
                    opt=escape_md(r.optimization_status or "Not annotated"),
                    details=escape_md(r.optimization_details or "none selected"),
                )
            )
    lines.extend(
        [
            "",
            "## Skipped Items",
            "",
        ]
    )
    if skipped:
        lines.append(f"Skipped items: {len(skipped)}")
        if skipped_csv is not None:
            lines.append(f"Skipped item CSV: `{skipped_csv.name}`")
        lines.append("")
        lines.append("| Item | Reason |")
        lines.append("|---|---|")
        for item in skipped[:50]:
            lines.append(f"| {escape_md(item.item_name or item.item_id)} | {escape_md(item.reason)} |")
        if len(skipped) > 50:
            lines.append(f"| ... | {len(skipped) - 50} additional skipped items in `{skipped_csv.name if skipped_csv else 'skipped item CSV'}` |")
    else:
        lines.append("No candidate items were skipped after candidate-source filtering.")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Each row is a single item replacement into the baseline request.",
            "- If `optimization` says `not fully optimized`, the sim used the item spec provided/exported plus any script-selected slot mutation, but did not run a proven upstream gem/enchant/reforge optimizer.",
            "- Keep enough iterations high enough for stable percent differences before making expensive loot/crafting decisions.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_normal_report(path: Path, baseline: SimRunResult, request_path: Path) -> None:
    lines = [
        "# WoWSims MoP Normal Sim Report",
        "",
        f"Generated UTC: {utc_stamp()}",
        f"Request: `{request_path.name}`",
        f"Result: `{baseline.result_path.name}`",
        "",
    ]
    if baseline.dps is not None:
        lines.append(f"Baseline DPS: **{baseline.dps:.2f}**")
        if baseline.dps_stdev is not None:
            lines.append(f"Baseline DPS stdev: **{baseline.dps_stdev:.2f}**")
        if baseline.dps_ci95 is not None:
            lines.append(f"Baseline DPS 95% CI half-width: **{baseline.dps_ci95:.2f}**")
        if baseline.iterations_done is not None:
            lines.append(f"Iterations done: **{baseline.iterations_done}**")
    else:
        lines.append(f"Sim failed: `{baseline.error}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


# ----------------------------- Modes -----------------------------------------


def run_normal(args: argparse.Namespace, wowsimcli: Path, request: dict[str, Any], out_dir: Path) -> None:
    result = run_single_sim(wowsimcli, request, out_dir / "runs", "baseline", timeout=args.timeout, verbose=args.verbose_cli, resume=args.resume)
    write_normal_report(out_dir / "normal_report.md", result, result.request_path)
    if result.dps is not None:
        info(f"Baseline DPS: {result.dps:.2f}")
    else:
        die(f"Sim failed: {result.error}")


def run_upgrade(
    args: argparse.Namespace,
    paths: RunnerPaths,
    wowsimcli: Path,
    request: dict[str, Any],
    out_dir: Path,
    ep_weights_stats: Mapping[str, Any] | None = None,
    ep_weights_source: str = "",
) -> None:
    item_index = load_item_index(paths.mop, paths.cache, refresh=args.refresh_item_index)
    bag_kind, bag_payload = prompt_bag_payload(args, wowsimcli, out_dir)
    phase = effective_phase(args, request)
    frontend_ep_data: FrontendEPData | None = None
    auto_frontend_ep_upgrades = False
    if getattr(args, "no_auto_ep_upgrades", False):
        info("Automatic frontend EP upgrade inclusion is disabled by --no-auto-ep-upgrades.")
    elif ep_weights_stats and has_nonzero_ep_weights(ep_weights_stats):
        frontend_ep_data = load_frontend_ep_data(paths.mop)
        auto_frontend_ep_upgrades = True
        source = f" from {ep_weights_source}" if ep_weights_source else ""
        info(f"Automatic frontend EP upgrade inclusion is enabled{source}.")
    else:
        warn(
            "Skipping automatic frontend EP upgrade inclusion because no nonzero EP weights were available. "
            "Provide a WoWSims share link or IndividualSimSettings JSON exported with UI settings to enable it."
        )
    candidates, skipped = build_candidate_specs(
        request,
        bag_kind,
        bag_payload,
        item_index,
        source_mode=args.upgrade_candidate_source,
        max_db_candidates=args.max_db_candidates,
        min_ilvl=args.min_ilvl,
        max_ilvl=args.max_ilvl,
        phase=phase,
        auto_frontend_ep_upgrades=auto_frontend_ep_upgrades,
        frontend_ep_weights=ep_weights_stats,
        frontend_ep_data=frontend_ep_data,
    )
    skipped_csv = out_dir / "skipped_items.csv" if skipped else None
    if skipped_csv is not None:
        write_skipped_items_csv(skipped_csv, skipped)
    if not candidates:
        detail = f" Skipped item reasons were written to {skipped_csv}." if skipped_csv is not None else ""
        die(f"No usable upgrade candidates were found. Provide a WSE bag export or use --upgrade-candidate-source db/both.{detail}")
    baseline = run_single_sim(wowsimcli, request, out_dir / "runs", "baseline", timeout=args.timeout, verbose=args.verbose_cli, resume=args.resume)
    if baseline.dps is None:
        die(f"Baseline sim failed, cannot compare upgrades. Error:\n{baseline.error}")
    info(f"Baseline DPS: {baseline.dps:.2f}")
    jobs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for spec in candidates:
        item_id = int(spec.get("id") or 0)
        for label, req, slot_idx, slot_name in replacement_requests_for_item(request, spec, item_index):
            meta = item_index.get(item_id)
            source = source_text_for_item(item_id, item_index, paths.cache, no_wowhead=args.no_wowhead)
            jobs.append(
                (
                    label,
                    req,
                    {
                        "item_id": item_id,
                        "item_name": item_name(item_id, item_index),
                        "slot_index": slot_idx,
                        "slot": slot_name,
                        "source": source,
                        "item_ilvl": meta.ilvl if meta else None,
                        "item_phase": meta.phase if meta else None,
                        "item_quality": meta.quality if meta else "",
                        "optimization_status": optimizer_status(args, meta),
                        "optimization_details": item_spec_mod_summary(spec),
                    },
                )
            )
    if args.max_upgrade_sims and len(jobs) > args.max_upgrade_sims:
        warn(f"Capping upgrade sim jobs from {len(jobs):,} to {args.max_upgrade_sims:,}. Use --max-upgrade-sims 0 for no cap.")
        jobs = jobs[: args.max_upgrade_sims]
    results = run_many_sims(wowsimcli, jobs, out_dir / "runs", timeout=args.timeout, workers=args.workers, verbose=args.verbose_cli, resume=args.resume)
    for r in results:
        if r.dps is not None:
            r.dps_delta = r.dps - baseline.dps
            r.percent_change = ((r.dps - baseline.dps) / baseline.dps) * 100.0
    results.sort(key=lambda r: (r.percent_change if r.percent_change is not None else -99999, r.dps or -1), reverse=True)
    csv_path = out_dir / "upgrade_results.csv"
    write_results_csv(csv_path, results)
    write_upgrade_report(out_dir / "upgrade_report.md", baseline, results, args.upgrade_threshold, csv_path, skipped=skipped, skipped_csv=skipped_csv)
    winners = [r for r in results if r.percent_change is not None and r.percent_change >= args.upgrade_threshold]
    if winners:
        print()
        print(f"Top upgrades >= {args.upgrade_threshold:.2f}%:")
        for r in winners[:20]:
            print(f"  {r.percent_change:7.2f}%  {r.dps:10.2f} DPS  {r.item_name} @ {r.slot} - {r.source}")
    else:
        info(f"No upgrades reached >= {args.upgrade_threshold:.2f}%.")
    info(f"Wrote upgrade report: {out_dir / 'upgrade_report.md'}")


def optimizer_status(args: argparse.Namespace, meta: ItemMeta | None) -> str:
    if args.require_optimizer:
        die(
            "--require-optimizer was set, but this script did not find a proven upstream MoP CLI optimizer. "
            "Use the Codex goal included with this bundle to wire the UI/import/reforge optimizer into a local command."
        )
    if args.optimizer_strategy == "none":
        return "not optimized; raw candidate item spec"
    if args.optimizer_strategy == "preserve-exported":
        return "not fully optimized; preserved exported item gems/enchant/reforge when present"
    if args.optimizer_strategy == "copy-current-slot-mods":
        return "not fully optimized; copied current-slot mods only when adapter supports it"
    return "not fully optimized; upstream optimizer adapter not detected"


def run_batch(args: argparse.Namespace, paths: RunnerPaths, wowsimcli: Path, request: dict[str, Any], out_dir: Path) -> None:
    item_index = load_item_index(paths.mop, paths.cache, refresh=args.refresh_item_index)
    bag_kind, bag_payload = prompt_bag_payload(args, wowsimcli, out_dir)
    phase = effective_phase(args, request)
    candidates, skipped = build_candidate_specs(
        request,
        bag_kind,
        bag_payload,
        item_index,
        source_mode="bag" if args.upgrade_candidate_source == "db" else args.upgrade_candidate_source,
        max_db_candidates=args.max_db_candidates,
        min_ilvl=args.min_ilvl,
        max_ilvl=args.max_ilvl,
        phase=phase,
    )
    skipped_csv = out_dir / "skipped_items.csv" if skipped else None
    if skipped_csv is not None:
        write_skipped_items_csv(skipped_csv, skipped)
    if not candidates:
        detail = f" Skipped item reasons were written to {skipped_csv}." if skipped_csv is not None else ""
        die(f"No usable batch candidates were found. Provide a WSE bag export.{detail}")
    baseline = run_single_sim(wowsimcli, request, out_dir / "runs", "baseline", timeout=args.timeout, verbose=args.verbose_cli, resume=args.resume)
    if baseline.dps is None:
        die(f"Baseline sim failed, cannot compare batch results. Error:\n{baseline.error}")
    combos = combination_requests(request, candidates, item_index, max_combinations=args.max_batch_combinations)
    jobs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for label, req, used_ids in combos:
        jobs.append((label, req, {"optimization_status": optimizer_status(args, None)}))
    results = run_many_sims(wowsimcli, jobs, out_dir / "runs", timeout=args.timeout, workers=args.workers, verbose=args.verbose_cli, resume=args.resume)
    for r in results:
        if r.dps is not None:
            r.dps_delta = r.dps - baseline.dps
            r.percent_change = ((r.dps - baseline.dps) / baseline.dps) * 100.0
    results.sort(key=lambda r: (r.percent_change if r.percent_change is not None else -99999, r.dps or -1), reverse=True)
    csv_path = out_dir / "batch_results.csv"
    write_results_csv(csv_path, results)
    lines = [
        "# WoWSims MoP Batch Sim Report",
        "",
        f"Generated UTC: {utc_stamp()}",
        f"Baseline DPS: {baseline.dps:.2f}",
        f"Candidates: {len(candidates)}",
        f"Combinations simulated: {len(results)}",
        f"All results CSV: `{csv_path.name}`",
        f"Skipped items CSV: `{skipped_csv.name}`" if skipped_csv is not None else "Skipped items: 0",
        "",
        "## Top results",
        "",
        "| Rank | Combination | DPS | Change % | Optimization |",
        "|---:|---|---:|---:|---|",
    ]
    for i, r in enumerate(results[:50], 1):
        lines.append(f"| {i} | {escape_md(r.label)} | {r.dps or 0:.2f} | {r.percent_change or 0:.2f}% | {escape_md(r.optimization_status)} |")
    (out_dir / "batch_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    info(f"Wrote batch report: {out_dir / 'batch_report.md'}")


# ----------------------------- CLI / main ------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local WoWSims MoP sims from WSE exports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--workdir", type=Path, default=None, help="Folder where mop/exporter/results/cache should live. Default: script folder.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Use this result folder instead of creating a timestamped folder.")
    parser.add_argument("--resume", action="store_true", help="Reuse cached sim result JSONs in --output-dir when request hashes match.")
    parser.add_argument("--skip-update", action="store_true", help="Do not fetch/pull existing repos.")
    parser.add_argument("--force-cli-download", action="store_true", help="Force re-download/rebuild of wowsimcli.")
    parser.add_argument("--mode", choices=["normal", "batch", "upgrade"], default=None, help="Simulation mode. Prompts if omitted.")
    parser.add_argument("--export", default="", help="WSE export / wowsims link / RaidSimRequest JSON / path. Use @path to force file.")
    parser.add_argument("--template", default="", help="Optional WoWSims share link / IndividualSimSettings / RaidSimRequest used as WSE import template.")
    parser.add_argument("--bag-export", default="", help="Optional WSE bag export / EquipmentSpec JSON / path for batch/upgrade candidates.")
    parser.add_argument("--iterations", type=int, default=10000, help="Iterations for generated requests.")
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)), help="Parallel wowsimcli processes for batch/upgrade runs.")
    parser.add_argument("--timeout", type=int, default=900, help="Timeout per sim process, seconds.")
    parser.add_argument("--upgrade-threshold", type=float, default=5.0, help="Minimum DPS percent increase for upgrade report winners.")
    parser.add_argument("--upgrade-candidate-source", choices=["bag", "db", "both"], default="bag", help="Where upgrade candidates come from.")
    parser.add_argument("--max-db-candidates", type=int, default=250, help="Max DB candidates when --upgrade-candidate-source includes db. 0 = no cap.")
    parser.add_argument("--max-upgrade-sims", type=int, default=0, help="Max single-swap upgrade sim jobs. 0 = no cap.")
    parser.add_argument("--no-auto-ep-upgrades", action="store_true", help="Disable automatic frontend EP upgrade inclusion in upgrade mode.")
    parser.add_argument("--max-batch-combinations", type=int, default=10000, help="Max batch combinations. 0 = no cap.")
    parser.add_argument("--min-ilvl", type=int, default=None, help="Minimum item level for DB candidates.")
    parser.add_argument("--max-ilvl", type=int, default=None, help="Maximum item level for DB candidates.")
    parser.add_argument("--phase", type=int, default=None, help="Maximum MoP content phase for candidate items. Defaults from template settings when present.")
    parser.add_argument("--optimizer-strategy", choices=["preserve-exported", "copy-current-slot-mods", "none"], default="preserve-exported", help="How to handle gem/enchant/reforge without upstream optimizer adapter.")
    parser.add_argument("--require-optimizer", action="store_true", help="Abort unless a proven upstream optimizer adapter is available.")
    parser.add_argument("--refresh-item-index", action="store_true", help="Re-scan WoWSims repo for item metadata.")
    parser.add_argument("--no-wowhead", action="store_true", help="Do not query Wowhead when source data is missing locally.")
    parser.add_argument("--no-prompt", action="store_true", help="Noninteractive mode: fail instead of prompting for missing input.")
    parser.add_argument("--verbose-cli", action="store_true", help="Pass --verbose to wowsimcli sim.")
    return parser.parse_args(argv)


def choose_mode(current: str | None, no_prompt: bool = False) -> str:
    if current:
        return current
    if no_prompt:
        die("--mode is required with --no-prompt.")
    print()
    print("Choose sim mode:")
    print("  1) normal  - currently equipped gear only")
    print("  2) batch   - equipped gear plus combinations of usable bag items")
    print("  3) upgrade - single-item replacement trials and >= threshold upgrade report")
    choice = input("Mode [normal/batch/upgrade or 1/2/3]: ").strip().lower()
    return {"1": "normal", "2": "batch", "3": "upgrade", "n": "normal", "b": "batch", "u": "upgrade"}.get(choice, choice or "normal")


def maybe_prompt_template(kind: str, args: argparse.Namespace) -> str:
    template = load_blob_or_path(args.template) if args.template else ""
    if template or args.no_prompt:
        return template
    if kind == "wse_character":
        print()
        print(
            "Recommended: provide a WoWSims share link or RaidSimRequest JSON from the UI for this same spec so buffs, APL, encounter, and sim settings are preserved."
        )
        answer = input("Provide template/share link now? [Y/n]: ").strip().lower()
        if answer in {"", "y", "yes"}:
            return prompt_blob("Paste the WoWSims share link / IndividualSimSettings / RaidSimRequest JSON template.", allow_blank=False)
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    script_dir = Path(__file__).resolve().parent
    root = (args.workdir or script_dir).expanduser().resolve()
    paths = RunnerPaths(
        root=root,
        mop=root / "mop",
        exporter=root / "exporter",
        cache=root / ".wowsims_mop_runner" / "cache",
        bin_dir=root / ".wowsims_mop_runner" / "bin",
        results=root / "wowsims_mop_results",
    )
    paths.cache.mkdir(parents=True, exist_ok=True)
    paths.results.mkdir(parents=True, exist_ok=True)

    ensure_repo(MOP_REPO_URL, paths.mop, skip_update=args.skip_update)
    ensure_repo(EXPORTER_REPO_URL, paths.exporter, skip_update=args.skip_update)
    wowsimcli = ensure_wowsimcli(paths, force_download=args.force_cli_download)
    glyph_spell_to_item = load_glyph_spell_to_item_map(paths.mop)
    if not glyph_spell_to_item:
        warn("Could not load WoWSims glyph spell-to-item mapping; WSE glyph imports may be incomplete.")

    out_dir = args.output_dir.expanduser().resolve() if args.output_dir else paths.results / utc_stamp()
    out_dir.mkdir(parents=True, exist_ok=True)

    export_blob = load_blob_or_path(args.export) if args.export else ""
    if not export_blob and not args.no_prompt:
        export_blob = prompt_blob("Paste your WSE export from /wse export, a WoWSims share link, RaidSimRequest JSON, or @file.")
    if not export_blob:
        die("No export input provided. Use --export @path or run interactively.")

    kind, payload = load_user_payload(export_blob, wowsimcli, out_dir)
    info(f"Detected input type: {kind}")
    if kind == "unknown":
        die("Could not recognize input. Expected WSE character JSON, EquipmentSpec JSON, IndividualSimSettings, RaidSimRequest, or WoWSims share link.")

    mode = choose_mode(args.mode, no_prompt=args.no_prompt)
    if mode not in {"normal", "batch", "upgrade"}:
        die(f"Unknown mode {mode!r}")

    template_blob = maybe_prompt_template(kind, args)
    input_context = build_input_context_from_payload(
        kind,
        payload,
        wowsimcli,
        out_dir,
        iterations=args.iterations,
        template_blob=template_blob,
        glyph_spell_to_item=glyph_spell_to_item,
        mop_dir=paths.mop,
    )
    request = input_context.request
    write_json_file(out_dir / "effective_raid_sim_request.json", request)
    if input_context.ep_weights_stats:
        write_json_file(out_dir / "effective_ep_weights_stats.json", input_context.ep_weights_stats)

    if mode == "normal":
        run_normal(args, wowsimcli, request, out_dir)
    elif mode == "batch":
        run_batch(args, paths, wowsimcli, request, out_dir)
    elif mode == "upgrade":
        run_upgrade(
            args,
            paths,
            wowsimcli,
            request,
            out_dir,
            ep_weights_stats=input_context.ep_weights_stats,
            ep_weights_source=input_context.ep_weights_source,
        )
    info(f"Output folder: {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
