import requests
import time
import jwt
from typing import Dict, List, Optional
from datetime import datetime, timezone
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
        """Extract all user priorities with pagination"""
        self._ensure_valid_token()

        all_records = []
        page = 0

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

        while True:
            url = (
                f"{self.base_url}/api/next/user-priorities/_report"
                f"?pageSize={PAGE_SIZE}&page={page}"
                f"&filterOptions=%7B%22status%22%3A%7B%22approved%22%3Afalse%2C%22cancelled%22%3Afalse%7D%7D"
                f"&tz=America/New_York&dateFormat=yyyy-MM-dd%27T%27HH%3Amm%3AssZ"
            )

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    response = requests.get(url, headers=headers, timeout=60)

                    if response.status_code == 200:
                        data = response.json().get("list", [])

                        if not data:
                            logger.info(f" User priorities extraction complete. Total: {len(all_records)} records")
                            return all_records

                        all_records.extend(data)
                        logger.info(f" Page {page}: {len(data)} records (Total: {len(all_records)})")
                        break
                    elif response.status_code == 204:
                        # 204 No Content means no more data
                        logger.info(f" User priorities extraction complete. Total: {len(all_records)} records")
                        return all_records
                    else:
                        logger.info(f" Status {response.status_code} on page {page}")
                        if attempt < MAX_RETRIES:
                            wait = 2 ** (attempt - 1)
                            logger.info(f" Retrying in {wait}s...")
                            time.sleep(wait)
                        else:
                            logger.info(f" Max retries reached on page {page} with status {response.status_code}")
                            return all_records
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
                        return all_records

            page += 1

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

    def extract_all_projects(self) -> List[Dict]:
        """Extract projects for all organizations"""
        orgs = self.extract_organizations()
        all_projects = []

        for org in orgs:
            org_id = org["id"]
            org_name = org.get("name", "Unknown")

            logger.info(f" Extracting projects for: {org_name}")

            try:
                projects = self.extract_projects(org_id)

                # Enrich projects with org context
                for proj in projects:
                    proj["_org_id"] = org_id
                    proj["_org_name"] = org_name

                all_projects.extend(projects)
                logger.info(f"    Retrieved {len(projects)} projects")

            except Exception as e:
                logger.error(f"Failed to extract projects for {org_name}: {e}")

        logger.info(f" Total projects extracted: {len(all_projects)}")
        return all_projects
