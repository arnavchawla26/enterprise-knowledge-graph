"""Settings, connections and the paths of the read-only Project 1 inputs.

Everything comes from environment variables (loaded from .env if present).
Secrets are never logged. The sibling repository services-ops-intelligence is
an input only: this module computes paths into it and never writes there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env", override=False)


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise KeyError(f"{name} is not set (see .env.example)")
    return v


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = env("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = env("NEO4J_USER", "neo4j")
    neo4j_password: str = env("NEO4J_PASSWORD", "ekg_local_dev_only")
    neo4j_reader_user: str = env("NEO4J_READER_USER", "ekg_reader")
    neo4j_reader_password: str = env("NEO4J_READER_PASSWORD", "ekg_reader_local_dev_only")
    pg_host: str = env("POSTGRES_HOST", "localhost")
    pg_port: int = int(env("POSTGRES_PORT", "5432"))
    pg_user: str = env("POSTGRES_USER", "sois")
    pg_password: str = env("POSTGRES_PASSWORD", "sois_local_dev_only")
    pg_db: str = env("POSTGRES_DB", "sois")
    es_url: str = env("ES_URL", "http://localhost:9200")
    kibana_url: str = env("KIBANA_URL", "http://localhost:5601")
    sois_repo: Path = Path(env("SOIS_REPO", str(REPO_ROOT.parent / "services-ops-intelligence"))).expanduser()

    @property
    def pg_dsn(self) -> str:
        return f"host={self.pg_host} port={self.pg_port} user={self.pg_user} password={self.pg_password} dbname={self.pg_db}"

    @property
    def enterprise_dir(self) -> Path:
        return self.sois_repo / "data" / "enterprise"

    @property
    def parquet_dir(self) -> Path:
        return self.sois_repo / "data" / "parquet"

    @property
    def truth_dir(self) -> Path:
        return self.sois_repo / "truth"

    @property
    def sois_reports_dir(self) -> Path:
        return self.sois_repo / "reports"

    @property
    def sois_models_dir(self) -> Path:
        return self.sois_repo / "models"


def settings() -> Settings:
    return Settings()


def neo4j_driver(read_only: bool = False):
    """Admin driver by default; the reader account when read_only=True."""
    from neo4j import GraphDatabase

    s = settings()
    auth = (s.neo4j_reader_user, s.neo4j_reader_password) if read_only else (s.neo4j_user, s.neo4j_password)
    return GraphDatabase.driver(s.neo4j_uri, auth=auth, notifications_min_severity="OFF")


def pg_connect():
    import psycopg

    return psycopg.connect(settings().pg_dsn)
