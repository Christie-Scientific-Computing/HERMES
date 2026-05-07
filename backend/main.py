"""
FastAPI backend for Hermes. Exposes REST endpoints for importing, cleaning and exporting data
"""
import os
import logging
from fastapi import FastAPI
from dotenv import load_dotenv
from backend.src.import_.endpoints import router as import_router
from backend.src.database import setup_status_db

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


setup_status_db(os.getenv('STATUS_DB'))
app = FastAPI()
app.include_router(import_router)