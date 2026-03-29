from __future__ import annotations

import importlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import build_dataset as build_dataset_module
from pipeline import train as train_module
from pipeline import utils as utils_module

build_dataset_module = importlib.reload(build_dataset_module)
train_module = importlib.reload(train_module)
utils_module = importlib.reload(utils_module)

build_dataset_file = build_dataset_module.build_dataset_file
build_session_dataset_file = build_dataset_module.build_session_dataset_file
list_campaign_bases = build_dataset_module.list_campaign_bases
load_session_plan_file = build_dataset_module.load_session_plan
train_dataset_file = train_module.train_dataset_file
build_live_feature_rows_for_all_devices = utils_module.build_live_feature_rows_for_all_devices
default_anchor_layout = utils_module.default_anchor_layout
load_json_file = utils_module.load_json
load_points = utils_module.load_points


st.set_page_config(page_title="NEXA-IPS", layout="wide")


def api_request(method: str, base_url: str, path: str, payload: dict | None = None) -> dict:
    url = base_url.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("detail", detail)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"{exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"No fue posible conectar con el servidor: {exc}") from exc


@st.cache_resource(show_spinner=False)
def load_model(model_path: str):
    pack = joblib.load(model_path)
    return pack["model"], pack["feat_cols"]


def default_models_dir(base: Path) -> Path:
    return base / "models"


def default_session_models_dir(session_base: Path) -> Path:
    return session_base / "models_master"


