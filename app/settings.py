from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TURSOR_AI_",
        extra="ignore",
    )

    embed_model: str = "BAAI/bge-small-en-v1.5"
    hash_embeddings: bool = False
    hash_embedding_dim: int = 384
    chunk_size: int = 1200
    chunk_overlap: int = 150
    host: str = "127.0.0.1"
    port: int = 8000

    text_extensions: frozenset[str] = frozenset(
        {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".py",
            ".json",
            ".md",
            ".mdx",
            ".css",
            ".scss",
            ".html",
            ".vue",
            ".svelte",
            ".yaml",
            ".yml",
            ".toml",
            ".sql",
            ".sh",
            ".env.example",
        }
    )


settings = Settings()
