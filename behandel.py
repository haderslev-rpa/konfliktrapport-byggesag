from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from automation_server_client import WorkItemError
from playwright.async_api import Page

from configuration import (
    ACADRE_AFSLUTTET_STATUS,
    ACADRE_DOKUMENT_TITEL,
    ACADRE_DOKUMENTTYPE,
    ACADRE_KATEGORI,
    ACADRE_PAGE_SIZE,
    ACADRE_PUBLIC_ACCESS,
    ACADRE_STATUS,
    ACADRE_TIMEOUT_MS,
    KONFLIKTRAPPORT_FILNAVN,
    KONFLIKTRAPPORT_STI,
    LANDZONESAG_TEKST,
    MANUELLE_BUILDINGREGULATIONER,
    RUNTIME_MAPPE,
    WEBGIS_RAPPORT_TIMEOUT_MS,
    WEBGIS_TEMA_BYGGESAG,
    WEBGIS_TEMA_LANDZONESAG,
    WEBGIS_TIMEOUT_MS,
    WEBGIS_URL,
)

from q_acadre.functionality.dokumenter import (
    upload_document_to_case,
)
from q_acadre.functionality.launch import launch_acadre
from q_acadre.functionality.sager import (
    get_case_details_by_id,
    search_case_by_number,
)
from q_haderslev_vbo.automation_server.ats_find_state import (
    find_state,
)
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_outlook_api.functionality.mail_api import send_mail
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)
from q_webgis.functionality.rapport import (
    opret_webgis_rapport,
)


logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# STATUS OG AFSLUTTENDE CHECKPOINTS
# ------------------------------------------------------------

STATUS_MANUEL = "Manuel"

# Mailopsætning ligger bevidst i behandel.py.
MANUEL_MAIL_AFSENDER = "dirxoas@haderslev.dk"
MANUEL_MAIL_BYGGESAG = "Byg@haderslev.dk"
MANUEL_MAIL_LANDZONESAG = "Plan@haderslev.dk"
MANUEL_MAIL_EMNE = "Robot: Konfliktrapporten fejl"

STATE_MANUEL_BUILDINGREGULATION_PREFIX = (
    "0.5 Buildingregulation skal ikke behandles"
)

STATE_KONFLIKTRAPPORT_IMPORTERET = (
    "1.0 Importeret Konfliktrapport på Acadre sag"
)
STATE_MANUEL_ACADRE_AFSLUTTET = (
    "0.6 Acadre-sagen er afsluttet"
)
STATE_MANUEL_ACADRE_IKKE_FUNDET = (
    "0.7 Acadre-sagen blev ikke fundet"
)
STATE_MANUEL_WEBGIS_RAPPORT_FEJLET = (
    "0.8 WebGIS kunne ikke generere konfliktrapport"
)


# ------------------------------------------------------------
# OFFENTLIG BEHANDLINGSFUNKTION
# ------------------------------------------------------------

