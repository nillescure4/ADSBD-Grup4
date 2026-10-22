# Ingestió de textos RAWG

`text_to_temporal_landing.py` recull les respostes JSON de RAWG, les desa localment
i, amb `--upload-minio`, puja els fitxers a MinIO. El col·lector original
`src/data_collection/collect_rawg_descriptions.py` conserva el funcionament
original i només desa dades locals.

Des de l'arrel del repositori, instal·leu les dependències amb
`python -m pip install -e ".[dev]"`, configureu `RAWG_API_KEY` i les
credencials de MinIO a `.env`, i arrenqueu MinIO amb `docker compose up -d`.

Per recollir un joc sense descarregar imatges abans:

```powershell
.\.venv\Scripts\python.exe -m src.ingestion.text_to_temporal_landing --game-ids 3498 --upload-minio
```

Per recollir els jocs del manifest d'imatges:

```powershell
.\.venv\Scripts\python.exe -m src.ingestion.text_to_temporal_landing --games-manifest data/landing/rawg_full/manifests/games.jsonl --upload-minio
```

`--game-ids` i `--games-manifest` són alternatives. Es pot limitar la recollida
amb `--max-games 5` i canviar el directori local amb `--output`.
Sense `--upload-minio`, el nou script també pot funcionar només en local.

La destinació es llegeix de `config/storage.yml`:

```text
1.landing-zone/temporal/texts/
├── <game_id>.json
└── manifests/text_sources.jsonl
```

La còpia local es desa per defecte a `data/landing/text_descriptions/`,
amb una carpeta per data UTC dins de `raw_api/rawg/game_details/<yyyy-mm-dd>/`.
Les còpies locals antigues continuen sent reutilitzables el seu dia de recollida.
El resum
`collection_summary.json` es conserva només en local. En repetir una execució
amb el mateix directori i el mateix dia UTC, es reutilitzen els JSON descarregats,
es repeteix la pujada i s'eviten entrades duplicades al manifest. En un dia nou,
es consulta RAWG de nou i es registra una nova captura amb la seva data i hash.
El manifest conserva l'històric de captures i es combina amb el manifest de
MinIO per no perdre els registres d'altres jocs o execucions.
Cal promocionar els JSON temporals d'un dia abans de pujar una captura d'un
altre dia per als mateixos jocs, perquè la ruta temporal és única per joc.
Si algun joc falla,
el procés retorna el codi de sortida 1.

## De temporal a persistent

```powershell
python -m src.ingestion.text_temporal_to_persistent --dry-run
python -m src.ingestion.text_temporal_to_persistent
```

El nou script detecta tots els JSON de jocs a `temporal/texts/` i els mou a
`persistent/texts/rawg/rawg$game_details_<id>$<dd-mm-yyyy>.json`, dins del
bucket Landing configurat. La data és la de recollida UTC del manifest,
no la data d'execució de la promoció. S'assumeix una captura per joc i dia.

No transforma el contingut: copia el JSON, comprova el SHA-256 i desa un
registre de procedència a
`persistent/texts/rawg/manifests/rawg$text_sources_<id>$<dd-mm-yyyy>.jsonl`.
Només elimina el JSON temporal després de verificar la còpia i el registre.
El manifest temporal es conserva com a registre de la recollida.

Es pot repetir el procés i incorporar jocs nous sense llistes fixes al codi.
Una còpia persistent idèntica permet reprendre una promoció interrompuda;
una destinació diferent o una data absent deixen el JSON a temporal amb un
error. `--dry-run` només llegeix i mostra les destinacions previstes.
