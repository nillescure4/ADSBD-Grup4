#!/usr/bin/env python3
"""
collect_opengameart_audio.py

Recolector reproducible de música CC0 de OpenGameArt para una Landing Zone.

Qué hace
--------
1. Recorre el buscador avanzado de OpenGameArt filtrando:
   - Art Type = Music
   - License = CC0
2. Visita cada submission.
3. Verifica de nuevo que sea Music + CC0.
4. Extrae metadata: título, autor, tags, descripción, licencia, URL, etc.
5. Descarga ficheros musicales y, opcionalmente, archivos ZIP/7Z/RAR/TAR.*
6. Calcula SHA-256 y escribe manifests JSONL.
7. Es reanudable: no vuelve a descargar URLs ya registradas ni ficheros existentes.

Notas
-----
- OpenGameArt no ofrece una API pública equivalente a RAWG para este caso:
  este script hace crawling HTML de forma deliberadamente lenta.
- Por defecto espera 2 s entre peticiones.
- Comprueba robots.txt antes de rastrear.
- La estructura HTML del sitio puede cambiar; el parser usa varios selectores
  de respaldo y falla de forma conservadora.
- Landing: se preservan los ficheros descargados tal cual. No se convierten,
  normalizan ni segmentan aquí.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://opengameart.org"
SEARCH_URL = f"{BASE_URL}/art-search-advanced"

# IDs observados en el buscador avanzado de OpenGameArt.
OGA_ART_TYPE_MUSIC = "12"
OGA_LICENSE_CC0 = "4"

AUDIO_EXTENSIONS = {
    ".wav", ".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac",
    ".aiff", ".aif", ".au", ".wma",
}

# Música tracker / MIDI: no siempre es audio PCM comprimido, pero sigue siendo
# material musical útil que puede interesar conservar en Landing.
MUSIC_SOURCE_EXTENSIONS = {
    ".mid", ".midi", ".mod", ".xm", ".it", ".s3m", ".nsf", ".spc",
    ".vgm", ".vgz",
}

ARCHIVE_EXTENSIONS = {
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_short_id(value: str, n: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:n]


def clean_filename(name: str, max_len: int = 180) -> str:
    name = name.strip().replace("\x00", "")
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "file"
    stem, ext = os.path.splitext(name)
    if len(name) > max_len:
        keep = max_len - len(ext)
        name = stem[:max(1, keep)].rstrip() + ext
    return name


def canonicalize_url(url: str) -> str:
    """
    Elimina fragmentos y normaliza el host; conserva la query porque algunos
    enlaces de descarga pueden depender de ella.
    """
    p = urlparse(url)
    return urlunparse(
        (
            p.scheme.lower() or "https",
            p.netloc.lower(),
            p.path,
            p.params,
            p.query,
            "",
        )
    )


def url_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    # Trata tar.gz como archive.
    if path.endswith(".tar.gz"):
        return ".tar.gz"
    if path.endswith(".tar.bz2"):
        return ".tar.bz2"
    if path.endswith(".tar.xz"):
        return ".tar.xz"
    return Path(path).suffix.lower()


def is_download_candidate(url: str, include_archives: bool) -> bool:
    ext = url_extension(url)
    if ext in AUDIO_EXTENSIONS or ext in MUSIC_SOURCE_EXTENSIONS:
        return True
    if include_archives and (
        ext in ARCHIVE_EXTENSIONS
        or ext in {".tar.gz", ".tar.bz2", ".tar.xz"}
    ):
        return True
    return False


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def build_session(user_agent: str) -> requests.Session:
    retry = Retry(
        total=6,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


class PoliteRequester:
    def __init__(
        self,
        session: requests.Session,
        delay: float,
        timeout: int,
        user_agent: str,
    ):
        self.session = session
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.user_agent = user_agent
        self._last_request_at = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, **kwargs) -> requests.Response:
        self.wait()
        r = self.session.get(url, timeout=self.timeout, **kwargs)
        self._last_request_at = time.monotonic()
        r.raise_for_status()
        return r


def check_robots(user_agent: str, target_url: str) -> tuple[bool, str]:
    robots_url = f"{BASE_URL}/robots.txt"
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp.can_fetch(user_agent, target_url), robots_url
    except Exception as exc:
        # Conservador: si robots.txt no se puede consultar, avisamos, pero no
        # fingimos que tenemos una prohibición explícita.
        return True, f"{robots_url} (no se pudo leer: {exc})"


def search_params(
    page: int,
    items_per_page: int,
    query: str = "",
    cc0_filter: bool = True,
) -> list[tuple[str, str]]:
    # OpenGameArt usa parámetros Drupal con [] en el formulario.
    params = [
        ("field_art_type_tid[]", OGA_ART_TYPE_MUSIC),
        ("field_art_tags_tid_op", "or"),
        ("items_per_page", str(items_per_page)),
        ("sort_by", "count"),
        ("sort_order", "DESC"),
        ("page", str(page)),
        ("keys", query),
        ("title", ""),
        ("name", ""),
        ("Collection", ""),
        ("field_art_tags_tid", ""),
    ]
    if cc0_filter:
        params.insert(1, ("field_art_licenses_tid[]", OGA_LICENSE_CC0))
    return params


def content_links_from_search(html: str, page_url: str) -> list[str]:
    """
    Extrae enlaces de submissions de una página de resultados.

    OpenGameArt/Drupal ha cambiado clases CSS varias veces, así que evitamos
    depender de una única estructura. Aceptamos aliases /content/... y
    URLs canónicas /node/<id>.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    # Primero, zonas típicas de resultados de Drupal Views.
    selectors = [
        ".view-content .views-row a[href]",
        ".view-art-search-advanced a[href]",
        ".view-id-art_search_advanced a[href]",
        "#block-system-main .view-content a[href]",
        "main .view-content a[href]",
    ]
    for selector in selectors:
        for a in soup.select(selector):
            href = a.get("href")
            if href:
                candidates.append(urljoin(page_url, href))

    # Fallback total: revisa todos los enlaces de la página.
    for a in soup.find_all("a", href=True):
        candidates.append(urljoin(page_url, a["href"]))

    out = []
    seen = set()
    host = urlparse(BASE_URL).netloc

    for url in candidates:
        url = canonicalize_url(url)
        p = urlparse(url)
        if p.netloc != host:
            continue

        is_content = p.path.startswith("/content/") and p.path != "/content/"
        is_node = bool(re.fullmatch(r"/node/\d+/?", p.path))
        if not (is_content or is_node):
            continue

        if url not in seen:
            seen.add(url)
            out.append(url)

    return out


