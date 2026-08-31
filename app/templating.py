"""Shared Jinja2 environment.

Both routers render from the same templates directory, and both need the
Keyrune set list — registering it as a global here keeps every route from
having to pass it into each TemplateResponse by hand.
"""
from fastapi.templating import Jinja2Templates

from .colors import combo_detail, combo_full_name, combo_kind, combo_name
from .mana import render_symbols
from .scryfall.keyrune import SUPPORTED_SETS

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["keyrune_sets"] = SUPPORTED_SETS
templates.env.filters["mana"] = render_symbols
templates.env.filters["combo_name"] = combo_name
templates.env.filters["combo_kind"] = combo_kind
templates.env.filters["combo_detail"] = combo_detail
templates.env.filters["combo_full_name"] = combo_full_name
