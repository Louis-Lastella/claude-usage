# claude-usage

Eigenes Dashboard, das zeigt, was in [Hermes](https://github.com/NousResearch/hermes-agent) die Claude-Usage frisst:
jedes Tool, jeder Skill, jedes Plugin, jeder Teil des Systemprompts, Denken, Hintergrund-Prüfung, Cache-Neuaufbau nach
Pausen und Cache-Brüche. Dazu die Pro-Limits (5 Stunden, Woche, Extra-Guthaben) mit Prognose, Warnungen aufs Handy und
konkrete Spartipps aus den eigenen Daten.

Eine Datei, nur Python-Standardbibliothek, liest `~/.hermes/state.db` und `~/.hermes/logs/agent.log*` nur lesend.
Keine KI: Rangliste, Aufteilung, Prognose und Spartipps sind feste Rechenregeln, es geht nie eine Anfrage an ein Modell raus.

Läuft mit jeder Hermes-Installation. Welche Plugins und welcher Memory-Anbieter aktiv sind, liest es aus
`config.yaml` (`plugins.enabled`, `memory.provider`) und ordnet deren Teile im Systemprompt und an den Nachrichten
danach zu. SOUL.md, Hermes' eigene Hinweise und die Herkunft (Telegram, Discord, Slack, Cron-Jobs ...) erkennt es
ebenfalls selbst.

## Seiten

| Seite | Inhalt |
|---|---|
| `/` | Limits mit Prognose, Kennzahlen, Rangliste was am meisten frisst, Herkunft, Modelle, Spartipps |
| `/verlauf` | Kosten pro Tag nach den größten Posten, Heatmap nach Wochentag und Uhrzeit, Tagestabelle |
| `/details` | Alle Tools, Skills, Plugins, Systemprompt-Teile und Tool-Beschreibungen einzeln |
| `/sessions` | Teuerste Sessions mit Filter nach Herkunft und Suche, Reiter Cron-Jobs mit Kosten pro Lauf und Woche |
| `/projekte` | Kosten pro Projekt, antippen zeigt die Sessions dazu |

Zeitraum per `?p=w` (seit dem letzten Reset des Wochenlimits, Standard), `?p=1`, `?p=7` oder `?p=30`.

Hell und dunkel folgen der Systemeinstellung. Auf dem iPhone in Safari öffnen und „Zum Home-Bildschirm“ wählen, dann
startet es wie eine App.

## So wird gerechnet

1. **Echte Kosten:** `session_model_usage` enthält pro Session die Tokens, die Anthropic gemeldet hat (Input,
   Cache lesen, Cache schreiben, Output). Mit der offiziellen API-Preisliste (`PRICES` in `app.py`) ergibt das den
   exakten API-Gegenwert. Beim Pro-Abo zählt Anthropic das Limit nach denselben Gewichten.
2. **Aufteilung:** Jede Session wird Schritt für Schritt nachgespielt. Für jeden API-Call ist bekannt, was im Prompt
   stand (Systemprompt-Teile, Tool-Beschreibungen, jede Tool-Rückgabe, Plugin-Einblendungen, Nachrichten, Denken),
   was neu dazukam und was aus dem Cache kam.
3. **Cache:** Wo Hermes' `agent.log` den Call hat (`in=… cache=…`), zählt der echte Cache-Wert. So werden auch
   Cache-Brüche ohne Pause sichtbar. Sonst gilt die Regel: nach mehr als `cache_ttl` (5 min oder 1 h) Pause ist der
   Cache weg.
4. **Eichung:** Größen kommen aus der Textlänge (Bilder pauschal) und werden pro Session auf die echten Token-Zahlen
   skaliert. Die Summe aller Posten ist immer exakt gleich den echten Kosten (Selbsttest prüft das).
5. **Zeitraum:** Kosten zählen nach dem Zeitpunkt jedes einzelnen Schritts. Eine Session, die vor dem Zeitraum begann,
   zählt nur mit dem Teil, der in den Zeitraum fällt.

Nicht gespeichertes Denken (Output, den Hermes nicht als Text ablegt) wird aus der Differenz zum echten Output
ergänzt und bleibt wie echtes Denken im Verlauf.

**Projekte:** Hermes speichert nur bei Sessions im Terminal einen Arbeitsordner. Alle anderen Sessions zählen zu dem
Projekt, dessen Pfad in ihren Tool-Aufrufen am häufigsten vorkommt: Ordner unter `/opt` und `/srv` sowie Git-Repos im
Home-Ordner (auch eine Ebene tiefer, etwa `~/projects/app`). Hermes' eigener Ordner zählt nur, wenn sonst kaum etwas
vorkommt.

**Prognose:** Die Woche wird linear aus dem Tempo seit dem letzten Reset hochgerechnet, das 5-Stunden-Fenster aus dem
Tempo der letzten Stunde.

## Warnungen per ntfy

Alle 10 Minuten prüft der Server die Limits und schickt höchstens einmal pro Fenster eine Push-Nachricht, wenn

- die Wochenprognose über 100 % liegt (frühestens einen Tag nach dem Reset),
- das 5-Stunden-Fenster beim Tempo der letzten Stunde in weniger als 30 Minuten voll ist,
- das Extra-Guthaben angezapft wird.

Das Ziel kommt aus Hermes' ntfy-Einstellungen (`NTFY_HOME_CHANNEL`, `NTFY_SERVER_URL`, `NTFY_TOKEN` in `~/.hermes/.env`)
oder aus `CLAUDE_USAGE_NTFY` (volle URL mit Topic). Ohne beides bleiben Warnungen aus.

## Betrieb

```bash
python3 app.py --test        # Selbsttest
python3 app.py --ntfy-test   # Test-Nachricht an ntfy schicken
PORT=7681 python3 app.py     # Server auf 127.0.0.1:7681
```

| Variable | Bedeutung |
|---|---|
| `PORT` | Port auf 127.0.0.1, Standard 7682 |
| `HERMES_HOME` | Hermes-Ordner, Standard `~/.hermes` |
| `CLAUDE_USAGE_DATA` | Ablage für Messungen und Limit-Verlauf, Standard `data/` neben `app.py` |
| `CLAUDE_USAGE_NTFY` | ntfy-Ziel, falls nicht aus Hermes |
| `CLAUDE_USAGE_URL` | Adresse des Dashboards, ein Tipp auf die Warnung öffnet sie |

Im Hintergrund misst der Server einmal am Tag Systemprompt und Tool-Beschreibungen (`--snapshot`, läuft im
Hermes-venv) und holt alle 10 Minuten die Pro-Limits über Hermes' OAuth-Login. Der Token verlässt dabei den
Hermes-Prozess nicht. Beides landet in `data/`.

`claude-usage.service` ist eine Vorlage für einen systemd-Benutzerdienst mit dem Repo unter `~/claude-usage`.
