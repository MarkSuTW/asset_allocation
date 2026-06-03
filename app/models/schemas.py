from typing import Optional
from pydantic import BaseModel, Field, model_validator


class TransactionCreate(BaseModel):
    date: str
    stock_id: str
    action: str = Field(pattern="^(buy|sell)$")
    shares: float = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    transaction_tax: float = Field(default=0, ge=0)


class TransactionUpdate(BaseModel):
    date: str
    stock_id: str
    action: str = Field(pattern="^(buy|sell)$")
    shares: float = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    transaction_tax: float = Field(default=0, ge=0)


class AdvisorRequest(BaseModel):
    question: str


class TaxSettingsUpdate(BaseModel):
    stock_buy_tax_rate: float = Field(ge=0)
    stock_sell_tax_rate: float = Field(ge=0)
    etf_buy_tax_rate: float = Field(ge=0)
    etf_sell_tax_rate: float = Field(ge=0)
    bond_buy_tax_rate: float = Field(ge=0)
    bond_sell_tax_rate: float = Field(ge=0)


class NhiSettingsUpdate(BaseModel):
    nhi_supplement_rate: float = Field(ge=0)
    nhi_supplement_threshold: float = Field(ge=0)


class CashDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    pay_date: Optional[str] = None
    amount_per_share: float = Field(ge=0)
    holding_shares: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendCreate(BaseModel):
    stock_id: str
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float
    holding_shares: Optional[float] = Field(default=None, ge=0)
    bonus_shares: Optional[float] = None
    event_type: str = Field(default="stock_dividend")
    cash_return_per_share: float = Field(default=0, ge=0)
    cash_return_amount: Optional[float] = Field(default=None, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class StockDividendUpdate(BaseModel):
    ex_date: str
    allot_date: Optional[str] = None
    ratio: float
    holding_shares: float = Field(ge=0)
    bonus_shares: float
    event_type: str = Field(default="stock_dividend")
    cash_return_per_share: float = Field(default=0, ge=0)
    cash_return_amount: float = Field(default=0, ge=0)
    source: str = Field(default="manual")
    note: str = Field(default="")


class LoanCreate(BaseModel):
    lender: str
    collateral: str
    collateral_lots: float = Field(default=0, ge=0)
    principal: float = Field(ge=0)
    interest_rate: float = Field(ge=0)
    start_date: str
    due_date: Optional[str] = None
    note: str = Field(default="")

    @model_validator(mode="after")
    def pure_collateral_must_have_zero_rate(self) -> "LoanCreate":
        if self.principal == 0 and self.interest_rate > 0:
            raise ValueError("純擔保（借款金額=0）利率必須為 0")
        return self


class LoanUpdate(BaseModel):
    lender: str
    collateral: str
    collateral_lots: float = Field(default=0, ge=0)
    principal: float = Field(ge=0)
    interest_rate: float = Field(ge=0)
    start_date: str
    due_date: Optional[str] = None
    note: str = Field(default="")

    @model_validator(mode="after")
    def pure_collateral_must_have_zero_rate(self) -> "LoanUpdate":
        if self.principal == 0 and self.interest_rate > 0:
            raise ValueError("純擔保（借款金額=0）利率必須為 0")
        return self
