from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["Clients"], prefix="/clients")

_ROUTE_DIR = Path(__file__).resolve().parent
_STATUSLINE_PATH = _ROUTE_DIR.parents[1] / "clients" / "claude-code-statusline.sh"


@router.get(
    "/claude-code-statusline.sh",
    response_class=PlainTextResponse,
    responses={200: {"description": "Status line bash script"}},
)
def statusline_script() -> PlainTextResponse:
    """Serve the Claude Code status line script so users can curl it directly."""
    return PlainTextResponse(_STATUSLINE_PATH.read_text(), media_type="text/x-shellscript")
