"""Japanese names of the reviewed synthetic towns' roads and places.

Gemini reads a visitor's report against these names (a person says "the bridge", not
"river_bridge"). They mirror roadNames/nodeNames in apps/web/static/map-view.js; a
test keeps the two in step.
"""

from __future__ import annotations

from packages.domain.models import Scenario
from packages.domain.report import RoadLabel

ROAD_NAMES = {
    "road_a": "道路A",
    "road_b1": "道路B1",
    "road_b2": "道路B2",
    "north_exit": "北地区の出口",
    "flooded_lane": "川沿いの道（浸水）",
    "river_path": "川沿いから橋への道",
    "river_bridge": "橋",
    "east_direct": "東地区から中央への道",
    "east_lane": "東地区から坂下への道",
    "school_access": "小学校への道",
    "hall_access": "公民館への道",
    "center_to_hill": "中央から坂下への道",
    "hill_road": "高台への坂道",
}
NODE_NAMES = {
    "home": "住宅地",
    "junction": "分岐点",
    "shelter_a_node": "避難所A",
    "shelter_b_node": "避難所B",
    "school": "小学校",
    "hall": "公民館",
    "hill_park": "高台公園",
    "north_homes": "北地区",
    "river_homes": "川沿い",
    "east_homes": "東地区",
    "bridge": "橋のたもと",
    "center": "中央",
    "hill_foot": "坂下",
}


def road_labels(scenario: Scenario) -> tuple[RoadLabel, ...]:
    """Every road of a reviewed scenario with its Japanese name; KeyError if one is missing."""
    return tuple(
        RoadLabel(
            id=road.id,
            name=ROAD_NAMES[road.id],
            between=(NODE_NAMES[road.source], NODE_NAMES[road.target]),
        )
        for road in scenario.roads
    )
