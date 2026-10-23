import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

METRICS = ["width", "height", "aspect_ratio", "sharpness", "brightness",
           "contrast", "entropy", "letterbox_ratio", "file_size"]
RATE_COLS = ["rejected", "flag_blurry", "flag_dark", "flag_bright", "flag_letterbox"]


def rates_by(df: pd.DataFrame, col: str) -> pd.DataFrame:
    return df.groupby(col, observed=True).agg(
        images=("key", "size"), **{c: (c, "mean") for c in RATE_COLS}
    ).sort_values("images", ascending=False)


def intrinsic_groups(df: pd.DataFrame) -> pd.DataFrame:
    """Grups derivats només dels píxels (no calen metadades)."""
    df = df.copy()
    df["resolution_group"] = pd.qcut(df["width"] * df["height"], 4, duplicates="drop")
    df["brightness_group"] = pd.cut(df["brightness"], [0, 60, 120, 180, 255], include_lowest=True)
    df["aspect_group"] = pd.cut(
        df["aspect_ratio"], [0, 0.99, 1.4, 1.9, 10],
        labels=["vertical", "~4:3", "~16:9", "ultra-ample"],
    )
    return df


def main(csv_input: str, output_dir: str):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    q = pd.read_csv(csv_input)
    q["rejected"] = q["reject_reason"].notna()
    ok = q[q["error"].isna()]

    # 1. Distribucions tècniques
    for col in METRICS:
        ok[col].plot.hist(bins=50, title=col)
        plt.savefig(out / f"hist_{col}.png")
        plt.close()

    # 2. Resultat global i taxes de flags / duplicats
    q["reject_reason"].fillna("accepted").value_counts().plot.bar(title="Resultat de qualitat")
    plt.savefig(out / "outcomes.png")
    plt.close()
    flag_cols = [c for c in ok.columns if c.startswith(("flag_", "dup_"))]
    ok[flag_cols].mean().to_csv(out / "flag_rates.csv")

    # 3. Fairness intrínsec: el filtre afecta més algun grup d'imatges?
    grouped = intrinsic_groups(ok)
    for col in ["resolution_group", "brightness_group", "aspect_group"]:
        rates_by(grouped, col).to_csv(out / f"quality_by_{col}.csv")

    print("Informe generat a", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Informe de qualitat de la zona trusted d'imatges.")
    parser.add_argument("--csv_input", required=True,
                        help="Ruta del CSV de qualitat generat per build_trusted_images.py")
    parser.add_argument("--output_dir", default="reports/images",
                        help="Carpeta on es guardaran les figures i taules.")
    args = parser.parse_args()
    main(args.csv_input, args.output_dir)