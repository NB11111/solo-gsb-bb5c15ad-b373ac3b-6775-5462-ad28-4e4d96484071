import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.checksum import crc16_ccitt  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

SENSOR_DEF = {
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
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
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def make_message(body: bytes) -> str:
    crc = crc16_ccitt(body)
    return (body + crc.to_bytes(2, "big")).hex()


def test_register_definition():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["device_type"] == "temp_sensor"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert len(data["fields"]) == 6


def test_register_duplicate_conflict():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 409


def test_register_invalid_condition_reference():
    bad = {
        "device_type": "bad_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8,
             "condition": {"field": "ghost", "op": "eq", "value": 1}},
        ],
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_register_checksum_width_mismatch():
    bad = {
        "device_type": "bad_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 8}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_parse_temperature_branch():
    # header=0xAA, msg_type=1, flags=0, temperature raw=235 (LE) -> 23.5 °C
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["consumed_bits"] == 48

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["header"]["value"] == 0xAA
    assert fields["header"]["bit_offset"] == 0
    assert fields["msg_type"]["value"] == 1
    assert fields["msg_type"]["bit_offset"] == 8
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # skipped by branch

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "parsed"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"


def test_parse_humidity_branch():
    # msg_type=2 -> humidity branch, temperature skipped
    body = bytes([0xAA, 0x20, 0x55])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["humidity"]["value"] == 0x55
    assert fields["humidity"]["bit_offset"] == 16
    assert "temperature" not in fields
    assert data["checksum"]["valid"] is True


def test_parse_checksum_mismatch():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    good = make_message(body)
    bad = good[:-2] + ("00" if good[-2:] != "00" else "ff")
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"]["valid"] is False


def test_parse_truncated_message():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "AA10"},
    )
    assert resp.status_code == 422


def test_parse_invalid_hex():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "zzzz"},
    )
    assert resp.status_code == 422


def test_parse_unknown_device():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "nope", "hex": "AABB"},
    )
    assert resp.status_code == 404


def test_trailing_bits_warning():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    msg = make_message(body) + "FF"  # extra byte beyond definition
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": msg},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trailing_bits"] == 8
    assert data["warnings"]


def _register(device_type, version):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    }
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_latest_version_and_deactivate():
    _register("meter", "1.9.0")
    _register("meter", "1.10.0")  # semver-ish: 1.10.0 > 1.9.0

    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.10.0"

    resp = client.post("/api/v1/definitions/meter/1.10.0/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # latest now falls back to the still-active older version
    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.json()["version"] == "1.9.0"

    # explicit-version parse of an inactive definition still works
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "meter", "version": "1.10.0", "hex": "2A"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["fields"][0]["value"] == 0x2A


def test_list_definitions():
    resp = client.get("/api/v1/definitions")
    assert resp.status_code == 200
    data = resp.json()
    types = {item["device_type"] for item in data["items"]}
    assert {"temp_sensor", "meter"} <= types
    # inactive 1.10.0 hidden by default
    versions = {(i["device_type"], i["version"]) for i in data["items"]}
    assert ("meter", "1.10.0") not in versions

    resp = client.get("/api/v1/definitions", params={"include_inactive": True})
    versions = {(i["device_type"], i["version"]) for i in resp.json()["items"]}
    assert ("meter", "1.10.0") in versions

    resp = client.get("/api/v1/definitions", params={"device_type": "meter"})
    assert all(i["device_type"] == "meter" for i in resp.json()["items"])


def test_get_definition_and_404s():
    resp = client.get("/api/v1/definitions/temp_sensor/1.0.0")
    assert resp.status_code == 200
    assert resp.json()["checksum"]["algorithm"] == "crc16-ccitt"

    assert client.get("/api/v1/definitions/temp_sensor/9.9.9").status_code == 404
    assert client.get("/api/v1/definitions/ghost/latest").status_code == 404
    assert client.post("/api/v1/definitions/ghost/1.0.0/deactivate").status_code == 404


# ------------------------------------------------------------------ encode --


def test_encode_temperature_branch_roundtrip():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "version": "1.0.0",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # crc computed by the service, humidity not required on this branch
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert data["message_bits"] == 48
    assert data["padding_bits"] == 0

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["crc"]["value"] == crc16_ccitt(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert "humidity" not in fields

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "encoded"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"
    assert data["checksum"]["calculated_hex"] == "0x" + data["hex"][-4:]

    # the encoded payload parses back to the same values
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": data["hex"]},
    )
    assert resp.status_code == 200
    parsed = {f["name"]: f for f in resp.json()["fields"]}
    assert parsed["temperature"]["value"] == 23.5
    assert resp.json()["checksum"]["valid"] is True


def test_encode_humidity_branch():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 0x55},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == make_message(bytes([0xAA, 0x20, 0x55]))
    names = [f["name"] for f in data["fields"]]
    assert "temperature" not in names  # skipped branch not required, not encoded


