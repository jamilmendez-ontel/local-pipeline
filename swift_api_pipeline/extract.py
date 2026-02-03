import requests
import time
import jwt
from typing import Dict, List, Optional
from datetime import datetime
from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD, PAGE_SIZE, MAX_RETRIES

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

        # Extract user_id from token
        decoded = jwt.decode(self.token.encode(), options={"verify_signature": False})
        self.user_id = decoded.get("sub").replace("|", ":")

        print(f"[{datetime.now():%H:%M:%S}] Authenticated as user: {self.user_id}")
        return self.token

    def extract_user_priorities(self) -> List[Dict]:
        """Extract all user priorities with pagination"""
        if not self.token:
            self.authenticate()

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
                    response = requests.get(url, headers=headers)

                    if response.status_code == 200:
                        data = response.json().get("list", [])

                        if not data:
                            print(f"[{datetime.now():%H:%M:%S}] User priorities extraction complete. Total: {len(all_records)} records")
                            return all_records

                        all_records.extend(data)
                        print(f"[{datetime.now():%H:%M:%S}] Page {page}: {len(data)} records (Total: {len(all_records)})")
                        break
                    elif response.status_code == 204:
                        # 204 No Content means no more data
                        print(f"[{datetime.now():%H:%M:%S}] User priorities extraction complete. Total: {len(all_records)} records")
                        return all_records
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] Status {response.status_code} on page {page}")

                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] Attempt {attempt}/{MAX_RETRIES} failed: {e}")

                    if attempt < MAX_RETRIES:
                        wait = 2 ** (attempt - 1)
                        print(f"[{datetime.now():%H:%M:%S}] Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"[{datetime.now():%H:%M:%S}] Max retries reached on page {page}")
                        return all_records

            page += 1

    def extract_organizations(self) -> List[Dict]:
        """Extract all organizations for the authenticated user"""
        if not self.token or not self.user_id:
            self.authenticate()

        url = f"{self.base_url}/api/users/{self.user_id}/organizations?page=0&pageSize=500"
        headers = {"Authorization": f"Bearer {self.token}"}

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        orgs = response.json().get("list", [])
        print(f"[{datetime.now():%H:%M:%S}] Retrieved {len(orgs)} organizations")

        return orgs

    def extract_projects(self, org_id: str) -> List[Dict]:
        """Extract all projects for a given organization"""
        if not self.token:
            self.authenticate()

        url = f"{self.base_url}/api/organizations/{org_id}/projects?page=0&pageSize=100"
        headers = {"Authorization": f"Bearer {self.token}"}

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        return response.json().get("list", [])

    def extract_all_projects(self) -> List[Dict]:
        """Extract projects for all organizations"""
        orgs = self.extract_organizations()
        all_projects = []

        for org in orgs:
            org_id = org["id"]
            org_name = org.get("name", "Unknown")

            print(f"[{datetime.now():%H:%M:%S}] Extracting projects for: {org_name}")

            try:
                projects = self.extract_projects(org_id)

                # Enrich projects with org context
                for proj in projects:
                    proj["_org_id"] = org_id
                    proj["_org_name"] = org_name

                all_projects.extend(projects)
                print(f"[{datetime.now():%H:%M:%S}]    Retrieved {len(projects)} projects")

            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] Failed to extract projects for {org_name}: {e}")

        print(f"[{datetime.now():%H:%M:%S}] Total projects extracted: {len(all_projects)}")
        return all_projects
