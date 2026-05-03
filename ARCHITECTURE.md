# 🗂️ Architectur actuelle du projet : `BULLET-1` en developpement...

## 🌳 Arborescence

```
BULLET-1/
├── config/
│  ├── analytics_engine_config.json
│  ├── atr_config.json
│  ├── config.json
│  ├── credentials.json
│  ├── data_processor_config.json
│  ├── data_validator_config.json
│  ├── logger_config.json
│  ├── momentum_config.json
│  ├── order_simulator_config.json
│  ├── regime_config.json
│  ├── structure_config.json
│  ├── trend_config.json
│  ├── uncertainty_candle_config.json
│  ├── volatility_config.json
│  └── volume_config.json
├── data/
│  ├── historical/
│  │  └── BTC-USDT/
│  │     └── processed/
│  │        ├── 15min.csv
│  │        ├── 5min.csv
│  │        └── Notes.txt
│  ├── bullet1_market_data.db
│  ├── db_status.py
│  ├── download_data_multi_exchange_v2.3.py
│  ├── download_data_v3.0.py
│  └── migrate_csv_to_db.py
├── src/
│  ├── backtesting/
│  │  ├── __init__.py
│  │  ├── analytics_engine.py
│  │  ├── engine.py
│  │  ├── metrics.py
│  │  ├── ohlcv_data_engine.py
│  │  ├── optimizer.py
│  │  ├── order_simulator.py
│  │  ├── report_generator.py
│  │  └── trading_engine.py
│  ├── core/
│  │  ├── __init__.py
│  │  ├── day_trades_manager.py
│  │  ├── position_manager.py
│  │  ├── risk_manager.py
│  │  ├── session_manager.py
│  │  ├── signal_generator.py
│  │  └── strategy.py
│  ├── data/
│  │  ├── __init__.py
│  │  ├── data_loader.py
│  │  ├── data_processor.py
│  │  ├── data_validator.py
│  │  └── db_manager.py
│  ├── exchange/
│  │  ├── __init__.py
│  │  ├── base_client.py
│  │  ├── binance_client.py
│  │  └── paper_trading.py
│  ├── indicators/
│  │  ├── __init__.py
│  │  ├── atr.py
│  │  ├── momentum.py
│  │  ├── regime.py
│  │  ├── structure.py
│  │  ├── trend.py
│  │  ├── uncertainty_candle.py
│  │  ├── volatility.py
│  │  └── volume.py
│  ├── ml/
│  │  ├── __init__.py
│  │  └── market_context.py
│  ├── notifications/
│  │  ├── __init__.py
│  │  ├── discord_notifier.py
│  │  └── email_notifier.py
│  ├── trading/
│  │  ├── __init__.py
│  │  └── trading_bot.py
│  └── utils/
│     ├── __init__.py
│     ├── config_loader.py
│     ├── error_handler.py
│     ├── helpers.py
│     ├── logger.py
│     ├── performance_monitor.py
│     ├── state_manager.py
│     └── validator.py
├── AUDIT_BACKTEST.md
├── backtest.py
├── CHANGELOG.md
├── main.py
├── optimize.py
└── README.md
```

---