def test_encode_negative_temperature_two_complement():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": -5.0},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # raw = -50 -> two's complement 0xFFCE, little-endian -> CE FF
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xCE, 0xFF]))
    resp = client.post(
        "/api/v1/parse", json={"device_type": "temp_sensor", "hex": data["hex"]}
    )
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["temperature"]["value"] == -5.0
    assert fields["temperature"]["raw_value"] == -50


def test_encode_missing_matched_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "temp_sensor", "values": {"header": 0xAA, "msg_type": 2, "flags": 0}},
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_unknown_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "ghost": 1},
        },
    )
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_encode_value_for_skipped_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "humidity": 5},
        },
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_checksum_field_not_settable():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "crc": 1},
        },
    )
    assert resp.status_code == 422
    assert "crc" in resp.json()["detail"]


def test_encode_out_of_range_422():
    # header is uint8, 0x1AA = 426 does not fit
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0x1AA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_value_not_representable_422():
    # scale 0.1 -> 23.55 has no exact raw
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.55},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_raw_out_of_range_422():
    # 4000.0 -> raw 40000 exceeds int16 range
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 4000.0},
        },
    )
    assert resp.status_code == 422


def test_encode_wrong_value_type_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": "0xAA", "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": True, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_unknown_device_404():
    resp = client.post("/api/v1/encode", json={"device_type": "ghost", "values": {}})
    assert resp.status_code == 404


def test_encode_unaligned_checksum_422():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "nibble_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "tag", "type": "uint", "bit_length": 4},
                {"name": "crc", "type": "uint", "bit_length": 8},
            ],
            "checksum": {"field": "crc", "algorithm": "sum8"},
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode", json={"device_type": "nibble_dev", "values": {"tag": 5}}
    )
    assert resp.status_code == 422
    assert "byte boundary" in resp.json()["detail"]


def test_encode_bool_hex_ascii_non_byte_aligned():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "mixed_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "enabled", "type": "bool", "bit_length": 1},
                {"name": "code", "type": "uint", "bit_length": 7},
                {"name": "magic", "type": "hex", "bit_length": 16},
                {"name": "label", "type": "ascii", "bit_length": 16},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "mixed_dev",
            "values": {"enabled": True, "code": 0x55, "magic": "0xABCD", "label": "OK"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 1|1010101 -> 0xD5, then magic and label bytes
    assert data["hex"] == "d5abcd4f4b"
    assert data["checksum"] is None

    resp = client.post("/api/v1/parse", json={"device_type": "mixed_dev", "hex": data["hex"]})
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["enabled"]["value"] is True
    assert fields["code"]["value"] == 0x55
    assert fields["magic"]["value"] == "0xabcd"
    assert fields["label"]["value"] == "OK"


def test_encode_condition_uses_raw_values():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "raw_cond",
            "version": "1.0.0",
            "fields": [
                {"name": "temp", "type": "int", "bit_length": 16, "scale": 0.1},
                {
                    "name": "alarm",
                    "type": "uint",
                    "bit_length": 8,
                    "condition": {"field": "temp", "op": "ge", "value": 1000},
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    # 99.9 -> raw 999 < 1000 -> alarm skipped, must not be provided
    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 99.9}}
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp"]

    # 100.0 -> raw 1000 >= 1000 -> alarm required
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "raw_cond", "values": {"temp": 100.0, "alarm": 1}},
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp", "alarm"]

    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 100.0}}
    )
    assert resp.status_code == 422


