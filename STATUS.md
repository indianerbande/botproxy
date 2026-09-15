# Stand

Stand: 15. September 2026. Diese Datei ist der Einstieg für eine neue Sitzung.

## Was läuft

Der Proxy ist vollständig und getestet. 79 Tests grün, Ruff sauber.

Ein Lauf gegen einen Stub-Endpunkt funktioniert von außen: Anmeldung
übersprungen bei gültigem Token, Modelle erkannt, Anfragen durchgereicht,
Streaming gestückelt, 403 ohne Key und mit `Origin`.

**Gegen LM Studio gelaufen** (15. September 2026, lokal, festes Token ohne
`exp`): Modelle beim Start erkannt, `python -m botproxy status` gegen die
laufende Instanz meldet `ok`, 403 ohne Key, Streaming Stück für Stück (154
Ereignisse, Median 50 ms Abstand), unbekannte Felder in beide Richtungen
durchgereicht — auch `reasoning_content` —, 411 für chunked, und nach einem
Abbruch mitten im Stream bedient botproxy weiter. Anmeldung und 401 prüft
LM Studio nicht; dafür bleiben die Stubs. Einen Fehler des Endpunkts gibt
LM Studio nicht her: einen unbekannten Modellnamen beantwortet es mit dem
geladenen Modell.

**Mit Kilo Code als Client** (gleicher Abend, `qwen/qwen3.8-27b` in LM Studio):
eine Agentensitzung über ein fremdes Repository, mindestens sieben
Chat-Anfragen hintereinander mit Werkzeugaufrufen (Dateien lesen,
Shell-Befehle), dazwischen fast drei Stunden Pause bis zu einer Freigabe —
alles durch, kein Fehler im Fenster. Dabei aufgefallen, beides Kilo, nicht
botproxy:

- Einen Anbieter einzutragen stellt den Chat nicht auf ihn um. Das Modell muss
  im Chat eigens gewählt werden, sonst geht die Frage an Kilos eigenen Dienst.
  Ein gleich aussehender Anbieter `lmstudio` übergeht botproxy.
- Kilo schickt `max_tokens: 32000`. Gegen einen Endpunkt mit 32K Kontext
  braucht es in `kilo.jsonc` eine Grenze unter dem Anbieter; ein `models` auf
  oberster Ebene lehnt Kilo 7.x als ungültig ab und startet dann gar nicht.

**Gemessen: botproxy kostet rund eine Millisekunde, die Zeit gehört dem
Modell.** Dieselben Anfragen direkt an LM Studio und über botproxy:

| Anfrage | direkt | über botproxy |
|---|---|---|
| `GET /v1/models`, 30×, Median | 0,8 ms | 2,1 ms |
| Chat, kurzer Prompt, gestreamt, 3×, erstes Byte (Median) | 925 ms | 881 ms |
| dasselbe, gesamt (Median) | 3.507 ms | 3.509 ms |

Beim Chat liegt der Unterschied im Rauschen. Wo die Wartezeit mit Kilo
herkommt, zeigt das Log von LM Studio: Das Modell liest auf dem Mac rund 100
Token je Sekunde ein und schreibt rund 20. Kilos erste Anfrage bringt 16.847
Token Systemprompt mit — knapp drei Minuten bis zum ersten Wort. Die Eingabe
wuchs mit jeder gelesenen Datei bis rund 54.000 Token; sobald LM Studio den
Verlauf zwischenspeichert, sind nur noch die neuen paar hundert Token
einzulesen, dann in Sekunden.

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest                      # ~100 s, startet eigene Stubs auf freien Ports
ruff check . && ruff format .
```

Ein vollständiger `pytest`-Lauf dauert gut anderthalb Minuten; einzelne Dateien
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

**Nur Windows.** macOS und Linux sind kein Ziel. Dass `urllib` dort nicht den
Systemspeicher liest, ist deshalb kein offener Punkt. Entwickelt und getestet
wird trotzdem auch auf dem Mac; die Tests hängen an keinem System.

**Der Port gehört botproxy allein.** `socketserver` setzt mit
`allow_reuse_address` `SO_REUSEADDR`, und das erlaubt unter Windows einem
zweiten Prozess, denselben Port zu binden — eine zweite Instanz startete ohne
Fehler, und die Meldung „vermutlich läuft schon eine Instanz“ kam nie. Jetzt
ohne `SO_REUSEADDR` und mit `SO_EXCLUSIVEADDRUSE`. Auf dem Mac scheitert das
zweite Binden auch vorher schon; der Test
`test_zweite_instanz_bekommt_den_port_nicht` beweist die Korrektur also erst
unter Windows.

**Ein aufgelegter Client ist keine Meldung wert.** Beim Lauf gegen LM Studio
stand für jede zurückgesetzte Keep-alive-Verbindung ein voller Traceback im
Fenster — `socketserver` druckt ihn von sich aus, und Editor-Clients schließen
ihre ruhenden Verbindungen ständig. `Proxy.handle_error` schweigt jetzt bei
`ConnectionResetError`, `ConnectionAbortedError` (so meldet Windows es) und
`BrokenPipeError`; alles andere wird eine Zeile mit Typ und Meldung.

**Der Start mit der unveränderten Vorlage wird gegen die Vorlage selbst
geprüft.** `tests/test_config.py` liest die `set`-Zeilen aus
`start-botproxy.cmd.example` und lädt `config` damit neu, wie ein echter Start
es tut — Attribute direkt zu setzen übersprünge genau das Lesen der
Umgebung. Kommt ein Platzhalter in die Vorlage, prüft der Test ihn mit; findet
er keinen mehr, schlägt ein Wächtertest an.

**Der Schrägstrich am Ende fällt beim Einlesen weg**, nicht in der Prüfung.
Vorher schnitt `_env.require_url` ihn ab, aber `config.validate` verwarf das
Ergebnis — aus `https://host/pfad/v1/` wurde `…/v1//chat/completions`.
`config.py` kürzt jetzt `BASE_URL` und `AUTHORITY` direkt; `require_url`
prüft nur noch. So gilt es auch dort, wo `validate` nicht läuft.

