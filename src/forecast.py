"""Sales & covers projections per location (Sep 2026 - Aug 2027).

Model: log-linear regression on daily data (statsmodels OLS)
    log(y) = trend + day-of-week + annual Fourier seasonality + holiday effects
             + menu-price step + new-store ramp
Brickell (L06) has only 18 months of history, so it borrows the annual
seasonal shape estimated from the five mature sister stores instead of
estimating it from a single, ramp-confounded cycle.

Accuracy is checked with a holdout backtest (train through May 2026, test
Jun-Aug 2026) against a seasonal-naive baseline (same month last year x YoY growth).
Prediction bands come from a 7-day block bootstrap of residuals (P10 / P50 / P90).

Run:  python src/forecast.py
"""
from datetime import date, timedelta
import json

import duckdb
import numpy as np
import pandas as pd
import statsmodels.api as sm

from config import WAREHOUSE, MARTS, END, FORECAST_MONTHS, MENU_PRICE_INCREASE_DATE, SEED

rng = np.random.default_rng(SEED)
K_FOURIER = 3
BACKTEST_START = pd.Timestamp("2026-06-01")

HOLIDAYS = {  # name -> dates (history + forecast horizon)
    "valentines": ["2025-02-14", "2026-02-14", "2027-02-14"],
    "mothers_day": ["2025-05-11", "2026-05-10", "2027-05-09"],
    "nye": ["2024-12-31", "2025-12-31", "2026-12-31"],
    "july4": ["2025-07-04", "2026-07-04", "2027-07-04"],
}
CLOSED_FUTURE = {pd.Timestamp("2026-11-26"), pd.Timestamp("2026-12-25")}
EPOCH = pd.Timestamp("2024-09-01")


def fourier(dates: pd.DatetimeIndex) -> pd.DataFrame:
    doy = dates.dayofyear.to_numpy() / 365.25
    cols = {}
    for k in range(1, K_FOURIER + 1):
        cols[f"sin{k}"] = np.sin(2 * np.pi * k * doy)
        cols[f"cos{k}"] = np.cos(2 * np.pi * k * doy)
    return pd.DataFrame(cols, index=dates)


def design(dates: pd.DatetimeIndex, open_date: pd.Timestamp, with_season: bool, ramp: bool) -> pd.DataFrame:
    X = pd.DataFrame(index=dates)
    X["const"] = 1.0
    X["trend"] = (dates - EPOCH).days / 365.25
    for i, name in enumerate(["tue", "wed", "thu", "fri", "sat", "sun"], start=1):
        X[name] = (dates.dayofweek == i).astype(float)
    for h, ds in HOLIDAYS.items():
        X[h] = dates.isin(pd.to_datetime(ds)).astype(float)
    X["price_step"] = (dates >= pd.Timestamp(MENU_PRICE_INCREASE_DATE)).astype(float)
    if with_season:
        X = X.join(fourier(dates))
    if ramp:
        age = np.maximum((dates - open_date).days, 0)
        X["ramp"] = np.exp(-age / 130.0)          # decays to 0 as the store matures
        X["opening_buzz"] = (age < 21).astype(float)
    return X


def fit_location(df, open_date, chain_season=None, train_end=None):
    """Fit OLS for one location & metric. Returns (model, X_cols, uses_chain_season)."""
    d = df if train_end is None else df[df.index < train_end]
    ramp = (d.index.min() - open_date).days < 60
    y = np.log(d["y"])
    if chain_season is not None:          # young store: subtract borrowed seasonality, trend & price effect
        y = y - chain_season(d.index)
        X = design(d.index, open_date, with_season=False, ramp=ramp).drop(columns=["trend", "price_step"])
    else:
        X = design(d.index, open_date, with_season=True, ramp=ramp)
    model = sm.OLS(y, X).fit()
    return model, ramp


def predict(model, dates, open_date, ramp, chain_season=None):
    X = design(dates, open_date, with_season=chain_season is None, ramp=ramp)[model.params.index]
    lp = X @ model.params
    if chain_season is not None:
        lp = lp + chain_season(dates)
    return lp


def block_bootstrap(resid: np.ndarray, n: int, sims: int = 1000, block: int = 7) -> np.ndarray:
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, len(resid) - block, size=(sims, nb))
    idx = (starts[:, :, None] + np.arange(block)).reshape(sims, -1)[:, :n]
    return resid[idx]


