# ADSBD - Grup 4

Pipeline local de gestió de dades basat en una arquitectura per zones. MinIO
actua com a emmagatzematge d'objectes compatible amb S3 i el codi Python del
repositori s'encarrega d'ingerir, transformar, netejar i preparar les dades.

## Run the complete pipeline

```powershell
python -m pip install -e ".[dev,app]"
python -m src.pipelines.run_pipeline run --start-minio --serve
```

Configure RAWG and MinIO credentials in `.env`, run Docker Desktop, and install
FFmpeg/FFprobe. The command collects text and images from RAWG and music from
OpenGameArt, then runs all three modalities through Landing, Formatted and
Trusted, including quality reports, then generates Jina/CLAP embeddings, verifies
the four collections and starts Streamlit. Omit `--serve` for batch preparation
or use `--through trusted` to stop before embeddings.
Use `--dry-run` to preview the plan without making changes. See the
[global pipeline instructions](src/pipelines/README.md) for limits, recovery,
media tools and isolated integration tests.

The default sample is 50 RAWG games, up to one image per game, and 16 audio
files. Source failures can use the verified [offline backup](backup_data/README.md)
containing 50 descriptions, 50 images and 16 audio files. Use `--no-backup`
to require live source availability. Reports distinguish API failures from
successful offline recovery.

## Interactive application · ADSBD · Group 4

```powershell
python -m pip install -e ".[app]"
python -m streamlit run streamlit_app.py
```

Open http://localhost:8501. The application includes a project overview and a
page for each TASK: similarity search, multimodal search and generation.
TASK 1 supports text, image and audio similarity. TASK 2 supports text/image
retrieval and text/audio search in both directions, with mixed results and
category tabs. TASK 3 provides an AI-guided game finder.
Jina and CLAP collections are separate in `data/exploitation/chroma`.
The interface is in English.
See the [application instructions](src/app/README.md).

## Arquitectura de dades

El flux general del projecte és:

```text
Fonts externes
      |
      v
1.landing-zone/temporal
      |
      v
1.landing-zone/persistent
      |
      v
2.formatted-zone
      |
      v
3.trusted-zone
      |
      v
4.exploitation-zone
```

### 1. Landing Zone

Conté les dades tal com arriben de les fonts externes.

- `temporal/`: punt d'entrada dels fitxers originals. Els objectes poden ser
  eliminats quan s'han validat i promocionat correctament.
- `persistent/`: còpia permanent de les dades originals, organitzada i amb una
  convenció de noms estable.

Exemple:

```text
temporal/<source>/<dataset>/<original_filename>
persistent/<source>/<dataset>/<year>/<month>/<source>$<dataset>$<date>.<ext>
```

### 2. Formatted Zone

Conté les dades amb una estructura i un format homogenis. Manté, sempre que
sigui possible, la jerarquia de `landing/persistent` per conservar la
traçabilitat.

### 3. Trusted Zone

Conté dades netes i validades. Aquí s'apliquen correccions genèriques, regles de
qualitat conegudes, deduplicació i normalització de valors inconsistents.

### 4. Exploitation Zone

Conté els productes de dades preparats per a un ús concret, com ara datasets
analítics, KPIs, embeddings o dades d'entrenament.

## Estructura del repositori

```text
ADSBD-Grup4/
├── docker-compose.yml        # MinIO i inicialització dels buckets
├── .env.example              # Variables d'entorn d'exemple
├── pyproject.toml            # Dependències i configuració de Python
├── config/
│   ├── storage.yml           # Noms dels buckets i prefixos
│   └── datasets.yml          # Catàleg de fonts i datasets
├── src/
│   ├── common/               # Configuració, logs i client de MinIO
│   ├── ingestion/
│   │   ├── collectors/       # Un collector per cada font externa
│   │   └── temporal_to_persistent.py
│   ├── formatting/           # Landing persistent -> Formatted
│   ├── trusted/              # Formatted -> Trusted
│   ├── exploitation/         # Trusted -> Exploitation
│   └── pipelines/            # Execució i orquestració del pipeline
├── tests/
│   ├── unit/                 # Proves sense serveis externs
│   ├── integration/          # Proves que utilitzen MinIO
│   └── fixtures/             # Mostres petites per a les proves
├── notebooks/                # Exploració; no lògica definitiva
└── docs/
    ├── architecture.md
    └── data_dictionary.md
```

Les dades reals no s'han de guardar al repositori. Han de viure a MinIO. Git
només ha de contenir codi, configuració sense secrets, documentació i mostres de
prova petites.

## Requisits

- Docker Desktop amb Docker Compose.
- Python 3.11 o superior.
- Git.

## Configuració inicial

### 1. Crear el fitxer d'entorn

En PowerShell:

```powershell
Copy-Item .env.example .env
```

En Linux o macOS:

```bash
cp .env.example .env
```

