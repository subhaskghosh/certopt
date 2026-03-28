SELECT
  c.name AS CUSTOMERS
FROM CUSTOMERS AS C
WHERE
  NOT EXISTS(
    (
      SELECT
        *
      FROM ORDERS AS O
      WHERE
        C.ID = O.CUSTOMERID
    )
  )