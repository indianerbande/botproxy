# botproxy

Ein lokaler Proxy für Clients, die nur einen statischen API-Key kennen, aber
mit einem Endpunkt sprechen sollen, der ein kurzlebiges JWT verlangt.

botproxy lauscht auf `127.0.0.1`, erneuert das Token rechtzeitig, meldet sich
bei Bedarf selbst per Device Code Flow an und reicht alles andere unverändert
weiter. Er kennt weder Modell noch Prompt noch Anfrageschema — er kennt eine
Adresse und ein Token.

Jeder Client, der eine OpenAI-kompatible Base URL akzeptiert, funktioniert.

## Installation

botproxy läuft unter **Windows**. Andere Systeme sind kein Ziel.

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Start

`start-botproxy.cmd.example` nach `start-botproxy.cmd` kopieren, Werte
eintragen, doppelklicken.

Beim ersten Start öffnet sich der Browser zur Anmeldung. Danach steht im
Fenster, worauf botproxy lauscht, bis wann das Token gilt und ob der Endpunkt
antwortet — dazu Base URL und API-Key für den Client.

## Einstellungen

Alles über Umgebungsvariablen.

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `BOTPROXY_BASE_URL` | — | Zieladresse, Pfad inklusive |
| `BOTPROXY_AUTHORITY` | — | Basis-URL des Identity Providers |
| `BOTPROXY_CLIENT_ID` | — | Anwendung, für die der Provider Token ausstellt |
| `BOTPROXY_SCOPES` | `openid allatclaims offline_access` | ohne `offline_access` kein Refresh-Token; `allatclaims` weglassen, wenn der Provider ihn nicht kennt |
| `BOTPROXY_PORT` | `8127` | |
| `BOTPROXY_REFRESH_MARGIN` | `300` | Sekunden vor Ablauf wird erneuert |
| `BOTPROXY_CHECK_INTERVAL` | `60` | Sekunden zwischen zwei Prüfungen |
| `BOTPROXY_UPSTREAM_TIMEOUT` | `600` | Sekunden, die auf den Endpunkt gewartet wird |

Token und lokaler API-Key liegen unter `%USERPROFILE%\.botproxy\`. Lesen
dürfen sie, wem das Benutzerprofil gehört — die Dateien erben dessen Rechte.

**`BOTPROXY_AUTHORITY` und `BOTPROXY_BASE_URL` gehören zur selben Umgebung.**
Ein Endpunkt nimmt nur Token seines eigenen Ausstellers an. Passen die beiden
nicht zusammen, antwortet er auf jedes Token mit 401, auch auf ein eben
ausgestelltes. botproxy erneuert dann einmal, reicht die Ablehnung danach
durch und sagt im Fenster, woran es vermutlich liegt.

Ein 403 vom Endpunkt heißt dagegen: das Token wurde verstanden, es fehlt die
Berechtigung. Das klärt der Betreiber, nicht botproxy. Die 403-Antworten von
botproxy selbst tragen `"type": "botproxy"` und sind daran zu unterscheiden.

## Client einrichten

Provider vom Typ *OpenAI Compatible*, Base URL `http://127.0.0.1:8127/v1`, als
API-Key den Wert aus der Statuszeile. Die Modellliste holt der Client selbst
über `/v1/models`.

Anfragen brauchen eine `Content-Length`. Mit `Transfer-Encoding: chunked`
antwortet botproxy 411: Er muss den Body vollständig kennen, um ihn nach einem
401 erneut senden zu können. Die üblichen OpenAI-kompatiblen Clients schicken
die Länge ohnehin mit.

**Modellnamen genau so eintragen, wie `/v1/models` sie nennt** — mit einem
führenden Schrägstrich, falls der Endpunkt einen hat, und in derselben
Schreibweise. botproxy liest den Body nicht und korrigiert daher keinen Namen;
ein abweichender ergibt ein 404 vom Endpunkt.

Eine Antwort kann dauern: rund eine Minute je Anfrage ist bei einem
ausgelasteten Endpunkt normal, gleichzeitige Anfragen stellt er oft nur in eine
Warteschlange. Das Timeout von botproxy liegt deshalb bei zehn Minuten
(`BOTPROXY_UPSTREAM_TIMEOUT`); ein Timeout im Client sollte nicht kürzer sein.

Schickt der Client eine feste Obergrenze für `max_tokens` mit, die den
gesamten Kontext des Endpunkts für die Antwort reserviert, bleibt nichts für
die Frage übrig und jede Anfrage scheitert. Diese Grenze gehört in die
Konfiguration des Clients, nicht hierher.

## Das Fenster

In einem Konsolenfenster zeigt botproxy vier feste Bereiche:

- **Kopf:** Adresse, Ziel, Zähler, Zustand des Tokens und des Endpunkts, dazu
  Base URL und API-Key für den Client.
- **Anfragen** (links): Uhrzeit, Nummer, Methode, Pfad und Größe.
- **Antworten** (rechts): zur selben Nummer der Status und der Verlauf —
  `wartet` (das Modell liest noch ein), `läuft` mit Durchsatz, `fertig` mit
  Dauer, Größe und Zeit bis zum ersten Stück.
- **Status** (unten): was botproxy selbst tut — Token erneuert, Anmeldung
  nötig samt Link, Fehler.

Inhalte von Anfragen und Antworten erscheinen nie, nur ihre Größen und Zeiten.
Wird die Ausgabe umgeleitet, schreibt botproxy stattdessen einfache Zeilen.
Beenden mit `Ctrl-C`.

## Zustand prüfen

```sh
python -m botproxy status
```

## Sicherheit

botproxy hält ein gültiges Token und fragt niemanden nach Legitimation. Zwei
Maßnahmen: ein lokaler API-Key, den der Client mitschicken muss, und die
Abweisung jeder Anfrage mit `Origin`- oder `Sec-Fetch-Site`-Header — beide
setzt ein Browser von sich aus, ein HTTP-Client nicht. Gebunden wird
ausschließlich an `127.0.0.1`.

## Was botproxy nicht tut

Anfragen umschreiben oder prüfen, Antworten zwischenspeichern oder
protokollieren, Modelle verwalten, mehrere Benutzer bedienen. Der Body wird
nie geparst.
