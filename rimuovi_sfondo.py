"""Rimozione sfondo via Pixelcut: accetta bytes in RAM e restituisce bytes.

La sequenza di pause/attese e i selettori restano quelli dello script originale
che funzionava; cambia solo I/O (bytes in RAM + file temporanei).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger("rimuovi-sfondo")

# Su Render serve headless; in locale puoi impostare PIXELCUT_HEADLESS=false
HEADLESS = os.getenv("PIXELCUT_HEADLESS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}


def _guess_suffix(image_bytes: bytes, fallback: str = ".jpg") -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    return fallback


def remove_background(image_bytes: bytes) -> bytes:
    """Carica l'immagine su Pixelcut, scarica il risultato e lo restituisce in RAM."""
    if not image_bytes:
        raise ValueError("Immagine vuota: impossibile rimuovere lo sfondo")

    suffix = _guess_suffix(image_bytes)
    input_path: Path | None = None
    output_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as input_file:
            input_file.write(image_bytes)
            input_path = Path(input_file.name)

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as output_file:
            output_path = Path(output_file.name)

        foto_path = str(input_path)

        with sync_playwright() as p:
            print("Avvio del browser...")
            browser = p.chromium.launch(headless=HEADLESS)
            context = browser.new_context()
            page = context.new_page()

            print("Apertura del sito web...")
            page.goto(
                "https://www.pixelcut.ai/it/rimuovi-sfondo/sfondo-bianco",
                timeout=60000,
            )

            # Aspettiamo che la pagina carichi
            page.wait_for_load_state("networkidle")

            print("Caricamento dell'immagine...")
            # Inseriamo il file direttamente nell'input nascosto
            try:
                page.locator('input[type="file"]').first.set_input_files(foto_path)
            except Exception:
                # Se l'input file non viene trovato, proviamo con il file chooser
                with page.expect_file_chooser() as fc_info:
                    page.locator("button, div, a").filter(
                        has_text=re.compile(r"(Carica|Upload)", re.IGNORECASE)
                    ).first.click()
                file_chooser = fc_info.value
                file_chooser.set_files(foto_path)

            print("Immagine caricata. Attesa dell'elaborazione...")

            # Aspettiamo un tempo generoso affinché l'IA finisca di rimuovere lo sfondo
            page.wait_for_timeout(10000)

            # Aspettiamo che appaia il pulsante "Scarica" (Download) e che sia pronto
            download_btn = page.locator("button, a").filter(
                has_text=re.compile(r"(Scarica|Download)", re.IGNORECASE)
            ).first
            download_btn.wait_for(state="visible", timeout=60000)

            print("Pulsante di download trovato. Tentativo di scaricamento...")

            # Clicchiamo sul pulsante Scarica principale e aspettiamo che il popup si apra
            download_btn.click(force=True)

            # Diamo tempo al popup di aprirsi
            print("Attesa apertura pannello di download...")
            page.wait_for_timeout(3000)

            # Cerchiamo il pulsante per il download gratuito / anteprima
            print("Selezione risoluzione gratuita...")

            # Cerchiamo vari testi possibili nel popup
            free_btn = page.locator("text=/anteprima/i").first

            try:
                free_btn.wait_for(state="visible", timeout=5000)
                print("Pulsante con 'anteprima' trovato!")
            except Exception:
                print("Testo 'anteprima' non trovato. Cerco la parola 'gratuito' o 'free'...")
                free_btn = page.locator("text=/gratuito|free/i").first
                try:
                    free_btn.wait_for(state="visible", timeout=5000)
                    print("Pulsante gratuito trovato!")
                except Exception:
                    print(
                        "Nessun testo specifico trovato. Cerco il pulsante 'Scarica' nel popup..."
                    )
                    # Cerchiamo un pulsante Scarica che sia visibile nel popup
                    free_btn = (
                        page.locator("button")
                        .filter(has_text=re.compile(r"(Scarica|Download)", re.IGNORECASE))
                        .locator("visible=true")
                        .last
                    )

            print("Avvio scaricamento...")
            with page.expect_download(timeout=60000) as download_info:
                free_btn.click(force=True)
            download = download_info.value

            download.save_as(str(output_path))
            print(f"Successo! Immagine elaborata: {output_path}")

            browser.close()

        result = output_path.read_bytes()
        if not result:
            raise RuntimeError("Download Pixelcut vuoto")
        return result
    finally:
        if input_path is not None:
            input_path.unlink(missing_ok=True)
        if output_path is not None:
            output_path.unlink(missing_ok=True)


def main() -> None:
    """Uso locale: cerca un file 'foto.*' e salva 'fotosenzasfondo'."""
    current_dir = Path(__file__).resolve().parent
    foto_path = next(current_dir.glob("foto.*"), None)
    if foto_path is None or not foto_path.is_file():
        print("Errore: Nessun file denominato 'foto' trovato nella cartella corrente.")
        return

    print(f"File trovato: {foto_path}")
    original_ext = foto_path.suffix
    result = remove_background(foto_path.read_bytes())
    save_path = current_dir / f"fotosenzasfondo{original_ext}"
    save_path.write_bytes(result)
    print(f"Successo! Immagine salvata in: {save_path}")


if __name__ == "__main__":
    main()
