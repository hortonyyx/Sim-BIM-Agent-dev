import asyncio
import json
from unittest.mock import patch

import src.agent.runtime_roles.parameters as parameter_module
from src.agent.runtime_roles.parameters import normalize_stringified_parameters
from src.agent.runtime_roles.readers import ELEVATION_READER_TOOL_NAMES, ReaderTools
from src.agent.runtime_roles.submission import OPERATION_SCHEMA


def obj(properties):
    return {"type": "object", "properties": properties, "additionalProperties": False}


def test_declared_containers_and_booleans_decode_recursively_without_guessing_scalars():
    schema = obj({
        "rows": {"type": "array", "items": obj({
            "enabled": {"type": "boolean"},
            "payload": obj({"values": {"type": "array", "items": {"type": "number"}}}),
            "raw_json": {"type": "string"},
            "count": {"type": "number"},
        })},
        "twice": {"type": "array", "items": {"type": "number"}},
        "tuple": {"type": "array", "prefixItems": [
            {"type": "boolean"}, {"type": "object"},
        ], "items": False},
    })
    arguments = {
        "rows": json.dumps([{
            "enabled": "true",
            "payload": json.dumps({"values": "[1,2]"}),
            "raw_json": '{"keep":"string"}',
            "count": "7",
        }]),
        "twice": json.dumps("[3,4]"),
        "tuple": '["false","{\\"source\\":\\"reader\\"}"]',
    }

    normalized = normalize_stringified_parameters(arguments, schema)

    assert normalized["rows"] == [{
        "enabled": True,
        "payload": {"values": [1, 2]},
        "raw_json": '{"keep":"string"}',
        "count": "7",
    }]
    assert normalized["twice"] == '"[3,4]"'
    assert normalized["tuple"] == [False, {"source": "reader"}]
    assert arguments["rows"] != normalized["rows"]


def test_unions_select_the_matching_type_and_operation_branch_only():
    height_schema = obj({"match_id": {"oneOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}, "minItems": 1},
    ]}})
    assert normalize_stringified_parameters(
        {"match_id": '["north","south"]'}, height_schema
    )["match_id"] == ["north", "south"]
    assert normalize_stringified_parameters(
        {"match_id": "north"}, height_schema
    )["match_id"] == "north"

    trial_schema = obj({"operations": {"type": "array", "items": OPERATION_SCHEMA}})
    normalized = normalize_stringified_parameters({"operations": json.dumps([{
        "op": "update",
        "reason": "measured correction",
        "source_refs": '["plan.png: wall"]',
        "bbox": "[1,2,3,4]",
        "collection": "openings",
        "id": "W1",
        "changes": '{"p1":[10,20]}',
        "value": '{"must":"stay in the other branch"}',
    }])}, trial_schema)
    operation = normalized["operations"][0]
    assert operation["source_refs"] == ["plan.png: wall"]
    assert operation["bbox"] == [1, 2, 3, 4]
    assert operation["changes"] == {"p1": [10, 20]}
    assert operation["value"] == '{"must":"stay in the other branch"}'


def test_overlapping_union_schema_parses_each_node_once_and_never_double_decodes():
    array_schema = {"type": "array", "items": {"type": "number"}}
    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string"}, "value": array_schema},
        "oneOf": [{
            "type": "object",
            "properties": {"kind": {"const": "array"}, "value": array_schema},
            "required": ["kind", "value"],
        }],
    }
    twice_encoded = json.dumps(json.dumps([1, 2]))
    original_loads = json.loads
    seen = []

    def counted_loads(value):
        seen.append(value)
        return original_loads(value)

    with patch.object(parameter_module.json, "loads", side_effect=counted_loads):
        normalized = normalize_stringified_parameters(
            {"kind": "array", "value": twice_encoded}, schema
        )

    assert normalized["value"] == twice_encoded
    assert seen == [twice_encoded]


class Frozen:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        tools = []
        for name in ELEVATION_READER_TOOL_NAMES:
            properties = {"name": {"type": "string"}}
            if name == "view_image":
                properties["coordinate_grid"] = {"type": "boolean"}
            tools.append({"name": name, "description": name,
                          "inputSchema": obj(properties)})
        return tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "{}"}],
                "structuredContent": {}}

    def repeatability(self, name):
        return "read_only"

    def snapshot_state(self):
        return {}

    def artifacts(self):
        return []

    def image_origins(self, result):
        return {}


def test_reader_call_uses_its_actual_catalog_schema_before_invocation():
    async def scenario():
        frozen = Frozen()
        reader = ReaderTools(
            frozen, role_id="elevation_reader", image_name="facade.png"
        )
        await reader.call_tool("view_image", {
            "name": "facade.png", "coordinate_grid": "false",
        })
        assert frozen.calls == [("view_image", {
            "name": "facade.png", "coordinate_grid": False,
        })]

    asyncio.run(scenario())
