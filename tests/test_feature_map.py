from app.embedder import embed_texts
from app.feature_map import dependency_chunks, select_features
from app.settings import settings
from app.storage import StoredChunkRecord


def test_select_features_returns_the_closest_without_its_embedding() -> None:
    settings.hash_embeddings = True
    signup = "Create account\nEmail password confirm\nHome click Create an account"
    login = "Sign in\nEmail and password on the login page"
    vectors = embed_texts([signup, login])
    features = [
        {
            "feature": "Create account",
            "details": "Email password confirm",
            "howToEnter": "Home click Create an account",
            "dependencies": ["src/pages/RegisterPage.tsx"],
            "embedding": vectors[0],
        },
        {
            "feature": "Sign in",
            "details": "Email and password on the login page",
            "howToEnter": "Home click Get started",
            "dependencies": ["src/pages/LoginPage.tsx"],
            "embedding": vectors[1],
        },
    ]
    chosen = select_features(features, signup, top_k=1)
    assert chosen[0]["feature"] == "Create account"
    assert "embedding" not in chosen[0]


def test_dependency_chunks_keep_only_the_feature_files() -> None:
    records = [
        StoredChunkRecord("src/pages/RegisterPage.tsx", 0, 1, 20, "email field", [0.0]),
        StoredChunkRecord("src/pages/LoginPage.tsx", 0, 1, 10, "sign in", [0.0]),
    ]
    features = [{"dependencies": ["src/pages/RegisterPage.tsx"]}]
    chunks = dependency_chunks(records, features)
    assert [item["path"] for item in chunks] == ["src/pages/RegisterPage.tsx"]
