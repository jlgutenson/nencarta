"""The configuration reference has one entry for every config key, and no others."""
import re
from pathlib import Path

from nencarta.core.defaults import CONFIG_OPTIONS

CONFIGURATION_DOCS = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"


def test_every_config_key_is_documented_once():
    text = CONFIGURATION_DOCS.read_text(encoding="utf-8")
    documented = re.findall(r'<a id="json-(\w+)"></a>', text)

    assert sorted(documented) == sorted(CONFIG_OPTIONS)
    for key in documented:
        assert f'\n* <a id="json-{key}"></a>`{key}` (' in text, f"the entry for {key} doesn't start its own item"
