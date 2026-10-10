"""
Monitor Mercari per annunci "Muse" - più ricerche indipendenti, titolo
tradotto in italiano, foto nella notifica, errori isolati per singolo
annuncio. Pensata per essere lanciata periodicamente da GitHub Actions.

Variabili d'ambiente richieste:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import asyncio
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from deep_translator import GoogleTranslator
from mercapi import Mercapi
from mercapi.requests import SearchRequestData

# ----------------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------------

# Categoria "CD" (75) + tutte le sue sottocategorie elencate esplicitamente
# (694 giapponese, 695 occidentale, 696 anime, 697 classica, 698 K-POP/Asia,
# 699 bambini, 700 altro): cosi' funziona sia che Mercari espanda da solo
# la categoria madre sia che non lo faccia.
CD_CATEGORIES = [75, 694, 695, 696, 697, 698, 699, 700]

SEARCHES = [
    {"label": "Muse CD", "query": "Muse", "categories": CD_CATEGORIES},
    {"label": "Muse Cassette", "query": "Muse cassette", "categories": None},
    {"label": "Muse Promo", "query": "Muse promo", "categories": None},
]

PAUSE_BETWEEN_SEARCHES_SECONDS = 3
MAX_PAGES_PER_SEARCH = 3  # fino a 360 risultati (120 per pagina)

EXCLUDE_KEYWORD = ""
TITLE_BLACKLIST: list[str] = []

STATE_FILE = Path(__file__).parent / "seen_items.json"
STATUS_FILE = Path(__file__).parent / "last_check.txt"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

translator = GoogleTranslator(source="ja", target="it")


# ----------------------------------------------------------------------------
# PERSISTENZA
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        raw = STATE_FILE.read_text().strip()
        if raw and raw != "[]":
            data = json.loads(raw)
            if isinstance(data, list):
                return {"seen_ids": data, "initialized_labels": [s["label"] for s in SEARCHES]}
            return data
    return {"seen_ids": [], "initialized_labels": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


# ----------------------------------------------------------------------------
# TRADUZIONE (best-effort: se fallisce, usiamo il titolo originale)
# ----------------------------------------------------------------------------

def translate_title(text: str) -> str | None:
    try:
        translated = translator.translate(text)
        return translated if translated else None
    except Exception as exc:
        print(f"Traduzione fallita per '{text}': {exc}")
        return None


# ----------------------------------------------------------------------------
# TELEGRAM - ogni funzione restituisce True/False, cosi' sappiamo davvero
# se il messaggio e' partito (prima un errore di Telegram veniva ignorato
# e l'annuncio risultava "gia' notificato" anche se non era arrivato nulla)
# ----------------------------------------------------------------------------

async def _post_telegram(client: httpx.AsyncClient, method: str, payload: dict) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = await client.post(url, json=payload, timeout=15)
    except Exception as exc:
        print(f"Telegram {method}: errore di rete: {exc}")
        return False
    if resp.status_code != 200:
        print(f"Telegram {method} fallito ({resp.status_code}): {resp.text}")
        return False
    return True


async def send_telegram_text(client: httpx.AsyncClient, text: str, html_mode: bool = True) -> bool:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if html_mode:
        payload["parse_mode"] = "HTML"
    return await _post_telegram(client, "sendMessage", payload)


async def send_telegram_photo(client: httpx.AsyncClient, photo_url: str, caption: str) -> bool:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    return await _post_telegram(client, "sendPhoto", payload)


async def notify_new_item(client: httpx.AsyncClient, item, label: str) -> None:
    translated = translate_title(item.name)
    price = "prezzo non impostato" if item.is_no_price else f"¥{item.price:,}"
    item_url = f"https://jp.mercari.com/item/{item.id_}"
    footer = "Per comprarlo: incolla questo link nella barra di ricerca di ZenMarket."

    # versione HTML: i caratteri speciali nei titoli (&, <, >) vanno "protetti",
    # altrimenti Telegram rifiuta il messaggio con errore 400
    if translated:
        title_html = f"🎵 <b>{html.escape(translated, quote=False)}</b>\n<i>{html.escape(item.name, quote=False)}</i>"
        title_plain = f"🎵 {translated}\n{item.name}"
    else:
        title_html = f"🎵 <b>{html.escape(item.name, quote=False)}</b>"
        title_plain = f"🎵 {item.name}"

    caption_html = f"{title_html}\n[{html.escape(label, quote=False)}] {price}\n{item_url}\n\n{footer}"
    caption_plain = f"{title_plain}\n[{label}] {price}\n{item_url}\n\n{footer}"

    # tentativi in ordine: foto -> testo formattato -> testo semplice
    if item.thumbnails and await send_telegram_photo(client, item.thumbnails[0], caption_html):
        return
    if await send_telegram_text(client, caption_html):
        return
    if await send_telegram_text(client, caption_plain, html_mode=False):
        return

    raise RuntimeError("tutti i tentativi di invio su Telegram sono falliti")


# ----------------------------------------------------------------------------
# RICERCA
# ----------------------------------------------------------------------------

async def run_search(mercapi: Mercapi, profile: dict):
    kwargs = {}
    if profile.get("categories"):
        kwargs["categories"] = profile["categories"]

    results = await mercapi.search(
        profile["query"],
        status=[SearchRequestData.Status.STATUS_ON_SALE],
        sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
        sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        exclude=EXCLUDE_KEYWORD,
        **kwargs,
    )
    all_items = list(results.items)

    page = 1
    while (
        page < MAX_PAGES_PER_SEARCH
        and results.meta.num_found > len(all_items)
        and results.meta.next_page_token
    ):
        await asyncio.sleep(1)
        results = await results.next_page()
        all_items.extend(results.items)
        page += 1

    return [
        item for item in all_items
        if not any(w.lower() in item.name.lower() for w in TITLE_BLACKLIST)
    ]


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

async def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    initialized_labels = set(state.get("initialized_labels", []))

    mercapi = Mercapi()

    async with httpx.AsyncClient() as client:
        for i, profile in enumerate(SEARCHES):
            label = profile["label"]
            is_first_run_for_this_search = label not in initialized_labels

            if i > 0:
                await asyncio.sleep(PAUSE_BETWEEN_SEARCHES_SECONDS)

            try:
                items = await run_search(mercapi, profile)
            except Exception as exc:
                print(f"Errore nella ricerca '{label}': {exc}")
                await send_telegram_text(
                    client, f"⚠️ La ricerca '{label}' è fallita in questo giro: {exc}", html_mode=False
                )
                continue

            new_items = [item for item in items if item.id_ not in seen_ids]

            for item in new_items:
                if is_first_run_for_this_search:
                    seen_ids.add(item.id_)
                    continue

                try:
                    await notify_new_item(client, item, label)
                    seen_ids.add(item.id_)  # segnato come visto SOLO se la notifica e' partita davvero
                    print(f"Nuovo annuncio notificato [{label}]: {item.name}")
                except Exception as exc:
                    # non blocchiamo l'intera esecuzione per un singolo annuncio:
                    # non essendo segnato come visto, ci riproveremo al giro successivo
                    print(f"Notifica fallita per '{item.name}' [{label}]: {exc}")

            if is_first_run_for_this_search:
                initialized_labels.add(label)
                print(f"Prima esecuzione per '{label}': {len(new_items)} annunci segnati come già visti.")

    save_state({"seen_ids": sorted(seen_ids), "initialized_labels": sorted(initialized_labels)})
    STATUS_FILE.write_text(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    asyncio.run(main())
