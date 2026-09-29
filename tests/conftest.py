import os

# Default to in-memory SQLite during automated test runs so tests do not attempt
# to connect to remote VPC databases like AWS RDS when running locally.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
