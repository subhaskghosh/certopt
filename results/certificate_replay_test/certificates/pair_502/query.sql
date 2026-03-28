SELECT
  c.name AS CUSTOMERS
FROM CUSTOMERS AS C
LEFT JOIN ORDERS AS O
  ON c.id = o.customerid
GROUP BY
  c.id,
  c.name
HAVING
  COUNT(o.id) = 0