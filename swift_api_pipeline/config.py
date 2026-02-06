import os
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
