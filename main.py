import os
import re
import html
import json
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

FILE_ARCHIVIO = "terreni_visti.txt"
FILE_ULTIMO_INVIO = "ultimo_invio_terreni.txt"
FILE_STORICO_MD = "storico_aste_proposte.md"

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

COMUNI_TARGET = [
    "firenze", "scandicci", "lastra a signa", "signa", "campi bisenzio", 
    "sesto fiorentino", "fiesole", "impruneta", "bagno a ripoli", 
    "san casciano in val di pesa", "montespertoli", "barberino tavarnelle", 
    "borgo san lorenzo", "calenzano", "capraia e limite", "castelfiorentino", 
    "cerreto guidi", "certaldo", "dicomano", "empoli", "figline e incisa valdarno", 
    "firenzuola", "fucecchio", "gambassi terme", "greve in chianti", "londa", 
    "marradi", "montaione", "montelupo fiorentino", "palazzuolo sul senio", 
    "pelago", "pontassieve", "reggello", "rignano sull'arno", "rufina", 
    "san godenzo", "scarperia e san piero", "vaglia", "vicchio",
    "san gimignano", "poggibonsi", "castellina in chianti", "radda in chianti",
    "prato", "carmignano", "poggio a caiano", "vaiano", "cantagallo",
    "san miniato", "castelfranco di sotto", "santa maria a monte", "volterra", "palaia"
]

BLACKLIST_CATEGORICA = [
    "appartamento", "villa", "villino", "negozio", "ufficio", "capannone", 
    "fabbricato industriale", "magazzino", "laboratorio", "albergo", "hotel", 
    "quota indivisa", "nuda proprietà", "usufrutto"
]


def safe_html(val, default="N.D."):
    if val is None or str(val).strip().lower() in ["none", "null", ""]:
        return default
    return html.escape(str(val).strip())


def carica_visti():
    if not os.path.exists(FILE_ARCHIVIO):
        return set()
    with open(FILE_ARCHIVIO, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def segna_visti(chiavi):
    with open(FILE_ARCHIVIO, "a", encoding="utf-8") as f:
        for ch in chiavi:
            c = str(ch).replace("\n", "").replace("\r", "").strip()
            if c:
                f.write(f"{c}\n")


def leggi_data_ultimo_invio():
    if os.path.exists(FILE_ULTIMO_INVIO):
        try:
            with open(FILE_ULTIMO_INVIO, "r", encoding="utf-8") as f:
                data_str = f.read().strip()
                if data_str:
                    return datetime.fromisoformat(data_str)
        except Exception:
            pass
    return None


def aggiorna_data_ultimo_invio():
    try:
        with open(FILE_ULTIMO_INVIO, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        print(f"[STATO] Errore timestamp: {e}")


def aggiorna_storico_markdown(nuovi_lotti):
    """Aggiorna il file Markdown di riepilogo con tutte le aste proposte."""
    righe_esistenti = []
    header = "# 🌿 Storico Aste Terreni Proposte\n\n| Data Rilevamento | Comune | ID Lotto | Titolo | Offerta Minima | Scadenza Asta | Link |\n| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
    
    if os.path.exists(FILE_STORICO_MD):
        try:
            with open(FILE_STORICO_MD, "r", encoding="utf-8") as f:
                contenuto = f.read()
                # Estrae le righe della tabella esistente (se presenti)
                if "|" in contenuto:
                    righe_esistenti = [r for r in contenuto.split("\n") if r.startswith("|") and "---" not in r and "Data Rilevamento" not in r]
        except Exception:
            pass

    oggi_str = datetime.now().strftime("%Y-%m-%d")
    
    # Crea le nuove righe
    nuove_righe_set = set(righe_esistenti)
    for l in nuovi_lotti:
        link_markdown = f"[Apri Scheda]({l['link']})"
        riga = f"| {oggi_str} | {l['comune']} | {l['id']} | {l['titolo'][:50]}... | {l['offerta_minima']} | **{l['data_asta']}** | {link_markdown} |"
        nuove_righe_set.add(riga)

    # Scrive il file aggiornato ordinato
    try:
        with open(FILE_STORICO_MD, "w", encoding="utf-8") as f:
            f.write(header)
            for r in sorted(list(nuove_righe_set), reverse=True):
                f.write(f"{r}\n")
        print("[STORICO] File Markdown aggiornato con successo.")
    except Exception as e:
        print(f"[ERRORE Scrittura Storico MD]: {e}")


def invia_telegram_html(messaggio):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERRORE] Credenziali Telegram mancanti.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": messaggio,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            payload.pop("parse_mode", None)
            requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"[TELEGRAM ECCEZIONE]: {e}")
    time.sleep(0.5)


