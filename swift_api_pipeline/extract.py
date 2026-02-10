import json
import requests
import time
import jwt
from typing import Dict, List, Optional
from datetime import datetime, timezone
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, PAGE_SIZE, MAX_RETRIES, get_logger

logger = get_logger("extract")

class SwiftAPIExtractor:
    def __init__(self):
        self.base_url = SWIFT_BASE_URL
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None

    def authenticate(self) -> str:
        """Obtain authentication token"""
        url = f"{self.base_url}/api/auth/token"
        payload = {
            "grantType": "password",
            "include": ["profile", "firebaseToken"],
            "username": SWIFT_USERNAME,
            "password": SWIFT_PASSWORD,
            "scope": "openid"
        }

        response = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload
        )
        response.raise_for_status()

        self.token = response.json()["idToken"]

        # Extract user_id and expiry from token
        decoded = jwt.decode(self.token.encode(), options={"verify_signature": False})
        self.user_id = decoded.get("sub").replace("|", ":")
        self._token_exp = decoded.get("exp", 0)

        logger.info(f" Authenticated as user: {self.user_id}")
        return self.token

    def _ensure_valid_token(self) -> str:
        """Re-authenticate if token is expired or about to expire (within 5 min)."""
        if not self.token or not hasattr(self, '_token_exp'):
            return self.authenticate()
        # Refresh if token expires within 5 minutes
        if time.time() > (self._token_exp - 300):
            logger.info(f" Token expiring soon, re-authenticating...")
            self.token = None
            return self.authenticate()
        return self.token

    def extract_user_priorities(self) -> List[Dict]:
        """Extract user priorities by status to avoid API 10K row cap.

        Queries one status at a time (pending, in_progress) with a filter that
        excludes all other statuses, then combines results.
        """
        self._ensure_valid_token()

        ALL_STATUSES = ["pending", "in_progress", "submitted", "approved", "rejected", "cancelled"]
        TARGET_STATUSES = ["pending", "in_progress"]

        all_records = []

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

        for target_status in TARGET_STATUSES:
            records = self._extract_priorities_by_status(
                target_status, ALL_STATUSES, headers
            )
            all_records.extend(records)

        logger.info(f" User priorities extraction complete. Total: {len(all_records):,} records")
        return all_records

    def _extract_priorities_by_status(self, target_status, all_statuses, headers):
        """Extract all user priorities for a single status value."""
        # Build filter: exclude every status except the target
        status_filter = {s: False for s in all_statuses if s != target_status}
        filter_options = quote(json.dumps({"status": status_filter}))

        records = []
        page = 0
        logger.info(f" Extracting user priorities with status: {target_status}")

        while True:
            url = (
                f"{self.base_url}/api/next/user-priorities/_report"
                f"?pageSize={PAGE_SIZE}&page={page}"
                f"&filterOptions={filter_options}"
                f"&tz=America/New_York&dateFormat=yyyy-MM-dd%27T%27HH%3Amm%3AssZ"
            )

            data = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = requests.get(url, headers=headers, timeout=60)

                    if response.status_code == 200:
                        data = response.json().get("list", [])
                        break
                    elif response.status_code == 204:
                        data = []
                        break
                    else:
                        logger.info(f" Status {response.status_code} on page {page}")
                        if attempt < MAX_RETRIES:
                            wait = 2 ** (attempt - 1)
                            logger.info(f" Retrying in {wait}s...")
                            time.sleep(wait)
                        else:
                            logger.info(f" Max retries reached on page {page} with status {response.status_code}")
                        continue

                except Exception as e:
                    logger.warning(f"Attempt {attempt}/{MAX_RETRIES} failed: {e}")

                    # Re-authenticate on 401
                    if hasattr(e, 'response') and e.response is not None and e.response.status_code == 401:
                        self.token = None
                        self._ensure_valid_token()
                        headers["Authorization"] = f"Bearer {self.token}"

                    if attempt < MAX_RETRIES:
                        wait = 2 ** (attempt - 1)
                        logger.info(f" Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        logger.info(f" Max retries reached on page {page}")

            # If all retries failed, stop pagination for this status
            if data is None:
                break

            if not data:
                break

            records.extend(data)
            logger.info(f"   [{target_status}] Page {page}: {len(data)} records (subtotal: {len(records):,})")
            page += 1

        logger.info(f"   [{target_status}] Complete: {len(records):,} records")
        return records

    def extract_organizations(self) -> List[Dict]:
        """Extract all organizations for the authenticated user (paginated)"""
        self._ensure_valid_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        all_orgs = []
        page = 0

        while True:
            url = f"{self.base_url}/api/users/{self.user_id}/organizations?page={page}&pageSize=500"
            response = requests.get(url, headers=headers, timeout=60)

            # Retry once on 401
            if response.status_code == 401:
                self.token = None
                self._ensure_valid_token()
                headers["Authorization"] = f"Bearer {self.token}"
                response = requests.get(url, headers=headers, timeout=60)

            response.raise_for_status()
            orgs = response.json().get("list", [])

            if not orgs:
                break

            all_orgs.extend(orgs)
            if len(orgs) < 500:
                break
            page += 1

        logger.info(f"Retrieved {len(all_orgs)} organizations")
        return all_orgs

    def extract_projects(self, org_id: str) -> List[Dict]:
        """Extract all projects for a given organization (paginated)"""
        self._ensure_valid_token()
        headers = {"Authorization": f"Bearer {self.token}"}
        all_projects = []
        page = 0

        while True:
            url = f"{self.base_url}/api/organizations/{org_id}/projects?page={page}&pageSize=100"
            response = requests.get(url, headers=headers, timeout=60)

            # Retry once on 401
            if response.status_code == 401:
                self.token = None
                self._ensure_valid_token()
                headers["Authorization"] = f"Bearer {self.token}"
                response = requests.get(url, headers=headers, timeout=60)

            response.raise_for_status()
            projects = response.json().get("list", [])

            if not projects:
                break

            all_projects.extend(projects)
            if len(projects) < 100:
                break
            page += 1

        return all_projects

    def extract_all_projects(self, max_workers: int = 10) -> List[Dict]:
        """Extract projects for all organizations in parallel"""
        orgs = self.extract_organizations()

        def extract_org_projects(org):
            org_id = org["id"]
            org_name = org.get("name", "Unknown")
            try:
                projects = self.extract_projects(org_id)
                for proj in projects:
                    proj["_org_id"] = org_id
                    proj["_org_name"] = org_name
                logger.info(f"    Retrieved {len(projects)} projects for: {org_name}")
                return projects
            except Exception as e:
                logger.error(f"Failed to extract projects for {org_name}: {e}")
                return []

        all_projects = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(extract_org_projects, org): org for org in orgs}
            for future in as_completed(futures):
                all_projects.extend(future.result())

        logger.info(f" Total projects extracted: {len(all_projects)}")
        return all_projects