def test_encode_signed_int_range():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "signed_dev",
            "version": "1.0.0",
            "fields": [{"name": "delta", "type": "int", "bit_length": 8}],
        },
    )
    assert resp.status_code == 201, resp.text

    def enc(value):
        return client.post(
            "/api/v1/encode", json={"device_type": "signed_dev", "values": {"delta": value}}
        )

    resp = enc(-1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == "ff"  # two's complement

    assert enc(127).json()["hex"] == "7f"
    assert enc(-128).json()["hex"] == "80"
    assert enc(128).status_code == 422
    assert enc(-129).status_code == 422


def test_encode_version_rules_match_parse():
    for version, bits in (("1.0.0", 8), ("2.0.0", 16)):
        resp = client.post(
            "/api/v1/definitions",
            json={
                "device_type": "enc_dev",
                "version": version,
                "fields": [{"name": "val", "type": "uint", "bit_length": bits}],
            },
        )
        assert resp.status_code == 201, resp.text

    # latest active (2.0.0) used by default
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x1234}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2.0.0"
    assert resp.json()["hex"] == "1234"

    resp = client.post("/api/v1/definitions/enc_dev/2.0.0/deactivate")
    assert resp.status_code == 200

    # falls back to the still-active 1.0.0
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x12}}
    )
    assert resp.json()["version"] == "1.0.0"
    assert resp.json()["hex"] == "12"

    # explicit inactive version still encodable
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_dev", "version": "2.0.0", "values": {"val": 0x1234}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition_active"] is False
    assert resp.json()["hex"] == "1234"


# ---------------------------------------------------- version compatibility --


def _compat(device_type, old, new):
    return client.post(
        f"/api/v1/definitions/{device_type}/compatibility",
        json={"old_version": old, "new_version": new},
    )


