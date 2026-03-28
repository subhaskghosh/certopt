SELECT DISTINCT
  p.email AS EMAIL
FROM PERSON AS P
JOIN PERSON AS Q
  ON p.email = q.email
WHERE
  p.id <> q.id