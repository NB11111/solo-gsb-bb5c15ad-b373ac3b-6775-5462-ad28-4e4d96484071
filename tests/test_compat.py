import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="compat-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _base_fields(extra=None, remove=(), override=None):
    fields = [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ]
    fields = [f for f in fields if f["name"] not in remove]
    if override:
        override = dict(override)
        target = override.pop("__target__")
        fields = [dict(f, **override) if f["name"] == target else f for f in fields]
    if extra:
        fields.extend(extra)
    return fields


def _register(device_type, version, *, fields=None, checksum="crc16-ccitt", active=True):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": fields if fields is not None else _base_fields(),
    }
    if checksum is not None:
        payload["checksum"] = {"field": "crc", "algorithm": checksum}
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text
    if not active:
        resp = client.post(
            f"/api/v1/definitions/{device_type}/{version}/deactivate"
        )
        assert resp.status_code == 200
    return payload


def _compat(device_type, old, new):
    return client.post(
        f"/api/v1/definitions/{device_type}/compatibility",
        json={"old_version": old, "new_version": new},
    )


def _by_status(report):
    return {b["status"]: b for b in report["branches"]}


def test_compatibility_identical_versions_compatible():
    _register("compat_same", "1.0.0")
    _register("compat_same", "2.0.0")
    resp = _compat("compat_same", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["verdict"] == "compatible"
    assert data["breaking_branch_count"] == 0
    # msg_type nibble: 3 distinct branches (eq 1, eq 2, complement)
    assert data["combination_count"] == 3
    assert data["distinct_branch_count"] == 3
    assert data["reachable_old_branches"] == 3
    # every branch compatible -> offsets/lengths must be present in both layouts
    for branch in data["branches"]:
        old_layout = branch["old_layout"]
        new_layout = branch["new_layout"]
        assert old_layout["total_bits"] == new_layout["total_bits"]
        assert all("bit_offset" in f for f in old_layout["fields"])


def test_compatibility_self_compare():
    _register("compat_self", "1.0.0")
    resp = _compat("compat_self", "1.0.0", "1.0.0")
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "compatible"


def test_compatibility_field_width_change_is_breaking():
    _register("compat_width", "1.0.0")
    widened = _base_fields(
        override={"__target__": "temperature", "bit_length": 32}
    )
    _register("compat_width", "2.0.0", fields=widened)
    resp = _compat("compat_width", "1.0.0", "2.0.0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "breaking"
    breaking = _by_status(data)["breaking"]
    assert breaking["condition_values"] == [{"msg_type": 1}]
    kinds = {d["kind"] for d in breaking["differences"]}
    assert "field_bit_length_changed" in kinds
    assert "field_bit_offset_changed" in kinds
    assert "total_length_changed" in kinds
    width_diff = next(d for d in breaking["differences"] if d["kind"] == "field_bit_length_changed")
    assert width_diff["field"] == "temperature"
    assert (width_diff["old"], width_diff["new"]) == (16, 32)
    assert breaking["old_layout"]["total_bits"] == 48
    assert breaking["new_layout"]["total_bits"] == 64


def test_compatibility_removed_branch_field_is_breaking():
    _register("compat_rm", "1.0.0")
    _register("compat_rm", "2.0.0", fields=_base_fields(remove=("humidity",)))
    resp = _compat("compat_rm", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["verdict"] == "breaking"
    breaking = _by_status(data)["breaking"]
    assert breaking["condition_values"] == [{"msg_type": 2}]
    kinds = {d["kind"]: d for d in breaking["differences"]}
    assert "field_removed" in kinds
    assert kinds["field_removed"]["field"] == "humidity"
    # old msg_type=2 branch: header+nibbles+humidity = 24 bits, crc at 24;
    # new branch: only header+nibbles = 16 bits, crc at 16
    offset_diff = kinds["field_bit_offset_changed"]
    assert offset_diff["field"] == "crc"
    assert (offset_diff["old"], offset_diff["new"]) == (24, 16)
    assert breaking["old_layout"]["total_bits"] == 40
    assert breaking["new_layout"]["total_bits"] == 32


def test_compatibility_checksum_algorithm_change_is_breaking():
    _register("compat_crc", "1.0.0", checksum="crc16-ccitt")
    _register("compat_crc", "2.0.0", checksum="crc16-modbus")
    resp = _compat("compat_crc", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["verdict"] == "breaking"
    for branch in data["branches"]:
        kinds = {d["kind"] for d in branch["differences"]}
        assert "checksum_algorithm_changed" in kinds
    diff = data["branches"][0]["differences"][-1]
    assert (diff["old"], diff["new"]) == ("crc16-ccitt", "crc16-modbus")


def test_compatibility_new_only_branch_not_breaking():
    # old: b only present when a==1, so c (cond on b) is unreachable otherwise
    old = [
        {"name": "a", "type": "uint", "bit_length": 8},
        {"name": "b", "type": "uint", "bit_length": 8,
         "condition": {"field": "a", "op": "eq", "value": 1}},
        {"name": "c", "type": "uint", "bit_length": 8,
         "condition": {"field": "b", "op": "eq", "value": 1}},
    ]
    # new: b unconditional -> c is reachable for a!=1 too (new-only branches)
    new = [
        {"name": "a", "type": "uint", "bit_length": 8},
        {"name": "b", "type": "uint", "bit_length": 8},
        {"name": "c", "type": "uint", "bit_length": 8,
         "condition": {"field": "b", "op": "eq", "value": 1}},
    ]
    _register("compat_newonly", "1.0.0", fields=old, checksum=None)
    _register("compat_newonly", "2.0.0", fields=new, checksum=None)
    resp = _compat("compat_newonly", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["verdict"] == "compatible"
    statuses = {b["status"] for b in data["branches"]}
    assert "new_only" in statuses
    assert "breaking" not in statuses
    new_only = _by_status(data)["new_only"]
    assert new_only["old_layout"] is None
    assert new_only["new_layout"] is not None


def test_compatibility_404_missing_versions():
    _register("compat_404", "1.0.0")
    resp = _compat("compat_404", "9.9.9", "1.0.0")
    assert resp.status_code == 404
    assert "9.9.9" in resp.json()["detail"]
    resp = _compat("compat_404", "1.0.0", "8.8.8")
    assert resp.status_code == 404
    assert "8.8.8" in resp.json()["detail"]
    resp = _compat("ghost_device", "1.0.0", "2.0.0")
    assert resp.status_code == 404


def test_compatibility_inactive_versions_allowed():
    _register("compat_inactive", "1.0.0", active=False)
    _register("compat_inactive", "2.0.0", fields=_base_fields(remove=("humidity",)))
    resp = _compat("compat_inactive", "1.0.0", "2.0.0")
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "breaking"


def test_compatibility_too_many_combinations_422():
    # 9 independent uint8 control fields each gated by eq -> 2^9 = 512
    def fields(n):
        out = [{"name": "h", "type": "uint", "bit_length": 8}]
        for i in range(n):
            out.append({"name": f"c{i}", "type": "uint", "bit_length": 8})
            out.append(
                {
                    "name": f"o{i}",
                    "type": "uint",
                    "bit_length": 8,
                    "condition": {"field": f"c{i}", "op": "eq", "value": 1},
                }
            )
        return out

    _register("compat_big", "1.0.0", fields=fields(9), checksum=None)
    _register("compat_big", "2.0.0", fields=fields(9), checksum=None)
    resp = _compat("compat_big", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "512" in resp.json()["detail"]
    assert "256" in resp.json()["detail"]


def test_compatibility_exactly_256_combinations_allowed():
    def fields(n):
        out = [{"name": "h", "type": "uint", "bit_length": 8}]
        for i in range(n):
            out.append({"name": f"c{i}", "type": "uint", "bit_length": 8})
            out.append(
                {
                    "name": f"o{i}",
                    "type": "uint",
                    "bit_length": 8,
                    "condition": {"field": f"c{i}", "op": "eq", "value": 1},
                }
            )
        return out

    _register("compat_256", "1.0.0", fields=fields(8), checksum=None)
    _register("compat_256", "2.0.0", fields=fields(8), checksum=None)
    resp = _compat("compat_256", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    assert resp.json()["combination_count"] == 256


def test_compatibility_signed_to_unsigned_controlling_field():
    # int4 control in old (domain -8..7), uint4 in new (0..15): the union
    # domain must keep the negative class so the old default branch is checked.
    old = [
        {"name": "t", "type": "int", "bit_length": 4},
        {
            "name": "frost",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "t", "op": "lt", "value": 0},
        },
    ]
    new = [
        {"name": "t", "type": "uint", "bit_length": 4},
        {
            "name": "frost",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "t", "op": "lt", "value": 0},
        },
    ]
    _register("compat_sign", "1.0.0", fields=old, checksum=None)
    _register("compat_sign", "2.0.0", fields=new, checksum=None)
    resp = _compat("compat_sign", "1.0.0", "2.0.0")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "breaking"
    neg_branches = [
        b for b in data["branches"]
        if b["old_layout"] is not None and b["status"] == "breaking"
        and any(v["t"] < 0 for v in b["condition_values"])
    ]
    assert neg_branches  # negative class was not silently dropped


def test_compatibility_in_operator_partition():
    fields = [
        {"name": "t", "type": "uint", "bit_length": 8},
        {
            "name": "ext",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "t", "op": "in", "value": [1, 2]},
        },
    ]
    _register("compat_in", "1.0.0", fields=fields, checksum=None)
    _register("compat_in", "2.0.0", fields=fields, checksum=None)
    resp = _compat("compat_in", "1.0.0", "2.0.0")
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "compatible"
    # in {1,2} partitions the domain into {1,2} and the complement -> 2 classes
    assert resp.json()["combination_count"] == 2
