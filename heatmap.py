"""Reise-Heatmap ueber Deutschland.

Liest eine Tabelle (Google-Sheets-URL, CSV oder XLSX) mit den Spalten
Postleitzahl, maximale Reiseweite (km) und einer Gewichtungs-Spalte. Um jede
PLZ wird ein Kreis mit dem Radius der Reiseweite gelegt; der Kreis traegt den
Wert ``value_empty`` (Gewichtung leer) bzw. ``value_filled`` (Gewichtung
befuellt). Alle Kreise werden auf einem Raster ueber Deutschland aufaddiert,
auf den Deutschland-Umriss zugeschnitten und als interaktive HTML-Karte
(Leaflet/folium) ausgegeben.

Beispiel:
    python heatmap.py --input "https://docs.google.com/spreadsheets/d/<ID>/edit"
    python heatmap.py --input daten.csv --falloff soft --resolution-km 2
"""

from __future__ import annotations

import argparse
import html
import math
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import branca.colormap as cm
import folium
import numpy as np
import pandas as pd
import shapely
from branca.element import MacroElement
from jinja2 import Template

import data_sources

# Mittlere Erdgroessen fuer die metrische Umrechnung von Grad <-> km.
KM_PER_DEG_LAT = 111.32

# Kontrastreicher Farbverlauf der Heatmap: Fraktion (0..1) -> RGB.
# Dunkelblau -> Hellblau -> Gelb -> Orange -> Rot -> Dunkelrot.
_COLOR_STOPS = [
    (0.0, (10, 30, 110)),     # Dunkelblau (wenig)
    (0.2, (90, 170, 230)),    # Hellblau
    (0.4, (255, 230, 0)),     # Gelb
    (0.6, (255, 140, 0)),     # Orange
    (0.8, (220, 20, 20)),     # Rot
    (1.0, (104, 0, 0)),       # Dunkelrot (#680000, viel)
]

# Deckkraft der Heatmap-Ebene (Karte darunter bleibt sichtbar).
DEFAULT_OPACITY = 0.3

# Rahmenfarbe der Top-Wert-Zellen: noch dunkler als die Max-Farbe (#680000).
TOP_OUTLINE_COLOR = "#330000"


