import asyncio
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright


ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

CATALOG_URL_TEMPLATE = (
    "https://www.vinted.it/catalog?catalog[]=19&brand_ids[]=6005&page=1"
    "&time=1790433089&order=newest_first&price_to={price}&currency=EUR"
)
POLL_SECONDS = max(20, int(os.getenv("VINTED_POLL_SECONDS", "60")))
INITIAL_BATCH_LIMIT = 5
PRICE_CHANGE_BATCH_LIMIT = 10
DEFAULT_MAX_PRICE = "10"
HEADLESS = os.getenv("VINTED_HEADLESS", "true").lower() not in {"0", "false", "no"}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vinted-monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def normalize_price(value: str) -> str | None:
    try:
        price = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not price.is_finite() or price <= 0:
        return None
    return format(price.normalize(), "f")


def set_max_price(price_state: dict[str, str], price: str) -> None:
    if price_state["value"] != price:
        price_state["value"] = price
        price_state["batch_price"] = price


def catalog_url(max_price: str) -> str:
    parts = urlsplit(CATALOG_URL_TEMPLATE.format(price="0"))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["price_to"] = max_price
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, safe="[]"), "")
    )


def item_id_from_url(url: str) -> str | None:
    match = re.search(r"/items/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def canonical_item_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


async def listing_item_urls(page) -> list[str]:
    await page.locator('a[href*="/items/"]').first.wait_for(timeout=45_000)
    links = await page.locator('a[href*="/items/"]').evaluate_all(
        "elements => elements.map(element => element.href)"
    )
    unique_urls: list[str] = []
    found_ids: set[str] = set()
    for url in links:
        item_id = item_id_from_url(url)
        if item_id and item_id not in found_ids:
            found_ids.add(item_id)
            unique_urls.append(url)
    return unique_urls


def select_new_item_urls(
    item_urls: list[str],
    seen_items: dict[str, dict[str, str]],
    limit: int | None = None,
) -> tuple[list[str], str | None]:
    selected_urls: list[str] = []
    for item_url in item_urls:
        item_id = item_id_from_url(item_url)
        if item_id is None:
            continue
        if item_id in seen_items:
            return selected_urls, item_id
        selected_urls.append(item_url)
        if limit is not None and len(selected_urls) >= limit:
            break
    return selected_urls, None


async def read_item(page, item_url: str) -> dict[str, str]:
    await page.goto(item_url, wait_until="domcontentloaded", timeout=60_000)
    info = page.locator("main.item-information")
    title_locator = info.locator("h1").first
    await title_locator.wait_for(timeout=30_000)
    title = (await title_locator.inner_text()).strip()

    price_locator = info.locator('[data-testid="item-price"]').first
    price = re.sub(r"\s+", " ", (await price_locator.inner_text())).strip()

    description_locator = info.locator(".u-text-wrap").first
    description = ""
    if await description_locator.count():
        description = (await description_locator.inner_text()).strip()

    image_locator = page.locator("main figure img").first
    await image_locator.wait_for(timeout=20_000)
    image_url = await image_locator.evaluate("image => image.currentSrc || image.src")
    if not image_url:
        raise RuntimeError("La scheda non contiene un URL immagine utilizzabile")

    return {
        "title": title,
        "price": price,
        "description": description or "Descrizione non presente",
        "image_url": image_url,
        "item_url": canonical_item_url(page.url),
    }


async def telegram_api_call(
    client: httpx.AsyncClient,
    method: str,
    data: dict[str, str],
    files: dict | None = None,
) -> dict:
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
            data=data,
            files=files,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise RuntimeError(
            f"Richiesta Telegram {method} fallita ({type(error).__name__})"
        ) from None
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram ha rifiutato {method}: {result.get('description', 'errore sconosciuto')}")
    return result


async def telegram_send_text(
    client: httpx.AsyncClient, chat_id: str, text: str, reply_markup: dict | None = None
) -> dict:
    data: dict[str, str] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    return await telegram_api_call(client, "sendMessage", data)


def price_menu() -> dict:
    values = ("5", "10", "15", "20", "30", "50")
    buttons = [
        {"text": f"{value} €", "callback_data": f"max:{value}"}
        for value in values
    ]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    rows.append([{"text": "Altro importo", "callback_data": "max:custom"}])
    return {"inline_keyboard": rows}


async def process_telegram_update(
    client: httpx.AsyncClient,
    update: dict,
    price_state: dict[str, str],
    awaiting_custom_price: set[str],
) -> None:
    callback = update.get("callback_query")
    if callback:
        callback_message = callback.get("message", {})
        chat_id = str(callback_message.get("chat", {}).get("id", ""))
        if chat_id != TELEGRAM_CHAT_ID:
            return
        await telegram_api_call(
            client,
            "answerCallbackQuery",
            {"callback_query_id": str(callback["id"])},
        )
        callback_data = str(callback.get("data", ""))
        if callback_data == "max:custom":
            awaiting_custom_price.add(chat_id)
            await telegram_send_text(
                client, chat_id, "Scrivi il nuovo prezzo massimo in euro (es. 17,50)."
            )
        elif callback_data.startswith("max:"):
            price = normalize_price(callback_data.removeprefix("max:"))
            if price:
                set_max_price(price_state, price)
                await telegram_send_text(
                    client, chat_id, f"Prezzo massimo impostato a {price} €."
                )
        return

    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text", "")).strip()
    if chat_id != TELEGRAM_CHAT_ID or not text:
        return

    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if chat_id in awaiting_custom_price and not text.startswith("/"):
        price = normalize_price(text)
        if price:
            set_max_price(price_state, price)
            awaiting_custom_price.discard(chat_id)
            await telegram_send_text(
                client, chat_id, f"Prezzo massimo impostato a {price} €."
            )
        else:
            await telegram_send_text(
                client,
                chat_id,
                "Importo non valido. Inserisci un numero maggiore di zero, ad esempio 17,50.",
            )
        return

    if command == "/prezzo":
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            price = normalize_price(parts[1])
            if price:
                set_max_price(price_state, price)
                await telegram_send_text(
                    client, chat_id, f"Prezzo massimo impostato a {price} €."
                )
            else:
                await telegram_send_text(
                    client, chat_id, "Importo non valido. Usa, ad esempio, /prezzo 17,50."
                )
            return
        await telegram_send_text(
            client,
            chat_id,
            f"Prezzo massimo attuale: {price_state['value']} €. Scegli un importo:",
            price_menu(),
        )
    elif command == "/start":
        await telegram_send_text(
            client, chat_id, "Monitor Vinted attivo. Usa /prezzo per cambiare il prezzo massimo."
        )


async def telegram_command_listener(
    client: httpx.AsyncClient, price_state: dict[str, str]
) -> None:
    offset: int | None = None
    awaiting_custom_price: set[str] = set()
    while True:
        try:
            params: dict[str, str | int] = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            response = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=25,
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError("Telegram getUpdates ha restituito un errore")
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                await process_telegram_update(
                    client, update, price_state, awaiting_custom_price
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Polling Telegram fallito (%s); nuovo tentativo tra 5 secondi",
                type(error).__name__,
            )
            await asyncio.sleep(5)


async def send_telegram_item(
    client: httpx.AsyncClient, item: dict[str, str]
) -> None:
    heading = f"{item['title']}\nPrezzo: {item['price']}"
    description = item["description"]
    caption_limit = 1000
    link_line = f"Link: {item['item_url']}"
    caption_overhead = len(heading) + len(link_line) + 10
    description_length = max(0, caption_limit - caption_overhead)
    caption = f"{heading}\n\n{description[:description_length]}\n\n{link_line}".strip()
    remaining_description = description[description_length:]

    await telegram_api_call(
        client,
        "sendPhoto",
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "photo": item["image_url"],
        },
    )

    if remaining_description:
        chunks = [
            remaining_description[index : index + 3500]
            for index in range(0, len(remaining_description), 3500)
        ]
        for index, chunk in enumerate(chunks):
            text = chunk
            if index == len(chunks) - 1:
                text = f"{chunk}\n\n{link_line}"
            data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
            await telegram_api_call(client, "sendMessage", data)