async def behandel_page(
    item: Any,
    session: BrowserSession,
    page: Page,
    webgis_session: BrowserSession,
) -> dict[str, Any] | None:
    """
    Behandler ét byggesags-item.

    Acadre kontrolleres før manuel buildingregulation, WebGIS og PDF.
    Er sagen afsluttet eller ikke fundet, sendes itemet til manuel
    behandling, og konfliktrapporten oprettes ikke.
    """
    data = item.data or {}
    box = _ensure_box(data)

    if find_state(
        data,
        STATE_KONFLIKTRAPPORT_IMPORTERET,
    ):
        logger.info(
            "Konfliktrapporten er allerede importeret for item %s. "
            "Robotten udfører ikke handlingen igen.",
            item.reference,
        )
        return None

    webgis_data = _hent_webgis_data(box)
    buildingregulation = _paakraevet_tekst(
        webgis_data.get("buildingregulation"),
        "box.webgis.buildingregulation",
    )
    sagsnummer = _paakraevet_tekst(
        webgis_data.get("esdhdisplayid"),
        "box.webgis.esdhdisplayid",
    )
    teknisk_case_id = _valgfrit_positivt_heltal_som_tekst(
        webgis_data.get("esdhid")
    )

    await launch_acadre(
        page=page,
        session=session,
    )

    if teknisk_case_id is None:
        logger.info(
            "box.webgis.esdhid mangler. Søger i Acadre efter "
            "sagsnummer %s som fallback.",
            sagsnummer,
        )
        search_result = await search_case_by_number(
            page=page,
            case_number=sagsnummer,
            page_size=ACADRE_PAGE_SIZE,
            page_index=0,
            timeout_ms=ACADRE_TIMEOUT_MS,
        )

        try:
            fallback_sag = _hent_entydigt_acadre_match(
                search_result=search_result,
                sagsnummer=sagsnummer,
            )
        except WorkItemError:
            logger.warning(
                "Acadre-sag %s kunne ikke findes entydigt, "
                "mens box.webgis.esdhid var tom.",
                sagsnummer,
            )
            return await _afslut_som_manuel_acadre_sag(
                data=data,
                item=item,
                buildingregulation=buildingregulation,
                sagsnummer=sagsnummer,
                state=STATE_MANUEL_ACADRE_IKKE_FUNDET,
                aarsag=(
                    "Konfliktrapporten kunne ikke journaliseres i "
                    "Acadre, fordi sagen ikke kunne findes entydigt."
                ),
            )

        teknisk_case_id = _positivt_heltal_som_tekst(
            fallback_sag.get("case_id"),
            "Acadre-søgningens case_id",
        )
        update_item_data(
            data,
            item=item,
            box_updates={
                "acadre_case_id_fundet_via_sagsnummer": (
                    teknisk_case_id
                ),
                "acadre_case_number_fallback": sagsnummer,
            },
        )
        logger.info(
            "Acadre-sag %s blev fundet via sagsnummer med "
            "caseId %s.",
            sagsnummer,
            teknisk_case_id,
        )

    try:
        case_details = await get_case_details_by_id(
            page=page,
            case_id=teknisk_case_id,
            timeout_ms=ACADRE_TIMEOUT_MS,
        )
    except RuntimeError as exc:
        if not _er_acadre_sag_ikke_fundet(exc):
            raise

        return await _afslut_som_manuel_acadre_sag(
            data=data,
            item=item,
            buildingregulation=buildingregulation,
            sagsnummer=sagsnummer,
            state=STATE_MANUEL_ACADRE_IKKE_FUNDET,
            aarsag=(
                "Konfliktrapporten kunne ikke journaliseres i "
                "Acadre, fordi sagen ikke findes."
            ),
        )

    if not isinstance(case_details, dict):
        raise WorkItemError(
            "get_case_details_by_id returnerede ikke en dictionary."
        )

    case_status = _paakraevet_tekst(
        case_details.get("case_status"),
        "Acadre-sagens case_status",
    )

    logger.info(
        "Acadre-sag %s har status %s.",
        sagsnummer,
        case_status,
    )

    if (
        _normaliser_tekst(case_status)
        == _normaliser_tekst(ACADRE_AFSLUTTET_STATUS)
    ):
        return await _afslut_som_manuel_acadre_sag(
            data=data,
            item=item,
            buildingregulation=buildingregulation,
            sagsnummer=sagsnummer,
            state=STATE_MANUEL_ACADRE_AFSLUTTET,
            aarsag=(
                "Konfliktrapporten blev ikke oprettet, fordi "
                "Acadre-sagen er afsluttet."
            ),
        )

    manuel_vaerdi = _find_manuel_buildingregulation(
        buildingregulation
    )

    if manuel_vaerdi is not None:
        aarsag = (
            "Buildingregulation skal ikke behandles: "
            f"{manuel_vaerdi}"
        )
        manuel_state = (
            f"{STATE_MANUEL_BUILDINGREGULATION_PREFIX}: "
            f"{manuel_vaerdi}"
        )

        if not find_state(data, manuel_state):
            update_item_data(
                data,
                item=item,
                status=STATUS_MANUEL,
                status_code=aarsag,
                state=manuel_state,
            )

        return {
            "manual": True,
            "status": STATUS_MANUEL,
            "status_code": aarsag,
            "state": manuel_state,
            "message": aarsag,
        }

    rapport_sti = Path(KONFLIKTRAPPORT_STI).resolve()
    _ryd_gammel_konfliktrapport(rapport_sti)

    try:
        shape_wkt = _udtraek_polygon_wkt(
            webgis_data.get("shape_wkt")
        )
        tema = _vaelg_webgis_tema(buildingregulation)

        try:
            rapport_sti = await _hent_konfliktrapport_fra_webgis(
                webgis_session=webgis_session,
                wkt=shape_wkt,
                tema=tema,
                rapport_sti=rapport_sti,
            )
        except RuntimeError as error:
            if not _er_webgis_rapport_timeout(error):
                raise

            aarsag = (
                "WebGIS kunne ikke generere konfliktrapporten "
                "efter to komplette forsøg i den eksisterende "
                "browserproces. Sagen skal behandles manuelt."
            )

            logger.error(
                "%s Sagsnummer=%s. Sidste WebGIS-fejl: %s",
                aarsag,
                sagsnummer,
                error,
            )

            return await _afslut_som_manuel_acadre_sag(
                data=data,
                item=item,
                buildingregulation=buildingregulation,
                sagsnummer=sagsnummer,
                state=STATE_MANUEL_WEBGIS_RAPPORT_FEJLET,
                aarsag=aarsag,
            )

        _valider_pdf(rapport_sti)

        logger.info(
            "Søger efter Acadre-sagsnummer %s.",
            sagsnummer,
        )
        search_result = await search_case_by_number(
            page=page,
            case_number=sagsnummer,
            page_size=ACADRE_PAGE_SIZE,
            page_index=0,
            timeout_ms=ACADRE_TIMEOUT_MS,
        )
        sag = _hent_entydigt_acadre_match(
            search_result=search_result,
            sagsnummer=sagsnummer,
        )
        fundet_case_id = _positivt_heltal_som_tekst(
            sag.get("case_id"),
            "Acadre-søgningens case_id",
        )

        if fundet_case_id != teknisk_case_id:
            raise WorkItemError(
                "Acadre-søgningen fandt en anden sag end "
                "box.webgis.esdhid. "
                f"Forventet caseId={teknisk_case_id}, "
                f"fundet caseId={fundet_case_id}."
            )

        fundet_sagsnummer = _paakraevet_tekst(
            sag.get("case_number"),
            "Acadre-søgningens case_number",
        )
        if (
            _normaliser_tekst(fundet_sagsnummer)
            != _normaliser_tekst(sagsnummer)
        ):
            raise WorkItemError(
                "Acadre-søgningens resultat har et andet "
                "sagsnummer end det ønskede. "
                f"Forventet={sagsnummer!r}, "
                f"fundet={fundet_sagsnummer!r}."
            )

        values = sag.get("values")
        if not isinstance(values, dict):
            raise WorkItemError(
                "search_case_by_number returnerede ikke "
                "sagens values som en dictionary."
            )

        sagsansvarlig_initialer = _paakraevet_tekst(
            values.get("case-responsible-user"),
            "Acadre-søgeresultatets case-responsible-user",
        )
        fields = case_details.get("fields")
        if not isinstance(fields, dict):
            raise WorkItemError(
                "get_case_details_by_id returnerede ikke "
                "fields som en dictionary."
            )

        sagstitel = _paakraevet_tekst(
            case_details.get("title"),
            "Acadre-sagens title",
        )
        sagsansvarlig_navn = _paakraevet_tekst(
            fields.get("Sagsansvarlig"),
            "Acadre-feltet Sagsansvarlig",
        )
        sagsbehandler = _sammensaet_sagsbehandler(
            initialer=sagsansvarlig_initialer,
            navn=sagsansvarlig_navn,
        )

        logger.info(
            "Sagsbehandler til dokumentimport: %s.",
            sagsbehandler,
        )

        dagsdato = date.today().strftime("%d-%m-%Y")
        upload_resultat = await upload_document_to_case(
            page=page,
            session=session,
            case_id=fundet_case_id,
            file_path=rapport_sti,
            title=ACADRE_DOKUMENT_TITEL,
            document_type=ACADRE_DOKUMENTTYPE,
            category=ACADRE_KATEGORI,
            caseworker=sagsbehandler,
            content_description=sagstitel,
            public_access=ACADRE_PUBLIC_ACCESS,
            status=ACADRE_STATUS,
            document_date=dagsdato,
            journal_date=dagsdato,
            timeout_ms=ACADRE_TIMEOUT_MS,
        )
        if not isinstance(upload_resultat, dict):
            raise WorkItemError(
                "upload_document_to_case returnerede ikke "
                "en dictionary."
            )

        document_id = _paakraevet_tekst(
            upload_resultat.get("document_id"),
            "Acadre-uploadens document_id",
        )
        case_url = _paakraevet_tekst(
            sag.get("case_url"),
            "Acadre-søgningens case_url",
        )

        _set_afsluttende_checkpoint(
            data=data,
            item=item,
            state=STATE_KONFLIKTRAPPORT_IMPORTERET,
            box_updates={
                "konfliktrapport_filnavn": rapport_sti.name,
                "konfliktrapport_tema": tema,
                "acadre_case_id": fundet_case_id,
                "acadre_case_number": fundet_sagsnummer,
                "acadre_case_url": case_url,
                "acadre_case_status": case_status,
                "acadre_caseworker_initialer": sagsansvarlig_initialer,
                "acadre_caseworker_navn": sagsansvarlig_navn,
                "acadre_caseworker": sagsbehandler,
                "acadre_document_id": document_id,
                "acadre_document_url": upload_resultat.get(
                    "document_url"
                ),
            },
        )
        return None
    finally:
        _ryd_gammel_konfliktrapport(rapport_sti)