# --------------------------------------------------------------------------- #
# 1) Eingabe einlesen
# --------------------------------------------------------------------------- #
def sheets_url_to_csv(url: str, gid: str | None) -> str:
    """Wandelt eine Google-Sheets-Bearbeitungs-URL in die CSV-Export-URL um."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not match:
        return url
    sheet_id = match.group(1)
    export = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid is None:
        gid_match = re.search(r"[#&?]gid=([0-9]+)", url)
        gid = gid_match.group(1) if gid_match else None
    if gid is not None:
        export += f"&gid={gid}"
    return export


def read_table(input_source: str, sep: str, gid: str | None) -> pd.DataFrame:
    """Liest die Tabelle aus Google-Sheets-URL, CSV- oder XLSX-Datei."""
    if "docs.google.com/spreadsheets" in input_source:
        csv_url = sheets_url_to_csv(input_source, gid)
        print(f"Lese Google Sheet (live): {csv_url}")
        return pd.read_csv(csv_url, dtype=str)  # Sheets-Export ist Komma-getrennt

    path = Path(input_source)
    if not path.exists():
        sys.exit(f"Eingabedatei nicht gefunden: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        print(f"Lese Excel-Datei: {path}")
        return pd.read_excel(path, dtype=str)
    print(f"Lese CSV-Datei: {path}")
    return pd.read_csv(path, sep=sep, dtype=str)


def find_column(df: pd.DataFrame, keywords: list[str], explicit: str | None) -> str | None:
    """Findet eine Spalte per exaktem Namen oder per Schluesselwort-Teiltreffer."""
    if explicit:
        if explicit in df.columns:
            return explicit
        sys.exit(f"Angegebene Spalte '{explicit}' nicht gefunden. "
                 f"Vorhandene Spalten: {list(df.columns)}")
    for kw in keywords:
        for col in df.columns:
            if kw.lower() in str(col).lower():
                return col
    return None


def extract_plz(value: str) -> str | None:
    """Holt die erste 5-stellige Zahl aus einem Zellwert (z.B. '81735')."""
    if value is None:
        return None
    match = re.search(r"\d{4,5}", str(value))
    if not match:
        return None
    return match.group(0).zfill(5)[:5]


def count_meetings(value: str) -> int:
    """Zaehlt die besuchten 'Treffen der Helden' in einem Zellwert.

    Das Google-Formular erlaubt Mehrfachauswahl; der Sheets-Export verkettet die
    angekreuzten Treffen (jeweils 'Treffen der Helden <Jahr> (<Ort>)'). Gezaehlt
    wird, wie oft 'Treffen der Helden' im Zellwert vorkommt - leere Zelle = 0.
    """
    if value is None:
        return 0
    return len(re.findall(r"treffen der helden", str(value), flags=re.IGNORECASE))


def prepare_points(
    df: pd.DataFrame,
    plz_col: str,
    dist_col: str,
    weight_col: str | None,
    value_empty: float,
    value_filled: float,
) -> pd.DataFrame:
    """Baut ein DataFrame mit Spalten plz, radius_km, value aus der Rohtabelle."""
    out = pd.DataFrame()
    out["plz"] = df[plz_col].map(extract_plz)
    out["radius_km"] = pd.to_numeric(df[dist_col], errors="coerce")

    if weight_col is not None:
        weight_series = df[weight_col].fillna("").astype(str).str.strip()
        filled = weight_series != ""
        out["n_meetings"] = df[weight_col].map(count_meetings)
    else:
        filled = pd.Series(False, index=df.index)
        out["n_meetings"] = 0
    out["value"] = np.where(filled, value_filled, value_empty)

    before = len(out)
    out = out.dropna(subset=["plz", "radius_km"])
    out = out[out["radius_km"] > 0]
    dropped = before - len(out)
    if dropped:
        print(f"  {dropped} Zeile(n) ohne gueltige PLZ/Reiseweite uebersprungen.")
    return out.reset_index(drop=True)


def join_coordinates(points: pd.DataFrame, plz_coords: pd.DataFrame) -> pd.DataFrame:
    """Verknuepft PLZ mit Lat/Lon; meldet nicht gefundene Postleitzahlen."""
    merged = points.merge(plz_coords, on="plz", how="left")
    missing = merged["lat"].isna()
    n_missing = int(missing.sum())
    if n_missing:
        beispiele = sorted(merged.loc[missing, "plz"].unique())[:10]
        print(f"  {n_missing} Eintrag/Eintraege ohne Koordinaten (PLZ unbekannt). "
              f"Beispiele: {beispiele}")
    merged = merged.dropna(subset=["lat", "lon"]).reset_index(drop=True)
    print(f"  {len(merged)} Punkt(e) mit Koordinaten verwendet.")
    return merged


# --------------------------------------------------------------------------- #
# 2) Raster-Akkumulation
# --------------------------------------------------------------------------- #
def build_grid(bounds, resolution_km: float):
    """Erzeugt 1D-Achsen (lons, lats) in Grad fuer die Bounding-Box (minx,miny,maxx,maxy)."""
    lon_min, lat_min, lon_max, lat_max = bounds
    mean_lat = (lat_min + lat_max) / 2.0
    dlat = resolution_km / KM_PER_DEG_LAT
    dlon = resolution_km / (KM_PER_DEG_LAT * math.cos(math.radians(mean_lat)))
    lats = np.arange(lat_min, lat_max + dlat, dlat)
    lons = np.arange(lon_min, lon_max + dlon, dlon)
    return lons, lats


def accumulate(points: pd.DataFrame, lons, lats, falloff: str, edge_frac: float = 0.8):
    """Stempelt fuer jeden Punkt einen Kreis ins Raster.

    Liefert zwei Raster:
      * ``grid``  - summierte (gewichtete) Werte gemaess Kreisform (falloff).
      * ``count`` - Anzahl der Personen, in deren Reise-Radius die Zelle liegt
        (immer harte Mitgliedschaft dist <= radius, unabhaengig vom falloff).
    """
    grid = np.zeros((len(lats), len(lons)), dtype=np.float32)
    count = np.zeros((len(lats), len(lons)), dtype=np.int32)
    dlon = lons[1] - lons[0]
    dlat = lats[1] - lats[0]

    for plat, plon, radius, value in zip(
        points["lat"].to_numpy(),
        points["lon"].to_numpy(),
        points["radius_km"].to_numpy(),
        points["value"].to_numpy(),
    ):
        km_per_deg_lon = KM_PER_DEG_LAT * math.cos(math.radians(plat))
        # Fenster (Index-Bereich) der Radius-Bounding-Box bestimmen.
        rad_lat = radius / KM_PER_DEG_LAT
        rad_lon = radius / km_per_deg_lon
        col_lo = max(0, int(math.floor((plon - rad_lon - lons[0]) / dlon)))
        col_hi = min(len(lons) - 1, int(math.ceil((plon + rad_lon - lons[0]) / dlon)))
        row_lo = max(0, int(math.floor((plat - rad_lat - lats[0]) / dlat)))
        row_hi = min(len(lats) - 1, int(math.ceil((plat + rad_lat - lats[0]) / dlat)))
        if col_lo > col_hi or row_lo > row_hi:
            continue

        sub_lon = lons[col_lo:col_hi + 1]
        sub_lat = lats[row_lo:row_hi + 1]
        dx = (sub_lon[np.newaxis, :] - plon) * km_per_deg_lon
        dy = (sub_lat[:, np.newaxis] - plat) * KM_PER_DEG_LAT
        dist = np.sqrt(dx * dx + dy * dy)

        inside = dist <= radius
        if falloff == "soft":
            sigma = radius / 2.0
            contrib = value * np.exp(-(dist * dist) / (2.0 * sigma * sigma))
        elif falloff == "plateau":
            # Voller Wert bis zum Knick, dann glatter Cosinus-Abfall auf 0 an radius.
            knee = edge_frac * radius
            contrib = np.where(dist <= knee, float(value), 0.0)
            shoulder = (dist > knee) & inside
            if radius > knee:
                t = (dist[shoulder] - knee) / (radius - knee)  # 0..1
                contrib[shoulder] = value * 0.5 * (1.0 + np.cos(np.pi * t))
        else:  # hard
            contrib = np.where(inside, value, 0.0)

        grid[row_lo:row_hi + 1, col_lo:col_hi + 1] += contrib.astype(np.float32)
        count[row_lo:row_hi + 1, col_lo:col_hi + 1] += inside.astype(np.int32)

    return grid, count


def mask_to_germany(grid: np.ndarray, lons, lats, boundary) -> np.ndarray:
    """Setzt alle Zellen ausserhalb des Deutschland-Umrisses auf NaN."""
    lon_mesh, lat_mesh = np.meshgrid(lons, lats)
    inside = shapely.contains_xy(boundary, lon_mesh, lat_mesh)
    masked = grid.astype(np.float32).copy()
    masked[~inside] = np.nan
    return masked


def occupied_range(surface: np.ndarray) -> tuple[float, float]:
    """Min/Max ueber belegte Zellen (Wert > 0); (0, 0) wenn nichts belegt ist.

    So wird die Farbskala auf den tatsaechlichen Wertebereich gestreckt (nicht 0..Max).
    """
    covered = surface[np.isfinite(surface) & (surface > 0)]
    if covered.size:
        return float(covered.min()), float(covered.max())
    return 0.0, 0.0


def find_top_cells(surface: np.ndarray, count_grid: np.ndarray) -> np.ndarray:
    """Boolean-Maske der 'Top-Wert'-Zellen.

    Top-Wert = hoechster (endlicher) gewichteter Wert. Gibt es mehrere Zellen
    mit exakt diesem Wert, zaehlt unter ihnen die hoechste Anzahl an
    Datenbankeintraegen (``count_grid``). Alle so bestimmten Zellen werden
    markiert (es koennen mehrere sein - sie werden spaeter zu einer Flaeche
    verschmolzen, falls sie aneinanderstossen).
    """
    finite = np.isfinite(surface) & (surface > 0)
    if not finite.any():
        return np.zeros(surface.shape, dtype=bool)
    vmax = float(surface[finite].max())
    at_max = finite & np.isclose(surface, vmax, rtol=1e-6, atol=1e-6)
    cmax = int(count_grid[at_max].max())
    return at_max & (count_grid == cmax)


# --------------------------------------------------------------------------- #
# 3) Rendering
# --------------------------------------------------------------------------- #
def build_top_outline(top_mask: np.ndarray, lons, lats):
    """Verschmilzt die Top-Wert-Zellen zu einer Flaeche und liefert deren Rand.

    Jede markierte Zelle wird als Lat/Lon-Rechteck erzeugt; alle Rechtecke
    werden per ``shapely.union_all`` vereinigt. Aneinanderstossende Zellen
    verschmelzen so zu einer Flaeche (gemeinsame Innenkanten verschwinden),
    sodass nur der aeussere Rand als dunkelrote Umrandung gezeichnet wird.
    Liefert ``(GeoJson | None, Anzahl Top-Zellen)``.
    """
    dlon = lons[1] - lons[0]
    dlat = lats[1] - lats[0]
    half_w, half_h = dlon / 2, dlat / 2

    rows, cols = np.where(top_mask)
    if rows.size == 0:
        return None, 0

    boxes = [
        shapely.box(
            float(lons[c]) - half_w, float(lats[r]) - half_h,
            float(lons[c]) + half_w, float(lats[r]) + half_h,
        )
        for r, c in zip(rows, cols)
    ]
    merged = shapely.union_all(boxes)

    outline = folium.GeoJson(
        merged.__geo_interface__,
        style_function=lambda _f: {
            "fill": False,
            "fillOpacity": 0.0,
            "color": TOP_OUTLINE_COLOR,
            "weight": 3,
            "opacity": 1.0,
        },
        tooltip=folium.Tooltip("Top-Wert"),
    )
    return outline, int(rows.size)


def build_heatmap_layer(value_grid, count_grid, lons, lats, boundary,
                        colormap, opacity, name, show):
    """Baut die Heatmap als farbige Polygone (eine je belegter Rasterzelle).

    Jede Zelle ist ein Lat/Lon-Rechteck, eingefaerbt nach ihrem Wert und mit
    einem Tooltip 'Wertigkeit (Personen)', z.B. ``12.0 (8)``. Dieselbe Geometrie
    traegt Farbe, Tooltip und Hover-Rahmen - dadurch sitzen sie zwangslaeufig
    deckungsgleich (kein Raster/Projektions-Versatz wie bei einem ImageOverlay).
    Leaflet projiziert die Polygon-Ecken korrekt nach Web-Mercator.

    Die Top-Wert-Zellen (siehe ``find_top_cells``) bekommen zusaetzlich eine
    verschmolzene dunkelrote Umrandung. Zellen und Umrandung liegen in einer
    gemeinsamen ``FeatureGroup`` und lassen sich daher gemeinsam ein-/ausblenden.
    Liefert ``(FeatureGroup, Anzahl Zellen, Anzahl Top-Zellen)``.
    """
    dlon = lons[1] - lons[0]
    dlat = lats[1] - lats[0]
    half_w, half_h = dlon / 2, dlat / 2

    features = []
    for ilat in range(len(lats)):
        clat = float(lats[ilat])
        south, north = clat - half_h, clat + half_h
        for ilon in range(len(lons)):
            count = int(count_grid[ilat, ilon])
            if count <= 0:
                continue
            clon = float(lons[ilon])
            if not shapely.contains_xy(boundary, clon, clat):
                continue
            value = float(value_grid[ilat, ilon])
            if not math.isfinite(value):  # Randzelle ausserhalb der Maske
                value = 0.0
            features.append({
                "type": "Feature",
                "properties": {
                    "info": f"{value:.1f} ({count})",
                    "fill": colormap(value),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [clon - half_w, south],
                        [clon + half_w, south],
                        [clon + half_w, north],
                        [clon - half_w, north],
                        [clon - half_w, south],
                    ]],
                },
            })

    group = folium.FeatureGroup(name=name, show=show)

    cells = folium.GeoJson(
        {"type": "FeatureCollection", "features": features},
        style_function=lambda f: {
            "fillColor": f["properties"]["fill"],
            "color": f["properties"]["fill"],
            "weight": 0,
            "fillOpacity": opacity,
        },
        highlight_function=lambda _f: {
            "weight": 1.2, "color": "#222222",
            "fillOpacity": min(1.0, opacity + 0.25),
        },
        tooltip=folium.GeoJsonTooltip(fields=["info"], labels=False, sticky=True),
    )
    cells.add_to(group)

    # Top-Wert-Zellen: nur innerhalb der Maske (Deutschland) gewertet.
    top_mask = find_top_cells(value_grid, count_grid)
    outline, n_top = build_top_outline(top_mask, lons, lats)
    if outline is not None:
        outline.add_to(group)

    return group, len(features), n_top


def add_param_box(fmap, params: dict) -> None:
    """Haengt ein fest positioniertes Info-Panel mit den Erstellungs-Parametern an.

    Das Panel sitzt unten links und kollidiert damit weder mit der LayerControl
    (oben rechts) noch mit der Farbskala (unten rechts). Es zeigt, mit welchen
    Einstellungen die Karte erzeugt wurde. Die Datenquelle wird bewusst nur als
    Typ/Dateiname (ohne URL) ausgegeben.
    """
    falloff = str(params.get("falloff", ""))
    rows = [("Falloff", falloff)]
    if falloff == "plateau":
        rows.append(("Kantenanteil", f"{params.get('edge_frac', 0):.2f}"))
    rows += [
        ("Aufloesung", f"{params.get('resolution_km', 0):g} km"),
        ("Werte", f"leer {params.get('value_empty', 0):g} / voll {params.get('value_filled', 0):g}"),
        ("Deckkraft", f"{params.get('opacity', 0):g}"),
        ("Datensaetze", f"{params.get('n_points', 0)} von {params.get('n_total', 0)}"),
        ("Bereich", f"{params.get('vmin', 0):.1f} .. {params.get('vmax', 0):.1f}"),
        ("Erstellt", str(params.get("created", ""))),
        ("Quelle", str(params.get("source", ""))),
    ]

    row_html = "".join(
        f"<tr><td style='padding-right:8px;color:#555;white-space:nowrap'>{html.escape(label)}</td>"
        f"<td style='font-weight:600'>{html.escape(value)}</td></tr>"
        for label, value in rows
    )
    box_html = f"""
    <div style="
        position: fixed; bottom: 18px; left: 12px; z-index: 9999;
        background: rgba(255,255,255,0.92); border: 1px solid #999;
        border-radius: 6px; padding: 8px 10px; font-size: 12px;
        font-family: Arial, sans-serif; color: #222; line-height: 1.35;
        box-shadow: 0 1px 4px rgba(0,0,0,0.3); max-width: 260px;">
      <div style="font-weight:700; margin-bottom:4px;">Parameter</div>
      <table style="border-collapse:collapse;">{row_html}</table>
    </div>
    """
    fmap.get_root().html.add_child(folium.Element(box_html))


def _make_colormap(vmin: float, vmax: float, caption: str):
    """Erzeugt eine branca-Farbskala mit dem Standard-Farbverlauf.

    branca erwartet Hex-Strings bzw. 0..1-Floats; 0..255-Integer-Tupel erzeugen
    fehlerhafte Hex-Farben (grauer Balken). Daher Hex-Konvertierung.
    """
    return cm.LinearColormap(
        colors=["#%02x%02x%02x" % stop[1] for stop in _COLOR_STOPS],
        index=[vmin + stop[0] * (vmax - vmin) for stop in _COLOR_STOPS]
        if vmax > vmin else None,
        vmin=vmin,
        vmax=vmax,
        caption=caption,
    )


class BindColormap(MacroElement):
    """Koppelt die Sichtbarkeit einer Farbskala an einen Layer.

    Beim Ein-/Ausblenden des Layers (LayerControl) wird die zugehoerige
    branca-Farbskala per Leaflet-Event mit ein- bzw. ausgeblendet, sodass immer
    nur die Legende(n) der aktuell sichtbaren Ebene(n) erscheint. Die
    Anfangs-Sichtbarkeit richtet sich nach ``visible`` (Default-Anzeige).
    """

    def __init__(self, layer, colormap, visible: bool):
        super().__init__()
        self.layer = layer
        self.colormap = colormap
        self.visible = visible
        self._template = Template("""
        {% macro script(this, kwargs) %}
            {{this.colormap.get_name()}}.svg[0][0].style.display =
                '{{ "block" if this.visible else "none" }}';
            {{this._parent.get_name()}}.on('overlayadd', function (e) {
                if (e.layer == {{this.layer.get_name()}}) {
                    {{this.colormap.get_name()}}.svg[0][0].style.display = 'block';
                }});
            {{this._parent.get_name()}}.on('overlayremove', function (e) {
                if (e.layer == {{this.layer.get_name()}}) {
                    {{this.colormap.get_name()}}.svg[0][0].style.display = 'none';
                }});
        {% endmacro %}
        """)


def render_map(
    layers: list[dict],
    lons,
    lats,
    boundary,
    opacity: float,
    params: dict,
    output: Path,
) -> None:
    """Rendert eine oder mehrere Heatmap-Ebenen in eine interaktive Karte.

    Jeder Eintrag in ``layers`` ist ein Dict mit den Schluesseln ``surface``,
    ``count_grid``, ``vmin``, ``vmax``, ``name`` (Ebene/Farbskala), ``caption``
    (Farbskala) und ``show`` (Default-Sichtbarkeit).
    """
    lon_min, lon_max = float(lons[0]), float(lons[-1])
    lat_min, lat_max = float(lats[0]), float(lats[-1])
    center = [(lat_min + lat_max) / 2, (lon_min + lon_max) / 2]

    fmap = folium.Map(location=center, zoom_start=6, tiles="OpenStreetMap")

    # Die HTML wird haeufig neu erzeugt -> Browser-Cache unterbinden, damit nach
    # einem neuen Build nicht versehentlich eine veraltete Karte angezeigt wird.
    fmap.get_root().header.add_child(folium.Element(
        '<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">'
        '<meta http-equiv="Pragma" content="no-cache">'
        '<meta http-equiv="Expires" content="0">'
    ))

    bindings = []
    for spec in layers:
        colormap = _make_colormap(spec["vmin"], spec["vmax"], spec["caption"])
        layer, n_cells, n_top = build_heatmap_layer(
            spec["surface"], spec["count_grid"], lons, lats, boundary,
            colormap, opacity, spec["name"], spec["show"],
        )
        layer.add_to(fmap)
        colormap.add_to(fmap)
        bindings.append((layer, colormap, spec["show"]))

        print(f"Layer '{spec['name']}': Wertebereich {spec['vmin']:.1f} .. "
              f"{spec['vmax']:.1f}, {n_cells} Zellen, {n_top} Top-Wert-Zelle(n).")

    folium.LayerControl().add_to(fmap)

    # Jede Legende an ihren Layer koppeln -> es ist immer nur die Legende der
    # aktuell sichtbaren Ebene(n) zu sehen.
    for layer, colormap, show in bindings:
        fmap.add_child(BindColormap(layer, colormap, show))

    add_param_box(fmap, params)

    output.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(output))
    print(f"\nKarte gespeichert: {output}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
# Config-Schluessel, die in config.toml gesetzt werden duerfen. Identisch mit den
# argparse-``dest``-Namen, damit ``parser.set_defaults(**config)`` direkt greift.
CONFIG_KEYS = {
    "falloff", "edge_frac", "resolution_km", "opacity",
    "value_empty", "value_filled", "value_special_max",
}


def load_config(path: str) -> dict:
    """Liest abweichende Stil-Parameter aus einer TOML-Datei.

    Existiert die Datei nicht, wird ein leeres Dict geliefert (kein Fehler).
    Unbekannte Schluessel werden mit einer Warnung ignoriert. Die Datenquelle
    (``--input``) und der Ausgabepfad (``--output``) gehoeren bewusst NICHT in
    die Config.
    """
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    config = {}
    for key, value in data.items():
        if key in CONFIG_KEYS:
            config[key] = value
        else:
            print(f"  Warnung: unbekannter Config-Schluessel '{key}' ignoriert.")
    return config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reise-Heatmap ueber Deutschland erzeugen.")
    p.add_argument("--config", default="config.toml",
                   help="Pfad zur TOML-Config mit abweichenden Stil-Parametern "
                        "(Default 'config.toml'; fehlt sie, gelten die Defaults).")
    p.add_argument("--input", required=True,
                   help="Google-Sheets-URL oder Pfad zu einer .csv/.xlsx-Datei.")
    p.add_argument("--sep", default=";",
                   help="CSV-Trennzeichen fuer lokale Dateien (Default ';').")
    p.add_argument("--gid", default=None,
                   help="Optionale Tabellenblatt-ID (gid) des Google Sheets.")
    p.add_argument("--value-empty", type=float, default=1.0,
                   help="Kreiswert, wenn die Gewichtungs-Spalte leer ist (Default 1).")
    p.add_argument("--value-filled", type=float, default=2.0,
                   help="Kreiswert, wenn die Gewichtungs-Spalte befuellt ist (Default 2).")
    p.add_argument("--value-special-max", type=float, default=4.0,
                   help="Obergrenze der Sonder-Gewichtung (Treffen-Layer): Grundwert 1 "
                        "+ 1 je besuchtem Treffen, gedeckelt auf diesen Wert (Default 4).")
    p.add_argument("--falloff", choices=["hard", "soft", "plateau"], default="hard",
                   help="Kreisform: 'hard' (harte Kante), 'soft' (Gauss-Abfall) "
                        "oder 'plateau' (voller Wert bis Knick, dann Abfall auf 0).")
    p.add_argument("--edge-frac", type=float, default=0.8,
                   help="Nur fuer --falloff plateau: Anteil des Radius mit vollem "
                        "Wert vor dem Abfall (0..1, Default 0.8).")
    p.add_argument("--resolution-km", type=float, default=5.0,
                   help="Rasteraufloesung in km fuer Bild UND Tooltips (Default 5.0; "
                        "kleinere Werte = feiner, aber deutlich groessere HTML).")
    p.add_argument("--opacity", type=float, default=DEFAULT_OPACITY,
                   help="Deckkraft der Heatmap-Ebene 0..1 (Default 0.3).")
    p.add_argument("--output", default="output/heatmap.html",
                   help="Pfad der HTML-Ausgabe.")
    p.add_argument("--plz-col", default=None, help="Exakter Name der PLZ-Spalte.")
    p.add_argument("--dist-col", default=None, help="Exakter Name der Reiseweite-Spalte.")
    p.add_argument("--weight-col", default=None, help="Exakter Name der Gewichtungs-Spalte.")
    return p


def parse_args(argv=None):
    parser = build_parser()
    # Erste Phase: nur ``--config`` ermitteln, um die Datei zu finden.
    pre, _ = parser.parse_known_args(argv)
    config = load_config(pre.config)
    if config:
        print(f"Config geladen aus '{pre.config}': {config}")
        # Praezedenz: Default < config.toml < explizites CLI-Argument.
        parser.set_defaults(**config)
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)

    df = read_table(args.input, args.sep, args.gid)
    print(f"  {len(df)} Zeile(n), Spalten: {list(df.columns)}")

    plz_col = find_column(df, ["postleitzahl", "plz"], args.plz_col)
    dist_col = find_column(df, ["kilometer", "reiseweite", "weit"], args.dist_col)
    weight_col = find_column(df, ["schon mal", "gewichtung", "treffen der helden"],
                             args.weight_col)
    if plz_col is None or dist_col is None:
        sys.exit(f"PLZ- oder Reiseweite-Spalte nicht erkannt. "
                 f"Spalten: {list(df.columns)}. Nutze --plz-col / --dist-col.")
    print(f"  PLZ-Spalte: '{plz_col}' | Reiseweite-Spalte: '{dist_col}' | "
          f"Gewichtungs-Spalte: '{weight_col}'")

    points = prepare_points(df, plz_col, dist_col, weight_col,
                            args.value_empty, args.value_filled)

    plz_coords = data_sources.load_plz_coordinates()
    points = join_coordinates(points, plz_coords)
    if points.empty:
        sys.exit("Keine verwertbaren Punkte vorhanden - Abbruch.")

    boundary = data_sources.load_germany_boundary()

    lons, lats = build_grid(boundary.bounds, args.resolution_km)
    print(f"Raster: {len(lats)} x {len(lons)} Zellen "
          f"(Aufloesung {args.resolution_km} km), {args.falloff}-Kanten.")
    # Gewichtete Flaeche (value_empty/value_filled aus Config/CLI).
    grid, count_grid = accumulate(points, lons, lats, args.falloff, args.edge_frac)
    surface = mask_to_germany(grid, lons, lats, boundary)
    vmin, vmax = occupied_range(surface)

    # Ungewichtete Flaeche: dieselben Parameter, aber jeder Punkt zaehlt 1
    # (leer = voll = 1). Die Mitgliedschaft (count_grid) ist identisch.
    points_uw = points.copy()
    points_uw["value"] = 1.0
    grid_uw, _ = accumulate(points_uw, lons, lats, args.falloff, args.edge_frac)
    surface_uw = mask_to_germany(grid_uw, lons, lats, boundary)
    vmin_uw, vmax_uw = occupied_range(surface_uw)

    # Sonder-Gewichtung (Experiment): Grundwert 1 je Datensatz, +1 je besuchtem
    # Treffen der Helden, gedeckelt auf args.value_special_max (Default 4).
    points_sp = points.copy()
    points_sp["value"] = np.clip(
        1 + points["n_meetings"].to_numpy(), 1, args.value_special_max
    ).astype(float)
    grid_sp, _ = accumulate(points_sp, lons, lats, args.falloff, args.edge_frac)
    surface_sp = mask_to_germany(grid_sp, lons, lats, boundary)
    vmin_sp, vmax_sp = occupied_range(surface_sp)

    # Warnung, falls die Tooltip-Ebene (eine Zelle je belegter Rasterzelle) sehr
    # gross wird -> aufgeblaehte, traege HTML. Schwelle grob, als oberer Schaetzer.
    hover_cells = int((count_grid > 0).sum())
    if hover_cells > 50_000:
        print(f"  WARNUNG: ~{hover_cells} Tooltip-Zellen -> sehr grosse/traege HTML. "
              f"Erhoehe --resolution-km (aktuell {args.resolution_km} km).")

    if "docs.google.com/spreadsheets" in args.input:
        source_label = "Google Sheet"
    else:
        source_label = Path(args.input).name

    params = {
        "falloff": args.falloff,
        "edge_frac": args.edge_frac,
        "resolution_km": args.resolution_km,
        "value_empty": args.value_empty,
        "value_filled": args.value_filled,
        "opacity": args.opacity,
        "n_points": len(points),
        "n_total": len(df),
        "vmin": vmin,
        "vmax": vmax,
        # Feste Zeitzone Europe/Berlin, da der CI-Lauf sonst UTC anzeigen wuerde.
        "created": datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M %Z"),
        "source": source_label,
    }

    layers = [
        {
            "surface": surface, "count_grid": count_grid,
            "vmin": vmin, "vmax": vmax,
            "name": "Reise-Heatmap (gewichtet)",
            "caption": "Reisebereitschaft gewichtet (Min..Max der belegten Flaeche)",
            "show": True,
        },
        {
            "surface": surface_uw, "count_grid": count_grid,
            "vmin": vmin_uw, "vmax": vmax_uw,
            "name": "Reise-Heatmap (ungewichtet)",
            "caption": "Reisebereitschaft ungewichtet, jeder = 1 (Min..Max)",
            "show": False,
        },
        {
            "surface": surface_sp, "count_grid": count_grid,
            "vmin": vmin_sp, "vmax": vmax_sp,
            "name": "Reise-Heatmap (Treffen-Gewichtung)",
            "caption": f"Grundwert 1 + 1 je besuchtem Treffen (max {args.value_special_max:g})",
            "show": False,
        },
    ]
    render_map(layers, lons, lats, boundary, args.opacity, params, Path(args.output))


if __name__ == "__main__":
    main()
