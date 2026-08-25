-- Replace database/table placeholders before execution.
-- Both raw CUR tables must expose the same columns.

CREATE OR REPLACE VIEW finops.cur_unified AS
SELECT
  'Linked Account A' AS linked_account_alias,
  source.*
FROM finops.cur_account_a_raw AS source
UNION ALL
SELECT
  'Linked Account B' AS linked_account_alias,
  source.*
FROM finops.cur_account_b_raw AS source;

CREATE OR REPLACE VIEW finops.v_dashboard_cost_daily AS
SELECT
  linked_account_alias,
  CAST(line_item_usage_start_date AS date) AS usage_date,
  COALESCE(product_product_name, line_item_product_code, 'Other') AS service_name,
  COALESCE(product_region, line_item_availability_zone, 'Global') AS region_name,
  SUM(CAST(line_item_unblended_cost AS decimal(38, 8))) AS unblended_cost,
  SUM(CAST(line_item_usage_amount AS decimal(38, 8))) AS usage_amount,
  MAX(bill_billing_currency) AS currency
FROM finops.cur_unified
WHERE line_item_line_item_type NOT IN ('Tax')
GROUP BY 1, 2, 3, 4;

-- QuickSight custom SQL example.
SELECT
  usage_date,
  linked_account_alias,
  service_name,
  region_name,
  currency,
  unblended_cost
FROM finops.v_dashboard_cost_daily
WHERE usage_date >= date_add('month', -6, current_date);
