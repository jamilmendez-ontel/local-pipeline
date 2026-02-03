# pyinstaller --onefile "SwiftAPI_OrgProjList.py"

import requests
import pandas as pd
import json
from dotenv import load_dotenv
import os
import jwt
from datetime import datetime
import pytz

# === CONFIG ===
base_url = "https://prod.api.swiftprojects.io"
load_dotenv(dotenv_path=os.path.expanduser("~/Downloads/mgmt.env"))
USERNAME = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")


def get_token(username, password):
    url = f"{base_url}/api/auth/token"
    headers = {"Content-Type": "application/json"}
    payload = {
        "grantType": "password",
        "include": ["profile", "firebaseToken"],
        "username": username,
        "password": password,
        "scope": "openid"
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json().get("idToken")


def get_user_id(token):
    decoded = jwt.decode(token.encode(), options={"verify_signature": False})
    return decoded.get("sub").replace("|", ":")


def get_organizations(token, user_id):
    url = f"{base_url}/api/users/{user_id}/organizations?page=0&pageSize=500"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("list", [])


def get_projects(token, org_id):
    url = f"{base_url}/api/organizations/{org_id}/projects?page=0&pageSize=100"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json().get("list", [])


def main():
    print("SwiftAPI | Org + Project List Export v20250603")
    try:
        token = get_token(USERNAME, PASSWORD)
        user_id = get_user_id(token)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Retrieving organizations...")
        orgs = get_organizations(token, user_id)

        if not orgs:
            print("⚠️ No organizations found.")
            return

        org_map = {org["id"]: org.get("name", "") for org in orgs}

        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Retrieved {len(org_map)} organizations.")

        all_projects = []
        for org_id, org_name in org_map.items():
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ➡️  Retrieving projects for: {org_name} ({org_id})")
            try:
                projects = get_projects(token, org_id)
                for proj in projects:
                    metrics = proj.get("metrics", {}).get("asset", {})
                    all_projects.append({
                        "Organization_ID": org_id,
                        "Organization_Name": org_name,
                        "Project_ID": proj.get("id"),
                        "Project_Name": proj.get("name"),
                        "Status": proj.get("status"),
                        "assetProjectCount": metrics.get("assetProjectCount"),
                        "taskCount": metrics.get("taskCount"),
                        "taskPending": metrics.get("taskPending"),
                        "taskApproved": metrics.get("taskApproved"),
                        "taskRejected": metrics.get("taskRejected"),
                        "taskCancelled": metrics.get("taskCancelled"),
                        "taskSubmitted": metrics.get("taskSubmitted"),
                        "taskInProgress": metrics.get("taskInProgress"),
                        "reqCount": metrics.get("reqCount"),
                        "reqPending": metrics.get("reqPending"),
                        "reqApproved": metrics.get("reqApproved"),
                        "reqRejected": metrics.get("reqRejected"),
                        "reqCancelled": metrics.get("reqCancelled"),
                        "reqSubmitted": metrics.get("reqSubmitted"),
                        "reqInProgress": metrics.get("reqInProgress")
                    })
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Failed to retrieve projects for {org_name}: {e}")

        df = pd.DataFrame(all_projects)
        df["retrieved_at"] = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")

        output_file = f"All_Org_Project_List_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        df.to_excel(output_file, index=False)
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Export complete. File saved as '{output_file}'.")

    except requests.HTTPError as err:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ❌ HTTP error occurred: {err.response.status_code} - {err.response.text}")
    except Exception as e:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ❌ Error: {e}")


if __name__ == "__main__":
    main()
