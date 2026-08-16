"""Test harness that injects deterministic providers without app test mode."""

from app.pipeline import run_analysis as production_run_analysis
from tests.fixture_market import fixture_history, fixture_metadata

import app.ui as ui


def fixture_run_analysis(files, as_of=None):
    return production_run_analysis(
        files,
        as_of=as_of,
        history_provider=fixture_history,
        metadata_provider=fixture_metadata,
    )


ui.run_analysis = fixture_run_analysis
ui.main()
