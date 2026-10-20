from __future__ import annotations

from pathlib import Path


# ------------------------------------------------------------
# Queue-populering
# ------------------------------------------------------------

MINIMUM_CASEID = 20261256


# ------------------------------------------------------------
# Browser
# ------------------------------------------------------------

HEADLESS = True
DEBUG_BROWSER = True


# ------------------------------------------------------------
# WebGIS
# ------------------------------------------------------------

WEBGIS_BASE_URL = (
    "https://webgis.intern.udcit.dk"
)

WEBGIS_URL = (
    f"{WEBGIS_BASE_URL}/spatialmap?profile=maxi"
)

WEBGIS_HOSTNAME = (
    "webgis.intern.udcit.dk"
)

WEBGIS_CREDENTIAL_NAME = "DIRXOAS"

WEBGIS_PROFILE = "maxi"


# ------------------------------------------------------------
# WebGIS-session
# ------------------------------------------------------------

JSESSIONID_COOKIE_NAME = "JSESSIONID"

SESSION_ID_HEADER_NAME = "session-id"

NAVIGATION_TIMEOUT_MS = 60_000

SESSION_TIMEOUT_SECONDS = 60


# ------------------------------------------------------------
# WebGIS spatialanalyse
# ------------------------------------------------------------

DATASOURCE = (
    "ds-byggesag_cases_matrikel"
)

COMMAND = "read-spatial"

SPATIAL_EXPRESSION = "this"

JSON_TYPES = "true"

OUTPUT_FORMAT = "json"

ERROR_CONTENT_TYPE = "json"

PROFILE = "maxi"

MAX_ROWS = 20_000


# ------------------------------------------------------------
# WebGIS spatialanalyse-koordinater
# ------------------------------------------------------------

# Verificerede koordinater fra WebGIS-requestet.

MIN_X_WC = 471348.098304

MIN_Y_WC = 6096657.6526848

MAX_X_WC = 569651.901696

MAX_Y_WC = 6149342.3473152


# ------------------------------------------------------------
# WebGIS-rapport
# ------------------------------------------------------------

WEBGIS_TEMA_BYGGESAG = "byggesag"

WEBGIS_TEMA_LANDZONESAG = (
    "landzonesager"
)

WEBGIS_TIMEOUT_MS = 60_000

WEBGIS_RAPPORT_TIMEOUT_MS = 900_000


# ------------------------------------------------------------
# Midlertidige filer
# ------------------------------------------------------------

RUNTIME_MAPPE = Path(
    "runtime_output"
)

KONFLIKTRAPPORT_FILNAVN = (
    "Konfliktrapport.pdf"
)

KONFLIKTRAPPORT_STI = (
    RUNTIME_MAPPE
    / KONFLIKTRAPPORT_FILNAVN
)


# ------------------------------------------------------------
# Acadre
# ------------------------------------------------------------

ACADRE_CREDENTIAL_NAME = "DIRXSDA"

ACADRE_TIMEOUT_MS = 120_000

ACADRE_PAGE_SIZE = 50


# ------------------------------------------------------------
# Acadre-dokumentimport
# ------------------------------------------------------------

ACADRE_DOKUMENT_TITEL = (
    "Konfliktrapport"
)

ACADRE_DOKUMENTTYPE = "N"

ACADRE_KATEGORI = (
    "Andet - Andet"
)

ACADRE_PUBLIC_ACCESS = "1"

ACADRE_STATUS = "J"



# ------------------------------------------------------------
# Acadre-status
# ------------------------------------------------------------
ACADRE_AFSLUTTET_STATUS = "A"

# ------------------------------------------------------------
# Buildingregulation
# ------------------------------------------------------------

MANUELLE_BUILDINGREGULATIONER = (
    "Indeklima beboelse",
    "Boligregulering",
    "Helårsbeboelse sommerhus",
)

LANDZONESAG_TEKST = "Landzonesag"
