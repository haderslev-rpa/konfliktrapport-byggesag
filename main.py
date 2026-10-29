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
from configuration import (
    ACADRE_CREDENTIAL_NAME,
    WEBGIS_CREDENTIAL_NAME,
)
from populate_queue import populate_queue
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)


logger = logging.getLogger(__name__)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s [%(levelname)s] "
        "%(name)s: %(message)s"
    ),
    force=True,
)

logging.getLogger(
    "httpx"
).setLevel(
    logging.WARNING
)

logging.getLogger(
    "automation_server_client"
).setLevel(
    logging.WARNING
)

logging.getLogger(
    "debugpy"
).setLevel(
    logging.WARNING
)


# ------------------------------------------------------------
# Work-item-valg
# ------------------------------------------------------------


def _vaelg_items_til_behandling(
    workqueue: Workqueue,
):
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


# ------------------------------------------------------------
# Browserindstillinger
# ------------------------------------------------------------


def get_headless_flag() -> bool:
    """Returnerer True, når browseren skal køre skjult."""
    return os.getenv(
        "HEADLESS",
        "true",
    ).strip().casefold() == "true"


# ------------------------------------------------------------
# Credentials
# ------------------------------------------------------------


def _hent_http_credentials(
    credential_name: str,
) -> dict[str, str]:
    """
    Henter og validerer HTTP-credentials fra Automation Server.

    Output:
        Dictionary med username og password.
    """
    credential = Credential.get_credential(
        credential_name
    )

    if credential is None:
        raise RuntimeError(
            f"Credentialen {credential_name!r} "
            "blev ikke fundet."
        )

    username = str(
        credential.username or ""
    ).strip()

    password = str(
        credential.password or ""
    )

    if not username or not password:
        raise RuntimeError(
            f"Credentialen {credential_name!r} "
            "mangler username eller password."
        )

    return {
        "username": username,
        "password": password,
    }


def _hent_acadre_http_credentials() -> dict[str, str]:
    """Henter Acadre-login fra Automation Server."""
    return _hent_http_credentials(
        ACADRE_CREDENTIAL_NAME
    )


def _hent_webgis_http_credentials() -> dict[str, str]:
    """Henter WebGIS-login fra Automation Server."""
    return _hent_http_credentials(
        WEBGIS_CREDENTIAL_NAME
    )


# ------------------------------------------------------------
# Worker
# ------------------------------------------------------------


async def process_workqueue(
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """
    Behandler Automation Server-items ét ad gangen.

    Acadre- og WebGIS-browserprocesserne startes én gang før
    item-loopet. behandel.py modtager de allerede startede sessioner
    og må derfor ikke starte eller lukke Chromium pr. item.
    """
    logger.info(
        "Process workqueue mode started (debug=%s)",
        debug,
    )

    headless = get_headless_flag()

    acadre_session = BrowserSession(
        headless=headless,
        debug=debug,
        http_credentials=(
            _hent_acadre_http_credentials()
        ),
    )

    webgis_session = BrowserSession(
        headless=headless,
        debug=debug,
        video=False,
        http_credentials=(
            _hent_webgis_http_credentials()
        ),
    )

    acadre_page = None

    try:
        logger.info(
            "Starter Acadre-browserprocessen én gang."
        )

        await acadre_session.start()

        logger.info(
            "Starter WebGIS-browserprocessen én gang."
        )

        await webgis_session.start()

        acadre_page = await acadre_session.new_page()

        logger.info(
            "Acadre- og WebGIS-browserprocesserne er klar."
        )

        for item in _vaelg_items_til_behandling(
            workqueue
        ):
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
                        session=acadre_session,
                        page=acadre_page,
                        webgis_session=webgis_session,
                    )

                    if (
                        resultat
                        and resultat.get("manual")
                    ):
                        data = item.data or data
                        item.update(data)
                        item.complete(
                            resultat["message"]
                        )

                        logger.info(
                            "Item %s er sendt til "
                            "manuel behandling.",
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

                    item.fail(
                        str(exc)
                    )

                except Exception as exc:
                    logger.exception(
                        "Uventet fejl for item %s.",
                        item.reference,
                    )

                    await _tag_hard_error_screenshot(
                        acadre_session=acadre_session,
                        acadre_page=acadre_page,
                        webgis_session=webgis_session,
                        fejltype=type(exc).__name__,
                    )

                    raise

    finally:
        logger.info(
            "Lukker WebGIS-browserprocessen."
        )

        try:
            await webgis_session.close()
        except Exception:
            logger.exception(
                "WebGIS BrowserSession kunne ikke "
                "lukkes korrekt."
            )

        logger.info(
            "Lukker Acadre-browserprocessen."
        )

        try:
            await acadre_session.close()
        except Exception:
            logger.exception(
                "Acadre BrowserSession kunne ikke "
                "lukkes korrekt."
            )


async def _tag_hard_error_screenshot(
    *,
    acadre_session: BrowserSession,
    acadre_page,
    webgis_session: BrowserSession,
    fejltype: str,
) -> None:
    """
    Tager et screenshot fra seneste åbne WebGIS- eller Acadre-side.

    Screenshotfejl logges, men skjuler ikke den oprindelige fejl.
    """
    try:
        screenshot_session = acadre_session
        screenshot_page = acadre_page

        if (
            webgis_session.context is not None
            and webgis_session.context.pages
        ):
            webgis_page = (
                webgis_session.context.pages[-1]
            )

            if not webgis_page.is_closed():
                screenshot_session = webgis_session
                screenshot_page = webgis_page

        elif (
            acadre_session.context is not None
            and acadre_session.context.pages
        ):
            screenshot_page = (
                acadre_session.context.pages[-1]
            )

        if (
            screenshot_page is not None
            and not screenshot_page.is_closed()
        ):
            await screenshot_session.screenshot(
                screenshot_page,
                (
                    "hard_exception_"
                    f"{fejltype}"
                ),
                always=True,
            )

    except Exception:
        logger.exception(
            "Kunne ikke tage screenshot ved hard error."
        )


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------


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