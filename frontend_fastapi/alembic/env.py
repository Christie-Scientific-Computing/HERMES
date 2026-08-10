import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from frontend_fastapi.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# disable_existing_loggers=False -- the fileConfig default (True) would
# silently disable every logger already created and not listed in
# alembic.ini's [loggers] section (uvicorn's, every frontend_fastapi.*
# module's, ...) whenever this runs from main.py's startup migration
# rather than the standalone `alembic` CLI. Mirrors backend/alembic/env.py.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# HERMES_FRONTEND_DATABASE_URL always wins over whatever's in alembic.ini --
# keeps the DSN out of a checked-in config file. Mirrors backend/alembic/
# env.py's identical DATABASE_URL override, and is deliberately a DIFFERENT
# env var: this project's local DB is never HermesDB (see settings.py).
database_url = os.getenv("HERMES_FRONTEND_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

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
    url = config.get_main_option("sqlalchemy.url")
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
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
