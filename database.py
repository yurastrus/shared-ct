"""Camera-traps database: engine/session singletons and init helpers (separate ct_db)."""

import threading
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.declarative import declarative_base
from flask import current_app

# Singleton state.
_ct_engine = None
_ct_session_factory = None
_engine_lock = threading.Lock()

# Declarative base for camera-traps models.
CTBase = declarative_base()

def get_ct_engine():
    """Return the singleton SQLAlchemy engine for the camera-traps database."""
    global _ct_engine

    if _ct_engine is None:
        with _engine_lock:
            if _ct_engine is None:
                _ct_engine = create_engine(
                    current_app.config['CT_DATABASE_URI'],
                    poolclass=QueuePool,
                    pool_size=5,
                    max_overflow=10,
                    pool_timeout=30,
                    pool_recycle=300,
                    pool_pre_ping=True,
                    echo=current_app.config.get('SQLALCHEMY_ECHO', False)  # log SQL in debug mode
                )

    return _ct_engine

def get_ct_session():
    """Return a scoped session bound to the camera-traps engine."""
    global _ct_session_factory

    if _ct_session_factory is None:
        engine = get_ct_engine()
        session_factory = sessionmaker(bind=engine)
        _ct_session_factory = scoped_session(session_factory)

    return _ct_session_factory()

def close_ct_session():
    """Remove the current camera-traps scoped session."""
    if _ct_session_factory:
        _ct_session_factory.remove()

def init_ct_database():
    """Initialize the camera-traps database, creating tables if they do not exist."""
    try:
        engine = get_ct_engine()

        # Probe the connection.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # Create tables only if missing.
        CTBase.metadata.create_all(engine)

        current_app.logger.info("Camera Traps database initialized successfully")

    except Exception as e:
        current_app.logger.error(f"Failed to initialize Camera Traps database: {e}")
        raise

def test_ct_connection():
    """Test connectivity to the camera-traps database; return (ok, message)."""
    try:
        engine = get_ct_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version()"))
            version = result.fetchone()[0]
            return True, f"Connected to PostgreSQL: {version}"
    except Exception as e:
        return False, f"Connection failed: {str(e)}"
