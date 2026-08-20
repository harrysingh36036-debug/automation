"""Read-only REST API over the MongoDB migration store.

Serves the records that were transferred out of Supabase so the laptop
inventory app can keep showing history (old sales, transfers, purchases,
repairs and sold laptops) after they leave the free-tier database.

Design rules:
  * READ ONLY — this API can never write, update or delete anything.
  * Credentials stay server-side (MONGODB_URI / READ_API_KEY from env).
  * Responses mirror the JSON shapes the app's Supabase RPCs return
    (timestamps as "YYYY-MM-DD HH:MM:SS", numeric values as numbers).
  * Optional bearer-token auth: when READ_API_KEY is set, every request
    must send ``Authorization: Bearer <key>``.

Run locally:
    pip install -r requirements.txt
    set MONGODB_URI=mongodb+srv://...
    set MONGODB_DATABASE=supabase_migration
    set READ_API_KEY=change-me
    uvicorn src.read_api:app --port 8000

Deploy free on Render via render.yaml (or any host that runs uvicorn).
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from pymongo.errors import PyMongoError

app = FastAPI(title="Laptop Inventory — MongoDB Read API", version="1.0.0")

# CORS is wide open because the consumer is a static GitHub Pages site; the
# bearer token (READ_API_KEY) is the actual gate.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["Authorization"],
)

_client: Optional[MongoClient] = None
_db = None


def _get_db():
    """Lazily create the MongoDB handle from environment variables."""
    global _client, _db
    if _db is not None:
        return _db
    uri = os.getenv("MONGODB_URI", "")
    name = os.getenv("MONGODB_DATABASE", "")
    if not uri or not name:
        raise HTTPException(status_code=500, detail="MONGODB_URI / MONGODB_DATABASE not configured")
    _client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
    _db = _client[name]
    return _db


def _require_auth(authorization: Optional[str] = Header(None)) -> None:
    """Enforce the bearer token when READ_API_KEY is configured."""
    key = os.getenv("READ_API_KEY", "")
    if not key:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != key:
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def _fmt(value: Any) -> Any:
    """Convert MongoDB values to the JSON shapes the app already expects."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _fmt(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_fmt(v) for v in value]
    return value


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Strip migration metadata and Mongo internals from a document."""
    out = {k: _fmt(v) for k, v in doc.items() if not k.startswith("_")}
    out.pop("_id", None)
    out.pop("_migration", None)
    return out


def _find(collection: str, query: Optional[Dict[str, Any]] = None, limit: int = 500, sort=None) -> List[Dict[str, Any]]:
    try:
        cursor = _get_db()[collection].find(query or {}, sort=sort, limit=limit)
        return [_clean(doc) for doc in cursor]
    except PyMongoError as exc:
        raise HTTPException(status_code=502, detail=f"MongoDB error: {exc}") from exc


def _stores_lookup() -> Dict[Any, str]:
    """Map store id -> store name (stores are mirrored, never deleted)."""
    lookup: Dict[Any, str] = {}
    for doc in _find("stores", limit=10000):
        sid = doc.get("id")
        if sid is not None:
            lookup[str(sid)] = doc.get("store_name", "")
    return lookup


# ---------------------------------------------------------------------------
# Health / status
# ---------------------------------------------------------------------------

@app.get("/health")
def health(authorization: Optional[str] = Header(None)) -> Dict[str, str]:
    _require_auth(authorization)
    return {"ok": "true"}


@app.get("/api/status")
def status(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(authorization)
    db = _get_db()
    counts = {}
    for name in ("laptops", "transferlogs", "sales", "purchases", "repairs", "stores", "brands", "vendors", "customers"):
        try:
            counts[name] = db[name].count_documents({})
        except PyMongoError:
            counts[name] = 0
    return {"collections": counts}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

@app.get("/api/laptops")
def get_laptops(
    q: Optional[str] = Query(None, description="Search brand / brand_model / serial_number"),
    status: Optional[str] = Query(None),
    store_id: Optional[int] = Query(None),
    limit: int = Query(200, le=1000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    """Return migrated (sold) laptops, mirroring app_get_laptops's payload."""
    _require_auth(authorization)

    query: Dict[str, Any] = {}
    if store_id is not None:
        query["current_store_id"] = store_id
    if status:
        query["status"] = status
    if q:
        needle = str(q).strip()
        if needle:
            query["$or"] = [
                {"brand": {"$regex": needle, "$options": "i"}},
                {"brand_model": {"$regex": needle, "$options": "i"}},
                {"serial_number": {"$regex": needle, "$options": "i"}},
            ]

    rows = _find("laptops", query, limit=limit, sort=[("updated_at", -1)])
    if not rows:
        return []

    # Enrich with current store name (join against the mirrored stores table).
    stores = _stores_lookup()
    ids = [r["id"] for r in rows]

    # Latest sale per laptop (for sold records).
    sales_by_laptop: Dict[Any, Dict[str, Any]] = {}
    try:
        sale_docs = _get_db()["sales"].find({"laptop_id": {"$in": ids}}).sort("sold_at", -1)
        for s in sale_docs:
            lid = s.get("laptop_id")
            if lid is not None and lid not in sales_by_laptop:
                sales_by_laptop[lid] = _clean(s)
    except PyMongoError:
        sales_by_laptop = {}

    customer_ids = [s.get("customer_id") for s in sales_by_laptop.values() if s.get("customer_id") is not None]
    customers: Dict[Any, str] = {}
    if customer_ids:
        try:
            for c in _get_db()["customers"].find({"id": {"$in": customer_ids}}):
                customers[str(c.get("id"))] = c.get("name", "")
        except PyMongoError:
            customers = {}

    out = []
    for r in rows:
        sale = sales_by_laptop.get(r.get("id"))
        r["current_store_name"] = stores.get(str(r.get("current_store_id")), "") if r.get("current_store_id") is not None else ""
        r["sale_price"] = sale.get("sale_price") if sale else None
        r["sale_customer_name"] = customers.get(str(sale.get("customer_id")), "") if sale and sale.get("customer_id") is not None else ""
        r["sold_at"] = sale.get("sold_at") if sale else None
        r["sold_by"] = sale.get("sold_by") if sale else None
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# History: transfer logs, sales, purchases, repairs
# ---------------------------------------------------------------------------

