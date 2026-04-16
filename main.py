from typing import Literal
from datetime import datetime
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from sqlalchemy import create_engine, Column, Integer, String, Float, text
from sqlalchemy.orm import sessionmaker, declarative_base
import csv
import io
import os

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# DB 설정
DB_PATH = os.getenv("DB_PATH", "./stock.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# DB 테이블 정의
class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String, nullable=False)
    stock_name = Column(String, nullable=False)
    trade_type = Column(String, nullable=False)  # buy or sell
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    trade_date = Column(String, nullable=False)


class StockPrice(Base):
    __tablename__ = "stock_prices"

    stock_code = Column(String, primary_key=True, index=True)
    stock_name = Column(String)
    current_price = Column(Float)


# 테이블 생성
Base.metadata.create_all(bind=engine)


def ensure_trade_date_column():
    with engine.connect() as conn:
        columns = conn.execute(text("PRAGMA table_info(trades)")).fetchall()
        column_names = {row[1] for row in columns}
        if "trade_date" not in column_names:
            conn.execute(
                text(
                    "ALTER TABLE trades "
                    "ADD COLUMN trade_date TEXT NOT NULL DEFAULT '1970-01-01'"
                )
            )
            conn.commit()


ensure_trade_date_column()


# 요청 데이터 형식
class TradeCreate(BaseModel):
    stock_code: str
    stock_name: str
    trade_type: Literal["buy", "sell"]
    quantity: int
    price: float
    trade_date: str

    @field_validator("stock_code", "stock_name")
    @classmethod
    def validate_text(cls, v: str):
        value = v.strip()
        if not value:
            raise ValueError("빈 값은 허용되지 않습니다.")
        return value

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int):
        if v <= 0:
            raise ValueError("수량은 1 이상이어야 합니다.")
        return v

    @field_validator("price")
    @classmethod
    def validate_price(cls, v: float):
        if v <= 0:
            raise ValueError("가격은 0보다 커야 합니다.")
        return v

    @field_validator("trade_date")
    @classmethod
    def validate_trade_date(cls, v: str):
        value = v.strip()
        if not value:
            raise ValueError("거래일은 비워둘 수 없습니다.")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("거래일 형식은 YYYY-MM-DD 이어야 합니다.") from exc
        return value


class StockPriceCreate(BaseModel):
    stock_code: str
    stock_name: str
    current_price: float

    @field_validator("stock_code", "stock_name")
    @classmethod
    def validate_text(cls, v: str):
        value = v.strip()
        if not value:
            raise ValueError("빈 값은 허용되지 않습니다.")
        return value

    @field_validator("current_price")
    @classmethod
    def validate_current_price(cls, v: float):
        if v < 0:
            raise ValueError("현재가는 0 이상이어야 합니다.")
        return v


def calculate_holding_quantity(db, stock_code: str, exclude_trade_id: int | None = None) -> int:
    query = db.query(Trade).filter(Trade.stock_code == stock_code).order_by(Trade.trade_date, Trade.id)

    if exclude_trade_id is not None:
        query = query.filter(Trade.id != exclude_trade_id)

    trades = query.all()

    quantity = 0
    for trade in trades:
        if trade.trade_type == "buy":
            quantity += trade.quantity
        elif trade.trade_type == "sell":
            quantity -= trade.quantity

    return quantity


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={}
    )


# 거래 저장 API
@app.post("/trades")
def create_trade(trade: TradeCreate):
    db = SessionLocal()

    if trade.trade_type == "sell":
        holding_qty = calculate_holding_quantity(db, trade.stock_code)
        if trade.quantity > holding_qty:
            db.close()
            raise HTTPException(status_code=400, detail="보유 수량보다 많은 매도는 불가능합니다.")

    new_trade = Trade(
        stock_code=trade.stock_code,
        stock_name=trade.stock_name,
        trade_type=trade.trade_type,
        quantity=trade.quantity,
        price=trade.price,
        trade_date=trade.trade_date,
    )

    db.add(new_trade)
    db.commit()
    db.refresh(new_trade)
    db.close()

    return {"message": "거래 저장 완료", "id": new_trade.id}


# 거래 조회 API
@app.get("/trades")
def get_trades():
    db = SessionLocal()
    trades = db.query(Trade).order_by(Trade.trade_date, Trade.id).all()
    db.close()

    return trades


@app.get("/trades/{trade_id}")
def get_trade(trade_id: int):
    db = SessionLocal()
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    db.close()

    if not trade:
        raise HTTPException(status_code=404, detail="해당 거래를 찾을 수 없습니다.")

    return trade


