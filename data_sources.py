"""Download und Caching der externen Geodaten.

Zwei Quellen:
  * GeoNames-PLZ-Datensatz (DE.zip)  -> PLZ -> Lat/Lon
  * Deutschland-Grenze als GeoJSON   -> Maskierung der Heatmap

Alle Downloads werden in ``data/`` zwischengespeichert, damit nachfolgende
Laeufe offline funktionieren.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import requests
from shapely.geometry import shape
from shapely.ops import unary_union

DATA_DIR = Path(__file__).resolve().parent / "data"

# GeoNames-Postleitzahlen fuer Deutschland (Public Domain, CC-BY).
GEONAMES_PLZ_URL = "https://download.geonames.org/export/zip/DE.zip"
PLZ_CACHE = DATA_DIR / "plz_de.csv"

# Deutschland-Umriss. Mehrere Kandidaten, der erste erreichbare wird genutzt.
GERMANY_GEOJSON_URLS = [
    "https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON/main/1_deutschland/3_mittel.geo.json",
    "https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON/master/1_deutschland/3_mittel.geo.json",
    "https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON/main/1_deutschland/4_niedrig.geo.json",
]
GERMANY_GEOJSON_CACHE = DATA_DIR / "germany.geojson"

_HEADERS = {"User-Agent": "Location-Heatmap/1.0 (+https://example.local)"}


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _http_get(url: str, timeout: int = 60) -> requests.Response:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def load_plz_coordinates() -> pd.DataFrame:
    """Liefert ein DataFrame mit Spalten ``plz`` (str, 5-stellig), ``lat``, ``lon``.

    Beim ersten Aufruf wird der GeoNames-Datensatz heruntergeladen und als
    schlanke CSV in ``data/plz_de.csv`` gecacht.
    """
    _ensure_data_dir()

    if PLZ_CACHE.exists():
        df = pd.read_csv(PLZ_CACHE, dtype={"plz": str})
        return df

    print(f"Lade PLZ-Datensatz von GeoNames: {GEONAMES_PLZ_URL} ...")
    resp = _http_get(GEONAMES_PLZ_URL)

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Die relevante Datei heisst DE.txt (Tab-getrennt, ohne Header).
        with zf.open("DE.txt") as fh:
            raw = pd.read_csv(
                fh,
                sep="\t",
                header=None,
                dtype=str,
                usecols=[1, 9, 10],
                names=["plz", "lat", "lon"],
            )

    raw["lat"] = pd.to_numeric(raw["lat"], errors="coerce")
    raw["lon"] = pd.to_numeric(raw["lon"], errors="coerce")
    raw = raw.dropna(subset=["lat", "lon"])
    raw["plz"] = raw["plz"].str.strip().str.zfill(5)

    # Eine PLZ kann mehrfach vorkommen -> Mittelwert der Koordinaten je PLZ.
    df = raw.groupby("plz", as_index=False)[["lat", "lon"]].mean()

    df.to_csv(PLZ_CACHE, index=False)
    print(f"  -> {len(df)} eindeutige Postleitzahlen gespeichert in {PLZ_CACHE}")
    return df


def load_germany_boundary():
    """Liefert die Deutschland-Grenze als (Multi)Polygon (shapely-Geometrie)."""
    _ensure_data_dir()

    if GERMANY_GEOJSON_CACHE.exists():
        geojson = json.loads(GERMANY_GEOJSON_CACHE.read_text(encoding="utf-8"))
    else:
        geojson = None
        last_err: Exception | None = None
        for url in GERMANY_GEOJSON_URLS:
            try:
                print(f"Lade Deutschland-Grenze: {url} ...")
                resp = _http_get(url)
                geojson = resp.json()
                GERMANY_GEOJSON_CACHE.write_text(
                    json.dumps(geojson), encoding="utf-8"
                )
                break
            except Exception as err:  # noqa: BLE001 - naechsten Kandidaten versuchen
                last_err = err
                print(f"  fehlgeschlagen: {err}")
        if geojson is None:
            raise RuntimeError(
                "Konnte keine Deutschland-Grenze laden."
            ) from last_err

    geometries = [shape(feat["geometry"]) for feat in geojson["features"]]
    return unary_union(geometries)
