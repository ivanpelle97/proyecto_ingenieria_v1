"""
Servidor FastAPI para captura, entrenamiento guiado por puntos e inferencia.

Flujo principal:
  - Las anclas ESP32 consultan su configuración en /api/anchors/{anchor_id}/config
  - Envían batches RAW a /ingest
  - El servidor persiste RAW append-only y, si hay un punto activo, captura hasta
    N muestras RSSI por ancla para la MAC objetivo
  - La UI consulta /api/session/status para guiar la campaña punto por punto
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


TZ = ZoneInfo("America/Argentina/Cordoba")
RUNS_DIR = Path(os.environ.get("NEXA_RUNS_DIR", "runs")).resolve()
CONTROL_DIR = RUNS_DIR / "_control"
ACTIVE_SESSION_FILE = CONTROL_DIR / "active_session.json"
STATE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(tz=TZ).isoformat()


def ensure_control_dir() -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)


def normalize_mac(value: str) -> str:
    mac = value.strip().lower()
    if not mac:
        raise ValueError("MAC vacia")
    parts = mac.split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError(f"MAC invalida: {value}")
    try:
        [int(part, 16) for part in parts]
    except ValueError as exc:
        raise ValueError(f"MAC invalida: {value}") from exc
    return mac


def normalize_name(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 128:
        raise ValueError("Nombre invalido")
    if any(not (ch.isalnum() or ch in "_-") for ch in clean):
        raise ValueError(f"Nombre invalido: {value}")
    return clean


def safe_name(value: str) -> str:
    try:
        return normalize_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def run_dir(session_id: str, campaign_id: str) -> Path:
    return RUNS_DIR / session_id / campaign_id


def session_dir(session_id: str) -> Path:
    return RUNS_DIR / session_id


def session_plan_path(session_id: str) -> Path:
    return session_dir(session_id) / "session_plan.json"


def manifest_path(session_id: str, campaign_id: str) -> Path:
    return run_dir(session_id, campaign_id) / "experiment.json"


def state_path(session_id: str, campaign_id: str) -> Path:
    return run_dir(session_id, campaign_id) / "training_state.json"


def points_path(session_id: str, campaign_id: str) -> Path:
    return run_dir(session_id, campaign_id) / "points.csv"


def samples_path(session_id: str, campaign_id: str) -> Path:
    return run_dir(session_id, campaign_id) / "samples.csv"


def raw_dir(session_id: str, campaign_id: str) -> Path:
    return run_dir(session_id, campaign_id) / "raw"


def load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def append_csv_row(path: Path, row: Dict[str, Any], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def anchor_ids_from_manifest(manifest: dict) -> List[str]:
    return [anchor["anchor_id"] for anchor in manifest.get("anchors", [])]


def make_default_state(manifest: dict) -> dict:
    anchor_statuses = {}
    for anchor_id in anchor_ids_from_manifest(manifest):
        anchor_statuses[anchor_id] = {
            "last_seen_at": None,
            "last_ip": None,
            "last_batch_packets": 0,
            "last_batch_at": None,
            "last_config_poll_at": None,
        }
    return {
        "status": "idle",
        "current_point": None,
        "points": [],
        "anchor_statuses": anchor_statuses,
        "updated_at": now_iso(),
    }


def ensure_run_files(session_id: str, campaign_id: str, manifest: Optional[dict] = None) -> None:
    base = run_dir(session_id, campaign_id)
    raw_dir(session_id, campaign_id).mkdir(parents=True, exist_ok=True)
    if manifest is not None and not manifest_path(session_id, campaign_id).exists():
        save_json(manifest_path(session_id, campaign_id), manifest)
    if manifest is not None and not state_path(session_id, campaign_id).exists():
        save_json(state_path(session_id, campaign_id), make_default_state(manifest))
    base.mkdir(parents=True, exist_ok=True)


def load_run_context(session_id: str, campaign_id: str) -> tuple[dict, dict]:
    manifest = load_json(manifest_path(session_id, campaign_id))
    state = load_json(state_path(session_id, campaign_id))
    if manifest is None or state is None:
        raise HTTPException(status_code=404, detail="No existe la campaña solicitada")
    return manifest, state


def load_active_context() -> Optional[tuple[dict, dict]]:
    pointer = load_json(ACTIVE_SESSION_FILE)
    if not pointer:
        return None
    session_id = pointer.get("session_id")
    campaign_id = pointer.get("campaign_id")
    if not session_id or not campaign_id:
        return None
    manifest = load_json(manifest_path(session_id, campaign_id))
    state = load_json(state_path(session_id, campaign_id))
    if manifest is None or state is None:
        return None
    return manifest, state


def save_active_pointer(session_id: str, campaign_id: str) -> None:
    ensure_control_dir()
    save_json(
        ACTIVE_SESSION_FILE,
        {
            "session_id": session_id,
            "campaign_id": campaign_id,
            "updated_at": now_iso(),
        },
    )


def clear_active_pointer() -> None:
    if ACTIVE_SESSION_FILE.exists():
        ACTIVE_SESSION_FILE.unlink()


def touch_anchor_status(state: dict, anchor_id: str, *, client_ip: Optional[str], field: str) -> None:
    anchor_statuses = state.setdefault("anchor_statuses", {})
    anchor_status = anchor_statuses.setdefault(
        anchor_id,
        {
            "last_seen_at": None,
            "last_ip": None,
            "last_batch_packets": 0,
            "last_batch_at": None,
            "last_config_poll_at": None,
        },
    )
    anchor_status["last_seen_at"] = now_iso()
    anchor_status["last_ip"] = client_ip
    anchor_status[field] = now_iso()


def point_counts_complete(point: dict, samples_per_anchor: int, anchor_ids: List[str]) -> bool:
    counts = point.get("counts", {})
    return all(int(counts.get(anchor_id, 0)) >= samples_per_anchor for anchor_id in anchor_ids)


def packet_contains_target(packet: "Packet", target_mac: str) -> bool:
    return packet.addr1 == target_mac or packet.addr2 == target_mac or packet.addr3 == target_mac


class Packet(BaseModel):
    ts_us: int = Field(..., description="Timestamp local de la ancla en microsegundos")
    rssi: int = Field(..., description="RSSI reportado por la ESP32 en dBm")
    channel: int = Field(..., description="Canal 2.4 GHz observado")
    addr1: str
    addr2: str
    addr3: str

    @field_validator("addr1", "addr2", "addr3")
    @classmethod
    def validate_mac(cls, value: str) -> str:
        return normalize_mac(value)


class IngestBatch(BaseModel):
    session_id: str
    campaign_id: str
    anchor_id: str
    packets: List[Packet]


class EnvironmentSpec(BaseModel):
    length_m: float = Field(..., gt=0)
    width_m: float = Field(..., gt=0)
    height_m: float = Field(..., gt=0)


class AnchorSpec(BaseModel):
    anchor_id: str
    x_m: float
    y_m: float
    z_m: float = Field(default=2.0, ge=0.0)
    model: str = ""
    notes: str = ""

    @field_validator("anchor_id")
    @classmethod
    def validate_anchor_id(cls, value: str) -> str:
        return normalize_name(value)


class SessionStartRequest(BaseModel):
    session_id: str
    campaign_id: str
    target_mac: str
    samples_per_anchor: int = Field(default=10, ge=1, le=100)
    planned_campaigns: int = Field(default=1, ge=1, le=20)
    environment: EnvironmentSpec
    anchors: List[AnchorSpec]

    @field_validator("session_id", "campaign_id")
    @classmethod
    def validate_names(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("target_mac")
    @classmethod
    def validate_target_mac(cls, value: str) -> str:
        return normalize_mac(value)


class PointStartRequest(BaseModel):
    point_id: str
    x_m: float = Field(..., ge=0.0)
    y_m: float = Field(..., ge=0.0)
    z_m: float = Field(default=1.0, ge=0.0)

    @field_validator("point_id")
    @classmethod
    def validate_point_id(cls, value: str) -> str:
        return normalize_name(value)


class PointFinishRequest(BaseModel):
    force: bool = False


app = FastAPI(title="NEXA-IPS Orchestrator", version="2.0")


def upsert_session_plan(
    session_id: str,
    campaign_id: str,
    planned_campaigns: int,
    target_mac: str,
    environment: dict,
    anchors: List[dict],
) -> dict:
    path = session_plan_path(session_id)
    existing = load_json(path) or {}
    campaign_ids = [str(value) for value in existing.get("campaign_ids", []) if str(value).strip()]
    if campaign_id not in campaign_ids:
        campaign_ids.append(campaign_id)

    payload = {
        "session_id": session_id,
        "planned_campaigns": int(planned_campaigns),
        "target_mac": target_mac,
        "environment": environment,
        "anchors": anchors,
        "campaign_ids": campaign_ids,
        "created_at": existing.get("created_at", now_iso()),
        "updated_at": now_iso(),
    }
    save_json(path, payload)
    return payload


@app.get("/health")
def health() -> Dict[str, Any]:
    active = load_json(ACTIVE_SESSION_FILE)
    return {"status": "ok", "runs_dir": str(RUNS_DIR), "active_session": active}


@app.get("/api/session/status")
def session_status() -> Dict[str, Any]:
    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is None:
            return {"active": False}
        manifest, state = ctx
        return {"active": True, "manifest": manifest, "state": state}


@app.post("/api/session/start")
def session_start(req: SessionStartRequest) -> Dict[str, Any]:
    anchor_ids = [anchor.anchor_id for anchor in req.anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        raise HTTPException(status_code=400, detail="Los identificadores de antena deben ser unicos")
    if not anchor_ids:
        raise HTTPException(status_code=400, detail="Debe configurar al menos una antena")
    for anchor in req.anchors:
        if anchor.x_m < 0 or anchor.x_m > req.environment.length_m:
            raise HTTPException(status_code=400, detail=f"La antena {anchor.anchor_id} queda fuera del largo del ambiente")
        if anchor.y_m < 0 or anchor.y_m > req.environment.width_m:
            raise HTTPException(status_code=400, detail=f"La antena {anchor.anchor_id} queda fuera del ancho del ambiente")
        if anchor.z_m < 0 or anchor.z_m > req.environment.height_m:
            raise HTTPException(status_code=400, detail=f"La antena {anchor.anchor_id} queda fuera del alto del ambiente")

    session_id = req.session_id
    campaign_id = req.campaign_id

    manifest = {
        "session_id": session_id,
        "campaign_id": campaign_id,
        "target_mac": req.target_mac,
        "samples_per_anchor": req.samples_per_anchor,
        "planned_campaigns": req.planned_campaigns,
        "environment": req.environment.model_dump(),
        "anchors": [anchor.model_dump() for anchor in req.anchors],
        "status": "active",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    with STATE_LOCK:
        ensure_run_files(session_id, campaign_id, manifest)

        existing_manifest = load_json(manifest_path(session_id, campaign_id))
        existing_state = load_json(state_path(session_id, campaign_id))

        if existing_manifest is not None:
            manifest["created_at"] = existing_manifest.get("created_at", manifest["created_at"])

        save_json(manifest_path(session_id, campaign_id), manifest)
        upsert_session_plan(
            session_id=session_id,
            campaign_id=campaign_id,
            planned_campaigns=req.planned_campaigns,
            target_mac=req.target_mac,
            environment=manifest["environment"],
            anchors=manifest["anchors"],
        )

        state = existing_state or make_default_state(manifest)
        state["status"] = "ready"
        state["updated_at"] = now_iso()

        valid_anchor_ids = set(anchor_ids)
        anchor_statuses = state.setdefault("anchor_statuses", {})
        for anchor_id in valid_anchor_ids:
            anchor_statuses.setdefault(
                anchor_id,
                {
                    "last_seen_at": None,
                    "last_ip": None,
                    "last_batch_packets": 0,
                    "last_batch_at": None,
                    "last_config_poll_at": None,
                },
            )
        for anchor_id in list(anchor_statuses.keys()):
            if anchor_id not in valid_anchor_ids:
                anchor_statuses.pop(anchor_id, None)

        save_json(state_path(session_id, campaign_id), state)
        save_active_pointer(session_id, campaign_id)

    return {
        "ok": True,
        "session_id": session_id,
        "campaign_id": campaign_id,
        "planned_campaigns": req.planned_campaigns,
    }


@app.post("/api/session/finish")
def session_finish() -> Dict[str, Any]:
    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is None:
            raise HTTPException(status_code=404, detail="No hay una campaña activa")
        manifest, state = ctx
        if state.get("current_point") is not None:
            raise HTTPException(status_code=409, detail="Todavia hay un punto activo")
        manifest["status"] = "finished"
        manifest["updated_at"] = now_iso()
        state["status"] = "finished"
        state["updated_at"] = now_iso()
        save_json(manifest_path(manifest["session_id"], manifest["campaign_id"]), manifest)
        save_json(state_path(manifest["session_id"], manifest["campaign_id"]), state)
        clear_active_pointer()
        return {"ok": True}


@app.post("/api/points/start")
def point_start(req: PointStartRequest) -> Dict[str, Any]:
    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is None:
            raise HTTPException(status_code=404, detail="No hay una campaña activa")
        manifest, state = ctx
        session_id = manifest["session_id"]
        campaign_id = manifest["campaign_id"]
        current_point = state.get("current_point")
        if current_point is not None:
            raise HTTPException(status_code=409, detail="Ya hay un punto activo. Finalicelo antes de avanzar.")

        environment = manifest["environment"]
        if req.x_m > float(environment["length_m"]) or req.y_m > float(environment["width_m"]) or req.z_m > float(environment["height_m"]):
            raise HTTPException(status_code=400, detail="El punto queda fuera del ambiente configurado")

        point_ids = {point["point_id"] for point in state.get("points", [])}
        if req.point_id in point_ids:
            raise HTTPException(status_code=409, detail=f"El punto {req.point_id} ya fue capturado")

        anchor_ids = anchor_ids_from_manifest(manifest)
        point = {
            "point_id": req.point_id,
            "x_m": req.x_m,
            "y_m": req.y_m,
            "z_m": req.z_m,
            "started_at": now_iso(),
            "capture_complete_at": None,
            "completed_at": None,
            "counts": {anchor_id: 0 for anchor_id in anchor_ids},
            "complete": False,
        }
        state["current_point"] = point
        state["status"] = "capturing"
        state["updated_at"] = now_iso()
        save_json(state_path(session_id, campaign_id), state)
        return {"ok": True, "point": point}


@app.post("/api/points/finish")
def point_finish(req: PointFinishRequest) -> Dict[str, Any]:
    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is None:
            raise HTTPException(status_code=404, detail="No hay una campaña activa")
        manifest, state = ctx
        session_id = manifest["session_id"]
        campaign_id = manifest["campaign_id"]
        current_point = state.get("current_point")
        if current_point is None:
            raise HTTPException(status_code=404, detail="No hay un punto activo")

        samples_per_anchor = int(manifest["samples_per_anchor"])
        anchor_ids = anchor_ids_from_manifest(manifest)
        is_complete = point_counts_complete(current_point, samples_per_anchor, anchor_ids)
        if not is_complete and not req.force:
            raise HTTPException(status_code=409, detail="El punto todavia no alcanzo la cantidad minima de muestras")

        current_point["complete"] = is_complete
        current_point["completed_at"] = now_iso()
        current_point["status"] = "complete" if is_complete else "partial"

        point_row = {
            "point_id": current_point["point_id"],
            "x_m": current_point["x_m"],
            "y_m": current_point["y_m"],
            "z_m": current_point["z_m"],
            "started_at": current_point["started_at"],
            "capture_complete_at": current_point.get("capture_complete_at"),
            "completed_at": current_point["completed_at"],
            "status": current_point["status"],
            "samples_per_anchor": samples_per_anchor,
        }
        for anchor_id in anchor_ids:
            point_row[f"{anchor_id}_count"] = int(current_point["counts"].get(anchor_id, 0))

        append_csv_row(
            points_path(session_id, campaign_id),
            point_row,
            fieldnames=list(point_row.keys()),
        )

        state.setdefault("points", []).append(current_point)
        state["current_point"] = None
        state["status"] = "ready"
        state["updated_at"] = now_iso()
        save_json(state_path(session_id, campaign_id), state)

        return {"ok": True, "point": point_row}


@app.get("/api/anchors/{anchor_id}/config")
def anchor_config(anchor_id: str, request: Request) -> Dict[str, Any]:
    anchor_id = safe_name(anchor_id)
    client_ip = request.client.host if request.client else None
    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is None:
            return {
                "enabled": False,
                "anchor_id": anchor_id,
                "reason": "no_active_session",
                "server_time": now_iso(),
            }

        manifest, state = ctx
        anchor_map = {anchor["anchor_id"]: anchor for anchor in manifest.get("anchors", [])}
        if anchor_id not in anchor_map:
            return {
                "enabled": False,
                "anchor_id": anchor_id,
                "reason": "anchor_not_configured",
                "server_time": now_iso(),
            }

        touch_anchor_status(state, anchor_id, client_ip=client_ip, field="last_config_poll_at")
        state["updated_at"] = now_iso()
        save_json(state_path(manifest["session_id"], manifest["campaign_id"]), state)

        current_point = state.get("current_point")
        return {
            "enabled": True,
            "anchor_id": anchor_id,
            "session_id": manifest["session_id"],
            "campaign_id": manifest["campaign_id"],
            "target_mac": manifest["target_mac"],
            "samples_per_anchor": manifest["samples_per_anchor"],
            "capture_active": current_point is not None and not current_point.get("complete", False),
            "current_point_id": current_point["point_id"] if current_point else None,
            "environment": manifest["environment"],
            "anchor": anchor_map[anchor_id],
            "server_time": now_iso(),
        }


@app.post("/ingest")
async def ingest(batch: IngestBatch, request: Request) -> Dict[str, Any]:
    session_id = safe_name(batch.session_id)
    campaign_id = safe_name(batch.campaign_id)
    anchor_id = safe_name(batch.anchor_id)

    client_ip = request.client.host if request.client else "unknown"
    ts_server = now_iso()
    raw_out = raw_dir(session_id, campaign_id)
    raw_out.mkdir(parents=True, exist_ok=True)
    raw_file = raw_out / f"{anchor_id}.jsonl"

    payload = {
        "ts_server": ts_server,
        "client_ip": client_ip,
        "session_id": session_id,
        "campaign_id": campaign_id,
        "anchor_id": anchor_id,
        "packets": [packet.model_dump() for packet in batch.packets],
    }

    with raw_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    stored_samples = 0

    with STATE_LOCK:
        ctx = load_active_context()
        if ctx is not None:
            manifest, state = ctx
            if manifest["session_id"] == session_id and manifest["campaign_id"] == campaign_id:
                anchor_ids = anchor_ids_from_manifest(manifest)
                if anchor_id in anchor_ids:
                    touch_anchor_status(state, anchor_id, client_ip=client_ip, field="last_batch_at")
                    state["anchor_statuses"][anchor_id]["last_batch_packets"] = len(batch.packets)

                    current_point = state.get("current_point")
                    if current_point is not None and not current_point.get("complete", False):
                        target_mac = manifest["target_mac"]
                        samples_per_anchor = int(manifest["samples_per_anchor"])
                        current_count = int(current_point["counts"].get(anchor_id, 0))

                        if current_count < samples_per_anchor:
                            for packet in batch.packets:
                                if current_count >= samples_per_anchor:
                                    break
                                if not packet_contains_target(packet, target_mac):
                                    continue
                                sample_index = current_count + 1
                                sample_row = {
                                    "ts_server": ts_server,
                                    "session_id": session_id,
                                    "campaign_id": campaign_id,
                                    "point_id": current_point["point_id"],
                                    "x_m": current_point["x_m"],
                                    "y_m": current_point["y_m"],
                                    "z_m": current_point["z_m"],
                                    "anchor_id": anchor_id,
                                    "sample_index": sample_index,
                                    "rssi": packet.rssi,
                                    "channel": packet.channel,
                                    "ts_us": packet.ts_us,
                                    "addr1": packet.addr1,
                                    "addr2": packet.addr2,
                                    "addr3": packet.addr3,
                                }
                                append_csv_row(
                                    samples_path(session_id, campaign_id),
                                    sample_row,
                                    fieldnames=list(sample_row.keys()),
                                )
                                current_count += 1
                                stored_samples += 1

                            current_point["counts"][anchor_id] = current_count
                            if point_counts_complete(current_point, samples_per_anchor, anchor_ids):
                                current_point["complete"] = True
                                current_point["capture_complete_at"] = now_iso()
                                state["status"] = "point_ready"

                    state["updated_at"] = now_iso()
                    save_json(state_path(session_id, campaign_id), state)

    if batch.packets:
        rssi_vals = [packet.rssi for packet in batch.packets]
        print(
            f"[{ts_server}] RX anchor={anchor_id} session={session_id} campaign={campaign_id} "
            f"ip={client_ip} n={len(batch.packets)} "
            f"rssi_min={min(rssi_vals)} rssi_med={sorted(rssi_vals)[len(rssi_vals)//2]} rssi_max={max(rssi_vals)} "
            f"stored_for_point={stored_samples}"
        )

    return {
        "ok": True,
        "stored": str(raw_file),
        "packets_received": len(batch.packets),
        "samples_recorded_for_point": stored_samples,
        "ts_server": ts_server,
    }
