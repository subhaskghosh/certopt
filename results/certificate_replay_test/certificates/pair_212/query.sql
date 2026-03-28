SELECT DISTINCT
  a.email
FROM PERSON AS A
JOIN PERSON AS B
  ON a.email = b.email
WHERE
  a.id <> b.id