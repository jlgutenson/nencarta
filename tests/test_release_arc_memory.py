import sys
import types

import pytest

from nencarta.tasks import run_models


@pytest.fixture
def calls(monkeypatch):
    """The garbage collections and memory releases made, in order, with an ARC that has release_freed_memory."""
    made = []
    monkeypatch.setattr(run_models.gc, "collect", lambda *args: made.append("collect"))
    monkeypatch.setitem(sys.modules, "arc", types.SimpleNamespace(release_freed_memory=lambda: made.append("release")))
    return made


@pytest.mark.parametrize("cells, expected", [
    (None, ["collect", "release"]),
    (run_models.COLLECT_FROM_CELLS, ["collect", "release"]),
    (114_000, ["collect", "release"]),
    (run_models.COLLECT_FROM_CELLS - 1, ["release"]),
    (406, ["release"]),
])
def test_only_a_large_arc_run_is_garbage_collected_after_but_every_one_is_released(calls, cells, expected):
    run_models._release_arc_memory(cells)
    assert calls == expected


@pytest.mark.parametrize("count, expected", [(406, ["release"]), (114_000, ["collect", "release"])])
def test_an_arc_run_says_how_many_cells_it_had(calls, monkeypatch, count, expected):
    class Arc:
        def __init__(self, config, quiet=False):
            pass

        def run(self):
            return types.SimpleNamespace(cells=types.SimpleNamespace(count=count))

    sys.modules["arc"].Arc = Arc
    run_models._run_arc("config.yaml", types.SimpleNamespace(quiet=True))
    assert calls == expected


def test_an_arc_run_that_returns_nothing_is_garbage_collected(calls):
    class Arc:
        def __init__(self, config, quiet=False):
            pass

        def run(self):
            return None

    sys.modules["arc"].Arc = Arc
    run_models._run_arc("config.yaml", types.SimpleNamespace(quiet=True))
    assert calls == ["collect", "release"]


def test_an_arc_without_release_freed_memory_is_left_alone(monkeypatch):
    made = []
    monkeypatch.setattr(run_models.gc, "collect", lambda *args: made.append("collect"))
    monkeypatch.setitem(sys.modules, "arc", types.SimpleNamespace())
    run_models._release_arc_memory(114_000)
    assert made == []