**Das ZIP enthält nur den Betrieb.** Bezogen wird ausschließlich über „Download
ZIP“ von GitHub, und das baut `git archive` — `export-ignore` in
`.gitattributes` hält `tests/`, `hooks/`, `pyproject.toml`,
`requirements-dev.txt`, `CLAUDE.md` und `STATUS.md` heraus. Lokal nachgestellt:
15 Einträge, aus dem entpackten ZIP installiert und gestartet. Auf GitHub bleiben
die Dateien trotzdem sichtbar; nur das ZIP ist schlanker. Skripte stehen nicht
darin; `tests/test_verteilung.py` hält das fest. Entpacken über eine
alte Version löscht nichts — beim Update in einen frischen Ordner.

**Vollbildanzeige mit vier Bereichen, ohne Inhalt.** Kopf, Anfragen links,
Antworten rechts, Status unten. `monitor.py` bekommt nur Methode, Pfad ohne
Query, Größen, Zeiten und Statuscodes; ein Body kommt dort nie an, geparst wird
nichts. `screen.py` zeichnet mit ANSI-Steuerzeichen, unter Windows über
`ctypes` eingeschaltet — kein `curses`, keine Bibliothek. `render` ist eine
reine Funktion und ohne Terminal getestet. Die Antwortzeile zeigt `wartet`,
solange das erste Stück fehlt: bei einem Sprachmodell die Zeit, in der es den
Prompt einliest. Ohne Konsole (umgeleitete Ausgabe) bleibt es bei einfachen
Zeilen, und das Fenster sagt warum.

Geprüft auf Windows 11 in einer Konsole über `ssh -t` (ConPTY): Rahmen,
Umlaute und Umbruch stimmen, `Ctrl-C` gibt das Fenster zurück. Die komplette
Testsuite läuft dort durch; die Verteilungstests überspringen sich ohne
Git-Verzeichnis. Nicht gesehen: ein per Doppelklick geöffnetes
Konsolenfenster.

**Kein `chmod`.** Unter Windows schaltet es nur den Schreibschutz. Token und
Key sind geschützt, weil sie unter `%USERPROFILE%` liegen und dessen ACL erben.

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

**Ohne `Content-Length` kein Durchgang.** Der Body wird vor dem Weiterreichen
vollständig gelesen, damit er nach einem 401 erneut gesendet werden kann —
dafür braucht es die Länge vorab. Eine Anfrage mit `Transfer-Encoding` bekommt
411, auch wenn zusätzlich eine Länge dasteht (RFC 9112 §6.3: dann gilt sie
nicht); eine unlesbare Länge bekommt 400. Vorher ging in beiden Fällen ein
leerer Body weiter, und der Endpunkt hätte über ein fehlendes Feld geklagt.
Chunked zu lesen wäre machbar, aber ohne bekannten Client, der es braucht.

**Jede Ablehnung schließt die Verbindung.** Die meisten kommen, bevor der Body
gelesen ist. Auf einer offenen HTTP/1.1-Verbindung wurden diese Bytes bisher als
nächste Anfrage gelesen — mit gültigem Key darin auch weitergereicht. Kein Weg
am Key vorbei, aber eine Anfrage, die niemand so gestellt hat. Geprüft in
`test_ungelesener_body_wird_nicht_zur_naechsten_anfrage`.

## Offen

**Die Vollbildanzeige im Doppelklick-Fenster ansehen.** Geprüft ist sie nur
über `ssh -t` (ConPTY). Ein per Doppelklick auf `start-botproxy.cmd`
geöffnetes Konsolenfenster kann sich bei Farben, Rahmenzeichen und beim
Verkleinern anders verhalten. Dabei auch mit echtem Verkehr: Anfrage links,
`wartet` → `läuft` → `fertig` rechts.

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

**Der echte GitHub-Download ist ungeprüft.** Dass GitHub `export-ignore`
beachtet, ist nur lokal mit `git archive` nachgestellt. Nach dem Push einmal
herunterladen und nachsehen.

**Nicht automatisch getestet:** `__main__.status` gegen eine laufende Instanz
(von Hand gegen LM Studio geprüft).

**Kein Log in eine Datei.** Absichtlich: Header tragen das Token, Bodies den
Quelltext des Benutzers. Die Anzeige im Fenster zeigt deshalb nur
Verkehrsdaten.

**Der Pre-commit-Hook muss je Klon eingeschaltet werden.** `hooks/pre-commit`
liegt im Repo, läuft aber erst nach `git config core.hooksPath hooks` — die
Einstellung steht in `.git/config`, nicht im Repo. In diesem Klon ist sie seit
dem 15. September 2026 gesetzt, gitleaks 8.30.1 ist installiert, der Hook hat
ein eingeschleustes JWT abgewiesen, und die Historie bis dahin ist sauber. Er
ruft `gitleaks git --pre-commit --staged` auf — `gitleaks protect` gilt seit
8.19 als veraltet.
