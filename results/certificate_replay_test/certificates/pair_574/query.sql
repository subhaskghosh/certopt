SELECT
  name AS CUSTOMERS
FROM CUSTOMERS
LEFT JOIN ORDERS
  ON customerid = customers.id
WHERE
  customerid IS NULL