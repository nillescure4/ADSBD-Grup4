# Formatting

Els processos d'aquesta carpeta llegeixen de `1.landing-zone/persistent`,
homogeneitzen el format i escriuen a `2.formatted-zone`.

## Textos RAWG

Instal·leu les dependències des de l'arrel del projecte:

```powershell
python -m pip install -e ".[dev]"
```

Executeu primer una simulació o directament la conversió:

```powershell
python -m src.formatting.text_to_formatted_zone --dry-run
python -m src.formatting.text_to_formatted_zone
```

El script llegeix els JSON i registres de procedència de
`1.landing-zone/persistent/texts/rawg/`. No modifica ni elimina els originals.
Els noms dels buckets i els prefixos es llegeixen de `config/storage.yml`.

La sortida és una taula Parquet per dia UTC de recollida:

```text
2.formatted-zone/texts/rawg/
├── collection_date=2026-10-20/
│   └── game_details.parquet
└── manifests/
    └── collection_date=2026-10-20.json
```

Cada fila correspon a un joc i data. Es conserven els 54 camps coneguts de
RAWG, amb tipus estables encara que una columna només contingui nuls.
Els camps nous també es conserven, codificats com a text JSON. El Parquet
actual té només les 54 columnes de RAWG. La font, data de recollida, ruta
d'origen i hash es desen al manifest, no com a columnes de la taula.

Les llistes i els objectes (`genres`, `platforms`, etc.) es representen com
a text JSON reversible. La metadada Parquet `json_encoded_fields` identifica
aquestes columnes. Es conserven les descripcions, HTML, salts de línia,
accents, valors nuls i llistes buides; no es netegen ni es generen embeddings.

La ingestió incremental reconstrueix només les particions amb nous originals,
incorporant tots els jocs d'aquella data sense duplicar-los. Les captures
d'altres dies es mantenen en particions diferents. Els registres a
`manifests/` guarden les claus i hashes dels originals, el nombre de files,
la versió del convertidor i el hash del Parquet verificat. `row_provenance`
relaciona explícitament cada `row_index` (començant en 0) amb l'ID del joc,
la font, el timestamp de recollida, la ruta original i el hash. L'ordre
coincideix exactament amb les files del Parquet. Una segona
execució sense canvis no escriu cap objecte. Una sortida corrupta o una
execució interrompuda es pot reconstruir amb els originals persistents.

Es comproven l'ID, la data, la ruta, el hash i el tipus dels camps abans
d'escriure cada partició. Els errors d'entrada impedeixen actualitzar aquella
partició; els de pujada o verificació permeten reconstruir-la en repetir
l'execució. El procés retorna el codi 1 i les altres dates es poden processar.
Si un original ja processat canvia o desapareix dins d'una partició encara
present, s'atura l'actualització d'aquella data per preservar l'històric.
El procés s'ha d'executar de manera seqüencial, sense escriptors simultanis
sobre les mateixes particions.

La versió 2 del convertidor regenera les particions antigues amb el nou
esquema sense columnes de procedència. Executeu aquest procés abans de
Trusted per actualitzar també els manifests amb `row_provenance`.

