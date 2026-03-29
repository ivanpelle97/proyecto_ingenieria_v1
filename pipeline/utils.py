from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


NON_FEATURE_COLUMNS = {
    "session_id",
    "campaign_id",
    "point_id",
    "x_m",
    "y_m",
    "z_m",
    "status",
    "started_at",
    "capture_complete_at",
    "completed_at",
    "samples_per_anchor",
    "target_mac",
}


def normalize_mac(value: str) -> str:
    mac = value.strip().lower()
    parts = mac.split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError(f"MAC invalida: {value}")
    [int(part, 16) for part in parts]
    return mac


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_jsonl(path: Path) -> List[dict]:
    out = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def default_anchor_layout(anchor_count: int, length_m: float, width_m: float, z_m: float = 2.0) -> List[dict]:
    if anchor_count < 1:
        return []

    corners = [
        (0.0, 0.0),
        (float(length_m), 0.0),
        (0.0, float(width_m)),
        (float(length_m), float(width_m)),
    ]

    anchors = []
    for idx in range(anchor_count):
        if idx < len(corners):
            x_m, y_m = corners[idx]
        else:
            frac = (idx - len(corners) + 1) / max(1, anchor_count - len(corners) + 1)
            x_m = float(length_m) * frac
            y_m = float(width_m) * 0.5
        anchors.append(
            {
                "anchor_id": f"A{idx + 1}",
                "x_m": x_m,
                "y_m": y_m,
                "z_m": float(z_m),
                "model": "ESP32",
                "notes": "",
            }
        )
    return anchors


def anchor_ids_from_manifest(manifest: dict) -> List[str]:
    return [anchor["anchor_id"] for anchor in manifest.get("anchors", [])]


def expand_batches_to_packets(batches: List[dict]) -> pd.DataFrame:
    """
    Convierte batches JSONL a una tabla por paquete y estima ts_pkt usando ts_us.
    """
    rows = []
    for batch in batches:
        ts_server = pd.to_datetime(batch["ts_server"])
        packets = batch.get("packets", [])
        if not packets:
            continue
        ts_us_list = [int(packet.get("ts_us", 0)) for packet in packets]
        tmax = max(ts_us_list) if ts_us_list else 0
        for packet in packets:
            ts_us = int(packet.get("ts_us", 0))
            dt_s = (tmax - ts_us) / 1_000_000.0
            ts_pkt = ts_server - pd.to_timedelta(dt_s, unit="s")
            rows.append(
                {
                    "ts_server": ts_server,
                    "ts_pkt": ts_pkt,
                    "anchor_id": batch["anchor_id"],
                    "rssi": int(packet["rssi"]),
                    "channel": int(packet["channel"]),
                    "addr1": packet["addr1"],
                    "addr2": packet["addr2"],
                    "addr3": packet["addr3"],
                }
            )
    if not rows:
        return pd.DataFrame(columns=["ts_pkt", "anchor_id", "rssi", "channel", "addr1", "addr2", "addr3"])
    return pd.DataFrame(rows)


def iqr(values: np.ndarray) -> float:
    if values.size == 0:
        return float("nan")
    q1 = np.quantile(values, 0.25)
    q3 = np.quantile(values, 0.75)
    return float(q3 - q1)


