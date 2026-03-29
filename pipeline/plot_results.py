\
#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def cdf(x: np.ndarray):
    xs = np.sort(x)
    ys = np.arange(1, len(xs)+1)/len(xs)
    return xs, ys


def main():
    ap = argparse.ArgumentParser(description="Generar gráficos (CDF, scatter, heatmap simple)")
    ap.add_argument("--pred", required=True, help="predictions.csv")
    ap.add_argument("--outdir", default="plots")
    ap.add_argument("--lx", type=float, default=6.0)
    ap.add_argument("--ly", type=float, default=3.5)
    args = ap.parse_args()

    df = pd.read_csv(args.pred)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # CDF
    if "err_m" in df.columns and df["err_m"].notna().any():
        e = df["err_m"].dropna().to_numpy()
        xs, ys = cdf(e)
        fig, ax = plt.subplots(figsize=(6,4))
        ax.plot(xs, ys)
        ax.set_xlabel("Error euclídeo [m]")
        ax.set_ylabel("F(e)")
        ax.grid(True, alpha=0.3)
        ax.set_title("CDF del error")
        fig.tight_layout()
        fig.savefig(outdir / "cdf.png", dpi=200)
        plt.close(fig)

    # Scatter
    if "x_m" in df.columns and df["x_m"].notna().any():
        m = df["x_m"].notna() & df["y_m"].notna()
        fig, ax = plt.subplots(figsize=(6,4))
        ax.scatter(df.loc[m,"x_m"], df.loc[m,"y_m"], s=10, label="GT")
        ax.scatter(df.loc[m,"x_hat"], df.loc[m,"y_hat"], s=10, label="Pred")
        ax.set_xlim(0,args.lx); ax.set_ylim(0,args.ly)
        ax.set_xlabel("x [m]"); ax.set_ylabel("y [m]")
        ax.grid(True, alpha=0.3)
        ax.legend()
        ax.set_title("Plano: ground truth vs predicción")
        fig.tight_layout()
        fig.savefig(outdir / "scatter.png", dpi=200)
        plt.close(fig)

    # Heatmap simple: KDE sobre predicciones
    X = df[["x_hat","y_hat"]].dropna().to_numpy()
    if len(X) > 10:
        grid_x = np.linspace(0, args.lx, 180)
        grid_y = np.linspace(0, args.ly, 105)
        Z = np.zeros((len(grid_y), len(grid_x)))
        # kernel isotrópico simple
        sx, sy = 0.35, 0.25
        for xh, yh in X[-500:]:
            Z += np.exp(-(((grid_x - xh)**2)/(2*sx**2))[None,:] - (((grid_y - yh)**2)/(2*sy**2))[:,None])
        fig, ax = plt.subplots(figsize=(7,4))
        im = ax.imshow(Z, origin="lower", extent=[0,args.lx,0,args.ly], aspect="auto")
        ax.set_xlabel("x [m]"); ax.set_ylabel("y [m]")
        ax.set_title("Mapa de calor (densidad de predicción)")
        fig.colorbar(im, ax=ax, label="densidad relativa")
        fig.tight_layout()
        fig.savefig(outdir / "heatmap.png", dpi=200)
        plt.close(fig)

    print(f"[OK] Gráficos en {outdir}")


if __name__ == "__main__":
    main()
