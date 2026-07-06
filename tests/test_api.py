import json

import geo_monitor
import geo_monitor.api
import geo_monitor.tool
from geo_monitor import GeoMonitorResult, StudyPaths, resolve_study_paths, run_geo_monitor
from geo_monitor.api import run_geo_monitor as api_run_geo_monitor


PUBLIC_API = {"GeoMonitorResult", "StudyPaths", "resolve_study_paths", "run_geo_monitor"}


def test_public_api_facade_exports_stable_symbols():
    assert run_geo_monitor is api_run_geo_monitor
    assert GeoMonitorResult.__name__ == "GeoMonitorResult"
    assert StudyPaths.__name__ == "StudyPaths"
    assert GeoMonitorResult.__module__ == "geo_monitor.api"
    assert StudyPaths.__module__ == "geo_monitor.api"
    assert resolve_study_paths(study_dir="/tmp/study").runs_dir.endswith("/tmp/study/runs")
    assert set(geo_monitor.__all__) == {"__version__", *PUBLIC_API}
    assert set(geo_monitor.api.__all__) == PUBLIC_API
    assert set(geo_monitor.tool.__all__) == PUBLIC_API
    assert "_analyze_job_bundle_unlocked" not in geo_monitor.__all__


def test_tool_compat_import_path_reexports_api_objects():
    assert geo_monitor.tool.run_geo_monitor is geo_monitor.api.run_geo_monitor
    assert geo_monitor.tool.GeoMonitorResult is geo_monitor.api.GeoMonitorResult
    assert geo_monitor.tool.StudyPaths is geo_monitor.api.StudyPaths
    assert geo_monitor.tool.resolve_study_paths is geo_monitor.api.resolve_study_paths


def test_geo_monitor_result_serializes_to_json():
    result = GeoMonitorResult(status="ok", metrics={"x": 1})

    assert result.to_dict()["status"] == "ok"
    assert json.loads(result.to_json())["metrics"] == {"x": 1}