async def _afslut_som_manuel_acadre_sag(
    *,
    data: dict[str, Any],
    item: Any,
    buildingregulation: str,
    sagsnummer: str,
    state: str,
    aarsag: str,
) -> dict[str, Any]:
    """Sender én mail og gemmer et afsluttende manuelt checkpoint."""
    if not find_state(data, state):
        modtager = _vaelg_manuel_mailmodtager(buildingregulation)
        await _send_manuel_mail(
            modtager=modtager,
            sagsnummer=sagsnummer,
            aarsag=aarsag,
        )
        update_item_data(
            data,
            item=item,
            status=STATUS_MANUEL,
            status_code=aarsag,
            state=state,
            box_updates={
                "manuel_mail_modtager": modtager,
                "manuel_acadre_sagsnummer": sagsnummer,
                "manuel_aarsag": aarsag,
            },
        )

    return {
        "manual": True,
        "status": STATUS_MANUEL,
        "status_code": aarsag,
        "state": state,
        "message": aarsag,
    }


def _vaelg_manuel_mailmodtager(buildingregulation: str) -> str:
    """Returnerer Plan for landzonesager, ellers Byg."""
    if (
        _normaliser_tekst(LANDZONESAG_TEKST)
        in _normaliser_tekst(buildingregulation)
    ):
        return MANUEL_MAIL_LANDZONESAG
    return MANUEL_MAIL_BYGGESAG


