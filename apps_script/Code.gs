/**
 * Loest den GitHub-Actions-Workflow aus, wenn eine neue Formular-Antwort eingeht.
 *
 * Funktionsweise:
 *   Die Tabelle ist eine Google-Form-Ergebnistabelle. Bei jeder
 *   Formularuebermittlung schickt das Skript einen POST an die
 *   repository_dispatch-API von GitHub (event_type "sheet-updated"). Der
 *   Workflow .github/workflows/build-heatmap.yml reagiert darauf und baut die
 *   Heatmap neu.
 *
 *   Wichtig: Formular-Antworten loesen KEINEN onEdit-Trigger aus (onEdit feuert
 *   nur bei manuellen Zellbearbeitungen). Daher reagiert dieses Skript auf
 *   onFormSubmit.
 *
 * Einrichtung (einmalig):
 *   1. Im Sheet:  Erweiterungen -> Apps Script  -> diesen Code einfuegen.
 *   2. Projekteinstellungen -> Skripteigenschaften:
 *        GITHUB_TOKEN = <fine-grained PAT>
 *      Der Token braucht NUR Zugriff auf das Repo TomBuZi/location_heatmap
 *      mit der Berechtigung "Contents: Read and write" (noetig fuer
 *      repository_dispatch). Der Token steht bewusst NICHT im Code.
 *   3. Trigger (Uhr-Symbol) -> Trigger hinzufuegen:
 *        Funktion:      onFormSubmit
 *        Ereignisquelle: Aus Tabelle
 *        Ereignistyp:   Bei Formularuebermittlung   (INSTALLIERBARER Trigger!)
 *      Nur installierbare Trigger duerfen externe Aufrufe (UrlFetchApp) mit
 *      Autorisierung machen.
 */

// Repo ist oeffentlich -> Owner/Name duerfen im Code stehen.
var GITHUB_OWNER = 'TomBuZi';
var GITHUB_REPO = 'location_heatmap';
var EVENT_TYPE = 'sheet-updated';

/**
 * Trigger-Ziel: bei jeder Formularuebermittlung aufgerufen.
 */
function onFormSubmit(e) {
  triggerHeatmapBuild();
}

/**
 * Sendet repository_dispatch an GitHub.
 *
 * Kein Debounce: jede Formular-Antwort loest genau einen Build aus, damit die
 * Karte immer die juengste Antwort enthaelt. (Falls je sehr viele gleichzeitige
 * Antworten zu viel Build-Last erzeugen, waere der Upgrade ein zeitgesteuerter,
 * nachlaufender Rebuild ueber einen zweiten Trigger.)
 */
function triggerHeatmapBuild() {
  var props = PropertiesService.getScriptProperties();

  var token = props.getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('Abbruch: Skripteigenschaft GITHUB_TOKEN fehlt.');
    return;
  }

  var url = 'https://api.github.com/repos/' + GITHUB_OWNER + '/' + GITHUB_REPO + '/dispatches';
  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({ event_type: EVENT_TYPE }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  if (code === 204) {
    Logger.log('Dispatch ok (204) - Workflow ausgeloest.');
  } else {
    Logger.log('Dispatch fehlgeschlagen (%s): %s', code, response.getContentText());
  }
}
