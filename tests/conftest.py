from __future__ import annotations

from pathlib import Path

import pytest

from qlik2pbi.app.loader import load_app
from qlik2pbi.findings import FindingLog
from qlik2pbi.pipeline import run
from qlik2pbi.script.interpret import interpret
from qlik2pbi.semantic.measures import measure_names, translator
from qlik2pbi.semantic.planner import plan_model

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "sales_app"


@pytest.fixture
def app():
    return load_app(SAMPLE)


@pytest.fixture
def planned(app):
    log = FindingLog()
    sm = interpret(app, log)
    model = plan_model(app, sm, log)
    names = measure_names(app, model, log)
    return app, sm, model, translator(app, sm, model, names), log


@pytest.fixture(scope="session")
def converted(tmp_path_factory):
    return run(SAMPLE, tmp_path_factory.mktemp("convert"))
