from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 默认值即 docker-compose 中的连接串，可用环境变量覆盖
    database_url: str = (
        "postgresql://royalty:royalty@db:5432/royalty"
    )
    db_min_size: int = 1
    db_max_size: int = 10
    # 小数比例最多保留的位数（百分比，4 位 = 0.0001%）
    percent_scale: int = 4
    # 协议修订预览令牌（amendment_preview_token）的 HMAC 签名密钥。
    # 默认值可直接单机运行；多实例/生产请通过环境变量覆盖为同一密钥。
    amendment_token_secret: str = "royalty-amendment-preview-default-secret"

    class Config:
        env_file = ".env"
        env_prefix = ""

    @field_validator("amendment_token_secret", mode="before")
    @classmethod
    def _empty_secret_uses_default(cls, v):
        # compose 透传时未设置会展开为空串：空串视为未设置，回退内置默认值
        if v is None or (isinstance(v, str) and v == ""):
            return cls.model_fields["amendment_token_secret"].default
        return v


settings = Settings()