def e_data_valida_non_passata(data_str):
    try:
        m = re.search(r"(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})", str(data_str))
        if not m:
            return True
        giorno, mese, anno = int(m.group(1)), int(m.group(2)), int(m.group(3))
        data_asta = datetime(anno, mese, giorno)
        return data_asta.date() >= datetime.now().date()
    except Exception:
        return True


def scansiona_catalogo_terreni_completo(citta):
    """Scansiona TUTTE le pagine disponibili per un comune senza limiti artificiali."""
    lotti = []
    ids_rilevati = set()
    base_url = f"https://www.astalegale.net/Immobili?luoghi={citta.lower()}&q=terreno"
    print(f"\n[CATALOGO {citta.upper()}] Connessione a: {base_url}")

    pagina = 1
    while True:
        url = f"{base_url}&page={pagina}" if pagina > 1 else base_url
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            trovati_pagina = 0

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/Aste/Detail/" in href:
                    parte_dopo = href.split("/Aste/Detail/")[1]
                    asta_id = parte_dopo.split("-")[0].strip()

                    if asta_id in ids_rilevati:
                        continue
                    ids_rilevati.add(asta_id)

                    titolo_pulito = parte_dopo.replace("-", " ")
                    titolo_str = " ".join(titolo_pulito.split())[:140]
                    titolo_lower = titolo_str.lower()

                    if "terreno" not in titolo_lower and "suolo" not in titolo_lower and "appezzamento" not in titolo_lower:
                        continue

                    if any(b in titolo_lower for b in BLACKLIST_CATEGORICA):
                        continue

                    link_completo = href if href.startswith("http") else f"https://www.astalegale.net{href}"

                    lotti.append({
                        "id": asta_id,
                        "comune": citta.capitalize(),
                        "titolo": titolo_str,
                        "link": link_completo
                    })
                    trovati_pagina += 1

            print(f"[{citta.upper()}] Pagina {pagina}: individuati {trovati_pagina} lotti terrieri.")
            
            # Se la pagina corrente non ha restituito nulla, interrompiamo il ciclo while
            if trovati_pagina == 0:
                break
                
            pagina += 1
            time.sleep(0.8)
        except Exception as e:
            print(f"[ERRORE Scansione {citta} pag {pagina}]: {e}")
            break

    return lotti


