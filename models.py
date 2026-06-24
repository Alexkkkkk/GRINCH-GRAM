import uuid
from datetime import datetime
from database import db


class UserWallet(db.Model):
    __tablename__ = "user_wallets"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False,
                      default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), default="")
    ton_address = db.Column(db.String(120), nullable=False)
    encrypted_mnemonic = db.Column(db.Text, nullable=False)
    trade_amount = db.Column(db.Float, default=1.0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    total_trades = db.Column(db.Integer, default=0)
    winning_trades = db.Column(db.Integer, default=0)
    total_fee_paid = db.Column(db.Float, default=0.0)
    total_pnl_ton = db.Column(db.Float, default=0.0)
    last_signal_at = db.Column(db.DateTime, nullable=True)
