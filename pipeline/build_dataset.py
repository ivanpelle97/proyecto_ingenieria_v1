#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

try:
    from .utils import (
        anchor_ids_from_manifest,
        build_point_dataset,
        load_json,
        load_points,
        load_training_samples,
    )
except ImportError:
    from utils import (
        anchor_ids_from_manifest,
        build_point_dataset,
        load_json,
        load_points,
        load_training_samples,
    )


def build_dataset_file(base: Path, anchors: list[str] | None = None, samples_per_anchor: int = 0) -> tuple[Path, int]:
    manifest = load_json(base / "experiment.json")
    if not anchors:
        anchors = anchor_ids_from_manifest(manifest)
    if not anchors:
        raise SystemExit("No se encontraron anclas configuradas")

    samples_per_anchor = samples_per_anchor or int(manifest["samples_per_anchor"])

    samples_df = load_training_samples(base / "samples.csv")
    if samples_df.empty:
        raise SystemExit("No existe samples.csv o no contiene muestras")

    points_df = load_points(base / "points.csv")
    dataset = build_point_dataset(samples_df, points_df, anchors, samples_per_anchor)
    if dataset.empty:
        raise SystemExit("No se pudo construir el dataset. Verifica que cada punto tenga muestras completas por ancla.")

    out_path = base / "dataset.csv"
    dataset.to_csv(out_path, index=False)
    return out_path, len(dataset)


def load_session_plan(session_base: Path) -> dict:
    path = session_base / "session_plan.json"
    if not path.exists():
        return {}
    return load_json(path)


def list_campaign_bases(session_base: Path) -> list[Path]:
    if not session_base.exists():
        return []
    return sorted(
        [
            path
            for path in session_base.iterdir()
            if path.is_dir() and (path / "experiment.json").exists()
        ],
        key=lambda path: path.name,
    )


def build_session_dataset_file(session_base: Path) -> tuple[Path, int, list[str], list[str]]:
    campaign_bases = list_campaign_bases(session_base)
    if not campaign_bases:
        raise SystemExit("No se encontraron campanas dentro de la sesion")

    frames: list[pd.DataFrame] = []
    included_campaigns: list[str] = []
    skipped_campaigns: list[str] = []

    for campaign_base in campaign_bases:
        campaign_id = campaign_base.name
        try:
            dataset_path, _ = build_dataset_file(campaign_base)
            frame = pd.read_csv(dataset_path)
            if frame.empty:
                skipped_campaigns.append(f"{campaign_id}: dataset vacio")
                continue
            frame["campaign_point_id"] = frame["campaign_id"].astype(str) + "::" + frame["point_id"].astype(str)
            frames.append(frame)
            included_campaigns.append(campaign_id)
        except SystemExit as exc:
            skipped_campaigns.append(f"{campaign_id}: {exc}")

    if not frames:
        raise SystemExit("No se pudo construir dataset maestro con las campanas disponibles")

    master_df = pd.concat(frames, ignore_index=True)
    master_df = master_df.sort_values(["campaign_id", "point_id"]).reset_index(drop=True)
    out_path = session_base / "dataset_master.csv"
    master_df.to_csv(out_path, index=False)
    return out_path, len(master_df), included_campaigns, skipped_campaigns


def main() -> None:
    ap = argparse.ArgumentParser(description="Construir dataset supervisado desde samples.csv")
    ap.add_argument("--runs_dir", default="runs", help="Directorio runs/")
    ap.add_argument("--session", required=True, help="session_id")
    ap.add_argument("--campaign", default="", help="campaign_id; si se omite junto con --all_campaigns, se construye dataset maestro de sesion")
    ap.add_argument("--all_campaigns", action="store_true", help="Construir dataset maestro con todas las campanas de la sesion")
    ap.add_argument("--anchors", default="", help="Lista opcional de anclas separadas por coma")
    ap.add_argument("--samples_per_anchor", type=int, default=0, help="Si es 0, se toma de experiment.json")
    args = ap.parse_args()

    session_base = Path(args.runs_dir) / args.session
    if args.all_campaigns:
        out_path, rows, included_campaigns, skipped_campaigns = build_session_dataset_file(session_base)
        print(f"[OK] Dataset maestro guardado en {out_path} (filas={rows})")
        print(f"[OK] Campanas incluidas: {', '.join(included_campaigns)}")
        if skipped_campaigns:
            print(f"[WARN] Campanas omitidas: {' | '.join(skipped_campaigns)}")
        return

    if not args.campaign:
        raise SystemExit("Debe indicar --campaign o usar --all_campaigns")

    base = session_base / args.campaign
    anchors = [anchor.strip() for anchor in args.anchors.split(",") if anchor.strip()]
    out_path, rows = build_dataset_file(base, anchors=anchors, samples_per_anchor=args.samples_per_anchor)
    print(f"[OK] Dataset guardado en {out_path} (filas={rows})")


if __name__ == "__main__":
    main()
