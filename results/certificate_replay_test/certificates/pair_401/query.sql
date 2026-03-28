SELECT
  customers.name AS CUSTOMERS
FROM CUSTOMERS
LEFT JOIN ORDERS
  ON customers.id = orders.customerid
WHERE
  customerid IS NULL