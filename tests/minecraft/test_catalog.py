import pytest

from applications.minecraft.catalog import CATALOG


def test_five_resource_technology_tree_and_deepslate_variants():
    resources = {entry.resource_id: entry for entry in CATALOG.resources()}
    assert {"oak_log", "cobblestone", "coal", "raw_iron", "diamond"} <= resources.keys()
    assert resources["diamond"].minimum_tool_tier == 3
    assert "diamond_ore" in resources["diamond"].blocks
    assert "deepslate_diamond_ore" in resources["diamond"].blocks
    assert resources["raw_iron"].prerequisites == ("stone_pickaxe",)


def test_survival_recipes_are_canonical_catalog_data():
    assert dict(CATALOG.recipe("iron_pickaxe").ingredients) == {
        "iron_ingot": 3,
        "stick": 2,
    }
    assert CATALOG.recipe("iron_ingot").station == "furnace"
    with pytest.raises(ValueError):
        CATALOG.recipe("command_block")
