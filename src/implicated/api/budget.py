from __future__ import annotations

from fastapi import APIRouter, Response

from implicated.api.deps import Lib, call
from implicated.models import (
    Budget,
    BudgetDetail,
    BudgetIn,
    BudgetOverlap,
    Cart,
    CartItem,
    CartItemIn,
    CartOption,
    ShippingIn,
)

router = APIRouter(prefix="/budget", tags=["Budget"])


# The cart


@router.get("/cart")
def get_cart(lib: Lib) -> Cart:
    """The cart, with its total and the budget it is editing. Lines with ``owned`` are albums
    you already have on that format."""
    return lib.get_cart()


@router.delete("/cart", status_code=204)
def clear_cart(lib: Lib) -> Response:
    """Empty the cart; a budget it was editing stays as it was saved."""
    lib.clear_cart()
    return Response(status_code=204)


@router.put("/cart/shipping")
def set_shipping(lib: Lib, data: ShippingIn) -> Cart:
    """Say what shipping the cart costs in all; it adds up to the total."""
    return call(lib.set_shipping, data.shipping)


@router.post("/cart/save", status_code=201)
def make_budget(lib: Lib, data: BudgetIn) -> Budget:
    """Save the cart as a budget (back into the one it is editing, if any) and empty it."""
    return call(lib.make_budget, data)


@router.post("/cart/buy", status_code=201)
def buy_cart(lib: Lib, data: BudgetIn, drop_from_other_budgets: bool = False) -> Budget:
    """Save the cart as a budget and buy it: its albums become owned on their formats. With
    ``drop_from_other_budgets`` they also leave the other budgets (see ``/cart/overlaps``), and
    budgets left empty are deleted."""
    return call(lib.buy_cart, data, drop_from_other_budgets)


@router.get("/cart/overlaps")
def cart_overlaps(lib: Lib) -> list[BudgetOverlap]:
    """The other budgets still to buy holding some of the cart's albums on the same formats."""
    return lib.budget_overlaps()


@router.get("/options")
def cart_options(lib: Lib, format: str | None = None) -> list[CartOption]:
    """The albums and formats (or only ``format``) that can still go in the cart, the wanted
    ones first."""
    return call(lib.cart_options, format)


@router.post("/cart/items", status_code=201)
def add_to_cart(lib: Lib, data: CartItemIn) -> CartItem:
    return call(lib.add_to_cart, data)


@router.get("/cart/items/{item_id}")
def get_cart_item(lib: Lib, item_id: int) -> CartItem:
    return call(lib.get_cart_item, item_id)


@router.put("/cart/items/{item_id}")
def update_cart_item(lib: Lib, item_id: int, data: CartItemIn) -> CartItem:
    return call(lib.update_cart_item, item_id, data)


@router.delete("/cart/items/{item_id}", status_code=204)
def remove_from_cart(lib: Lib, item_id: int) -> Response:
    call(lib.remove_from_cart, item_id)
    return Response(status_code=204)


# Saved budgets


@router.get("/budgets")
def list_budgets(lib: Lib) -> list[BudgetDetail]:
    """Every budget, the ones still to buy first."""
    return lib.list_budgets()


@router.get("/budgets/{budget_id}")
def get_budget(lib: Lib, budget_id: int) -> BudgetDetail:
    return call(lib.get_budget_detail, budget_id)


@router.post("/budgets/{budget_id}/buy")
def buy_budget(lib: Lib, budget_id: int, drop_from_other_budgets: bool = False) -> Budget:
    """Mark the budget bought, and its albums owned on their formats. With
    ``drop_from_other_budgets`` they also leave the other budgets (see ``overlaps``), and
    budgets left empty are deleted."""
    return call(lib.buy_budget, budget_id, drop_from_other_budgets)


@router.get("/budgets/{budget_id}/overlaps")
def budget_overlaps(lib: Lib, budget_id: int) -> list[BudgetOverlap]:
    """The other budgets still to buy holding some of this one's albums on the same formats."""
    return call(lib.budget_overlaps, budget_id)


@router.post("/budgets/{budget_id}/edit")
def edit_budget(lib: Lib, budget_id: int) -> Cart:
    """Load the budget into the cart, replacing what it held; save the cart to keep changes."""
    return call(lib.edit_budget, budget_id)


@router.delete("/budgets/{budget_id}", status_code=204)
def delete_budget(lib: Lib, budget_id: int) -> Response:
    call(lib.delete_budget, budget_id)
    return Response(status_code=204)