@app.put("/trades/{trade_id}")
def update_trade(trade_id: int, trade_data: TradeCreate):
    db = SessionLocal()
    trade = db.query(Trade).filter(Trade.id == trade_id).first()

    if not trade:
        db.close()
        raise HTTPException(status_code=404, detail="해당 거래를 찾을 수 없습니다.")

    if trade_data.trade_type == "sell":
        holding_qty = calculate_holding_quantity(
            db,
            trade_data.stock_code,
            exclude_trade_id=trade_id
        )
        if trade_data.quantity > holding_qty:
            db.close()
            raise HTTPException(status_code=400, detail="보유 수량보다 많은 매도는 불가능합니다.")

    trade.stock_code = trade_data.stock_code
    trade.stock_name = trade_data.stock_name
    trade.trade_type = trade_data.trade_type
    trade.quantity = trade_data.quantity
    trade.price = trade_data.price
    trade.trade_date = trade_data.trade_date

    db.commit()
    db.refresh(trade)
    db.close()

    return {"message": "거래 수정 완료", "id": trade.id}


@app.get("/portfolio")
def get_portfolio():
    db = SessionLocal()
    trades = db.query(Trade).order_by(Trade.trade_date, Trade.id).all()
    prices = db.query(StockPrice).all()
    db.close()

    price_map = {price.stock_code: price.current_price for price in prices}

    portfolio = {}

    for trade in trades:
        code = trade.stock_code

        if code not in portfolio:
            portfolio[code] = {
                "stock_code": trade.stock_code,
                "stock_name": trade.stock_name,
                "quantity": 0,
                "total_cost": 0.0,
                "avg_price": 0.0,
                "realized_profit": 0.0,
            }

        item = portfolio[code]

        if trade.trade_type == "buy":
            buy_amount = trade.quantity * trade.price
            item["quantity"] += trade.quantity
            item["total_cost"] += buy_amount

            if item["quantity"] > 0:
                item["avg_price"] = item["total_cost"] / item["quantity"]

        elif trade.trade_type == "sell":
            if item["quantity"] <= 0:
                continue

            sell_quantity = trade.quantity

            if sell_quantity > item["quantity"]:
                raise ValueError(f"{code} 종목의 매도 수량이 보유 수량보다 많습니다.")

            avg_price = item["avg_price"]
            cost_of_sold = avg_price * sell_quantity
            sell_amount = trade.price * sell_quantity
            profit = sell_amount - cost_of_sold

            item["realized_profit"] += profit
            item["quantity"] -= sell_quantity
            item["total_cost"] -= cost_of_sold

            if item["quantity"] > 0:
                item["avg_price"] = item["total_cost"] / item["quantity"]
            else:
                item["avg_price"] = 0.0
                item["total_cost"] = 0.0

    result = []

    for item in portfolio.values():
        current_price = price_map.get(item["stock_code"], 0.0)
        eval_amount = current_price * item["quantity"]
        unrealized_profit = eval_amount - item["total_cost"]

        profit_rate = 0.0
        if item["total_cost"] > 0:
            profit_rate = (unrealized_profit / item["total_cost"]) * 100

        result.append({
            "stock_code": item["stock_code"],
            "stock_name": item["stock_name"],
            "quantity": item["quantity"],
            "avg_price": round(item["avg_price"], 2),
            "total_cost": round(item["total_cost"], 2),
            "realized_profit": round(item["realized_profit"], 2),
            "current_price": round(current_price, 2),
            "eval_amount": round(eval_amount, 2),
            "unrealized_profit": round(unrealized_profit, 2),
            "profit_rate": round(profit_rate, 2),
        })

    return result


@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: int):
    db = SessionLocal()
    trade = db.query(Trade).filter(Trade.id == trade_id).first()

    if not trade:
        db.close()
        raise HTTPException(status_code=404, detail="해당 거래를 찾을 수 없습니다.")

    db.delete(trade)
    db.commit()
    db.close()

    return {"message": "거래 삭제 완료"}


@app.post("/prices")
def save_price(price_data: StockPriceCreate):
    db = SessionLocal()

    stock_price = db.query(StockPrice).filter(
        StockPrice.stock_code == price_data.stock_code
    ).first()

    if stock_price:
        stock_price.stock_name = price_data.stock_name
        stock_price.current_price = price_data.current_price
    else:
        stock_price = StockPrice(
            stock_code=price_data.stock_code,
            stock_name=price_data.stock_name,
            current_price=price_data.current_price
        )
        db.add(stock_price)

    db.commit()
    db.close()

    return {"message": "현재가 저장 완료"}


@app.get("/prices")
def get_prices():
    db = SessionLocal()
    prices = db.query(StockPrice).all()
    db.close()
    return prices


