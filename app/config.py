from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AWS China FinOps AI"
    app_version: str = "2.0.0"
    environment: str = "poc"
    data_dir: Path = Path("/data")
    database_path: Path = Path("/data/finops.db")
    account_b_role_arn: str = ""
    account_b_external_id: str = ""
    account_alias_a: str = "Linked Account A"
    account_alias_b: str = "Linked Account B"
    regions: str = "cn-north-1,cn-northwest-1"
    billing_region: str = "cn-northwest-1"
    pricing_region: str = "cn-northwest-1"
    athena_region: str = "cn-north-1"
    athena_database: str = "finops_phase1"
    athena_workgroup: str = "finops-phase1"
    athena_view: str = "cur_unified"
    quicksight_region: str = "cn-north-1"
    quicksight_dashboard_id: str = "cost_intelligence_dashboard"
    quicksight_user_arn: str = ""
    quicksight_allowed_domains: str = "http://localhost:8080"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_key_file: Path = Path("/run/secrets/deepseek_api_key")
    session_secret_file: Path = Path("/run/secrets/session_secret")
    admin_password_hash_file: Path = Path("/run/secrets/admin_password_hash")
    pseudonym_secret_file: Path = Path("/run/secrets/pseudonym_secret")
    session_ttl_seconds: int = 28800
    api_cache_ttl_seconds: int = 21600
    secure_cookie: bool = False

    model_config = SettingsConfigDict(env_prefix="FINOPS_", case_sensitive=False, extra="ignore")

    @property
    def region_list(self) -> list[str]:
        return [value.strip() for value in self.regions.split(",") if value.strip()]

    @property
    def allowed_domains(self) -> list[str]:
        return [value.strip() for value in self.quicksight_allowed_domains.split(",") if value.strip()]

    @staticmethod
    def read_secret(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @property
    def deepseek_api_key(self) -> str:
        return self.read_secret(self.deepseek_key_file)

    @property
    def session_secret(self) -> str:
        return self.read_secret(self.session_secret_file)

    @property
    def admin_password_hash(self) -> str:
        return self.read_secret(self.admin_password_hash_file)

    @property
    def pseudonym_secret(self) -> str:
        return self.read_secret(self.pseudonym_secret_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
