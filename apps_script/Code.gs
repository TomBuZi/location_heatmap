/**
 * Loest den GitHub-Actions-Workflow aus, wenn sich das Google Sheet aendert.
 *
 * Funktionsweise:
 *   Bei jeder Bearbeitung schickt das Skript einen POST an die
 *   repository_dispatch-API von GitHub (event_type "sheet-updated"). Der
 *   Workflow .github/workflows/build-heatmap.yml reagiert darauf und baut die
 *   Heatmap neu.
 *
 * Einrichtung (einmalig):
 *   1. Im Sheet:  Erweiterungen -> Apps Script  -> diesen Code einfuegen.
 *   2. Projekteinstellungen -> Skripteigenschaften:
 *        GITHUB_TOKEN = <fine-grained PAT>
 *      Der Token braucht NUR Zugriff auf das Repo TomBuZi/location_heatmap
 *      mit der Berechtigung "Contents: Read and write" (noetig fuer
 *      repository_dispatch). Der Token steht bewusst NICHT im Code.
 *   3. Trigger (Uhr-Symbol) -> Trigger hinzufuegen:
 *        Funktion: onSheetEdit
 *        Ereignis: Aus Tabelle / Bei Bearbeitung   (INSTALLIERBARER Trigger!)
 *      Wichtig: der einfache onEdit-Trigger darf KEINE externen Aufrufe
 *      (UrlFetchApp) machen - daher einen installierbaren Trigger anlegen.
 */

// Repo ist oeffentlich -> Owner/Name duerfen im Code stehen.
var GITHUB_OWNER = 'TomBuZi';
var GITHUB_REPO = 'location_heatmap';
var EVENT_TYPE = 'sheet-updated';

// Mindestabstand zwischen zwei Ausloesungen (Debounce), in Millisekunden.
// onEdit feuert bei jeder Zelle - so vermeiden wir einen Lauf-Sturm.
var MIN_INTERVAL_MS = 5 * 60 * 1000; // 5 Minuten

/**
 * Trigger-Ziel: bei Bearbeitung des Sheets aufgerufen.
 */
function onSheetEdit(e) {
  triggerHeatmapBuild();
}

/**
 * Sendet repository_dispatch an GitHub - mit Debounce ueber Skripteigenschaften.
 */
function triggerHeatmapBuild() {
  var props = PropertiesService.getScriptProperties();

  var token = props.getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('Abbruch: Skripteigenschaft GITHUB_TOKEN fehlt.');
    return;
  }

  // Debounce: hoechstens alle MIN_INTERVAL_MS ausloesen.
  var now = new Date().getTime();
  var last = Number(props.getProperty('LAST_DISPATCH_MS') || 0);
  if (now - last < MIN_INTERVAL_MS) {
    Logger.log('Debounce aktiv - kein Dispatch (letzter vor %s ms).', now - last);
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
    props.setProperty('LAST_DISPATCH_MS', String(now));
    Logger.log('Dispatch ok (204) - Workflow ausgeloest.');
  } else {
    Logger.log('Dispatch fehlgeschlagen (%s): %s', code, response.getContentText());
  }
}