@app.get("/summary")
def get_summary():
    portfolio = get_portfolio()

    total_cost = sum(item["total_cost"] for item in portfolio)
    total_eval_amount = sum(item["eval_amount"] for item in portfolio)
    total_realized_profit = sum(item["realized_profit"] for item in portfolio)
    total_unrealized_profit = sum(item["unrealized_profit"] for item in portfolio)

    total_profit_rate = 0.0
    if total_cost > 0:
        total_profit_rate = (total_unrealized_profit / total_cost) * 100

    return {
        "total_cost": round(total_cost, 2),
        "total_eval_amount": round(total_eval_amount, 2),
        "total_realized_profit": round(total_realized_profit, 2),
        "total_unrealized_profit": round(total_unrealized_profit, 2),
        "total_profit_rate": round(total_profit_rate, 2),
    }


@app.get("/export/trades")
def export_trades_csv():
    db = SessionLocal()
    trades = db.query(Trade).order_by(Trade.trade_date, Trade.id).all()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["id", "stock_code", "stock_name", "trade_type", "quantity", "price", "trade_date"])

    for trade in trades:
        writer.writerow([
            trade.id,
            trade.stock_code,
            trade.stock_name,
            trade.trade_type,
            trade.quantity,
            trade.price,
            trade.trade_date,
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"}
    )


@app.post("/import/trades")
async def import_trades_csv(file: UploadFile = File(...)):
    db = SessionLocal()
    imported_count = 0

    try:
        content = await file.read()
        decoded = content.decode("utf-8-sig")
        csv_file = io.StringIO(decoded)
        reader = csv.DictReader(csv_file)

        required_columns = {"stock_code", "stock_name", "trade_type", "quantity", "price", "trade_date"}
        if not reader.fieldnames or not required_columns.issubset(set(reader.fieldnames)):
            raise HTTPException(status_code=400, detail="거래 CSV 헤더가 올바르지 않습니다.")

        holding_cache: dict[str, int] = {}

        for idx, row in enumerate(reader, start=2):
            try:
                stock_code = row["stock_code"].strip()
                stock_name = row["stock_name"].strip()
                trade_type = row["trade_type"].strip().lower()
                quantity = int(row["quantity"])
                price = float(row["price"])
                trade_date = row["trade_date"].strip()

                if not stock_code or not stock_name:
                    raise ValueError("종목코드/종목명이 비어 있습니다.")
                if trade_type not in ("buy", "sell"):
                    raise ValueError("trade_type은 buy 또는 sell 이어야 합니다.")
                if quantity <= 0:
                    raise ValueError("수량은 1 이상이어야 합니다.")
                if price <= 0:
                    raise ValueError("가격은 0보다 커야 합니다.")
                try:
                    datetime.strptime(trade_date, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError("거래일 형식은 YYYY-MM-DD 이어야 합니다.") from exc

                if stock_code not in holding_cache:
                    holding_cache[stock_code] = calculate_holding_quantity(db, stock_code)

                if trade_type == "sell":
                    if quantity > holding_cache[stock_code]:
                        raise ValueError("보유 수량보다 많은 매도는 불가능합니다.")
                    holding_cache[stock_code] -= quantity
                else:
                    holding_cache[stock_code] += quantity
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"{idx}행 오류: {str(e)}")

            trade = Trade(
                stock_code=stock_code,
                stock_name=stock_name,
                trade_type=trade_type,
                quantity=quantity,
                price=price,
                trade_date=trade_date,
            )
            db.add(trade)
            imported_count += 1

        db.commit()
    finally:
        db.close()

    return {"message": "거래 CSV 불러오기 완료", "count": imported_count}


@app.get("/export/prices")
def export_prices_csv():
    db = SessionLocal()
    prices = db.query(StockPrice).order_by(StockPrice.stock_code).all()
    db.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["stock_code", "stock_name", "current_price"])

    for price in prices:
        writer.writerow([
            price.stock_code,
            price.stock_name,
            price.current_price
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=prices.csv"}
    )


@app.post("/import/prices")
async def import_prices_csv(file: UploadFile = File(...)):
    db = SessionLocal()

    content = await file.read()
    decoded = content.decode("utf-8-sig")
    csv_file = io.StringIO(decoded)
    reader = csv.DictReader(csv_file)

    imported_count = 0

    for row in reader:
        stock_price = db.query(StockPrice).filter(
            StockPrice.stock_code == row["stock_code"]
        ).first()

        if stock_price:
            stock_price.stock_name = row["stock_name"]
            stock_price.current_price = float(row["current_price"])
        else:
            stock_price = StockPrice(
                stock_code=row["stock_code"],
                stock_name=row["stock_name"],
                current_price=float(row["current_price"])
            )
            db.add(stock_price)

        imported_count += 1

    db.commit()
    db.close()

    return {"message": "현재가 CSV 불러오기 완료", "count": imported_count}
