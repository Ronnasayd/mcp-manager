import json

from src.hooks.mcp_manager_context import SERVER_DESCRIPTIONS, build_context


def _catalog(tmp_path, ids):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"backends": [{"id": i, "tool_count": 1} for i in ids]}))
    return str(path)


def test_includes_description_for_known_server(tmp_path):
    out = build_context(_catalog(tmp_path, ["exa"]))

    assert "| Server | Tools | Description |" in out
    assert f"| exa | 1 | {SERVER_DESCRIPTIONS['exa']} |" in out


def test_unknown_server_leaves_description_empty(tmp_path):
    out = build_context(_catalog(tmp_path, ["new-server"]))

    assert "| new-server | 1 |  |" in out


def test_every_cataloged_server_has_description():
    with open("catalog.json", encoding="utf-8") as f:
        ids = {b["id"] for b in json.load(f)["backends"]}

    assert ids <= SERVER_DESCRIPTIONS.keys()