def latest_model_path_from_dir(models_dir: Path) -> Path | None:
    if not models_dir.exists():
        return None
    candidates = sorted(models_dir.glob("best_model_*.joblib"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def latest_model_path(base: Path) -> Path | None:
    return latest_model_path_from_dir(default_models_dir(base))


def latest_session_model_path(session_base: Path) -> Path | None:
    return latest_model_path_from_dir(default_session_models_dir(session_base))


def summarize_session(session_base: Path) -> dict:
    plan = load_session_plan_file(session_base)
    campaign_bases = list_campaign_bases(session_base)
    rows = []
    for campaign_base in campaign_bases:
        manifest_path = campaign_base / "experiment.json"
        manifest = load_json_file(manifest_path) if manifest_path.exists() else {}
        rows.append(
            {
                "campaign_id": campaign_base.name,
                "status": manifest.get("status", "unknown"),
                "dataset_ready": (campaign_base / "dataset.csv").exists(),
                "model_ready": latest_model_path(campaign_base) is not None,
            }
        )

    summary_df = pd.DataFrame(rows)
    planned = int(plan.get("planned_campaigns", 1)) if plan else 1
    return {
        "plan": plan,
        "campaigns": summary_df,
        "planned_campaigns": planned,
        "registered_campaigns": int(len(summary_df)),
        "datasets_ready": int(summary_df["dataset_ready"].sum()) if not summary_df.empty else 0,
        "models_ready": int(summary_df["model_ready"].sum()) if not summary_df.empty else 0,
    }


def manifest_to_start_payload(manifest: dict) -> dict:
    return {
        "session_id": manifest["session_id"],
        "campaign_id": manifest["campaign_id"],
        "target_mac": manifest["target_mac"],
        "samples_per_anchor": int(manifest["samples_per_anchor"]),
        "planned_campaigns": int(manifest.get("planned_campaigns", 1)),
        "environment": manifest["environment"],
        "anchors": manifest["anchors"],
    }


def list_inference_ready_runs(runs_dir: Path) -> list[dict]:
    entries: list[dict] = []
    if not runs_dir.exists():
        return entries

    for session_base in sorted(runs_dir.iterdir()):
        if not session_base.is_dir() or session_base.name.startswith("_"):
            continue
        for campaign_base in list_campaign_bases(session_base):
            manifest_path = campaign_base / "experiment.json"
            model_path = latest_model_path(campaign_base)
            if not manifest_path.exists() or model_path is None:
                continue
            manifest = load_json_file(manifest_path)
            if not manifest:
                continue
            entries.append(
                {
                    "label": (
                        f"{manifest['session_id']} / {manifest['campaign_id']} | "
                        f"{len(manifest.get('anchors', []))} antenas | modelo {model_path.name}"
                    ),
                    "manifest": manifest,
                    "run_base": campaign_base,
                    "model_path": model_path,
                }
            )
    return entries


def anchor_status_frame(manifest: dict, state: dict) -> pd.DataFrame:
    rows = []
    point = state.get("current_point") or {}
    counts = point.get("counts", {})
    sample_target = int(manifest["samples_per_anchor"])
    for anchor in manifest.get("anchors", []):
        status = state.get("anchor_statuses", {}).get(anchor["anchor_id"], {})
        rows.append(
            {
                "anchor_id": anchor["anchor_id"],
                "x_m": anchor["x_m"],
                "y_m": anchor["y_m"],
                "z_m": anchor["z_m"],
                "model": anchor.get("model", ""),
                "last_seen_at": status.get("last_seen_at"),
                "last_ip": status.get("last_ip"),
                "last_batch_packets": status.get("last_batch_packets", 0),
                "captured_for_point": int(counts.get(anchor["anchor_id"], 0)),
                "target_per_point": sample_target,
            }
        )
    return pd.DataFrame(rows)


def antenna_status_display_frame(manifest: dict, state: dict) -> pd.DataFrame:
    frame = anchor_status_frame(manifest, state)
    return frame.rename(
        columns={
            "anchor_id": "antena_id",
            "x_m": "x_m",
            "y_m": "y_m",
            "z_m": "z_m",
            "model": "modelo",
            "last_seen_at": "ultimo_reporte",
            "last_ip": "ultima_ip",
            "last_batch_packets": "paquetes_ultimo_lote",
            "captured_for_point": "muestras_punto",
            "target_per_point": "objetivo_punto",
        }
    )


def render_prediction_heatmap(ax, lx: float, ly: float, prediction_history: list[tuple[float, float]] | None, alpha: float = 0.45) -> None:
    if not prediction_history:
        return

    hist = np.array(prediction_history, dtype=float)
    if len(hist) <= 1:
        return

    grid_x = np.linspace(0, lx, 180)
    grid_y = np.linspace(0, ly, 140)
    Z = np.zeros((len(grid_y), len(grid_x)))
    sx = max(lx / 12.0, 0.25)
    sy = max(ly / 12.0, 0.25)
    for x_val, y_val in hist[-400:]:
        Z += np.exp(
            -(((grid_x - x_val) ** 2) / (2 * sx**2))[None, :]
            - (((grid_y - y_val) ** 2) / (2 * sy**2))[:, None]
        )
    ax.imshow(Z, origin="lower", extent=[0, lx, 0, ly], aspect="auto", alpha=alpha, cmap="YlOrRd")


def plot_layout(
    manifest: dict,
    state: dict,
    prediction_history: list[tuple[float, float]] | None = None,
    current_predictions: pd.DataFrame | None = None,
) -> plt.Figure:
    env = manifest["environment"]
    lx = float(env["length_m"])
    ly = float(env["width_m"])
    anchors = manifest.get("anchors", [])
    completed_points = [point for point in state.get("points", []) if point.get("status") == "complete"]
    current_point = state.get("current_point")

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.set_xlim(0, lx)
    ax.set_ylim(0, ly)
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")
    ax.set_title("Plano, antenas y heatmap de dispositivos")
    ax.grid(True, alpha=0.25)

    render_prediction_heatmap(ax, lx, ly, prediction_history, alpha=0.45)

    for anchor in anchors:
        ax.scatter(anchor["x_m"], anchor["y_m"], marker="^", s=130, color="#005f73")
        ax.text(anchor["x_m"] + 0.05, anchor["y_m"] + 0.05, anchor["anchor_id"], color="#005f73")

    if completed_points:
        xs = [point["x_m"] for point in completed_points]
        ys = [point["y_m"] for point in completed_points]
        ax.scatter(xs, ys, s=45, color="#0a9396", label="Puntos entrenados")
        for point in completed_points:
            ax.text(point["x_m"] + 0.03, point["y_m"] + 0.03, point["point_id"], fontsize=8, color="#0a9396")

    if current_point:
        ax.scatter(current_point["x_m"], current_point["y_m"], s=90, color="#ee9b00", label="Punto actual")
        ax.text(current_point["x_m"] + 0.03, current_point["y_m"] + 0.03, current_point["point_id"], color="#ca6702")

    if current_predictions is not None and not current_predictions.empty:
        ax.scatter(current_predictions["x_hat"], current_predictions["y_hat"], s=120, marker="x", color="#bb3e03", label="Dispositivos actuales")
        for _, row in current_predictions.head(12).iterrows():
            label = row.get("device_label", row["device_mac"][-5:])
            ax.text(float(row["x_hat"]) + 0.03, float(row["y_hat"]) + 0.03, label, fontsize=8, color="#9b2226")

    ax.legend(loc="upper right")
    fig.tight_layout()
    return fig


def plot_cad_heatmap(
    manifest: dict,
    current_predictions: pd.DataFrame,
    prediction_history: list[tuple[float, float]] | None,
    floorplan_image: Image.Image | None,
    image_alpha: float = 0.9,
    heatmap_alpha: float = 0.5,
    flip_vertical: bool = False,
    flip_horizontal: bool = False,
) -> plt.Figure:
    env = manifest["environment"]
    lx = float(env["length_m"])
    ly = float(env["width_m"])
    anchors = manifest.get("anchors", [])

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_xlim(0, lx)
    ax.set_ylim(0, ly)
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")
    ax.set_title("Plano CAD + heatmap de dispositivos")
    ax.grid(False)

    if floorplan_image is not None:
        img = np.array(floorplan_image.convert("RGBA"))
        if flip_vertical:
            img = np.flipud(img)
        if flip_horizontal:
            img = np.fliplr(img)
        ax.imshow(img, extent=[0, lx, 0, ly], origin="upper", aspect="auto", alpha=image_alpha)

    render_prediction_heatmap(ax, lx, ly, prediction_history, alpha=heatmap_alpha)

    for anchor in anchors:
        ax.scatter(anchor["x_m"], anchor["y_m"], marker="^", s=110, color="#005f73")
        ax.text(anchor["x_m"] + 0.04, anchor["y_m"] + 0.04, anchor["anchor_id"], color="#005f73")

    if current_predictions is not None and not current_predictions.empty:
        ax.scatter(current_predictions["x_hat"], current_predictions["y_hat"], s=120, marker="x", color="#bb3e03")
        for _, row in current_predictions.head(20).iterrows():
            label = row.get("device_label", row["device_mac"][-5:])
            ax.text(float(row["x_hat"]) + 0.03, float(row["y_hat"]) + 0.03, label, fontsize=8, color="#9b2226")

    fig.tight_layout()
    return fig


def init_anchor_editor(length_m: float, width_m: float, anchor_count: int, z_m: float = 2.0) -> None:
    st.session_state["anchor_editor_df"] = pd.DataFrame(default_anchor_layout(anchor_count, length_m, width_m, z_m))


def ensure_anchor_editor(length_m: float, width_m: float, anchor_count: int, z_m: float = 2.0) -> None:
    if "anchor_editor_df" not in st.session_state:
        init_anchor_editor(length_m, width_m, anchor_count, z_m)


st.title("NEXA-IPS - Captura guiada, entrenamiento e inferencia")
st.caption("Campana indoor con ESP32, dataset fijo por punto y heatmap de posicion.")

server_url = st.sidebar.text_input("Servidor FastAPI", "http://127.0.0.1:8000")
runs_dir = Path(st.sidebar.text_input("Directorio runs", "runs"))
auto_refresh = st.sidebar.checkbox("Auto refresh", value=False)
refresh_seconds = st.sidebar.slider("Refresh [s]", min_value=2, max_value=20, value=4, step=1)

status = None
server_error = None
try:
    status = api_request("GET", server_url, "/api/session/status")
except RuntimeError as exc:
    server_error = str(exc)

if server_error:
    st.error(server_error)

active = bool(status and status.get("active"))
active_manifest = status.get("manifest") if active else None
active_state = status.get("state") if active else None
inference_ready_runs = list_inference_ready_runs(runs_dir)

default_length = float(active_manifest["environment"]["length_m"]) if active else 6.0
default_width = float(active_manifest["environment"]["width_m"]) if active else 4.0
default_height = float(active_manifest["environment"]["height_m"]) if active else 2.8
default_samples = int(active_manifest["samples_per_anchor"]) if active else 10
default_planned_campaigns = int(active_manifest.get("planned_campaigns", 5)) if active else 5
default_mac = active_manifest["target_mac"] if active else "02:11:22:33:44:55"
default_session = active_manifest["session_id"] if active else "demo_s1"
default_campaign = active_manifest["campaign_id"] if active else "train_01"
default_anchor_count = len(active_manifest["anchors"]) if active else 3

ensure_anchor_editor(default_length, default_width, default_anchor_count)
if active:
    current_editor = st.session_state.get("anchor_editor_df")
    active_anchor_df = pd.DataFrame(active_manifest["anchors"])
    current_ids = current_editor["anchor_id"].tolist() if isinstance(current_editor, pd.DataFrame) and "anchor_id" in current_editor.columns else []
    active_ids = active_anchor_df["anchor_id"].tolist() if "anchor_id" in active_anchor_df.columns else []
    if current_editor is None or sorted(current_ids) != sorted(active_ids):
        st.session_state["anchor_editor_df"] = active_anchor_df

with st.expander("0. Activar campana entrenada para solo inferencia", expanded=not active):
    st.caption(
        "Este modo reutiliza una campana ya entrenada para inferencia online. "
        "No abre puntos nuevos ni obliga a repetir la medicion."
    )
    if not inference_ready_runs:
        st.info("No hay campanas entrenadas disponibles en el directorio runs/.")
    else:
        labels = [entry["label"] for entry in inference_ready_runs]
        default_index = 0
        if active and active_manifest:
            active_key = f"{active_manifest['session_id']} / {active_manifest['campaign_id']}"
            for idx, entry in enumerate(inference_ready_runs):
                manifest = entry["manifest"]
                entry_key = f"{manifest['session_id']} / {manifest['campaign_id']}"
                if entry_key == active_key:
                    default_index = idx
                    break
        selected_label = st.selectbox(
            "Campana entrenada",
            labels,
            index=default_index,
            key="inference_ready_campaign",
        )
        selected_entry = next(entry for entry in inference_ready_runs if entry["label"] == selected_label)
        selected_manifest = selected_entry["manifest"]
        st.caption(
            f"Sesion {selected_manifest['session_id']} | campana {selected_manifest['campaign_id']} | "
            f"{len(selected_manifest.get('anchors', []))} antenas | "
            f"muestras por antena: {selected_manifest['samples_per_anchor']}"
        )
        if st.button("Activar esta campana para solo inferencia"):
            if active_state and active_state.get("current_point"):
                st.error("Cierre el punto activo antes de cambiar de campana.")
            else:
                try:
                    api_request(
                        "POST",
                        server_url,
                        "/api/session/start",
                        manifest_to_start_payload(selected_manifest),
                    )
                    st.success(
                        "Campana reactivada para inferencia online. "
                        "Las antenas ESP32 ya pueden consultar configuracion y enviar datos."
                    )
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))

