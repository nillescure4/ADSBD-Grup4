import argparse
from pathlib import Path, PurePosixPath

import pandas as pd

from src.common.config import load_yaml
from src.common.minio_client import create_minio_client
from src.trusted.images.metrics import compute_metrics
from src.trusted.images.rules import apply_rules, flag_duplicates

SRC_PREFIX = "images/"
DST_PREFIX = "images/"


def parse_args():
    parser = argparse.ArgumentParser(description="Construeix la zona trusted d'imatges.")
    parser.add_argument(
        "--csv_output",
        default="reports/images/images_quality.csv",
        help="Ruta local on es guardarà el CSV de qualitat.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    csv_output = Path(args.csv_output)

    client = create_minio_client()
    buckets = load_yaml("storage.yml")["buckets"]
    bucket_fmt, bucket_trusted = buckets["formatted"], buckets["trusted"]

    rows = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_fmt, Prefix=SRC_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            print(f"Processing {key}...")
            row = {"key": key, "error": None}
            try:
                raw = client.get_object(Bucket=bucket_fmt, Key=key)["Body"].read()
                row.update(compute_metrics(raw))
            except Exception as e:
                row["error"] = str(e)
            rows.append(row)

    df = apply_rules(flag_duplicates(pd.DataFrame(rows)))
    accepted = df[df["reject_reason"].isna()]

    print("Processing accepted images...")
    # Còpia al servidor, sense tornar a descarregar les imatges
    for key in accepted["key"]:
        client.copy(
            CopySource={"Bucket": bucket_fmt, "Key": key},
            Bucket=bucket_trusted,
            Key=f"{DST_PREFIX}{PurePosixPath(key).name}",
        )

    csv_output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_output, index=False)
    print(f"CSV guardat a {csv_output}")

    print(f"Total: {len(df)} | Acceptades: {len(accepted)} | Rebutjades: {len(df) - len(accepted)}")
    print(df["reject_reason"].value_counts().to_string())


if __name__ == "__main__":
    main()