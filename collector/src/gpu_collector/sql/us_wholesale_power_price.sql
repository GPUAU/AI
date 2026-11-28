-- ICE wholesale power market data republished by EIA.
CREATE TABLE IF NOT EXISTS public.us_wholesale_power_price (
    hub text NOT NULL,
    trade_date date NOT NULL,
    delivery_start_date date NOT NULL,
    delivery_end_date date NOT NULL,
    high_usd_mwh numeric,
    low_usd_mwh numeric,
    weighted_avg_usd_mwh numeric NOT NULL,
    change_usd_mwh numeric,
    daily_volume_mwh numeric CHECK (daily_volume_mwh >= 0),
    trade_count integer CHECK (trade_count >= 0),
    counterparty_count integer CHECK (counterparty_count >= 0),
    source_url text NOT NULL,
    source_file text NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
    source_row integer NOT NULL CHECK (source_row > 1),
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hub, trade_date, delivery_start_date, delivery_end_date),
    CHECK (delivery_start_date <= delivery_end_date)
);

COMMENT ON TABLE public.us_wholesale_power_price IS
    'EIA/ICE daily US wholesale power indexes. Rows describe traded delivery windows.';
COMMENT ON COLUMN public.us_wholesale_power_price.weighted_avg_usd_mwh IS
    'ICE volume-weighted wholesale market price in USD per MWh.';
