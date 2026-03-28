SELECT
  customers.name AS CUSTOMERS
FROM CUSTOMERS
LEFT JOIN (
  SELECT DISTINCT
    CUSTOMERID
  FROM ORDERS
) AS ORDS
  ON customers.id = ords.customerid
WHERE
  ords.customerid IS NULL