def describe_search_page(html: str) -> dict:
    """Información de diagnóstico si OpenGameArt devuelve una página inesperada."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    text = " ".join(soup.stripped_strings)
    m = re.search(r"Displaying\s+([\d,]+)\s*-\s*([\d,]+)\s+of\s+([\d,]+)", text, re.I)
    return {
        "title": title,
        "displaying": m.group(0) if m else None,
        "html_chars": len(html),
        "contains_access_denied": "access denied" in text.lower(),
        "contains_captcha": "captcha" in text.lower(),
    }


def has_next_search_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    # Drupal suele usar estas clases.
    if soup.select_one("li.pager-next a[href], .pager-next a[href]"):
        return True

    # Fallback textual.
    for a in soup.find_all("a", href=True):
        text = " ".join(a.stripped_strings).strip().lower()
        if text in {"next", "next ›", "next »", "›", "»"}:
            return True
    return False


def text_of_first(soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = " ".join(el.stripped_strings).strip()
            if text:
                return text
    return None


def link_texts(soup: BeautifulSoup, selectors: list[str]) -> list[str]:
    for selector in selectors:
        els = soup.select(selector)
        vals = [" ".join(e.stripped_strings).strip() for e in els]
        vals = [v for v in vals if v]
        if vals:
            # Dedupe preservando orden
            return list(dict.fromkeys(vals))
    return []


def extract_node_id(soup: BeautifulSoup, page_url: str) -> Optional[str]:
    # Drupal moderno puede exponer data-history-node-id.
    el = soup.select_one("[data-history-node-id]")
    if el and el.get("data-history-node-id"):
        return str(el["data-history-node-id"])

    # IDs/clases como node-12345.
    for tag in soup.find_all(True):
        values = []
        if tag.get("id"):
            values.append(str(tag.get("id")))
        values.extend(str(x) for x in (tag.get("class") or []))
        for value in values:
            m = re.search(r"\bnode[-_](\d+)\b", value)
            if m:
                return m.group(1)

    # Metadatos / shortlink.
    for link in soup.find_all("link", href=True):
        href = link["href"]
        m = re.search(r"/node/(\d+)", href)
        if m:
            return m.group(1)

    return None


def extract_description(soup: BeautifulSoup) -> str:
    selectors = [
        ".field-name-body .field-item",
        ".field--name-body .field__item",
        ".field-name-body",
        ".node .content .field-type-text-with-summary",
        "article .field--type-text-with-summary",
    ]
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = "\n".join(el.stripped_strings).strip()
            if text:
                return text
    return ""


def extract_file_links(
    soup: BeautifulSoup,
    page_url: str,
    include_archives: bool,
) -> list[dict]:
    candidates: list[tuple[str, str]] = []

    file_selectors = [
        ".field-name-field-art-files a[href]",
        ".field--name-field-art-files a[href]",
        ".field-field-art-files a[href]",
        ".field-name-field-files a[href]",
        ".field--name-field-files a[href]",
    ]

    found_structured = False
    for selector in file_selectors:
        els = soup.select(selector)
        if els:
            found_structured = True
            for a in els:
                href = urljoin(page_url, a["href"])
                label = " ".join(a.stripped_strings).strip()
                candidates.append((href, label))

    # Fallback: busca enlaces que por extensión parecen audio / fuente musical /
    # archivo descargable. Esto evita depender totalmente del HTML de Drupal.
    if not found_structured:
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"])
            label = " ".join(a.stripped_strings).strip()
            if is_download_candidate(href, include_archives):
                candidates.append((href, label))

    out = []
    seen = set()
    for href, label in candidates:
        href = canonicalize_url(href)
        if not is_download_candidate(href, include_archives):
            continue
        if href in seen:
            continue
        seen.add(href)
        ext = url_extension(href)
        out.append(
            {
                "file_url": href,
                "link_label": label or Path(urlparse(href).path).name,
                "extension": ext,
                "file_kind": (
                    "audio"
                    if ext in AUDIO_EXTENSIONS
                    else "music_source"
                    if ext in MUSIC_SOURCE_EXTENSIONS
                    else "archive"
                ),
            }
        )
    return out


@dataclass
class Submission:
    source_url: str
    source_node_id: Optional[str]
    source_slug: str
    title: str
    author: Optional[str]
    art_types: list[str]
    licenses: list[str]
    tags: list[str]
    description: str
    files: list[dict]


def parse_submission(html: str, page_url: str, include_archives: bool) -> Submission:
    soup = BeautifulSoup(html, "html.parser")

    title = text_of_first(
        soup,
        ["main h1", "article h1", ".page-header", "#page-title", "h1"],
    )
    if not title:
        title = Path(urlparse(page_url).path).name.replace("-", " ").strip() or "untitled"

    author = text_of_first(
        soup,
        [
            ".field-name-field-art-author a",
            ".field--name-field-art-author a",
            ".submitted a.username",
            ".author a.username",
            ".username",
        ],
    )

    art_types = link_texts(
        soup,
        [
            ".field-name-field-art-type a",
            ".field--name-field-art-type a",
            ".field-name-field-art-type .field-item",
            ".field--name-field-art-type .field__item",
        ],
    )

    tags = link_texts(
        soup,
        [
            ".field-name-field-art-tags a",
            ".field--name-field-art-tags a",
            ".field-name-field-tags a",
            ".field--name-field-tags a",
        ],
    )

    licenses = link_texts(
        soup,
        [
            ".field-name-field-art-licenses a",
            ".field--name-field-art-licenses a",
            ".field-name-field-art-license a",
            ".field--name-field-art-license a",
        ],
    )

    # Fallbacks basados en texto, por si cambian los nombres de campo.
    page_text = "\n".join(soup.stripped_strings)
    if not art_types and re.search(r"Art Type:\s*Music", page_text, flags=re.I):
        art_types = ["Music"]
    if not licenses and re.search(r"License\(s\):\s*CC0", page_text, flags=re.I):
        licenses = ["CC0"]

    files = extract_file_links(soup, page_url, include_archives)
    slug = Path(urlparse(page_url).path.rstrip("/")).name

    return Submission(
        source_url=canonicalize_url(page_url),
        source_node_id=extract_node_id(soup, page_url),
        source_slug=slug,
        title=title,
        author=author,
        art_types=art_types,
        licenses=licenses,
        tags=tags,
        description=extract_description(soup),
        files=files,
    )


def is_verified_cc0_music(sub: Submission) -> bool:
    is_music = any(x.strip().lower() == "music" for x in sub.art_types)
    is_cc0 = any(
        x.strip().lower() in {"cc0", "cc0 1.0", "public domain", "cc zero"}
        or x.strip().lower().startswith("cc0")
        for x in sub.licenses
    )
    return is_music and is_cc0


def choose_local_filename(file_info: dict, ordinal: int) -> str:
    label = clean_filename(file_info.get("link_label") or "")
    url_name = clean_filename(Path(urlparse(file_info["file_url"]).path).name)

    # Prefiere el nombre real de la URL si tiene extensión válida.
    candidate = url_name if Path(url_name).suffix else label
    if not candidate or candidate == "file":
        ext = file_info.get("extension") or ""
        candidate = f"file_{ordinal:03d}{ext}"

    # Ante colisiones se añade un hash de la URL.
    return candidate


def stream_download(
    requester: PoliteRequester,
    url: str,
    destination: Path,
) -> tuple[int, str, Optional[str]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")

    with requester.get(url, stream=True, allow_redirects=True) as r:
        content_type = r.headers.get("Content-Type")
        h = hashlib.sha256()
        size = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)

    tmp.replace(destination)
    return size, h.hexdigest(), content_type


def submission_is_complete_for_current_run(
    submission_row: dict,
    downloaded_urls: set[str],
    include_archives: bool,
    include_music_source: bool,
) -> bool:
    """Return True when a previously catalogued submission has nothing left
    to download under the current run settings."""
    files = submission_row.get("files") or []
    eligible_urls = []
    for info in files:
        kind = info.get("file_kind")
        if kind == "audio":
            eligible_urls.append(info.get("file_url"))
        elif kind == "archive" and include_archives:
            eligible_urls.append(info.get("file_url"))
        elif kind == "music_source" and include_music_source:
            eligible_urls.append(info.get("file_url"))
    eligible_urls = [u for u in eligible_urls if u]
    if not eligible_urls:
        return True
    return all(u in downloaded_urls for u in eligible_urls)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Recolecta música CC0 de OpenGameArt con metadata y manifest JSONL."
    )
    p.add_argument(
        "--output",
        default="data/landing/opengameart_audio",
        help="Directorio raíz de salida.",
    )
    p.add_argument(
        "--max-audio-files",
        type=int,
        default=2000,
        help=(
            "Objetivo TOTAL de ficheros de audio directos descargados en este output. "
            "El recolector se detiene al alcanzar el objetivo. Usa 0 para sin límite."
        ),
    )
    p.add_argument(
        "--max-items",
        type=int,
        default=0,
        help=(
            "Límite opcional de submissions CC0 procesadas en esta ejecución. "
            "Usa 0 para sin límite. Normalmente controla el corpus con --max-audio-files."
        ),
    )
    p.add_argument(
        "--query",
        default="",
        help="Texto opcional para restringir el buscador (p.ej. 'battle', 'rpg').",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Segundos mínimos entre peticiones HTTP (default: 2.0).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout HTTP por petición en segundos.",
    )
    p.add_argument(
        "--items-per-page",
        type=int,
        default=144,
        choices=(24, 48, 72, 96, 120, 144),
        help="Resultados por página del buscador.",
    )
    p.add_argument(
        "--include-archives",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Descarga ZIP/7Z/RAR/TAR además de audio directo (default: no).",
    )
    p.add_argument(
        "--include-music-source",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Descarga MIDI/tracker/VGM además de audio reproducible directo "
            "(default: no). Estos ficheros nunca cuentan para --max-audio-files."
        ),
    )
    p.add_argument(
        "--download",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Descarga los ficheros; con --no-download solo cataloga metadata.",
    )
    p.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent del crawler.",
    )
    p.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Ignora robots.txt. No recomendado; úsalo solo si has verificado permiso.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    output = Path(args.output)

    raw_dir = output / "raw"
    pages_dir = raw_dir / "pages"
    files_dir = raw_dir / "files"
    manifests_dir = output / "manifests"

    submissions_manifest = manifests_dir / "submissions.jsonl"
    files_manifest = manifests_dir / "files.jsonl"
    errors_manifest = manifests_dir / "errors.jsonl"

    existing_submission_rows = load_jsonl(submissions_manifest)
    processed_submissions = {
        r.get("source_url")
        for r in existing_submission_rows
        if r.get("source_url")
    }
    submission_rows_by_url = {
        r.get("source_url"): r
        for r in existing_submission_rows
        if r.get("source_url")
    }

    existing_file_rows = load_jsonl(files_manifest)
    downloaded_urls = {
        r.get("file_url")
        for r in existing_file_rows
        if r.get("file_url") and r.get("download_status") == "downloaded"
    }
    downloaded_audio_urls = {
        r.get("file_url")
        for r in existing_file_rows
        if (
            r.get("file_url")
            and r.get("download_status") == "downloaded"
            and r.get("file_kind") == "audio"
        )
    }
    initial_audio_files = len(downloaded_audio_urls)
    total_audio_files = initial_audio_files

    session = build_session(args.user_agent)
    requester = PoliteRequester(
        session=session,
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
    )

    first_search = requests.Request(
        "GET",
        SEARCH_URL,
        params=search_params(0, args.items_per_page, args.query),
    ).prepare().url

    if not args.ignore_robots:
        allowed, robots_info = check_robots(args.user_agent, first_search)
        print(f"[ROBOTS] {robots_info}")
        if not allowed:
            raise SystemExit(
                "robots.txt no permite rastrear esta URL con este User-Agent. "
                "El script se detiene de forma conservadora."
            )

    print("=== OpenGameArt CC0 Music Collector ===")
    print(f"Output: {output}")
    print(
        f"Target direct-audio files: "
        f"{'sin límite' if args.max_audio_files == 0 else args.max_audio_files}"
    )
    print(f"Already downloaded direct-audio files: {initial_audio_files}")
    if args.max_audio_files:
        print(f"Remaining to target: {max(0, args.max_audio_files - initial_audio_files)}")
    print(f"Max submissions this run: {'sin límite' if args.max_items == 0 else args.max_items}")
    print(f"Query: {args.query or '(ninguna)'}")
    print(f"Delay: {args.delay}s")
    print(f"Include archives: {args.include_archives}")
    print(f"Include MIDI/tracker/VGM sources: {args.include_music_source}")
    print(f"Download files: {args.download}")

    stats = {
        "search_pages": 0,
        "submission_pages_seen": 0,
        "verified_cc0_music": 0,
        "new_submissions_written": 0,
        "files_discovered": 0,
        "files_downloaded": 0,
        "direct_audio_downloaded_this_run": 0,
        "files_skipped": 0,
        "music_source_files_ignored": 0,
        "bytes_downloaded": 0,
        "errors": 0,
    }

    accepted_this_run = 0
    search_page = 0
    seen_submission_urls_this_run: set[str] = set()
    stop_target_reached = False

    while True:
        # Primer intento: filtro Music + CC0 en el propio buscador.
        params = search_params(
            search_page, args.items_per_page, args.query, cc0_filter=True
        )
        try:
            r = requester.get(SEARCH_URL, params=params)
            stats["search_pages"] += 1
        except Exception as exc:
            append_jsonl(
                errors_manifest,
                {
                    "stage": "search_page",
                    "page": search_page,
                    "error": repr(exc),
                    "timestamp_utc": utc_now(),
                },
            )
            stats["errors"] += 1
            print(f"[ERROR] página de búsqueda {search_page}: {exc}", file=sys.stderr)
            break

        search_url = r.url
        page_cache = pages_dir / "search" / f"page_{search_page:05d}_cc0.html"
        page_cache.parent.mkdir(parents=True, exist_ok=True)
        page_cache.write_text(r.text, encoding="utf-8")

        links = content_links_from_search(r.text, search_url)
        diag = describe_search_page(r.text)
        print(
            f"\n[SEARCH {search_page}] {len(links)} candidatos | "
            f"{diag.get('displaying') or diag.get('title')}"
        )

        # OpenGameArt a veces no aplica bien filtros avanzados a clientes HTTP.
        # Si el filtro CC0 devuelve cero, reintentamos SOLO Music y verificamos
        # CC0 en cada página individual antes de descargar nada.
        used_cc0_search_filter = True
        if not links:
            print("  [FALLBACK] filtro CC0 sin candidatos; probando búsqueda Music-only")
            fallback_params = search_params(
                search_page, args.items_per_page, args.query, cc0_filter=False
            )
            try:
                r = requester.get(SEARCH_URL, params=fallback_params)
                stats["search_pages"] += 1
                search_url = r.url
                page_cache = pages_dir / "search" / f"page_{search_page:05d}_music_only.html"
                page_cache.write_text(r.text, encoding="utf-8")
                links = content_links_from_search(r.text, search_url)
                diag = describe_search_page(r.text)
                used_cc0_search_filter = False
                print(
                    f"  [FALLBACK RESULT] {len(links)} candidatos | "
                    f"{diag.get('displaying') or diag.get('title')}"
                )
            except Exception as exc:
                stats["errors"] += 1
                append_jsonl(
                    errors_manifest,
                    {
                        "stage": "search_page_fallback",
                        "page": search_page,
                        "error": repr(exc),
                        "timestamp_utc": utc_now(),
                    },
                )
                print(f"[ERROR] fallback de búsqueda: {exc}", file=sys.stderr)
                break

        if not links:
            print(
                "\n[STOP] OpenGameArt respondió, pero no se pudieron extraer "
                "submissions. Revisa los HTML guardados en raw/pages/search/."
            )
            print("Diagnóstico:", json.dumps(diag, ensure_ascii=False))
            break

        for submission_url in links:
            if args.max_audio_files and total_audio_files >= args.max_audio_files:
                stop_target_reached = True
                break
            if args.max_items and accepted_this_run >= args.max_items:
                break
            if submission_url in seen_submission_urls_this_run:
                continue
            seen_submission_urls_this_run.add(submission_url)

            already_manifested = submission_url in processed_submissions
            if already_manifested:
                old_row = submission_rows_by_url.get(submission_url, {})
                if submission_is_complete_for_current_run(
                    old_row,
                    downloaded_urls=downloaded_urls,
                    include_archives=args.include_archives,
                    include_music_source=args.include_music_source,
                ):
                    print(f"  [FAST SKIP complete] {submission_url}")
                    continue
                print(f"  [RESUME partial] {submission_url}")

            stats["submission_pages_seen"] += 1
            print(f"  [PAGE] {submission_url}")

            try:
                page = requester.get(submission_url)
                sub = parse_submission(page.text, page.url, args.include_archives)

                page_id = sub.source_node_id or stable_short_id(sub.source_url)
                cached_submission = pages_dir / "submissions" / f"{page_id}.html"
                cached_submission.parent.mkdir(parents=True, exist_ok=True)
                cached_submission.write_text(page.text, encoding="utf-8")

                # Doble verificación: no confiamos solo en el filtro de búsqueda.
                if not is_verified_cc0_music(sub):
                    print(
                        f"    [FILTERED] art_types={sub.art_types} licenses={sub.licenses}"
                    )
                    continue

                stats["verified_cc0_music"] += 1
                accepted_this_run += 1

                submission_asset_id = (
                    f"oga:node:{sub.source_node_id}"
                    if sub.source_node_id
                    else f"oga:page:{stable_short_id(sub.source_url)}"
                )

                sub_row = {
                    "asset_id": submission_asset_id,
                    "modality": "audio",
                    "audio_type": "game_music_asset",
                    "source": "opengameart",
                    **asdict(sub),
                    "cached_source_page": str(cached_submission.relative_to(output)),
                    "files_discovered": len(sub.files),
                    "ingested_at_utc": utc_now(),
                }
                if sub.source_url not in processed_submissions:
                    append_jsonl(submissions_manifest, sub_row)
                    processed_submissions.add(sub.source_url)
                    submission_rows_by_url[sub.source_url] = sub_row
                    stats["new_submissions_written"] += 1
                stats["files_discovered"] += len(sub.files)

                print(
                    f"    [OK] {sub.title!r} | {sub.author or '?'} | "
                    f"{len(sub.tags)} tags | {len(sub.files)} files"
                )

                submission_folder = files_dir / (
                    sub.source_node_id
                    or f"{clean_filename(sub.source_slug)}_{stable_short_id(sub.source_url, 8)}"
                )

                used_names: set[str] = set()
                for ordinal, file_info in enumerate(sub.files, start=1):
                    if args.max_audio_files and total_audio_files >= args.max_audio_files:
                        stop_target_reached = True
                        break

                    if (
                        file_info.get("file_kind") == "music_source"
                        and not args.include_music_source
                    ):
                        stats["music_source_files_ignored"] += 1
                        continue

                    file_url = file_info["file_url"]
                    base_name = choose_local_filename(file_info, ordinal)

                    # Resuelve nombres repetidos dentro de una misma submission.
                    candidate = base_name
                    if candidate.lower() in used_names:
                        stem = Path(base_name).stem
                        suffix = Path(base_name).suffix
                        candidate = f"{stem}_{stable_short_id(file_url, 8)}{suffix}"
                    used_names.add(candidate.lower())

                    destination = submission_folder / candidate
                    file_asset_id = (
                        f"{submission_asset_id}:file:{stable_short_id(file_url)}"
                    )

                    if file_url in downloaded_urls:
                        stats["files_skipped"] += 1
                        print(f"      [SKIP url] {candidate}")
                        continue

                    if destination.exists():
                        # Registra el fichero local existente para poder reanudar aunque
                        # el manifest se hubiera perdido parcialmente.
                        size = destination.stat().st_size
                        digest = sha256_file(destination)
                        append_jsonl(
                            files_manifest,
                            {
                                "asset_id": file_asset_id,
                                "parent_asset_id": submission_asset_id,
                                "source": "opengameart",
                                "source_url": sub.source_url,
                                **file_info,
                                "local_path": str(destination.relative_to(output)),
                                "bytes": size,
                                "sha256": digest,
                                "content_type": mimetypes.guess_type(destination.name)[0],
                                "download_status": "downloaded",
                                "ingested_at_utc": utc_now(),
                            },
                        )
                        downloaded_urls.add(file_url)
                        if (
                            file_info.get("file_kind") == "audio"
                            and file_url not in downloaded_audio_urls
                        ):
                            downloaded_audio_urls.add(file_url)
                            total_audio_files += 1
                        stats["files_skipped"] += 1
                        print(
                            f"      [REGISTER existing] {candidate} "
                            f"[audio total={total_audio_files}]"
                        )
                        continue

                    if not args.download:
                        append_jsonl(
                            files_manifest,
                            {
                                "asset_id": file_asset_id,
                                "parent_asset_id": submission_asset_id,
                                "source": "opengameart",
                                "source_url": sub.source_url,
                                **file_info,
                                "local_path": None,
                                "download_status": "catalogued_only",
                                "ingested_at_utc": utc_now(),
                            },
                        )
                        print(f"      [CATALOG] {candidate}")
                        continue

                    try:
                        size, digest, content_type = stream_download(
                            requester, file_url, destination
                        )
                        append_jsonl(
                            files_manifest,
                            {
                                "asset_id": file_asset_id,
                                "parent_asset_id": submission_asset_id,
                                "source": "opengameart",
                                "source_url": sub.source_url,
                                **file_info,
                                "local_path": str(destination.relative_to(output)),
                                "bytes": size,
                                "sha256": digest,
                                "content_type": content_type
                                or mimetypes.guess_type(destination.name)[0],
                                "download_status": "downloaded",
                                "ingested_at_utc": utc_now(),
                            },
                        )
                        downloaded_urls.add(file_url)
                        stats["files_downloaded"] += 1
                        stats["bytes_downloaded"] += size

                        if (
                            file_info.get("file_kind") == "audio"
                            and file_url not in downloaded_audio_urls
                        ):
                            downloaded_audio_urls.add(file_url)
                            total_audio_files += 1
                            stats["direct_audio_downloaded_this_run"] += 1

                        print(
                            f"      [DOWNLOADED] {candidate} "
                            f"({size / 1_048_576:.2f} MiB) "
                            f"[direct-audio total={total_audio_files}]"
                        )
                    except Exception as exc:
                        stats["errors"] += 1
                        append_jsonl(
                            errors_manifest,
                            {
                                "stage": "download_file",
                                "source_url": sub.source_url,
                                "file_url": file_url,
                                "error": repr(exc),
                                "timestamp_utc": utc_now(),
                            },
                        )
                        print(f"      [WARN] descarga fallida: {exc}", file=sys.stderr)

            except Exception as exc:
                stats["errors"] += 1
                append_jsonl(
                    errors_manifest,
                    {
                        "stage": "submission_page",
                        "source_url": submission_url,
                        "error": repr(exc),
                        "timestamp_utc": utc_now(),
                    },
                )
                print(f"    [WARN] submission fallida: {exc}", file=sys.stderr)

        if stop_target_reached:
            print(
                f"\n[TARGET] alcanzados {total_audio_files} "
                f"ficheros de audio directos."
            )
            break

        if args.max_items and accepted_this_run >= args.max_items:
            break

        if not has_next_search_page(r.text):
            break

        search_page += 1

    summary = {
        "completed_at_utc": utc_now(),
        "source": "opengameart",
        "filters": {
            "art_type": "Music",
            "license": "CC0",
            "query": args.query or None,
        },
        "settings": {
            "delay_seconds": args.delay,
            "items_per_page": args.items_per_page,
            "include_archives": args.include_archives,
            "include_music_source": args.include_music_source,
            "download": args.download,
            "max_items": args.max_items,
            "max_audio_files": args.max_audio_files,
        },
        "direct_audio_files_before_run": initial_audio_files,
        "direct_audio_files_total": total_audio_files,
        "target_reached": (
            bool(args.max_audio_files)
            and total_audio_files >= args.max_audio_files
        ),
        **stats,
    }

    output.mkdir(parents=True, exist_ok=True)
    (output / "collection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
