# Trusted

Els processos d'aquesta carpeta corregeixen errors generics, validen les dades
i escriuen el resultat a `3.trusted-zone`.

## Textos RAWG

```powershell
python -m src.trusted.text_to_trusted_zone --dry-run
python -m src.trusted.text_to_trusted_zone
```

`text_to_trusted_zone.py` processa totes les dates de Formatted la primera
vegada. Les execucions seguents nomes reprocessen particions noves, modificades,
amb sortides absents/corruptes o amb regles de qualitat diferents. No s'eliminen
ni es modifiquen els originals de Formatted. Un joc en dates diferents conserva
totes les captures. Els buckets i prefixos es llegeixen de `config/storage.yml`.

```text
3.trusted-zone/texts/rawg/
├── collection_date=2026-10-20/
│   └── game_details.parquet
├── quality/
│   ├── collection_date=2026-10-20.json
│   └── collection_date=2026-10-20.md
└── manifests/
    └── collection_date=2026-10-20.json
```

Les regles reutilitzables viuen a `text_quality.py`, separades de l'acces a
MinIO i de l'orquestracio. Els processos d'altres modalitats han de tenir els
seus scripts propis.

### Neteja i columnes

Al Parquet de Trusted es conserven les columnes de negoci,
substituint `description` i `description_raw` per una unica columna:

| Columna | Contingut |
|---|---|
| `description_clean` | Text net, o nul si no hi ha descripcio utilitzable |

Amb l'esquema actual, son 53 columnes: 52 camps RAWG i `description_clean`.
Les quatre dades de procedencia es llegeixen de `row_provenance` al manifest
Formatted i es conserven als informes Trusted, no al Parquet.
Les descripcions originals es conserven a Formatted i
Landing. El camp utilitzat per netejar, la longitud i els avisos es desen nomes
als informes, no com a columnes del Parquet.

Es prefereix `description_raw`. Si no produeix text utilitzable, es recorre a
`description`. La neteja elimina etiquetes HTML, contingut script/style i
caracters de control, descodifica entitats HTML i ajusta espais i salts de
linia, conservant paragrafs, accents, puntuacio i majuscules. No es tradueix,
no s'imputen camps, no es corregeix ortografia de manera especulativa, no
s'eliminen stopwords i no es generen embeddings.

Nomes s'eliminen files exactament iguals dins de la mateixa particio.
Captures diferents del mateix joc es marquen si entren en conflicte; dos jocs
amb el mateix text es conserven i es marquen. No es deduplica entre dates.

### Qualitat i informes

Abans de llegir la taula es comprova el hash del Parquet contra el registre
de Formatted. Les files es validen quant a ID positiu, dates, nom, procedencia,
referencies al manifest Formatted, valors numerics no finits i sintaxi/estructura
dels camps JSON. Les incidencies per fila es marquen, no s'eliminen automaticament.
Es comprova que el manifest cobreixi totes les files, respecti els indexs
i coincideixi amb els IDs del Parquet; si no, no es processa aquella data.
Un text buit queda nul a la columna neta. Un text curt nomes genera un avis.

El llindar orientatiu es de 50 caracters i es pot canviar:

```powershell
python -m src.trusted.text_to_trusted_zone --min-description-chars 80
```

Cada data te un informe JSON detallat amb nuls per columna, estadistiques de
longitud, HTML/control, duplicats exactes, correccions, avisos per fila i
procedencia del Parquet d'entrada. L'informe Markdown compara l'abans i el
despres, explica els criteris i identifica les incidencies pendents. Els
avisos s'identifiquen amb l'ID del joc, la data de captura, la ruta d'origen
i la fila d'entrada/sortida, per localitzar-los encara que canviï l'ordre.
El JSON inclou `row_analysis` per a cada fila, amb el camp de descripcio
utilitzat, la longitud del text net i els avisos. Els indexs de fila del JSON
comencen en 0; els numeros mostrats al Markdown comencen en 1.
Els resultats nomes descriuen la mostra d'aquella data; no garanteixen la qualitat
semantica ni es generalitzen al cataleg de RAWG.

### Analisi aprofundida i resum global

La versio 4 amplia els informes sense afegir columnes al Parquet de 53 camps:

- Perfil numeric abans/despres: nuls, valors no finits, minim, maxim,
  mitjana, mediana, quartils, P95, valors diferents i histograma de cinc
  intervals (un de sol si tots els valors son iguals).
- Possibles extrems amb el criteri descriptiu d'1,5 vegades l'IQR, nomes
  amb almenys quatre valors finits i excloent l'ID. No s'eliminen valors,
  no s'imputen nuls i no es normalitzen les columnes.
- Completesa: percentatges de nuls/textos buits, diferenciant zeros i
  arrays JSON buits. Els camps absents a totes les captures s'interpreten
  com a aspectes a revisar, no com a errors demostrats.
- Mostra qualitativa determinista de fins a cinc textos per data, amb
  fragments abans/despres de fins a 2000 caracters, longituds completes,
  paragrafs, canvis nomes d'espais i senyals com reduccions grans o text
  que queda buit. Els fragments truncats s'indiquen explicitament.
  La mostra prioritza incidencies i canvis i no es presenta com a aleatoria
  ni representativa. La revisio humana del significat queda pendent;
  cap comprovacio automatica certifica que el text sigui informatiu o cert.

`quality/overview.json` i `quality/overview.md` resumeixen totes les dates
amb dades Formatted verificades de l'execucio: captures, jocs diferents,
captures per joc, avisos recurrents, distribucions numeriques i percentatges
globals d'absencia. Les estadistiques son sobre captures i no sobre jocs
independents. La mostra global prioritza fins a vuit exemples dels informes
per data, amb preferencia per jocs diferents.

La procedencia del resum es registra a `manifests/quality_overview.json`.
Sense canvis no es reescriu ni el resum ni les particions. Si hi ha un error
en alguna data, no es publica un nou resum parcial com si fos complet;
l'anterior queda com a fotografia de la seva procedencia registrada.
Un resum absent/corrupte es regenera sense tornar a netejar les particions
que ja s'han verificat.

Es verifica cada sortida abans de registrar-la com a processada. Una segona
execucio sense canvis no escriu res; els errors de pujada es poden reparar
reexecutant. Un error en una data no bloqueja les altres i retorna codi 1.
Cal executar el proces de manera sequencial, sense escriptors simultanis
sobre les mateixes particions.

La versio 4 de qualitat regenera automaticament els informes antics i conserva
les 53 columnes. Primer cal executar la versio 2 de Formatted per obtenir els
Parquet sense columnes de procedencia i els manifests amb correspondencia
explicita entre fila i origen.

