CREATE TABLE IF NOT EXISTS public.dram_export_price_index (
    observation_month date PRIMARY KEY CHECK (EXTRACT(DAY FROM observation_month) = 1),
    index_value numeric NOT NULL CHECK (index_value > 0),
    source_url text NOT NULL,
    collected_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.dram_export_price_index IS
    'Bank of Korea ECOS 402Y016 / 30911201AA / D: monthly DRAM export price index, US dollar basis, 2020=100.';
COMMENT ON COLUMN public.dram_export_price_index.observation_month IS
    'First calendar day of the reference month, not a daily observation.';
