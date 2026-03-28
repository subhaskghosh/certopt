SELECT
  customers.name AS CUSTOMERS
FROM CUSTOMERS
LEFT JOIN ORDERS
  ON customers.id = orders.customerid
GROUP BY
  customers.id
HAVING
  COUNT(orders.id) = 0