Editeu `.env` i canvieu, com a mínim, les credencials d'exemple:

```dotenv
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=una-contrasenya-segura
MINIO_ENDPOINT=http://localhost:9000
```

El fitxer `.env` està ignorat per Git i no s'ha de publicar.

### 2. Arrencar MinIO

```bash
docker compose up -d
```

El servei `minio-init` crea idempotentment aquests buckets:

```text
1.landing-zone
2.formatted-zone
3.trusted-zone
4.exploitation-zone
```

Els prefixos `temporal/` i `persistent/` apareixen quan s'hi desen objectes;
no es creen fitxers buits per representar carpetes.

Podeu consultar l'estat dels contenidors amb:

```bash
docker compose ps -a
```

Que `minio-init` aparegui com `Exited (0)` és correcte: és una tasca
d'inicialització que acaba després de crear l'estructura.

### 3. Obrir la consola de MinIO

Obriu [http://localhost:9001](http://localhost:9001) i inicieu sessió amb les
credencials definides a `.env`.

L'API compatible amb S3 està disponible a `http://localhost:9000`.

### 4. Preparar Python

En PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

En Linux o macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 5. Comprovar la connexió

Amb MinIO en funcionament i l'entorn virtual activat:

```bash
python -m src.pipelines.run_pipeline status
```

La comanda mostra `OK` per a cadascun dels quatre buckets configurats.

## Configuració dels datasets

`config/datasets.yml` conté tres entrades provisionals. S'han de substituir
pels datasets reals del projecte:

```yaml
datasets:
  nom_dataset:
    source: nom_de_la_font
    modality: text
    raw_format: json
```

- `source`: sistema, API, web o proveïdor del qual provenen les dades.
- `dataset`: conjunt lògic de dades obtingut d'aquella font.
- `modality`: tipus de contingut, per exemple text, imatge, àudio o vídeo.
- `raw_format`: format original en què arriba la informació.

No dupliqueu els noms dels buckets als scripts. Llegiu-los sempre de
`config/storage.yml`.

## Desenvolupar un collector

Cada font externa ha de tenir un mòdul propi dins de
`src/ingestion/collectors/`. La seva responsabilitat és:

1. Obtenir les dades de la font.
2. Conservar el contingut original sense transformar-lo.
3. Construir una clau amb el patró
   `temporal/<source>/<dataset>/<original_filename>`.
4. Pujar l'objecte a `1.landing-zone` mitjançant el client compartit de
   `src/common/minio_client.py`.

Les claus d'API de les fonts externes també s'han de guardar a `.env`, mai al
codi ni a `datasets.yml`.

## Promocionar un fitxer a Landing persistent

Quan un objecte temporal ja s'ha validat, es pot moure a persistent:

```bash
python -m src.ingestion.temporal_to_persistent 'temporal/source/dataset/original.json' 'persistent/source/dataset/2026/10/source$dataset$13-10-2026.json'
```

La comanda comprova que l'objecte existeixi, el copia a la destinació i elimina
l'objecte temporal. No l'executeu fins que les validacions del dataset hagin
acabat correctament.

## Responsabilitat de cada mòdul

| Mòdul | Llegeix de | Escriu a |
|---|---|---|
| `ingestion/collectors` | Fonts externes | `1.landing-zone/temporal` |
| `temporal_to_persistent.py` | Landing temporal | Landing persistent |
| `formatting/` | `1.landing-zone/persistent` | `2.formatted-zone` |
| `trusted/` | `2.formatted-zone` | `3.trusted-zone` |
| `exploitation/` | `3.trusted-zone` | `4.exploitation-zone` |

Cada procés ha de poder repetir-se sense crear duplicats ni corrompre dades.
També ha de registrar quins objectes ha llegit i quins ha escrit.

## Executar les proves

```bash
pytest
```

Les proves unitàries no haurien de necessitar MinIO. Les proves d'integració sí
que poden assumir que `docker compose up -d` ja s'ha executat.

## Aturar l'entorn

Per aturar els contenidors conservant les dades:

```bash
docker compose down
```

El volum `minio_data` conserva els objectes entre reinicis.

Per eliminar també el volum i totes les dades locals caldria executar
`docker compose down -v`. Aquesta operació és destructiva i no s'ha d'utilitzar
si es vol conservar el contingut de MinIO.

## Ordre recomanat d'implementació

1. Substituir els exemples de `config/datasets.yml` pels datasets reals.
2. Implementar un collector i carregar dades a Landing temporal.
3. Validar i promocionar els objectes a Landing persistent.
4. Implementar la conversió a Formatted Zone.
5. Afegir regles de qualitat per construir la Trusted Zone.
6. Crear els productes finals de l'Exploitation Zone.
7. Afegir proves unitàries i d'integració a cada etapa.

## Pipelines desenvolupats

Per executar la pipeline d'imatges en Windows:

```bash
.\run_image_pipeline.ps1
```
