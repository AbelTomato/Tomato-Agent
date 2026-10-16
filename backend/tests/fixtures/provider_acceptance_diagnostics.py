"""Best-effort, dependency-free UI diagnostics for Provider acceptance runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MAX_PAGE_TEXT = 4000


def _error(source: str, exc: BaseException) -> dict[str, str]:
    return {"source": source, "error_type": type(exc).__name__, "error": str(exc)[:500]}


async def collect_ui_diagnostics(
    page: Any,
    output_dir: str | Path,
    *,
    scenario_id: str,
    visible_run_status: str | None = None,
    original_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, Any] = {
        "scenario_id": scenario_id,
        "visible_run_status": visible_run_status,
        "page_url": None,
        "page_title": None,
        "page_text": None,
    }
    errors: list[dict[str, str]] = []
    files: dict[str, str] = {}

    screenshot_path = output / f"{scenario_id}.png"
    try:
        snapshot["page_url"] = page.url
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        errors.append(_error("page_url", exc))
    try:
        snapshot["page_title"] = await page.title()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        errors.append(_error("page_title", exc))
    try:
        text = await page.locator("body").inner_text(timeout=2000)
        snapshot["page_text"] = text[:MAX_PAGE_TEXT]
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        errors.append(_error("page_text", exc))

    snapshot_path = output / f"{scenario_id}-ui.json"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        files["screenshot"] = str(screenshot_path)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        errors.append(_error("screenshot", exc))

    files["ui_snapshot"] = str(snapshot_path)
    result = {
        "snapshot": snapshot,
        "diagnostic_errors": errors,
        "diagnostic_files": files,
        "original_error": original_error,
    }
    snapshot_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result