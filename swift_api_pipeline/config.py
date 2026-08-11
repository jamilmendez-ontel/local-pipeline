import os
import logging
import sys
from dotenv import load_dotenv

load_dotenv()

# API Configuration
SWIFT_BASE_URL = "https://prod.api.swiftprojects.io"
SWIFT_USERNAME = os.getenv("SWIFT_EMAIL")
SWIFT_PASSWORD = os.getenv("SWIFT_PASSWORD")

# Pipeline Configuration
PAGE_SIZE = 2000
MAX_RETRIES = 5
TIMEZONE = "America/New_York"

# Schema Configuration
SCHEMA_RAW = "data_raw"
SCHEMA_STAGING = "data_staging"
SCHEMA_REFERENCE = "reference"
SCHEMA_PIPELINE = "pipeline"
# Timer-correction workflow state (OLTP), moved out of data_staging by migration 117.
# Distinct from SCHEMA_STAGING, which still holds the canonical stg_timer_activities
# / stg_timer_activities_clean warehouse facts.
SCHEMA_TIMER = "app_timer"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the pipeline with consistent format."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    root = logging.getLogger("pipeline")
    root.handlers = []
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Get a pipeline logger with the given name."""
    return logging.getLogger(f"pipeline.{name}")


# Re-export database functions (replaces Supabase client code)
from db import get_db, close_db, reconnect_db, retry_db


# ---- Invoicing Form (Quote Automation) ----------------------------------
# Swift forms whose requirement-responses feed the quotation pipeline.
# Add a new DID here to onboard a future invoicing form — no schema change.
INVOICING_FORMS = [
    "-NnoFijdV83f4LCm6Ktr",
    "-OmoXCo93LkiEzTrsVDy",
    "-O_NzUh9FPjw2Sgr3ztS",
    "-ONLRetis86GmTN0_TFm",
]

INVOICING_RAW_TABLE = "raw_invoicing_form"  # in SCHEMA_RAW

# Exact CSV header keys we map to typed staging columns. Everything else a
# form returns lands in stg_invoicing_form.extra_fields (jsonb).
INVOICING_KNOWN_FIELDS = [
    "Project",
    "Site Name",
    "Site ID",
    "Task",
    "Requirement",
    "Requirement Status",
    "Scope of Work (SOW)",
    "Invoice Category",
    "Service Rate",                              # actual key in raw data (not "Service Rate (Price)")
    "LL COP to be handled by Ontel?",
    "Landlord",
    "Landlord (Others)",
    "PMI COP to be handled by Ontel?",
    "RF Mitigation COP to be handled by Ontel?", # older form_dids use this variant
    "RF Mitigation COP to be handled by Ontel",  # newer form_dids drop the trailing "?"
]
