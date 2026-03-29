\
#!/usr/bin/env python3
"""
Marca intervalos de ground truth (GT) por punto.

Genera/actualiza:
  runs/<session>/<campaign>/gt_intervals.csv
con columnas:
  point_id,x_m,y_m,t_start,t_end

Uso típico:
  python scripts/mark_point.py --session living_S1 --campaign L01_base --point P05 --x 3.0 --y 1.75 --duration 60
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

TZ = ZoneInfo("America/Argentina/Cordoba")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--session", required=True)
    ap.add_argument("--campaign", required=True)
    ap.add_argument("--point", required=True, help="ID punto, p.ej. P05")
    ap.add_argument("--x", type=float, required=True)
    ap.add_argument("--y", type=float, required=True)
    ap.add_argument("--duration", type=float, default=60.0, help="Segundos a mantener el TAG estacionario")
    args = ap.parse_args()

    base = Path(args.runs_dir) / args.session / args.campaign
    base.mkdir(parents=True, exist_ok=True)
    out = base / "gt_intervals.csv"

    t_start = datetime.now(tz=TZ)
    t_end = t_start + timedelta(seconds=args.duration)

    print(f"[GT] Punto {args.point} ({args.x:.2f},{args.y:.2f})  start={t_start.isoformat()}  end={t_end.isoformat()}")
    print("[GT] Mantener el TAG estacionario hasta finalizar el intervalo...")

    # Espera activa simple
    while datetime.now(tz=TZ) < t_end:
        pass

    row = {
        "point_id": args.point,
        "x_m": args.x,
        "y_m": args.y,
        "t_start": t_start.isoformat(),
        "t_end": t_end.isoformat(),
    }

    if out.exists():
        df = pd.read_csv(out)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])

    df.to_csv(out, index=False)
    print(f"[OK] GT actualizado: {out} (filas={len(df)})")


if __name__ == "__main__":
    main()
