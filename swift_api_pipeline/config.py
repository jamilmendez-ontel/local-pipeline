import os
import time
import logging
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# API Configuration
SWIFT_BASE_URL = "https://prod.api.swiftprojects.io"
SWIFT_USERNAME = os.getenv("SWIFT_EMAIL")
SWIFT_PASSWORD = os.getenv("SWIFT_PASSWORD")

# Supabase Configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")  # Use service role key for backend

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


_supabase_client: Client | None = None


def get_supabase_client() -> Client:
    """Return cached Supabase client (singleton)."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def create_supabase_client() -> Client:
    """Create a new Supabase client instance (thread-safe, for parallel pipelines)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def retry_supabase(fn, max_retries=5, description="operation"):
    """Execute a Supabase operation with retry and exponential backoff.

    Args:
        fn: Callable that performs the Supabase operation
        max_retries: Number of attempts before re-raising
        description: Human-readable label for log messages
    """
    _logger = get_logger("retry")
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = min(2 ** attempt, 15)
            _logger.warning(f"{description} failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)


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
}
