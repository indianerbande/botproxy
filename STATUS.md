# Stand

Stand: 15. September 2026. Diese Datei ist der Einstieg für eine neue Sitzung.

## Was läuft

Der Proxy ist vollständig und getestet. 33 Tests grün, Ruff sauber.

Ein Lauf gegen einen Stub-Endpunkt funktioniert von außen: Anmeldung
übersprungen bei gültigem Token, Modelle erkannt, Anfragen durchgereicht,
Streaming gestückelt, 403 ohne Key und mit `Origin`.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # ~45 s, startet eigene Stubs auf freien Ports
ruff check . && ruff format .
```

Ein vollständiger `pytest`-Lauf dauert eine Dreiviertelminute; einzelne Dateien
laufen in ein bis zwei Sekunden.

## Module

Zwei Hälften, eine Berührung: `forward.py` ruft `tokens.ensure_fresh()`.
Aufteilung und Konventionen stehen in `CLAUDE.md`.

`tests/stubs.py` bringt beides mit, was zum Prüfen nötig ist: einen Identity
Provider mit Device Code Flow und Refresh, und einen Endpunkt, der 401
antworten und chunked streamen kann. Beide binden Port 0 und starten im
Testlauf selbst.

## Entscheidungen, die nicht offensichtlich sind

**`force_refresh(stale=…)`.** Nach einem 401 erneuert nur der erste Aufrufer.
Wer die Sperre später bekommt und feststellt, dass sein abgewiesenes Token
längst ersetzt ist, nimmt das neue. Ohne das lösen zehn gleichzeitig
abgewiesene Anfragen zehn Erneuerungen aus — im Betrieb unsichtbar, nur im Log
des Providers zu sehen. Geprüft in
`test_zehn_gleichzeitige_anfragen_loesen_eine_erneuerung_aus`.

**Ein frisch ausgestelltes Token wird nach 401 nicht erneuert.** Jünger als
`FRESHLY_ISSUED_SECONDS` (60 s) und trotzdem abgelehnt heißt: nicht das Token
ist falsch, sondern die Umgebung — typisch ein Aussteller, den der Endpunkt
nicht kennt, weil `AUTHORITY` und `BASE_URL` nicht zusammengehören. Ohne die
Frist holte jede Anfrage ein neues Token, bekäme wieder 401 und reichte es
durch; `stale` hilft dort nicht, weil jeder Aufrufer das gerade erneuerte Token
vorlegt. Die Ablehnung geht mit dem Grund des Endpunkts an den Client, im
Fenster steht einmal ein Hinweis. Nach der Frist darf wieder erneuert werden,
damit ein später widerrufenes Token nicht bis zum Ablauf klemmt. Geprüft in
`test_dauerhaftes_401_erneuert_nicht_bei_jeder_anfrage`.

**`allatclaims` in den Scopes.** Ohne ihn fehlen dem Token die Claims der
Anmeldung, und die Ablehnung kommt als 403 an der Berechtigung, nicht als 401 —
nichts daran zeigt auf den Scope. Ein Provider, der ihn nicht kennt, lehnt ihn
bei der Anmeldung mit Namen ab; dann wird er über `BOTPROXY_SCOPES` weggelassen.

**Kein `Content-Length` auf dem Rückweg.** Eine gesetzte Länge zwingt zum
Sammeln, und eine gestreamte Antwort käme am Stück statt Wort für Wort — ohne
Fehlermeldung.

**Der Body wird nie geparst.** Kein `json.loads` auf dem, was der Client
schickt. Der Preis dafür ist, dass der 401-Wiederholungsversuch den Body im
Speicher halten muss.

**Wiederholung nur vor dem ersten Byte.** Sobald die Antwort begonnen hat,
wird nichts mehr wiederholt.

**`truststore` statt `certifi`.** Hinter einem TLS-inspizierenden Proxy
scheitert sonst jede Weiterleitung, während die Anmeldung weiterläuft — die
geht über `urllib` und liest unter Windows den Systemspeicher. Dasselbe Netz,
zwei Urteile.

Das gilt **nur unter Windows**. Unter macOS und Linux prüft `urllib` gegen die
Pfade von OpenSSL, nicht gegen den Schlüsselbund. Dort wäre das Bild hinter
einem solchen Proxy umgekehrt: Weiterleitung geht, Anmeldung scheitert. Nicht
behoben, weil die Zielumgebung Windows ist; `oauth.py` bekäme dafür denselben
`truststore`-Kontext.

**Der Wecker ist der Normalfall.** Erneuert wird, bevor eine Anfrage auf ein
abgelaufenes Token trifft, nicht weil eine darauf getroffen ist.

**Der Wecker startet eine Anmeldung, nicht eine nach der anderen.** Läuft ein
Code unbestätigt ab oder wird die Anmeldung abgelehnt, sitzt niemand davor.
Bis dahin begann der nächste Durchlauf sofort die nächste, mit
`webbrowser.open` — über Nacht ein Tab je Viertelstunde. Jetzt merkt sich der
Manager das (`_login_unanswered`), der Wecker hält still, und erst eine Anfrage
fragt wieder; im Fenster steht, dass es so ist. Geprüft in
`test_wecker_startet_nach_unbestaetigtem_code_keine_neue_anmeldung`.

**Beim Start wird auf das Ende der Anmeldung gewartet, nicht auf eine Frist.**
`Manager.wait_for_login()` schläft auf einem Event, das der Anmelde-Thread
setzt, wie immer er endet. Das Warten ist in halbe Sekunden zerteilt, nur damit
Ctrl-C unter Windows durchkommt. Endet die Anmeldung ohne Token, beendet sich
botproxy mit Code 1 und öffnet keinen Port — der Grund steht schon im Fenster,
und ein Port ohne Token hieße nur 503 im Client. Vorher wartete
`server._wait_for_login` bis zu 15 Minuten und öffnete danach trotzdem.

## Offen

**botproxy selbst lief noch nie gegen einen echten Endpunkt.** Alles bisher
gegen Stubs. Der erste echte Lauf braucht `BOTPROXY_BASE_URL`,
`BOTPROXY_AUTHORITY` und `BOTPROXY_CLIENT_ID`.

**Belegt ist der Mechanismus trotzdem**, aus einem früheren Werkzeug mit
demselben `oauth.py` im selben Zielnetz:

- Device Code Flow funktioniert **ohne eigene App-Registrierung** — die
  Client-ID, mit der bisher Token geholt wurden, reicht.
- Die Bestätigung im Browser nutzt die bestehende Windows-Anmeldung; es kommt
  keine Passwortabfrage.
- Ein Refresh-Token wird ausgegeben, obwohl `scp` im Zugangstoken kein
  `offline_access` nennt.
- Das Zertifikat des Endpunkts wird mit `truststore` über den Systemspeicher
  angenommen.
- Eine Anfrage dauert rund 51 s, gleichzeitige werden eingereiht. Das Timeout
  von 600 s reicht.
- Modellnamen tragen einen führenden Schrägstrich und eigene Schreibweise.
  botproxy kann das nicht ausgleichen; es steht im README.

**Weiterhin ungeprüft:** ob der Provider `verification_uri_complete` liefert
(dann ist der Code im Browser schon eingetragen) und wie er sich bei
`slow_down` verhält.

**Nicht getestet:** `__main__.status` gegen eine laufende Instanz, und der
Start mit Platzhalterwerten (`config.validate`).

**Kein Log.** Absichtlich: Header tragen das Token, Bodies den Quelltext des
Benutzers. Falls Diagnose nötig wird, muss vorher feststehen, was nicht
hineindarf.

**Anfragen ohne `Content-Length`** (`Transfer-Encoding: chunked`) gehen mit
leerem Body hinaus, ohne Meldung. Ein 411 wäre ehrlicher.

**Der Pre-commit-Hook ist nicht aktiv.** `hooks/pre-commit` liegt im Repo, aber
ohne `git config core.hooksPath hooks` läuft er nie.