def main():
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    daily = con.execute("SELECT location_id, business_date, net_sales, covers FROM mart_daily_ops").df()
    locs = con.execute("SELECT location_id, name, open_date FROM dim_locations ORDER BY 1").df()
    daily["business_date"] = pd.to_datetime(daily["business_date"])
    mature = [l for l, od in zip(locs.location_id, locs.open_date) if pd.Timestamp(od) < EPOCH]

    horizon = pd.date_range(pd.Timestamp(END) + timedelta(days=1), periods=400, freq="D")
    horizon = horizon[horizon < pd.Timestamp(END) + pd.DateOffset(months=FORECAST_MONTHS) + timedelta(days=1)]
    horizon = horizon[~horizon.isin(list(CLOSED_FUTURE))]

    forecasts, backtests, model_info = [], [], []
    for metric in ["net_sales", "covers"]:
        # 1) chain seasonal shape from mature stores (pooled, location fixed effects via demeaning)
        pooled = []
        for lid in mature:
            s = daily[daily.location_id == lid].set_index("business_date")[metric].rename("y").to_frame()
            m, _ = fit_location(s, pd.Timestamp("2019-01-01"))
            pooled.append(m.params[[c for c in m.params.index if c.startswith(("sin", "cos")) or c in ("trend", "price_step")]])
        season_coef = pd.concat(pooled, axis=1).mean(axis=1)

        def chain_season(dates, coef=season_coef):
            # borrowed components: annual seasonality + chain trend + chain price-step effect
            dates = pd.DatetimeIndex(dates)
            X = fourier(dates)
            X["trend"] = (dates - EPOCH).days / 365.25
            X["price_step"] = (dates >= pd.Timestamp(MENU_PRICE_INCREASE_DATE)).astype(float)
            return X[coef.index] @ coef

        for lid, name, od in locs.itertuples(index=False):
            od = pd.Timestamp(od)
            s = daily[daily.location_id == lid].set_index("business_date")[metric].rename("y").to_frame()
            cs = chain_season if lid not in mature else None

            # ---- backtest
            m_bt, ramp_bt = fit_location(s, od, cs, train_end=BACKTEST_START)
            test = s[s.index >= BACKTEST_START]
            pred = np.exp(predict(m_bt, test.index, od, ramp_bt, cs) + m_bt.mse_resid / 2)
            bt = pd.DataFrame({"actual": test["y"], "pred": pred})
            bt_m = bt.groupby(bt.index.to_period("M")).sum()
            # seasonal naive: same month last year x trailing YoY growth (Mar-May)
            ly = s.groupby(s.index.to_period("M"))["y"].sum()
            yoy = ly.loc["2026-03":"2026-05"].sum() / ly.loc["2025-03":"2025-05"].sum()
            bt_m["naive"] = [ly.get(p - 12, np.nan) * yoy for p in bt_m.index]
            for p, r in bt_m.iterrows():
                backtests.append(dict(location_id=lid, metric=metric, month=str(p), actual=r.actual,
                                      model=r.pred, naive=r.naive))

            # ---- final fit on all history and forecast
            m, ramp = fit_location(s, od, cs)
            lp = predict(m, horizon, od, ramp, cs)
            resid = m.resid.to_numpy()
            sims = np.exp(lp.to_numpy()[None, :] + block_bootstrap(resid, len(horizon)))
            sim_df = pd.DataFrame(sims.T, index=horizon)
            monthly = sim_df.groupby(horizon.to_period("M")).sum()
            for p, row in monthly.iterrows():
                forecasts.append(dict(location_id=lid, metric=metric, month=p.to_timestamp().date().isoformat(),
                                      p10=float(np.percentile(row, 10)), p50=float(np.percentile(row, 50)),
                                      p90=float(np.percentile(row, 90))))
            if metric == "net_sales":
                pr = m.params if cs is None else pd.concat([m.params, season_coef])
                model_info.append(dict(location_id=lid, r2=round(m.rsquared, 3),
                                       annual_trend_pct=round((np.exp(pr["trend"]) - 1) * 100, 1),
                                       sat_uplift_pct=round((np.exp(pr["sat"]) - 1) * 100, 1),
                                       price_step_pct=round((np.exp(pr["price_step"]) - 1) * 100, 1),
                                       borrowed_seasonality=lid not in mature, n_days=len(s)))

    fc = pd.DataFrame(forecasts)
    bt = pd.DataFrame(backtests)
    MARTS.mkdir(parents=True, exist_ok=True)
    fc.to_csv(MARTS / "forecast_monthly.csv", index=False)
    bt.to_csv(MARTS / "forecast_backtest.csv", index=False)

    # accuracy summary
    b = bt[bt.metric == "net_sales"].copy()
    b["ape_model"] = (b.model - b.actual).abs() / b.actual
    b["ape_naive"] = (b.naive - b.actual).abs() / b.actual
    acc = b.groupby("location_id")[["ape_model", "ape_naive"]].mean().mul(100).round(1)
    acc.loc["CHAIN"] = [b.ape_model.mean() * 100, b.ape_naive.mean() * 100]
    acc = acc.round(1).reset_index().rename(columns={"ape_model": "mape_model", "ape_naive": "mape_naive"})
    acc.to_csv(MARTS / "forecast_accuracy.csv", index=False)
    pd.DataFrame(model_info).to_csv(MARTS / "forecast_model_info.csv", index=False)
    print(acc.to_string(index=False))
    print(pd.DataFrame(model_info).to_string(index=False))
    tot = fc[fc.metric == "net_sales"].groupby("month")[["p10", "p50", "p90"]].sum() / 1e6
    print(tot.round(2).to_string())


if __name__ == "__main__":
    main()
