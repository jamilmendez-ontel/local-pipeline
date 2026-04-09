import requests
import pandas as pd
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
from requests.exceptions import RequestException

# Load environment variables
load_dotenv(os.path.expanduser("~/Downloads/mgmt.env"))
USERNAME = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

BASE_URL = "https://prod.api.swiftprojects.io"
API_ENDPOINT = f"{BASE_URL}/api/timer-activities/_report"
MAX_RETRIES = 3


def get_token(username, password):
    url = f"{BASE_URL}/api/auth/token"
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


def fetch_paginated_data(token, project_id, from_date, to_date):
    headers = {"Authorization": f"Bearer {token}"}
    all_data = []
    page = 0

    from_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp() * 1000)
    to_ts = int(datetime.strptime(to_date + " 23:59:00", "%Y-%m-%d %H:%M:%S").timestamp() * 1000)

    while True:
        params = {
            "tz": "America/New_York",
            "dateFormat": "yyyy-MM-dd'T'HH:mm:ssZ",
            "filterOptions": json.dumps({
                "dateRange": {
                    "useAfter": True,
                    "afterDate": from_ts,
                    "useBefore": True,
                    "beforeDate": to_ts
                },
                "project": project_id
            }),
            "pageSize": "1000",
            "page": str(page)
        }

        for attempt in range(MAX_RETRIES):
            try:
                response = requests.get(API_ENDPOINT, headers=headers, params=params)
                response.raise_for_status()

                # ✅ Check if empty or no content
                if response.status_code == 204 or not response.content.strip():
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📭 No more data on page {page + 1}.")
                    return all_data

                # ✅ Safe JSON parse only if content is present
                try:
                    data = response.json().get("list", [])
                except ValueError:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Response not in JSON format. Assuming end of data.")
                    return all_data

                if not data:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚫 Empty list on page {page + 1}, stopping.")
                    return all_data

                all_data.extend(data)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Retrieved page {page + 1} with {len(data)} records.")

                # ✅ Stop if last page
                if len(data) < 1000:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📦 Last page reached.")
                    return all_data

                break

            except RequestException as e:
                wait_time = 0.5 * (2 ** attempt)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Retry {attempt + 1} after {wait_time:.1f}s due to error: {e}")
                time.sleep(wait_time)

        else:
            raise RuntimeError(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Failed to fetch page {page} after {MAX_RETRIES} attempts.")

        page += 1


def main():
    print("Swift Timer Report Extractor v20250530")
    print("TS17: -ONLJdAstPfeGwVNgpYH\nTS16: -O99xSQdLiGywc6KRVw-\nTS15: -Np5nDzlfJrK_nt5Ro7e\nTS14: -NV5j_QcTmdwoaGklFvf\nTS13: -NFkG865XjMXlwqZ1AqU\n")
    try:
        token = get_token(USERNAME, PASSWORD)
        print("🔐 Token retrieved successfully.")

        project_id = input("Enter the Project ID: ").strip()
        from_date = input("Enter From Date (YYYY-MM-DD): ").strip()
        to_date = input("Enter To Date (YYYY-MM-DD): ").strip()

        data = fetch_paginated_data(token, project_id, from_date, to_date)

        # Placeholder for columns
        df = pd.DataFrame([{
            "Project": row.get("Project"),
            "Site Name": row.get("Site Name"),
            "Site ID": row.get("Site ID"),
            "Task": row.get("Task"),
            "Site Lat": row.get("Site Lat"),
            "User Lat": row.get("User Lat"),
            "Site Long": row.get("Site Long"),
            "User Long": row.get("User Long"),
            "User Accuracy (m)": row.get("User Accuracy (m)"),
            "Site vs User (km)": row.get("Site vs User (km)"),
            "Start Time": row.get("Start Time"),
            "End Time": row.get("End Time"),
            "Duration (min)": row.get("Duration (min)"),
            "User Name": row.get("User Name"),
            "User Email": row.get("User Email"),
            "User Role": row.get("User Role")
        } for row in data])

        df["Start Time"] = pd.to_datetime(df["Start Time"], errors='coerce')
        df["End Time"] = pd.to_datetime(df["End Time"], errors='coerce')

        filename = f"Swift_Timer_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        df.to_excel(filename, index=False)
        print(f"\n✅ Report saved as {filename}")

    except Exception as e:
        print(f"\n❌ Error occurred: {e}")


if __name__ == "__main__":
    main()