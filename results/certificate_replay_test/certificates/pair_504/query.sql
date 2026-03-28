SELECT
  cust.name AS CUSTOMERS
FROM CUSTOMERS AS CUST
LEFT JOIN ORDERS AS ORD
  ON cust.id = ord.customerid
WHERE
  ord.customerid IS NULL