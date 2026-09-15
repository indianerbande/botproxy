# botproxy

Lokaler Proxy zwischen einem Editor-Client mit statischem API-Key und einem
Endpunkt, dessen Token nach kurzer Zeit abläuft.

## Stack

- **Nur Windows.** macOS und Linux sind kein Ziel; Code für sie wird nicht
  geschrieben. Die Tests laufen trotzdem überall, auch auf dem Entwicklungsrechner.
- Python ≥ 3.11
- `httpx` — Weiterreichen der Anfragen, inklusive Streaming
- `truststore` — TLS gegen den Zertifikatsspeicher des Systems
- sonst Standardbibliothek: `http.server`, `urllib`, `threading`

Kein Web-Framework, kein OpenAI-SDK. botproxy spricht kein Modell an, er
reicht Bytes weiter.

**Keine neuen Abhängigkeiten ohne Not.** Zielumgebungen sind oft abgeschottet;
jedes Paket mehr ist dort ein Antrag.

## Kommandos

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python -m botproxy          # starten
python -m botproxy status   # Zustand abfragen
pytest                      # Tests
ruff check . && ruff format .
```

`requirements.txt` ist die einzige Wahrheit über Abhängigkeiten und wird von
Hand gepflegt — die Liste ist zwei Zeilen lang.

## Architektur

Zwei Hälften, die sich an genau einer Stelle berühren: `forward.py` ruft
`tokens.ensure_fresh()` und bekommt eine Zeichenkette zurück. Die Vorderseite
weiß nichts über OAuth, die Rückseite nichts über HTTP-Weiterleitung. Wer
beides in einer Datei anfasst, hat vermutlich die falsche offen.

```
botproxy/
  __main__.py   Einsprung: serve (Vorgabe) und status
  config.py     Werte oben, Mechanik in _env.py
  errors.py     ConfigError, AuthError, UpstreamError
  oauth.py      Device Code Flow und Refresh, nur Standardbibliothek
  store.py      Geheimnisse atomar auf die Platte, JWT-Ablauf lesen
  tokens.py     Zustand, Sperre, Wecker
  forward.py    Weiterreichen, kennt nur httpx
  server.py     Port, Routen, Statuszeile — das einzige Modul, das ausgibt
```

## Konventionen

- Typannotationen überall, auch bei internen Funktionen.
- Eigene Exceptions aus `errors.py`, kein nacktes `raise Exception`.
- Benutzersichtbare Texte auf Deutsch, Code und Kommentare auf Englisch.
- Keine stillen Fallbacks. Fehlt etwas, wird es gesagt.
- **Der Body wird nie geparst.** Kein `json.loads` auf dem, was der Client
  schickt. Alles, was botproxy über den Inhalt zu wissen glaubt, ist eines
  Tages falsch — Clients ändern ihr Format, ohne zu fragen.
- Keine Geheimnisse ins Log. Weder Token noch Bodies, unter keinem Schalter.

## Was Endpunkt und Provider tatsächlich tun

Annahmen, die naheliegen und falsch sind:

- **Ein 401 heißt nicht „abgelaufen“.** Ein Endpunkt nimmt nur Token seines
  eigenen Ausstellers an; ein fremder ergibt 401 auf jedes Token, auch ein
  frisches. Deshalb wird ein eben ausgestelltes Token nach 401 nicht erneut
  erneuert (`FRESHLY_ISSUED_SECONDS`).
- **Ein 403 vom Endpunkt heißt: Token verstanden, Berechtigung fehlt.** Kein
  Grund zu erneuern. Fehlende Claims im Token sehen genauso aus — daher
  `allatclaims` in den Scopes.
- **`scp` sagt nichts über das Refresh-Token.** Es kann eines kommen, obwohl
  `offline_access` dort fehlt. Nichts aus `scp` ableiten.
- **`urllib` liest den Zertifikatsspeicher von Windows, `httpx` nicht.**
  Deshalb `truststore` für die Weiterleitung; sonst scheitert sie hinter einem
  TLS-aufbrechenden Proxy, während die Anmeldung durchgeht.
- **`SO_REUSEADDR` heißt unter Windows „Port teilen“.** Ein zweiter Prozess
  bindet denselben Port ohne Fehler. Deshalb `SO_EXCLUSIVEADDRUSE`.
- **Dateirechte sind ACLs, keine Modusbits.** `chmod` schaltet nur
  Schreibschutz; geschützt ist, was unter dem Benutzerprofil liegt.
- **Modellnamen gehören dem Endpunkt**, mit führendem Schrägstrich und eigener
  Schreibweise. botproxy gleicht nichts an — der Body wird nie geparst.
- **Eine Anfrage darf eine Minute dauern.** Timeouts nicht verkürzen.

## Testen

Ohne Netz, ohne Token. Der Prüfstand sind zwei Stubs in `tests/`: ein
Identity Provider, der Device Code Flow und Refresh spricht, und ein Endpunkt,
der 401 antworten und in Stücken streamen kann. Beide klein genug, um im
Testlauf zu starten — kein Docker, keine externe Umgebung.

Was Abdeckung braucht:

- Pfad, Query und Body kommen unverändert an, auch mit unbekannten Feldern.
- Hop-by-hop-Header verschwinden, `Authorization` wird ersetzt.
- Eine gestreamte Antwort kommt in denselben Stücken heraus, nicht in einem.
- 401 führt zu genau einem Wiederholungsversuch; ein zweiter 401 geht durch.
- Ein dauerhafter 401 löst eine Erneuerung aus, nicht eine pro Anfrage.
- Zehn gleichzeitige Anfragen auf abgelaufenem Token lösen eine Erneuerung
  aus, nicht zehn. Dasselbe für die Anmeldung.
- Ein unbestätigt abgelaufener Code: der Wecker startet keine neue Anmeldung,
  erst die nächste Anfrage. Beim Start endet das Warten mit dem Code.
- Ein rotiertes Refresh-Token wird gespeichert, das alte verschwindet.
- Ein fremder Fingerprint verhindert die Erneuerung.
- `Origin`, `Sec-Fetch-Site` und ein falscher API-Key ergeben 403.
- Eine zweite Instanz bekommt den Port nicht.
- `Transfer-Encoding` ergibt 411, eine unlesbare Länge 400. Nach jeder
  Ablehnung ist die Verbindung zu, ungelesene Bytes werden keine Anfrage.
- Kein Log-Eintrag enthält ein Token.
