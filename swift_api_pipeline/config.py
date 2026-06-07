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


# QA Forms configuration (TS13+) — single source of truth
QA_FORMS = {
    "qa_ts13": {
        "form_id": "-NH1hUPkaKtPdd7BK9cb",
        "table_name": "raw_form_qa_ts13",
        "display_name": "QA Form TS13"
    },
    "qa_ts14": {
        "form_id": "-NXCg4vTDNVykN8ioMYp",
        "table_name": "raw_form_qa_ts14",
        "display_name": "QA Form TS14"
    },
    "qa_ts15": {
        "form_id": "-Np6o9OCL4RWIJq68HJe",
        "table_name": "raw_form_qa_ts15",
        "display_name": "QA Form TS15"
    },
    "qa_ts16": {
        "form_id": "-O9ACLN3je1w7oEoG5hY",
        "table_name": "raw_form_qa_ts16",
        "display_name": "QA Form TS16"
    },
    "qa_ts17": {
        "form_id": "-ONMD-cGBq-_3r9ybaAq",
        "table_name": "raw_form_qa_ts17",
        "display_name": "QA Form TS17"
    },
    "qa_ts18": {
        "form_id": "-O_J2hPlryTezP9RhujA",
        "table_name": "raw_form_qa_ts18",
        "display_name": "QA Form TS18"
    },
    "qa_ts19": {
        "form_id": "-Omun_NWXeQE1tEhSPXf",
        "table_name": "raw_form_qa_ts19",
        "display_name": "QA Form TS19"
    },
}


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