async def _send_manuel_mail(
    *,
    modtager: str,
    sagsnummer: str,
    aarsag: str,
) -> None:
    """
    Sender mail via q-outlook-api uden PDF-vedhæftning.

    Output:
        Funktionen returnerer None, når mailen er sendt.
        Fejl fra Microsoft Graph sendes videre til workerens
        almindelige fejlhåndtering.
    """
    afsender = _paakraevet_tekst(
        MANUEL_MAIL_AFSENDER,
        "MANUEL_MAIL_AFSENDER",
    )

    valideret_modtager = _paakraevet_tekst(
        modtager,
        "modtager",
    )

    valideret_sagsnummer = _paakraevet_tekst(
        sagsnummer,
        "sagsnummer",
    )

    valideret_aarsag = _paakraevet_tekst(
        aarsag,
        "aarsag",
    )

    mail = {
        "subject": MANUEL_MAIL_EMNE,
        "body": (
            f"{valideret_aarsag}<br><br>"
            f"Acadre-sag: {valideret_sagsnummer}"
        ),
        "content_type": "HTML",
        "to": valideret_modtager,
        "attachments": [],
        "save_to_sent_items": True,
    }

    logger.info(
        "Sender manuel mail om Acadre-sag %s "
        "fra %s til %s.",
        valideret_sagsnummer,
        afsender,
        valideret_modtager,
    )

    mail_sendt = await asyncio.to_thread(
        send_mail,
        afsender,
        mail,
    )

    if mail_sendt is not True:
        raise RuntimeError(
            "q-outlook-api bekræftede ikke, "
            "at den manuelle mail blev sendt."
        )

    logger.info(
        "Manuel mail om Acadre-sag %s er sendt til %s.",
        valideret_sagsnummer,
        valideret_modtager,
    )

