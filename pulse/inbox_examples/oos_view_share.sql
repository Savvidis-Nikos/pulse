-- oos_view_share: share of product page views that land on products that are out of stock (per category)
-- from the product sales feed; the merch team watches this every morning
SELECT ts, category,
       SUM(CASE WHEN in_stock = 0 THEN views ELSE 0 END) AS num,
       SUM(views) AS den
FROM product_sales
GROUP BY ts, category
