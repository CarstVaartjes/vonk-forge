from fastapi.testclient import TestClient
from vonk_control.api import SpaFiles


def test_spa_falls_back_to_index_for_client_routes_but_not_assets(tmp_path) -> None:
    from fastapi import FastAPI
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>Admin</h1>")
    app = FastAPI()
    app.mount("/", SpaFiles(directory=web, html=True))
    client = TestClient(app)
    assert client.get("/profiles").text == "<h1>Admin</h1>"
    assert client.get("/missing.js").status_code == 404