st.subheader("1. Configuracion de la campana")
with st.form("setup_form"):
    setup_col1, setup_col2, setup_col3 = st.columns(3)
    with setup_col1:
        session_id = st.text_input("session_id", value=default_session)
        target_mac = st.text_input("MAC del dispositivo objetivo", value=default_mac)
        samples_per_anchor = st.number_input("RSSI por antena y punto", min_value=1, max_value=100, value=default_samples)
    with setup_col2:
        campaign_id = st.text_input("campaign_id", value=default_campaign)
        room_length = st.number_input("Largo [m]", min_value=0.5, value=default_length, step=0.5)
        room_width = st.number_input("Ancho [m]", min_value=0.5, value=default_width, step=0.5)
    with setup_col3:
        anchor_count = st.number_input("Cantidad de antenas", min_value=1, max_value=8, value=default_anchor_count, step=1)
        planned_campaigns = st.number_input("Campanas planificadas en la sesion", min_value=1, max_value=20, value=default_planned_campaigns, step=1)
        room_height = st.number_input("Alto [m]", min_value=0.5, value=default_height, step=0.1)
        default_anchor_height = st.number_input("Altura de antenas por defecto [m]", min_value=0.1, value=2.0, step=0.1)

    editor_col1, editor_col2 = st.columns([3, 1])
    with editor_col2:
        reset_layout = st.form_submit_button("Cargar esquinas por defecto")
    if reset_layout:
        init_anchor_editor(float(room_length), float(room_width), int(anchor_count), float(default_anchor_height))

    st.caption("Si cambia la cantidad de antenas o las dimensiones, use 'Cargar esquinas por defecto' para regenerar la tabla.")

    anchor_df = st.data_editor(
        st.session_state["anchor_editor_df"],
        num_rows="dynamic",
        use_container_width=True,
        key="anchors_editor",
        column_config={
            "anchor_id": "antena_id",
            "x_m": "x_m",
            "y_m": "y_m",
            "z_m": "z_m",
            "model": "modelo",
            "notes": "notas",
        },
    )

    launch = st.form_submit_button("Guardar y activar campana")
    if launch:
        anchors_payload = []
        for row in anchor_df.fillna("").to_dict(orient="records"):
            anchor_id = str(row.get("anchor_id", "")).strip()
            if not anchor_id:
                continue
            anchors_payload.append(
                {
                    "anchor_id": anchor_id,
                    "x_m": float(row.get("x_m", 0.0)),
                    "y_m": float(row.get("y_m", 0.0)),
                    "z_m": float(row.get("z_m", default_anchor_height)),
                    "model": str(row.get("model", "")),
                    "notes": str(row.get("notes", "")),
                }
            )
        payload = {
            "session_id": session_id,
            "campaign_id": campaign_id,
            "target_mac": target_mac,
            "samples_per_anchor": int(samples_per_anchor),
            "planned_campaigns": int(planned_campaigns),
            "environment": {
                "length_m": float(room_length),
                "width_m": float(room_width),
                "height_m": float(room_height),
            },
            "anchors": anchors_payload,
        }
        try:
            api_request("POST", server_url, "/api/session/start", payload)
            st.success("Campana activada. Las antenas ya pueden solicitar configuracion al servidor.")
            st.session_state["anchor_editor_df"] = anchor_df
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))

