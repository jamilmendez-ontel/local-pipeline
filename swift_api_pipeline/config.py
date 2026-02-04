import os
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

def get_supabase_client() -> Client:
    """Initialize Supabase client with service role key"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_KEY)
