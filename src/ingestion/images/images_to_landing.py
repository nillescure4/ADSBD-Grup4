from src.common.config import load_yaml
from src.common.minio_client import create_minio_client
import os
from pathlib import Path

PROJECT_DIR = os.getenv("PROJECT_DIR")
REQUIRED_KEYS = ("modality", "directory", "source")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


def iter_image_files(root: Path):
    """Totes les imatges sota root, planes o niuades a qualsevol profunditat."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.startswith(".") \
                and path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def load_images_to_landing(name: str, dataset: dict) -> None:
    """Puja les imatges del dataset a landing/temporal/images/<dataset>/."""
    missing = [k for k in REQUIRED_KEYS if k not in dataset]
    if missing:
        raise ValueError(f"Dataset '{name}': falten les claus {missing} a datasets.yml")
    if dataset["modality"] != "image":
        raise ValueError(f"El dataset '{name}' no és d'imatges.")
    if not PROJECT_DIR:
        raise EnvironmentError("La variable d'entorn PROJECT_DIR no està definida.")

    images_path = Path(PROJECT_DIR) / "data" / "landing" / dataset["directory"]
    if not images_path.is_dir():
        raise FileNotFoundError(f"No existeix el directori {images_path}")

    client = create_minio_client()
    bucket_landing = load_yaml("storage.yml")["buckets"]["landing"]
    prefix = f"temporal/images/{name}/"

    count = 0
    for image_file in iter_image_files(images_path):
        client.upload_file(
            Filename=str(image_file),
            Bucket=bucket_landing,
            Key=f"{prefix}{image_file.name}",
        )
        count += 1

    print(f"[OK] {name}: {count} imatges pujades a '{bucket_landing}/{prefix}'")


def main():
    datasets = load_yaml("datasets.yml")["datasets"]
    image_datasets = {n: d for n, d in datasets.items() if d.get("modality") == "image"}
    if not image_datasets:
        print("No s'ha trobat cap dataset d'imatges a datasets.yml")
        return

    for name, dataset in image_datasets.items():
        print(f"Processant dataset '{name}'...")
        try:
            load_images_to_landing(name, dataset)
        except Exception as e:
            print(f"[ERROR] {name}: {e}")


if __name__ == "__main__":
    main()