\
#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd


def euclid_err(y_true: np.ndarray, y_pred: np.ndarray) -> np.ndarray:
    return np.sqrt(((y_true - y_pred) ** 2).sum(axis=1))


def main():
    ap = argparse.ArgumentParser(description="Inferencia batch sobre dataset.csv")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--model", required=True, help="models/best_model_*.joblib")
    ap.add_argument("--out", default="predictions.csv")
    args = ap.parse_args()

    df = pd.read_csv(args.dataset)
    pack = joblib.load(args.model)
    model = pack["model"]
    feat_cols = pack["feat_cols"]

    X = df[feat_cols].to_numpy(dtype=float)
    yp = model.predict(X)
    df_out = df.copy()
    df_out["x_hat"] = yp[:,0]
    df_out["y_hat"] = yp[:,1]

    if "x_m" in df_out.columns and df_out["x_m"].notna().any():
        m = df_out["x_m"].notna() & df_out["y_m"].notna()
        e = euclid_err(df_out.loc[m, ["x_m","y_m"]].to_numpy(), df_out.loc[m, ["x_hat","y_hat"]].to_numpy())
        df_out.loc[m, "err_m"] = e

    out_path = Path(args.out)
    df_out.to_csv(out_path, index=False)
    print(f"[OK] Predicciones guardadas en {out_path}")


if __name__ == "__main__":
    main()
