from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceDefinition:
    resource_id: str
    aliases: tuple[str, ...]
    blocks: tuple[str, ...]
    drop_item: str
    minimum_tool_tier: int = 0
    prerequisites: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipeDefinition:
    output: str
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    station: str = "crafting"


_RESOURCES = (
    ResourceDefinition(
        resource_id="oak_log",
        aliases=("原木", "木头", "橡木原木", "木材", "log", "logs"),
        blocks=(
            "oak_log",
            "birch_log",
            "spruce_log",
            "jungle_log",
            "acacia_log",
            "dark_oak_log",
            "mangrove_log",
            "cherry_log",
        ),
        drop_item="oak_log",
    ),
    ResourceDefinition(
        resource_id="cobblestone",
        aliases=("圆石", "石头", "cobblestone"),
        blocks=("stone",),
        drop_item="cobblestone",
        minimum_tool_tier=1,
        prerequisites=("wooden_pickaxe",),
    ),
    ResourceDefinition(
        resource_id="coal",
        aliases=("煤", "煤炭", "coal"),
        blocks=("coal_ore", "deepslate_coal_ore"),
        drop_item="coal",
        minimum_tool_tier=1,
        prerequisites=("wooden_pickaxe",),
    ),
    ResourceDefinition(
        resource_id="raw_iron",
        aliases=("铁", "铁矿", "铁原矿", "raw iron", "iron"),
        blocks=("iron_ore", "deepslate_iron_ore"),
        drop_item="raw_iron",
        minimum_tool_tier=2,
        prerequisites=("stone_pickaxe",),
    ),
    ResourceDefinition(
        resource_id="diamond",
        aliases=("钻石", "diamond", "diamonds"),
        blocks=("diamond_ore", "deepslate_diamond_ore"),
        drop_item="diamond",
        minimum_tool_tier=3,
        prerequisites=("iron_pickaxe",),
    ),
    ResourceDefinition(
        resource_id="stone_pickaxe",
        aliases=("石镐", "stone pickaxe", "stone_pickaxe"),
        blocks=(),
        drop_item="stone_pickaxe",
        prerequisites=("oak_log", "cobblestone"),
    ),
)

_RECIPES = (
    RecipeDefinition("oak_planks", 4, (("oak_log", 1),)),
    RecipeDefinition("stick", 4, (("oak_planks", 2),)),
    RecipeDefinition(
        "wooden_pickaxe", 1, (("oak_planks", 3), ("stick", 2))
    ),
    RecipeDefinition(
        "stone_pickaxe", 1, (("cobblestone", 3), ("stick", 2))
    ),
    RecipeDefinition("furnace", 1, (("cobblestone", 8),)),
    RecipeDefinition(
        "iron_ingot", 1, (("raw_iron", 1), ("coal", 1)), station="furnace"
    ),
    RecipeDefinition(
        "iron_pickaxe", 1, (("iron_ingot", 3), ("stick", 2))
    ),
)


class DomainCatalog:
    def __init__(self) -> None:
        self._by_id = {entry.resource_id: entry for entry in _RESOURCES}
        self._by_alias: dict[str, ResourceDefinition] = {}
        for entry in _RESOURCES:
            self._by_alias[entry.resource_id.lower()] = entry
            for alias in entry.aliases:
                self._by_alias[alias.lower()] = entry
        self._recipes = {entry.output: entry for entry in _RECIPES}

    def resource(self, resource_id: str) -> ResourceDefinition:
        try:
            return self._by_id[str(resource_id).strip().lower()]
        except KeyError as exc:
            raise ValueError(f"unknown resource: {resource_id}") from exc

    def resolve(self, value: str) -> ResourceDefinition | None:
        return self._by_alias.get(str(value).strip().lower())

    def resources(self) -> tuple[ResourceDefinition, ...]:
        return tuple(self._by_id.values())

    def recipe(self, output: str) -> RecipeDefinition:
        try:
            return self._recipes[str(output).strip().lower()]
        except KeyError as exc:
            raise ValueError(f"unknown recipe: {output}") from exc

    def recipes(self) -> tuple[RecipeDefinition, ...]:
        return tuple(self._recipes.values())


CATALOG = DomainCatalog()