def test_compatibility_identical_structure_compatible():
    # temp_sensor 1.0.0 vs a re-registered copy under a new version
    payload = dict(SENSOR_DEF)
    payload["version"] = "1.1.0"
    payload["description"] = "same wire format, new docs"
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text

    resp = _compat("temp_sensor", "1.0.0", "1.1.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is True
    assert data["status"] == "compatible"
    assert data["breaking_branches"] == 0
    # msg_type is a 4-bit control field cut at 1,2 -> 4 equivalence classes
    assert data["combination_count"] == 4
    assert data["reachable_branches"] == 4
    assert data["equivalence_classes"][0]["field"] == "msg_type"
    assert data["equivalence_classes"][0]["representatives"] == [0, 1, 2, 3]

    # every enumerated branch lays out fields with offsets and a total length
    for branch in data["branches"]:
        assert branch["compatible"] is True
        layout = branch["old_layout"]
        assert layout["total_bits"] == branch["new_layout"]["total_bits"]
        assert layout["checksum_algorithm"] == "crc16-ccitt"
        offsets = [f["bit_offset"] for f in layout["fields"]]
        assert offsets == sorted(offsets)
        assert all(f["bit_length"] > 0 for f in layout["fields"])

    # the temperature branch (msg_type == 1) layout
    temp_branch = next(
        b for b in data["branches"]
        if b["representative"]["msg_type"]["value"] == 1
    )
    names = [f["name"] for f in temp_branch["old_layout"]["fields"]]
    assert names == ["header", "msg_type", "flags", "temperature", "crc"]
    temp_field = next(f for f in temp_branch["old_layout"]["fields"] if f["name"] == "temperature")
    assert (temp_field["type"], temp_field["bit_length"], temp_field["bit_offset"]) == (
        "int", 16, 16,
    )
    assert temp_branch["old_layout"]["total_bits"] == 48


def test_compatibility_field_width_change_is_breaking():
    payload = {
        "device_type": "compat_w",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    new = {
        "device_type": "compat_w",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 16,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_w", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is False
    assert data["status"] == "breaking"
    # t == 1 isolates a point, leaving one class above it -> 3 classes total;
    # only the t == 1 branch contains v.
    assert data["combination_count"] == 3
    assert data["breaking_branches"] == 1

    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
    )
    assert bad["compatible"] is False
    kinds = {d["kind"]: d for d in bad["differences"]}
    assert kinds["field_changed"]["field"] == "v"
    assert kinds["field_changed"]["bit_length"] == {"old": 8, "new": 16}
    assert kinds["total_length_changed"]["old"] == 12
    assert kinds["total_length_changed"]["new"] == 20

    # the other branches (t != 1) are unaffected and remain compatible
    assert all(b["compatible"] for b in data["branches"]
               if b["representative"]["t"]["value"] != 1)


def test_compatibility_checksum_algorithm_change_is_breaking():
    payload = {
        "device_type": "compat_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 16}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201
    new = dict(payload)
    new["version"] = "2.0.0"
    new["checksum"] = {"field": "crc", "algorithm": "crc16-modbus"}
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_crc", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["compatible"] is False
    assert data["combination_count"] == 1  # no control fields -> one layout
    diff = data["branches"][0]["differences"][0]
    assert diff == {
        "kind": "checksum_algorithm_changed",
        "old": "crc16-ccitt",
        "new": "crc16-modbus",
    }
    assert data["branches"][0]["old_layout"]["checksum_algorithm"] == "crc16-ccitt"


def test_compatibility_new_version_adds_finer_branch_is_breaking_only_there():
    base = {
        "device_type": "compat_fine",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=base).status_code == 201
    new = {
        "device_type": "compat_fine",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
            {"name": "w", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 2}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_fine", "1.0.0", "2.0.0")
    data = resp.json()
    # eq 1 isolates one point and eq 2 another -> joint partition:
    # [0], [1], [2], [3..15]
    assert data["combination_count"] == 4
    assert data["breaking_branches"] == 1
    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 2
    )
    assert bad["compatible"] is False
    assert any(d["kind"] == "field_added" and d["field"] == "w" for d in bad["differences"])
    assert {
        b["representative"]["t"]["value"]
        for b in data["branches"] if b["compatible"]
    } == {0, 1, 3}


def test_compatibility_versions_missing_404_and_same_version_422():
    resp = _compat("compat_missing", "1.0.0", "2.0.0")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "9.9.9")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "1.0.0")
    assert resp.status_code == 422


def test_compatibility_too_many_combinations_422():
    # two independent uint9 control fields, each split into 17 classes -> 289
    def payload(version):
        fields = [{"name": "a", "type": "uint", "bit_length": 9}]
        for i in range(16):
            fields.append(
                {"name": f"x{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "a", "op": "lt", "value": i + 1}}
            )
        fields.append({"name": "b", "type": "uint", "bit_length": 9})
        for i in range(16):
            fields.append(
                {"name": f"y{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "b", "op": "lt", "value": i + 1}}
            )
        return {"device_type": "compat_big", "version": version, "fields": fields}

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_big", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "256" in resp.json()["detail"]


def test_compatibility_nested_conditional_unreachable_combinations_skipped():
    def payload(version):
        return {
            "device_type": "compat_nested",
            "version": version,
            "fields": [
                {"name": "t", "type": "uint", "bit_length": 4},
                {"name": "c1", "type": "uint", "bit_length": 8,
                 "condition": {"field": "t", "op": "eq", "value": 1}},
                # c2's control field c1 is itself conditional: when t != 1 the
                # branch is unreachable and must not be compared.
                {"name": "c2", "type": "uint", "bit_length": 8,
                 "condition": {"field": "c1", "op": "gt", "value": 5}},
            ],
        }

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_nested", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # t partition [0],[1],[2..15] crossed with c1 classes {..5},{6..} = 6
    # combos; only the t==1 ones are reachable (c1 is absent otherwise).
    assert data["combination_count"] == 6
    assert data["reachable_branches"] == 2
    assert data["unreachable_combinations"] == 4
    assert data["compatible"] is True
    # only reachable combos (t == 1, c1 present) appear in the branch list
    reps = {
        (b["representative"]["t"]["value"], b["representative"]["c1"]["value"])
        for b in data["branches"]
    }
    assert reps == {(1, 0), (1, 6)}
    c2_branch = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
        and b["representative"]["c1"]["value"] == 6
    )
    assert [f["name"] for f in c2_branch["old_layout"]["fields"]] == ["t", "c1", "c2"]
    assert c2_branch["old_layout"]["total_bits"] == 20
