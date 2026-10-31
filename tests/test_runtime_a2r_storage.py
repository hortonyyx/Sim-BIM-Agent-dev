"""A2-R: lossless shared storage, report reopening and conservative GLM errors."""

import asyncio
import copy
import gzip
import hashlib
import json

import pytest

from src.agent.runtime_behaviour import _captured, load_behaviour, write_behaviour_report
from src.agent_runtime.store import json_bytes
from test_agent_runtime import MESSAGES, response, runtime


def test_checkpoint_tree_reuses_unchanged_data_and_restores_each_version(tmp_path):
    engine = runtime(tmp_path, [])
    state = {"history": [{"id": i, "text": str(i) + "甲乙丙丁" * 500} for i in range(20)],
             "config": {"guide": "keep all requirements" * 1000}, "count": 0}
    with engine.store as store:
        roots, snapshots = [], []
        for i in range(10):
            state["count"] = i
            state["history"].append({"id": i + 20, "text": str(i) + "new observation" * 100})
            snapshots.append(copy.deepcopy(state))
            roots.append(store.put_json_tree(state))
        used = sum(p.stat().st_size for p in (store.directory / "blobs").iterdir())
        assert used < sum(len(json_bytes(s)) for s in snapshots) / 3
        for ref, expected in zip(roots, snapshots, strict=True):
            assert store.get_json_tree(ref) == expected
        assert store.get_json_tree(store.put_json(state)) == state  # old format
        # Returning to earlier small state must not reuse an aliased, mutated
        # literal from the in-memory node cache.
        state["small"] = {"revision": 1}
        first = store.put_json_tree(state)
        state["small"]["revision"] = 2
        store.put_json_tree(state)
        state["small"] = {"revision": 1}
        assert store.get_json_tree(store.put_json_tree(state)) == store.get_json_tree(first)


@pytest.mark.parametrize("damage", ["node", "missing", "wire_hash", "capture_hash"])
def test_shared_json_corruption_never_silently_falls_back(tmp_path, damage):
    engine = runtime(tmp_path, [])
    with engine.store as store:
        capture = store.capture({"text": "whole conversation" * 3000})
        assert capture.kind == "json_references"
        root = json.loads(store.get_bytes(capture.blob))
        node = root["tree"][1][0][1][1]
        path = store.directory / node["uri"]
        if damage == "node":
            path.write_bytes(b"corrupted")
        elif damage == "missing":
            path.unlink()
        elif damage == "wire_hash":
            root["wire_sha256"] = "0" * 64
            capture = capture.model_copy(update={"blob": store.put_bytes(json_bytes(root), capture.blob.media_type)})
        else:
            capture = capture.model_copy(update={"wire_sha256": "0" * 64})
        with pytest.raises((ValueError, FileNotFoundError)):
            store.capture_bytes(capture)


def test_json_values_cannot_impersonate_storage_nodes_and_references_do_not_alias(tmp_path):
    engine = runtime(tmp_path, [])
    child = {"kind": "ref", "tree": ["ref", {"uri": "outside"}], "text": "unicode \"\\\n雪" * 500}
    value = {"a/b~c": [child, child], "booleans": [True, False, 0, 1, None]}
    with engine.store as store:
        capture = store.capture(value)
        assert store.capture_bytes(capture) == json_bytes(value)
        assert _captured(capture, store.directory) == value
        restored = store.resolve(capture)
        restored["a/b~c"][0]["text"] = "changed"
        assert restored["a/b~c"][1]["text"] == child["text"]


@pytest.mark.parametrize("input_form", ["directory", "file", "old_expanded_record"])
def test_runtime_behaviour_reference_reopens_full_record_and_detects_changes(tmp_path, input_form):
    engine = runtime(tmp_path, [response(("view", "view", {})), response(text="Done")])
    with engine.store as store:
        assert asyncio.run(engine.run(MESSAGES))["status"] == "completed"
        report = store.directory / "behaviour"
        expected = load_behaviour(store.directory)
        write_behaviour_report(store.directory, report)
        with gzip.open(report / "record.json.gz", "rt") as stream:
            manifest = json.load(stream)
        assert manifest["source_format"] == "event_envelope_reference"
        assert (report / "record.json.gz").stat().st_size < 1024
        actual = load_behaviour(report)
        assert actual["summary"] == expected["summary"]
        assert actual["invocations"] == expected["invocations"]
        if input_form == "old_expanded_record":
            with gzip.open(report / "record.json.gz", "wt") as stream:
                json.dump(expected, stream)
            with pytest.raises(ValueError, match="verified native event-log reference"):
                load_behaviour(report)
            with pytest.raises(ValueError, match="verified native event-log reference"):
                write_behaviour_report(report, store.directory / "rejected-export")
            return
        source = report if input_form == "directory" else report / "record.json.gz"
        exported = store.directory / "reexported"
        write_behaviour_report(source, exported)
        reopened = load_behaviour(exported)
        assert reopened["invocations"] == expected["invocations"]
        assert reopened["requests"] == expected["requests"]
        if input_form != "old_expanded_record":
            assert (exported / "record.json.gz").stat().st_size < 1024
        # Regenerate the reference before checking journal mutation, including
        # the case that deliberately overwrote it with an old expanded record.
        write_behaviour_report(store.directory, report)
        # No writer changes in this fixture: changing the referenced journal
        # must make the previously saved report explicitly invalid.
        store.path.write_bytes(store.path.read_bytes() + b"\n")
        with pytest.raises(ValueError, match="event-log hash"):
            load_behaviour(report)