def window_aggregate(df: pd.DataFrame, window_s: float) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("ts_pkt").copy()
    t0 = df["ts_pkt"].min()
    rel_s = (df["ts_pkt"] - t0).dt.total_seconds()
    df["win_id"] = (rel_s // window_s).astype(int)
    df["t_win"] = t0 + pd.to_timedelta(df["win_id"] * window_s, unit="s")

    grouped = df.groupby(["t_win", "anchor_id"], as_index=False)
    agg = grouped["rssi"].agg(rssi_med="median", rssi_mean="mean", rssi_std="std", n="count")
    iqr_vals = grouped["rssi"].apply(lambda sample: iqr(sample.to_numpy())).reset_index(name="rssi_iqr")
    return agg.merge(iqr_vals, on=["t_win", "anchor_id"], how="left")


def pivot_features(agg: pd.DataFrame, anchors: List[str]) -> pd.DataFrame:
    if agg.empty:
        cols = ["t_win"] + sum([[f"{anchor}_med", f"{anchor}_iqr", f"{anchor}_n"] for anchor in anchors], [])
        return pd.DataFrame(columns=cols)

    med = agg.pivot(index="t_win", columns="anchor_id", values="rssi_med")
    iqr_df = agg.pivot(index="t_win", columns="anchor_id", values="rssi_iqr")
    n_df = agg.pivot(index="t_win", columns="anchor_id", values="n")

    for anchor in anchors:
        if anchor not in med.columns:
            med[anchor] = np.nan
        if anchor not in iqr_df.columns:
            iqr_df[anchor] = np.nan
        if anchor not in n_df.columns:
            n_df[anchor] = np.nan

    med = med[anchors].add_suffix("_med")
    iqr_df = iqr_df[anchors].add_suffix("_iqr")
    n_df = n_df[anchors].add_suffix("_n")

    out = pd.concat([med, iqr_df, n_df], axis=1).reset_index()
    for anchor in anchors:
        out[f"{anchor}_med"] = out[f"{anchor}_med"].fillna(-100.0)
        out[f"{anchor}_iqr"] = out[f"{anchor}_iqr"].fillna(0.0)
        out[f"{anchor}_n"] = out[f"{anchor}_n"].fillna(0).astype(int)
    return out


def load_gt_intervals(path: Path) -> pd.DataFrame:
    gt = pd.read_csv(path)
    gt["t_start"] = pd.to_datetime(gt["t_start"])
    gt["t_end"] = pd.to_datetime(gt["t_end"])
    return gt.sort_values("t_start")


def assign_groundtruth(features: pd.DataFrame, gt: pd.DataFrame) -> pd.DataFrame:
    if features.empty:
        return features

    xs = np.full(len(features), np.nan)
    ys = np.full(len(features), np.nan)
    pid = np.array([""] * len(features), dtype=object)

    t = features["t_win"].to_numpy()
    starts = gt["t_start"].to_numpy()
    ends = gt["t_end"].to_numpy()

    for idx in range(len(gt)):
        mask = (t >= starts[idx]) & (t < ends[idx])
        xs[mask] = float(gt.iloc[idx]["x_m"])
        ys[mask] = float(gt.iloc[idx]["y_m"])
        pid[mask] = str(gt.iloc[idx]["point_id"])

    out = features.copy()
    out["x_m"] = xs
    out["y_m"] = ys
    out["point_id"] = pid
    return out


def load_training_samples(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(
            columns=[
                "ts_server",
                "session_id",
                "campaign_id",
                "point_id",
                "x_m",
                "y_m",
                "z_m",
                "anchor_id",
                "sample_index",
                "rssi",
                "channel",
                "ts_us",
                "addr1",
                "addr2",
                "addr3",
            ]
        )
    df = pd.read_csv(path)
    if "ts_server" in df.columns:
        df["ts_server"] = pd.to_datetime(df["ts_server"])
    return df


def load_points(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(
            columns=[
                "point_id",
                "x_m",
                "y_m",
                "z_m",
                "started_at",
                "capture_complete_at",
                "completed_at",
                "status",
                "samples_per_anchor",
            ]
        )
    df = pd.read_csv(path)
    for column in ["started_at", "capture_complete_at", "completed_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column])
    return df


def complete_points(points_df: pd.DataFrame) -> pd.DataFrame:
    if points_df.empty:
        return points_df
    if "status" not in points_df.columns:
        return points_df
    return points_df[points_df["status"] == "complete"].copy()


def _point_feature_block(anchor_samples: pd.DataFrame, anchor_id: str, samples_per_anchor: int) -> Dict[str, float]:
    rows = anchor_samples.sort_values(["sample_index", "ts_server"]).head(samples_per_anchor)
    if len(rows) < samples_per_anchor:
        raise ValueError(f"Muestras insuficientes para {anchor_id}")

    rssis = rows["rssi"].astype(float).tolist()
    channels = rows["channel"].astype(float).tolist()

    block: Dict[str, float] = {}
    for idx, rssi in enumerate(rssis, start=1):
        block[f"{anchor_id}_rssi_{idx:02d}"] = float(rssi)
    block[f"{anchor_id}_rssi_mean"] = float(np.mean(rssis))
    block[f"{anchor_id}_rssi_std"] = float(np.std(rssis))
    block[f"{anchor_id}_rssi_min"] = float(np.min(rssis))
    block[f"{anchor_id}_rssi_max"] = float(np.max(rssis))
    block[f"{anchor_id}_rssi_median"] = float(np.median(rssis))
    block[f"{anchor_id}_channel_mean"] = float(np.mean(channels))
    block[f"{anchor_id}_sample_count"] = float(len(rssis))
    return block


def build_point_dataset(
    samples_df: pd.DataFrame,
    points_df: pd.DataFrame,
    anchors: List[str],
    samples_per_anchor: int,
) -> pd.DataFrame:
    if samples_df.empty:
        return pd.DataFrame()

    valid_points = complete_points(points_df)
    if valid_points.empty:
        base_points = (
            samples_df[["point_id", "x_m", "y_m", "z_m", "session_id", "campaign_id"]]
            .drop_duplicates()
            .sort_values("point_id")
        )
    else:
        base_points = valid_points.merge(
            samples_df[["point_id", "session_id", "campaign_id"]].drop_duplicates(),
            on="point_id",
            how="left",
        )

    rows = []
    for _, point in base_points.iterrows():
        point_id = point["point_id"]
        point_samples = samples_df[samples_df["point_id"] == point_id].copy()
        if point_samples.empty:
            continue

        row = {
            "session_id": point_samples["session_id"].iloc[0],
            "campaign_id": point_samples["campaign_id"].iloc[0],
            "point_id": point_id,
            "x_m": float(point_samples["x_m"].iloc[0]),
            "y_m": float(point_samples["y_m"].iloc[0]),
            "z_m": float(point_samples["z_m"].iloc[0]),
            "samples_per_anchor": int(samples_per_anchor),
        }

        try:
            for anchor_id in anchors:
                anchor_samples = point_samples[point_samples["anchor_id"] == anchor_id]
                row.update(_point_feature_block(anchor_samples, anchor_id, samples_per_anchor))
        except ValueError:
            continue

        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("point_id").reset_index(drop=True)


def list_feature_columns(df: pd.DataFrame) -> List[str]:
    return [
        column
        for column in df.columns
        if column not in NON_FEATURE_COLUMNS and pd.api.types.is_numeric_dtype(df[column])
    ]


def filter_packets_for_mac(df_packets: pd.DataFrame, target_mac: str) -> pd.DataFrame:
    target = normalize_mac(target_mac)
    mask = (df_packets["addr1"] == target) | (df_packets["addr2"] == target) | (df_packets["addr3"] == target)
    return df_packets[mask].copy()


def infer_device_mac(df_packets: pd.DataFrame) -> pd.DataFrame:
    if df_packets.empty:
        out = df_packets.copy()
        out["device_mac"] = pd.Series(dtype=object)
        return out

    out = df_packets.copy()
    out["device_mac"] = out["addr2"].astype(str).str.lower()
    out = out[out["device_mac"] != "ff:ff:ff:ff:ff:ff"].copy()
    return out


def load_recent_packets_from_raw(
    raw_base: Path,
    anchors: List[str],
    limit_lines: int = 300,
    max_age_s: float = 45.0,
) -> pd.DataFrame:
    packets = []
    for anchor_id in anchors:
        anchor_file = raw_base / f"{anchor_id}.jsonl"
        if not anchor_file.exists():
            continue
        lines = anchor_file.read_text(encoding="utf-8").splitlines()[-limit_lines:]
        batches = [json.loads(line) for line in lines if line.strip()]
        if not batches:
            continue
        df_anchor = expand_batches_to_packets(batches)
        if df_anchor.empty:
            continue
        df_anchor["anchor_id"] = anchor_id
        packets.append(df_anchor)
    if not packets:
        return pd.DataFrame()
    out = pd.concat(packets, ignore_index=True)
    if out.empty:
        return out
    if max_age_s > 0:
        latest_ts = out["ts_pkt"].max()
        cutoff_ts = latest_ts - pd.to_timedelta(float(max_age_s), unit="s")
        out = out[out["ts_pkt"] >= cutoff_ts].copy()
    return out


def _build_feature_row_from_packets(
    packets: pd.DataFrame,
    anchors: List[str],
    samples_per_anchor: int,
    pad_missing: bool = True,
) -> tuple[pd.DataFrame, Dict[str, int]]:
    if packets.empty:
        return pd.DataFrame(), {anchor_id: 0 for anchor_id in anchors}

    packets = packets.sort_values("ts_pkt")
    row: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for anchor_id in anchors:
        anchor_packets = packets[packets["anchor_id"] == anchor_id].sort_values("ts_pkt")
        latest = anchor_packets.tail(samples_per_anchor)
        counts[anchor_id] = int(len(latest))
        rssis = latest["rssi"].astype(float).tolist()

        if len(rssis) < samples_per_anchor:
            if not pad_missing:
                return pd.DataFrame(), counts
            rssis = ([-100.0] * (samples_per_anchor - len(rssis))) + rssis

        channels = latest["channel"].astype(float).tolist()
        if len(channels) < samples_per_anchor:
            if not pad_missing:
                return pd.DataFrame(), counts
            channels = ([0.0] * (samples_per_anchor - len(channels))) + channels

        for idx, rssi in enumerate(rssis, start=1):
            row[f"{anchor_id}_rssi_{idx:02d}"] = float(rssi)
        row[f"{anchor_id}_rssi_mean"] = float(np.mean(rssis))
        row[f"{anchor_id}_rssi_std"] = float(np.std(rssis))
        row[f"{anchor_id}_rssi_min"] = float(np.min(rssis))
        row[f"{anchor_id}_rssi_max"] = float(np.max(rssis))
        row[f"{anchor_id}_rssi_median"] = float(np.median(rssis))
        row[f"{anchor_id}_channel_mean"] = float(np.mean(channels))
        row[f"{anchor_id}_sample_count"] = float(counts[anchor_id])

    return pd.DataFrame([row]), counts


def build_live_feature_row(
    raw_base: Path,
    anchors: List[str],
    target_mac: str,
    samples_per_anchor: int,
    limit_lines: int = 300,
    max_age_s: float = 45.0,
    pad_missing: bool = True,
) -> tuple[pd.DataFrame, Dict[str, int]]:
    packets = load_recent_packets_from_raw(raw_base, anchors, limit_lines=limit_lines, max_age_s=max_age_s)
    if packets.empty:
        return pd.DataFrame(), {anchor_id: 0 for anchor_id in anchors}

    packets = filter_packets_for_mac(packets, target_mac)
    if packets.empty:
        return pd.DataFrame(), {anchor_id: 0 for anchor_id in anchors}

    return _build_feature_row_from_packets(
        packets,
        anchors,
        samples_per_anchor,
        pad_missing=pad_missing,
    )


def build_live_feature_rows_for_all_devices(
    raw_base: Path,
    anchors: List[str],
    samples_per_anchor: int,
    limit_lines: int = 300,
    min_total_packets: int = 6,
    min_anchors_seen: int = 2,
    max_age_s: float = 20.0,
    hold_last_age_s: float = 60.0,
    min_samples_per_anchor_seen: int = 3,
    min_anchors_with_min_samples: int = 2,
    min_best_rssi: float = -85.0,
    pad_missing: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    hold_last_age_s = max(float(hold_last_age_s), float(max_age_s))
    packets = load_recent_packets_from_raw(raw_base, anchors, limit_lines=limit_lines, max_age_s=hold_last_age_s)
    if packets.empty:
        return pd.DataFrame(), pd.DataFrame()

    packets = infer_device_mac(packets)
    if packets.empty:
        return pd.DataFrame(), pd.DataFrame()

    latest_ts = packets["ts_pkt"].max()
    cutoff_ts = latest_ts - pd.to_timedelta(float(max_age_s), unit="s")
    recent_packets = packets[packets["ts_pkt"] >= cutoff_ts].copy()
    if recent_packets.empty:
        return pd.DataFrame(), pd.DataFrame()

    feature_rows = []
    stats_rows = []
    for device_mac, recent_device_packets in recent_packets.groupby("device_mac"):
        recent_counts_by_anchor = recent_device_packets.groupby("anchor_id").size().to_dict()
        recent_anchors_seen = sum(1 for anchor_id in anchors if recent_counts_by_anchor.get(anchor_id, 0) > 0)
        recent_total_packets = int(len(recent_device_packets))
        recent_best_rssi = float(recent_device_packets["rssi"].max())
        recent_mean_rssi = float(recent_device_packets["rssi"].mean())
        if recent_total_packets < min_total_packets:
            continue
        if recent_best_rssi < min_best_rssi:
            continue
        device_packets = packets[packets["device_mac"] == device_mac].copy()
        counts_by_anchor = device_packets.groupby("anchor_id").size().to_dict()
        anchors_seen = sum(1 for anchor_id in anchors if counts_by_anchor.get(anchor_id, 0) > 0)
        anchors_with_min_samples = sum(
            1 for anchor_id in anchors if counts_by_anchor.get(anchor_id, 0) >= int(min_samples_per_anchor_seen)
        )
        if anchors_seen < min_anchors_seen:
            continue
        if anchors_with_min_samples < min_anchors_with_min_samples:
            continue

        feature_df, counts = _build_feature_row_from_packets(
            device_packets,
            anchors,
            samples_per_anchor,
            pad_missing=pad_missing,
        )
        if feature_df.empty:
            continue

        feature_row = feature_df.iloc[0].to_dict()
        feature_row["device_mac"] = device_mac
        feature_row["anchors_seen"] = anchors_seen
        feature_row["anchors_with_min_samples"] = anchors_with_min_samples
        feature_row["recent_anchors_seen"] = recent_anchors_seen
        feature_row["total_packets"] = recent_total_packets
        feature_row["best_rssi"] = recent_best_rssi
        feature_row["mean_rssi"] = recent_mean_rssi
        feature_rows.append(feature_row)

        stats_row = {
            "device_mac": device_mac,
            "anchors_seen": anchors_seen,
            "anchors_with_min_samples": anchors_with_min_samples,
            "recent_anchors_seen": recent_anchors_seen,
            "total_packets": recent_total_packets,
            "best_rssi": recent_best_rssi,
            "mean_rssi": recent_mean_rssi,
        }
        for anchor_id in anchors:
            stats_row[f"{anchor_id}_count"] = int(counts.get(anchor_id, 0))
        stats_rows.append(stats_row)

    if not feature_rows:
        return pd.DataFrame(), pd.DataFrame()
    return (
        pd.DataFrame(feature_rows),
        pd.DataFrame(stats_rows)
        .sort_values(["anchors_with_min_samples", "anchors_seen", "total_packets"], ascending=[False, False, False])
        .reset_index(drop=True),
    )
