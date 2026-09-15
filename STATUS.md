# Stand

Stand: 15. September 2026. Diese Datei ist der Einstieg für eine neue Sitzung.

## Was läuft

Der Proxy ist vollständig und getestet. 28 Tests grün, Ruff sauber.

Ein Lauf gegen einen Stub-Endpunkt funktioniert von außen: Anmeldung
übersprungen bei gültigem Token, Modelle erkannt, Anfragen durchgereicht,
Streaming gestückelt, 403 ohne Key und mit `Origin`.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # ~31 s, startet eigene Stubs auf freien Ports
ruff check . && ruff format .
```

Ein vollständiger `pytest`-Lauf dauert eine halbe Minute; einzelne Dateien
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
geht über `urllib` und liest den Systemspeicher. Dasselbe Netz, zwei Urteile.

**Der Wecker ist der Normalfall.** Erneuert wird, bevor eine Anfrage auf ein
abgelaufenes Token trifft, nicht weil eine darauf getroffen ist.

## Offen

**Noch nie gegen einen echten Endpunkt gelaufen.** Alles bisher gegen Stubs.
Der erste echte Lauf braucht `BOTPROXY_BASE_URL`, `BOTPROXY_AUTHORITY` und
`BOTPROXY_CLIENT_ID` — Letzteres muss beim Provider für den Device Code Flow
registriert sein. Ohne diese Registrierung kann botproxy nichts erneuern.

**Ungeprüft, weil Stubs es nicht hergeben:** ob der Provider
`verification_uri_complete` liefert (dann ist der Code im Browser schon
eingetragen), wie er sich bei `slow_down` verhält, und ob das Zertifikat des
Endpunkts über den Systemspeicher akzeptiert wird.

**`_wait_for_login` in `server.py`** pollt alle zwei Sekunden statt sich
wecken zu lassen. Funktioniert, ist aber die unschönste Stelle im Code.

**Nicht getestet:** `__main__.status` gegen eine laufende Instanz, und der
Start mit Platzhalterwerten (`config.validate`).

**Kein Log.** Absichtlich: Header tragen das Token, Bodies den Quelltext des
Benutzers. Falls Diagnose nötig wird, muss vorher feststehen, was nicht
hineindarf.

## Commit-Stand

Offen sind `forward.py` und `tokens.py` (die `force_refresh`-Korrektur),
`CLAUDE.md` und die fünf Dateien unter `tests/`. Sinnvoll als zwei Commits:
erst der Fehler, dann die Tests — oder umgekehrt, damit erkennbar bleibt,
wodurch er aufgefallen ist.
