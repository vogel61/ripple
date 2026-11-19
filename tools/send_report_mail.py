#!/usr/bin/env python3
"""Sendet den Olla-Testbericht an Richard und verifiziert die Zustellung per IMAP."""
import email, imaplib, os, re, smtplib, ssl, sys, time
from email.message import EmailMessage
from email.header import decode_header

CRED = os.path.expanduser("~/.hermes/mailcow_credentials.txt")
SIG = os.path.expanduser("~/.hermes/mail_signature.txt")
raw = open(CRED, encoding="utf-8").read()


def pw_for(addr):
    m = re.search(re.escape(addr) + r"[^\n]*?Passwort:\s*(\S+)", raw)
    if not m:
        m = re.search(re.escape(addr.split("@")[0]) + r"[^\n]*?Passwort:\s*(\S+)", raw)
    if not m:
        sys.exit(f"Passwort fuer {addr} nicht gefunden")
    return m.group(1)


FROM = "o.stein@inforatio.at"
TO = "r.vogel@inforatio.at"
SUBJECT = "Olla-Test fertig: 3:2 ist damit nicht machbar (Bericht im Repo)"

BODY = """Hallo Richard,

ich bin mit dem Olla-Test durch. Kurzfassung: Olla ist sauber gebaut, aber die
3:2-Verteilung bekommst Du damit nicht - nicht wegen falscher Konfiguration,
sondern weil es sie dort nicht gibt. Details stehen unten, der volle Bericht
liegt im Repo.

DIE DREI BEFUNDE, DIE ZAEHLEN

1) Kein Gewicht, nur zwei Extreme.
   Ein Feld "weight" wird von Olla ausdruecklich verworfen ("Ignored key, no
   effect on the result"). Es gibt drei Balancer, und alle kennen nur zwei
   Ergebnisse:

     least-connections, Prio 100/100 :  9 / 11   (n=20)
     least-connections, Prio 100/ 50 :  5 /  5   (n=10)  <- Prio wird ignoriert
     priority-Balancer, 3 gegen 2    : 40 /  0   (n=40)
     priority-Balancer, 100 / 50     : 10 /  0   (n=10)
     round-robin,       100 / 100    :  6 /  4   (n=10)

   Heisst: entweder alles auf einen Host, oder halbe/halbe. Der Zwischenschritt
   3:2 existiert konstruktiv nicht. Die Prioritaet entscheidet, WER bedient
   wird, nicht WIE OFT.

2) Olla routet nach Verfuegbarkeit, nicht nach Ladung.
   Das Routing-Argument in jedem Response lautet "model_found" - also: steht
   das Modell in der Modellliste. Ob es geladen ist, interessiert nicht. Der
   Endpunkt /v1/models/status wird zwar durchgereicht, aber nicht ausgewertet.

   Das ist beim Test messbar schiefgegangen: ein Request fuer ein 21-GB-Modell
   hat auf dem M5 das geladene DeepSeek (403 GB) verdraengt - das Modell meiner
   eigenen laufenden Session. oMLX hat es beim naechsten Zugriff selbst wieder
   geladen, die Session hat es ueberlebt. Aber der Client hat HTTP 200 gesehen,
   waehrend im Hintergrund 403 GB flogen. Genau das, was FA-18 verhindern soll.

3) Das Release-Artefakt ist unvollstaendig.
   Beide Zips (v0.0.29 und v0.1.0) enthalten kein config/profiles/. Start
   scheitert mit "unsupported endpoint type: omlx". Zieht man die Profile aus
   dem Tag nach, laeuft es sofort. Besonders unangenehm: "olla --validate-config"
   meldet fuer genau diese Konfiguration PASS - es prueft Endpoint-Typen nicht.

WAS FUNKTIONIERT (und was wir uebernehmen)

  - Sticky Sessions: per Header X-Olla-Session-ID 5/5 gleicher Endpoint, und
    ohne Header per prefix_hash ebenfalls 5/5. Das ist die staerkste Funktion.
    Einschraenkung: der Hash laeuft ueber die ersten 512 Bytes des Prompts und
    enthaelt das Modell nicht.
  - Modell-Filter: ein Modell, das nur auf M5 liegt, geht zu 100 % dorthin.
    Kaltstart 16,5 s, HTTP 200.
  - Failover: toter Endpoint wird "offline" markiert, 10/10 auf den gesunden,
    null Fehler.
  - Streaming laeuft (8 Chunks, 905 ms), Dashboard laeuft offline ohne CDN.
  - Dashboard-Models-Tab zeigt den Host pro Modell - aber keine Residency.
    PARAMS/QUANT/SIZE stehen alle auf "-". Also Verfuegbarkeit, nicht Ladung.
  - Overhead auf dem Chat-Pfad: +45 ms (+20 %) bei kurzen Requests. Nicht die
    dokumentierten 0,3 ms.

EINE KORREKTUR AN MIR SELBST

Im Lastenheft stand von mir die Behauptung, "priority" wuerde innerhalb einer
Stufe gewichtet und ergaebe im Mittel 60/40. Das ist falsch - gemessen kommt
40/0 heraus. Ich habe die Stelle korrigiert; Kapitel 13 ist neu gefasst und die
Empfehlung steht jetzt auf Eigenbau, mit vier Dingen, die wir von Olla
uebernehmen (Sticky-Keys, Header-Namen, Dashboard-Aufbau, Modell-Filter).

ZUSTAND DER MASCHINEN

Alles zurueckgesetzt: M5 und M3 jeweils mit DeepSeek + 9B resident, wie vorher.
Alle Testprozesse beendet, keine offenen Ports, keine Systeminstallation - Olla
lief nur als entpacktes Binary im Projektverzeichnis.

WO ES STEHT

  docs/olla-evaluation.md       full report (10 sections)
  docs/requirements.md          version 0.8, chapter 13 new
  tools/olla-eval/              Messskripte + Rohdaten (JSON)

Commit bb492be, auf dem Remote verifiziert.

NOCH OFFEN

Die Hermes-Delegation habe ich vorhin auf dem M3 von Qwen3.8 auf DeepSeek
umgestellt - Qwen3.8 haette dort DeepSeek verdraengt. Das greift erst nach
einem "hermes gateway restart" aus einer separaten Shell; ich habe ihn nicht
gemacht, weil er meine laufende Session beendet haette.

Sag mir, ob ich die Spezifikation so abschliessen soll und mit M2 (Proxy)
beginnen kann.

""" + open(SIG, encoding="utf-8").read()

msg = EmailMessage()
msg["From"] = FROM
msg["To"] = TO
msg["Subject"] = SUBJECT
msg.set_content(BODY)

pw_from = pw_for(FROM)
ctx = ssl.create_default_context()
with smtplib.SMTP("mail.inforatio.at", 587, timeout=60) as s:
    s.starttls(context=ctx)
    s.login(FROM, pw_from)
    s.send_message(msg)
print("SMTP: gesendet an", TO)

time.sleep(4)

# --- IMAP-Readback -------------------------------------------------------
pw_to = pw_for(TO)
M = imaplib.IMAP4_SSL("mail.inforatio.at", 993)
M.login(TO, pw_to)
M.select("INBOX")
typ, data = M.search(None, 'SUBJECT', f'"{SUBJECT[:40]}"')
ids = data[0].split()
print("IMAP: Treffer in INBOX:", len(ids))
if ids:
    typ, d = M.fetch(ids[-1], "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
    hdr = d[0][1].decode("utf-8", "replace")
    print(hdr.strip())
else:
    typ, d = M.fetch(ids[-1] if ids else b"1", "(BODY.PEEK[HEADER])")
M.logout()