async def monitor() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Configura TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID nel file .env prima dell'avvio"
        )

    seen_items: dict[str, dict[str, str]] = {}
    price_state = {"value": DEFAULT_MAX_PRICE}
    initial_batch = not seen_items
    first_cycle = True

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=HEADLESS)
        page = await browser.new_page(locale="it-IT")
        page.set_default_timeout(30_000)

        try:
            async with httpx.AsyncClient(timeout=45) as telegram_client:
                telegram_task = asyncio.create_task(
                    telegram_command_listener(telegram_client, price_state)
                )
                try:
                    while True:
                        cycle_price = price_state["value"]
                        cycle_batch_price = price_state.get("batch_price")
                        cycle_succeeded = False
                        try:
                            desired_catalog_url = catalog_url(cycle_price)
                            if page.url != desired_catalog_url:
                                await page.goto(
                                    desired_catalog_url,
                                    wait_until="domcontentloaded",
                                    timeout=60_000,
                                )
                            elif not first_cycle:
                                await page.reload(
                                    wait_until="domcontentloaded", timeout=60_000
                                )

                            item_urls = await listing_item_urls(page)
                            if not item_urls:
                                raise RuntimeError("Nessuna inserzione trovata nel catalogo")

                            logger.info(
                                "Inserzioni trovate in elenco: %d (prezzo massimo: %s EUR)",
                                len(item_urls),
                                cycle_price,
                            )
                            if initial_batch:
                                limit = INITIAL_BATCH_LIMIT
                            elif cycle_batch_price == cycle_price:
                                limit = PRICE_CHANGE_BATCH_LIMIT
                            else:
                                limit = None
                            urls_to_process, existing_item_id = select_new_item_urls(
                                item_urls, seen_items, limit
                            )

                            for item_url in urls_to_process:
                                item_id = item_id_from_url(item_url)
                                if item_id is None:
                                    continue

                                item = await read_item(page, item_url)
                                await send_telegram_item(telegram_client, item)
                                seen_items[item_id] = {
                                    "title": item["title"],
                                    "url": item["item_url"],
                                }
                                logger.info(
                                    "Trovata e inviata su Telegram: %s (%s)",
                                    item["title"],
                                    item["price"],
                                )

                                await page.go_back(
                                    wait_until="domcontentloaded", timeout=60_000
                                )

                            if existing_item_id:
                                logger.info(
                                    "Raggiunta inserzione gia salvata: %s",
                                    seen_items[existing_item_id]["title"],
                                )
                            elif initial_batch and len(urls_to_process) >= INITIAL_BATCH_LIMIT:
                                logger.info(
                                    "Primo avvio: limite di %d inserzioni raggiunto",
                                    INITIAL_BATCH_LIMIT,
                                )
                            else:
                                logger.info("Fine inserzioni nuove nell'elenco")
                            if limit == PRICE_CHANGE_BATCH_LIMIT:
                                logger.info(
                                    "Scansione dopo cambio prezzo: massimo %d nuove inserzioni",
                                    PRICE_CHANGE_BATCH_LIMIT,
                                )
                            cycle_succeeded = True

                        except Exception:
                            logger.exception(
                                "Errore durante il controllo; riprovo al prossimo ciclo"
                            )
                        finally:
                            initial_batch = False
                            first_cycle = False
                            if (
                                cycle_succeeded
                                and cycle_batch_price == cycle_price
                                and price_state.get("batch_price") == cycle_price
                            ):
                                price_state.pop("batch_price", None)

                        logger.info(
                            "Prossimo aggiornamento del catalogo tra %d secondi",
                            POLL_SECONDS,
                        )
                        await asyncio.sleep(POLL_SECONDS)
                finally:
                    telegram_task.cancel()
                    try:
                        await telegram_task
                    except asyncio.CancelledError:
                        pass
        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        logger.info("Monitor interrotto dall'utente")