if active:
    manifest = active_manifest
    state = active_state
    session_base = runs_dir / manifest["session_id"]
    run_base = runs_dir / manifest["session_id"] / manifest["campaign_id"]
    points_csv = run_base / "points.csv"
    anchors = [anchor["anchor_id"] for anchor in manifest["anchors"]]
    session_summary = summarize_session(session_base)
    current_campaign_model_path = latest_model_path(run_base)
    session_model_path = latest_session_model_path(session_base)
    model_scope_options = ["Campana actual"]
    if session_model_path is not None:
        model_scope_options.append("Sesion completa")
    default_model_scope = "Sesion completa" if session_model_path is not None else "Campana actual"

    st.subheader("2. Estado de la captura")
    status_col1, status_col2 = st.columns([1, 2])
    with status_col1:
        st.metric("Sesion", manifest["session_id"])
        st.metric("Campana", manifest["campaign_id"])
        st.metric("Campanas planificadas", session_summary["planned_campaigns"])
        st.metric("Campanas registradas", session_summary["registered_campaigns"])
        st.metric("MAC objetivo", manifest["target_mac"].upper())
        if state.get("current_point"):
            st.metric("Punto actual", state["current_point"]["point_id"])
        else:
            st.metric("Punto actual", "Sin punto activo")
    with status_col2:
        st.dataframe(antenna_status_display_frame(manifest, state), use_container_width=True)
        model_scope = st.radio(
            "Modelo para inferencia",
            model_scope_options,
            index=model_scope_options.index(default_model_scope),
            horizontal=True,
            key=f"model_scope::{manifest['session_id']}",
        )

    model_path = session_model_path if model_scope == "Sesion completa" and session_model_path is not None else current_campaign_model_path

    st.subheader("3. Captura punto por punto")
    current_point = state.get("current_point")
    point_col1, point_col2 = st.columns([2, 1])
    with point_col1:
        with st.form("point_form"):
            point_id = st.text_input("point_id", value=f"P{len(state.get('points', [])) + 1:02d}")
            point_x = st.number_input("Posicion x [m]", min_value=0.0, max_value=float(manifest["environment"]["length_m"]), value=0.5, step=0.1)
            point_y = st.number_input("Posicion y [m]", min_value=0.0, max_value=float(manifest["environment"]["width_m"]), value=0.5, step=0.1)
            point_z = st.number_input("Altura del dispositivo [m]", min_value=0.0, max_value=float(manifest["environment"]["height_m"]), value=1.0, step=0.1)
            start_point = st.form_submit_button("Comenzar captura en este punto")
            if start_point:
                try:
                    api_request(
                        "POST",
                        server_url,
                        "/api/points/start",
                        {"point_id": point_id, "x_m": point_x, "y_m": point_y, "z_m": point_z},
                    )
                    st.success("Punto iniciado. Esperando probe requests de la MAC configurada.")
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))

    with point_col2:
        if current_point:
            counts = current_point.get("counts", {})
            ready = current_point.get("complete", False)
            st.write("Conteo actual")
            for anchor_id in anchors:
                st.progress(
                    min(int(counts.get(anchor_id, 0)), int(manifest["samples_per_anchor"])) / int(manifest["samples_per_anchor"]),
                    text=f"{anchor_id}: {int(counts.get(anchor_id, 0))}/{int(manifest['samples_per_anchor'])}",
                )
            if ready:
                st.success("Este punto ya tiene todas las muestras necesarias.")
            finish_disabled = not ready
            if st.button("Cerrar punto y habilitar el siguiente", disabled=finish_disabled):
                try:
                    api_request("POST", server_url, "/api/points/finish", {"force": False})
                    st.success("Punto cerrado.")
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
        else:
            st.info("No hay un punto activo. Cargue coordenadas y presione comenzar.")

    st.subheader("4. Plano y heatmap")
    hist_key = f"pred_hist::{manifest['session_id']}::{manifest['campaign_id']}"
    if hist_key not in st.session_state:
        st.session_state[hist_key] = []

    prediction_history = st.session_state[hist_key]
    st.caption(f"Inferencia usando: {model_scope.lower()}")
    infer_cfg_row1_col1, infer_cfg_row1_col2, infer_cfg_row1_col3 = st.columns(3)
    with infer_cfg_row1_col1:
        min_total_packets = st.number_input(
            "Min paquetes por dispositivo",
            min_value=1,
            max_value=100,
            value=max(4, int(manifest["samples_per_anchor"]) // 2),
            step=1,
            key=f"min_packets::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    with infer_cfg_row1_col2:
        min_anchors_seen = st.number_input(
            "Min antenas vistas por dispositivo",
            min_value=1,
            max_value=max(1, len(anchors)),
            value=1 if len(anchors) == 1 else 2,
            step=1,
            key=f"min_anchors::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    with infer_cfg_row1_col3:
        max_packet_age_s = st.number_input(
            "Ventana de deteccion [s]",
            min_value=5,
            max_value=600,
            value=20,
            step=5,
            key=f"max_age::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    infer_cfg_row2_col1, infer_cfg_row2_col2, infer_cfg_row2_col3, infer_cfg_row2_col4 = st.columns(4)
    with infer_cfg_row2_col1:
        hold_last_age_s = st.number_input(
            "Memoria por antena [s]",
            min_value=5,
            max_value=600,
            value=60,
            step=5,
            key=f"hold_last::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    with infer_cfg_row2_col2:
        min_samples_per_anchor_seen = st.number_input(
            "Min muestras por antena",
            min_value=1,
            max_value=max(1, int(manifest["samples_per_anchor"])),
            value=min(3, int(manifest["samples_per_anchor"])),
            step=1,
            key=f"min_samples_anchor::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    with infer_cfg_row2_col3:
        min_anchors_with_min_samples = st.number_input(
            "Min antenas con ese minimo",
            min_value=1,
            max_value=max(1, len(anchors)),
            value=1 if len(anchors) == 1 else min(2, len(anchors)),
            step=1,
            key=f"min_anchors_with_samples::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    with infer_cfg_row2_col4:
        min_best_rssi = st.number_input(
            "RSSI max minimo [dBm]",
            min_value=-100,
            max_value=-20,
            value=-85,
            step=1,
            key=f"min_best_rssi::{manifest['session_id']}::{manifest['campaign_id']}",
        )
    strict_full_window = st.checkbox(
        "Exigir ventana completa por antena (sin relleno artificial)",
        value=True,
        key=f"strict_full_window::{manifest['session_id']}::{manifest['campaign_id']}",
    )
    st.caption(
        "La deteccion usa la ventana corta; cada antena puede conservar su ultima lectura valida dentro de la memoria configurada. "
        "Si se activa la ventana completa, la prediccion solo se realiza cuando cada antena dispone de todas las muestras reales requeridas."
    )
    if st.button("Reiniciar historial del heatmap", key=f"reset_heatmap::{manifest['session_id']}::{manifest['campaign_id']}"):
        st.session_state[hist_key] = []
        prediction_history = []
        st.rerun()

    current_predictions_df = pd.DataFrame()
    device_stats_df = pd.DataFrame()
    inference_error = None
    if model_path is not None:
        try:
            model, feat_cols = load_model(str(model_path))
            device_features_df, device_stats_df = build_live_feature_rows_for_all_devices(
                raw_base=run_base / "raw",
                anchors=anchors,
                samples_per_anchor=int(manifest["samples_per_anchor"]),
                min_total_packets=int(min_total_packets),
                min_anchors_seen=int(min_anchors_seen),
                max_age_s=float(max_packet_age_s),
                hold_last_age_s=float(hold_last_age_s),
                min_samples_per_anchor_seen=int(min_samples_per_anchor_seen),
                min_anchors_with_min_samples=int(min_anchors_with_min_samples),
                min_best_rssi=float(min_best_rssi),
                pad_missing=not bool(strict_full_window),
            )
            if not device_features_df.empty:
                missing = [column for column in feat_cols if column not in device_features_df.columns]
                for column in missing:
                    device_features_df[column] = 0.0
                X = device_features_df[feat_cols].to_numpy(dtype=float)
                y_hat = model.predict(X)

                current_predictions_df = device_features_df[
                    ["device_mac", "anchors_seen", "anchors_with_min_samples", "recent_anchors_seen", "total_packets"]
                ].copy()
                current_predictions_df["x_hat"] = y_hat[:, 0]
                current_predictions_df["y_hat"] = y_hat[:, 1]
                current_predictions_df["is_target"] = current_predictions_df["device_mac"].eq(manifest["target_mac"])
                current_predictions_df["device_label"] = np.where(
                    current_predictions_df["is_target"],
                    "TAG",
                    current_predictions_df["device_mac"].str[-5:],
                )
                if not device_stats_df.empty:
                    current_predictions_df = current_predictions_df.merge(
                        device_stats_df,
                        on=["device_mac", "anchors_seen", "anchors_with_min_samples", "recent_anchors_seen", "total_packets"],
                        how="left",
                    )

                prediction_history.extend(list(zip(current_predictions_df["x_hat"], current_predictions_df["y_hat"])))
                st.session_state[hist_key] = prediction_history[-1500:]
                prediction_history = st.session_state[hist_key]
        except Exception as exc:  # pragma: no cover
            inference_error = str(exc)

    live_col1, live_col2 = st.columns([2, 1])
    with live_col1:
        st.pyplot(
            plot_layout(
                manifest,
                state,
                prediction_history=prediction_history,
                current_predictions=current_predictions_df,
            ),
            use_container_width=True,
        )
    with live_col2:
        st.write("Puntos capturados")
        if points_csv.exists():
            st.dataframe(load_points(points_csv), use_container_width=True)
        else:
            st.info("Todavia no hay puntos cerrados.")

    st.subheader("5. Dataset y entrenamiento")
    st.caption(
        f"Sesion: {session_summary['registered_campaigns']}/{session_summary['planned_campaigns']} campanas registradas, "
        f"{session_summary['datasets_ready']} datasets listos, {session_summary['models_ready']} modelos de campana listos."
    )
    session_metrics_path = default_session_models_dir(session_base) / "metrics.csv"
    master_dataset_path = session_base / "dataset_master.csv"
    build_col1, build_col2, build_col3, build_col4, build_col5 = st.columns(5)
    with build_col1:
        if st.button("Construir dataset campana"):
            try:
                out_path, rows = build_dataset_file(run_base)
                st.success(f"Dataset listo: {out_path} ({rows} filas)")
            except SystemExit as exc:
                st.error(str(exc))
    with build_col2:
        if st.button("Entrenar modelo campana"):
            try:
                dataset_path, _ = build_dataset_file(run_base)
                model_out, metrics_out, metrics_df = train_dataset_file(dataset_path, default_models_dir(run_base))
                st.success(f"Modelo entrenado: {model_out}")
                st.dataframe(metrics_df, use_container_width=True)
                st.caption(f"Metricas guardadas en {metrics_out}")
                load_model.clear()
            except SystemExit as exc:
                st.error(str(exc))
    with build_col3:
        if st.button("Construir dataset maestro"):
            try:
                out_path, rows, included_campaigns, skipped_campaigns = build_session_dataset_file(session_base)
                st.success(f"Dataset maestro listo: {out_path} ({rows} filas)")
                st.caption(f"Campanas incluidas: {', '.join(included_campaigns)}")
                if skipped_campaigns:
                    st.warning("Campanas omitidas: " + " | ".join(skipped_campaigns))
            except SystemExit as exc:
                st.error(str(exc))
    with build_col4:
        if st.button("Entrenar modelo maestro"):
            try:
                dataset_path, _, included_campaigns, skipped_campaigns = build_session_dataset_file(session_base)
                model_out, metrics_out, metrics_df = train_dataset_file(dataset_path, default_session_models_dir(session_base))
                st.success(f"Modelo maestro entrenado: {model_out}")
                st.dataframe(metrics_df, use_container_width=True)
                st.caption(f"Metricas guardadas en {metrics_out}")
                st.caption(f"Campanas incluidas: {', '.join(included_campaigns)}")
                if skipped_campaigns:
                    st.warning("Campanas omitidas: " + " | ".join(skipped_campaigns))
                load_model.clear()
                st.rerun()
            except SystemExit as exc:
                st.error(str(exc))
    with build_col5:
        if st.button("Finalizar campana"):
            try:
                api_request("POST", server_url, "/api/session/finish")
                st.success("Campana finalizada.")
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))

    metrics_path = default_models_dir(run_base) / "metrics.csv"
    if metrics_path.exists():
        st.caption("Metricas de la campana actual")
        st.dataframe(pd.read_csv(metrics_path), use_container_width=True)
    if session_metrics_path.exists():
        st.caption("Metricas del modelo maestro de la sesion")
        st.dataframe(pd.read_csv(session_metrics_path), use_container_width=True)

    st.subheader("6. Inferencia online")
    if model_path is None:
        st.info("Todavia no hay un modelo disponible para la fuente seleccionada.")
    elif inference_error:
        st.error(f"No fue posible ejecutar la inferencia: {inference_error}")
    elif current_predictions_df.empty:
        st.warning("Aun no hay suficientes dispositivos observados para inferir con la heuristica actual.")
    else:
        infer_col1, infer_col2 = st.columns([1, 2])
        with infer_col1:
            st.metric("Dispositivos detectados", int(len(current_predictions_df)))
            target_rows = current_predictions_df[current_predictions_df["is_target"]]
            if not target_rows.empty:
                st.metric("TAG x [m]", f"{float(target_rows.iloc[0]['x_hat']):.2f}")
                st.metric("TAG y [m]", f"{float(target_rows.iloc[0]['y_hat']):.2f}")
            st.caption(f"Modelo: {model_path.name} ({model_scope.lower()})")
        with infer_col2:
            display_df = current_predictions_df.copy()
            for column in ["x_hat", "y_hat"]:
                display_df[column] = display_df[column].map(lambda value: round(float(value), 2))
            for column in ["best_rssi", "mean_rssi"]:
                if column in display_df.columns:
                    display_df[column] = display_df[column].map(lambda value: round(float(value), 1))
            ordered_cols = ["device_mac", "device_label", "is_target", "x_hat", "y_hat", "anchors_seen", "anchors_with_min_samples", "recent_anchors_seen", "total_packets", "best_rssi", "mean_rssi"] + [
                column for column in display_df.columns if column.endswith("_count")
            ]
            ordered_cols = [column for column in ordered_cols if column in display_df.columns]
            st.dataframe(
                display_df[ordered_cols].sort_values(
                    ["is_target", "anchors_with_min_samples", "anchors_seen", "total_packets"],
                    ascending=[False, False, False, False],
                ),
                use_container_width=True,
            )

    st.subheader("7. Plano CAD + heatmap")
    st.caption("Suba una imagen 2D exportada desde AutoCAD en PNG o JPG para superponer el heatmap de los dispositivos.")
    floorplan_col1, floorplan_col2 = st.columns([2, 1])
    with floorplan_col2:
        uploaded_floorplan = st.file_uploader(
            "Plano 2D",
            type=["png", "jpg", "jpeg", "bmp"],
            key=f"floorplan::{manifest['session_id']}::{manifest['campaign_id']}",
        )
        floorplan_flip_vertical = st.checkbox(
            "Invertir vertical",
            value=False,
            key=f"floorplan_flip_v::{manifest['session_id']}::{manifest['campaign_id']}",
        )
        floorplan_flip_horizontal = st.checkbox(
            "Invertir horizontal",
            value=False,
            key=f"floorplan_flip_h::{manifest['session_id']}::{manifest['campaign_id']}",
        )
        floorplan_image_alpha = st.slider(
            "Opacidad del plano",
            min_value=0.1,
            max_value=1.0,
            value=0.95,
            step=0.05,
            key=f"floorplan_alpha::{manifest['session_id']}::{manifest['campaign_id']}",
        )
        floorplan_heatmap_alpha = st.slider(
            "Opacidad del heatmap",
            min_value=0.1,
            max_value=1.0,
            value=0.45,
            step=0.05,
            key=f"floorplan_heat_alpha::{manifest['session_id']}::{manifest['campaign_id']}",
        )
        st.write("Dimensiones del ambiente", f"{manifest['environment']['length_m']} m x {manifest['environment']['width_m']} m")

    floorplan_image = None
    if uploaded_floorplan is not None:
        floorplan_image = Image.open(uploaded_floorplan)

    with floorplan_col1:
        st.pyplot(
            plot_cad_heatmap(
                manifest=manifest,
                current_predictions=current_predictions_df,
                prediction_history=prediction_history,
                floorplan_image=floorplan_image,
                image_alpha=float(floorplan_image_alpha),
                heatmap_alpha=float(floorplan_heatmap_alpha),
                flip_vertical=bool(floorplan_flip_vertical),
                flip_horizontal=bool(floorplan_flip_horizontal),
            ),
            use_container_width=True,
        )
        if floorplan_image is None:
            st.info("Todavia no hay un plano cargado. Exporte el plano desde AutoCAD como PNG/JPG y carguelo aqui.")

if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
