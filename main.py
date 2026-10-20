from __future__ import annotations

import asyncio
import logging
import os
import sys

from automation_server_client import (
    AutomationServer,
    Credential,
    WorkItemError,
    WorkItemStatus,
    Workqueue,
)

from behandel import behandel_page
from configuration import ACADRE_CREDENTIAL_NAME
from populate_queue import populate_queue
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import BrowserSession

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("automation_server_client").setLevel(logging.WARNING)
logging.getLogger("debugpy").setLevel(logging.WARNING)


def _vaelg_items_til_behandling(workqueue: Workqueue):
    """Returnerer hele køen eller ét NEW-item til lokal debug."""
    item_reference = os.getenv(
        "DEBUG_ITEM_REFERENCE",
        "",
    ).strip()

    if not item_reference:
        return workqueue

    items = workqueue.get_item_by_reference(
        reference=item_reference,
        status=WorkItemStatus.NEW,
    )
    if not items:
        raise RuntimeError(
            "Ingen NEW-items fundet med reference: "
            f"{item_reference}"
        )

    item = items[0]
    item.update_status(
        WorkItemStatus.IN_PROGRESS.value,
        "Startet via lokal debugkørsel",
    )
    return [item]


def get_headless_flag() -> bool:
    """Returnerer True, når browseren skal køre skjult."""
    return os.getenv(
        "HEADLESS",
        "true",
    ).strip().casefold() == "true"


def _hent_acadre_http_credentials() -> dict[str, str]:
    """Henter Acadre-login fra Automation Server."""
    credential = Credential.get_credential(
        ACADRE_CREDENTIAL_NAME
    )
    username = str(credential.username or "").strip()
    password = str(credential.password or "")

    if not username or not password:
        raise RuntimeError(
            f"Credentialen {ACADRE_CREDENTIAL_NAME!r} "
            "mangler username eller password."
        )

    return {
        "username": username,
        "password": password,
    }


async def process_workqueue(
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Behandler Automation Server-items ét ad gangen."""
    logger.info(
        "Process workqueue mode started (debug=%s)",
        debug,
    )

    session = BrowserSession(
        headless=get_headless_flag(),
        debug=debug,
        http_credentials=_hent_acadre_http_credentials(),
    )
    page = None

    try:
        await session.start()
        page = await session.new_page()

        for item in _vaelg_items_til_behandling(workqueue):
            with item:
                data = item.data or {}

                try:
                    logger.info(
                        "Behandler item id=%s reference=%s.",
                        item.id,
                        item.reference,
                    )

                    resultat = await behandel_page(
                        item=item,
                        session=session,
                        page=page,
                    )

                    if resultat and resultat.get("manual"):
                        data = item.data or data
                        item.update(data)
                        item.complete(resultat["message"])
                        logger.info(
                            "Item %s er sendt til manuel behandling.",
                            item.reference,
                        )
                        continue

                    data = item.data or data
                    update_item_data(
                        data,
                        item=item,
                        status="Completed",
                        status_code="Færdig",
                    )
                    item.update(data)
                    item.complete("Completed")
                    logger.info(
                        "Item %s er afsluttet.",
                        item.reference,
                    )

                except WorkItemError as exc:
                    logger.error(
                        "WorkItemError for item %s: %s",
                        item.reference,
                        exc,
                    )
                    item.fail(str(exc))

                except Exception as exc:
                    logger.exception(
                        "Uventet fejl for item %s.",
                        item.reference,
                    )
                    try:
                        screenshot_page = page
                        if (
                            session.context is not None
                            and session.context.pages
                        ):
                            screenshot_page = session.context.pages[-1]

                        if (
                            screenshot_page is not None
                            and not screenshot_page.is_closed()
                        ):
                            await session.screenshot(
                                screenshot_page,
                                (
                                    "hard_exception_"
                                    f"{type(exc).__name__}"
                                ),
                                always=True,
                            )
                    except Exception:
                        logger.exception(
                            "Kunne ikke tage screenshot ved hard error."
                        )
                    raise
    finally:
        await session.close()


def main() -> None:
    """Starter producer med --queue, ellers worker."""
    debug = "--debug" in sys.argv
    queue_mode = "--queue" in sys.argv

    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    if queue_mode:
        asyncio.run(
            populate_queue(
                workqueue=workqueue,
                debug=debug,
            )
        )
        return

    asyncio.run(
        process_workqueue(
            workqueue=workqueue,
            debug=debug,
        )
    )


if __name__ == "__main__":
    main()
