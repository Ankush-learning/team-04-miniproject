import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    APP_NAME: str = "Intelligent Emergency Response System"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True
    
    # Database config - default to local sqlite+aiosqlite for guaranteed zero-credential setup,
    # allows seamless swap to postgresql+asyncpg when configured
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///C:/Projects/Intelligent-Emergency-Response/database/emergency_db.sqlite3")
    
    # Map & Simulation
    SIMULATION_TICK_SECONDS: float = 1.0
    DEFAULT_SPEED_LIMIT_KMH: float = 50.0
    CONGESTION_PENALTY_FACTOR: float = 2.5

    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()
