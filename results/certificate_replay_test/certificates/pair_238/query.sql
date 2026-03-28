SELECT
  p.email
FROM PERSON AS P
WHERE
  P.ID IN (
    SELECT
      P1.ID
    FROM PERSON AS P1
    WHERE
      1 < (
        SELECT
          COUNT(*)
        FROM PERSON AS P2
        WHERE
          P1.EMAIL = P2.EMAIL
      )
  )
GROUP BY
  p.email