# botproxy

Ein lokaler Proxy für Clients, die nur einen statischen API-Key kennen, aber
mit einem Endpunkt sprechen sollen, der ein kurzlebiges JWT verlangt.

botproxy lauscht auf `127.0.0.1`, erneuert das Token rechtzeitig, meldet sich
bei Bedarf selbst per Device Code Flow an und reicht alles andere unverändert
weiter. Er kennt weder Modell noch Prompt noch Anfrageschema — er kennt eine
Adresse und ein Token.

Jeder Client, der eine OpenAI-kompatible Base URL akzeptiert, funktioniert.

## Installation

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Start

```sh
export BOTPROXY_BASE_URL=https://beispiel/api/v1
export BOTPROXY_AUTHORITY=https://idp.beispiel
export BOTPROXY_CLIENT_ID=deine-client-id
python -m botproxy
```

Beim ersten Start öffnet sich der Browser zur Anmeldung. Danach steht im
Fenster, worauf botproxy lauscht, bis wann das Token gilt und ob der Endpunkt
antwortet — dazu Base URL und API-Key für den Client.

Unter Windows: `start-botproxy.cmd.example` kopieren, Werte eintragen,
doppelklicken.

## Einstellungen

Alles über Umgebungsvariablen.

| Variable | Vorgabe | Bedeutung |
|---|---|---|
| `BOTPROXY_BASE_URL` | — | Zieladresse, Pfad inklusive |
| `BOTPROXY_AUTHORITY` | — | Basis-URL des Identity Providers |
| `BOTPROXY_CLIENT_ID` | — | dort registrierte Anwendung |
| `BOTPROXY_SCOPES` | `openid offline_access` | ohne `offline_access` kein Refresh-Token |
| `BOTPROXY_PORT` | `8127` | |
| `BOTPROXY_REFRESH_MARGIN` | `300` | Sekunden vor Ablauf wird erneuert |
| `BOTPROXY_CHECK_INTERVAL` | `60` | Sekunden zwischen zwei Prüfungen |

Token und lokaler API-Key liegen unter `~/.botproxy/`, jeweils mit `0600`.

## Client einrichten

Provider vom Typ *OpenAI Compatible*, Base URL `http://127.0.0.1:8127/v1`, als
API-Key den Wert aus der Statuszeile. Die Modellliste holt der Client selbst
über `/v1/models`.

Schickt der Client eine feste Obergrenze für `max_tokens` mit, die den
gesamten Kontext des Endpunkts für die Antwort reserviert, bleibt nichts für
die Frage übrig und jede Anfrage scheitert. Diese Grenze gehört in die
Konfiguration des Clients, nicht hierher.

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
