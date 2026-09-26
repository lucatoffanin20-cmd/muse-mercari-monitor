"""
Monitor Mercari per nuovi annunci "Muse" (CD) - versione a esecuzione
singola, pensata per essere lanciata periodicamente da GitHub Actions
(non ha un loop interno: fa un controllo e termina).

Variabili d'ambiente richieste:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mercapi import Mercapi
from mercapi.requests import SearchRequestData

# ----------------------------------------------------------------------------
# CONFIGURAZIONE
# ----------------------------------------------------------------------------

SEARCH_QUERY = "Muse"

# ID categoria Mercari "CD > 洋楽" (musica occidentale).
# Usare [75] per includere TUTTE le sottocategorie CD (musica giapponese,
# anime, classica, K-pop...) se in futuro si vuole allargare la ricerca.
CATEGORY_IDS = [695]

EXCLUDE_KEYWORD = ""  # parole da escludere lato server, es. "profumo maglietta"
TITLE_BLACKLIST: list[str] = []  # es. ["nintendo switch", "profumo"]

SEEN_FILE = Path(__file__).parent / "seen_items.json"
STATUS_FILE = Path(__file__).parent / "last_check.txt"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


# ----------------------------------------------------------------------------
# PERSISTENZA (il file viene poi ricommittato nel repo dal workflow)
# ----------------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


# ----------------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------------

async def send_telegram_message(client: httpx.AsyncClient, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    resp = await client.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Invio Telegram fallito ({resp.status_code}): {resp.text}")


def format_message(item) -> str:
    price = "prezzo non impostato" if item.is_no_price else f"¥{item.price:,}"
    item_url = f"https://jp.mercari.com/item/{item.id_}"
    return (
        f"🎵 <b>{item.name}</b>\n{price}\n{item_url}\n\n"
        f"Per comprarlo: incolla questo link nella barra di ricerca di ZenMarket."
    )


# ----------------------------------------------------------------------------
# MAIN (esecuzione singola)
# ----------------------------------------------------------------------------

async def main():
    seen = load_seen()
    first_run = not seen  # al primissimo avvio non notifichiamo tutto lo storico

    results = await Mercapi().search(
        SEARCH_QUERY,
        categories=CATEGORY_IDS,
        status=[SearchRequestData.Status.STATUS_ON_SALE],
        sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
        sort_order=SearchRequestData.SortOrder.ORDER_DESC,
        exclude=EXCLUDE_KEYWORD,
    )

    new_items = [
        item for item in results.items
        if item.id_ not in seen
        and not any(w.lower() in item.name.lower() for w in TITLE_BLACKLIST)
    ]

    async with httpx.AsyncClient() as client:
        for item in new_items:
            seen.add(item.id_)
            if first_run:
                continue
            print(f"Nuovo annuncio: {item.name}")
            await send_telegram_message(client, format_message(item))

    if first_run and new_items:
        print(f"Primo avvio: {len(new_items)} annunci esistenti segnati come già visti.")

    save_seen(seen)
    # timestamp aggiornato ad ogni run, cosi' c'e' sempre qualcosa da committare
    # e il workflow schedulato non viene disattivato per inattivita' del repo
    STATUS_FILE.write_text(datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    asyncio.run(main())
