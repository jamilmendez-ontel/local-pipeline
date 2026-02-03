#!/usr/bin/env python3
"""
Discover Forms from Swift Projects using Playwright
Automatically collects form IDs from the Swift Projects UI
"""

import sys
import json
import re
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from config import SWIFT_USERNAME, SWIFT_PASSWORD

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True)

SWIFT_URL = "https://swiftprojects.io"
FORMS_URL = f"{SWIFT_URL}/#/app/forms"


def discover_forms(headless: bool = True, filter_pattern: str = None):
    """
    Discover all forms from Swift Projects UI

    Args:
        headless: Run browser in headless mode (default True)
        filter_pattern: Regex pattern to filter form names (e.g., r'QA.*TS1[3-9]')

    Returns:
        List of form dictionaries with id, name, and other metadata
    """
    print(f"[{datetime.now():%H:%M:%S}] Starting form discovery...")

    forms = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            # Navigate to Swift Projects
            print(f"[{datetime.now():%H:%M:%S}] Navigating to Swift Projects...")
            page.goto(SWIFT_URL, wait_until="networkidle")
            time.sleep(2)

            # Wait for login form and enter credentials
            print(f"[{datetime.now():%H:%M:%S}] Logging in with credentials from .env...")

            # Wait for email input to be visible
            page.wait_for_selector('input[type="email"]:visible', timeout=30000)

            # Fill email field
            email_input = page.locator('input[type="email"]:visible').first
            email_input.fill(SWIFT_USERNAME)
            print(f"[{datetime.now():%H:%M:%S}] Filled email field")

            # Fill password field
            password_input = page.locator('input[type="password"]:visible').first
            password_input.fill(SWIFT_PASSWORD)
            print(f"[{datetime.now():%H:%M:%S}] Filled password field")

            # Click login button - wait a moment for password field to settle
            time.sleep(0.5)
            login_button = page.locator('button:has-text("Log in")').first
            # Use force click to avoid element interception issues
            login_button.click(force=True)
            print(f"[{datetime.now():%H:%M:%S}] Clicked login button")

            # Wait for navigation after login
            print(f"[{datetime.now():%H:%M:%S}] Waiting for login to complete...")
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)  # Extra wait for SPA to settle

            # Navigate to forms page - click on Forms in sidebar
            print(f"[{datetime.now():%H:%M:%S}] Navigating to forms page...")
            page.goto(FORMS_URL, wait_until="networkidle")
            time.sleep(2)

            # Click on Forms in the sidebar to ensure we're on the forms page
            forms_menu = page.locator('text="Forms"').first
            if forms_menu.is_visible():
                forms_menu.click()
                print(f"[{datetime.now():%H:%M:%S}] Clicked Forms menu item")
                time.sleep(2)

            # In visible mode, wait for user to navigate to correct organization
            if not headless:
                wait_seconds = 60
                print(f"\n{'='*60}")
                print("MANUAL NAVIGATION REQUIRED")
                print(f"1. Navigate to the correct organization/project in the browser")
                print(f"2. Make sure you're on the Forms page showing QA Forms")
                print(f"3. You have {wait_seconds} seconds to navigate...")
                print(f"{'='*60}\n")
                for i in range(wait_seconds, 0, -10):
                    print(f"  {i} seconds remaining...")
                    time.sleep(10)
                print(f"[{datetime.now():%H:%M:%S}] Time's up! Capturing forms...")
                time.sleep(2)
                page.screenshot(path="forms_ready_debug.png", full_page=True)
                print(f"[{datetime.now():%H:%M:%S}] Screenshot saved after manual navigation")

            # Try to find form elements - adjust selectors based on actual page structure
            print(f"[{datetime.now():%H:%M:%S}] Searching for forms...")

            # Wait for content to load
            page.wait_for_load_state("networkidle", timeout=30000)

            # Look for form links/items - these patterns may need adjustment
            # Try various selectors that might contain form info
            selectors_to_try = [
                'a[href*="/forms/"]',
                '[data-form-id]',
                '.form-item',
                '.form-list-item',
                'tr[data-id]',
                '.list-item a',
                'mat-list-item',
                '.mat-mdc-list-item',
            ]

            for selector in selectors_to_try:
                elements = page.locator(selector).all()
                if elements:
                    print(f"[{datetime.now():%H:%M:%S}] Found {len(elements)} elements with selector: {selector}")
                    break

            # Extract form info from page - capture network requests for forms API
            # Intercept API calls to find form data
            print(f"[{datetime.now():%H:%M:%S}] Capturing form data from page...")

            # Get page content for analysis
            content = page.content()

            # Look for form IDs in the page content (Firebase push IDs start with -)
            form_id_pattern = r'-[A-Za-z0-9_-]{19,}'
            potential_ids = set(re.findall(form_id_pattern, content))

            # Also check URLs in the page
            url_pattern = r'/forms/(-[A-Za-z0-9_-]{19,})'
            url_ids = re.findall(url_pattern, content)
            potential_ids.update(url_ids)

            print(f"[{datetime.now():%H:%M:%S}] Found {len(potential_ids)} potential form IDs in page")

            # Try clicking on each form to get its details
            # Look for clickable form items
            form_links = page.locator('a[href*="forms"]').all()

            if form_links:
                print(f"[{datetime.now():%H:%M:%S}] Found {len(form_links)} form links, extracting details...")

                for i, link in enumerate(form_links):
                    try:
                        href = link.get_attribute('href')
                        text = link.inner_text().strip()

                        # Extract form ID from href
                        match = re.search(r'/forms/(-[A-Za-z0-9_-]+)', href or '')
                        if match:
                            form_id = match.group(1)
                            form_info = {
                                'form_id': form_id,
                                'name': text,
                                'href': href
                            }

                            # Apply filter if specified
                            if filter_pattern:
                                if re.search(filter_pattern, text, re.IGNORECASE):
                                    forms.append(form_info)
                                    print(f"  [{i+1}] {text}: {form_id}")
                            else:
                                forms.append(form_info)
                                print(f"  [{i+1}] {text}: {form_id}")
                    except Exception as e:
                        print(f"  Error extracting form {i+1}: {e}")

            # Click-based extraction method - click Edit (pencil) icon to get form IDs
            if not forms:
                print(f"[{datetime.now():%H:%M:%S}] Extracting forms by clicking Edit buttons...")

                # Scroll to load all forms first
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1)

                # Find all edit buttons (pencil icons) - they're typically SVG or button elements
                # Look for buttons/icons with edit-related attributes
                edit_buttons = page.locator('button[aria-label*="edit" i], button[title*="edit" i], [class*="edit"]:not([class*="editor"]), svg[class*="edit"], ion-icon[name*="create"]').all()
                print(f"[{datetime.now():%H:%M:%S}] Found {len(edit_buttons)} potential edit buttons")

                if not edit_buttons:
                    # Try finding pencil icons by looking at the row structure
                    # Each form row has icons on the right side
                    edit_buttons = page.locator('[class*="pencil"], [class*="icon"]:has-text("create"), button:has(ion-icon)').all()
                    print(f"[{datetime.now():%H:%M:%S}] Found {len(edit_buttons)} edit icons (second attempt)")

                # Alternative: Find all form rows and click the edit icon within each
                form_rows = page.locator('text=/ACTIVE.*QA Form TS\\d+/i').all()
                print(f"[{datetime.now():%H:%M:%S}] Found {len(form_rows)} QA Form rows")

                for i, row in enumerate(form_rows):
                    try:
                        text = row.inner_text().strip()
                        form_name_match = re.search(r'QA Form TS(\d+)', text, re.IGNORECASE)
                        if not form_name_match:
                            continue

                        ts_num = int(form_name_match.group(1))
                        form_name = f"QA Form TS{ts_num}"
                        print(f"  [{i+1}] {form_name}")

                        # Get the parent row element to find the edit button
                        # The edit button should be a sibling or nearby element
                        # Try to find and click the edit button in the same row

                        # First, scroll the row into view
                        row.scroll_into_view_if_needed()
                        time.sleep(0.3)

                        # Get bounding box of the form name
                        box = row.bounding_box()
                        if box:
                            # The edit (pencil) icon is typically to the right of the form name
                            # Click at a position to the right of the form name
                            edit_x = box['x'] + box['width'] + 50  # 50px to the right
                            edit_y = box['y'] + box['height'] / 2

                            # Click the edit button position
                            page.mouse.click(edit_x, edit_y)
                            time.sleep(1.5)

                            # Check URL for form ID (should be like /forms/editor/-XXXXX)
                            current_url = page.url
                            match = re.search(r'/forms/(?:editor/)?(-[A-Za-z0-9_-]+)', current_url)
                            if match:
                                form_id = match.group(1)

                                form_info = {
                                    'form_id': form_id,
                                    'name': form_name
                                }

                                # Check if already collected
                                if form_id not in [f['form_id'] for f in forms]:
                                    if filter_pattern:
                                        if re.search(filter_pattern, form_name, re.IGNORECASE):
                                            forms.append(form_info)
                                            print(f"      -> ID: {form_id}")
                                    else:
                                        forms.append(form_info)
                                        print(f"      -> ID: {form_id}")

                                # Go back to forms list
                                page.go_back()
                                time.sleep(1)
                            else:
                                print(f"      -> No form ID in URL: {current_url[:80]}")
                                # Go back just in case
                                if 'forms' not in current_url or 'editor' in current_url:
                                    page.go_back()
                                    time.sleep(0.5)

                    except Exception as e:
                        print(f"  Error on form {i}: {e}")
                        # Try to navigate back to forms
                        try:
                            page.goto(FORMS_URL, wait_until="networkidle")
                            time.sleep(1)
                        except:
                            pass
                        continue

            # Take screenshot for debugging if no forms found
            if not forms:
                screenshot_path = "forms_page_debug.png"
                page.screenshot(path=screenshot_path, full_page=True)
                print(f"[{datetime.now():%H:%M:%S}] No forms found. Screenshot saved to {screenshot_path}")
                print(f"[{datetime.now():%H:%M:%S}] Potential form IDs found in page: {list(potential_ids)[:10]}")

        except PlaywrightTimeout as e:
            print(f"[{datetime.now():%H:%M:%S}] Timeout error: {e}")
            page.screenshot(path="timeout_error.png", full_page=True)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Error: {e}")
            page.screenshot(path="error_debug.png", full_page=True)
        finally:
            browser.close()

    print(f"\n[{datetime.now():%H:%M:%S}] Discovery complete. Found {len(forms)} forms.")
    return forms


def generate_qa_forms_config(forms: list) -> dict:
    """
    Generate QA_FORMS configuration dictionary from discovered forms

    Args:
        forms: List of form dictionaries with form_id and name

    Returns:
        Dictionary in QA_FORMS format for extract_forms.py
    """
    qa_forms = {}

    # Pattern to extract TS number from form name
    ts_pattern = r'TS\s*(\d+)'

    for form in forms:
        name = form.get('name', '')
        form_id = form.get('form_id', '')

        match = re.search(ts_pattern, name, re.IGNORECASE)
        if match:
            ts_num = int(match.group(1))

            # Only include TS13 and above
            if ts_num >= 13:
                key = f"qa_ts{ts_num}"
                qa_forms[key] = {
                    "form_id": form_id,
                    "table_name": f"raw_form_qa_ts{ts_num}",
                    "display_name": f"QA Form TS{ts_num}"
                }

    # Sort by TS number
    qa_forms = dict(sorted(qa_forms.items(), key=lambda x: int(re.search(r'\d+', x[0]).group())))

    return qa_forms


def save_forms_config(forms_config: dict, output_file: str = "qa_forms_config.json"):
    """Save forms configuration to JSON file"""
    with open(output_file, 'w') as f:
        json.dump(forms_config, f, indent=4)
    print(f"[{datetime.now():%H:%M:%S}] Configuration saved to {output_file}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Discover Forms from Swift Projects")
    parser.add_argument("--visible", action="store_true", help="Run browser in visible mode (not headless)")
    parser.add_argument("--filter", type=str, default=r'QA.*TS', help="Regex pattern to filter form names")
    parser.add_argument("--output", type=str, default="qa_forms_config.json", help="Output file for forms config")
    parser.add_argument("--all", action="store_true", help="Discover all forms (no filter)")

    args = parser.parse_args()

    filter_pattern = None if args.all else args.filter

    # Discover forms
    forms = discover_forms(headless=not args.visible, filter_pattern=filter_pattern)

    if forms:
        # Generate and save configuration
        config = generate_qa_forms_config(forms)

        print(f"\n{'='*60}")
        print("Generated QA_FORMS configuration:")
        print('='*60)
        print(json.dumps(config, indent=4))
        print('='*60)

        save_forms_config(config, args.output)

        # Print Python dict format for easy copy-paste
        print("\nPython format for extract_forms.py:")
        print("QA_FORMS = {")
        for key, value in config.items():
            print(f'    "{key}": {{')
            print(f'        "form_id": "{value["form_id"]}",')
            print(f'        "table_name": "{value["table_name"]}",')
            print(f'        "display_name": "{value["display_name"]}"')
            print(f'    }},')
        print("}")
    else:
        print("\nNo forms discovered. Try running with --visible flag to debug.")
