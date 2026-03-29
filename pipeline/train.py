#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor, RandomForestRegressor
from sklearn.model_selection import GroupKFold
from sklearn.multioutput import MultiOutputRegressor
from sklearn.neighbors import KNeighborsRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR

try:
    from .utils import list_feature_columns
except ImportError:
    from utils import list_feature_columns


def euclid_err(y_true: np.ndarray, y_pred: np.ndarray) -> np.ndarray:
    return np.sqrt(((y_true - y_pred) ** 2).sum(axis=1))


def summarize_errors(errors: np.ndarray) -> dict:
    return {
        "mae_eucl": float(np.mean(errors)),
        "p50": float(np.percentile(errors, 50)),
        "p90": float(np.percentile(errors, 90)),
        "p95": float(np.percentile(errors, 95)),
        "rmse_eucl": float(np.sqrt(np.mean(errors**2))),
    }


def train_eval_with_groups(model, X: np.ndarray, y: np.ndarray, groups: np.ndarray) -> dict:
    unique_groups = np.unique(groups)
    if len(unique_groups) < 2:
        model.fit(X, y)
        yp = model.predict(X)
        errors = euclid_err(y, yp)
        summary = summarize_errors(errors)
        summary["eval_mode"] = "resubstitution"
        return summary

    n_splits = min(5, len(unique_groups))
    gkf = GroupKFold(n_splits=n_splits)
    errors = []
    for train_idx, test_idx in gkf.split(X, y, groups=groups):
        model.fit(X[train_idx], y[train_idx])
        yp = model.predict(X[test_idx])
        errors.append(euclid_err(y[test_idx], yp))

    summary = summarize_errors(np.concatenate(errors))
    summary["eval_mode"] = f"groupkfold_{n_splits}"
    return summary


def train_dataset_file(dataset_path: Path, outdir: Path, group_col: str = "point_id") -> tuple[Path, Path, pd.DataFrame]:
    df = pd.read_csv(dataset_path)
    df_lab = df.dropna(subset=["x_m", "y_m"]).copy()
    if df_lab.empty:
        raise SystemExit("El dataset no tiene labels (x_m, y_m)")

    feat_cols = list_feature_columns(df_lab)
    if not feat_cols:
        raise SystemExit("No se detectaron columnas de features en el dataset")

    X = df_lab[feat_cols].to_numpy(dtype=float)
    y = df_lab[["x_m", "y_m"]].to_numpy(dtype=float)

    if group_col in df_lab.columns:
        groups = df_lab[group_col].astype(str).to_numpy()
    else:
        groups = df_lab["point_id"].astype(str).to_numpy()

    outdir.mkdir(parents=True, exist_ok=True)

    models = {
        "knn_k3": Pipeline([("scaler", StandardScaler()), ("m", KNeighborsRegressor(n_neighbors=3, weights="distance"))]),
        "svr_rbf": Pipeline([("scaler", StandardScaler()), ("m", MultiOutputRegressor(SVR(C=20.0, gamma="scale", epsilon=0.05)))]),
        "rf_300": MultiOutputRegressor(RandomForestRegressor(n_estimators=300, random_state=7, n_jobs=-1)),
        "extratrees_500": MultiOutputRegressor(ExtraTreesRegressor(n_estimators=500, random_state=7, n_jobs=-1)),
        "gbrt": MultiOutputRegressor(GradientBoostingRegressor(random_state=7)),
    }

    rows = []
    for model_name, model in models.items():
        summary = train_eval_with_groups(model, X, y, groups)
        summary["model"] = model_name
        rows.append(summary)
        print(
            f"[{model_name}] mode={summary['eval_mode']} "
            f"MAE={summary['mae_eucl']:.3f} m p95={summary['p95']:.3f} m"
        )

    metrics = pd.DataFrame(rows).sort_values(["p95", "mae_eucl"]).reset_index(drop=True)
    metrics_path = outdir / "metrics.csv"
    metrics.to_csv(metrics_path, index=False)

    best_name = str(metrics.iloc[0]["model"])
    best_model = models[best_name]
    best_model.fit(X, y)
    model_path = outdir / f"best_model_{best_name}.joblib"
    joblib.dump({"model": best_model, "feat_cols": feat_cols}, model_path)

    return model_path, metrics_path, metrics


def main() -> None:
    ap = argparse.ArgumentParser(description="Entrenar y evaluar modelos RSSI -> posicion")
    ap.add_argument("--dataset", required=True, help="CSV dataset.csv")
    ap.add_argument("--outdir", default="models", help="Directorio de salida")
    ap.add_argument("--group_col", default="point_id", help="Columna para GroupKFold")
    args = ap.parse_args()

    model_path, metrics_path, _ = train_dataset_file(Path(args.dataset), Path(args.outdir), group_col=args.group_col)

    print(f"[OK] Metricas: {metrics_path}")
    print(f"[OK] Mejor modelo: {model_path}")


if __name__ == "__main__":
    main()
