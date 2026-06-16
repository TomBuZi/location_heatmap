# Reise-Heatmap über Deutschland

Erzeugt aus einer Umfrage-Tabelle (Postleitzahl, maximale Reiseweite, Gewichtung)
eine **Heatmap der Reisebereitschaft** über Deutschland und gibt sie als
interaktive HTML-Karte aus.

Um jede Postleitzahl wird ein Kreis mit dem Radius der maximalen Reiseweite
gelegt. Jeder Kreis trägt einen Wert:

- **1** (`--value-empty`), wenn die Gewichtungs-Spalte **leer** ist
- **2** (`--value-filled`), wenn die Gewichtungs-Spalte **befüllt** ist

Alle Kreise werden auf einem Raster über Deutschland aufaddiert. Der Punkt mit
der höchsten Summe ist der Ort, zu dem die meisten Menschen reisen würden – er
wird mit einem Marker hervorgehoben. Anwendungsfall: optimaler Veranstaltungsort
für das *Treffen der Helden 2027*.

## Installation

```bash
pip install -r requirements.txt
```

## Verwendung

Direkt aus einem öffentlich (per Link) freigegebenen Google Sheet:

```bash
python heatmap.py --input "https://docs.google.com/spreadsheets/d/<SHEET-ID>/edit"
```

Aus einer lokalen Datei:

```bash
python heatmap.py --input example_input.csv
python heatmap.py --input daten.xlsx
```

Anschließend `output/heatmap.html` im Browser öffnen.

## Wichtige Optionen

| Option             | Default               | Beschreibung |
|--------------------|-----------------------|--------------|
| `--input`          | –                     | Google-Sheets-URL **oder** Pfad zu `.csv`/`.xlsx` |
| `--falloff`        | `hard`                | `hard` = harte Kante, `soft` = Gauß-Abfall, `plateau` = voller Wert bis zum Knick, dann Abfall auf 0 am Rand |
| `--edge-frac`      | `0.8`                 | Nur bei `--falloff plateau`: Anteil des Radius mit vollem Wert vor dem Abfall (0–1) |
| `--value-empty`    | `1`                   | Kreiswert bei leerer Gewichtungs-Spalte |
| `--value-filled`   | `2`                   | Kreiswert bei befüllter Gewichtungs-Spalte |
| `--resolution-km`  | `5.0`                 | Rasterauflösung in km für **Bild und Tooltips** (kleiner = feiner, aber deutlich größere HTML) |
| `--opacity`        | `0.3`                 | Deckkraft der Heatmap-Ebene (0–1); Karte darunter bleibt sichtbar |
| `--config`         | `config.toml`         | TOML-Datei mit abweichenden Stil-Parametern (s. u.) |
| `--sep`            | `;`                   | CSV-Trennzeichen für lokale Dateien |
| `--gid`            | –                     | Tabellenblatt-ID des Google Sheets |
| `--output`         | `output/heatmap.html` | Pfad der HTML-Ausgabe |
| `--plz-col` / `--dist-col` / `--weight-col` | Auto | Spaltennamen manuell setzen, falls die Auto-Erkennung danebenliegt |

Die Tooltips (Mouseover „Wertigkeit (Personen)") liegen jetzt **deckungsgleich auf
den Heatmap-Rasterzellen** – gesteuert durch dieselbe `--resolution-km`. Eine feine
Auflösung erzeugt daher viele Tooltip-Zellen und große HTML-Dateien; das Programm warnt
ab ~50.000 Zellen.

## Konfiguration (`config.toml`)

Abweichungen vom Default lassen sich dauerhaft in `config.toml` ablegen, statt sie bei
jedem Aufruf als Flags zu setzen:

```toml
falloff = "soft"
resolution_km = 5
opacity = 0.7
value_filled = 3
value_empty = 1
```

**Präzedenz**: eingebauter Default < `config.toml` < explizites CLI-Argument. Ein per
CLI gesetztes Flag (z. B. `--falloff hard`) überschreibt also die Config. Die Datenquelle
(`--input`) und der Ausgabepfad (`--output`) gehören **nicht** in die Config. Eingelesen
wird per `tomllib` – das erfordert **Python ≥ 3.11**.

## Online betreiben (GitHub Actions + Pages)

Das Repo enthält einen Workflow (`.github/workflows/build-heatmap.yml`), der die Karte in
der Cloud baut und auf **GitHub Pages** veröffentlicht. Die Sheets-URL liegt dabei als
Repository-Secret `SHEET_URL` (nicht im Code), die veröffentlichte HTML enthält nur
aggregierte Werte. Auslöser: Push auf `main`, manueller Button und `repository_dispatch`
(vom Google Apps Script in `apps_script/Code.gs`, das bei jeder Formular-Antwort feuert).

Einrichtung: Secret `SHEET_URL` setzen, Pages-Quelle auf „GitHub Actions" stellen,
Apps Script samt `GITHUB_TOKEN` und installierbarem Trigger „Bei Formularübermittlung"
(`onFormSubmit`) einrichten (Details in `apps_script/Code.gs`).

## Eingabeformat

Die Spalten werden per Schlüsselwort automatisch erkannt:

- **PLZ**: Spalte mit „Postleitzahl"/„PLZ" – z. B. `Wo wohnst Du? (Postleitzahl)`
- **Reiseweite**: Spalte mit „Kilometer"/„Reiseweite" (numerisch, in km)
- **Gewichtung**: Spalte mit „schon mal"/„Gewichtung" – leer vs. befüllt

Liegt die Erkennung daneben, die Spaltennamen explizit per `--*-col` angeben.

## Datenquellen

Beim ersten Lauf werden automatisch heruntergeladen und in `data/` zwischengespeichert:

- **PLZ → Koordinaten**: [GeoNames](https://download.geonames.org/export/zip/) (`DE.zip`)
- **Deutschland-Umriss**: [deutschlandGeoJSON](https://github.com/isellsoap/deutschlandGeoJSON)