@app.get("/api/transferlogs")
def get_transfer_logs(
    limit: int = Query(200, le=1000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    rows = _find("transferlogs", limit=limit, sort=[("changed_at", -1)])
    stores = _stores_lookup()
    for r in rows:
        r["from_store_name"] = stores.get(str(r.get("from_store_id")), "") if r.get("from_store_id") is not None else ""
        r["to_store_name"] = stores.get(str(r.get("to_store_id")), "") if r.get("to_store_id") is not None else ""
    return rows


@app.get("/api/sales")
def get_sales(
    store_id: Optional[int] = Query(None),
    limit: int = Query(500, le=2000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    query: Dict[str, Any] = {}
    if store_id is not None:
        query["store_id"] = store_id
    rows = _find("sales", query, limit=limit, sort=[("sold_at", -1)])
    stores = _stores_lookup()
    for r in rows:
        r["store_name"] = stores.get(str(r.get("store_id")), "") if r.get("store_id") is not None else ""
    return rows


@app.get("/api/purchases")
def get_purchases(
    limit: int = Query(500, le=2000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    rows = _find("purchases", limit=limit, sort=[("purchased_at", -1)])
    stores = _stores_lookup()
    for r in rows:
        r["current_store_name"] = stores.get(str(r.get("current_store_id")), "") if r.get("current_store_id") is not None else ""
    return rows


@app.get("/api/repairs")
def get_repairs(
    status: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status
    return _find("repairs", query, limit=limit, sort=[("updated_at", -1)])


# ---------------------------------------------------------------------------
# Reference data (mirrored, never deleted from Supabase)
# ---------------------------------------------------------------------------

@app.get("/api/stores")
def get_stores(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    return _find("stores", limit=10000, sort=[("id", 1)])


@app.get("/api/brands")
def get_brands(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    return _find("brands", limit=10000, sort=[("name", 1)])


@app.get("/api/vendors")
def get_vendors(authorization: Optional[str] = Header(None)) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    return _find("vendors", limit=10000, sort=[("id", 1)])


@app.get("/api/customers")
def get_customers(
    q: Optional[str] = Query(None),
    limit: int = Query(500, le=2000),
    authorization: Optional[str] = Header(None),
) -> List[Dict[str, Any]]:
    _require_auth(authorization)
    query: Dict[str, Any] = {}
    if q:
        needle = str(q).strip()
        if needle:
            query["$or"] = [
                {"name": {"$regex": needle, "$options": "i"}},
                {"phone": {"$regex": needle, "$options": "i"}},
            ]
    return _find("customers", query, limit=limit, sort=[("id", 1)])


# ---------------------------------------------------------------------------
# Summaries (fallback when Supabase aggregates have nothing left to sum)
# ---------------------------------------------------------------------------

@app.get("/api/sales/summary")
def sales_summary(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(authorization)
    docs = _find("sales", limit=100000, sort=None)
    count = len(docs)
    total_sales = sum(float(d.get("sale_price") or 0) for d in docs)
    total_cost = sum(float(d.get("cost_price") or 0) for d in docs)
    total_profit = sum(float(d.get("profit") or 0) for d in docs)
    return {"count": count, "total_sales": total_sales, "total_profit": total_profit, "total_cost": total_cost}


@app.get("/api/purchases/summary")
def purchases_summary(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(authorization)
    docs = _find("purchases", limit=100000, sort=None)
    total_units = sum(int(d.get("quantity") or 0) for d in docs)
    total_rate = sum(float(d.get("purchase_rate") or 0) for d in docs)
    total_charges = sum(float(d.get("extra_charges") or 0) for d in docs)
    return {
        "total_units": total_units,
        "total_rate": total_rate,
        "total_charges": total_charges,
        "total_value": total_rate + total_charges,
    }


@app.get("/api/repairs/summary")
def repairs_summary(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    _require_auth(authorization)
    docs = _find("repairs", limit=100000, sort=None)
    pending = sum(1 for d in docs if d.get("status") == "Pending")
    in_progress = sum(1 for d in docs if d.get("status") == "In Progress")
    repaired = sum(1 for d in docs if d.get("status") == "Repaired")
    total_cost = sum(float(d.get("cost") or 0) for d in docs)
    return {
        "total": len(docs),
        "pending": pending,
        "in_progress": in_progress,
        "repaired": repaired,
        "total_cost": total_cost,
    }