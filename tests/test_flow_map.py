import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.embed_service import build_embeddings
from app.flow_map import (
    _embedding_text,
    build_flow_map,
    dependency_chunks,
    describe_entry,
    extract_routes,
    load_flow_map,
    select_nodes,
)
from app.main import app
from app.settings import settings
from app.storage import StoredChunkRecord, load_stored_chunks
from app.tursor_config import load_tursor_config

APP = """import { Route, Routes } from 'react-router-dom';
import { HomePage } from './pages/HomePage';
import { LoginPage } from './pages/LoginPage';
import { StudioPage } from './pages/StudioPage';
import { RequireAuth } from './RequireAuth';

export function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/studio" element={<StudioPage />} />
        <Route path="/works/:id" element={<StudioPage />} />
      </Route>
    </Routes>
  );
}
"""

HOME = """import { Link } from 'react-router-dom';
export function HomePage() {
  return <Link to="/login">Get started</Link>;
}
"""

LOGIN = """export function LoginPage() {
  const navigate = useNavigate();
  return <form onSubmit={() => navigate('/studio')}><input name="email" /></form>;
}
"""

STUDIO = """const items = [{ to: `/works/${id}`, label: 'Open work' }];
export function StudioPage() { return <h1>Studio</h1>; }
"""


@pytest.fixture(autouse=True)
def hash_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    settings.hash_embeddings = True
    monkeypatch.setenv("TURSOR_AI_MOCK_LLM", "1")


def _workspace(tmp_path):
    tursor = tmp_path / ".tursor"
    tursor.mkdir()
    (tursor / "config.json").write_text(
        json.dumps({"ai": {"generationModel": "gpt-5.6", "apiKey": "sk-test"}}),
        encoding="utf-8",
    )
    pages = tmp_path / "src" / "pages"
    pages.mkdir(parents=True)
    (tmp_path / "src" / "App.tsx").write_text(APP, encoding="utf-8")
    (pages / "HomePage.tsx").write_text(HOME, encoding="utf-8")
    (pages / "LoginPage.tsx").write_text(LOGIN, encoding="utf-8")
    (pages / "StudioPage.tsx").write_text(STUDIO, encoding="utf-8")
    return tursor


def test_routes_keep_their_guard_layout() -> None:
    routes = extract_routes(
        {
            "src/App.tsx": APP,
            "src/pages/HomePage.tsx": HOME,
            "src/pages/LoginPage.tsx": LOGIN,
            "src/pages/StudioPage.tsx": STUDIO,
        }
    )
    by_route = {route["route"]: route for route in routes}
    assert list(by_route) == ["/", "/login", "/studio", "/works/:id"]
    assert by_route["/studio"]["guards"] == ["RequireAuth"]
    assert by_route["/login"]["guards"] == []
    assert by_route["/login"]["file"] == "src/pages/LoginPage.tsx"


def test_flow_map_is_a_tree_from_the_root(tmp_path) -> None:
    tursor = _workspace(tmp_path)
    legacy = tursor / "embeddings" / "feature-map.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"features": []}', encoding="utf-8")
    build_embeddings(str(tmp_path))

    res = TestClient(app).post("/v1/flow-map", json={"directory_path": str(tmp_path)})
    assert res.status_code == 200
    assert res.json() == {
        "ok": True,
        "screens": 4,
        "reachable": 4,
        "described": 0,
        "failed_batches": 0,
        "skipped": False,
    }
    assert not legacy.exists()

    flow = load_flow_map(tursor / "embeddings")
    assert flow is not None
    nodes = flow["nodes"]
    assert nodes["/login"]["parent"] == "/"
    assert nodes["/studio"]["parent"] == "/login"
    assert nodes["/works/:id"]["parent"] == "/studio"
    assert nodes["/studio"]["requires_auth"] is True
    steps = describe_entry(nodes["/studio"])
    assert steps[0] == "Open /"
    assert 'Click "Get started" to reach /login' in steps


def test_select_nodes_returns_public_view(tmp_path) -> None:
    tursor = _workspace(tmp_path)
    build_embeddings(str(tmp_path))
    TestClient(app).post("/v1/flow-map", json={"directory_path": str(tmp_path)})
    flow = load_flow_map(tursor / "embeddings")
    # Hash embeddings are exact-match only, so query with the screen's own text.
    chosen = select_nodes(flow, _embedding_text(flow["nodes"]["/login"]), top_k=1)
    assert chosen[0]["module_key"] == "/login"
    assert "embedding" not in chosen[0]
    assert chosen[0]["how_to_reach"][0] == "Open /"


def test_dependency_chunks_keep_only_the_screen_files() -> None:
    records = [
        StoredChunkRecord("src/pages/RegisterPage.tsx", 0, 1, 20, "email field", [0.0]),
        StoredChunkRecord("src/pages/LoginPage.tsx", 0, 1, 10, "sign in", [0.0]),
    ]
    nodes = [{"dependencies": ["src/pages/RegisterPage.tsx"]}]
    chunks = dependency_chunks(records, nodes)
    assert [item["path"] for item in chunks] == ["src/pages/RegisterPage.tsx"]


def test_a_timed_out_batch_keeps_the_code_only_map(tmp_path, monkeypatch) -> None:
    _workspace(tmp_path)
    build_embeddings(str(tmp_path))
    cfg = load_tursor_config(tmp_path)
    calls: list[int] = []

    def time_out(*_args, **_kwargs):
        calls.append(1)
        raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr("app.flow_map._describe_batch", time_out)
    flow = build_flow_map(cfg, load_stored_chunks(cfg.embeddings_path), describe=True)
    assert flow["failed_batches"] >= 1
    assert flow["described"] == 0
    assert len(calls) == 2 * flow["failed_batches"]
    nodes = flow["nodes"]
    assert set(nodes) == {"/", "/login", "/studio", "/works/:id"}
    assert nodes["/studio"]["parent"] == "/login"


def test_a_partial_map_is_rebuilt_on_the_next_embed(tmp_path) -> None:
    tursor = _workspace(tmp_path)
    build_embeddings(str(tmp_path))
    map_file = tursor / "embeddings" / "flow-map.json"
    map_file.write_text(json.dumps({"nodes": {}, "failed_batches": 1}), encoding="utf-8")
    assert build_embeddings(str(tmp_path)).flow_map_needed is True
    map_file.write_text(json.dumps({"nodes": {}, "failed_batches": 0}), encoding="utf-8")
    assert build_embeddings(str(tmp_path)).flow_map_needed is False


def test_unchanged_screens_reuse_their_description(tmp_path, monkeypatch) -> None:
    _workspace(tmp_path)
    build_embeddings(str(tmp_path))
    cfg = load_tursor_config(tmp_path)
    records = load_stored_chunks(cfg.embeddings_path)
    from app.flow_map import file_hashes

    hashes = file_hashes(cfg.embeddings_path)
    described_routes: list[str] = []

    def describe(_cfg, batch, *_args):
        described_routes.extend(route["route"] for route in batch)
        return {route["route"]: {"name": f"Model {route['route']}"} for route in batch}

    monkeypatch.setattr("app.flow_map._describe_batch", describe)
    first = build_flow_map(cfg, records, describe=True, hashes=hashes)
    assert sorted(described_routes) == ["/", "/login", "/studio", "/works/:id"]

    described_routes.clear()
    changed = {**hashes, "src/pages/LoginPage.tsx": "edited"}
    second = build_flow_map(cfg, records, describe=True, previous=first, hashes=changed)
    assert described_routes == ["/login"]
    assert second["nodes"]["/studio"]["name"] == "Model /studio"
