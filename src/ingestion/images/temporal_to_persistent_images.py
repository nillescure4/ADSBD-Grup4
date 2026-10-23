from src.common.config import load_yaml
from src.common.minio_client import create_minio_client
from pathlib import PurePosixPath

SRC_PREFIX = "temporal"
DST_PREFIX = "persistent"


def build_standard_name(source: str, dataset_name: str, key: str, last_modified) -> str:
    """Construeix source$dataset$nom$dd-mm-yyyy.format."""
    path = PurePosixPath(key)
    timestamp = last_modified.strftime("%d-%m-%Y")
    return f"{source}${dataset_name}${timestamp}${path.stem}{path.suffix.lower()}"


def move_dataset_to_persistent(client, bucket: str, dataset_name: str, dataset: dict) -> int:
    """Reanomena els objectes de temporal/<source>/ i els mou a persistent/images/."""
    source = dataset["source"]
    prefix = f"{SRC_PREFIX}/images/{dataset_name}/"
    paginator = client.get_paginator("list_objects_v2")
    count = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            new_name = build_standard_name(source, dataset_name, key, obj["LastModified"])
            new_key = f"{DST_PREFIX}/images/{new_name}"
            print(f"Processant {key} -> {new_key}")

            client.copy(
                CopySource={"Bucket": bucket, "Key": key},
                Bucket=bucket,
                Key=new_key,
            )
            client.delete_object(Bucket=bucket, Key=key)
            count += 1

    return count


def main():
    client = create_minio_client()
    bucket = load_yaml("storage.yml")["buckets"]["landing"]
    datasets = load_yaml("datasets.yml")["datasets"]

    for name, dataset in datasets.items():
        if dataset["modality"] != "image":
            continue
        print(f"Movent dataset '{name}' a persistent...")
        n = move_dataset_to_persistent(client, bucket, name, dataset)
        print(f"[OK] {n} fitxers moguts per '{name}'")


if __name__ == "__main__":
    main()