def estrai_dati_scheda(lotto):
    dati = {
        "offerta_minima": "N.D.",
        "prezzo_num": 99999999,
        "data_asta": "Vedi scheda",
        "stato_procedura": "ATTIVA",
        "testo_perizia": ""
    }
    try:
        r = session.get(lotto["link"], timeout=12)
        if r.status_code != 200:
            return dati

        soup = BeautifulSoup(r.text, "html.parser")
        testo_pulito = soup.get_text(separator=" ", strip=True)
        testo_lower = testo_pulito.lower()

        if any(w in testo_lower for w in ["sospesa", "sospeso", "revocata"]):
            dati["stato_procedura"] = "SOSPESA"

        m_off = re.search(r"Offerta\s+minima[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)
        if not m_off:
            m_off = re.search(r"Prezzo\s+base[^\d€]{0,25}€?\s*([\d\.]+(?:,\d{2})?)", testo_pulito, re.IGNORECASE)

        if m_off:
            cifra = m_off.group(1).strip()
            dati["offerta_minima"] = f"€ {cifra}"
            n = cifra.split(",")[0].replace(".", "").replace(" ", "")
            if n.isdigit():
                dati["prezzo_num"] = int(n)

        m_data = re.search(r"(?:data\s+vendita|data\s+asta|termine)[^\d]{0,25}(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{4})", testo_pulito, re.IGNORECASE)
        if m_data:
            dati["data_asta"] = m_data.group(1).replace("-", "/").replace(".", "/").strip()

        blocchi = []
        chiavi_rilevanti = [
            "terreno", "agricolo", "edificabile", "mq", "ettari", "superficie", 
            "destinazione", "strumento urbanistico", "poggio", "seminativo", "bosco", "uliveto"
        ]
        for el in soup.find_all(["p", "tr", "td", "li", "dd"]):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if 15 <= len(txt) <= 800 and any(k in txt.lower() for k in chiavi_rilevanti):
                if not any(txt in b for b in blocchi):
                    blocchi.append(txt)

        dati["testo_perizia"] = "\n".join(blocchi[:10]) if blocchi else testo_pulito[:2500]

    except Exception as e:
        print(f"[ERRORE Lettura Lotto {lotto['id']}]: {e}")

    return dati


def audit_terreni_batch(batch_lotti):
    if not client or not batch_lotti:
        return {}

    payload_ai = [
        {
            "id": l["id"],
            "comune": l["comune"],
            "titolo": l["titolo"],
            "offerta_minima": l["offerta_minima"],
            "descrizione": l["testo_perizia"][:2500]
        }
        for l in batch_lotti
    ]

    prompt = f"""
    Sei un perito agrario ed esperto immobiliare specializzato in stime di terreni agricoli ed edificabili in Toscana.
    Valuta questo gruppo di lotti terrieri situati nell'area vasta tra Firenze, Siena, Prato e Pisa per un acquirente privato con un BUDGET MASSIMO DI 50.000 €:
    {json.dumps(payload_ai, ensure_ascii=False, indent=2)}

    CRITERI DI SELEZIONE RIGIDI:
    1. TIPOLOGIA: Deve trattarsi esclusivamente di terreni (agricoli, boschivi, uliveti o edificabili). Scarta se include fabbricati principali abitativi o commerciali complessi.
    2. VALUTAZIONE ECONOMICA E SUPERFICIE: Verifica il rapporto prezzo/metri quadri. Il prezzo deve essere inferiore o uguale a 50.000 €.
    3. ACCESSIBILITÀ E POTENZIALE: Analizza dalla descrizione se il terreno ha accesso carrabile comodo o se risulta interclusi/impervi.
    4. VERDETTO: 'ACQUISTO CONSIGLIATO' solo se il prezzo è vantaggioso e il terreno ha buone potenzialità (es. buona esposizione, uliveto produttivo, edificabilità chiara o prezzo d'occasione). Altrimenti 'SCARTATO'.

    Rispondi RIGOROSAMENTE in JSON conforme a questo schema:
    {{
      "risultati": [
        {{
          "id": "ID_LOTTO",
          "destinazione_uso": "Agricolo / Edificabile / Boschivo / Misto",
          "superficie_stimata": "es. 2.500 mq o 1.5 ettari",
          "prezzo_mq_stimato": "es. 5 €/mq",
          "verdetto": "ACQUISTO CONSIGLIATO oppure SCARTATO",
          "giudizio": "Sintetica spiegazione urbanistica e commerciale del terreno"
        }}
      ]
    }}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        testo_resp = response.text.strip()
        if testo_resp.startswith("```"):
            testo_resp = re.sub(r"^```(?:json)?", "", testo_resp)
            testo_resp = re.sub(r"```$", "", testo_resp).strip()

        dati = json.loads(testo_resp)
        return {item["id"]: item for item in dati.get("risultati", [])}
    except Exception as e:
        print(f"[AI BATCH TERRENI ERRORE]: {e}")
        return {}


if __name__ == "__main__":
    print("=== AVVIO RADAR ASTE TERRENI (SCANSIONE COMPLETA - BUDGET MAX 50.000€) ===")

    FILTRA_GIA_VISTE = True
    visti = carica_visti()

    totali = []
    for comune in COMUNI_TARGET:
        lotti_comune = scansiona_catalogo_terreni_completo(comune)
        totali.extend(lotti_comune)
        time.sleep(0.4)

    print(f"Lotti terrieri totali individuati nell'area estesa: {len(totali)}")

    lotti_arricchiti = []
    for lotto in totali:
        lotto.update(estrai_dati_scheda(lotto))
        ch_data = str(lotto.get("data_asta", "ND")).strip().replace(" ", "_")
        lotto["chiave_tracciamento"] = f"TERRENO_{lotto['id']}_{ch_data}_{lotto['stato_procedura']}"
        lotti_arricchiti.append(lotto)
        time.sleep(0.5)

    candidati = []
    for l in lotti_arricchiti:
        if FILTRA_GIA_VISTE and l["chiave_tracciamento"] in visti:
            continue
        if l["stato_procedura"] == "SOSPESA":
            continue
        if 1000 <= l["prezzo_num"] <= 50000 and e_data_valida_non_passata(l.get("data_asta", "")):
            candidati.append(l)

    print(f"Candidati all'audit immobiliare AI (<= 50.000€): {len(candidati)}")

    dizionario_audit = {}
    dimensione_batch = 4
    for i in range(0, len(candidati), dimensione_batch):
        batch = candidati[i:i + dimensione_batch]
        if batch:
            dizionario_audit.update(audit_terreni_batch(batch))
            time.sleep(1.5)

    selezionati = []
    for l in candidati:
        aud = dizionario_audit.get(l["id"], {})
        l["audit"] = aud
        if aud.get("verdetto") == "ACQUISTO CONSIGLIATO":
            selezionati.append(l)

    # Rimosso il limite fisso [:5]: ora invia TUTTI i terreni validi trovati
    selezionati = sorted(selezionati, key=lambda x: x["prezzo_num"])

    if not selezionati:
        print("Nessun terreno interessante trovato oggi nell'area estesa.")
        data_ultimo = leggi_data_ultimo_invio()
        adesso = datetime.now()

        if data_ultimo is None:
            msg_primo_avvio = (
                "🌱 <b>RADAR ASTE TERRENI (TOSCANA ESTESA) AVVIATO</b>\n\n"
                "Il bot è operativo (scansione completa) e monitora tutta Firenze e i comuni di confine.\n"
                "<i>Nessun terreno idoneo (budget &le; 50.000 €) rilevato oggi.</i>"
            )
            invia_telegram_html(msg_primo_avvio)
            aggiorna_data_ultimo_invio()
        else:
            giorni = (adesso - data_ultimo).days
            if giorni >= 7:
                msg_hb = (
                    "ℹ️ <b>RADAR ASTE TERRENI (TOSCANA ESTESA)</b>\n\n"
                    "Il bot è regolarmente <b>attivo</b>.\n"
                    "<i>Nessun nuovo terreno rilevato negli ultimi 7 giorni nell'area selezionata.</i>"
                )
                invia_telegram_html(msg_hb)
                aggiorna_data_ultimo_invio()
    else:
        intro = (
            f"🌿 <b>OPPORTUNITÀ TERRENI (AREA ESTESA)</b>\n\n"
            f"Individuati e proposti <b>{len(selezionati)}</b> lotti terrieri d'interesse:"
        )
        invia_telegram_html(intro)

        for l in selezionati:
            aud = l["audit"]
            scheda = (
                f"📍 <b>{safe_html(l['comune'].upper())}</b> — <a href=\"{safe_html(l['link'])}\">{safe_html(l['id'])}</a>\n\n"
                f"🌿 <b>Lotto:</b> {safe_html(l['titolo'])}\n"
                f"📂 <b>Destinazione:</b> <code>{safe_html(aud.get('destinazione_uso', 'Terreno'))}</code>\n"
                f"📐 <b>Superficie:</b> <code>{safe_html(aud.get('superficie_stimata', 'N.D.'))}</code>\n"
                f"💰 <b>Offerta Minima:</b> <code>{safe_html(l['offerta_minima'])}</code>\n"
                f"📊 <b>Stima Prezzo/MQ:</b> <code>{safe_html(aud.get('prezzo_mq_stimato', 'N.D.'))}</code>\n"
                f"📅 <b>Data Asta:</b> <code>{safe_html(l['data_asta'])}</code>\n\n"
                f"💡 <b>ANALISI ESPERTO:</b>\n<i>{safe_html(aud.get('giudizio'))}</i>"
            )
            invia_telegram_html(scheda)

        # Aggiorna il documento Markdown di riepilogo con le aste proposte
        aggiorna_storico_markdown(selezionati)
        aggiorna_data_ultimo_invio()

    segna_visti([l["chiave_tracciamento"] for l in lotti_arricchiti if l.get("chiave_tracciamento")])
    print("=== MONITORAGGIO CONCLUSO ===")
