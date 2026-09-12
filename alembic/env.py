import os
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


def get_url() -> str:
    # Priority:
    # 1. URL from alembic config (passed via MigrationManager)
    # 2. environment variable
    # 3. default path
    config_url = config.get_main_option("sqlalchemy.url")
    if config_url and "memory.db" in config_url:
        return config_url

    data_dir = os.environ.get("MCP_MEMORY_DATA_DIR", str(Path.home() / ".mcp-ariel-memory"))
    db_path = Path(data_dir) / "memory.db"
    return f"sqlite:///{db_path}"


# Logging: deliberately NO fileConfig() here. The stock Alembic template
# calls logging.config.fileConfig(alembic.ini) whose default
# disable_existing_loggers=True SILENCES every ariel logger that exists at
# migration time — and migrations run inside the MCP server lifespan at
# startup, so all production logging died quietly (found 2026-09-12 via the
# hash-fallback telemetry being invisible in full-suite logs). Ariel owns
# its logging config; alembic inherits it.

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
