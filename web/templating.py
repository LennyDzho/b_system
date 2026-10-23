"""Configure reusable Jinja rendering and presentation-only Decimal formatting."""

from decimal import Decimal

from fastapi.templating import Jinja2Templates

from src.core.settings import BASE_DIR
from web.presentation import human_date, label, navigation

templates = Jinja2Templates(
    directory=BASE_DIR / "web" / "templates", context_processors=[navigation]
)


def money(value: str | None) -> str:
    """Format a decimal string without floating-point conversion or financial arithmetic."""
    if value is None:
        return "Не определено"
    text = format(Decimal(value), ",.2f").replace(",", " ")
    return text[:-3] if text.endswith(".00") else text.replace(".", ",")


templates.env.filters["money"] = money
templates.env.filters["human_date"] = human_date
templates.env.filters["label"] = label
