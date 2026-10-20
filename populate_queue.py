from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from automation_server_client import (
    AutomationServer,
    Credential,
    Workqueue,
)
from playwright.async_api import (
    Page,
    Request,
)

from configuration import (
    COMMAND,
    DATASOURCE,
    DEBUG_BROWSER,
    ERROR_CONTENT_TYPE,
    HEADLESS,
    JSESSIONID_COOKIE_NAME,
    JSON_TYPES,
    MAX_ROWS,
    MAX_X_WC,
    MAX_Y_WC,
    MINIMUM_CASEID,
    MIN_X_WC,
    MIN_Y_WC,
    NAVIGATION_TIMEOUT_MS,
    OUTPUT_FORMAT,
    PROFILE,
    SESSION_ID_HEADER_NAME,
    SESSION_TIMEOUT_SECONDS,
    SPATIAL_EXPRESSION,
    WEBGIS_BASE_URL,
    WEBGIS_CREDENTIAL_NAME,
    WEBGIS_HOSTNAME,
    WEBGIS_URL,
)
from q_haderslev_vbo.automation_server.ats_is_item_in_queue import (
    is_item_in_queue,
)
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)
from q_webgis.functionality.spatialanalyze import (
    hent_spatialanalyze_raekker,
    spatialanalyze,
)


logger = logging.getLogger(__name__)

# Vis kun advarsler og fejl fra det underliggende spatialanalysemodul.
logging.getLogger(
    "q_webgis.functionality.spatialanalyze"
).setLevel(
    logging.WARNING
)


# ------------------------------------------------------------
# Offentlig producer-funktion
# ------------------------------------------------------------