def _er_acadre_sag_ikke_fundet(exc: RuntimeError) -> bool:
    """Genkender et eksplicit HTTP 404-svar fra Acadre."""
    fejltekst = _normaliser_tekst(str(exc))
    return (
        "http-status: 404" in fejltekst
        or "http-status 404" in fejltekst
    )

# ------------------------------------------------------------
# WEBGIS-RAPPORT
# ------------------------------------------------------------

async def _hent_konfliktrapport_fra_webgis(
    *,
    webgis_session: BrowserSession,
    wkt: str,
    tema: str,
    rapport_sti: Path,
) -> Path:
    """
    Henter WebGIS-rapporten gennem browserprocessen fra main.py.

    Chromium startes ikke igen for hvert item. Hvert forsøg får kun
    en ny Page i den eksisterende BrowserSession.

    BrowserContext oprettes automatisk af BrowserSession.new_page(),
    første gang funktionen kaldes.

    Output:
        Path til den gemte og validerbare PDF-fil.
    """
    if webgis_session is None:
        raise ValueError(
            "webgis_session skal angives."
        )

    # start() opretter browseren.
    # Contexten bliver først oprettet af new_page().
    if webgis_session.browser is None:
        raise RuntimeError(
            "WebGIS BrowserSession er ikke startet. "
            "Sessionen skal startes én gang i main.py."
        )

    samlet_antal_forsoeg = 2
    sidste_fejl: RuntimeError | None = None

    for forsoeg in range(
        1,
        samlet_antal_forsoeg + 1,
    ):
        _ryd_gammel_konfliktrapport(
            rapport_sti
        )

        webgis_page: Page | None = None

        logger.info(
            "Starter WebGIS-forsøg %s af %s i den "
            "eksisterende browserproces.",
            forsoeg,
            samlet_antal_forsoeg,
        )

        try:
            # Første kald opretter automatisk BrowserContext.
            # Senere kald genbruger samme context.
            webgis_page = (
                await webgis_session.new_page()
            )

            response = await webgis_page.goto(
                WEBGIS_URL,
                wait_until="domcontentloaded",
                timeout=WEBGIS_TIMEOUT_MS,
            )

            if (
                response is None
                or not response.ok
            ):
                status = (
                    response.status
                    if response is not None
                    else "intet svar"
                )

                raise RuntimeError(
                    "WebGIS kunne ikke åbnes. "
                    f"HTTP-status: {status}."
                )

            rapport_sti.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            gemt_sti = await opret_webgis_rapport(
                page=webgis_page,
                session=webgis_session,
                wkt=wkt,
                tema=tema,
                rapportmappe=rapport_sti.parent,
                filnavn=KONFLIKTRAPPORT_FILNAVN,
                vaelg_jordstykker_byggesag=True,
                timeout_ms=WEBGIS_TIMEOUT_MS,
                rapport_timeout_ms=(
                    WEBGIS_RAPPORT_TIMEOUT_MS
                ),
            )

            gemt_sti = Path(
                gemt_sti
            ).resolve()

            if gemt_sti != rapport_sti:
                if rapport_sti.exists():
                    rapport_sti.unlink()

                gemt_sti.replace(
                    rapport_sti
                )

            logger.info(
                "WebGIS-forsøg %s af %s lykkedes.",
                forsoeg,
                samlet_antal_forsoeg,
            )

            return rapport_sti

        except RuntimeError as error:
            if not _er_webgis_rapport_timeout(
                error
            ):
                raise

            sidste_fejl = error

            logger.warning(
                "WebGIS-forsøg %s af %s leverede "
                "ikke en PDF. Den aktuelle side lukkes. "
                "Fejl: %s",
                forsoeg,
                samlet_antal_forsoeg,
                error,
            )

            if forsoeg < samlet_antal_forsoeg:
                logger.info(
                    "Starter et sidste WebGIS-forsøg med "
                    "en ny side i den eksisterende "
                    "browserproces."
                )

        finally:
            if (
                webgis_page is not None
                and not webgis_page.is_closed()
            ):
                await webgis_session.close_page(
                    webgis_page
                )

    raise RuntimeError(
        "WebGIS leverede ikke en PDF efter to "
        "forsøg i den eksisterende browserproces. "
        f"Sidste fejl: {sidste_fejl}"
    ) from sidste_fejl


