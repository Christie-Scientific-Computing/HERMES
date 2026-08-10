"""The single Jinja2Templates instance every router renders through."""
from fastapi.templating import Jinja2Templates

from frontend_fastapi.settings import TEMPLATES_DIR

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