async def populate_queue(
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """
    Henter WebGIS-spatialanalysen og opretter relevante ATS-items.

    Regler:
        - caseid skal kunne læses som et positivt heltal.
        - caseid skal være MINIMUM_CASEID eller højere.
        - caseid bruges som item-reference.
        - En reference oprettes ikke, hvis den allerede findes i køen.
        - Dubletter i samme WebGIS-resultat oprettes kun én gang.
        - Hele WebGIS-rækken gemmes i item.data["box"]["webgis"].

    Logging:
        Der logges ikke én linje pr. oprettet WorkItem.
        Til sidst logges en kort samlet oversigt.

    Output:
        Funktionen returnerer ikke data.
        Relevante WorkItems oprettes direkte i workqueuen.
    """

    _valider_workqueue(
        workqueue
    )

    logger.info(
        "Starter populering af kø fra WebGIS spatialanalyse."
    )

    logger.info(
        "Laveste tilladte caseid: %s.",
        MINIMUM_CASEID,
    )

    raekker = await _hent_webgis_raekker(
        debug=debug
    )

    queue_id = _hent_queue_id(
        workqueue
    )

    antal_tilfoejet = 0
    antal_allerede_i_koe = 0
    antal_under_minimum = 0
    sete_referencer: set[str] = set()

    for raekke_nummer, raekke in enumerate(
        raekker,
        start=1,
    ):
        try:
            caseid = _hent_caseid(
                raekke
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Springer WebGIS-række %s over: %s",
                raekke_nummer,
                exc,
            )
            continue

        if caseid < MINIMUM_CASEID:
            antal_under_minimum += 1
            continue

        item_reference = str(
            caseid
        )

        if item_reference in sete_referencer:
            continue

        sete_referencer.add(
            item_reference
        )

        if is_item_in_queue(
            queue_id=queue_id,
            item_reference=item_reference,
        ):
            antal_allerede_i_koe += 1
            continue

        data_json: dict[str, Any] = {}

        update_item_data(
            data_json,
            box_updates={
                "caseid": item_reference,
                "webgis": dict(
                    raekke
                ),
            },
            update=False,
        )

        workqueue.add_item(
            data=data_json,
            reference=item_reference,
        )

        antal_tilfoejet += 1

    logger.info(
        "Resultat | Hentet: %s | Tilføjet: %s | "
        "Allerede i kø: %s | Under minimum: %s",
        _format_antal(len(raekker)),
        _format_antal(antal_tilfoejet),
        _format_antal(antal_allerede_i_koe),
        _format_antal(antal_under_minimum),
    )


# ------------------------------------------------------------
# WebGIS-flow
# ------------------------------------------------------------

async def _hent_webgis_raekker(
    *,
    debug: bool,
) -> list[dict[str, Any]]:
    """
    Kører samme spatialanalyseflow som integrationstesten.

    Output:
        Liste med konkrete WebGIS-rækker.
    """

    http_credentials = (
        _hent_webgis_http_credentials()
    )

    session = BrowserSession(
        headless=HEADLESS,
        debug=(
            DEBUG_BROWSER
            or debug
        ),
        http_credentials=http_credentials,
    )

    page: Page | None = None

    try:
        await session.start()

        page = await session.new_page()

        session_id_future = (
            _opret_session_id_listener(
                page
            )
        )

        await _launch_webgis(
            page
        )

        _, session_id = await asyncio.gather(
            _vent_paa_jsessionid(
                page
            ),
            _vent_paa_session_id(
                session_id_future
            ),
        )

        resultat = await spatialanalyze(
            page=page,
            session_id=session_id,
            datasource=DATASOURCE,
            min_x_wc=MIN_X_WC,
            min_y_wc=MIN_Y_WC,
            max_x_wc=MAX_X_WC,
            max_y_wc=MAX_Y_WC,
            maxrows=MAX_ROWS,
            command=COMMAND,
            spatialexpression=SPATIAL_EXPRESSION,
            jsontypes=JSON_TYPES,
            outputformat=OUTPUT_FORMAT,
            error_content_type=ERROR_CONTENT_TYPE,
            profile=PROFILE,
        )

        if not isinstance(
            resultat,
            dict,
        ) or not resultat:
            raise RuntimeError(
                "WebGIS spatialanalyze returnerede ikke "
                "en udfyldt dictionary."
            )

        raekker = await hent_spatialanalyze_raekker(
            page=page,
            session_id=session_id,
            maxrows=MAX_ROWS,
            jsontypes=JSON_TYPES,
            outputformat=OUTPUT_FORMAT,
            error_content_type=ERROR_CONTENT_TYPE,
        )

        if not isinstance(
            raekker,
            list,
        ):
            raise TypeError(
                "hent_spatialanalyze_raekker() "
                "returnerede ikke en liste."
            )

        for index, raekke in enumerate(
            raekker,
            start=1,
        ):
            if not isinstance(
                raekke,
                dict,
            ):
                raise TypeError(
                    "WebGIS-række "
                    f"{index} er ikke en dictionary."
                )

        return raekker

    except Exception:
        logger.exception(
            "Hentning af WebGIS spatialanalyse fejlede."
        )

        if (
            page is not None
            and not page.is_closed()
        ):
            try:
                await session.screenshot(
                    page,
                    "FEJL_populate_queue_webgis",
                    always=True,
                )
            except Exception:
                logger.exception(
                    "Fejlscreenshot kunne ikke gemmes."
                )

        raise

    finally:
        if (
            page is not None
            and not page.is_closed()
        ):
            try:
                await session.close_page(
                    page
                )
            except Exception:
                logger.exception(
                    "WebGIS-siden kunne ikke lukkes korrekt."
                )

        try:
            await session.close()
        except Exception:
            logger.exception(
                "BrowserSession kunne ikke lukkes korrekt."
            )


# ------------------------------------------------------------
# Credentials og WebGIS-session
# ------------------------------------------------------------

def _hent_webgis_http_credentials() -> dict[str, str]:
    """
    Henter WebGIS-login fra Automation Server.

    Output:
        Dictionary med username og password til BrowserSession.
    """

    AutomationServer.from_environment()

    credential = Credential.get_credential(
        WEBGIS_CREDENTIAL_NAME
    )

    username = str(
        credential.username or ""
    ).strip()

    password = str(
        credential.password or ""
    )

    if not username:
        raise ValueError(
            f"Credentialen {WEBGIS_CREDENTIAL_NAME!r} "
            "mangler username."
        )

    if not password:
        raise ValueError(
            f"Credentialen {WEBGIS_CREDENTIAL_NAME!r} "
            "mangler password."
        )

    return {
        "username": username,
        "password": password,
    }


def _opret_session_id_listener(
    page: Page,
) -> asyncio.Future[str]:
    """
    Opretter request-listeneren før navigation til WebGIS.

    Output:
        Future, som udfyldes med WebGIS Session-Id.
    """

    event_loop = asyncio.get_running_loop()

    session_id_future: asyncio.Future[str] = (
        event_loop.create_future()
    )

    async def kontroller_request(
        request: Request,
    ) -> None:
        if session_id_future.done():
            return

        request_hostname = (
            urlparse(
                request.url
            ).hostname
            or ""
        ).casefold()

        if (
            request_hostname
            != WEBGIS_HOSTNAME.casefold()
        ):
            return

        try:
            headers = await request.all_headers()
        except Exception as exc:
            logger.debug(
                "Headers kunne ikke læses fra WebGIS-request: %s",
                exc,
            )
            return

        session_id = str(
            headers.get(
                SESSION_ID_HEADER_NAME,
                "",
            )
            or ""
        ).strip()

        if (
            not session_id
            or session_id_future.done()
        ):
            return

        logger.info(
            "WebGIS Session-Id er fundet automatisk."
        )

        session_id_future.set_result(
            session_id
        )

    def log_listenerfejl(
        completed_task: asyncio.Task[None],
    ) -> None:
        if completed_task.cancelled():
            return

        try:
            exception = (
                completed_task.exception()
            )
        except asyncio.CancelledError:
            return

        if exception is not None:
            logger.error(
                "Fejl i Session-Id-listeneren: %s",
                exception,
                exc_info=(
                    type(exception),
                    exception,
                    exception.__traceback__,
                ),
            )

    def ved_request(
        request: Request,
    ) -> None:
        task = asyncio.create_task(
            kontroller_request(
                request
            )
        )

        task.add_done_callback(
            log_listenerfejl
        )

    page.on(
        "request",
        ved_request,
    )

    return session_id_future


async def _vent_paa_session_id(
    session_id_future: asyncio.Future[str],
) -> str:
    """
    Venter på Session-Id fra WebGIS.

    Output:
        Den fundne Session-Id.
    """

    try:
        session_id = await asyncio.wait_for(
            asyncio.shield(
                session_id_future
            ),
            timeout=SESSION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            "WebGIS blev åbnet, men Session-Id "
            "blev ikke fundet inden for "
            f"{SESSION_TIMEOUT_SECONDS} sekunder."
        ) from exc

    session_id = str(
        session_id or ""
    ).strip()

    if not session_id:
        raise RuntimeError(
            "WebGIS-listeneren returnerede "
            "en tom Session-Id."
        )

    return session_id


async def _vent_paa_jsessionid(
    page: Page,
) -> None:
    """Venter på WebGIS-sessionens JSESSIONID-cookie."""

    event_loop = asyncio.get_running_loop()

    deadline = (
        event_loop.time()
        + SESSION_TIMEOUT_SECONDS
    )

    while event_loop.time() < deadline:
        cookies = await page.context.cookies(
            [
                WEBGIS_BASE_URL,
            ]
        )

        har_jsessionid = any(
            (
                str(
                    cookie.get(
                        "name",
                        "",
                    )
                ).casefold()
                == JSESSIONID_COOKIE_NAME.casefold()
            )
            and bool(
                cookie.get(
                    "value"
                )
            )
            for cookie in cookies
        )

        if har_jsessionid:
            logger.info(
                "WebGIS-sessionens JSESSIONID er fundet."
            )
            return

        await asyncio.sleep(
            0.5
        )

    raise RuntimeError(
        "WebGIS blev åbnet, men JSESSIONID "
        "blev ikke fundet inden for "
        f"{SESSION_TIMEOUT_SECONDS} sekunder."
    )


async def _launch_webgis(
    page: Page,
) -> None:
    """Åbner WebGIS i den eksisterende browsersession."""

    logger.info(
        "Åbner WebGIS."
    )

    response = await page.goto(
        WEBGIS_URL,
        wait_until="domcontentloaded",
        timeout=NAVIGATION_TIMEOUT_MS,
    )

    if response is None:
        raise RuntimeError(
            "Navigationen til WebGIS returnerede "
            "ikke et HTTP-svar."
        )

    if not response.ok:
        raise RuntimeError(
            "WebGIS afviste navigationen med "
            f"HTTP-status {response.status}."
        )


# ------------------------------------------------------------
# Række- og køvalidering
# ------------------------------------------------------------

def _hent_caseid(
    raekke: dict[str, Any],
) -> int:
    """
    Henter caseid uden forskel på store og små bogstaver.

    Output:
        caseid som positivt heltal.
    """

    if not isinstance(
        raekke,
        dict,
    ):
        raise TypeError(
            "WebGIS-rækken skal være en dictionary."
        )

    caseid_value: Any = None

    for key, value in raekke.items():
        if (
            str(key).strip().casefold()
            == "caseid"
        ):
            caseid_value = value
            break

    if caseid_value is None:
        raise ValueError(
            "WebGIS-rækken mangler feltet 'caseid'."
        )

    if isinstance(
        caseid_value,
        bool,
    ):
        raise TypeError(
            "caseid må ikke være boolsk."
        )

    caseid_text = str(
        caseid_value
    ).strip()

    if not caseid_text.isdigit():
        raise ValueError(
            f"caseid {caseid_value!r} "
            "er ikke et positivt heltal."
        )

    caseid = int(
        caseid_text
    )

    if caseid <= 0:
        raise ValueError(
            "caseid skal være større end 0."
        )

    return caseid


def _format_antal(value: int) -> str:
    """Returnerer et heltal med dansk tusindtalsseparator."""

    return f"{value:,}".replace(",", ".")


def _hent_queue_id(
    workqueue: Workqueue,
) -> int:
    """
    Henter og validerer det tekniske queue-id.

    Output:
        Positivt queue-id som heltal.
    """

    queue_id = getattr(
        workqueue,
        "id",
        None,
    )

    if (
        queue_id is None
        or isinstance(
            queue_id,
            bool,
        )
    ):
        raise ValueError(
            "workqueue mangler et gyldigt id."
        )

    try:
        validated_queue_id = int(
            queue_id
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "workqueue.id skal være et heltal."
        ) from exc

    if validated_queue_id <= 0:
        raise ValueError(
            "workqueue.id skal være større end 0."
        )

    return validated_queue_id


def _valider_workqueue(
    workqueue: Workqueue,
) -> None:
    """Kontrollerer Workqueue-objektet fra main.py."""

    if workqueue is None:
        raise ValueError(
            "workqueue skal angives."
        )

    if not callable(
        getattr(
            workqueue,
            "add_item",
            None,
        )
    ):
        raise TypeError(
            "workqueue skal have metoden add_item()."
        )

    _hent_queue_id(
        workqueue
    )


__all__ = [
    "populate_queue",
]
