SELECT
  name AS CUSTOMERS
FROM CUSTOMERS
LEFT JOIN ORDERS
  ON customers.id = orders.customerid
WHERE
  orders.id IS NULL