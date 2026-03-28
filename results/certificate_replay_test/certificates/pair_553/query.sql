SELECT
  c.name AS CUSTOMERS
FROM CUSTOMERS AS C
LEFT JOIN ORDERS AS O
  ON c.id = o.customerid
GROUP BY
  c.id
HAVING
  COUNT(o.customerid) < 1