"""
FastAPI backend for Hermes. Exposes REST endpoints for importing, cleaning and exporting data
"""
import os
import logging
from fastapi import FastAPI
from dotenv import load_dotenv
from backend.src.retrieve.endpoints import router as import_router
from backend.src.export.endpoints import router as export_router
from backend.src.database import setup_status_db
from backend.src.results.endpoints import router as results_router

load_dotenv()

#TODO load config file
# Setup FastAPI logging
logging.basicConfig(
        filename=None, 
        level="INFO",
        format="[%(asctime)s] [%(levelname)s] (%(name)s:%(lineno)d) - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        )
logging.getLogger('httpx').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


STATUS_DB = os.getenv('STATUS_DB')
if STATUS_DB:
    setup_status_db(STATUS_DB)
else:
    logger.warning("STATUS_DB not set; ABORTING!")
    exit()
app = FastAPI()
app.include_router(import_router)
app.include_router(export_router)
app.include_router(results_router)