# Wie die RIPPLE-Seite aussehen soll

**Status:** Entwurf, 15.11.2026. Gehört zu Issue #1 (GitHub `<owner>/ripple`).
Die Vorlage ist [`dashboard-mockup.html`](dashboard-mockup.html) — eine Datei,
kein CDN, kein Framework, kein Build-Schritt, kein `fetch()`. Seit #84 ist sie
**kein handgezeichneter Entwurf mehr, sondern ein erzeugter Schnappschuss der
gebauten Seite**: sechs Tabs untereinander, erzeugt von
`tools/mockup_snapshot.py` aus einer laufenden Instanz, mit dem Commit und dem
Neuerzeugungs-Befehl im Kopf der Datei. Sie wird nach jeder sichtbaren Änderung
neu erzeugt, nicht gepflegt. Der Entwurf ist dieses Dokument.

Dieses Dokument ist die Referenz für den Rebuild (#2). Es beschreibt nicht den
Entwurf als Bild, sondern die Entscheidungen dahinter: welche Farbe was
bedeutet, warum Zahlen rechts stehen, was in jedem Tooltip steht, wo das Layout
bricht. Wer die Seite neu baut, soll gegen diese Sätze prüfen können und nicht
gegen einen Screenshot.

---

## 1. Der Befund

Gemessen an der laufenden Seite: **4 948 px hoch bei 741 px Breite**, eine
Spalte, Spaltenköpfe in 10 px, Speicherbalken als Haarstrich ohne Grenze, fünf
Zustände als Wörter in derselben grauen Schrift, und alle zwei Sekunden ein
Vollreload, der die Scrollposition verwirft.

Die Ursache ist nicht Nachlässigkeit. FA-22 ist eine **Inhaltsliste**, und sie
wurde 1:1 als Tabelle umgesetzt. Die vier Punkte der Liste sind aber vier
verschiedene Arten von Aussage:

| FA-22 | Was es eigentlich ist | Wie es hier aussieht |
|---|---|---|
| 1 Flotte | zwei Vergleichsobjekte, je ein Zustand plus ein Vorrat | zwei Karten mit Speicherbalken |
| 2 Modelle | eine Matrix (Modell × Knoten) mit Resident/Verfügbar | eine echte Tabelle |
| 3 Verteilung | ein Soll-Ist-Vergleich, also Balken | Balken mit Zahlen |
| 4 Entscheidungen | ein Protokoll, chronologisch | scrollendes Feld mit stehendem Kopf |

Eine Tabelle für alle vier ist der Grund für die Höhe: jede Zeile trägt eine
Spalte, die sie nicht braucht, und die Breite bleibt ungenutzt.

**Nummerierung, Stand #19 (neu gezählt mit #56, fünf Tabs seit #73).** Jede
Überschrift trägt ihre Nummer, keine Lücke — und die Nummern sind die
**Lesereihenfolge innerhalb der Tabs**, nicht die Reihenfolge der FA-22-Liste.
`1 Flotte` (mit den Kennzahlen), `2 Verteilung` (ein Block je Modell,
nebeneinander), `2.1 Auslastung je Host` (über die volle Breite, direkt unter
`2`), dann `3 Modellmatrix`, `4 Threads`, `5 Letzte Interaktionsrunde`, `6
Betrieb` mit `6.1 Wartung`, `6.2 Modelle`, `6.3 Kalibrierung`, `6.4 Replikation`,
`6.5 Parameter`, `6.6 Neustart`. Damit läuft jeder Tab aufsteigend, und die
Tab-Reihenfolge ist die Zahlenreihenfolge (`1, 2, 2.1 | 3 | 4 | 5 | 6.x`). Die
Nummer sitzt in `.idx` mit einem Ring in `--maint`: ein **Kreis** um eine
Top-Level-Nummer, eine **Ellipse** (Pille, Breite folgt dem Text) um eine
Unternummer. Abschnitt 5 heißt seit der Betreiberkorrektur `Letzte
Interaktionsrunde`, nicht mehr `Letzte Entscheidungen`; das Protokoll selbst
bleibt ein Entscheidungsprotokoll.

Weil die Nummern jetzt der Lesereihenfolge folgen, tragen sie die Zuordnung zur
Anforderung nicht mehr selbst. Die steht hier:

| Seite | FA-22-Punkt |
|---|---|
| `1` Flotte + Kennzahlen | 1 Flotte |
| `3` Modellmatrix | 2 Modelle |
| `2` Verteilung | 3 Verteilung |
| `4` Threads | 3 Verteilung (die Registry zeigt den Ist-Anteil, auf den sich die Verteilung bezieht) |
| `5` Letzte Interaktionsrunde | 4 Entscheidungen |
| `2.1` Auslastung je Host | — (über FA-22 hinaus, issue #33) |
| `6` Betrieb mit `6.1`…`6.6` | — (über FA-22 hinaus, #10, #11, #13, #16, #25) |

## 2. Drei Regeln

1. **Die Breite ist ein Werkzeug, kein Zufall.** Bei 1440 px stehen Flotte,
   Modelle und Verteilung nebeneinander. Was untereinander gehört (Protokoll),
   steht untereinander; was verglichen wird (zwei Knoten, zwei Modelle), steht
   nebeneinander.
2. **Zustand ist eine Farbe, kein Wort.** `healthy` / `loading` / `degraded` /
   `maintenance` / `down` müssen aus dem Augenwinkel unterscheidbar sein — und
   zwar auch dann, wenn man die Farben nicht unterscheiden kann. Deshalb trägt
   jeder Zustand zusätzlich eine **Form**.
3. **Eine Zahl ohne Erklärung ist keine Zahl.** Jede Kennzahl auf der Seite hat
   einen Tooltip: ein Satz, was sie ist, und der Endpunkt samt Feld, aus dem sie
   kommt. 593 Tooltips, 56 verschiedene Definitionen.

## 3. Farbe: Zustand, nicht Dekoration

| Zustand | Farbe | Hex | Form | Bedeutung |
|---|---|---|---|---|
| `healthy` | blaugrün | `#57a639` | gefüllter Kreis | antwortet, Identität stimmt |
| `loading` | himmelblau | `#5aa9e6` | halber Kreis mit Ring | lädt ein Modell, nimmt aber Arbeit an |
| `degraded` | orange | `#e8a33d` | gefülltes Dreieck | antwortet, aber über Schwelle oder mit Fehlern |
| `maintenance` | flieder | `#c08bd6` | offenes Quadrat | vom Betreiber aus dem Pool genommen |
| `draining` | weiß | `#e8ecf1` | Tropfen | läuft aus, nimmt keine neue Arbeit an |
| `down` | zinnober | `#e8604a` | Kreuz | antwortet nicht |
| `disabled` | grau | `#7b8694` | hohles Dreieck | vom Betreiber stillgelegt, mit Weg zurück |

**Eine Ausnahme, und nur eine: die Wortmarke.** `RIPPLE` oben links steht in
`#9BBA7D`, einem gedämpften Salbeigrün. Das ist der einzige dekorative Ton der
Seite. Er liegt bewusst neben der Zustandsfarbe `healthy` (`#31c48d`) und nicht
auf ihr: andere Farbe, andere Form (Textur statt Kreis), andere Größe (40 px).
Auf einem Datenelement darf `#9BBA7D` nie erscheinen — dort gilt weiter: Farbe
trägt Zustand, nie Dekoration.

**Warum diese Töne.** Die Palette folgt Okabe-Ito, der Referenzpalette für
Farbfehlsichtigkeit. Reines Grün gegen reines Rot ist genau die Paarung, die
bei Deuteranopie zusammenfällt; Blaugrün gegen Zinnober liegt auch dann noch
auseinander, wenn der Farbton verschwindet, weil sich die Helligkeit
unterscheidet. Der Test ist nicht der Bildschirm, sondern der Ausdruck in
Graustufen — dort bleiben sieben verschiedene **Silhouetten** übrig.

**Warum Formen und nicht nur Farbe.** Ein Kreuz, ein Dreieck und ein offenes
Quadrat sind auch bei 9 px noch unterscheidbar, ein Farbton bei 9 px nicht.
Die Form ist die primäre Information, die Farbe verstärkt sie. Das Wort steht
zusätzlich im Chip, weil ein Dashboard auch vorgelesen und zitiert wird — aber
niemand soll das Wort lesen müssen, um den Zustand zu erkennen.

**Die Legende ist Teil des Entwurfs.** Das Zustandssystem steht einmal
ausgeschrieben unter dem Kopf, mit allen sieben Zuständen und ihren Formen. Es
ist die einzige Stelle, an der ein neuer Leser die Bedeutung lernt; deshalb
steht sie oben und nicht in einem Handbuch. Im Abzug sind beide Knoten
`healthy` — die anderen fünf Zustände sind nur in der Legende zu sehen.

**Wo Farbe *nicht* Zustand bedeutet** — das ist die eigentliche Disziplin:

- **Der Speicherbalken ist keine Ampel.** Die Füllung ist eine Menge, kein
  Zustand; sie ist blau, solange die harte Grenze nicht überschritten ist. Erst
  darüber wird sie zinnober, weil sie dann tatsächlich ein Zustand ist. Ein
  Balken, der bei 82 % schon rot leuchtet, erzieht dazu, Rot zu ignorieren.
- **Gründe bleiben grau.** `thread` gegen `ephemeral` ist eine Ursache, kein
  Zustand. Ein Chip in Zustandsfarbe würde das System verwässern.
- **Abweichungen sind bernstein, und nur ab 10 Prozentpunkten.** Darunter ist
  die Abweichung Rauschen und bleibt neutral.
- **In der Verteilung ist Grau das Soll und Blau das Ist.** Der Vergleich ist
  die Aussage; zwei kräftige Farben würden ihn zu einem Wettkampf machen. Das
  Soll ist keine Fläche, sondern eine **Marke**: ein 2 px breiter Strich auf der
  Spur, der auch bei 0 % und 100 % noch sichtbar bleibt. Zwei übereinandergelegte
  Balken hätten den kleineren verdeckt und den Vergleich genau dort zerstört,
  wo er interessant ist.

**Zwei Träger kamen später dazu (#18, #23), und beide bleiben in dieser Regel.**

- **Das Blinken zeigt *wann*, nicht *was* (#18).** Der Punkt neben einem Modell
  sagt „geladen"; solange der Knoten an diesem Modell arbeitet, blinkt er im
  Takt 1 Hz mit einer 500 ms helleren Hälfte **im gleichen Farbton**: orange
  (`--warn`) im Prefill, grün (`--ok`) im Decode, danach wieder stehend. Der
  Gesundheitspunkt des Knotens blinkt grün, sobald **irgend ein** Modell auf ihm
  in PP oder TG ist. Der Grund ist gemessen: ein 26 s Prefill sieht über das Netz
  wie ein leerer Knoten aus, und die Seite hat keine andere Stelle, an der sie
  das zeigen könnte. Die Farbe bekommt dadurch keine neue Bedeutung — orange
  heißt weiter „Achtung", grün weiter „antwortet"; dazu kommt nur der Zeitpunkt.
  Bei `prefers-reduced-motion: reduce` entfällt die Animation, die Phase bleibt
  als **stehende hellere Tinte** (`--warn-bright`/`--ok-bright`) lesbar — sonst
  wäre die Information für genau die Leute weg, die sie am wenigsten schnell
  nachlesen können.
- **Das Schloss ist Bedienerzustand, nicht Störung (#23).** Über dem Modellnamen
  steht ein Vorhängeschloss: offen in `--ok`, solange nichts gesperrt ist,
  geschlossen in `--down` und der Modellname in `--load`, sobald gesperrt. Das
  ist die **einzige** Stelle, an der `--down` „du hast das gemacht" heißt und
  nicht „etwas ist kaputt" — deshalb trägt der Tooltip das Wort dazu, und
  deshalb ist das Icon ohne Admin-Schlüssel sichtbar, aber wirkungslos. Ein
  zweites Icon je Knotenzeile gibt es nicht: die Sperre gilt dem Modell, nicht
  dem Knoten, und zwei Icons für einen Zustand würden zwei Zustände behaupten.

### Der Host-Streifen (issue #89)

Dreißig Threads lesen zu müssen, um zu wissen, welcher auf welchem Knoten
sitzt, kostet Aufmerksamkeit — und die Seite kennt die Antwort längst. Jeder
Block, der zu einem Knoten gehört, trägt deshalb einen **Streifen** an seinem
linken Rand: die Flottenkarte, der Speicherbalken in ihr, der Balken in der
Verteilung und die Thread-Zeile. Ein Blick sagt „dieser Thread sitzt dort", ohne
dass eine Spalte gelesen wird. Der Name bleibt trotzdem im Text: der Streifen
macht das Scannen schneller, er ersetzt die Auskunft nicht.

**Die Farbe leitet sich aus der `node_id` ab, nie aus dem Index.** Ein Knoten,
der den Pool verlässt, würde sonst alle anderen ummalen, und das Gedächtnis des
Betreibers („M3 ist der grüne") wäre nach jedem Neustart falsch. Ohne gemeldete
`node_id` tritt der Name an ihre Stelle — ebenfalls stabil, ebenfalls nicht
positional. Aus 32 Bit FNV-1a über diese Zeichenkette fallen Ton und Muster:

| Platz | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| Ton | 1 | 2 | 3 | 4 | 1 | 2 | 3 | 4 |
| Muster | `solid` | `hatch` | `dots` | `lines` | `hatch` | `dots` | `lines` | `solid` |

| Platz | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|
| Ton | 1 | 2 | 3 | 4 | 1 | 2 | 3 | 4 |
| Muster | `dots` | `lines` | `solid` | `hatch` | `lines` | `solid` | `hatch` | `dots` |

Kurz: `Ton = Platz % 4`, `Muster = (Platz / 4 + Ton) % 4`. Die **Drehung** ist
der Punkt — zwei Knoten mit demselben Ton bekommen nie dasselbe Muster. Ein
Platz kann doppelt vergeben werden (dann trägt der Name die Auskunft), aber nie
ein Ton ohne ein anderes Muster.

**Die vier Töne, gemessen.** Sie sitzen in der Mitte der vier größten Lücken der
Zustandspalette (36° · 103° · 206° · 282° · 8°) und damit 33–43° von der
nächsten Zustandsfarbe entfernt; keiner liegt auf einem Zustand. Die Helligkeit
ist gestaffelt, damit zwei Töne auch ohne Farbkanal auseinanderfallen:

| Ton | Hex | Farbton/S/V | L* | gegen `--surface` | Wäsche (30 %) | Wäsche gegen `--surface` |
|---|---|---|---|---|---|---|
| `--host-1` | `#56dbc5` | 170°/61 %/86 % | 80 | 10,66:1 | `#26514f` | 2,05:1 |
| `--host-2` | `#9fad58` | 70°/49 %/68 % | 68 | 7,42:1 | `#3c432e` | 1,76:1 |
| `--host-3` | `#bd6c9b` | 325°/43 %/74 % | 56 | 5,00:1 | `#453042` | 1,52:1 |
| `--host-4` | `#6964b0` | 244°/43 %/69 % | 46 | 3,50:1 | `#2c2d48` | 1,36:1 |

Paarweise, Tinte gegen Tinte: 1/2 **1,44:1** · 1/3 2,13:1 · 1/4 3,05:1 ·
2/3 1,49:1 · 2/4 2,12:1 · 3/4 **1,43:1**. Der schwächste Wert ist die Untergrenze
der Zusage: zwei Töne sind auch in Graustufen zwei. Die Wäsche ist der Grund
unter dem Muster, kein Träger einer Aussage — sie liegt bewusst unter 3:1, weil
sie nichts bedeutet; die Tinte darüber trägt.

**Das Muster, nicht nur die Farbe (§6).** Vier Zeichnungen: volle Tinte,
45°-Schraffur (2 px auf 5 px), Punkte (1,2 px auf einem 5-px-Raster),
waagrechte Linien (2 px auf 5 px). Sie liegen als `repeating-linear-gradient`
bzw. `radial-gradient` auf der Wäsche ihres Tons, damit ein gepunkteter Streifen
seinen Ton behält.

**Graustufen, gemessen statt behauptet.** Am laufenden Dienst (`:8011`, zwei
Knoten) mit `html{filter:grayscale(1)}` gerendert und die Streifen pixelweise
ausgezählt (6 px × 160 px, sechsfach vergrößert):

| Knoten | Ton/Muster | Grauwerte im Streifen | Tinte | Zeilenwechsel | Spaltenwechsel |
|---|---|---|---|---|---|
| alpha | 1 / `hatch` | 22 · 72 · 189 | 34,3 % | 0,8 | 2,4 |
| beta | 3 / `lines` | 22 · 54 · 129 | 34,3 % | 4,3 | 1,5 |

Ohne Farbkanal bleiben zwei Merkmale: die **Tinte** liegt 2,07:1 auseinander
(189 gegen 129), und die **Zeichnung** ist gespiegelt — der Schraffurstreifen
wechselt quer zur Spalte (Spaltenwechsel 2,4 gegen Zeilenwechsel 0,8), der
Liniestreifen längs (4,3 gegen 1,5). Beides ist eine Zahl, kein Eindruck.

**Der Streifen ist kein Zustand.** Er ist 6 px breit (3 px in der Verteilungsspur,
4 px in der Thread-Zeile, **gar nicht im Speicherbalken** — siehe unten), liegt
`position:absolute` im **vorhandenen** Innenabstand von 16 px und ist
`aria-hidden` — die Karte wächst um keinen Pixel (gemessen: 668,5 × 405,45 px mit
und ohne Streifen, Flotten-Panel 1361 × 405,45 px, `scrollWidth == clientWidth`).
Die sieben Knotenzustände behalten ihre Formen und ihre Farben; der Streifen
sitzt am Blockrand, nie im Zustandsbereich (Chip, Punkt, Dreieck).

**Wo kein Streifen hingehört (19.11.).** Der Speicherbalken trug eine 4-px-Fassung
des Streifens, damit der Ton auch unter der Füllung sichtbar bleibt. Gemessen
ist das falsch gedacht, in beiden Layouts: bei 756 px Fensterbreite beginnt der
Balken bei x 33, der Kartenstreifen liegt bei 33..39 — der Balken startet
**unter** dem Streifen. Bei 1440 px beginnt er bei 49, der Streifen bei 33..39 —
10 px Abstand, aber **dieselbe Flucht und dasselbe Muster**, und das liest das
Auge als eine durchlaufende Schraffur, die in den Balken blutet. Die Marke lag
also entweder auf dem Streifen, der den Ton ohnehin trägt, oder unmittelbar
daneben in derselben Zeichnung.

Die Regel daraus: **eine Marke muss von ihrem Nachbarn durch Achse oder Abstand
getrennt sein, nicht nur durch das Muster.** Teilen zwei Elemente Ton und Achse,
ist das zweite keine Marke, sondern Rauschen. Der Balken zeichnet deshalb keinen
Streifen (`display:none` mit der Messung im CSS-Kommentar), der Ton kommt vom
Kartenstreifen.

**Grenze.** Sechzehn Plätze für beliebig viele Knoten: ab dem fünften kann ein
Platz doppelt vergeben werden, und zwei Knoten tragen dann dieselbe Farbe *und*
dasselbe Muster. Der Name steht in jeder Zeile, die Auskunft geht also nicht
verloren — die Farbe hört nur auf, für dieses Paar zu helfen.

## 4. Typografie

| Stufe | Größe | Verwendung |
|---|---|---|
| 40 px | Wortmarke | `RIPPLE` in Rubik Microbe, `#9BBA7D`, Sperrung 0.02 em |
| 20 px | Abschnittstitel, Kennzahl | `Flotte`, `Modelle`, der große Wert einer Karte |
| 17 px | Knotenname, Betragszeile | `alpha`, `432,2 GB von 525,4 GB` |
| 14 px | Fließtext | Hinweise, Erklärungen |
| 13 px | Tabellenzelle, Sekundärtext | alle Datenzeilen |
| 12 px | Spaltenkopf, Einheit, Tooltip | `GRÖSSE`, `GB · geschätzt / gemessen` |

**Nichts unter 12 px.** Die 10-px-Spaltenköpfe der alten Seite waren kein
Stilmittel, sondern ein Defekt: bei 10 px ist ein Versalwort mit Sperrung
gerade noch ein Muster, kein Wort mehr. 12 px ist die Untergrenze, auch für
Einheiten und Tooltips.

**Einheiten stehen im Kopf, nicht in der Zelle.** `Größe` mit der Unterzeile
`GB · gesch. / gem.`, darunter `453,6 / 432,0`. Die Zelle enthält nur Ziffern;
die Einheit steht einmal, nicht dreißigmal. Das ist der Unterschied zwischen
einer Spalte, die man überfliegt, und einer, die man liest.

**Eine Kopfzelle je Datenspalte — und keine Unterzeile breiter als ihre
Spalte.** Das Kopfband der Modelltabelle ist acht Zellen breit, weil der
Datenkörper acht Spalten hat; die Zählung ist maschinell geprüft
(`thead th` == `tbody tr td`), nicht nach Augenmaß. Jede Unterzeile nennt die
Einheit *ihrer* Spalte und bricht innerhalb der Spalte um, statt unter die
Nachbarspalte zu laufen: `geladen` trägt `ja/nein` (ein Zustand, keine Größe),
`Größe` trägt `GB · gesch. / gem.`, `Kontext` trägt `Token`, `tok/s` trägt
`live / capability` (§5: die Spalte zeigt zwei Basen). Vorher trug `geladen` gar
keine Unterzeile und die breite Größen-Unterzeile lief nach links unter die
Nachbarspalte — zwei Beschriftungen
fast ohne Abstand, und der Leser ordnete die Einheit der falschen Spalte zu.
Eine Haarlinie zwischen den Kopfzellen trennt sie zusätzlich, ohne den
Datenkörper zu zerschneiden.

**In der Verteilung hat jede Zahl ihre eigene Spalte.** `Ziel %`, `Ist %` und
`Abweichung` (Prozentpunkte) stehen als drei beschriftete Spalten nebeneinander,
mit einem Kopfband über dem Block. Vorher stand die Abweichung als Chip
zwischen den Wörtern „Ziel" und „Ist" und las sich dadurch wie der Zielwert.
Zwei Zahlen mit einer Beschriftung sind keine Aussage, sondern eine Falle.

**Ohne belegten Slot gibt es keinen Ist-Anteil.** Ein Block mit `0 von 40 Slots`
zeigt keine 100 % und keine 0 %, sondern den Satz „keine Slots belegt — kein
Ist-Anteil". 0 geteilt durch 0 ist keine Messung; die Prozentwerte wären eine
Division durch null, die wie ein Befund aussieht. Die Zeilen bleiben dabei
stehen: in der Spalte des Balkens steht dann `kein warmer Thread` — der Grund,
nicht die Folge.

**Zwei Währungen, zwei Spalten (#91).** Der Slot-Anteil ist die Routing-Größe,
aber er zählt **Threads**, nicht Arbeit. Ein Modell, das auf beiden Knoten
geladen ist und Anfragen bedient, ohne einen warmen Thread zu halten, hatte
hier gar keine Zeile — es sah abwesend aus, obwohl es arbeitete (gemessen:
`Qwen3.5-9B-oQ4e-mtp`, 127 Requests im Log, null Threads). Deshalb heißt die
Spalte jetzt **`Thread-Anteil`** und nicht mehr `Anteil`: sie sagt, was sie
misst. Daneben steht die zweite, ehrliche Zahl — **Requests je (Knoten, Modell)
im Fenster**, dasselbe Fenster wie der Verlauf (900 s). Ein Modell erscheint,
sobald es einen Slot hält **oder** im Fenster Requests bedient hat; ein Modell
ohne beides bekommt weiterhin keine leere Zeile. Damit ist „verschwunden" von
„gerade kein warmer Thread" unterscheidbar, ohne dass die Routing-Größe
verschwindet.

**Zahlen rechts, Text links.** Mit `font-variant-numeric: tabular-nums` haben
alle Ziffern dieselbe Breite; rechtsbündig stehen deshalb die
Dezimaltrennzeichen und die Größenordnungen untereinander. `0` und `524 288`
beginnen an derselben Kante zu enden — das Auge vergleicht eine Spalte an ihrem
rechten Rand, nicht an ihrem linken. Linksbündige Zahlen zwingen es, für jede
Zeile den Anfang zu suchen; genau das war auf der alten Seite der Fall.
Auch die Uhrzeit steht rechts, damit die Doppelpunkte auf einer Linie liegen.

**Zwei Zeilen pro Spaltenkopf.** Oben das Wort in Versalien mit Sperrung
(0.075 em), darunter die Einheit oder die Qualifizierung in normaler Schrift
und gedämpfter Farbe. Damit trägt der Kopf die Information, die sonst in jeder
Zelle stünde.

### Die Wortmarke

`RIPPLE` steht in **Rubik Microbe** (Google Fonts, SIL Open Font License 1.1),
40 px, Großbuchstaben, `#9BBA7D`. Die Schrift ist eine Textur: die Buchstaben
bestehen aus Zellen statt aus Flächen, was zum Namen passt und dem Kopf der
Seite etwas gibt, das keine Tabellenzahl leisten kann.

Eingebettet wird sie **lokal, nicht über Google Fonts**: die Seite lädt nichts
aus dem Netz (FA-22), also liegt die Datei im Repo — `ripple/static/fonts/`
in der echten Seite, im Mockup als Base64-Daten-URI in derselben HTML-Datei.
Ausgeliefert wird ein Subset auf die fünf Buchstaben des Wortes (R, I, P, L, E,
8,8 kB woff2 statt 235 kB); ein anderes Wort in dieser Schrift braucht ein neues
Subset. Die Lizenz liegt als `OFL.txt` neben der Datei und muss mitgeliefert
werden — Rubik Microbe ist unter OFL 1.1 frei, auch kommerziell.

Cap-Höhe 700/1000 em: bei 40 px sind die Versalien 28 px hoch. Das ist die
Größe, in der die Textur lesbar bleibt; unter 28 px zerfällt sie in Punkte.

## 5. Der Speicherbalken

Der Balken beantwortet genau eine Frage: **wie viel Platz ist noch, bevor der
Guard „nein“ sagt.** Deshalb ist er von links nach rechts gebaut:

1. **Spur = Decke.** Die volle Breite ist `ceiling_bytes`. Der Balken endet
   also dort, wo der Knoten nicht mehr darf, nicht irgendwo im Freien.
2. **Füllung = belegt, und zwar gemessen.** 14 px hoch, mit Innenkante, nicht
   6 px als Haarstrich. Ein Vorrat, den man als Linie zeichnet, sieht aus wie
   ein Trennstrich. Die Füllung ist die Summe der `actual_size` der residenten
   Modelle — der Wert nach dem Laden, nicht die Schätzung aus der
   Modellkonfiguration.
3. **Der Schätzungsüberschuss ist schraffiert.** Zwischen der Messung und
   `used_bytes` liegt die Strecke, die nur die Schätzung behauptet (M5 21,4 GB,
   M3 18,1 GB). Sie steht als zweites, heller schraffiertes Stück in derselben
   Spur: die Schätzung bleibt sichtbar, ohne sich als Messung auszugeben. Ist
   die Schätzung kleiner als die Messung, entfällt das Stück — die Messung
   bleibt die Füllung.
4. **Die Decke ist gezeichnet.** Eine 2 px breite Kante am rechten Ende, die
   über den Balken hinausragt. Ohne diese Kante ist „voll“ eine Behauptung.
5. **Die harte Grenze ist gestrichelt und schraffiert.** Ab `hard_threshold`
   (95 %) liegt ein schraffiertes Feld; dort verweigert der Guard das Laden
   (HTTP 507). Man sieht also gleichzeitig, wie voll es ist und wo die Wand
   steht. Die Linie sitzt bei `0.95 × ceiling_bytes`, also basisunabhängig —
   **entschieden wird dort aber auf der Schätzung**, weil der Guard mit
   `estimated_size` rechnet. Darum steht an der Zahl `Grenze 95 % (geschätzt)`,
   und die Farbe des Balkens folgt der Schätzung, nicht der Messung.
6. **Die Zahl steht darüber, die Reserve darunter.** `432,2 GB von 525,4 GB`
   (gemessen), darunter klein `geschätzt 453,6 GB`; in der Fußzeile
   `82,3 % belegt · 93,1 GB Reserve · Grenze 95 % (geschätzt)`. Belegung und
   Reserve sind auf der Messung gerechnet. Die Schätzung steht als eigener Satz
   daneben, nicht in derselben Rechnung.

Die Farbe wechselt erst, wenn die harte Grenze überschritten ist (§3). Bis
dahin ist der Balken eine Mengenangabe.

### Datenvertrag für #2: zwei Basen, nie gemischt

RIPPLE muss in `/healthz` je Knoten `used_bytes_measured` liefern — die Summe
von `models[].hosts[].actual_size` aus oMLX `/v1/models/status`. `used_bytes`
behält seine bisherige Bedeutung: `engine_pool.current_model_memory` aus oMLX
`/health`, also die Summe der **geschätzten** Größen. Beide Felder sind Pflicht,
weil sie zwei verschiedene Fragen beantworten.

**Platzierung, Eviction und der Guard rechnen weiter mit `estimated_size`** —
das ist der Wert, mit dem oMLX selbst kalkuliert. Eine Anzeige auf gemessener
Basis darf diese Entscheidung nicht umdeuten, und die beiden Basen werden nie in
einer Rechnung gemischt: Anzeige, Belegung und Reserve auf der Messung, Guard
und Schwelle auf der Schätzung. Fehlt `used_bytes_measured`, fällt die Karte auf
`used_bytes` zurück (`h.used_bytes_measured ?? h.used_bytes`) und zeigt dann
Schätzwerte — sichtbar schlechter, aber nie leer.

**Woher der Wert kommt.** Gebildet wird er in `ripple/hosts.py`
(`HostRuntime.used_bytes_measured()`), ausgeliefert von `HostRuntime.to_dict()`
und damit Teil jedes `hosts[]`-Eintrags in `/healthz`. Drei Fälle, die den Wert
sonst stillschweigend verfälschen würden:

- Ein Modell ohne `actual_size` wird **übersprungen**, nicht als 0 gezählt:
  „keine Angabe“ ist nicht dasselbe wie „null Byte“.
- Eine gemeldete **0 zählt als Beitrag**: ein residentes Modell, das keinen
  Speicher belegt (ein reiner Tokenizer-Eintrag), darf die Summe nicht
  „unbekannt“ machen.
- Gibt **kein** residentes Modell eine Größe an, ist das Feld `null` — „nicht
  gemessen“, nicht „leer“. Genau dafür existiert der Rückfall oben.

`tests/test_dashboard.py` hält beides fest: die Summe über zwei geladene Modelle
und die Regel, dass `None` und `0` sie nicht vergiften.

### Zwei tok/s-Basen: live und Kapazität (#30)

Dieselbe Regel gilt für die Rate, und sie stand lange falsch: die Spalte `tok/s`
hieß `gemessen` und zeigte `models[].hosts[].tg_tps`. Dieser Wert ist aber keine
Eigenschaft der Maschine. oMLX meldet `generation_tokens_per_second` **je
Anfrage**, und `HostRuntime.record_rate` glättet die Folge mit alpha 0,3. Die
Zahl fällt also, sobald mehrere Anfragen parallel laufen — sie beschreibt den
Moment, nicht den Knoten. Der Backend-Code weiß das längst: `ripple/placement.py`
rechnet mit `cfg.tg_tps` und sagt das im Kommentar dazu.

Die Spalte zeigt deshalb beide Basen, in der Form, die die Tabelle schon hat
(`inflight 3 / 6`): **live / Kapazität**.

| Hälfte | Was sie ist | Quelle |
|---|---|---|
| **live** | die Rate, die oMLX für die Anfragen dieses Modells auf diesem Knoten meldet, vom Dienst exponentiell geglättet — sie fällt mit der Parallelität | `/healthz → models[].hosts[].tg_tps` |
| **Kapazität** | der kalibrierte Wert aus `config.yaml`, unabhängig von der Last; dieselbe Zahl, mit der die Platzierung rechnet | `/config → hosts[].tg_tps` |

Beide Hälften sind einzeln nachprüfbar, weil sie aus zwei Endpunkten kommen, die
der 2-s-Takt ohnehin holt. Fehlt die Live-Messung, bleibt die Kapazität stehen:
`— / 43,5` sagt mehr als ein nackter Strich, der auch die kalibrierte Zahl
versteckt. Die Unterzeile der Spalte heißt `live / capability` (`live /
Kapazität`), und der Tooltip nennt beide Zahlen mit ihrer Quelle — das Wort
„gemessen“ allein behauptete eine Eigenschaft, die die Zahl nicht hat.

## 6. Layout und Umbruch

Seit #56 ist die Seite in Tabs geteilt, seit #73 sind es **fünf**; der Aufriss
unten ist der Tab `Flotte`. Er hat seit #73 **keine zwei Spalten mehr**: die
Thread-Registry ist ausgezogen, und was bleibt, ist eine Spalte — `2` legt seine
Modellblöcke selbst nebeneinander.

```
1440 px                         900 px (eine Spalte)
┌─────────────────────┐         ┌────────────────────┐
│ alpha    │ beta    │         │ alpha              │
├─────────────────────┤         ├────────────────────┤
│ Kennzahlen (7)      │         │ beta               │
├──────────┬──────────┤         ├────────────────────┤
│ DeepSeek │ Qwen     │  ← 2    │ Kennzahlen (2×2)   │
├──────────┴──────────┤         ├────────────────────┤
│ Auslastung (2.1)    │         │ DeepSeek (2)       │
└─────────────────────┘         ├────────────────────┤
                                │ Qwen (2)           │
                                ├────────────────────┤
                                │ Auslastung (2.1)   │
                                └────────────────────┘
```

`2` ist **ein Block je Modell** — der Modellname ist die Überschrift des
Blocks, die Blöcke stehen nebeneinander und unter 900 px untereinander. `2.1`
folgt direkt darunter und nimmt die volle Breite: die Lücke, die das alte
Zweispalten-Raster unter der kürzeren Spalte ließ („Zahnlücke"), gibt es nicht
mehr, weil es kein Raster mehr gibt, in dem zwei Blöcke um dieselbe Zeile
konkurrieren.

| Breite | Was passiert |
|---|---|
| ≥ 1180 px | Flotte zweispaltig, Kennzahlen siebenmal, Verteilung ein Block je Modell nebeneinander (~680 px je Block), 2.1 über die volle Breite |
| < 1180 px | Kennzahlen vier Spalten |
| < 1100 px | Flotte eine Karte je Zeile, Legende vierspaltig |
| < 900 px | Verteilung untereinander (ein Block je Modell in einer Spalte), Kennzahlen zwei Spalten, Kartenkennzahlen 2 × 2, Kopf untereinander, Legende zweispaltig |
| < 820 px | Tabellenschrift 12 px, Modellname bricht um statt abgeschnitten zu werden, Verteilungsbalken schmaler, Verlauf scrollt waagerecht |

### Tabs (issue #56, fünf seit #73)

Die Seite war bei 1440 × 1000 **2 785 px** hoch — 2,8 Bildschirme für ein
Fenster, das immer nur eine Frage beantwortet. Die Blöcke stehen deshalb in
Tabs; die Leiste (`nav.tabs`, `role="tablist"`) klebt am Seitenkopf. Seit #73
sind es fünf: die Thread-Registry wächst mit der Zahl der Gespräche und hat den
Flotten-Tab auseinandergezogen.

| Tab | Blöcke | Höhe bei 1440 × 1000 |
|---|---|---|
| `Flotte` | Legende, Banner, 1 Flotte + Kennzahlen, 2 Verteilung (ein Block je Modell), 2.1 Auslastung | 1 331 px (vorher, mit der Registry in der zweiten Spalte: 1 598 px) |
| `Modelle` | 3 Modellmatrix, über die volle Breite (die Tabelle braucht ihre 840 px) | 1 000 px (Prüfstand, 3 Modelle) |
| `Threads` | 4 Thread-Registry, **ohne eigenes Scrollfenster** | wächst mit der Registry: 12 Zeilen = 1 364 px, eine Zeile = 102 px |
| `Verlauf` | 5 Entscheidungsstrom, **ohne eigenes Scrollfenster** | 2 111 px bei vollem Ringpuffer (50 Zeilen) |
| `Betrieb` | 6 Betrieb mit 6.1 … 6.6 | 2 329 px aufgeklappt (der Tab klappt ihn auf), 61 px solange er zu ist |

**Warum `Threads` einen eigenen Tab hat und `Auslastung` (2.1) nicht:** die
Registry wächst mit jedem Gespräch — sie ist die einzige Liste auf der Seite,
deren Länge der Betreiber nicht in der Hand hat, und als Nachbar von `2` hat sie
den Flotten-Tab auf 1 598 px gezogen. `Auslastung` dagegen beantwortet dieselbe
Frage wie `2` — was die Knoten gerade tun —, hat eine feste Höhe (ein Balken je
Knoten) und gehört deshalb neben die Verteilung, nicht in einen eigenen Tab.

**Woher der Tab kommt:** aus dem URL-Hash — `#fleet` (auch die Vorgabe für
einen unbekannten oder fehlenden Hash), `#models`, `#threads`, `#history`,
`#ops`. `dashboard.js` liest ihn bei jedem Start und bei jedem `hashchange`; er
schreibt ihn über `history.replaceState`, also ohne Verlaufseintrag (der
Zurück-Knopf soll die Seite verlassen, nicht durch fünf Tabs blättern). Kein
`localStorage`: ein gemerkter Tab würde mit der Adresszeile streiten, und ein
Reload mit `#models` muss auf `Modelle` landen, nicht dort, wo man zuletzt war.
Der Tab wird auch nicht aus dem DOM gelesen — der 2-s-Patch ersetzt Knoten, ein
Zustand in einer Klasse wäre nach einem Tick weg.

**Warum die Nummern bleiben:** sie sind die **Lesereihenfolge** innerhalb der
Tabs. `1`, `2`, `2.1` laufen auf dem Flotten-Tab abwärts durch den Bildschirm,
`3` steht auf `Modelle`, `4` auf `Threads`, `5` auf `Verlauf`, `6` mit
`6.1`…`6.6` auf `Betrieb`, `7` mit `7.1`…`7.3` auf `Knoten`. Die Zahlen laufen
lückenlos von 1 bis 7; sie decken sich seit #71 **nicht mehr** mit den Positionen
der Tabs — die Leiste zählt sechs (`1`…`6`), und Tab 6 (`Knoten`) trägt den
Block `7`, weil der Tab angehängt wurde statt die Reihenfolge umzunummerieren.
Sie sind
**nicht** die Nummern der FA-22-Liste: die hat Modelle an zweiter und Verteilung
an dritter Stelle, was auf dem Flotten-Tab rückwärts zählen würde. Die Zuordnung
Seite → FA-22-Punkt steht deshalb als Tabelle in §1. Die Tabs tragen die Namen,
und die kommen aus dem Katalog (`tab.fleet`, `tab.models`, `tab.threads`,
`tab.history`, `tab.ops`).

**Der Spaltenkopf des Verlaufs** klebt unter der Leiste, nicht am Fensterrand:
`.log thead th{top:var(--tabbar-h)}`. Die Höhe der Leiste steht als eine
Variable (`--tabbar-h:45px`, gemessen: 44 px Knopf + 1 px Rand) und nicht in
der Regel — sie ändert sich mit der Typo-Skala, nicht mit dem Log. Auf
schmalen Fenstern (< 820 px) ist die Entscheidungstabelle breiter als die
Spalte; dort scrollt der Verlauf waagerecht in seinem Panel, und der Kopf
steht still (ein `overflow` am Panel würde ihn am Panel statt am Fenster
festmachen).

Gemessen an der Vorlage: **2 662 px** hoch bei 1440 px, **3 769 px** bei 1000 px,
**4 296 px** bei 741 px — in allen drei Fällen ohne waagerechten Überlauf
(`scrollWidth == clientWidth`). Mit Tabs bleibt davon der Tab `Flotte` übrig;
die anderen drei sind kürzer als ein Bildschirm oder werden beim Scrollen von
der klebenden Leiste begleitet.

Die letzte Zeile ist die ehrliche: bei 741 px ist die Seite mit 4 296 px kaum
kürzer als heute (4 948 px). Das ist keine Frage von CSS mehr. Wenn alles
untereinander steht, kostet jede der 29 Modellzeilen und jede der 50
Entscheidungen ihre Höhe. Wer die Seite auch auf schmalen Fenstern kurz haben
will, muss Inhalte zusammenklappen (nicht geladene Modelle, ältere
Entscheidungen) — das ist eine Inhaltsentscheidung für #2, keine
Layoutentscheidung.

## 7. Der Entscheidungsstrom

Die letzten 50 Entscheidungen sind ein **Protokoll**, kein Bericht. Deshalb:

- **stehender Spaltenkopf**, seit #56 unter der klebenden Tab-Leiste statt am
  Fensterrand, und **kein eigenes Scrollfenster mehr**: der Verlauf hat einen
  eigenen Tab, also scrollt die Seite. Vorher waren es 560 px Fenster für 50
  Zeilen × 27 px = 1 350 px Inhalt.
- **Zeit rechtsbündig**, damit die Minuten untereinander stehen.
- **Grund als Chip mit Erklärung.** Der Chip sagt `thread` oder `ephemeral`;
  der Tooltip sagt, was das bedeutet (Tabelle unten). Das Wort allein ist eine
  Vokabel, kein Grund.
- **Dauer mit relativem Balken.** Die Zahl sagt, wie lange es dauerte; der
  Balken (46 px, relativ zum längsten Wert im Puffer) sagt, ob das viel war.
  Ohne den Balken muss man 50 Zahlen vergleichen, um einen Ausreißer zu finden.
- **Status als Punkt plus Code.** Grün ab 200, bernstein ab 400, zinnober ab
  500. Der Punkt ist schneller als die Zahl und die Zahl ist eindeutig.
- **Fußzeile mit Median und p90** — die zwei Zahlen, die man aus 50 Zeilen
  sonst selbst ausrechnet.

## 8. Tooltips

Jeder Tooltip hat drei Teile: **Name — Definition — Quelle**, wobei die Quelle
immer den Endpunkt und den Feldpfad nennt.

```
inflight — Anfragen, die in diesem Moment auf diesem Knoten laufen.
Quelle: /healthz → hosts[].inflight
```

Umgesetzt als reines CSS (`::after` mit `attr(data-tip)`), ohne JavaScript:
rechtsbündige Zellen und die rechte Spalte öffnen nach links, damit die
Erklärung nicht aus dem Fenster läuft. Kein `title`-Attribut daneben — sonst
zeigt der Browser zwei Tooltips.

**Zur Quelle.** Der Issue-Text nennt `/healthz`, `/stats` und
`/admin/api/models`. Die Seite liest die ersten beiden; `/admin/api/models` wird
von RIPPLE nicht abgefragt. Die Tooltips nennen deshalb den Feldpfad im
Endpunkt, den die Seite tatsächlich liest, und dort, wo es zählt, den oMLX-
Ursprung in Klammern:

| Anzeige | kommt aus | ursprünglich aus |
|---|---|---|
| Speicher (gemessen/geschätzt), RTT, Knotenidentität | `/healthz` | oMLX `/v1/models/status`, `/health`, `/api/cluster/node_id` |
| Modellliste, Kontextlänge | `/healthz` | oMLX `/v1/models` |
| geladen, Größe geschätzt/gemessen | `/healthz` | oMLX `/v1/models/status` |
| Threads, Verteilung, Zähler | `/healthz`, `/stats` | RIPPLE selbst |

Wer den Tooltip liest, soll wissen, **welche Maschine die Zahl behauptet** —
eine Größe aus `/v1/models/status` ist eine Angabe von oMLX, keine Messung von
RIPPLE.


### Knotenkarte (Flotte)

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Zustand** | Ergebnis der letzten Erreichbarkeitsprüfung: antwortet der Knoten, stimmt seine Identität, flappt er? | `/healthz → hosts[].state` |
| **Adresse** | Adresse, über die RIPPLE den Knoten gerade anspricht; die Alternativen dienen dem Site-Wechsel (FA-21). | `/healthz → hosts[].address, addresses[]` |
| **Adresswechsel** | Wie oft RIPPLE seit dem Start auf eine andere Adresse desselben Knotens ausgewichen ist. | `/healthz → hosts[].addr_switches` |
| **oMLX-Version** | Version, die der Knoten über seine eigene Statusantwort meldet. | `/healthz → hosts[].version (oMLX /api/cluster/node_id)` |
| **Memory-Guard** | Ob der Knoten ein Laden ablehnt, wenn der Speicher über die harte Grenze laufen würde — HTTP 507 statt Eviction. | `/healthz → hosts[].guard_enabled` |
| **belegter Speicher** | Gemessener Modellspeicher der residenten Gewichte auf diesem Knoten — Summe der tatsächlichen Größen nach dem Laden, nicht der gesamte RAM des Rechners. | `/healthz → hosts[].used_bytes_measured (Summe models[].hosts[].actual_size, oMLX /v1/models/status)` |
| **belegter Speicher, geschätzt** | Summe der geschätzten Modellgrößen auf diesem Knoten — der Wert, mit dem oMLX und der Guard rechnen. | `/healthz → hosts[].used_bytes (oMLX /health → engine_pool.current_model_memory)` |
| **Decke** | Obergrenze, gegen die der Guard prüft; darüber wird nichts mehr geladen. | `/healthz → hosts[].ceiling_bytes (oMLX /health)` |
| **Belegung** | gemessener belegter Speicher geteilt durch die Decke. | `berechnet: used_bytes_measured / ceiling_bytes` |
| **Reserve** | Decke minus gemessener belegter Speicher: so viel Platz ist noch da, bevor der Guard zuschlägt. | `berechnet: ceiling_bytes − used_bytes_measured` |
| **harte Grenze** | Anteil der Decke, ab dem der Guard ein Laden verweigert. Gestrichelt im Balken. Der Guard rechnet mit der Schätzung, nicht mit der Messung. | `/healthz → hosts[].hard_threshold × ceiling_bytes` |
| **inflight** | Anfragen, die in diesem Moment auf diesem Knoten laufen. | `/healthz → hosts[].inflight` |
| **max. inflight** | Obergrenze gleichzeitiger Anfragen; darüber spillt RIPPLE auf den anderen Knoten (FA-5). | `/healthz → hosts[].max_inflight` |
| **Spill-Schwelle** | Ab dieser Zahl laufender Anfragen gilt der Knoten als voll und neue Arbeit geht bevorzugt woandershin. | `/healthz → hosts[].spill_threshold` |
| **Thread-Slots** | Belegte Slots: Gespräche, die diesem Knoten zugeordnet sind und ihren Prefix-Cache dort halten. | `/healthz → hosts[].thread_slots` |
| **max. Threads** | Wie viele Thread-Slots der Knoten führen darf. | `/healthz → hosts[].max_threads` |
| **Zielanteil** | Anteil der Thread-Slots, den dieser Knoten nach seiner Gewichtung bekommen soll (M5 3 : M3 2). | `/healthz → hosts[].target_share` |
| **relative Last** | Anteil der Einzelstrom-Rate, der gerade verloren geht: `1 − aktuelle Rate / Einzelstrom-Rate`. 0 % heißt, die Maschine liefert noch ihre Einzelstrom-Geschwindigkeit; `—` heißt noch nicht gemessen. | `/healthz → hosts[].relative_load` |
| **RTT** | Gemessene Antwortzeit eines Probe-Requests zu diesem Knoten; ein WAN-Knoten wäre hier sofort sichtbar. | `/healthz → hosts[].rtt_ms` |
| **tok/s** | Live: die Dekodierrate je Anfrage für das Modell mit den meisten Messproben auf diesem Knoten, vom Dienst geglättet — sie fällt mit der Parallelität. Die kalibrierte Kapazität steht daneben in der Modelltabelle (§5). | `/healthz → hosts[].metrics[<modell>].tg_tps`, `/config → hosts[].tg_tps` |
| **Messproben** | Wie viele abgeschlossene Anfragen in die Rate eingehen; unter zehn ist die Zahl eine Momentaufnahme. | `/healthz → hosts[].metrics[<modell>].tg_samples` |
| **Messalter** | Wie lange die letzte Messung zurückliegt. Eine Rate von vor einer Stunde ist keine Aussage über jetzt. | `/healthz → hosts[].metrics[<modell>].measured_at` |
| **Anfragen** | Anfragen, die RIPPLE seit dem Start an diesen Knoten geschickt hat. | `/healthz → hosts[].requests_total` |
| **Fehler** | Fehlgeschlagene Anfragen an diesen Knoten seit dem Start. | `/healthz → hosts[].errors_total` |
| **Modelle verfügbar** | Modelle, die der Knoten anbietet — nicht: gerade geladen. | `/healthz → hosts[].models_available` |

**Relative Last neben dem Gewicht (issue #80).** Das Gewicht ist die **Absicht**:
`target_share` sagt, welchen Anteil der Thread-Slots der Optimierer diesem Knoten
zudenkt. Die relative Last ist die **Wirklichkeit**: `1 − aktuelle Rate /
Einzelstrom-Rate`, also der Anteil der Einzelstrom-Rate, der gerade verloren
geht, weil sich mehrere Ströme eine Maschine teilen. Beide stehen in derselben
Unterzeile der Slots-Zelle (`target 60 % · 16 %`) — nebeneinander, weil erst der
Vergleich die Frage beantwortet, ob ein Knoten bekommt, was ihm zugedacht ist;
untereinander wären es zwei Aussagen ohne Bezug, und eine zweite Zeile hätte die
Karte wachsen lassen, deren Platz der Fahrtenschreiber schon verbraucht hat.

Die Last steht dort **ohne Wort**, nur als Zahl: in einer Viertel-Karte (150 px
bei 1440) sind zwei Wörter nicht in jeder der 16 Sprachen unterzubringen —
griechisch „στόχος 100 % · φόρτος 100 %" misst 171 px, russisch und polnisch
fehlten 19/17 px. Das Wort steht im Tooltip der Zelle, den beide Zahlen teilen
(`slots`, `max_threads`, `target_share`, `relative_load`), die Erklärung in
`tip.relative_load`. Der Preis ist benannt: zwei Prozentzahlen nebeneinander sind
nicht selbsterklärend, und der Tooltip ist der Ort, an dem das Wort steht. Die
Alternative — ungleiche Spalten, damit beide Wörter passen — wurde verworfen,
weil die zwei Host-Karten dann nicht mehr pixelgleich sind; die Karten sind das
Vergleichsbild.

Ein `—` heißt **noch nicht gemessen**, nicht „null Last": ohne Messung ist über
die Maschine nichts gesagt, und eine 0 wäre genau diese Aussage. Die Zahl trägt
tabellarische Ziffern, damit sie beim 2-s-Patch nicht springt.

**Alter der Messung (issue #80).** Die Unterzeile der tok/s-Zelle trug das Alter
in **rohen Sekunden** — `32018569 s ago` — und daneben die Probenzahl. Beides
zusammen war in sieben der 16 Sprachen breiter als die 149,6-px-Zelle (polnisch
+78 px, französisch +62 px, englisch +39 px), und weil die Zeile mit Ellipse
abschneidet, las der Operator „86,400 s ago" statt „1 d ago". Das Alter wird
deshalb in der Einheit gelesen, in der man es liest: `fleet.ago_s` unter einer
Minute, `fleet.ago_min` unter einer Stunde, `fleet.ago_h` unter einem Tag,
`fleet.ago_d` darüber. Bis 5 s bleibt es `fleet.now` („just now"), weil „0 s ago"
keine Messung ist, sondern ein Moment. Die Grenzen sind die des Kalenders
(60/60/24), und `Math.floor` schneidet ab, statt „1,5 d" zu behaupten — die
Messung hat keine halben Tage. Der Sentinel 32 018 569 s („nie gemessen, aber die
Zeile existiert") liest sich als `370 d`.

Die **Probenzahl** steht seitdem im Tooltip der Zelle, nicht mehr in der Zeile:
zwei Angaben passen in einer Viertel-Karte in keiner Sprache, eine schon —
dieselbe Regel wie bei der relativen Last darüber. Der Tip ist derselbe Layer wie
jeder andere (`data-tip`), und er sagt weiterhin, wie viele abgeschlossene
Anfragen in die Rate eingegangen sind (`fleet.samples`). Alle 16 Kataloge tragen
die vier Einheiten-Ids; `fleet.ago` gibt es nicht mehr, damit die Sekunden nicht
zurückkommen. Das Grid bleibt unverändert `repeat(4,minmax(0,1fr))` — der Text
war die Ursache, nicht die Spalte.

### Der Fahrtenschreiber je Knoten (issue #83)

Die Karte sagte bisher nur den Moment: 18,3 tok/s, 5 in flight. Ob das gut ist,
besser wird oder das Ende eines Einbruchs, stand nirgends. Zwei Linien über das
Fenster beantworten es — eine Aufzeichnung, kein Momentbild.

**Zwei Linien, zwei Aussagen**

| Linie | Strich | Bedeutung | Rechnung |
|---|---|---|---|
| **total** | durchgezogen | tok/s über alle Ströme des Knotens | `n · rate(n)` |
| **per thread** | gestrichelt | tok/s je Strom | `rate(n)` |

Beide kommen fertig aus `/healthz` (`hosts[].series`); `total = n · rate` wird in
`ripple/stats.py` **einmal** gerechnet und auf der Seite nicht nachgeholt. `n`
selbst bekommt keine Linie: es ist der Quotient der beiden, und der steht in der
Karte daneben (`inflight` / `thread slots`).

**Was die Form heißt**

| Bild | Lesart |
|---|---|
| die Linien spreizen sich | Nebenläufigkeit ist gestiegen: die Summe wächst, die Rate je Strom fällt — Batching, kein Fehler |
| beide springen zusammen | Last ist angekommen |
| beide springen, `n` unverändert | ein Rebalance hat stattgefunden — das ist der Fahrtenschreiber |
| beide fallen, `n` unverändert | der Knoten ist langsamer geworden aus einem Grund außerhalb von RIPPLE: Speicherdruck, Wärme, ein fremder Prozess |
| Summe flach, Rate je Strom fallend | Ströme stapeln sich ohne Gewinn — genau der Fall, für den #80 (der Optimierer) existiert |

**Die Lücke bleibt eine Lücke.** Ein Bucket ohne Probe ist im Vertrag `null`, und
`null` setzt beim Zeichnen den Stift ab: der nächste echte Punkt beginnt einen
neuen Teilpfad (`M`), es gibt keine gerade Verbindung über die Lücke und keine
Interpolation. Ein Neustart leert den Ringpuffer; die Kurve beginnt dann neu,
statt einen Absturz zu behaupten, den es nicht gab. Dieselbe Regel gilt für eine
fehlende oder leere `series`: die Karte zeichnet **nichts** — kein leerer Rahmen,
keine Nulllinie.

**Achse, Maßstab, Höhe.** Beide Linien teilen eine lineare Skala von 0 bis zum
höchsten Wert des Fensters; es gibt kein Gitternetz und keine Skalenstriche.
Beschriftet ist nur, was eine Zahl trägt: die Einheit `tok/s` links oben und das
Fenster rechts (`15 min`, bei krummen Fenstern `90 s`). Der Spitzenwert wird
nicht gedruckt — die Zahl für „jetzt“ steht in der tok/s-Zelle der Karte direkt
darüber, und die Grafik ist die Form, nicht die Tabelle. Der Block ist **48 px**
hoch (14 px Kopfzeile mit Einheit, Legende und Fenster + 34 px Fläche); mit 2 px
Abstand darüber und 6 px darunter belegt er 56 px, damit der Flotten-Tab bei
1440 px nicht wächst — das ist der Platz, den #80 beim Deckel auf Block 2
freigelassen hat.

**Graustufen.** Die Strichart trägt die Aussage, nicht die Farbe: `total` ist
durchgezogen, `per_thread` gestrichelt (`stroke-dasharray: 4 3`), und die Marke
in der Legende hat dasselbe Muster wie die Linie, die sie nennt. In Graustufen
und für Rot-Grün-Schwäche bleiben die beiden unterscheidbar; die Farbe
(`--load` gegen `--fg-3`) ist die zweite, leisere Auskunft.

**Gezeichnet wird das Modell der Karte.** `hosts[].series` ist je Modell ein
Eintrag, weil sich `rate(n)` je Modell unterscheidet. Die Karte zeichnet den
Eintrag des Modells, dessen Rate in ihrer tok/s-Zelle steht; hat sie keine
Messung, den ersten Eintrag — dann ist die Kurve die einzige Auskunft über den
Knoten.

**Warum `<svg>` und nicht `<canvas>`.** Die Karte ist eine Zeichenkette, und ein
SVG ist Teil dieser Zeichenkette: kein zweiter Zeichenschritt nach dem Patch,
keine Breitenmessung, kein `devicePixelRatio`, kein `ResizeObserver`.
`width:100%` und `preserveAspectRatio="none"` folgen der Karte von selbst, und
`vector-effect="non-scaling-stroke"` hält Strichbreite (1,5 px) und Strichmuster
(4 px an, 3 px aus) in Pixeln — die gestrichelte Linie bleibt auch in einer
breiten Karte gestrichelt statt gestreckt.

**Die Kataloge.** Zwei ids, in allen 16 Katalogen: `fleet.series_total` und
`fleet.series_per_thread`. Die Einheit `tok/s` und das Fenster (`min`, `s`) sind
Einheiten der Achse und bleiben unübersetzt.

**Der Zeiger auf der Fläche (issue #102).** Die Fläche ist 34 px hoch; eine
y-Achse wäre dort kleiner als ihre Beschriftung. Der Zeiger ersetzt sie: er
nennt **beide** Zahlen des Buckets, auf den er zeigt — `total` und
`per_thread` —, und zwar in der Kopfzeile, in den zwei Legendenmarken, die
ohnehin dort stehen. Kein Kasten über der Fläche (er verdeckte genau die Form,
nach der man fragt), kein neues Wort: die zwei ids der Legende sind die zwei ids
des Readouts, und beim Verlassen steht die Legende wieder da. Die Marke behält
ihr Muster (durchgezogen/gestrichelt), die Zahl steht daneben in tabellarischen
Ziffern; der Kopf ist 14 px hoch und bleibt es, also springt nichts.

Ein 1-px-Fadenkreuz im gedämpften Vordergrundton (`--fg-3`, `non-scaling-stroke`)
zeigt die x-Position, nicht den Wert. Die Rechnung ist eine Abbildung, keine
Schätzung: der viewBox (1000 Einheiten) ist auf die Karte gestreckt, also kommen
die Pixel aus `getBoundingClientRect()` des `<svg>`, der Bucket aus dem Bruchteil
und `span` (Anzahl Buckets − 1, dieselbe Zahl, mit der der Pfad gezeichnet wird),
gerundet und auf 0…`span` geklemmt. Am Rand heißt das: der erste Pixel ist
Bucket 0, der letzte ist der letzte Bucket.

**Ein leerer Bucket ist keine Null.** Die Sondierung läuft alle 10 s in 5-s-Eimer
(#96), also ist jeder zweite Bucket planmäßig leer. Der Readout schreibt dort
`—` und nie `0` — eine 0 würde den Abtakt als Messung ausgeben. Dieselbe Regel
wie beim Pfad: `null` ist „keine Probe", nicht „null tok/s".

**Der Zustand überlebt den Patch.** Er liegt in einer Modulvariable und wird von
`sparkline()` gelesen, nicht nachträglich ins DOM gemalt: der 2-s-Patch baut die
Zeichenkette neu, und die Zeichenkette ist die Funktion dieses Zustands (dieselbe
Regel wie `showDim` und `loadHideIdle`). Ein Fadenkreuz, das nur einmal ins SVG
geschrieben wird, wäre zweimal pro Sekunde weg. Der Zeiger wird auf
Dokument-Ebene gelesen, weil ein Listener am `<svg>` nach dem ersten Patch nicht
mehr existiert; `pointerType === 'touch'` bleibt draußen — auf einem Finger gibt
es kein Hovern, und das `aria-label` nennt unverändert die letzten Werte beider
Reihen.

### Modelltabelle

| Kennzahl | Definition | Quelle |
|---|---|---|
| **geladen** | Ob die Gewichte dieses Modells auf diesem Knoten resident sind. „verfügbar“ kostet nur Plattenplatz, „geladen“ Speicher. | `/healthz → models[].hosts[].loaded (oMLX /v1/models/status)` |
| **Größe geschätzt** | Erwartete Größe der Gewichte aus der Modellkonfiguration, bevor der Knoten sie geladen hat. | `/healthz → models[].hosts[].estimated_size (oMLX /v1/models/status)` |
| **Größe gemessen** | Tatsächlich belegter Speicher nach dem Laden; die Abweichung zur Schätzung ist der Kalibrierfehler. | `/healthz → models[].hosts[].actual_size (oMLX /v1/models/status)` |
| **Kontext** | Maximale Kontextlänge des Modells in Token. | `/healthz → models[].hosts[].context_len (oMLX /v1/models)` |
| **laufende Anfragen** | Anfragen für dieses Modell auf diesem Knoten, gerade jetzt. | `/healthz → models[].hosts[].inflight` |
| **Thread-Slots** | Threads dieses Modells, die auf diesem Knoten liegen. | `/healthz → models[].hosts[].threads` |
| **tok/s** | Live / Kapazität: links die Rate je Anfrage, die oMLX für dieses Modell auf diesem Knoten meldet (geglättet, lastabhängig), rechts die kalibrierte Kapazität aus `config.yaml`; fehlt die Messung, steht `— / 43,5`. | `/healthz → models[].hosts[].tg_tps`, `/config → hosts[].tg_tps` |

### Verteilung

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Zielanteil** | Soll-Anteil an den Thread-Slots nach Gewichtung (3 : 2). | `/healthz → threads.target_share` |
| **Ist-Anteil** | Tatsächlicher Anteil an den belegten Thread-Slots, gerade jetzt. Die Spaltenüberschrift heißt `Thread-Anteil` (#91), weil der Wert Threads zählt und nicht Requests. | `/healthz → threads.actual_share` |
| **Abweichung** | Ist minus Ziel in Prozentpunkten. | `berechnet aus threads.actual_share − target_share` |
| **Requests im Fenster** | Anfragen, die RIPPLE in diesem Fenster für dieses Modell an diesen Knoten geschickt hat — die zweite, ehrliche Zahl neben dem Slot-Anteil. Sie bleibt sichtbar, wenn kein Thread gebunden ist. | `/healthz → hosts[].requests_by_model`, `requests_window_seconds` |
| **kein warmer Thread** | Steht in der Balkenspalte, solange das Modell keinen einzigen Thread hält: der Balken misst den Anteil an den belegten Slots, und ohne einen gibt es nichts zu füllen. Die Zeile bleibt trotzdem, mit ihrer Request-Zahl (#91). | `/healthz → models[].hosts[].threads` |
| **Slots belegt** | Thread-Slots, die dieses Modell gerade belegt, gegen die Slots, die seine Knoten zusammen führen dürfen (max_threads je Knoten). | `/healthz → threads.registry, hosts[].max_threads` |
### Kennzahlenleiste

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Anfragen gesamt** | Alle Anfragen seit dem Start des Dienstes. | `/stats → requests_total` |
| **erfolgreich** | Anfragen, die mit 2xx beantwortet wurden. | `/stats → requests_ok` |
| **Fehler** | Anfragen, die mit einem Fehler endeten. | `/stats → errors_total` |
| **503 / 507** | 503 = kein Knoten konnte die Anfrage nehmen; 507 = ein Knoten verweigerte das Laden aus Speichergründen. | `/stats → http_503_total, http_507_total` |
| **Thread-Treffer** | Anfragen, die auf einen bestehenden Thread trafen und dessen Prefix-Cache weiterbenutzten. | `/stats → thread_hits` |
| **Threads neu / verdrängt** | Neu angelegte Threads und Threads, die für einen neuen Platz machen mussten. | `/stats → threads_new, threads_evicted` |
| **Rebind** | Threads, die auf den anderen Knoten umgehängt wurden, um die Verteilung zu glätten. | `/stats → threads_rebound` |
| **Uptime** | Laufzeit des RIPPLE-Prozesses seit dem letzten Start. | `/healthz → uptime_seconds` |
### Entscheidungsstrom

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Momentaufnahme** | Zeitpunkt, zu dem dieser Abzug von /healthz und /stats genommen wurde. Die Seite selbst verbindet sich nicht. | `Abzug von /healthz + /stats, 15.11.2026` |
| **Zeit** | Zeitpunkt der Entscheidung, Ortszeit des Rechners, auf dem RIPPLE läuft. | `/stats → decisions[].time` |
| **Modell** | Angefragtes Modell. | `/stats → decisions[].model` |
| **Knoten** | Knoten, an den die Anfrage ging. | `/stats → decisions[].host` |
| **Grund** | Warum dieser Knoten — die Erklärung steht am Chip. | `/stats → decisions[].reason` |
| **Prompt-Größe** | Zeichen im Prompt der Anfrage; bestimmt den Prefill-Aufwand. | `/stats → decisions[].prompt_chars` |
| **Dauer** | Zeit von der Annahme bis zur letzten Antwortzeile, in Millisekunden. | `/stats → decisions[].duration_ms` |
| **Status** | HTTP-Status, den der Client bekommen hat. | `/stats → decisions[].status` |
| **Dauerbalken** | Länge relativ zur längsten Entscheidung im Puffer — zeigt Ausreißer, nicht Absolutwerte. | `berechnet aus decisions[].duration_ms` |
| **Einträge** | Wie viele Entscheidungen der Ringpuffer gerade hält (höchstens 50). | `/stats → decisions[]` |
| **Median / p90** | Median und 90-Prozent-Wert der Dauer über alle Einträge im Puffer. | `berechnet aus /stats → decisions[].duration_ms` |
### Thread-Registry

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Schlüssel** | Der Client-Schlüssel, der diesen Thread geöffnet hat; `ohne Schlüssel` heißt, die Anfrage kam ohne Schlüssel an (Loopback). Der Name bleibt beim Öffner, auch wenn ein anderer Schlüssel den Thread per Prefix-Match übernimmt. | `/healthz → threads.registry[].key_name` |
| **Züge** | Anfragen, die dieser Thread seit seinem Anlegen geführt hat. | `/healthz → threads.registry[].turns` |
| **idle** | Sekunden seit der letzten Anfrage dieses Threads. | `/healthz → threads.registry[].idle_seconds` |
| **TTL** | Nach dieser Zeit ohne Anfrage wird der Thread verworfen und gibt seinen Slot frei. Die Zeile zeigt seit #34 nicht mehr die feste Zahl, sondern die Restzeit („27 min"), bei null „frei"; die eingestellte TTL steht im Tooltip. | `/healthz → threads.registry[].ttl_seconds − idle_seconds (server_time)` |
| **Anfrage läuft** | Anfragen, die gerade auf diesem Thread laufen. Solange eine läuft, hält der Thread seinen Slot, und der Löschknopf ist aus — der Endpunkt antwortet dann 409. | `/healthz → threads.registry[].active` |

**Der Löschknopf (#35).** Ein Thread hält den Slot seines Knotens bis zum TTL
(1800 s) nach seiner letzten Anfrage; die Seite konnte das bisher nur abwarten.
Jeder Eintrag trägt deshalb einen Knopf `löschen`, der den Thread verwirft und
den Slot damit sofort freigibt — `DELETE /admin/threads/{key}`, hinter
demselben Admin-Schlüssel wie jeder andere Schreibgriff der Seite. Der Schlüssel
im Pfad ist das `label`, das die Seite zeigt (die ersten 24 Zeichen, bei
Sitzungs-Ids `sid:…`); RIPPLE löst ihn **gegen die Registry** auf, statt ihn neu
abzuleiten — ein langer `sid:`-Schlüssel kommt hier abgeschnitten an, und nur die
Registry kennt den ganzen. Eine Rückfrage vor dem Aufruf; ein Abbruch schickt
nichts, und nach dem Erfolg läuft derselbe Tick wie nach jeder anderen
Admin-Aktion, ohne Neuladen der Seite. Läuft noch eine Anfrage auf dem Thread,
ist der Knopf aus und sagt warum (`Anfrage läuft`): den Slot unter einer
laufenden Anfrage wegzuziehen ist genau das, was die Reservierung verhindert.
Der Preis steht im Tooltip — der Prefix-Cache ist nicht wiederherstellbar, die
nächste Anfrage zahlt den Prefill.

### Auslastung je Host (2.1, issue #33)

Ein Balken je Knoten (die fünf Zustände als Segmente), darunter die Legende,
dann die Arbeitstabelle, darunter die Gegenprobe. Die Zeit ist **exklusiv**:
jeder Probe-Takt (10 s) wird genau einem der fünf Zustände zugeordnet, also
addieren sie sich auf das abgetastete Fenster — die Segmentbreite ist der Anteil
daran. `Lädt` ist ein eigener Zustand, weil ein 453-GB-Kaltstart rund zwei
Minuten dauert und weder Leerlauf noch Decode ist.

Farben: Leerlauf neutralgrau, **Lädt blau**, **Prefill orange**, Decode grün,
Gemischt violett. Laden und Prefill sind beide „der Knoten arbeitet, liefert
aber noch nichts" — zwei Blautöne nebeneinander waren im Balken nicht zu
unterscheiden, deshalb trägt Prefill den Warn-Ton (issue #39).

**Leerlauf ausblenden** (issue #39): Ein Umschalter über den Balken nimmt den
Leerlauf aus der Rechnung, die Prozente beziehen sich dann auf die Zeit unter
Last (Fenster minus Leerlauf) — man sieht die Aufteilung der *Arbeit* statt des
Anteils Arbeit am Tag. Reine Ansicht, kein Serverzustand; eine Zeile ohne Last
sagt das („keine Last") statt einen leeren Balken zu zeigen, der von fehlenden
Daten nicht zu unterscheiden wäre.

**Die Summenzeile ist die lauteste Zeile (issue #102).** Sie war es nicht: der
Name stand in `--fg-2`, der Balken trug `opacity:.7`. Die Summe war also leiser
gezeichnet als ihre Teile, und weil die Deckkraft am Container hing, wurde auch
die leere Spur mitgeblasst — die gepunktete Fläche, die „hier war nichts" sagt,
sah aus wie „hier war wenig". Beides ist ersetzt. Der Name liest in `--fg` wie
jeder andere Name; die fünf Segmentfarben bleiben unverändert.

**Summenzeilen werden durch eine Achse getrennt, nicht durch einen Rahmen
(issue #106).** #102 hatte die Summe mit einem 1-px-Ring in `--fg-2` (`outline`,
`outline-offset:1px`) um den Balken hervorgehoben. Der Ring war #a8b2c0
(Luminanz 0,440), der idle-Anteil des Balkens #7b8694 (0,234): 45
Luminanzstufen Unterschied, aber beide **unbunt** — und ein dünner grauer Strich
um eine Fläche, die zu 70–80 % aus demselben Grau besteht, trennt sich nicht,
das Auge gruppiert ihn mit der Fläche. Ein weiteres Neutral hätte dasselbe
Problem nur verschoben. Statt dessen eine **Achse**: die Summenzeile steht
8 px tiefer als ihre Teile, und über ihr läuft eine 1-px-Linie in `--line-3`
(#46505e) über die volle Zeilenbreite. Eine Linie über einer Summenzeile ist die
Buchhaltungskonvention, und sie kann mit keiner Segmentfarbe verwechselt werden:
ihre Luminanz (0,079) liegt unter der jedes Segments (das dunkelste, idle, ist
0,234 — die Linie ist dreimal dunkler), gegen die Segmente sind es 2,2–3,8 : 1
(idle 2,2, decode 2,7, gemischt 3,1, lädt 3,2, prefill 3,8), gegen das Panel
2,2 : 1. Sie ist damit sichtbar, aber leise — sie trennt, sie ruft nicht.

Die Linie ist ein eigenes Element (`#load .load-sum-rule`) im Markup der
Summenzeile, kein `border-top`: ein Border würde die Zeile um 1 px verschieben.
Sie steht absolut (`position:absolute; left:0; right:0`) gegen die Zeile und
sitzt damit **im** Abstand — ein `grid-column:1/-1`-Kind würde eine zweite
Rasterreihe samt `gap` aufmachen und die Zeile um 15 px wachsen lassen. Der
Abstand (8 px `margin-top`) wird aus dem unteren Padding des Blocks bezahlt
(12 px − 8 px), damit die Höhe von `#load` gleich bleibt — bei 1440 px
(287 px) und bei 741 px (422,56 px) vor und nach der Änderung gemessen, nicht
geschätzt.

| Kennzahl | Definition | Quelle |
|---|---|---|
| **Leerlauf** | Exklusive Zeit ohne jede Anfrage auf diesem Knoten. | `/healthz → load.hosts[].idle` (oMLX `/admin/api/activity`, ein Takt je 10 s) |
| **Lädt** | Exklusive Zeit, in der ein Modell geladen wurde (`is_loading`). | `/healthz → load.hosts[].loading` |
| **Prefill** | Exklusive Zeit mit einer Anfrage zwischen Auslieferung und erstem Token. | `/healthz → load.hosts[].prefill` (`prefilling`) |
| **Decode** | Exklusive Zeit, in der Token erzeugt wurden. | `/healthz → load.hosts[].decode` (`generating`) |
| **Gemischt** | Takte, in denen der Knoten gleichzeitig prefilled und generiert hat. | `/healthz → load.hosts[].mixed` |
| **Anfragen** | Vom Knoten beantwortete Anfragen seit Beginn des Zählers; ein gescheiterter Verbindungsversuch ist ein Fehler und keine Anfrage. | `/stats → host_stats.hosts[].requests` |
| **Prefill s / Decode s** | Summe von `usage.prompt_eval_duration` / `usage.generation_duration` — von oMLX selbst gemessen, hier nur addiert. Keine eigene Spalte mehr: der Balken zeigt sie als Anteil, der Tooltip als Dauer. | `/healthz → load.hosts[].prefill_seconds, generation_seconds` |
| **Client s** | RIPPLEs eigene Uhr von der Auslieferung bis zum letzten Byte: was der Client gewartet hat. Steht **neben** den Knotenzeiten, nicht an ihrer Stelle — Warteschlange und Netz sind darin. Nicht in der Tabelle: es ist eine Diagnose, kein Zustand. | `/healthz → load.hosts[].client_seconds` |
| **Prompt-/Completion-Tokens** | Summe aus dem `usage` der Antworten, angezeigt in **Mtok** (zwei Stellen, `<0,01` unterhalb der Anzeigegrenze). | `/healthz → load.hosts[].prompt_tokens, completion_tokens` |
| **ohne Aufteilung** | Anfragen ohne `usage`-Objekt (Client-Abbruch, Fehler, ein Backend, das nicht misst). Tokens sind vielleicht gezählt, die Aufteilung ist unbekannt und wird **nicht** geschätzt. | `/healthz → load.total.requests_without_usage` |
| **seit** | Beginn des Fensters (letzter Reset oder Prozessstart) und seine Länge. | `/healthz → load.since, load.window_seconds` |
| **Gegenprobe** | Die eigene Zählung der Knoten aus ihrer SQLite (`range=today`), neben RIPPLEs Buch und nie darin. Die Lücke zwischen beiden ist der Verkehr, der an RIPPLE vorbei ging. | `GET /admin/api/usage?range=today` je Knoten → `totals` |
| **Zurücksetzen** | Setzt die Zählung auf null und zählt ab jetzt neu; die Datei auf der Platte hält die Nullen, bevor die Antwort kommt. Braucht den Admin-Schlüssel. | `POST /admin/stats/reset` |

### Zwei Fristen, die herunterzählen (issue #34)

Zwei Zahlen auf der Seite sind Fristen und standen bis dahin als feste Werte da:
die Restzeit eines Ladevorgangs (`142 s`) und die eines Threads (`1800 s`). Beide
zählen jetzt lokal herunter — ohne neuen Abruf, ohne Neuzeichnen des Blocks.

- **Die Uhr ist die des Servers.** `GET /healthz` trägt `server_time`
  (Epoche-Sekunden). Die Seite rechnet bei *jedem* Abruf
  `offset = server_time − browser_now` und benutzt `browser_now + offset` als
  ihre Uhr. Ein Browser auf einem anderen Rechner mit schiefer Uhr zählt damit
  trotzdem richtig; der Offset wird bei jedem Abruf neu gesetzt, damit eine
  driftende Uhr nicht wegläuft.
- **Ein Element, ein Attribut.** Jedes Element, das eine Frist zeigt, trägt
  `data-deadline` (absolutes Ende in Epoche-Sekunden) und
  `data-deadline-kind` (`load` oder `thread`). Der 1-s-Takt schreibt
  ausschließlich `textContent` dieses Elements. Kein Block wird neu gerendert,
  also kein Flackern, kein Scroll-Sprung, kein verlorener Tooltip und kein
  neuer Knoten; nach dem 2-s-Patch wird die Liste der Elemente neu abgefragt,
  damit der Takt die neuen Knoten sieht.
- **Laden.** Ende = `started_at + eta_seconds`. Über einer Minute `m:ss`
  (`2:22`), darunter `n s`. Bei null steht „überzogen" (`load.overdue`) mit der
  Zeit *über* der Schätzung — nie eine negative Zahl und nie „fertig": ein
  Ladevorgang endet durch das Signal von oMLX, nicht durch unsere Arithmetik
  (`docs/loading-state.md`).
- **Thread.** Ende = `fetch_time + ttl_seconds − idle_seconds`, wobei
  `fetch_time` die `server_time` des Abrufs ist, zu dem `idle_seconds` gemessen
  wurde. Über einer Minute Minuten (`27 min`), darunter Sekunden. Bei null steht
  „frei" (`threads.free`): die TTL räumt den Thread binnen 30 s ab, null heißt
  „gleich weg" und nicht „weg". Die feste TTL selbst steht im Tooltip, nicht
  mehr in der Zeile.

### Der Parameterblock (6.5, issue #78)

`6.5 Parameter` war eine Liste dessen, was gilt, und sagte es selbst:
„Änderungen bedeuten: Konfigurationsdatei bearbeiten und neu starten". Das
stimmt für Identität und Transport und ist für Routing-Policy falsch — wer einen
Knoten eine Stufe zurücknehmen oder einen Rebind-Sturm beenden will, hätte dafür
jede laufende Anfrage verloren.

Der Block hat seit #78 **drei Spalten hinter den zwei Bezeichnern**: den **Wert
in Kraft**, seine **Herkunft** (`config.yaml` oder `am Feld gesetzt`) und ein
**Zurücksetzen je Zeile**.

- **Die Herkunft wird abgeleitet, nie fest verdrahtet.** Der Server schickt mit
  `/config` zwei zusätzliche Schlüssel: `editable` (welche Pfade sich setzen
  lassen, mit `*` für jede Zeile einer Familie — `hosts.*.weight`) und
  `overrides` (welche tatsächlich gesetzt sind). Steht der Pfad einer Zeile in
  `overrides`, ist sie „am Feld gesetzt" und trägt den Chip in der Farbe der
  Betreiberaktionen (`--maint`); sonst steht dort `config.yaml`. Ein älterer
  Server, der beide Schlüssel nicht kennt, bekommt die Tabelle von vorher: drei
  Spalten, kein Feld, kein leerer Platz, kein Fehler.
- **Ein Feld, kein Dialog.** Eine Zeile, deren Pfad in `editable` steht, trägt
  neben dem formatierten Wert ein Feld in der Schreibweise der Sprache (`0,9`
  auf Deutsch, `0.9` auf Englisch). Enter speichert, Escape stellt den Wert
  zurück, ein Wechsel des Fokus speichert ebenfalls. Ohne Admin-Schlüssel sind
  die Felder aus und der Block sagt warum — derselbe Satz wie in 6.1–6.4.
- **Die Meldung des Laders, wörtlich.** Ein `400` wird nicht übersetzt und nicht
  zusammengefasst: der Satz, den der Lader geschrieben hat, steht neben dem
  Feld, in der Zeile, zu der er gehört.
- **Zurücksetzen** ist ein `DELETE` auf denselben Pfad. Es entfernt den Eintrag
  aus dem Overlay (`~/.ripple/fleet.yaml`); `config.yaml` bleibt unverändert die
  Basis. Danach liest der bestehende 2-s-Takt `/config` neu — kein zweiter
  Auffrischweg, und keine zwei Sekunden mit einer Zeile, die es nicht mehr gibt.
- **Identität und Transport haben kein Feld, sondern einen Grund.** `listen`,
  `log_file`, `log_level`, `affinity.header` und die Identität eines Knotens
  (Adressen, `node_id`, `api_key`, `backend`, Grenzen) stehen mit „Neustart
  nötig: config.yaml ändern und neu starten" da. Ein Feld, das ohnehin einen
  Neustart verlangt, wäre eine Lüge.

**Das Gewicht ist die Ausnahme, die Sorgfalt braucht.** Ein Gewicht, das sich
unter einem laufenden Thread verschiebt, zieht ein Gespräch von seinem
Prefix-Cache weg. Deshalb gilt eine Gewichtsänderung **nur für neue Threads**,
und die Rückfrage nennt vorher die Zahl: „Gewicht 2,5 nur für neue Threads
setzen? 3 laufende Threads behalten ihren Knoten bis zum Ende." Erst das
bestätigte `PATCH` (mit `confirm: true`) schreibt; laufende Threads behalten
ihren Knoten, bis sie enden — keine stille Umverteilung.

Die Spaltenbreiten stehen im `colgroup` (96 · 280 · Rest · 150 · 132 px bei
1440), und `overflow-wrap:break-word` ist die Zusage, dass kein Wort breiter ist
als seine Spalte. Nachgemessen wird das in `tests/test_dashboard.py` an den fünf
Sprachen mit den längsten Wörtern (en, de, pl, cs, el): das breiteste Wort je
Zelle plus Feld plus Polsterung muss in die Spalte passen. Der Block ist früher
in acht von sechzehn Sprachen übergelaufen, solange er rohe Sekunden druckte.

### Gründe im Entscheidungsstrom

| Grund | Bedeutung |
|---|---|
| `thread` | Dieses Gespräch hat schon einen Thread auf diesem Knoten — der Prefix-Cache bleibt gültig, nichts wird neu geladen. |
| `new_thread` | Neuer Thread; der Knoten wurde nach aktueller Last und Residency gewählt. |
| `ephemeral` | Einmalige Anfrage ohne Thread: kein Slot, kein Cache, nur die Last entscheidet. |
| `one_shot` | Anfrage, die bewusst keinen Thread hält — Slots und Slot-Eviction gelten für sie nicht. |
| `rebind` | Der Thread wurde auf diesen Knoten umgehängt, um die Verteilung zu glätten. |
| `fallback` | Der erste Knoten scheiterte; die Anfrage wurde auf dem anderen wiederholt. |
| `slot_evict` | Alle Slots waren von idle Threads belegt — der älteste gab seinen Platz her. |
| `model_pinned` | Das Modell ist auf diesem Knoten gepinnt und darf nicht verdrängt werden. |
| `degraded_host` | Der bevorzugte Knoten war degraded; die Anfrage ging an den gesunden. |
| `conflict` | Kein Knoten konnte die Anfrage nehmen, ohne einen laufenden Thread zu verdrängen. |
| `no_healthy_host` | Kein gesunder Knoten bietet dieses Modell an. |
| `unknown_model` | Kein Knoten kennt dieses Modell. |
| `model_list_unavailable` | Die Modellliste eines Knotens war nicht lesbar — ob er das Modell hat, ist unbekannt; 503 statt 404, weil das eine Betreiberfrage ist (Admin-Schlüssel), keine Eigenschaft des Modells. |
| `no_candidate` | Kein weiterer Knoten für einen Retry übrig. |

## 9. Was über FA-22 hinausgeht

Drei Zutaten, die nicht in der Inhaltsliste stehen, aber die Fragen
beantworten, die die Liste aufwirft:

- **Kennzahlenleiste** (Anfragen, Fehler, 503/507, Thread-Treffer, neue
  Threads, Rebind, Uptime). Ohne sie muss man für „läuft es?“ zwei Karten
  addieren.
- **Thread-Registry.** Die Verteilung zeigt `100 % / 0 %`; ohne den einen
  offenen Thread daneben ist nicht zu sehen, warum. Der Eintrag nennt Schlüssel,
  Modell, Knoten, Züge, Idle-Zeit und TTL und trägt den Löschknopf (#35), der
  den Slot vor dem TTL freigibt.
- **Fußzeile des Protokolls** mit Median, p90 und Fehlerzahl.

Nicht enthalten: Kalibrierung und Parameterliste der heutigen Seite. Beides
gehört nicht zu FA-22 und macht den Entwurf nur schwerer zu beurteilen; #2
entscheidet, ob sie zurückkommen.

## 10. Sprache

Die Seite ist **zweisprachig ab Werk**: Deutsch ist die Vorgabe, Englisch ist die
Quelle. Bezeichner bleiben in **jeder** Sprache englisch (`inflight`, `tok/s`,
`thread`, `ephemeral`, `healthy`) — weil sie so im Code, in den Logs und in
`/stats` stehen. Ein übersetztes `inflight` wäre im Protokoll nicht
wiederzufinden. Dasselbe gilt für alles, was in `<code>` steht: Pfade, Flags,
HTTP-Methoden, JSON-Schlüssel.

Eine Sprache ist **eine Datei plus zwei Listen**, und alle drei müssen
zusammenpassen:

| Ort | was |
|---|---|
| `ripple/static/i18n/<code>.json` | flach `id → Text`, derselbe Schlüsselsatz wie `en.json` |
| `ripple/static/dashboard.js` | ein Eintrag in `LANGS` mit `code`, `label`, `flag`, `locale` (Intl-Kennung) |
| `ripple/dashboard.py` | ein `<option value="<code>" lang="<code>">` im Selektor |

`tests/test_i18n.py::test_every_catalog_on_disk_is_wired_complete_and_not_a_copy`
läuft über das Verzeichnis, nicht über eine Liste: eine Datei ohne Eintrag in
`LANGS` oder im Selektor fällt genauso durch wie ein Eintrag ohne Datei, ein
fehlender `{platzhalter}` und ein Katalog, der nur abgeschrieben ist.

**Zahlen und Zeiten kommen aus `Intl`**, nicht aus einer Tabelle: `nf()` formatiert
mit der `locale` der Sprache, `clock()` ebenso. Die einzige Ausnahme ist der
Tausendertrenner im Deutschen — CLDR gruppiert seit 2021 mit einem schmalen
Leerzeichen, die Betreiber-Dokumente schreiben `1.234,5`, deshalb ersetzt
`GROUP_DOT` nur im Deutschen das Trennzeichen. Jede weitere Sprache bekommt die
CLDR-Vorgabe; wer dort etwas anderes will, trägt es in `GROUP_DOT` ein und sagt
im Kommentar, warum.

### Prüfstand der Kataloge

Ein Katalog, der wie geprüft aussieht und es nicht ist, ist schlimmer als keiner.
Deshalb steht hier, wer welchen Katalog gelesen hat:

| Katalog | Stand |
|---|---|
| `en.json` | Quelle, vom Betreiber abgenommen |
| `de.json` | vom Betreiber abgenommen (er ist der Leser) |
| `tr.json` | **Entwurf**, maschinell erstellt, noch nicht von einem Muttersprachler gelesen (a colleague) |
| `fr.json` | **Entwurf**, maschinell erstellt, kein Prüfer benannt |
| `it.json` | **Entwurf**, maschinell erstellt, kein Prüfer benannt |
| `es.json` | **Entwurf**, maschinell erstellt, kein Prüfer benannt |
| `pt.json` | **Entwurf**, maschinell erstellt (pt-PT), kein Prüfer benannt |
| `ru.json` | **Entwurf**, maschinell erstellt, kein Prüfer benannt; Pluralformen bewusst umgangen, weil die Kataloge keinen Plural-Mechanismus haben |
| `zh.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt; Bezeichner wie `host`, `thread`, `slot`, `token` bleiben lateinisch |
| `pl.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt; Zählwerte als „liczba …: {n}", um die Pluralformen zu umgehen |
| `hu.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt; Ungarisch steht nach der Zahl im Singular, `count_one`/`count_many` sind deshalb gleich |
| `cs.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt |
| `sk.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt |
| `sl.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt |
| `hr.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt |
| `el.json` | **Entwurf** (2026-11-16), maschinell erstellt, kein Prüfer benannt |

Ein Entwurf darf ausgeliefert werden, solange er als solcher bekannt ist; er darf
nicht als „die türkische Version" beworben werden, bevor ein Muttersprachler
darüber war. Wenn ein Katalog zurückgezogen werden soll, ist es eine Zeile in
`LANGS` und eine im Selektor — die Datei kann bleiben.

### Glossar der zwölf Kernbegriffe

Damit die nächste Sprache nicht neu raten muss: die Wahl jedes Übersetzers für
die Begriffe, die auf fast jeder Seite stehen. Bezeichner (`host`, `thread`,
`slot`, `token`, `prefill`, `decode`, `inflight`) bleiben dort englisch, wo die
Spalte es sagt — das ist die Regel aus dem Abschnitt oben, keine Ausnahme.

| en | de | tr | fr | it | es | pt | ru |
|---|---|---|---|---|---|---|---|
| idle | Leerlauf | boşta | inactif | inattivo | inactivo | inativo | простой |
| loading | Lädt | yükleniyor | chargement | caricamento | cargando | a carregar | загрузка |
| mixed | Gemischt | karışık | mixte | misto | mixto | misto | смешанный |
| requests | Anfragen | istekler | requêtes | richieste | solicitudes | pedidos | запросы |
| share | Anteil | pay | part | quota | parte | parte | доля |
| maintenance | Wartung | bakım | maintenance | manutenzione | mantenimiento | manutenção | обслуживание |
| calibration | Kalibrierung | kalibrasyon | calibration | calibrazione | calibración | calibração | калибровка |
| replication | Replikation | replikasyon | réplication | replica | replicación | replicação | репликация |
| tokens | Tokens | tokenlar | tokens | token | tokens | tokens | токены |

| en | zh | pl | hu | cs | sk | sl | hr | el |
|---|---|---|---|---|---|---|---|---|
| idle | 空闲 | bezczynność | tétlen | nečinnost | nečinnosť | prosto | neaktivno | αδράνεια |
| loading | 加载中 | wczytywanie | betöltés | načítání | načítavanie | nalaganje | učitavanje | φόρτωση |
| mixed | 混合 | mieszany | vegyes | smíšené | zmiešané | mešano | miješano | μικτό |
| requests | 请求 | żądania | kérések | požadavky | požiadavky | zahteve | zahtjevi | αιτήματα |
| host | host | host | host | hostitel | host | host | host | host |
| thread | thread | wątek | thread | vlákno | thread | thread | thread | thread |
| slot | slot | slot | slot | slot | slot | slot | slot | slot |
| share | 份额 | udział | részesedés | podíl | podiel | delež | udio | μερίδιο |
| maintenance | 维护 | serwis | karbantartás | údržba | údržba | vzdrževanje | održavanje | συντήρηση |
| calibration | 校准 | kalibracja | kalibrálás | kalibrace | kalibrácia | kalibracija | kalibracija | βαθμονόμηση |
| replication | 复制 | replikacja | replikáció | replikace | replikácia | replikacija | replikacija | αναπαραγωγή |
| tokens | token | tokeny | token | tokeny | tokeny | tokeni | tokeni | tokens |

## 11. Bekannte Grenzen des Entwurfs

- **Tooltips sind CSS, nicht positioniert.** Im scrollenden Protokollfeld
  würden Erklärungen der untersten Zeilen am Rand abgeschnitten; deshalb hat
  das Feld 52 px Innenabstand nach unten. Eine echte Seite sollte den Tooltip
  per JavaScript positionieren.
- **Der Abzug ist ein Moment.** Die Seite verbindet sich nicht; sie zeigt
  `healthy` auf beiden Knoten. Die anderen vier Zustände sind nur in der
  Legende zu prüfen. Für den Rebuild heißt das: die Zustände einmal erzwungen
  ansehen, bevor die Farben abgenommen werden.
- **Bei 741 px wird der Modellname im Protokoll gekürzt** (vollständig im
  `title`). In der Modelltabelle bricht er um.
- **Kein Leerzustand gezeichnet.** Wie die Seite aussieht, wenn ein Knoten
  `down` ist und die Verteilung keine Threads hat, ist offen.

## 12. Woran der Rebuild (#2) gemessen wird

| Prüfung | Soll |
|---|---|
| Höhe bei 1440 px | unter 2 800 px |
| waagerechter Überlauf bei 741 px | keiner (`scrollWidth == clientWidth`) |
| Spaltenkopf-Schrift | ≥ 12 px |
| Zahlen ohne Tooltip | 0 |
| Tooltip ohne Quelle | 0 |
| Zustände in Graustufen | fünf unterscheidbare Formen |
| Speicherbalken | Decke als sichtbare Kante, harte Grenze markiert, Füllung gemessen, Schätzungsüberschuss schraffiert |
| Volle Reloads beim 2-s-Tick | 0 — `apply()` schreibt nur geänderte Knoten (`tests/dashboard_dom_stub.js` zählt die Patch-Aufrufe) |
| `/healthz → hosts[].used_bytes_measured` | je Knoten vorhanden, Summe der `actual_size`; `0` zählt, `null` wird übersprungen |
| FA-22-Punkte 1–4 | in unter zehn Sekunden auffindbar; die Abschnitte tragen die Nummern der Liste |

Die Vorlage erfüllt die ersten sechs Zeilen. Die beiden Zeilen davor prüft der
Testlauf (`tests/test_dashboard.py`, `tests/dashboard_dom_stub.js`); die letzte
ist die Frage, die der Betreiber beantwortet.
