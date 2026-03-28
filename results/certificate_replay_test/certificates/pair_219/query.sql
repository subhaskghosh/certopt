SELECT DISTINCT
  p.email AS EMAIL
FROM PERSON AS P
JOIN PERSON AS D
  ON d.email = p.email
WHERE
  d.id <> p.id