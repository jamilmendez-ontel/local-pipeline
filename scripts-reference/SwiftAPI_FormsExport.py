# pyinstaller --onefile "SwiftAPI_FormsExport.py"

import requests
import os
from dotenv import load_dotenv
from datetime import datetime
import time

# Constants
base_url = "https://prod.api.swiftprojects.io"
PAGE_SIZE = 2000
MAX_RETRIES = 5

# Load credentials from env file
load_dotenv(os.path.expanduser("~/Downloads/mgmt.env"))
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


def fetch_csv_pages(token, form_id):
    csv_chunks = []
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/csv"
    }
    next_cursor = None
    loop_count = 1

    while True:
        params = {"pageSize": str(PAGE_SIZE)}
        if next_cursor:
            params["after"] = next_cursor

        url = f"{base_url}/api/forms/{form_id}/requirement-responses"
        success = False

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(url, headers=headers, params=params)
                # print(f"[{datetime.now().strftime('%H:%M:%S')}] 📥 Content-Type: {resp.headers.get('Content-Type')}")

                if resp.status_code in [200, 204]:
                    success = True
                    break
            except requests.RequestException:
                pass

            delay = 0.5 * (2 ** (attempt - 1))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ Retry {attempt} after {delay:.1f}s")
            time.sleep(delay)

        if not success:
            raise RuntimeError(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Failed to fetch page after {MAX_RETRIES} attempts.")

        if resp.status_code == 204:
            break

        csv_chunks.append(resp.text)
        next_cursor = resp.headers.get("x-next")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Page {loop_count} retrieved, {len(resp.text.splitlines())} lines.")
        loop_count += 1

        if not next_cursor:
            break

    return csv_chunks


def save_csv_chunks(csv_chunks, form_id):
    filename = f"{form_id}_Data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("".join(csv_chunks))
    print(f"\n✅ CSV saved to {filename}")


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Swift Form Export | v20250602")

    print("\n📄 Available Forms:")
    print("ACTIVE - QA Form TS10: -N0iQAwHBd_Hw9QeI8AZ")
    print("ACTIVE - QA Form TS11: -N4E59bGFqrrt3h3hUl0")
    print("ACTIVE - QA Form TS12: -NC0oUZnAHZbUBCWuUKR")
    print("ACTIVE - QA Form TS13: -NH1hUPkaKtPdd7BK9cb")
    print("ACTIVE - QA Form TS14: -NXCg4vTDNVykN8ioMYp")
    print("ACTIVE - QA Form TS15: -Np6o9OCL4RWIJq68HJe")
    print("ACTIVE - QA Form TS16: -O9ACLN3je1w7oEoG5hY")
    print("ACTIVE - QA Form TS17: -ONMD-cGBq-_3r9ybaAq")
    print("ACTIVE - QA Form TS7: -MjeVvKardGyBkaqenYQ")
    print("Miss Log Form: -NVAuYzobt7fFYC8_HiK")
    print("Miss Log Form (v2.0): -Np6hYV6Aaf-x3fL9Z8j")
    print("Miss Log Form (v3) (2024): -O9FL6K_hH6TU_ldihSo")
    print("Second Level Review Form: -NXHPM2Ws4ctschvMyCm")
    print("Second Level Review Form (v2.0): -Np6Zg9vWJvwSEyAdaI9")
    print("Task Rescheduling Form 04052023: -NSDr1UISYc9Gppb3k_q")
    print("QA Rejection Form : -ONMQxzi3IR88J7O7LYw\n")

    try:
        token = get_token(USERNAME, PASSWORD)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔐 Token retrieved successfully.")

        while True:
            form_id = input(f"[{datetime.now().strftime('%H:%M:%S')}] 🏢 Enter the Form ID: ").strip()
            if not form_id:
                raise ValueError("Form ID cannot be empty.")

            csv_chunks = fetch_csv_pages(token, form_id)
            if csv_chunks:
                save_csv_chunks(csv_chunks, form_id)
            else:
                print("⚠️ No CSV data returned.")

            again = input("\n🔁 Do you want to run another form? (y/n): ").strip().lower()
            if again != "y":
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 👋 Exiting the tool. Goodbye!")
                break

    except requests.HTTPError as err:
        print(f"\n❌ HTTP error: {err.response.status_code} - {err.response.text}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