def _er_webgis_rapport_timeout(
    error: BaseException,
) -> bool:
    """
    Genkender den målrettede fejl, hvor WebGIS ikke leverer en PDF.

    Andre browser-, login- og programmeringsfejl genkøres ikke og
    sendes ikke automatisk til manuel behandling.
    """
    fejltekst = _normaliser_tekst(str(error))

    return (
        "webgis leverede ikke en pdf" in fejltekst
        or "webgis startede ikke pdf-downloadet" in fejltekst
    )


# ------------------------------------------------------------
# WORK ITEM-DATA
# ------------------------------------------------------------

def _ensure_box(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Sikrer, at item.data indeholder en dictionary i box."""
    box = data.get("box")

    if box is None:
        box = {}
        data["box"] = box

    if not isinstance(box, dict):
        raise WorkItemError(
            "item.data['box'] skal være en dictionary."
        )

    return box


def _hent_webgis_data(
    box: dict[str, Any],
) -> dict[str, Any]:
    """Returnerer den oprindelige WebGIS-række fra box['webgis']."""
    webgis_data = box.get("webgis")

    if not isinstance(webgis_data, dict):
        raise WorkItemError(
            "item.data['box']['webgis'] mangler "
            "eller er ikke en dictionary."
        )

    return webgis_data


# ------------------------------------------------------------
# ACADRE-SØGERESULTAT
# ------------------------------------------------------------

def _hent_entydigt_acadre_match(
    *,
    search_result: dict[str, Any],
    sagsnummer: str,
) -> dict[str, Any]:
    """
    Returnerer præcis ét Acadre-match for sagsnummeret.

    Der vælges aldrig automatisk mellem flere resultater.
    """
    if not isinstance(search_result, dict):
        raise WorkItemError(
            "search_case_by_number returnerede ikke "
            "en dictionary."
        )

    exact_matches = search_result.get(
        "exact_matches"
    )

    if not isinstance(exact_matches, list):
        raise WorkItemError(
            "search_case_by_number returnerede ikke "
            "exact_matches som en liste."
        )

    if not exact_matches:
        result_count = search_result.get(
            "result_count",
            0,
        )

        raise WorkItemError(
            "Acadre fandt ikke et præcist match for "
            f"sagsnummer {sagsnummer!r}. "
            f"Antal øvrige søgeresultater: {result_count}."
        )

    if len(exact_matches) > 1:
        case_ids = [
            match.get("case_id")
            for match in exact_matches
            if isinstance(match, dict)
        ]

        raise WorkItemError(
            "Acadre returnerede flere præcise matches for "
            f"sagsnummer {sagsnummer!r}. "
            f"Fundne caseId-værdier: {case_ids}."
        )

    sag = exact_matches[0]

    if not isinstance(sag, dict):
        raise WorkItemError(
            "Det præcise Acadre-match er ikke "
            "en dictionary."
        )

    return sag


def _sammensaet_sagsbehandler(
    *,
    initialer: str,
    navn: str,
) -> str:
    """
    Samler Acadre-initialer og navn.

    Eksempel:
        dimkro - Marianne Kryger Olsen
    """
    normaliserede_initialer = _paakraevet_tekst(
        initialer,
        "Acadre-sagsbehandlerens initialer",
    )

    normaliseret_navn = _paakraevet_tekst(
        navn,
        "Acadre-sagsbehandlerens navn",
    )

    return (
        f"{normaliserede_initialer} - "
        f"{normaliseret_navn}"
    )


# ------------------------------------------------------------
# FORRETNINGSREGLER
# ------------------------------------------------------------

def _find_manuel_buildingregulation(
    buildingregulation: str,
) -> str | None:
    """Returnerer den første manuelle værdi, som teksten indeholder."""
    normaliseret = _normaliser_tekst(
        buildingregulation
    )

    for vaerdi in MANUELLE_BUILDINGREGULATIONER:
        if _normaliser_tekst(vaerdi) in normaliseret:
            return vaerdi

    return None


def _vaelg_webgis_tema(
    buildingregulation: str,
) -> str:
    """Vælger Landzonesager eller Byggesager til WebGIS."""
    if (
        _normaliser_tekst(LANDZONESAG_TEKST)
        in _normaliser_tekst(buildingregulation)
    ):
        return WEBGIS_TEMA_LANDZONESAG

    return WEBGIS_TEMA_BYGGESAG


# ------------------------------------------------------------
# FIL OG PDF
# ------------------------------------------------------------

def _udtraek_polygon_wkt(
    value: Any,
) -> str:
    """Udtrækker én komplet POLYGON-geometri fra shape_wkt."""
    tekst = _paakraevet_tekst(
        value,
        "box.webgis.shape_wkt",
    )

    match = re.search(
        r"POLYGON\s*\(\(.*?\)\)",
        tekst,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if match is None:
        raise WorkItemError(
            "box.webgis.shape_wkt indeholder ikke en gyldig "
            "POLYGON ((...))-værdi."
        )

    return " ".join(
        match.group(0).split()
    )


def _ryd_gammel_konfliktrapport(
    rapport_sti: Path,
) -> None:
    """Sletter kun processens konkrete konfliktrapport."""
    Path(RUNTIME_MAPPE).mkdir(
        parents=True,
        exist_ok=True,
    )

    if rapport_sti.exists():
        rapport_sti.unlink()

    if rapport_sti.exists():
        raise RuntimeError(
            "Den tidligere rapport kunne ikke slettes: "
            f"{rapport_sti}"
        )


def _valider_pdf(
    rapport_sti: Path,
) -> None:
    """Kontrollerer filnavn, eksistens, størrelse og PDF-signatur."""
    if rapport_sti.name != KONFLIKTRAPPORT_FILNAVN:
        raise RuntimeError(
            "Rapporten har forkert filnavn: "
            f"{rapport_sti.name}"
        )

    if not rapport_sti.is_file():
        raise FileNotFoundError(
            "Konfliktrapporten blev ikke oprettet: "
            f"{rapport_sti}"
        )

    if rapport_sti.stat().st_size == 0:
        raise RuntimeError(
            "Konfliktrapporten er tom."
        )

    with rapport_sti.open("rb") as pdf_file:
        if pdf_file.read(5) != b"%PDF-":
            raise RuntimeError(
                "Den hentede Konfliktrapport.pdf har ikke "
                "en gyldig PDF-signatur."
            )


# ------------------------------------------------------------
# CHECKPOINT
# ------------------------------------------------------------

def _set_afsluttende_checkpoint(
    *,
    data: dict[str, Any],
    item: Any,
    state: str,
    box_updates: dict[str, Any] | None = None,
) -> None:
    """Gemmer et afsluttende checkpoint uden state-dubletter."""
    if find_state(
        data,
        state,
    ):
        return

    update_item_data(
        data,
        item=item,
        box_updates=box_updates,
        state=state,
    )


# ------------------------------------------------------------
# CREDENTIALS OG VALIDERING
# ------------------------------------------------------------

def _valgfrit_positivt_heltal_som_tekst(
    value: Any,
) -> str | None:
    """Returnerer et positivt heltal som tekst eller None ved blank værdi."""
    if value is None:
        return None

    tekst = str(value).strip()
    if not tekst:
        return None

    if isinstance(value, bool) or not tekst.isdigit() or int(tekst) <= 0:
        raise WorkItemError(
            "box.webgis.esdhid skal være tom eller et positivt heltal."
        )

    return tekst


def _positivt_heltal_som_tekst(
    value: Any,
    field_name: str,
) -> str:
    """Returnerer et positivt heltal som tekst."""
    if isinstance(value, bool):
        raise WorkItemError(
            f"{field_name} må ikke være boolsk."
        )

    tekst = str(
        value or ""
    ).strip()

    if (
        not tekst.isdigit()
        or int(tekst) <= 0
    ):
        raise WorkItemError(
            f"{field_name} skal være et positivt heltal."
        )

    return tekst


def _paakraevet_tekst(
    value: Any,
    field_name: str,
) -> str:
    """Returnerer en obligatorisk, trimmet tekstværdi."""
    tekst = str(
        value or ""
    ).strip()

    if not tekst:
        raise WorkItemError(
            f"{field_name} mangler eller er tom."
        )

    return tekst


def _normaliser_tekst(
    value: Any,
) -> str:
    """Normaliserer tekst til robuste sammenligninger."""
    return " ".join(
        str(value or "").split()
    ).casefold()


__all__ = ["behandel_page"]
