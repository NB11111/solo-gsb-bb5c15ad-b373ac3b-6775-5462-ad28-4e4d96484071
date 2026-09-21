"""Industrial binary message parsing API."""

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from . import database
from .compat import MAX_COMBINATIONS, CompatibilityError, analyze_versions
from .encoder import EncodeError, encode_message
from .parser import ParseError, parse_message
from .schemas import (
    CompatibilityRequest,
    DefinitionCreate,
    EncodeRequest,
    ParseRequest,
    validate_definition,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Industrial Binary Message Parser",
    description=(
        "Register versioned binary message definitions (bit widths, endianness, "
        "conditional branches, checksums) and parse hex payloads against them."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Make the service usable even if the lifespan hook is bypassed (e.g. tests).
database.init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/definitions", status_code=201)
def register_definition(defn: DefinitionCreate):
    """Register a new versioned message definition."""
    try:
        validate_definition(defn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record = database.create_definition(
        device_type=defn.device_type,
        version=defn.version,
        description=defn.description,
        fields=[f.model_dump(exclude_none=True) for f in defn.fields],
        checksum=defn.checksum.model_dump() if defn.checksum else None,
    )
    if record is None:
        raise HTTPException(
            status_code=409,
            detail=f"definition '{defn.device_type}' version '{defn.version}' already exists",
        )
    return record


@app.get("/api/v1/definitions")
def list_definitions(
    device_type: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
):
    """List registered definitions (active ones by default)."""
    return {
        "count": len(
            items := database.list_definitions(device_type, include_inactive)
        ),
        "items": items,
    }


@app.get("/api/v1/definitions/{device_type}/latest")
def get_latest_definition(device_type: str):
    """Get the latest active version for a device type."""
    record = database.get_latest(device_type, active_only=True)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active definition found for device type '{device_type}'",
        )
    return record


@app.get("/api/v1/definitions/{device_type}/{version}")
def get_definition(device_type: str, version: str):
    record = database.get_definition(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate")
def deactivate_definition(device_type: str, version: str):
    """Deactivate a definition. It stays parseable by explicit version but is
    excluded from listing (by default) and from latest-version lookups."""
    record = database.deactivate(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


_HEX_CLEANER = re.compile(r"(0[xX])|[\s_-]")


def _select_definition(device_type: str, version: Optional[str]) -> dict:
    """Shared by /parse and /encode: an explicit version selects that definition
    (even if inactive); otherwise the latest active version is used."""
    if version:
        record = database.get_definition(device_type, version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"definition '{device_type}' version '{version}' not found",
            )
    else:
        record = database.get_latest(device_type, active_only=True)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no active definition found for device type '{device_type}'",
            )
    return record


@app.post("/api/v1/definitions/{device_type}/compatibility")
def analyze_compatibility(device_type: str, req: CompatibilityRequest):
    """Statically compare two registered versions of a device type.

    Conditional constants partition the controlling fields into finite
    equivalence classes; every reachable branch is enumerated and its fields
    laid out (bit offsets + total length). The verdict is ``breaking`` when an
    old-version branch has no field-for-field match (name, type, bit width,
    offset, total length, checksum algorithm) in the new version at the same
    representative values, otherwise ``compatible``. Both versions may be
    inactive; they must be explicitly registered."""
    old_record = database.get_definition(device_type, req.old_version)
    if old_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.old_version}' not found",
        )
    new_record = database.get_definition(device_type, req.new_version)
    if new_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.new_version}' not found",
        )

    try:
        report = analyze_versions(
            old_fields=old_record["fields"],
            old_checksum=old_record["checksum"],
            new_fields=new_record["fields"],
            new_checksum=new_record["checksum"],
        )
    except CompatibilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": device_type,
        "old_version": req.old_version,
        "new_version": req.new_version,
        **report,
    }


@app.post("/api/v1/parse")
def parse(req: ParseRequest):
    """Parse a hex payload against a definition (latest active version unless
    an explicit version is given)."""
    record = _select_definition(req.device_type, req.version)

    cleaned = _HEX_CLEANER.sub("", req.hex)
    if not cleaned:
        raise HTTPException(status_code=422, detail="hex payload is empty")
    try:
        data = bytes.fromhex(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="invalid hex payload: non-hex characters or odd number of digits",
        )

    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


@app.post("/api/v1/encode")
def encode(req: EncodeRequest):
    """Encode field values into a hex payload (latest active version unless an
    explicit version is given). Conditions are evaluated on raw values; the
    checksum field is computed automatically and must not be provided."""
    record = _select_definition(req.device_type, req.version)

    try:
        result = encode_message(record["fields"], record["checksum"], req.values)
    except EncodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }
