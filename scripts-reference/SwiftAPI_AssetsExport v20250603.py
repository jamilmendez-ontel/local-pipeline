"""Extract asset tasks from SwiftProjects API."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from typing import Generator

from etl.clients.swift_api import SwiftProjectsAPI
from etl.utils.helpers import sanitize_column_name
from etl.utils.time_helpers import get_now
from etl.utils.logging_config import get_logger

logger = get_logger(__name__)

# Number of concurrent API fetch threads
MAX_WORKERS = 3

# Columns to extract from the API response
ASSET_TASK_COLUMNS = [
    "Project_DID",
    "Project_Status",
    "Asset_DID",
    "Asset_ID",
    "Asset_Name",
    "Asset_Requirement_Count",
    "Task_DID",
    "Task_Name",
    "Task_Status",
    "Task_Scheduled",
    "Task_Assigned_To_DID",
    "Task_Assigned_To_Collection",
    "Task_Assigned_To_Name",
    "Task_Assigned_To_Email",
    "Task_Submitted_On",
    "Task_Submitted_By_DID",
    "Task_Submitted_By_Name",
    "Task_Submitted_By_Email",
    "Task_Approved_On",
    "Task_Approved_By_DID",
    "Task_Approved_By_Name",
    "Task_Approved_By_Email",
    "Task_Cancelled_On",
    "Task_Cancelled_By_DID",
    "Task_Cancelled_By_Name",
    "Task_Cancelled_By_Email",
]


def transform_asset_row(row: dict) -> dict:
    """Transform API row to database format."""
    retrieved_at = get_now().isoformat()

    transformed = {}
    for col in ASSET_TASK_COLUMNS:
        snake_col = sanitize_column_name(col)
        transformed[snake_col] = row.get(col)

    transformed["retrieved_at"] = retrieved_at
    return transformed


def _extract_project_asset_tasks(
    api: SwiftProjectsAPI,
    project_id: str,
    page_size: int,
    timezone: str,
    result_queue: Queue,
) -> int:
    """Extract asset tasks for a single project (for parallel processing)."""
    project_rows = 0
    logger.info(f"Extracting asset tasks for project {project_id}")

    for batch in api.get_asset_tasks_export(
        project_id=project_id,
        page_size=page_size,
        timezone=timezone,
    ):
        transformed_batch = [transform_asset_row(row) for row in batch]
        project_rows += len(transformed_batch)
        result_queue.put(transformed_batch)

    logger.info(f"Project {project_id}: extracted {project_rows} rows")
    return project_rows


def extract_asset_tasks(
    api: SwiftProjectsAPI,
    project_ids: list[str],
    page_size: int = 1000,
    timezone: str = "America/New_York",
    parallel: bool = True,
    max_workers: int = MAX_WORKERS,
) -> Generator[list[dict], None, None]:
    """
    Extract asset tasks for given projects.

    Args:
        api: Authenticated SwiftProjectsAPI client
        project_ids: List of project IDs to extract
        page_size: Number of rows per API page
        timezone: Timezone for date formatting
        parallel: Use parallel processing for multiple projects
        max_workers: Maximum number of concurrent threads

    Yields:
        Batches of transformed asset task rows
    """
    if parallel and len(project_ids) > 1:
        # Parallel extraction using ThreadPoolExecutor
        yield from _extract_parallel(api, project_ids, page_size, timezone, max_workers)
    else:
        # Sequential extraction
        yield from _extract_sequential(api, project_ids, page_size, timezone)


def _extract_sequential(
    api: SwiftProjectsAPI,
    project_ids: list[str],
    page_size: int,
    timezone: str,
) -> Generator[list[dict], None, None]:
    """Sequential extraction (original method)."""
    total_rows = 0

    for project_id in project_ids:
        logger.info(f"Extracting asset tasks for project {project_id}")
        project_rows = 0

        for batch in api.get_asset_tasks_export(
            project_id=project_id,
            page_size=page_size,
            timezone=timezone,
        ):
            transformed_batch = [transform_asset_row(row) for row in batch]
            project_rows += len(transformed_batch)
            total_rows += len(transformed_batch)

            yield transformed_batch

        logger.info(f"Project {project_id}: extracted {project_rows} rows")

    logger.info(f"Total asset task rows extracted: {total_rows}")


def _extract_parallel(
    api: SwiftProjectsAPI,
    project_ids: list[str],
    page_size: int,
    timezone: str,
    max_workers: int,
) -> Generator[list[dict], None, None]:
    """Parallel extraction using ThreadPoolExecutor."""
    from threading import Thread
    import time

    result_queue = Queue()
    total_rows = 0
    active_futures = 0

    logger.info(f"Starting parallel extraction with {max_workers} workers for {len(project_ids)} projects")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all projects to the thread pool
        futures = {}
        for project_id in project_ids:
            future = executor.submit(
                _extract_project_asset_tasks,
                api,
                project_id,
                page_size,
                timezone,
                result_queue,
            )
            futures[future] = project_id
            active_futures += 1

        # Yield results as they come in from the queue
        completed = 0
        while completed < len(project_ids):
            # Check for completed futures
            done_futures = [f for f in futures if f.done()]
            for future in done_futures:
                if future in futures:
                    try:
                        rows = future.result()
                        total_rows += rows
                    except Exception as e:
                        logger.error(f"Project {futures[future]} failed: {e}")
                    completed += 1
                    del futures[future]

            # Yield any available batches from the queue
            while not result_queue.empty():
                batch = result_queue.get_nowait()
                yield batch

            # Small sleep to avoid busy waiting
            if completed < len(project_ids):
                time.sleep(0.1)

        # Drain any remaining items in the queue
        while not result_queue.empty():
            batch = result_queue.get_nowait()
            yield batch

    logger.info(f"Total asset task rows extracted (parallel): {total_rows}")
